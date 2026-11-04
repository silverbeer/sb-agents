"""Specialist discovery, routing, escalation and thread follow-ups (SB-1476)."""

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.test import TestModel

from sb_support.agent import build_agent
from sb_support.core import ConversationMemory, SupportCore, answer
from sb_support.slack import handle_event, is_follow_up
from sb_support.specialists import Specialist, SpecialistAnswer, SpecialistError, discover

BASE = "http://127.0.0.1:8101"


@dataclass
class StubClient:
    confidence: str | None = "high"
    fail: bool = False
    asked: list[str] | None = None
    contexts: list[str | None] | None = None

    async def ask(self, question: str, context_id: str | None = None) -> SpecialistAnswer:
        (self.asked if self.asked is not None else []).append(question)
        (self.contexts if self.contexts is not None else []).append(context_id)
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

    await handle_event(event, slack, SupportCore(routed_agent(StubClient("low"))))

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
    memory = ConversationMemory()
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
    memory = ConversationMemory(max_conversations=2, max_runs=2)
    for i in range(3):
        memory.add("t1", [ModelRequest(parts=[UserPromptPart(content=f"q{i}")])])
    assert [m.parts[0].content for m in memory.history("t1")] == ["q1", "q2"]
    memory.add("t2", [])
    memory.add("t3", [])
    assert "t1" not in memory and "t3" in memory


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"channel": "C1", "thread_ts": "t1", "text": "and then?"}, True),
        ({"channel": "C1", "thread_ts": "t1", "text": "<@UBOT> and then?"}, False),  # mention
        ({"channel": "C1", "thread_ts": "t9", "text": "unrelated thread"}, False),
        ({"channel": "C2", "thread_ts": "t1", "text": "same ts, other channel"}, False),
        ({"channel": "C1", "text": "top-level message"}, False),
        ({"channel": "C1", "thread_ts": "t1", "text": "x", "bot_id": "B1"}, False),
        ({"channel": "C1", "thread_ts": "t1", "text": "x", "subtype": "message_changed"}, False),
    ],
)
def test_is_follow_up(event: dict, expected: bool) -> None:
    core = SupportCore(build_agent(TestModel()))
    core.memory.add("slack:C1:t1", [])
    assert is_follow_up(event, core, "UBOT") is expected


# --- A2A answer parsing (live bug, 2026-11-02) ---------------------------------------


def test_answer_skips_empty_text_parts_from_adk() -> None:
    """mt-agent (ADK) sent ["", "real answer", data]; the client returned "" to Slack."""
    from sb_support.specialists import _Answer

    answer = _Answer()
    answer.read(
        {
            "result": {
                "artifactUpdate": {
                    "artifact": {
                        "parts": [
                            {"text": ""},
                            {"text": "IFA U15's next match is Saturday."},
                            {"data": {"confidence": "high", "sources": ["get_upcoming_matches"]}},
                        ]
                    }
                }
            }
        }
    )
    assert answer.text == "IFA U15's next match is Saturday."
    assert answer.data == {"confidence": "high", "sources": ["get_upcoming_matches"]}


def test_answer_keeps_the_first_artifact_with_text() -> None:
    from sb_support.specialists import _Answer

    answer = _Answer()
    answer.read({"result": {"artifactUpdate": {"artifact": {"parts": [{"text": "first"}]}}}})
    answer.read({"result": {"task": {"artifacts": [{"parts": [{"text": "again"}]}], "status": {}}}})
    assert answer.text == "first"


# --- conversation contexts (SB-1525) ---------------------------------------------------


def test_context_id_is_stable_opaque_and_only_for_conversational_specialists() -> None:
    mt = Specialist("mt-agent", "MT data.", StubClient(), conversational=True)
    plain = Specialist("docs-agent", "Docs.", StubClient())
    key = "slack:C123:1712.5"
    assert mt.context_id(key) == mt.context_id(key)
    assert mt.context_id(key) != mt.context_id("slack:C123:9999.1")
    assert "C123" not in mt.context_id(key)
    assert plain.context_id(key) is None
    assert mt.context_id(None) is None


