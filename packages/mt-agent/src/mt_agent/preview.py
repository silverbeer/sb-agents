"""Match preview as an ADK Workflow: deterministic reads in parallel, one model call (SB-1527).

"Preview IFA U15's next game" needs five mt-mcp reads that don't depend on the model's
judgment: the next fixture, then (in parallel) both teams' form, the table and the
head-to-head. A single LlmAgent would make those calls one model round trip at a time.
Here a graph runs them and the model only writes:

    START ─► fixture ─┬─► team_form ─────┐
                      ├─► opponent_form ─┤
                      ├─► table ─────────┼─► gather (JoinNode) ─► writer (LlmAgent)
                      └─► head_to_head ──┘

ADK 2.13's `Workflow` is the graph engine; `ParallelAgent` / `SequentialAgent` are
deprecated in its favor, and a Workflow can't be an LlmAgent sub-agent yet. So mt-agent
exposes the workflow as one tool, `match_preview(team_id, age_group_id)`: the main agent
resolves the team as usual, calls the tool, and relays the preview. The tool runs the
workflow in a nested Runner and adds the writer's usage to the request's RunRecord.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import START, JoinNode, Workflow, node
from google.genai import types

from mt_agent.mt_client import McpCallError, MtClient, connect

log = logging.getLogger("mt_agent.preview")

WRITER_INSTRUCTIONS = """\
Write a short match preview from the JSON you are given: MissingTable data for one
upcoming match. Use ONLY that data; never invent a stat, a player or a prediction.

- Lead with the fixture: date, kickoff (club's local time), home and away, competition.
- Then each team's recent form (results, from that team's side) and where the team sits
  in the table, with the table's coverage ("X played of Y scheduled").
- Then the head-to-head, if they have played; say so plainly if they haven't.
- A field that is null or has an "error" is unknown: say it's unavailable, don't guess.
- Never say who will win, qualify or advance.
- Slack-friendly: a few short lines or a compact list. No tables, no headings.
"""


def _opponent(match: dict[str, Any]) -> dict[str, Any]:
    return match["away_team"] if match["is_home"] else match["home_team"]


async def _read(client: MtClient, tool: str, **args: Any) -> dict[str, Any]:
    """A tool result, or {"error": ...} so one failed read doesn't sink the preview."""
    try:
        return await client.call(tool, **args)
    except McpCallError as exc:
        log.warning("preview read failed", extra={"tool": tool, "error": str(exc)})
        return {"error": {"kind": "unavailable", "message": f"{tool} failed"}}


def _form(results: dict[str, Any]) -> dict[str, Any]:
    """The fields the writer needs from get_recent_results, nothing else."""
    if results.get("error") or results.get("matches") is None:
        return {"error": results.get("error") or {"kind": "unavailable"}}
    return {
        "summary": results.get("summary"),
        "matches": [
            {
                "date": m["match_date"],
                "result": m["result"],
                "score": f"{m['goals_for']}-{m['goals_against']}",
                "opponent": _opponent(m)["name"],
                "home": m["is_home"],
                "competition": m.get("competition"),
                "penalties": (
                    f"{m['home_penalty_score']}-{m['away_penalty_score']}"
                    if m.get("home_penalty_score") is not None
                    else None
                ),
            }
            for m in results["matches"]
        ],
    }


def _position(table: dict[str, Any], team_id: int) -> dict[str, Any] | None:
    row = next((r for r in table.get("rows") or [] if r["team"]["id"] == team_id), None)
    if row is None:
        return None
    return {
        "position": row["position"],
        "of": len(table["rows"]),
        "points": row["points"],
        "played": row["played"],
        "record": f"{row['wins']}W {row['draws']}D {row['losses']}L",
    }


