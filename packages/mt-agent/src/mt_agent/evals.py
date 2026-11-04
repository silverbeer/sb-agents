"""Eval set for mt-agent, with ground truth read from MT at eval time. Not part of CI.

    op run --env-file=.env.op --env-file=.env.mt.op -- uv run mt-agent-eval

MT data moves every weekend (results, standings, fixtures), so a fixed expected answer
would rot within days. Each case is a question plus a `truth` function that asks mt-mcp
directly, with no model in the loop, and builds the expected answer from what it gets
back. The agent then answers the same question through its own tools, and four checks
compare the two:

- **FactsMentioned** (deterministic): the key facts from the truth (a position, points,
  an opponent) appear in the answer text.
- **Confidence**: the data part's confidence is what the case expects ("high" for a
  clean lookup, "low" when the agent has to ask or found nothing).
- **ToolPath**: the expected tools ran in order (ADK's `TrajectoryEvaluator`, IN_ORDER,
  arguments ignored). Extra calls are allowed: a retry or a second search is fine.
- **LLMJudge** (Sonnet): the answer agrees with the truth and asks rather than guesses
  when it should.

If MT's data no longer fits a case (a team became unambiguous, a name started matching),
`truth` raises `TruthError` and the run stops: that's a broken eval, not a bad agent.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from google.adk.evaluation.eval_case import IntermediateData, Invocation
from google.adk.evaluation.eval_metrics import EvalMetric, ToolTrajectoryCriterion
from google.adk.evaluation.trajectory_evaluator import TrajectoryEvaluator
from google.genai import types
from pydantic_ai.models import Model
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, LLMJudge

from mt_agent.agent import build_agent, build_toolset
from mt_agent.mt_client import McpCallError, MtClient, connect
from mt_agent.runner import ToolLedger, TrackedRunner, is_answer
from sb_obs import EvalRecord, emit_eval, setup_telemetry
from sb_obs.evals import PricedModel, summarize

PASS_THRESHOLD = 0.8
JUDGE_MODEL = os.environ.get("MT_AGENT_JUDGE_MODEL", "anthropic:claude-sonnet-5-5")
SUITE = "mt-agent"

Confidence = Literal["high", "low"]
Turns = list[str]  # a case's user messages, in one session; the last one is scored


class TruthError(RuntimeError):
    """MT's data no longer fits a case's premise: fix the case, not the agent."""


# --- ground truth: mt-mcp with no model in the loop ---------------------------------


class MtTruth(MtClient):
    """mt-mcp with no model in the loop, plus the helpers the cases share."""

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        try:
            return await super().call(tool, **args)
        except McpCallError as exc:
            raise TruthError(str(exc)) from exc

    async def team(self, query: str, age_group: str) -> tuple[int, int]:
        """(team_id, age_group_id) for a team the case expects to resolve uniquely."""
        found = await self.call("search_teams", query=query, age_group=age_group)
        if found["status"] != "resolved":
            raise TruthError(f"{query} {age_group} is {found['status']}, expected resolved")
        team = found["team"]
        return team["team_id"], team["age_group"]["id"]

    @staticmethod
    def row(standings: dict[str, Any], team_id: int) -> dict[str, Any]:
        for row in standings.get("rows") or []:
            if row["team"]["id"] == team_id:
                return row
        raise TruthError(f"team {team_id} not in table: {standings.get('error')}")


@asynccontextmanager
async def mt_truth(url: str | None = None, token: str | None = None) -> AsyncIterator[MtTruth]:
    async with connect(url, token) as client:
        yield MtTruth(client.session)


# --- cases --------------------------------------------------------------------------


@dataclass
class Meta:
    facts: list[str] = field(default_factory=list)  # each must appear in the answer
    confidence: Confidence | None = None  # None: not checked
    tools: list[str] = field(default_factory=list)  # expected in order; extras allowed


@dataclass
class Truth:
    expected: str  # the reference answer, for the judge
    meta: Meta


@dataclass(frozen=True)
class Spec:
    name: str
    question: str
    truth: Callable[[MtTruth], Awaitable[Truth]]
    before: tuple[str, ...] = ()  # earlier turns in the same session (follow-up cases)


def _record(row: dict[str, Any]) -> str:
    return f"{row['wins']}W {row['draws']}D {row['losses']}L"


async def _team_standing(t: MtTruth) -> Truth:
    team_id, age_id = await t.team("IFA", "U15")
    table = await t.call("get_standings", team_id=team_id, age_group_id=age_id)
    row = t.row(table, team_id)
    cov = table["coverage"]
    division = table["division"]
    return Truth(
        f"IFA U15 are {row['position']} of {len(table['rows'])} in the {division['league']} "
        f"{division['name']} table with {row['points']} points ({_record(row)}, "
        f"{row['played']} played). Coverage: {cov['matches_counted']} played of "
        f"{cov['fixtures']} scheduled.",
        Meta(
            facts=[str(row["position"]), str(row["points"]), str(cov["fixtures"])],
            confidence="high",
            tools=["search_teams", "get_standings"],
        ),
    )


async def _next_match(t: MtTruth) -> Truth:
    team_id, age_id = await t.team("IFA", "U15")
    upcoming = await t.call("get_upcoming_matches", team_id=team_id, age_group_id=age_id)
    if not upcoming["matches"]:
        raise TruthError("IFA U15 has no upcoming matches; pick a team that does")
    m = upcoming["matches"][0]
    opponent = m["away_team"] if m["is_home"] else m["home_team"]
    where = "at home" if m["is_home"] else "away"
    return Truth(
        f"Next: {m['match_date']} (kickoff {m['kickoff']}), {where} against "
        f"{opponent['name']} ({m['competition']}).",
        Meta(
            facts=[opponent["name"]],
            confidence="high",
            tools=["search_teams", "get_upcoming_matches"],
        ),
    )


async def _division_leader(t: MtTruth) -> Truth:
    found = await t.call("list_divisions", age_group="U15", query="Northeast")
    matches = [d for d in found["divisions"] if d["division"]["league"] == "Homegrown"]
    if len(matches) != 1:
        raise TruthError(f"expected one Homegrown Northeast division, got {len(matches)}")
    division = matches[0]["division"]
    age_id = matches[0]["age_groups"][0]["id"]
    table = await t.call("get_standings", division_id=division["id"], age_group_id=age_id)
    if not table.get("rows"):
        raise TruthError(f"U15 Homegrown Northeast table is empty: {table.get('error')}")
    top = table["rows"][0]
    return Truth(
        f"{top['team']['name']} top the U15 Homegrown Northeast table with {top['points']} "
        f"points from {top['played']} games.",
        Meta(
            facts=[top["team"]["name"], str(top["points"])],
            confidence="high",
            tools=["list_divisions", "get_standings"],
        ),
    )


async def _flex_table(t: MtTruth) -> Truth:
    team_id, age_id = await t.team("IFA", "U15")
    first = await t.call("get_standings", team_id=team_id, age_group_id=age_id, competition="Flex")
    options = [o for o in first.get("options") or [] if o["division"]["league"] == "Flex"]
    if len(options) != 1:
        raise TruthError(f"expected one Flex table for IFA U15, got {len(options)}")
    division = options[0]["division"]
    table = await t.call(
        "get_standings",
        team_id=team_id,
        age_group_id=age_id,
        division_id=division["id"],
        competition="Flex",
    )
    row = t.row(table, team_id)
    return Truth(
        f"In the Flex {division['name']} table IFA U15 are {row['position']} of "
        f"{len(table['rows'])} with {row['points']} points ({_record(row)}).",
        Meta(
            facts=[str(row["position"]), str(row["points"])],
            confidence="high",
            tools=["search_teams", "get_standings"],
        ),
    )


async def _qualifying(t: MtTruth) -> Truth:
    team_id, age_id = await t.team("IFA", "U15")
    table = await t.call(
        "get_standings", team_id=team_id, age_group_id=age_id, competition="qualifying"
    )
    row = t.row(table, team_id)
    outside = table["coverage"]["matches_vs_outside_table"]
    return Truth(
        f"IFA U15's qualifying record (League + Flex combined): {_record(row)}, "
        f"{row['points']} points from {row['played']} games. It includes {outside} matches "
        "against teams outside the table.",
        Meta(
            facts=[str(row["wins"]), str(row["losses"]), str(row["played"])],
            confidence="high",
            tools=["search_teams", "get_standings"],
        ),
    )


async def _ambiguous_team(t: MtTruth) -> Truth:
    found = await t.call("search_teams", query="Metropolitan Oval")
    if found["status"] != "ambiguous":
        raise TruthError(f"Metropolitan Oval is {found['status']}, expected ambiguous")
    ages = sorted({c["age_group"]["name"] for c in found["candidates"]})
    return Truth(
        "Metropolitan Oval has several teams; ask which one is meant "
        f"(age groups {', '.join(ages)}) instead of picking one.",
        Meta(confidence="low", tools=["search_teams"]),
    )


async def _unknown_team(t: MtTruth) -> Truth:
    found = await t.call("search_teams", query="Zxqv Rovers")
    if found["status"] != "not_found":
        raise TruthError(f"Zxqv Rovers is {found['status']}, expected not_found")
    return Truth(
        "No team called Zxqv Rovers was found in MT. Say so; don't guess a team.",
        Meta(confidence="low", tools=["search_teams"]),
    )


async def _other_ages(t: MtTruth) -> Truth:
    found = await t.call("search_teams", query="IFA", age_group="U12")
    if found["status"] != "not_found" or not found.get("other_ages"):
        raise TruthError(f"IFA U12 is {found['status']} with no other_ages")
    ages = found["other_ages"]
    return Truth(
        f"IFA has no U12 team in MT; it plays at {', '.join(ages)}.",
        Meta(facts=[ages[0]], confidence="low", tools=["search_teams"]),
    )


def _score(m: dict[str, Any]) -> str:
    """A result as the team saw it: "W 2-1 vs Bayside FC (home)"."""
    opponent = m["away_team"] if m["is_home"] else m["home_team"]
    pens = ""
    if m.get("home_penalty_score") is not None:
        pens = f", {m['home_penalty_score']}-{m['away_penalty_score']} on penalties"
    where = "home" if m["is_home"] else "away"
    return (
        f"{m['match_date']}: {m['result']} {m['goals_for']}-{m['goals_against']} vs "
        f"{opponent['name']} ({where}{pens}, {m['competition']})"
    )


async def _last_result(t: MtTruth) -> Truth:
    team_id, age_id = await t.team("IFA", "U15")
    recent = await t.call("get_recent_results", team_id=team_id, age_group_id=age_id, limit=1)
    if not recent.get("matches"):
        raise TruthError(f"IFA U15 has no played match: {recent.get('error')}")
    m = recent["matches"][0]
    opponent = m["away_team"] if m["is_home"] else m["home_team"]
    return Truth(
        f"IFA U15's last game: {_score(m)}.",
        Meta(
            facts=[opponent["name"], str(m["goals_for"]), str(m["goals_against"])],
            confidence="high",
            tools=["search_teams", "get_recent_results"],
        ),
    )


async def _form(t: MtTruth) -> Truth:
    team_id, age_id = await t.team("IFA", "U15")
    recent = await t.call("get_recent_results", team_id=team_id, age_group_id=age_id, limit=5)
    matches, summary = recent.get("matches") or [], recent.get("summary") or {}
    if len(matches) < 5:
        raise TruthError(f"IFA U15 has {len(matches)} played matches, need 5")
    return Truth(
        f"IFA U15's last 5: {summary['wins']}W {summary['draws']}D {summary['losses']}L, "
        f"goals {summary['goals_for']}-{summary['goals_against']}. "
        + "; ".join(_score(m) for m in matches),
        Meta(
            facts=[str(summary["wins"]), str(summary["losses"])],
            confidence="high",
            tools=["search_teams", "get_recent_results"],
        ),
    )


async def _head_to_head(t: MtTruth) -> Truth:
    team_id, age_id = await t.team("IFA", "U15")
    opponent_id, _ = await t.team("FC Westchester", "U15")
    h2h = await t.call(
        "get_recent_results",
        team_id=team_id,
        age_group_id=age_id,
        opponent_id=opponent_id,
        limit=20,
    )
    matches, summary = h2h.get("matches") or [], h2h.get("summary") or {}
    if not matches:
        raise TruthError("IFA U15 and FC Westchester U15 have no played match")
    return Truth(
        f"IFA U15 vs FC Westchester: {summary['wins']}W {summary['draws']}D "
        f"{summary['losses']}L in {summary['played']} games. "
        + "; ".join(_score(m) for m in matches),
        Meta(
            facts=[str(matches[0]["goals_for"]), str(matches[0]["goals_against"])],
            confidence="high",
            tools=["search_teams", "get_recent_results"],
        ),
    )


async def _preview(t: MtTruth) -> Truth:
    team_id, age_id = await t.team("IFA", "U15")
    upcoming = await t.call("get_upcoming_matches", team_id=team_id, age_group_id=age_id, limit=1)
    if not upcoming["matches"]:
        raise TruthError("IFA U15 has no upcoming match to preview")
    m = upcoming["matches"][0]
    opponent = m["away_team"] if m["is_home"] else m["home_team"]
    form = await t.call("get_recent_results", team_id=team_id, age_group_id=age_id, limit=5)
    opp_form = await t.call(
        "get_recent_results", team_id=opponent["id"], age_group_id=age_id, limit=5
    )
    s, o = form.get("summary") or {}, opp_form.get("summary") or {}
    where = "home" if m["is_home"] else "away"
    return Truth(
        f"Preview: {m['match_date']} ({m['kickoff']}), IFA U15 {where} vs {opponent['name']} "
        f"({m['competition']}). IFA last 5: {s.get('wins')}W {s.get('draws')}D "
        f"{s.get('losses')}L. {opponent['name']} last 5: {o.get('wins')}W {o.get('draws')}D "
        f"{o.get('losses')}L. Any table position and head-to-head given must match MT. "
        "No prediction of the result.",
        Meta(facts=[opponent["name"]], confidence="high", tools=["search_teams"]),
    )


async def _follow_up(t: MtTruth) -> Truth:
    # Turn 1 asked about IFA U15; "their U14 team" only resolves from that context.
    team_id, age_id = await t.team("IFA", "U14")
    table = await t.call("get_standings", team_id=team_id, age_group_id=age_id)
    row = t.row(table, team_id)
    return Truth(
        f"(Follow-up: 'their' is IFA, from the previous turn.) IFA U14 are {row['position']} "
        f"with {row['points']} points ({_record(row)}).",
        Meta(
            facts=[str(row["position"]), str(row["points"])],
            confidence="high",
            tools=["search_teams", "get_standings"],
        ),
    )


async def _my_team(t: MtTruth) -> Truth:
    return Truth(
        "The agent doesn't know who is asking: it should ask which team they mean.",
        Meta(confidence="low"),
    )


async def _no_qualification_call(t: MtTruth) -> Truth:
    # "Playoffs" fairly reads as either table, so the truth carries both; the point of the
    # case is that the answer doesn't call qualification.
    team_id, age_id = await t.team("FC Westchester", "U15")
    league = t.row(await t.call("get_standings", team_id=team_id, age_group_id=age_id), team_id)
    combined = t.row(
        await t.call(
            "get_standings", team_id=team_id, age_group_id=age_id, competition="qualifying"
        ),
        team_id,
    )
    return Truth(
        f"League table: FC Westchester U15 are {league['position']} with {league['points']} "
        f"points. Qualifying (League + Flex) record: {_record(combined)}, "
        f"{combined['points']} points. Either is a fair basis. MT has no qualification "
        "cutoff, so the answer must not say whether they make the playoffs.",
        Meta(tools=["search_teams", "get_standings"]),
    )


SPECS: list[Spec] = [
    Spec("team-standing", "Where are IFA U15 in the league standings?", _team_standing),
    Spec("next-match", "When is IFA U15's next game, and who is it against?", _next_match),
    Spec("division-leader", "Who is top of the U15 Homegrown Northeast table?", _division_leader),
    Spec("flex-table", "How are IFA U15 doing in the Flex table?", _flex_table),
    Spec("qualifying-record", "What is IFA U15's qualifying record?", _qualifying),
    Spec("ambiguous-team", "How is Metropolitan Oval doing in the standings?", _ambiguous_team),
    Spec("unknown-team", "What are the standings for Zxqv Rovers?", _unknown_team),
    Spec("other-ages", "What are the standings for IFA U12?", _other_ages),
    Spec("last-result", "How did IFA U15 do in their last game?", _last_result),
    Spec("recent-form", "What's IFA U15's record over their last 5 games?", _form),
    Spec("head-to-head", "How has IFA U15 done against FC Westchester?", _head_to_head),
    Spec("match-preview", "Preview IFA U15's next game.", _preview),
    Spec(
        "follow-up",
        "And their U14 team?",
        _follow_up,
        before=("Where are IFA U15 in the league standings?",),
    ),
    Spec("my-team", "When is our next game?", _my_team),
    Spec(
        "no-qualification-call",
        "Will FC Westchester U15 make the playoffs?",
        _no_qualification_call,
    ),
]


async def build_cases(truth: MtTruth, specs: list[Spec] = SPECS) -> list[Case[Turns, Answer, Meta]]:
    """Ground truth for every case. All premises are checked before any model call."""
    cases, broken = [], []
    for spec in specs:
        try:
            t = await spec.truth(truth)
        except TruthError as exc:
            broken.append(f"{spec.name}: {exc}")
            continue
        cases.append(
            Case(
                name=spec.name,
                inputs=[*spec.before, spec.question],
                expected_output=t.expected,
                metadata=t.meta,
            )
        )
    if broken:
        raise TruthError("; ".join(broken))
    return cases


# --- the agent under test -----------------------------------------------------------


@dataclass
class Answer:
    text: str
    confidence: Confidence
    tools: list[str]
    cost_usd: float | None = None


async def ask(runner: TrackedRunner, turns: Turns) -> Answer:
    """Each turn through the real runner (caps, RunRecord) in one session, as A2A runs a
    conversation with a reused contextId. The answer, confidence and tools are the last
    turn's; cost is the RunRecords' (so it includes the preview workflow's writer).
    Give each case its own runner: `last_usage` is per runner."""
    session = await runner.session_service.create_session(app_name=runner.app_name, user_id="eval")
    cost: float | None = None
    for question in turns:
        message = types.Content(role="user", parts=[types.Part(text=question)])
        ledger, text = ToolLedger(), ""
        async for event in runner.run_async(
            user_id="eval", session_id=session.id, new_message=message
        ):
            ledger.see(event)
            if is_answer(event, runner.agent.name):
                text = "\n".join(p.text for p in event.content.parts if p.text)
        turn_cost = runner.last_usage.get("cost_usd")
        if turn_cost is not None:
            cost = (cost or 0.0) + turn_cost
    return Answer(text, ledger.confidence, ledger.calls, cost)


# --- evaluators ---------------------------------------------------------------------


def mentions(text: str, fact: str) -> bool:
    """Case-insensitive; a number must stand alone ("10" matches "10th", not "110")."""
    if fact.isdigit():
        return re.search(rf"(?<!\d){fact}(?!\d)", text) is not None
    return fact.casefold() in text.casefold()


@dataclass
class FactsMentioned(Evaluator[Turns, Answer, Meta]):
    def evaluate(self, ctx: EvaluatorContext[Turns, Answer, Meta]) -> bool | dict[str, bool]:
        facts = ctx.metadata.facts if ctx.metadata else []
        return all(mentions(ctx.output.text, f) for f in facts) if facts else {}


@dataclass
class ConfidenceMatches(Evaluator[Turns, Answer, Meta]):
    def evaluate(self, ctx: EvaluatorContext[Turns, Answer, Meta]) -> bool | dict[str, bool]:
        expected = ctx.metadata.confidence if ctx.metadata else None
        return {} if expected is None else ctx.output.confidence == expected


_TRAJECTORY = TrajectoryEvaluator(
    eval_metric=EvalMetric(
        metric_name="tool_trajectory_avg_score",
        criterion=ToolTrajectoryCriterion(
            threshold=1.0,
            match_type=ToolTrajectoryCriterion.MatchType.IN_ORDER,
            ignore_args=True,
        ),
    )
)


def _invocation(turns: Turns, tools: list[str]) -> Invocation:
    calls = [types.FunctionCall(name=name, args={}) for name in tools]
    return Invocation(
        user_content=types.Content(role="user", parts=[types.Part(text=turns[-1])]),
        intermediate_data=IntermediateData(tool_uses=calls),
    )


@dataclass
class ToolPath(Evaluator[Turns, Answer, Meta]):
    """ADK's trajectory metric over one invocation: expected tools ran, in order."""

    def evaluate(self, ctx: EvaluatorContext[Turns, Answer, Meta]) -> bool | dict[str, bool]:
        expected = ctx.metadata.tools if ctx.metadata else []
        if not expected:
            return {}
        result = _TRAJECTORY.evaluate_invocations(
            [_invocation(ctx.inputs, ctx.output.tools)], [_invocation(ctx.inputs, expected)]
        )
        return result.overall_score == 1.0


RUBRIC = (
    "The expected output is ground truth read from MissingTable's API just now. The "
    "answer must agree with it: same facts, nothing that contradicts it. Extra correct "
    "detail is fine and dates may be written in any format. When the expected output says "
    "to ask a question or that nothing was found, the answer must do that instead of "
    "guessing or inventing data. The input lists the conversation's user messages; only "
    "the reply to the last one is graded."
)


def build_dataset(
    cases: list[Case[Turns, Answer, Meta]], judge: Model | str | None = None
) -> Dataset[Turns, Answer, Meta]:
    return Dataset(
        name=SUITE,
        cases=cases,
        evaluators=[
            FactsMentioned(),
            ConfidenceMatches(),
            ToolPath(),
            LLMJudge(
                rubric=RUBRIC,
                model=judge or JUDGE_MODEL,
                include_input=True,
                include_expected_output=True,
            ),
        ],
    )


# --- run ------------------------------------------------------------------------------


async def run_evals(
    only: list[str] | None = None, verbose: bool = False, preview: bool = True
) -> float:
    specs = [s for s in SPECS if not only or s.name in only]
    broken: TruthError | None = None
    async with mt_truth() as truth:
        try:
            cases = await build_cases(truth, specs)
        except TruthError as exc:
            broken = exc
    if broken is not None:  # raised outside the MCP task group, so not an ExceptionGroup
        raise broken
    toolset = build_toolset()
    agent = build_agent(toolset=toolset, preview=preview)
    model = TrackedRunner(agent).model
    judge = PricedModel(JUDGE_MODEL)
    agent_cost: list[float] = []

    async def answer(turns: Turns) -> Answer:
        out = await ask(TrackedRunner(agent, caller="eval"), turns)
        if out.cost_usd is not None:
            agent_cost.append(out.cost_usd)
        return out

    start = time.perf_counter()
    try:
        report = await build_dataset(cases, judge).evaluate(
            answer, max_concurrency=4, progress=False
        )
    finally:
        await toolset.close()
    report.print(include_input=verbose, include_output=verbose, include_durations=True)

    passed, total, scores = summarize(report)
    rate = passed / total if total else 0.0
    record = EvalRecord(
        suite=SUITE,
        model=model,
        judge_model=judge.model_name,
        cases=total,
        passed=passed,
        pass_rate=rate,
        scores=scores,
        agent_cost_usd=sum(agent_cost) if agent_cost else None,
        judge_cost_usd=judge.cost_usd,
        duration_s=round(time.perf_counter() - start, 1),
    )
    emit_eval(record)
    print(f"\n{passed}/{total} cases passed ({rate:.0%}); threshold {PASS_THRESHOLD:.0%}")
    print("per evaluator: " + ", ".join(f"{k} {v:.0%}" for k, v in scores.items()))
    agent = "unpriced" if record.agent_cost_usd is None else f"${record.agent_cost_usd:.4f}"
    print(f"cost: agent {agent}, judge ${judge.cost_usd or 0:.4f}")
    for f in report.failures:
        print(f"  ERROR {f.name}: {f.error_message}")
    return rate


def main() -> None:
    parser = argparse.ArgumentParser(prog="mt-agent-eval", description=__doc__.split("\n")[0])
    parser.add_argument("--model", help="agent model (default: $MT_AGENT_MODEL or registry)")
    parser.add_argument("--case", action="append", help="run only this case (repeatable)")
    parser.add_argument("-v", "--verbose", action="store_true", help="print answers")
    parser.add_argument(
        "--no-preview",
        action="store_true",
        help="drop the match_preview workflow tool (single-agent baseline, SB-1527)",
    )
    args = parser.parse_args()
    if args.model:
        os.environ["MT_AGENT_MODEL"] = args.model
    setup_telemetry("mt-agent")
    try:
        rate = asyncio.run(run_evals(args.case, args.verbose, not args.no_preview))
    except TruthError as exc:
        raise SystemExit(f"broken eval (MT data changed): {exc}") from exc
    sys.exit(0 if rate >= PASS_THRESHOLD else 1)
