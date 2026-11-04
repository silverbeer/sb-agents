"""mt-agent over A2A, in-process: the card, the reply contract, and sb-support as the caller.

sb-support's `discover()` and `SpecialistClient` are the PydanticAI side of the first
PydanticAI → ADK hop; the same calls run against the fasta2a docs-agent to show one
client works for both servers.
"""

from contextlib import asynccontextmanager

import httpx
import pytest
from mt_fakes import (
    AMBIGUOUS_TABLE,
    MCP_URL,
    StubMcp,
    calls_tool,
    says,
    scripted,
)

from mt_agent.a2a_server import build_app
from mt_agent.agent import build_agent, build_toolset
from sb_support.specialists import SpecialistClient, SpecialistError, discover

BASE = "http://127.0.0.1:8102"
DOCS = "http://127.0.0.1:8101"


@asynccontextmanager
async def served(model, stub: StubMcp | None = None):
    """mt-agent's ASGI app (lifespan run, so ADK attaches its routes) + an httpx client."""
    stub = stub or StubMcp()
    toolset = build_toolset(MCP_URL, "t0k", httpx_client_factory=stub.client_factory())
    app = build_app(build_agent(model, toolset), url=BASE)
    async with stub.running(), app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as http:
            yield http


def standings_script(*extra):
    return scripted(
        calls_tool("search_teams", query="IFA", age_group="U14"),
        calls_tool("get_standings", team_id=102, age_group_id=14),
        *extra,
    )


async def test_agent_card_has_name_skills_and_examples() -> None:
    async with served(scripted()) as http:
        card = (await http.get("/.well-known/agent-card.json")).json()
    assert card["name"] == "mt-agent"
    assert card["supportedInterfaces"][0]["url"] == BASE
    [skill] = card["skills"]
    assert skill["id"] == "mt-data"
    assert 3 <= len(skill["examples"]) <= 5


async def test_clean_answer_has_text_and_high_confidence_data_part() -> None:
    stub = StubMcp()
    model = standings_script(says("IFA is 1st in the U14 League (18 played of 42 scheduled)."))
    async with served(model, stub) as http:
        answer = await SpecialistClient(BASE, http).ask("IFA U14 standings?")

    assert answer.text.startswith("IFA is 1st")
    assert answer.confidence == "high"
    assert answer.data["sources"] == ["search_teams", "get_standings"]
    assert answer.data["issues"] == []
    # The phase-1 identity and client header reach mt-mcp; get_my_team is filtered out.
    assert {h.get("x-mt-client") for h in stub.headers} == {"sb-agents"}
    assert {h.get("authorization") for h in stub.headers} == {"Bearer t0k"}
    assert model.declared_tools() == ["get_standings", "get_upcoming_matches", "search_teams"]
    # The model saw the tool's own result, not the MCP envelope.
    assert model.tool_results()[-1]["coverage"] == {"matches_counted": 18, "fixtures": 42}


async def test_ambiguous_table_left_unresolved_is_low_confidence() -> None:
    stub = StubMcp(get_standings=AMBIGUOUS_TABLE)
    model = standings_script(says("IFA U14 plays in two tables: League NE or Flex A?"))
    async with served(model, stub) as http:
        answer = await SpecialistClient(BASE, http).ask("IFA U14 standings?")
    assert answer.confidence == "low"
    assert answer.data["issues"] == ["get_standings: ambiguous"]


async def test_tool_failure_is_low_confidence() -> None:
    stub = StubMcp(get_standings=RuntimeError("db down"))
    model = standings_script(says("Standings are unavailable right now."))
    async with served(model, stub) as http:
        answer = await SpecialistClient(BASE, http).ask("IFA U14 standings?")
    assert answer.confidence == "low"
    assert answer.data["issues"] == ["get_standings: unavailable"]