async def test_follow_ups_reuse_the_specialists_context(emitted: list) -> None:
    client = StubClient(asked=[], contexts=[])
    mt = Specialist("mt-agent", "MT data.", client, conversational=True)
    from sb_support.core import InboundMessage

    # A fresh core per message (TestModel won't re-call a tool it sees in history): the
    # contextId comes from the conversation key alone, so it also survives a restart.
    for conversation, text in [("t1", "IFA U15 standings?"), ("t1", "and U14?"), ("t2", "hi")]:
        core = SupportCore(build_agent(TestModel(call_tools=["ask_mt_agent"]), [mt]))
        await core.handle(InboundMessage("slack", conversation, text))
    first, second, other = client.contexts
    assert first and first == second
    assert other and other != first


async def test_specialist_client_sends_context_id_only_when_given() -> None:
    from sb_support.specialists import SpecialistClient

    sent: list[dict] = []

    def reply(request: httpx.Request) -> httpx.Response:
        import json as _json

        sent.append(_json.loads(request.content)["params"]["message"])
        body = {"result": {"message": {"parts": [{"text": "ok"}]}}}
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        await SpecialistClient(BASE, http).ask("q", context_id="ctx-1")
        await SpecialistClient(BASE, http).ask("q")
    assert sent[0]["contextId"] == "ctx-1"
    assert "contextId" not in sent[1]


def test_card_extension_marks_a_specialist_conversational() -> None:
    from sb_support.specialists import CONVERSATION_EXTENSION, specialist_from_card

    ext = {"capabilities": {"extensions": [{"uri": CONVERSATION_EXTENSION}]}}
    assert specialist_from_card({"name": "a", **ext}, BASE, None).conversational
    assert not specialist_from_card({"name": "b", "capabilities": {}}, BASE, None).conversational
    assert not specialist_from_card({"name": "c"}, BASE, None).conversational


# --- tables from the data part (SB-1528) -------------------------------------------------


def test_parse_tables_keeps_well_formed_ones_and_cleans_floats() -> None:
    from sb_support.tables import parse_tables

    data = {
        "tables": [
            {"title": "t", "columns": ["#", "Pts"], "rows": [[1.0, 22.0], ["2", "18"]]},
            {"title": "ragged", "columns": ["a", "b"], "rows": [["only one"]]},
            {"title": "no columns", "columns": [], "rows": []},
            "not a table",
        ]
    }
    [table] = parse_tables(data)
    assert table.rows == [["1", "22"], ["2", "18"]]
    assert table.notes == []
    [noted] = parse_tables({"tables": [{**data["tables"][0], "notes": ["Hot run.", "", 3.0]}]})
    assert noted.notes == ["Hot run.", "3"]
    [bad] = parse_tables({"tables": [{**data["tables"][0], "notes": "not a list"}]})
    assert bad.notes == []
    # Floats from protobuf become ints; out-of-range, bools and junk are dropped.
    [lit] = parse_tables({"tables": [{**data["tables"][0], "highlight": [1.0, 5, True, "0", -1]}]})
    assert lit.highlight == [1]
    assert parse_tables(None) == [] and parse_tables({"tables": "x"}) == []


def test_the_model_is_told_what_is_shown_under_its_reply() -> None:
    from sb_support.agent import shown_note
    from sb_support.tables import Table

    assert shown_note([]) == ""
    note = shown_note([Table("U15 Flex New England", ["#"], [["1"], ["2"]], ["a", "b"])])
    assert '"U15 Flex New England" with all 2 rows and 2 trend notes' in note
    assert "don't say they are missing" in note


async def test_reply_carries_the_specialists_tables(emitted: list) -> None:
    @dataclass
    class TableClient(StubClient):
        async def ask(self, question: str, context_id: str | None = None) -> SpecialistAnswer:
            data = {
                "confidence": "high",
                "tables": [{"title": "T", "columns": ["a"], "rows": [["1"]]}],
            }
            return SpecialistAnswer("answer", "high", data)

    mt = Specialist("mt-agent", "MT data.", TableClient())
    agent = build_agent(TestModel(call_tools=["ask_mt_agent"]), [mt])
    reply = await answer(agent, "IFA standings?")
    assert [t.title for t in reply.tables] == ["T"]
