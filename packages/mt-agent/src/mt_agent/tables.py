"""Tool results → typed tables for the A2A data part (SB-1528).

The data part carries `tables: [{title, columns, rows}]` built from what the tools
returned, never from the model's text, so a table can't disagree with the data. Cells
are strings: a2a-sdk sends data parts as protobuf `Value`s, where every number becomes a
float (`2.0`). The channel decides how a table looks (Slack: a Block Kit table); the
agent never knows about Slack.
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

MAX_ROWS = 25


class Table(TypedDict):
    title: str
    columns: list[str]
    rows: list[list[str]]
    notes: NotRequired[list[str]]  # trends computed here, from the data (SB-1533)
    # Row indexes (0-based, header excluded) to emphasize: the team the question named.
    highlight: NotRequired[list[int]]


# Form as squares reads at a glance; the channel needs no legend for W/D/L letters.
# Spaced, so adjacent squares don't merge into one bar (picked in Slack, SB-1541).
FORM_SQUARES = {"W": "🟩", "D": "🟨", "L": "🟥"}


def _name(team: dict[str, Any] | None) -> str:
    return (team or {}).get("name") or "?"


def standings_title(result: dict[str, Any]) -> str:
    """ "U15 Homegrown Northeast · 51 of 190 matches played".

    The competition is named only when the table's league doesn't already say it
    (a Flex division's Flex table is just "Flex New England").
    """
    division = result.get("division") or {}
    age = (result.get("age_group") or {}).get("name")
    league = division.get("league")
    parts = [" ".join(p for p in (age, league, division.get("name")) if p)]
    competition = result.get("competition") or "League"
    if competition.lower() not in {"league", (league or "").lower()}:
        parts.append(f"{competition} record" if competition == "qualifying" else competition)
    coverage = result.get("coverage") or {}
    if isinstance(coverage.get("matches_counted"), int) and isinstance(
        coverage.get("fixtures"), int
    ):
        parts.append(f"{coverage['matches_counted']} of {coverage['fixtures']} matches played")
    elif (result.get("meta") or {}).get("coverage"):
        parts.append(result["meta"]["coverage"])
    return " · ".join(p for p in parts if p)


def standings_table(result: dict[str, Any]) -> Table | None:
    rows = result.get("rows")
    if result.get("error") or not rows:
        return None
    shown = rows[:MAX_ROWS]
    return Table(
        title=standings_title(result),
        columns=["#", "Team", "P", "W", "D", "L", "GD", "Pts", "Form →"],
        rows=[
            [
                f"{r['position']} {_movement(r.get('position_change') or 0)}".strip(),
                _name(r.get("team")),
                str(r["played"]),
                str(r["wins"]),
                str(r["draws"]),
                str(r["losses"]),
                f"{r['goal_difference']:+d}" if "goal_difference" in r else "",
                str(r["points"]),
                " ".join(FORM_SQUARES.get(x, x) for x in r.get("form") or []),
            ]
            for r in shown
        ],
        notes=standings_notes(rows),
        highlight=[i for i, r in enumerate(shown) if r.get("is_requested_team")],
    )


def _movement(change: int) -> str:
    """Places moved since the table before the latest match day (mt-mcp's position_change)."""
    if change > 0:
        return f"▲{change}"
    if change < 0:
        return f"▼{-change}"
    return ""


def _run(form: list[str], results: set[str]) -> int:
    """How many of the latest results (form is oldest first) are in `results`."""
    count = 0
    for result in reversed(form):
        if result not in results:
            break
        count += 1
    return count


def _bold(text: str) -> str:
    """Notes use CommonMark bold; each channel renders it its own way."""
    return f"**{text}**"


def _names(rows: list[dict[str, Any]]) -> str:
    return " and ".join(_bold(_name(r.get("team"))) for r in rows[:2])


def _best(rows: list[dict[str, Any]], key: Any, minimum: float) -> tuple[float, list[dict]]:
    """The rows sharing the top value of `key`, if it reaches `minimum`."""
    top = max((key(r) for r in rows), default=0)
    return top, ([r for r in rows if key(r) == top] if top >= minimum else [])


# Thresholds for a note to be worth saying. A run counts only the last five results
# (mt-mcp's form window), so "5 in a row" means "at least 5".
MIN_RUN, MIN_MOVE, MIN_PLAYED_GAP, MIN_PLAYED = 3, 2, 2, 3


def standings_notes(rows: list[dict[str, Any]]) -> list[str]:
    """A few facts about the table's trends, computed from `form` and `position_change`.

    Deterministic on purpose: a model asked to "spot a trend" in 20 rows miscounts
    ("5 wins... more precisely 2"). Each note says what it measures.
    """
    notes = []
    form = {id(r): r.get("form") or [] for r in rows}
    wins, hot = _best(rows, lambda r: _run(form[id(r)], {"W"}), MIN_RUN)
    if hot:
        notes.append(f"Hottest run: {_names(hot)}, {wins} straight wins.")
    losses, cold = _best(rows, lambda r: _run(form[id(r)], {"L"}), MIN_RUN)
    if cold:
        notes.append(f"Coldest run: {_names(cold)}, {losses} straight losses.")
    up, risers = _best(rows, lambda r: r.get("position_change") or 0, MIN_MOVE)
    if risers:
        notes.append(f"Biggest climb since the last match day: {_names(risers)}, up {up}.")
    down, fallers = _best(rows, lambda r: -(r.get("position_change") or 0), MIN_MOVE)
    if fallers:
        notes.append(f"Biggest drop since the last match day: {_names(fallers)}, down {down}.")
    notes.extend(_games_in_hand(rows))
    return notes


def _games_in_hand(rows: list[dict[str, Any]]) -> list[str]:
    """The best points-per-game side, when it isn't the leader and has played fewer."""
    played = [r for r in rows if r["played"] >= MIN_PLAYED]
    if not played or rows[0] not in played:
        return []
    leader = rows[0]
    best = max(played, key=lambda r: (r["points"] / r["played"], -r["position"]))
    gap = leader["played"] - best["played"]
    if best is leader or gap < MIN_PLAYED_GAP:
        return []
    return [
        f"Games in hand: {_bold(_name(best.get('team')))} ({best['points']} pts from "
        f"{best['played']}, {best['points'] / best['played']:.2f} per game) has played "
        f"{gap} fewer than leader {_bold(_name(leader.get('team')))} ({leader['points']} from "
        f"{leader['played']}, {leader['points'] / leader['played']:.2f})."
    ]


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