async def test_hitting_the_llm_cap_fails_the_task() -> None:
    model = scripted(*[calls_tool("search_teams", query=f"q{i}") for i in range(12)])
    async with served(model) as http:
        with pytest.raises(SpecialistError, match="TASK_STATE_FAILED"):
            await SpecialistClient(BASE, http).ask("loop forever")


# --- interop: one sb-support client, two A2A servers -------------------------------


@asynccontextmanager
async def docs_agent_served():
    from pydantic_ai.models.test import TestModel

    from docs_agent.a2a_server import build_app as build_docs_app
    from docs_agent.agent import Deps
    from docs_agent.agent import build_agent as build_docs_agent
    from docs_agent.corpus import Corpus, KeywordRetriever, split_sections

    md = "# Install\n\nRun `uv tool install janitor-cli`."
    corpus = Corpus(split_sections("janitor", "README.md", md), {"janitor/README.md": md})
    answer = {
        "answer": "Run `uv tool install janitor-cli`.",
        "sources": [{"ref": "janitor/README.md", "heading": "Install"}],
        "confidence": "high",
    }
    model = TestModel(call_tools=["search_docs"], custom_output_args=answer)
    app = build_docs_app(Deps(corpus, KeywordRetriever(corpus)), build_docs_agent(model), DOCS)
    async with app.router.lifespan_context(app):
        yield app


async def test_sb_support_discovers_and_asks_both_adk_and_fasta2a_specialists() -> None:
    model = standings_script(says("IFA is 1st (18 played of 42 scheduled)."))
    async with served(model) as mt_http, docs_agent_served() as docs_app:
        docs_transport = httpx.ASGITransport(app=docs_app)
        async with httpx.AsyncClient(transport=docs_transport, base_url=DOCS) as docs_http:
            [mt] = await discover([BASE], mt_http)
            [docs] = await discover([DOCS], docs_http)
            mt_answer = await mt.client.ask("IFA U14 standings?")
            docs_answer = await docs.client.ask("How do I install janitor?")

    assert (mt.name, mt.tool_name) == ("mt-agent", "ask_mt_agent")
    assert "standings" in mt.tool_description and "IFA U14" in mt.tool_description
    assert (mt_answer.text, mt_answer.confidence) == (
        "IFA is 1st (18 played of 42 scheduled).",
        "high",
    )

    assert docs.tool_name == "ask_docs_agent"
    assert "uv tool install janitor-cli" in docs_answer.text
    assert docs_answer.confidence == "high"
    assert docs_answer.data["sources"][0]["ref"] == "janitor/README.md"


# --- follow-up questions (SB-1525) ---------------------------------------------------


def _texts(request) -> list[str]:
    return [p.text for c in request.contents for p in c.parts or [] if p.text]


async def test_same_context_id_continues_the_conversation() -> None:
    model = standings_script(
        says("IFA is 1st in the U14 League."),
        calls_tool("search_teams", query="IFA", age_group="U13"),
        says("IFA U13 is 2nd."),
        says("Which team do you mean?"),
    )
    async with served(model) as http:
        client = SpecialistClient(BASE, http)
        await client.ask("IFA U14 standings?", context_id="ctx-a")
        await client.ask("and U13?", context_id="ctx-a")
        await client.ask("and U13?", context_id="ctx-b")  # a new conversation

    follow_up = _texts(model.requests[3])  # first model call of the second question
    assert "IFA U14 standings?" in follow_up and "IFA is 1st in the U14 League." in follow_up
    assert "and U13?" in follow_up
    fresh = _texts(model.requests[-1])
    assert "and U13?" in fresh and "IFA U14 standings?" not in fresh


async def test_card_declares_the_conversation_extension() -> None:
    from mt_agent.a2a_server import CONVERSATION_EXTENSION
    from sb_support.specialists import CONVERSATION_EXTENSION as SUPPORT_SIDE

    assert CONVERSATION_EXTENSION == SUPPORT_SIDE
    async with served(scripted()) as http:
        [mt] = await discover([BASE], http)
    assert mt.conversational
