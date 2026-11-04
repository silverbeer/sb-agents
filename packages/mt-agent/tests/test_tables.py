"""Typed tables from tool results (SB-1528)."""

from mt_agent.tables import results_table, standings_table, tables_from, upcoming_table

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
    assert table["title"] == "Homegrown Northeast U15 (League): 51 played of 190 scheduled"
    assert table["columns"] == ["#", "Team", "P", "W", "D", "L", "GD", "Pts"]
    assert table["rows"] == [["1", "Metropolitan Oval", "9", "7", "1", "1", "+20", "22"]]


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
