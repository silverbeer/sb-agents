"""Specialist discovery, routing, escalation and thread follow-ups (SB-1476)."""

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.test import TestModel

from sb_support.agent import build_agent
from sb_support.app import ThreadMemory, answer, handle_mention, is_follow_up
from sb_support.specialists import Specialist, SpecialistAnswer, SpecialistError, discover

BASE = "http://127.0.0.1:8101"


@dataclass
class StubClient:
    confidence: str | None = "high"
    fail: bool = False
    asked: list[str] | None = None

    async def ask(self, question: str) -> SpecialistAnswer:
        (self.asked if self.asked is not None else []).append(question)
        if self.fail:
            raise SpecialistError("unreachable at http://x")
        return SpecialistAnswer(f"answer\n\n_confidence: {self.confidence}_", self.confidence)


def docs(client: StubClient) -> Specialist:
    return Specialist("docs-agent", "Answers from SB docs.", client, ["How do I deploy MT?"])


def routed_agent(client: StubClient):
    return build_agent(TestModel(call_tools=["ask_docs_agent"]), [docs(client)])


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list:
    from sb_obs import runs

    records: list = []
    monkeypatch.setattr(runs, "emit", records.append)
    return records


def fake_slack() -> MagicMock:
    return MagicMock(
        chat_postMessage=AsyncMock(), reactions_add=AsyncMock(), reactions_remove=AsyncMock()
    )


# --- discovery from agent cards ------------------------------------------------------


async def test_discover_builds_specialist_from_docs_agent_card() -> None:
    from docs_agent.a2a_server import build_app as build_docs_app
    from docs_agent.agent import Deps
    from docs_agent.agent import build_agent as build_docs_agent
    from docs_agent.corpus import Corpus, KeywordRetriever, split_sections

    corpus = Corpus(split_sections("j", "README.md", "# A\nb"), {"j/README.md": "# A\nb"})
    app = build_docs_app(
        Deps(corpus, KeywordRetriever(corpus)), build_docs_agent(TestModel()), BASE
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as http:
            [spec] = await discover([BASE], http)
    assert spec.name == "docs-agent"
    assert spec.tool_name == "ask_docs_agent"
    assert "markdown docs" in spec.tool_description
    assert spec.examples


async def test_discover_skips_unreachable_specialists() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
        assert await discover([BASE], http) == []


# --- routing + escalation ------------------------------------------------------------


async def test_confident_answer_is_routed_and_not_escalated(emitted: list) -> None:
    asked: list[str] = []
    reply = await answer(routed_agent(StubClient("high", asked=asked)), "deploy MT?")
    assert reply.escalation is None
    assert len(asked) == 1
    assert (emitted[0].routed_to, emitted[0].escalated) == ("docs-agent", False)


async def test_low_confidence_escalates_with_mention(
    emitted: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SB_SUPPORT_ESCALATE_TO", "U0HUMAN")
    slack = fake_slack()
    event = {"channel": "C1", "ts": "1.1", "text": "<@U0BOT> who is on call?"}

    await handle_mention(event, slack, routed_agent(StubClient("low")))

    text = slack.chat_postMessage.await_args.kwargs["text"]
    assert "Escalating to <@U0HUMAN>: docs-agent wasn't confident" in text
    slack.reactions_add.assert_any_await(channel="C1", timestamp="1.1", name="raising_hand")
    assert emitted[0].escalated is True


async def test_failed_specialist_still_replies_and_escalates(emitted: list) -> None:
    reply = await answer(routed_agent(StubClient(fail=True)), "deploy MT?")
    assert reply.escalation == "docs-agent failed"
    assert reply.text  # the model still answered
    assert emitted[0].outcome == "ok"


async def test_small_talk_without_tools_is_not_escalated(emitted: list) -> None:
    agent = build_agent(TestModel(call_tools=[], custom_output_text="hi!"), [docs(StubClient())])
    reply = await answer(agent, "hello")
    assert (reply.text, reply.escalation) == ("hi!", None)
    assert emitted[0].routed_to is None


# --- thread memory + follow-ups ------------------------------------------------------


async def test_follow_up_sees_the_thread_history(emitted: list) -> None:
    memory = ThreadMemory()
    agent = routed_agent(StubClient())
    await answer(agent, "how do I deploy MT?", memory, "t1")
    await answer(agent, "and roll it back?", memory, "t1")

    prompts = [
        p.content
        for m in memory.history("t1")
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    ]
    assert prompts == ["how do I deploy MT?", "and roll it back?"]
    assert memory.history("other") == []


def test_memory_trims_whole_runs_and_old_threads() -> None:
    memory = ThreadMemory(max_threads=2, max_runs=2)
    for i in range(3):
        memory.add("t1", [ModelRequest(parts=[UserPromptPart(content=f"q{i}")])])
    assert [m.parts[0].content for m in memory.history("t1")] == ["q1", "q2"]
    memory.add("t2", [])
    memory.add("t3", [])
    assert "t1" not in memory and "t3" in memory


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"thread_ts": "t1", "text": "and then?"}, True),
        ({"thread_ts": "t1", "text": "<@UBOT> and then?"}, False),  # app_mention handles it
        ({"thread_ts": "t9", "text": "unrelated thread"}, False),
        ({"text": "top-level message"}, False),
        ({"thread_ts": "t1", "text": "x", "bot_id": "B1"}, False),
        ({"thread_ts": "t1", "text": "x", "subtype": "message_changed"}, False),
    ],
)
def test_is_follow_up(event: dict, expected: bool) -> None:
    memory = ThreadMemory()
    memory.add("t1", [])
    assert is_follow_up(event, memory, "UBOT") is expected
