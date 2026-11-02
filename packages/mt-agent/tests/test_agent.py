"""mt-agent's runner: confidence rule, caps and run records, without A2A."""

import pytest
from google.adk.events import Event
from google.genai import types
from mt_fakes import (
    AMBIGUOUS_SEARCH,
    AMBIGUOUS_TABLE,
    MCP_URL,
    StubMcp,
    calls_tool,
    says,
    scripted,
)

from mt_agent.agent import TOOLS, build_agent, build_toolset, unwrap_mcp_result
from mt_agent.runner import ToolLedger, TrackedRunner, UsageLimitExceeded, tool_outcome


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list:
    from sb_obs import runs

    records: list = []
    monkeypatch.setattr(runs, "emit", records.append)
    return records


def tool_event(name: str, response: dict) -> Event:
    parts = [
        types.Part(function_call=types.FunctionCall(name=name, args={})),
        types.Part(function_response=types.FunctionResponse(name=name, response=response)),
    ]
    return Event(author="mt_agent", content=types.Content(role="model", parts=parts))


def ledger(*events: Event) -> ToolLedger:
    out = ToolLedger()
    for event in events:
        out.see(event)
    return out


# --- the confidence rule -----------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        ({"status": "resolved"}, "ok"),
        ({"rows": [], "error": None}, "ok"),
        ({"status": "ambiguous"}, "ambiguous"),
        ({"status": "not_found", "other_ages": ["U15"]}, "not_found"),
        ({"error": {"kind": "unavailable", "message": "down"}}, "unavailable"),
        ({"error": {"kind": "ambiguous", "message": "2 tables"}}, "ambiguous"),
        ({"error": "boom"}, "error"),
        ("plain string", "ok"),
    ],
)
def test_tool_outcome(result, outcome: str) -> None:
    assert tool_outcome(result) == outcome


def test_clean_tool_results_are_high_confidence() -> None:
    report = ledger(
        tool_event("search_teams", {"status": "resolved"}),
        tool_event("get_standings", {"rows": [], "error": None}),
    ).report()
    assert report == {
        "confidence": "high",
        "sources": ["search_teams", "get_standings"],
        "tool_calls": 2,
        "issues": [],
    }


def test_ending_on_a_gap_or_error_is_low_confidence() -> None:
    assert ledger(tool_event("search_teams", AMBIGUOUS_SEARCH)).confidence == "low"
    assert ledger(tool_event("get_standings", AMBIGUOUS_TABLE)).report()["issues"] == [
        "get_standings: ambiguous"
    ]
    down = {"error": {"kind": "unavailable", "message": "x"}}
    assert ledger(tool_event("get_standings", down)).confidence == "low"


def test_a_gap_the_model_resolved_is_still_high_confidence() -> None:
    """Ambiguous table → the model picks the League option and calls again."""
    resolved = ledger(
        tool_event("get_standings", AMBIGUOUS_TABLE),
        tool_event("get_standings", {"rows": [], "error": None}),
    )
    assert resolved.confidence == "high"
    assert resolved.report()["sources"] == ["get_standings"]


def test_no_tool_call_is_low_confidence() -> None:
    assert ledger().confidence == "low"


def test_unwrap_mcp_result() -> None:
    structured = {"content": [{"type": "text", "text": "{}"}], "structuredContent": {"a": 1}}
    assert unwrap_mcp_result(structured) == {"a": 1}
    failed = {"content": [{"type": "text", "text": "refused"}], "isError": True}
    assert unwrap_mcp_result(failed) == {"error": {"kind": "unavailable", "message": "refused"}}
    assert unwrap_mcp_result({"content": [{"type": "text", "text": '{"b": 2}'}]}) == {"b": 2}
    assert unwrap_mcp_result({"x": 1}) == {"x": 1}


# --- toolset + runner --------------------------------------------------------------


def test_toolset_sends_token_and_client_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MT_AGENT_MCP_TOKEN", "t0k")
    params = build_toolset(MCP_URL)._connection_params
    assert params.headers == {"X-MT-Client": "sb-agents", "Authorization": "Bearer t0k"}
    assert params.url == MCP_URL


def test_toolset_without_token_sends_no_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MT_AGENT_MCP_TOKEN", raising=False)
    assert "Authorization" not in build_toolset(MCP_URL)._connection_params.headers


def test_default_model_is_claude_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MT_AGENT_MODEL", "claude-sonnet-5")
    agent = build_agent(toolset=build_toolset(MCP_URL, "t"))
    assert agent.model.model == "claude-sonnet-5"
    assert TOOLS == ("search_teams", "list_divisions", "get_upcoming_matches", "get_standings")


async def run(runner: TrackedRunner, session_id: str = "s1") -> list[Event]:
    await runner.session_service.create_session(
        app_name=runner.app_name, user_id="u", session_id=session_id
    )
    message = types.Content(role="user", parts=[types.Part(text="IFA U14 standings?")])
    return [
        e async for e in runner.run_async(user_id="u", session_id=session_id, new_message=message)
    ]


def agent_with(model, stub: StubMcp):
    return build_agent(
        model, build_toolset(MCP_URL, "t", httpx_client_factory=stub.client_factory())
    )


async def test_run_record_has_usage_cost_and_confidence(emitted: list) -> None:
    stub = StubMcp()
    model = scripted(
        calls_tool("search_teams", query="IFA", age_group="U14"),
        calls_tool("get_standings", team_id=102, age_group_id=14),
        says("IFA is 1st (18 played of 42 scheduled)."),
    )
    async with stub.running():
        runner = TrackedRunner(agent_with(model, stub))
        events = await run(runner)
        session = await runner.session_service.get_session(
            app_name=runner.app_name, user_id="u", session_id="s1"
        )

    [record] = emitted
    assert (record.agent, record.caller, record.model) == ("mt-agent", "a2a", "claude-haiku-5-5")
    assert (record.outcome, record.confidence) == ("ok", "high")
    assert (record.requests, record.tool_calls) == (3, 2)
    assert (record.input_tokens, record.output_tokens) == (3000, 150)
    assert record.cost_usd and record.cost_usd > 0
    # The answer event carries the data part; the session copy doesn't (AnthropicLlm
    # can't send a data blob back to Claude on the next turn).
    assert events[-1].content.parts[-1].inline_data is not None
    stored = [p for e in session.events if e.content for p in e.content.parts or []]
    assert any(p.text for p in stored)
    assert not [p for p in stored if p.inline_data]


async def test_llm_call_cap_is_outcome_limit(emitted: list) -> None:
    stub = StubMcp()
    model = scripted(*[calls_tool("search_teams", query=f"q{i}") for i in range(5)])
    async with stub.running():
        with pytest.raises(UsageLimitExceeded):
            await run(TrackedRunner(agent_with(model, stub), max_llm_calls=2))
    assert emitted[0].outcome == "limit"


async def test_cost_cap_stops_before_the_next_tool_call(emitted: list) -> None:
    stub = StubMcp()
    model = scripted(calls_tool("search_teams", query="IFA"), says("never reached"))
    async with stub.running():
        with pytest.raises(UsageLimitExceeded, match="cost"):
            await run(TrackedRunner(agent_with(model, stub), max_cost_usd=1e-9))
    assert emitted[0].outcome == "limit"
    assert stub.calls == []
