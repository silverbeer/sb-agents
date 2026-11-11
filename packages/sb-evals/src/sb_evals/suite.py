"""Run an eval suite and get one result, whichever way the outputs came to exist.

    run_suite         calls the agent for each case (a fixture eval, by hand)
    score_executions  scores runs that already happened (right after a real run)

Both send one `SuiteRecord` to the `Sink` given. Use a separate suite name for scoring
real runs, so a production signal doesn't mix with a fixture trend.

A case passes only if every check ran and passed. A check that raised (the judge's API
was down, say) is not a pass: the case counts as failed and names the check.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator
from pydantic_evals.reporting import EvaluationReport

from sb_evals.records import CaseResult, ExecutionRecord, SuiteRecord

log = logging.getLogger("sb_evals")

MAX_CONCURRENCY = 4

Sink = Callable[[SuiteRecord], None]
DescribeCase = Callable[[Any, Any], dict[str, Any]]


class CostedJudge(Protocol):
    """A judge model that adds up what it spends. Anything with these two will do."""

    @property
    def model_name(self) -> str: ...

    @property
    def cost_usd(self) -> float | None: ...


@dataclass(frozen=True)
class SuiteResult:
    record: SuiteRecord
    cases: list[CaseResult]
    report: EvaluationReport

    @property
    def passed(self) -> int:
        return self.record.passed

    @property
    def total(self) -> int:
        return self.record.cases

    @property
    def pass_rate(self) -> float:
        return self.record.pass_rate

    def summary(self) -> str:
        """One line: `2/3 passed. #2 failed LLMJudge: unsupported claim.`"""
        head = f"{self.passed}/{self.total} passed."
        problems = [c.describe() for c in self.cases if not c.passed]
        return " ".join([head, "; ".join(problems) + "."]) if problems else head


def _detail(describe_case: DescribeCase | None, case: Any) -> dict[str, Any]:
    """What the agent says is worth logging. Failing to describe a case can't fail the eval."""
    if describe_case is None:
        return {}
    try:
        return describe_case(case.inputs, case.output)
    except Exception:
        log.exception("describing eval case %s failed", case.name)
        return {}


def _case_results(
    report: EvaluationReport,
    executions: Sequence[ExecutionRecord],
    describe_case: DescribeCase | None,
) -> list[CaseResult]:
    ids = {e.name: e.execution_id for e in executions}
    results = [
        CaseResult(
            name=case.name,
            execution_id=ids.get(case.name),
            checks={n: bool(a.value) for n, a in case.assertions.items()},
            reasons={n: a.reason for n, a in case.assertions.items() if a.reason},
            unchecked=[f.name for f in case.evaluator_failures],
            detail=_detail(describe_case, case),
        )
        for case in report.cases
    ]
    results += [
        CaseResult(name=f.name, execution_id=ids.get(f.name), crashed=True) for f in report.failures
    ]
    for case in report.cases:
        for f in case.evaluator_failures:
            log.error("eval check raised: %s on %s: %s", f.name, case.name, f.error_message)
    for f in report.failures:
        log.error("eval case raised: %s: %s", f.name, f.error_message)
    return results


def _scores(report: EvaluationReport) -> dict[str, float]:
    """Fraction passed per check, over the cases where that check ran."""
    results: dict[str, list[bool]] = {}
    for case in report.cases:
        for name, assertion in case.assertions.items():
            results.setdefault(name, []).append(bool(assertion.value))
    return {name: sum(vals) / len(vals) for name, vals in sorted(results.items())}


async def run_suite(
    suite: str,
    cases: Sequence[Case],
    task: Callable[[Any], Awaitable[Any]],
    evaluators: Sequence[Evaluator],
    *,
    model: str,
    sink: Sink,
    judge: CostedJudge | None = None,
    agent_cost_usd: Callable[[], float | None] = lambda: None,
    executions: Sequence[ExecutionRecord] = (),
    describe_case: DescribeCase | None = None,
) -> SuiteResult:
    """Run `task` on every case, score it, and send the record to `sink`.

    `agent_cost_usd` is read after the run, so a task can add up its own cost as it goes.
    `describe_case(inputs, output)` returns what's worth logging about a case.
    """
    start = time.perf_counter()
    dataset = Dataset(name=suite, cases=list(cases), evaluators=list(evaluators))
    report = await dataset.evaluate(task, max_concurrency=MAX_CONCURRENCY, progress=False)
    results = _case_results(report, executions, describe_case)
    passed = sum(r.passed for r in results)
    record = SuiteRecord(
        suite=suite,
        model=model,
        judge_model=judge.model_name if judge else "none",
        cases=len(results),
        passed=passed,
        pass_rate=passed / len(results) if results else 0.0,
        scores=_scores(report),
        agent_cost_usd=agent_cost_usd(),
        judge_cost_usd=judge.cost_usd if judge else None,
        duration_s=round(time.perf_counter() - start, 1),
        case_results=results,
    )
    sink(record)
    return SuiteResult(record, results, report)


async def score_executions(
    suite: str,
    executions: Sequence[ExecutionRecord],
    evaluators: Sequence[Evaluator],
    *,
    sink: Sink,
    judge: CostedJudge | None = None,
    describe_case: DescribeCase | None = None,
) -> SuiteResult:
    """Score runs that already happened. No agent call, so the only cost is the judge's.

    The evaluators are the same ones a fixture suite uses: the runner replays each stored
    output as if the agent had just returned it.
    """
    by_input = {id(e.inputs): e.output for e in executions}
    if len(by_input) != len(executions):
        raise ValueError("each ExecutionRecord needs its own inputs object")

    async def replay(inputs: Any) -> Any:
        return by_input[id(inputs)]

    cases = [
        Case(
            name=e.name,
            inputs=e.inputs,
            expected_output=e.expected_output,
            metadata=e.metadata,
        )
        for e in executions
    ]
    return await run_suite(
        suite,
        cases,
        replay,
        evaluators,
        model=",".join(sorted({e.model for e in executions})),
        sink=sink,
        judge=judge,
        executions=executions,
        describe_case=describe_case,
    )
