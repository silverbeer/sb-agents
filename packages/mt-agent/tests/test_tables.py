"""Typed tables from tool results (SB-1528)."""

from mt_agent.tables import (
    results_table,
    standings_notes,
    standings_table,
    tables_from,
    upcoming_table,
)

ROW = {
    "position": 1,
    "team": {"id": 21, "name": "Metropolitan Oval"},
    "played": 9,
    "wins": 7,
    "draws": 1,
    "losses": 1,
    "goal_difference": 20,
    "points": 22,
}
STANDINGS = {
    "competition": "League",
    "age_group": {"name": "U15"},
    "division": {"name": "Northeast", "league": "Homegrown"},
    "rows": [ROW],
    "meta": {"coverage": "51 played of 190 scheduled"},
    "error": None,
}
RESULT = {
    "match_date": "2026-11-01",
    "result": "D",
    "goals_for": 1,
    "goals_against": 1,
    "home_penalty_score": 4,
    "away_penalty_score": 3,
    "is_home": False,
    "home_team": {"id": 30, "name": "Ironbound"},
    "away_team": {"id": 19, "name": "IFA"},
    "competition": "Flex",
}
UPCOMING = {
    "matches": [
        {
            "match_date": "2026-11-07",
            "kickoff": "2026-11-07T13:15:00-05:00",
            "home_team": {"name": "IFA"},
            "away_team": {"name": "Valeo"},
            "competition": "Flex",
            "status": "scheduled",
        },
        {
            "match_date": "2026-11-15",
            "kickoff": None,
            "home_team": {"name": "Bayside"},
            "away_team": {"name": "IFA"},
            "status": "tbd",
        },
    ],
    "error": None,
}


def test_standings_rows_are_strings_with_title_and_coverage() -> None:
    table = standings_table(STANDINGS)
    assert table["title"] == "U15 Homegrown Northeast · 51 played of 190 scheduled"
    assert table["columns"] == ["#", "Team", "P", "W", "D", "L", "GD", "Pts", "Form →"]
    assert table["rows"] == [["1", "Metropolitan Oval", "9", "7", "1", "1", "+20", "22", ""]]
    assert table["notes"] == [] and table["highlight"] == []


def test_title_uses_structured_coverage_and_names_the_competition_once() -> None:
    from mt_agent.tables import standings_title

    coverage = {"matches_counted": 11, "fixtures": 21}
    flex = {
        "competition": "Flex",
        "age_group": {"name": "U15"},
        "division": {"name": "New England", "league": "Flex"},
        "coverage": coverage,
    }
    assert standings_title(flex) == "U15 Flex New England · 11 of 21 matches played"
    qualifying = {**STANDINGS, "competition": "qualifying", "coverage": coverage}
    assert standings_title(qualifying) == (
        "U15 Homegrown Northeast · qualifying record · 11 of 21 matches played"
    )


def test_the_requested_team_is_highlighted() -> None:
    rows = [
        ROW,
        {**ROW, "position": 2, "team": {"id": 9, "name": "IFA"}, "is_requested_team": True},
    ]
    assert standings_table({**STANDINGS, "rows": rows})["highlight"] == [1]


def _row(position: int, name: str, played: int, points: int, form: str, change: int = 0):
    return {
        "position": position,
        "team": {"id": position, "name": name},
        "played": played,
        "wins": 0,
        "draws": 0,
        "losses": 0,
        "points": points,
        "form": list(form),
        "position_change": change,
    }


# The top and bottom of U15 Homegrown Northeast on 2026-11-04, from prod.
TABLE = [
    _row(1, "Metropolitan Oval", 9, 22, "LWWWW"),
    _row(2, "FC Westchester", 6, 18, "WWWWW"),
    _row(3, "Cedar Stars Academy Bergen", 6, 16, "WWWWD"),
    _row(6, "Seacoast United", 6, 9, "LLLWW", 4),
    _row(10, "IFA", 5, 6, "LLWLW", 5),
    _row(12, "Valeo Futbol Club", 5, 6, "WWLLL", -3),
    _row(18, "Downtown United Soccer Club", 8, 3, "LLLLL"),
]


def test_notes_name_the_runs_the_movers_and_games_in_hand() -> None:
    assert standings_notes(TABLE) == [
        "Hottest run: **FC Westchester**, 5 straight wins.",
        "Coldest run: **Downtown United Soccer Club**, 5 straight losses.",
        "Biggest climb since the last match day: **IFA**, up 5.",
        "Biggest drop since the last match day: **Valeo Futbol Club**, down 3.",
        "Games in hand: **FC Westchester** (18 pts from 6, 3.00 per game) has played 3 fewer "
        "than leader **Metropolitan Oval** (22 from 9, 2.44).",
    ]


def test_movement_and_form_cells() -> None:
    rows = standings_table({**STANDINGS, "rows": TABLE})["rows"]
    # Movement rides in the # column; form is squares, oldest first (newest on the right).
    assert [(r[0], r[8]) for r in rows[3:6]] == [
        ("6 ▲4", "🟥🟥🟥🟩🟩"),
        ("10 ▲5", "🟥🟥🟩🟥🟩"),
        ("12 ▼3", "🟩🟩🟥🟥🟥"),
    ]
    assert rows[2][8] == "🟩🟩🟩🟩🟨"


def test_quiet_table_has_no_notes() -> None:
    quiet = [_row(1, "A", 4, 7, "WDLW"), _row(2, "B", 4, 6, "DWLW", 1)]
    assert standings_notes(quiet) == []


def test_ties_name_two_teams_and_no_form_is_no_run() -> None:
    rows = [_row(1, "A", 4, 12, "WWWW"), _row(2, "B", 4, 9, "LWWW"), _row(3, "C", 4, 9, "")]
    del rows[2]["form"]
    assert standings_notes(rows) == ["Hottest run: **A**, 4 straight wins."]
    rows[1]["form"] = list("WWWW")
    assert standings_notes(rows)[0] == "Hottest run: **A** and **B**, 4 straight wins."


def test_leader_with_best_rate_or_small_gap_is_no_games_in_hand() -> None:
    leader_best = [_row(1, "A", 6, 18, ""), _row(2, "B", 4, 10, "")]
    small_gap = [_row(1, "A", 6, 13, ""), _row(2, "B", 5, 12, "")]
    assert standings_notes(leader_best) == [] and standings_notes(small_gap) == []


def test_results_are_from_the_teams_side_with_the_shootout() -> None:
    [row] = results_table({"matches": [RESULT]})["rows"]
    assert row == ["2026-11-01", "D", "1-1 (4-3 pens)", "Ironbound", "A", "Flex"]


def test_upcoming_kickoff_is_local_time_or_tbd() -> None:
    rows = upcoming_table(UPCOMING)["rows"]
    assert rows[0][:2] == ["2026-11-07", "13:15"] and rows[1][1] == "TBD"


def test_no_data_or_an_error_is_no_table() -> None:
    assert standings_table({"rows": [], "error": None}) is None
    assert standings_table({"rows": None, "error": {"kind": "ambiguous"}}) is None
    assert results_table({"matches": [], "error": None}) is None


def test_tables_from_follows_tool_order_and_skips_unknown_shapes() -> None:
    tables = tables_from(
        {
            "search_teams": {"status": "resolved"},
            "get_recent_results": {"matches": [RESULT]},
            "get_standings": {"rows": [{"position": 1}]},  # malformed: no table, no crash
            "get_upcoming_matches": UPCOMING,
        }
    )
    assert [t["title"] for t in tables] == ["Recent results", "Upcoming matches"]
