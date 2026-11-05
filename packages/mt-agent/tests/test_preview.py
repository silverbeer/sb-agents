"""Match preview workflow (SB-1527): parallel deterministic reads, one writer call."""

from __future__ import annotations

import json

import pytest
from mt_fakes import MCP_URL, StubMcp, calls_tool, says, scripted

from mt_agent.agent import build_agent, build_toolset
from mt_agent.preview import run_preview
from mt_agent.runner import TrackedRunner

MATCH = {
    "match_id": 4068,
    "match_date": "2026-11-07",
    "kickoff": "2026-11-07T13:15:00-05:00",
    "status": "scheduled",
    "home_team": {"id": 19, "name": "IFA"},
    "away_team": {"id": 27, "name": "Valeo Futbol Club"},
    "is_home": True,
    "division": "Northeast",
    "competition": "League",
}
UPCOMING = {"team_id": 19, "matches": [MATCH], "error": None}
RESULT = {
    "match_date": "2026-11-01",
    "result": "W",
    "goals_for": 2,
    "goals_against": 0,
    "is_home": True,
    "home_team": {"id": 19, "name": "IFA"},
    "away_team": {"id": 30, "name": "Ironbound"},
    "competition": "League",
}
RECENT = {"matches": [RESULT], "summary": {"played": 1, "wins": 1}, "error": None}
TABLE = {
    "rows": [
        {
            "position": 3,
            "team": {"id": 27},
            "points": 12,
            "played": 6,
            "wins": 4,
            "draws": 0,
            "losses": 2,
        },
        {
            "position": 10,
            "team": {"id": 19},
            "points": 6,
            "played": 5,
            "wins": 2,
            "draws": 0,
            "losses": 3,
        },
    ],
    "division": {"name": "Northeast", "league": "Homegrown"},
    "meta": {"coverage": "51 played of 190 scheduled"},
    "error": None,
}


def stub(**overrides) -> StubMcp:
    results = {
        "get_upcoming_matches": UPCOMING,
        "get_recent_results": RECENT,
        "get_standings": TABLE,
    }
    results.update(overrides)
    return StubMcp(**results)


def connect_to(s: StubMcp) -> dict:
    return {"url": MCP_URL, "token": "t", "httpx_client_factory": s.client_factory()}


async def test_preview_reads_everything_then_one_writer_call() -> None:
    s = stub()
    writer = scripted(says("IFA host Valeo on Saturday."))
    async with s.running():
        out = await run_preview(19, 3, writer, connect_kwargs=connect_to(s))

    assert out == {"preview": "IFA host Valeo on Saturday.", "match_id": 4068}
    tools = [t for t, _ in s.calls]
    assert tools[0] == "get_upcoming_matches"
    assert sorted(tools[1:]) == ["get_recent_results"] * 3 + ["get_standings"]
    # Head-to-head asked for the opponent from the fixture.
    assert {"team_id": 19, "opponent_id": 27} in [
        a for t, a in s.calls if t == "get_recent_results"
    ]
    # The writer made one request, and saw every branch plus the fixture.
    [request] = writer.requests
    brief = json.loads(request.contents[-1].parts[0].text)
    assert set(brief) == {"team_form", "opponent_form", "table", "head_to_head", "carry"}
    assert brief["table"]["team"]["position"] == 10 and brief["table"]["opponent"]["position"] == 3
    assert brief["head_to_head"]["matches"][0]["score"] == "2-0"
    assert brief["carry"]["match"]["away_team"]["name"] == "Valeo Futbol Club"


async def test_no_upcoming_match_is_not_found() -> None:
    s = stub(get_upcoming_matches={"team_id": 19, "matches": [], "error": None})
    writer = scripted(says("Nothing scheduled."))
    async with s.running():
        out = await run_preview(19, 3, writer, connect_kwargs=connect_to(s))
    assert out["status"] == "not_found"
    assert [t for t, _ in s.calls] == ["get_upcoming_matches"]  # branches skip their reads


async def test_one_failed_read_does_not_sink_the_preview() -> None:
    s = stub(get_standings=RuntimeError("db down"))
    writer = scripted(says("Preview without the table."))
    async with s.running():
        out = await run_preview(19, 3, writer, connect_kwargs=connect_to(s))
    assert out["preview"] == "Preview without the table."
    brief = json.loads(writer.requests[0].contents[-1].parts[0].text)
    assert brief["table"]["error"]["kind"] == "unavailable"
    assert brief["team_form"]["summary"]["wins"] == 1


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list:
    from sb_obs import runs

    records: list = []
    monkeypatch.setattr(runs, "emit", records.append)
    return records


async def test_through_the_agent_one_run_record_counts_the_writer(emitted: list) -> None:
    s = stub()
    model = scripted(
        calls_tool("search_teams", query="IFA", age_group="U15"),
        calls_tool("match_preview", team_id=19, age_group_id=3),
        says("IFA host Valeo on Saturday."),  # the workflow's writer
        says("IFA host Valeo on Saturday."),  # the main agent relays it
    )
    async with s.running():
        toolset = build_toolset(MCP_URL, "t", httpx_client_factory=s.client_factory())
        agent = build_agent(model, toolset, preview_connect=connect_to(s))
        runner = TrackedRunner(agent)
        await runner.session_service.create_session(
            app_name=runner.app_name, user_id="u", session_id="s1"
        )
        from google.genai import types

        message = types.Content(role="user", parts=[types.Part(text="Preview IFA U15's game")])
        async for _ in runner.run_async(user_id="u", session_id="s1", new_message=message):
            pass
        await toolset.close()

    [record] = emitted
    assert record.outcome == "ok" and record.confidence == "high"
    assert record.requests == 4  # 3 main-agent calls + the writer
    assert record.tool_calls == 2  # search_teams, match_preview
    assert record.input_tokens == 4000


def test_preview_can_be_switched_off_for_the_baseline() -> None:
    agent = build_agent(scripted(), build_toolset(MCP_URL, "t"), preview=False)
    assert [getattr(t, "__name__", None) for t in agent.tools][1:] == []
    assert "match_preview" not in agent.instruction(None)
