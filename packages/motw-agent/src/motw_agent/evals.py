"""motw-agent eval: the writer against the real model. Not in CI (it costs money).

    op run --env-file=.env.op -- uv run motw-agent-eval            # fixtures, ~$0.03
    op run --env-file=.env.op --env-file=.env.mt.op -- uv run motw-agent-eval --live

Fixture cases (`fixtures.py`) each bait one failure; `--live` adds this week's real
candidates from mt-mcp. Checks per case, all of which must pass:

| Check | Kind | What |
|---|---|---|
| ModelDraft | deterministic | the model produced a valid draft within its retries (no fallback) |
| Grounded | deterministic | `check_draft` is clean: reasons cite real facts, numbers match |
| NoInventedQualification | deterministic | qualification only from rule facts; unconfirmed hedged |
| NoZeroForAbsent | deterministic | no 0/zero/no points/winless unless a fact says so |
| LLMJudge | Sonnet | every line is supported by the facts it cites; energetic, not insulting |

The first three restate the output validator on purpose: the validator can be loosened
by a later change, the eval is the record of what "good" meant.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import time
from dataclasses import dataclass

from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, LLMJudge

from motw_agent import fixtures
from motw_agent.candidates import MotwCandidate
from motw_agent.mt import LiveMt, parse_week
from motw_agent.writer import HEDGE, QUALIFICATION, Written, build_writer, check_draft, write
from sb_obs import EvalRecord, emit_eval, model_name, price, setup_telemetry
from sb_obs.evals import PricedModel, summarize

PASS_THRESHOLD = 0.8
JUDGE_MODEL = os.environ.get("MOTW_AGENT_JUDGE_MODEL", "anthropic:claude-sonnet-5-5")

ABSENT_AS_ZERO = re.compile(
    r"\b0\s*(points?|goals?|wins?|games?)\b|\bzero\b|\bno (points|goals|wins)\b|"
    r"\bwinless\b|\bpointless\b|\bscoreless\b|\bgoalless\b|\bhaven'?t (won|scored)\b",
    re.IGNORECASE,
)


def _texts(w: Written) -> list[str]:
    return [w.draft.blurb, *(r.text for r in w.draft.reasons)]


@dataclass
class ModelDraft(Evaluator[MotwCandidate, Written, None]):
    def evaluate(self, ctx: EvaluatorContext[MotwCandidate, Written, None]) -> bool:
        return not ctx.output.fallback


@dataclass
class Grounded(Evaluator[MotwCandidate, Written, None]):
    def evaluate(self, ctx: EvaluatorContext[MotwCandidate, Written, None]) -> bool:
        return not check_draft(ctx.inputs, ctx.output.draft)


@dataclass
class NoInventedQualification(Evaluator[MotwCandidate, Written, None]):
    def evaluate(self, ctx: EvaluatorContext[MotwCandidate, Written, None]) -> bool:
        c, d = ctx.inputs, ctx.output.draft
        facts = {f.id: f for f in c.facts}
        rules = [f for f in c.facts if f.is_rule]
        if QUALIFICATION.search(d.blurb) and not rules:
            return False
        for r in d.reasons:
            cited = [facts[i] for i in r.fact_ids if i in facts]
            if QUALIFICATION.search(r.text) and not any(f.is_rule for f in cited):
                return False
            if any(f.confirmed is False for f in cited) and not HEDGE.search(r.text):
                return False
        return True


@dataclass
class NoZeroForAbsent(Evaluator[MotwCandidate, Written, None]):
    def evaluate(self, ctx: EvaluatorContext[MotwCandidate, Written, None]) -> bool:
        facts = " ".join(f.text for f in ctx.inputs.facts)
        return not any(
            m.group(0).lower() not in facts.lower()
            for t in _texts(ctx.output)
            for m in ABSENT_AS_ZERO.finditer(t)
        )


RUBRIC = (
    "The input is a fixture with facts (each with an id); the output is a blurb and reasons, "
    "each reason citing fact ids. Pass only if: every reason's claim is supported by the "
    "facts it cites (no added stats, records, streaks or qualification claims); the blurb "
    "claims nothing the facts don't support; a fact marked confirmed=false is presented as "
    "unconfirmed; a team with no facts is not described with made-up or zero numbers; the "
    "tone is energetic and never insulting."
)


def build_dataset(cands: dict[str, MotwCandidate], judge: PricedModel) -> Dataset:
    return Dataset(
        name="motw-agent",
        cases=[Case(name=name, inputs=c) for name, c in cands.items()],
        evaluators=[
            ModelDraft(),
            Grounded(),
            NoInventedQualification(),
            NoZeroForAbsent(),
            LLMJudge(rubric=RUBRIC, model=judge, include_input=True),
        ],
    )


async def run_evals(live: bool = False, week: str | None = None) -> float:
    cands = {name: fixtures.candidate(name) for name in fixtures.ALL}
    if live:
        result = await LiveMt().candidates(parse_week(week))
        for c in (result.candidates or [])[:3]:
            cands[f"live-{c.match_id}"] = c
    writer = build_writer()
    judge = PricedModel(JUDGE_MODEL)
    costs: list[float] = []

    async def task(c: MotwCandidate) -> Written:
        w = await write(writer, c)
        if w.usage is not None and w.usage.requests:
            cost = price(
                model_name(writer.model),
                input_tokens=w.usage.input_tokens,
                output_tokens=w.usage.output_tokens,
            )
            if cost is not None:
                costs.append(cost)
        return w

    start = time.perf_counter()
    report = await build_dataset(cands, judge).evaluate(task, max_concurrency=4, progress=False)
    report.print(
        include_input=False, include_output=False, include_durations=True, include_reasons=True
    )
    passed, total, scores = summarize(report)
    rate = passed / total if total else 0.0
    record = EvalRecord(
        suite="motw-agent",
        model=model_name(writer.model),
        judge_model=judge.model_name,
        cases=total,
        passed=passed,
        pass_rate=rate,
        scores=scores,
        agent_cost_usd=sum(costs) if costs else None,
        judge_cost_usd=judge.cost_usd,
        duration_s=round(time.perf_counter() - start, 1),
    )
    emit_eval(record)
    print(f"\n{passed}/{total} cases passed ({rate:.0%}); threshold {PASS_THRESHOLD:.0%}")
    print("per evaluator: " + ", ".join(f"{k} {v:.0%}" for k, v in scores.items()))
    cost = record.cost_usd
    print(f"cost: ${cost:.4f}" if cost is not None else "cost: unknown")
    return rate


def main() -> None:
    p = argparse.ArgumentParser(prog="motw-agent-eval", description=__doc__.splitlines()[0])
    p.add_argument("--live", action="store_true", help="add this week's real candidates")
    p.add_argument("--week", help="with --live: any YYYY-MM-DD in the week")
    p.add_argument("--model", help="writer model (default: MOTW_AGENT_MODEL / registry)")
    args = p.parse_args()
    if args.model:
        os.environ["MOTW_AGENT_MODEL"] = args.model
    telemetry = setup_telemetry("motw-agent")
    try:
        rate = asyncio.run(run_evals(args.live, args.week))
    finally:
        telemetry.shutdown()
    raise SystemExit(0 if rate >= PASS_THRESHOLD else 1)
