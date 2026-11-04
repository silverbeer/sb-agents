"""Tool results → typed tables for the A2A data part (SB-1528).

The data part carries `tables: [{title, columns, rows}]` built from what the tools
returned, never from the model's text, so a table can't disagree with the data. Cells
are strings: a2a-sdk sends data parts as protobuf `Value`s, where every number becomes a
float (`2.0`). The channel decides how a table looks (Slack: a Block Kit table); the
agent never knows about Slack.
"""

from __future__ import annotations

from typing import Any, TypedDict

MAX_ROWS = 25


class Table(TypedDict):
    title: str
    columns: list[str]
    rows: list[list[str]]


def _name(team: dict[str, Any] | None) -> str:
    return (team or {}).get("name") or "?"


def standings_table(result: dict[str, Any]) -> Table | None:
    rows = result.get("rows")
    if result.get("error") or not rows:
        return None
    division = result.get("division") or {}
    age = (result.get("age_group") or {}).get("name") or ""
    where = " ".join(p for p in (division.get("league"), division.get("name"), age) if p)
    title = f"{where} ({result.get('competition') or 'League'})".strip()
    coverage = (result.get("meta") or {}).get("coverage")
    if coverage:
        title += f": {coverage}"
    return Table(
        title=title,
        columns=["#", "Team", "P", "W", "D", "L", "GD", "Pts"],
        rows=[
            [
                str(r["position"]),
                _name(r.get("team")),
                str(r["played"]),
                str(r["wins"]),
                str(r["draws"]),
                str(r["losses"]),
                f"{r['goal_difference']:+d}" if "goal_difference" in r else "",
                str(r["points"]),
            ]
            for r in rows[:MAX_ROWS]
        ],
    )


def results_table(result: dict[str, Any]) -> Table | None:
    matches = result.get("matches")
    if result.get("error") or not matches:
        return None
    rows = []
    for m in matches[:MAX_ROWS]:
        opponent = m["away_team"] if m["is_home"] else m["home_team"]
        score = f"{m['goals_for']}-{m['goals_against']}"
        if m.get("home_penalty_score") is not None:
            score += f" ({m['home_penalty_score']}-{m['away_penalty_score']} pens)"
        rows.append(
            [
                str(m["match_date"]),
                m["result"],
                score,
                _name(opponent),
                "H" if m["is_home"] else "A",
                m.get("competition") or "",
            ]
        )
    return Table(
        title="Recent results", columns=["Date", "", "Score", "Opponent", "H/A", "Comp"], rows=rows
    )


def upcoming_table(result: dict[str, Any]) -> Table | None:
    matches = result.get("matches")
    if result.get("error") or not matches:
        return None
    rows = []
    for m in matches[:MAX_ROWS]:
        kickoff = m.get("kickoff") or ""
        time = kickoff[11:16] if len(kickoff) >= 16 else "TBD"
        rows.append(
            [
                str(m["match_date"]),
                time,
                _name(m.get("home_team")),
                _name(m.get("away_team")),
                m.get("competition") or "",
                m.get("status") or "",
            ]
        )
    return Table(
        title="Upcoming matches",
        columns=["Date", "Kickoff", "Home", "Away", "Comp", "Status"],
        rows=rows,
    )


BUILDERS = {
    "get_standings": standings_table,
    "get_recent_results": results_table,
    "get_upcoming_matches": upcoming_table,
}


def tables_from(results: dict[str, Any]) -> list[Table]:
    """One table per tool whose latest result has data, in the order the tools last ran."""
    out = []
    for tool, result in results.items():
        build = BUILDERS.get(tool)
        if build is None or not isinstance(result, dict):
            continue
        try:
            table = build(result)
        except (KeyError, TypeError, ValueError):
            table = None  # a shape we don't know: no table, the text still answers
        if table is not None:
            out.append(table)
    return out
