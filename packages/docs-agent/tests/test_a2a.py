"""A2A server tests, including a real ADK RemoteA2aAgent round trip (in-process, no network)."""

import warnings
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from fasta2a.pydantic_ai import AgentWorker
from pydantic_ai.models.test import TestModel

from docs_agent.a2a_server import SpecAgentWorker, UtcStorage, WithDeps, build_app, render_text
from docs_agent.agent import Deps, DocsAnswer, Source, build_agent
from docs_agent.corpus import Corpus, KeywordRetriever, split_sections

warnings.filterwarnings("ignore", message=r"\[EXPERIMENTAL\]")

ANSWER = {
    "answer": "Run `uv tool install janitor-cli`.",
    "sources": [{"ref": "janitor/README.md", "heading": "Install"}],
    "confidence": "high",
}


@pytest.fixture
def deps() -> Deps:
    md = "# Install\n\nRun `uv tool install janitor-cli`."
    corpus = Corpus(split_sections("janitor", "README.md", md), {"janitor/README.md": md})
    return Deps(corpus, KeywordRetriever(corpus))


def fake_model() -> TestModel:
    return TestModel(call_tools=["search_docs"], custom_output_args=ANSWER)


# --- unit: the fasta2a fixes ---------------------------------------------------------


async def test_storage_timestamps_are_timezone_aware() -> None:
    storage = UtcStorage()
    msg = {"role": "user", "parts": [{"text": "hi"}], "message_id": "m1"}
    task = await storage.submit_task("ctx", msg)
    assert datetime.fromisoformat(task["status"]["timestamp"]).tzinfo is not None
    task = await storage.update_task(task["id"], "working")
    assert datetime.fromisoformat(task["status"]["timestamp"]).tzinfo is not None


async def test_first_streamed_chunk_creates_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = AsyncMock()
    monkeypatch.setattr(AgentWorker, "publish_artifact", sent)
    worker = SpecAgentWorker(agent=AsyncMock(), broker=AsyncMock(), storage=UtcStorage())
    art = {"artifact_id": "a1", "parts": []}

    await worker.publish_artifact("t", "c", art, append=True, last_chunk=False)
    await worker.publish_artifact("t", "c", art, append=True, last_chunk=False)
    await worker.publish_artifact("t", "c", art, append=False, last_chunk=True)
    await worker.publish_artifact("t", "c", art, append=True, last_chunk=False)  # new stream

    appends = [c.kwargs["append"] for c in sent.await_args_list]
    assert appends == [False, True, False, False]


def test_streaming_disabled_by_default() -> None:
    worker = SpecAgentWorker(agent=AsyncMock(), broker=AsyncMock(), storage=UtcStorage())
    assert worker._streaming is False
    streaming = SpecAgentWorker(
        agent=AsyncMock(), broker=AsyncMock(), storage=UtcStorage(), stream=True
    )
    assert streaming._streaming is None


def test_result_artifact_has_text_then_data_part() -> None:
    worker = SpecAgentWorker(agent=AsyncMock(), broker=AsyncMock(), storage=UtcStorage())
    parts = worker.build_artifacts(DocsAnswer.model_validate(ANSWER))[0]["parts"]
    assert "uv tool install" in parts[0]["text"]
    assert "janitor/README.md § Install" in parts[0]["text"]
    assert parts[1]["data"]["result"]["confidence"] == "high"


def test_render_text_without_sources() -> None:
    text = render_text(DocsAnswer(answer="Not covered.", sources=[], confidence="low"))
    assert "Sources" not in text
    assert "_confidence: low_" in text


async def test_with_deps_injects_deps(deps: Deps) -> None:
    result = await WithDeps(build_agent(fake_model()), deps).run("install jt?")
    assert result.output.sources == [Source(ref="janitor/README.md", heading="Install")]


# --- integration: ADK RemoteA2aAgent -> fasta2a docs-agent ------------------------------

BASE = "http://127.0.0.1:8101"  # ADK requires https or loopback http for agent cards


async def run_adk(remote, question: str) -> list:
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    runner = InMemoryRunner(agent=remote, app_name="t")
    session = await runner.session_service.create_session(app_name="t", user_id="u")
    msg = types.Content(role="user", parts=[types.Part(text=question)])
    return [e async for e in runner.run_async(user_id="u", session_id=session.id, new_message=msg)]


async def test_adk_remote_agent_round_trip(deps: Deps) -> None:
    from google.adk.agents.remote_a2a_agent import RemoteA2aAgent

    app = build_app(deps=deps, agent=build_agent(fake_model()), url=BASE)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
            card = (await client.get("/.well-known/agent-card.json")).json()
            assert card["name"] == "docs-agent"
            assert card["skills"][0]["id"] == "answer-from-docs"

            remote = RemoteA2aAgent(
                name="docs_agent",
                agent_card=f"{BASE}/.well-known/agent-card.json",
                httpx_client=client,
            )
            events = await run_adk(remote, "How do I install jt?")

    assert not [e.error_message for e in events if e.error_message]
    texts = [p.text for e in events if e.content for p in e.content.parts or [] if p.text]
    assert any("uv tool install janitor-cli" in t for t in texts)
    assert any("janitor/README.md" in t for t in texts)


async def test_adk_reports_error_when_docs_agent_is_down() -> None:
    from google.adk.agents.remote_a2a_agent import RemoteA2aAgent

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
        remote = RemoteA2aAgent(
            name="docs_agent",
            agent_card=f"{BASE}/.well-known/agent-card.json",
            httpx_client=client,
        )
        events = await run_adk(remote, "How do I install jt?")

    errors = [e.error_message for e in events if e.error_message]
    assert errors, "ADK should surface an error event when the remote agent is unreachable"


@pytest.mark.xfail(
    strict=True,
    reason="fasta2a 2.1.1 naive timestamps / append-first-chunk break ADK. If this passes, "
    "upstream fixed it: delete UtcStorage and SpecAgentWorker's append fix.",
)
async def test_canary_unpatched_fasta2a_with_adk(deps: Deps) -> None:
    from fasta2a.pydantic_ai import agent_to_a2a
    from google.adk.agents.remote_a2a_agent import RemoteA2aAgent

    app = agent_to_a2a(WithDeps(build_agent(fake_model()), deps), url=BASE)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
            remote = RemoteA2aAgent(
                name="docs_agent",
                agent_card=f"{BASE}/.well-known/agent-card.json",
                httpx_client=client,
            )
            events = await run_adk(remote, "How do I install jt?")

    assert not [e.error_message for e in events if e.error_message]
