"""Candidate fixtures shaped like mt-mcp's output, each built to bait one failure.

Used by the tests (with a scripted model) and by the eval (with the real one). The
default team in MT has scraped results and nothing else (missing-table CLAUDE.md), so
the low-sample and no-rules fixtures are the normal case, not the edge.
"""

from __future__ import annotations

from typing import Any

from motw_agent.candidates import CandidatesResult, MotwCandidate


def _team(id_: int, name: str, position: int | None, played: int, points: int | None, **kw: Any):
    return {
        "id": id_,
        "name": name,
        "position": position,
        "table_size": 12 if position else None,
        "played": played,
        "points": points,
        "strength": kw.pop("strength", 0.7),
        "form": kw.pop("form", 0.6 if played else None),
        "low_sample": kw.pop("low_sample", played < 3),
        **kw,
    }


def _candidate(match_id: int, home: dict, away: dict, facts: list[dict], **kw: Any) -> dict:
    return {
        "match_id": match_id,
        "match_date": kw.pop("match_date", "2026-11-14"),
        "scheduled_kickoff": kw.pop("scheduled_kickoff", "10:00"),
        "age_group": kw.pop("age_group", {"id": 15, "name": "U15"}),
        "division": kw.pop("division", {"id": 7, "name": "Northeast", "league": "Homegrown"}),
        "competition": kw.pop("competition", "League"),
        "home": home,
        "away": away,
        "score": kw.pop(
            "score",
            {"quality": 0.8, "closeness": 0.9, "stakes": 0.7, "form": 0.6, "total": 0.78},
        ),
        "flags": kw.pop("flags", []),
        "facts": facts,
        "rules": kw.pop("rules", []),
    }


TOP_CLASH = _candidate(
    4096,
    _team(101, "IFA", 1, 8, 20),
    _team(102, "NEFC", 2, 8, 19),
    [
        {
            "id": "4096:pos:home",
            "kind": "standings",
            "team_id": 101,
            "text": "IFA are 1st of 12 in U15 Homegrown Northeast with 20 points from 8 games (6-2-0).",
        },
        {
            "id": "4096:pos:away",
            "kind": "standings",
            "team_id": 102,
            "text": "NEFC are 2nd of 12 in U15 Homegrown Northeast with 19 points from 8 games (6-1-1).",
        },
        {
            "id": "4096:gap",
            "kind": "standings",
            "text": "1 point separates IFA (20) and NEFC (19).",
        },
        {
            "id": "4096:streak:home",
            "kind": "form",
            "team_id": 101,
            "text": "IFA are unbeaten in their last 5 (4 wins, 1 draw).",
        },
        {"id": "4096:h2h", "kind": "rivalry", "text": "They met on Sep 20 (League): NEFC 2-1 IFA."},
        {
            "id": "4096:rule:hg-cup",
            "kind": "stakes",
            "rule_id": "hg-cup",
            "confirmed": True,
            "text": "League → MLS NEXT Cup. The top 2 in each Homegrown conference qualify.",
        },
    ],
    flags=["adjacent", "top_of_table", "qualifying_line"],
    rules=[
        {
            "id": "hg-cup",
            "pathway": "MLS NEXT Cup",
            "confirmed": True,
            "summary": "The top 2 in each Homegrown conference qualify.",
        }
    ],
)

LOW_SAMPLE = _candidate(
    5120,
    _team(201, "Seacoast United", 3, 4, 9),
    _team(202, "Bayside FC", None, 0, None),
    [
        {
            "id": "5120:pos:home",
            "kind": "standings",
            "team_id": 201,
            "text": "Seacoast United are 3rd of 12 in U14 Academy East with 9 points from 4 games (3-0-1).",
        },
        {
            "id": "5120:streak:home",
            "kind": "form",
            "team_id": 201,
            "text": "Seacoast United have won 3 in a row.",
        },
        {
            "id": "5120:left:home",
            "kind": "stakes",
            "team_id": 201,
            "text": "Seacoast United have 7 games left in U14 Academy East after this one.",
        },
    ],
    age_group={"id": 14, "name": "U14"},
    division={"id": 9, "name": "East", "league": "Academy"},
    flags=["low_sample"],
)