def build_workflow(client: MtClient, model: BaseLlm | str) -> Workflow:
    """The preview graph over one MCP session. Built per request (it's a few objects)."""

    @node
    async def fixture(node_input: Any) -> dict[str, Any]:
        request = json.loads(_text(node_input))
        team_id, age_id = request["team_id"], request["age_group_id"]
        upcoming = await _read(
            client, "get_upcoming_matches", team_id=team_id, age_group_id=age_id, limit=1
        )
        matches = upcoming.get("matches") or []
        return {
            "team_id": team_id,
            "age_group_id": age_id,
            "match": matches[0] if matches else None,
            "error": upcoming.get("error"),
        }

    async def recent(team_id: int, age_id: int, **extra: Any) -> dict[str, Any]:
        return _form(
            await _read(client, "get_recent_results", team_id=team_id, age_group_id=age_id, **extra)
        )

    @node
    async def team_form(node_input: dict[str, Any]) -> dict[str, Any] | None:
        if node_input["match"] is None:
            return None
        return await recent(node_input["team_id"], node_input["age_group_id"], limit=5)

    @node
    async def opponent_form(node_input: dict[str, Any]) -> dict[str, Any] | None:
        if node_input["match"] is None:
            return None
        opponent_id = _opponent(node_input["match"])["id"]
        return await recent(opponent_id, node_input["age_group_id"], limit=5)

    @node
    async def head_to_head(node_input: dict[str, Any]) -> dict[str, Any] | None:
        if node_input["match"] is None:
            return None
        opponent_id = _opponent(node_input["match"])["id"]
        return await recent(
            node_input["team_id"], node_input["age_group_id"], opponent_id=opponent_id, limit=10
        )

    @node
    async def table(node_input: dict[str, Any]) -> dict[str, Any] | None:
        match = node_input["match"]
        if match is None:
            return None
        team_id, age_id = node_input["team_id"], node_input["age_group_id"]
        competition = match.get("competition") or "League"
        args = {"team_id": team_id, "age_group_id": age_id, "competition": competition}
        standings = await _read(client, "get_standings", **args)
        if (standings.get("error") or {}).get("kind") == "ambiguous":
            # The fixture names its division: use that table.
            picked = next(
                (
                    o
                    for o in standings.get("options") or []
                    if o["division"]["name"] == match.get("division")
                ),
                None,
            )
            if picked:
                standings = await _read(
                    client, "get_standings", **args, division_id=picked["division"]["id"]
                )
        if standings.get("error") or standings.get("rows") is None:
            return {"error": standings.get("error") or {"kind": "unavailable"}}
        opponent_id = _opponent(match)["id"]
        division = standings.get("division") or {}
        return {
            "table": f"{division.get('league')} {division.get('name')} ({competition})",
            "coverage": (standings.get("meta") or {}).get("coverage"),
            "team": _position(standings, team_id),
            "opponent": _position(standings, opponent_id),
        }

    @node
    async def carry(node_input: dict[str, Any]) -> dict[str, Any]:
        """The fixture itself, through the join, so the writer sees date and venue."""
        return node_input

    gather = JoinNode(name="gather")

    @node
    async def brief(node_input: dict[str, Any]) -> str:
        """Join output + the fixture → the writer's one input message."""
        return json.dumps(node_input, default=str)

    writer = LlmAgent(name="preview_writer", model=model, instruction=WRITER_INSTRUCTIONS)
    return Workflow(
        name="match_preview",
        edges=[
            (START, fixture, (team_form, opponent_form, table, head_to_head, carry), gather),
            (gather, brief, writer),
        ],
    )


def _text(content: Any) -> str:
    if isinstance(content, types.Content):
        return "".join(p.text or "" for p in content.parts or [])
    return str(content)


UsageSink = Callable[[Any], None]


async def run_preview(
    team_id: int,
    age_group_id: int,
    model: BaseLlm | str,
    *,
    on_model_event: UsageSink | None = None,
    connect_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the workflow; the result is the `match_preview` tool's result."""
    async with connect(**(connect_kwargs or {})) as client:
        workflow = build_workflow(client, model)
        runner = Runner(node=workflow, session_service=InMemorySessionService())
        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id="preview"
        )
        request = json.dumps({"team_id": team_id, "age_group_id": age_group_id})
        message = types.Content(role="user", parts=[types.Part(text=request)])
        fixture: dict[str, Any] | None = None
        text = ""
        async for event in runner.run_async(
            user_id="preview", session_id=session.id, new_message=message
        ):
            if event.author == "preview_writer":
                if on_model_event is not None:
                    on_model_event(event)
                parts = event.content.parts if event.content else []
                text = "".join(p.text or "" for p in parts) or text
            output = getattr(event, "output", None)
            if isinstance(output, dict) and "match" in output and fixture is None:
                fixture = output
        await runner.close()
    if fixture is not None and fixture.get("error"):
        return {"error": fixture["error"]}
    if fixture is None or fixture.get("match") is None:
        return {"status": "not_found", "message": "No upcoming match is scheduled."}
    return {"preview": text, "match_id": fixture["match"]["match_id"]}
