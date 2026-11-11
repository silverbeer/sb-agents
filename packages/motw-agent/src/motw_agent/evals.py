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

The same checks also score the real drafts right after `suggest` posts them
(`evaluate_posted`, SB-1620), as the separate suite `motw-agent-posted`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any

from pydantic_evals import Case
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, LLMJudge

from motw_agent import fixtures
from motw_agent.candidates import MotwCandidate
from motw_agent.mt import LiveMt, parse_week
from motw_agent.writer import HEDGE, QUALIFICATION, Written, build_writer, check_draft, write
from sb_evals import ExecutionRecord, run_suite, score_executions
from sb_evals.obs_adapter import emit_to_sb_obs
from sb_obs import model_name, price, setup_telemetry
from sb_obs.evals import PricedModel

PASS_THRESHOLD = 0.8
JUDGE_MODEL = os.environ.get("MOTW_AGENT_JUDGE_MODEL", "anthropic:claude-sonnet-5-5")
POSTED_SUITE = "motw-agent-posted"

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


def build_evaluators(judge: PricedModel) -> list[Evaluator]:
    return [
        ModelDraft(),
        Grounded(),
        NoInventedQualification(),
        NoZeroForAbsent(),
        LLMJudge(rubric=RUBRIC, model=judge, include_input=True),
    ]


def _describe(c: MotwCandidate, w: Written) -> dict[str, Any]:
    """What an `eval_case` log line shows, so a verdict can be checked against the draft."""
    return {
        "match_id": c.match_id,
        "fixture": c.label,
        "fallback": w.fallback,
        "blurb": w.draft.blurb,
        "reasons": [r.text for r in w.draft.reasons],
    }


def _cost(model: str, w: Written) -> float | None:
    if w.usage is None or not w.usage.requests:
        return None
    return price(model, input_tokens=w.usage.input_tokens, output_tokens=w.usage.output_tokens)


async def evaluate_posted(posted: list[tuple[MotwCandidate, Written]], model: str) -> str:
    """Score the drafts a real run just posted; returns the line for the post's thread."""
    judge = PricedModel(JUDGE_MODEL)
    executions = [
        ExecutionRecord(
            name=f"#{n}",
            agent="motw-agent",
            model=model,
            inputs=c,
            output=w,
            input_tokens=w.usage.input_tokens if w.usage else None,
            output_tokens=w.usage.output_tokens if w.usage else None,
            cost_usd=_cost(model, w),
        )
        for n, (c, w) in enumerate(posted, 1)
    ]
    result = await score_executions(
        POSTED_SUITE,
        executions,
        build_evaluators(judge),
        sink=emit_to_sb_obs,
        judge=judge,
        describe_case=_describe,
    )
    # Slack's three control characters: a judge's reason must render as plain text.
    text = result.summary().replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"Eval: {text}"


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
        cost = _cost(model_name(writer.model), w)
        if cost is not None:
            costs.append(cost)
        return w

    result = await run_suite(
        "motw-agent",
        [Case(name=name, inputs=c) for name, c in cands.items()],
        task,
        build_evaluators(judge),
        model=model_name(writer.model),
        sink=emit_to_sb_obs,
        judge=judge,
        agent_cost_usd=lambda: sum(costs) if costs else None,
        describe_case=_describe,
    )
    result.report.print(
        include_input=False, include_output=False, include_durations=True, include_reasons=True
    )
    rate = result.pass_rate
    print(f"\n{result.passed}/{result.total} cases passed ({rate:.0%}); ", end="")
    print(f"threshold {PASS_THRESHOLD:.0%}")
    print("per evaluator: " + ", ".join(f"{k} {v:.0%}" for k, v in result.record.scores.items()))
    cost = result.record.cost_usd
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
