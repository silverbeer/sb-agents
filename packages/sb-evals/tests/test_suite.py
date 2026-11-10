"""The behaviour a re-implementation has to keep. No real model anywhere."""

import asyncio
from dataclasses import dataclass

import pytest
from pydantic_evals import Case
from pydantic_evals.evaluators import EvaluationReason, Evaluator, EvaluatorContext

from sb_evals import ExecutionRecord, SuiteRecord, guarded, run_suite, score_executions
from sb_evals.obs_adapter import emit_to_sb_obs
from sb_evals.suite import REASON_CHARS


@dataclass
class IsGood(Evaluator):
    def evaluate(self, ctx: EvaluatorContext) -> EvaluationReason:
        return EvaluationReason(value=ctx.output == "good", reason=f"got {ctx.output}")


@dataclass
class Boom(Evaluator):
    def evaluate(self, ctx: EvaluatorContext) -> bool:
        raise RuntimeError("secret api detail")


@dataclass
class LongReason(Evaluator):
    def evaluate(self, ctx: EvaluatorContext) -> EvaluationReason:
        return EvaluationReason(value=False, reason="x" * 1000)


@pytest.fixture
def sunk() -> list[SuiteRecord]:
    return []


def runs(*outputs: str) -> list[ExecutionRecord]:
    return [
        ExecutionRecord(name=f"#{n}", agent="a", model="m", inputs={"n": n}, output=o)
        for n, o in enumerate(outputs, 1)
    ]


async def test_scoring_finished_runs_reports_them_by_execution(sunk: list) -> None:
    executions = runs("good", "good")
    result = await score_executions("s-live", executions, [IsGood()], sink=sunk.append)
    assert (result.passed, result.total, result.pass_rate) == (2, 2, 1.0)
    assert result.summary() == "2/2 passed."
    [record] = sunk
    assert (record.suite, record.model, record.judge_model) == ("s-live", "m", "none")
    assert record.execution_ids == [e.execution_id for e in executions]


async def test_a_failed_check_is_named_with_its_reason(sunk: list) -> None:
    result = await score_executions("s", runs("good", "bad"), [IsGood()], sink=sunk.append)
    assert result.summary() == "1/2 passed. #2 failed IsGood: got bad."
    assert sunk[0].scores == {"IsGood": 0.5}


async def test_each_output_is_scored_against_its_own_case(sunk: list) -> None:
    result = await score_executions(
        "s", runs("good", "bad", "good", "bad"), [IsGood()], sink=sunk.append
    )
    by_name = {c.name: c.passed for c in result.cases}
    assert by_name == {"#1": True, "#2": False, "#3": True, "#4": False}


async def test_a_check_that_raises_is_not_a_pass(sunk: list) -> None:
    result = await score_executions("s", runs("good"), [IsGood(), Boom()], sink=sunk.append)
    assert (result.passed, result.pass_rate) == (0, 0.0)
    assert result.summary() == "0/1 passed. #1 could not run Boom."
    assert "secret" not in result.summary()


async def test_a_task_that_raises_counts_against_the_suite(sunk: list) -> None:
    async def task(inputs: str) -> str:
        if inputs == "bad":
            raise RuntimeError("secret api detail")
        return "good"

    cases = [Case(name="a", inputs="ok"), Case(name="b", inputs="bad")]
    result = await run_suite("s", cases, task, [IsGood()], model="m", sink=sunk.append)
    assert (result.passed, result.total) == (1, 2)
    assert result.summary() == "1/2 passed. b did not run."


async def test_agent_cost_is_read_after_the_run(sunk: list) -> None:
    costs: list[float] = []

    async def task(inputs: str) -> str:
        costs.append(0.01)
        return "good"

    await run_suite(
        "s",
        [Case(name="a", inputs="x")],
        task,
        [IsGood()],
        model="m",
        sink=sunk.append,
        agent_cost_usd=lambda: sum(costs),
    )
    assert sunk[0].agent_cost_usd == 0.01
    assert sunk[0].cost_usd == 0.01  # no judge here, so the total is the agent's alone


async def test_long_reasons_are_clipped(sunk: list) -> None:
    result = await score_executions("s", runs("good"), [LongReason()], sink=sunk.append)
    assert len(result.summary()) < REASON_CHARS + 50


async def test_shared_inputs_are_refused(sunk: list) -> None:
    shared = {"n": 1}
    executions = [
        ExecutionRecord(name=n, agent="a", model="m", inputs=shared, output="good") for n in "ab"
    ]
    with pytest.raises(ValueError):
        await score_executions("s", executions, [IsGood()], sink=sunk.append)


async def test_guarded_returns_the_work() -> None:
    async def work() -> str:
        return "done"

    assert await guarded(work, timeout_s=1, fallback="no", label="w") == "done"


async def test_guarded_turns_an_error_into_the_fallback() -> None:
    async def work() -> str:
        raise RuntimeError("boom")

    assert await guarded(work, timeout_s=1, fallback="no", label="w") == "no"


async def test_guarded_cuts_off_slow_work() -> None:
    async def work() -> str:
        await asyncio.sleep(30)
        return "done"

    assert await guarded(work, timeout_s=0.01, fallback="no", label="w") == "no"


async def test_guarded_lets_cancellation_through() -> None:
    async def work() -> str:
        await asyncio.sleep(30)
        return "done"

    task = asyncio.create_task(guarded(work, timeout_s=30, fallback="no", label="w"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_the_telemetry_adapter_keeps_every_scored_field(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list = []
    monkeypatch.setattr("sb_evals.obs_adapter.emit_eval", emitted.append)
    record = SuiteRecord(
        suite="s",
        model="m",
        judge_model="j",
        cases=2,
        passed=1,
        pass_rate=0.5,
        scores={"IsGood": 0.5},
        judge_cost_usd=0.01,
        execution_ids=["e1"],
    )
    emit_to_sb_obs(record)
    [out] = emitted
    assert (out.suite, out.passed, out.scores, out.cost_usd) == ("s", 1, {"IsGood": 0.5}, 0.01)