UNCONFIRMED_RULE = _candidate(
    6200,
    _team(301, "FC Westchester", 4, 6, 12),
    _team(302, "Metropolitan Oval", 5, 6, 11),
    [
        {
            "id": "6200:gap",
            "kind": "standings",
            "text": "1 point separates FC Westchester (12) and Metropolitan Oval (11).",
        },
        {
            "id": "6200:pos:home",
            "kind": "standings",
            "team_id": 301,
            "text": "FC Westchester are 4th of 12 in U16 Homegrown Mid-Atlantic with 12 points from 6 games (4-0-2).",
        },
        {
            "id": "6200:common",
            "kind": "storyline",
            "text": "Against the 3 opponents both have played, FC Westchester went 2-0-1 and Metropolitan Oval 1-1-1.",
        },
        {
            "id": "6200:rule:hg-fest-bye",
            "kind": "stakes",
            "rule_id": "hg-fest-bye",
            "confirmed": False,
            "text": "Unconfirmed: League → Fest. The top 4 in each conference may get a group-stage bye.",
        },
    ],
    age_group={"id": 16, "name": "U16"},
    division={"id": 11, "name": "Mid-Atlantic", "league": "Homegrown"},
    flags=["adjacent", "qualifying_line"],
    rules=[
        {
            "id": "hg-fest-bye",
            "pathway": "Fest",
            "confirmed": False,
            "summary": "The top 4 in each conference may get a group-stage bye.",
        }
    ],
)

NO_RULES = _candidate(
    7300,
    _team(401, "Boston Bolts", 1, 7, 18),
    _team(402, "Hartford Athletic", 3, 7, 15),
    [
        {
            "id": "7300:pos:home",
            "kind": "standings",
            "team_id": 401,
            "text": "Boston Bolts are 1st of 12 in U17 Academy Northeast with 18 points from 7 games (6-0-1).",
        },
        {
            "id": "7300:pos:away",
            "kind": "standings",
            "team_id": 402,
            "text": "Hartford Athletic are 3rd of 12 in U17 Academy Northeast with 15 points from 7 games (5-0-2).",
        },
        {
            "id": "7300:rate:home",
            "kind": "form",
            "team_id": 401,
            "text": "Boston Bolts score 2.9 and concede 0.7 goals per game in U17 Academy Northeast.",
        },
        {
            "id": "7300:streak:away",
            "kind": "form",
            "team_id": 402,
            "text": "Hartford Athletic have won 4 in a row.",
        },
    ],
    age_group={"id": 17, "name": "U17"},
    division={"id": 12, "name": "Northeast", "league": "Academy"},
    flags=["top_of_table", "qualifying_line"],
)

ALL = {
    "top-clash": TOP_CLASH,
    "low-sample": LOW_SAMPLE,
    "unconfirmed-rule": UNCONFIRMED_RULE,
    "no-rules": NO_RULES,
}


def candidate(name: str) -> MotwCandidate:
    return MotwCandidate.model_validate(ALL[name])


def result(*names: str, **kw: Any) -> CandidatesResult:
    data: dict[str, Any] = {
        "week_start": "2026-11-09",
        "week_end": "2026-11-15",
        "today": "2026-11-10",
        "season": {"id": 3, "name": "2026-2027"},
        "current_pick": None,
        "candidates": [ALL[n] for n in names],
        "reason": None,
        "coverage": {
            "divisions": 8,
            "fixtures_in_week": 31,
            "eligible": 22,
            "teams_low_sample": 5,
            "rules_season": "2026-27",
        },
        "error": None,
    }
    data.update(kw)
    return CandidatesResult.model_validate(data)
