"""mt-agent's eval machinery, offline: truth premises, the evaluators, and `ask`."""

from __future__ import annotations

from typing import Any

import pytest
from mt_fakes import MCP_URL, StubMcp, calls_tool, says, scripted
from pydantic_ai.models.test import TestModel
from pydantic_evals import Case

from mt_agent import evals as E
from mt_agent.agent import build_agent, build_toolset
from mt_agent.runner import TrackedRunner


class FakeTruth(E.MtTruth):
    """MtTruth over canned tool results instead of an MCP session."""

    def __init__(self, **results: Any) -> None:
        self.results = results
        self.calls: list[tuple[str, dict]] = []

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        self.calls.append((tool, args))
        result = self.results[tool]
        return result.pop(0) if isinstance(result, list) else result


@pytest.fixture(autouse=True)
def no_emit(monkeypatch: pytest.MonkeyPatch) -> None:
    from sb_obs import runs

    monkeypatch.setattr(runs, "emit", lambda record: None)


# --- facts -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "fact", "hit"),
    [
        ("IFA are 10th with 6 points", "10", True),
        ("IFA are 110th", "10", False),
        ("6 points", "6", True),
        ("16 points", "6", False),
        ("against valeo futbol club", "Valeo Futbol Club", True),
        ("against Valeo", "Valeo Futbol Club", False),
    ],
)
def test_mentions(text: str, fact: str, hit: bool) -> None:
    assert E.mentions(text, fact) is hit


# --- truth premises ------------------------------------------------------------------

RESOLVED_IFA = {"status": "resolved", "team": {"team_id": 19, "age_group": {"id": 3}}}
ROW = {
    "position": 10,
    "team": {"id": 19},
    "points": 6,
    "played": 5,
    "wins": 2,
    "draws": 0,
    "losses": 3,
}
TABLE = {
    "rows": [ROW],
    "division": {"name": "Northeast", "league": "Homegrown"},
    "coverage": {"matches_counted": 51, "fixtures": 190},
}


async def test_team_standing_truth_comes_from_the_table() -> None:
    truth = await E._team_standing(FakeTruth(search_teams=RESOLVED_IFA, get_standings=TABLE))
    assert truth.meta.facts == ["10", "6", "190"]
    assert truth.meta.tools == ["search_teams", "get_standings"]
    assert "51 played of 190 scheduled" in truth.expected


async def test_a_changed_premise_is_a_broken_eval_not_a_failure() -> None:
    # Metropolitan Oval became one team: the case can no longer test "ask which one".
    fake = FakeTruth(search_teams={"status": "resolved", "team": {}, "candidates": []})
    spec = next(s for s in E.SPECS if s.name == "ambiguous-team")
    with pytest.raises(E.TruthError, match="ambiguous-team: .*expected ambiguous"):
        await E.build_cases(fake, [spec])


async def test_every_premise_is_checked_before_reporting() -> None:
    fake = FakeTruth(search_teams={"status": "resolved", "team": {}, "candidates": []})
    specs = [s for s in E.SPECS if s.name in ("ambiguous-team", "unknown-team")]
    with pytest.raises(E.TruthError) as exc:
        await E.build_cases(fake, specs)
    assert "ambiguous-team" in str(exc.value) and "unknown-team" in str(exc.value)


# --- evaluators ----------------------------------------------------------------------


def report_for(answer: E.Answer, meta: E.Meta, expected: str = "x"):
    case = Case(name="c", inputs=["q?"], expected_output=expected, metadata=meta)
    dataset = E.build_dataset(
        [case], judge=TestModel(custom_output_args={"reason": "ok", "pass": True, "score": 1.0})
    )

    async def task(turns: list[str]) -> E.Answer:
        return answer

    return dataset.evaluate(task, progress=False)


async def test_all_checks_pass_on_a_matching_answer() -> None:
    answer = E.Answer("IFA are 10th with 6 points.", "high", ["search_teams", "get_standings"])
    meta = E.Meta(facts=["10", "6"], confidence="high", tools=["search_teams", "get_standings"])
    report = await report_for(answer, meta)
    [case] = report.cases
    assert {n: a.value for n, a in case.assertions.items()} == {
        "FactsMentioned": True,
        "ConfidenceMatches": True,
        "ToolPath": True,
        "LLMJudge": True,
    }


@pytest.mark.parametrize(
    ("tools", "ok"),
    [
        (["search_teams", "get_standings"], True),
        (["search_teams", "search_teams", "get_standings"], True),  # a retry is fine
        (["get_standings", "search_teams"], False),  # wrong order
        (["search_teams"], False),  # never read the table
        ([], False),
    ],
)
async def test_tool_path_uses_adk_in_order_match(tools: list[str], ok: bool) -> None:
    report = await report_for(
        E.Answer("t", "high", tools), E.Meta(tools=["search_teams", "get_standings"])
    )
    assert report.cases[0].assertions["ToolPath"].value is ok


async def test_wrong_fact_or_confidence_fails() -> None:
    answer = E.Answer("IFA are 11th.", "low", ["search_teams", "get_standings"])
    report = await report_for(answer, E.Meta(facts=["10"], confidence="high"))
    assertions = report.cases[0].assertions
    assert assertions["FactsMentioned"].value is False
    assert assertions["ConfidenceMatches"].value is False


async def test_unchecked_dimensions_are_skipped_not_passed() -> None:
    report = await report_for(E.Answer("Which team?", "low", []), E.Meta())
    assert set(report.cases[0].assertions) == {"LLMJudge"}


# --- ask: the real runner, scripted model, stub mt-mcp --------------------------------


async def test_ask_returns_text_confidence_tools_and_cost() -> None:
    stub = StubMcp()
    model = scripted(
        calls_tool("search_teams", query="IFA", age_group="U14"),
        calls_tool("get_standings", team_id=102, age_group_id=14),
        says("IFA is 1st (18 played of 42 scheduled)."),
    )
    async with stub.running():
        toolset = build_toolset(MCP_URL, "t", httpx_client_factory=stub.client_factory())
        runner = TrackedRunner(build_agent(model, toolset), caller="eval")
        answer = await E.ask(runner, ["Where are IFA U14?"])
        await toolset.close()
    assert answer.text == "IFA is 1st (18 played of 42 scheduled)."
    assert answer.confidence == "high"
    assert answer.tools == ["search_teams", "get_standings"]
    assert answer.cost_usd and answer.cost_usd > 0
