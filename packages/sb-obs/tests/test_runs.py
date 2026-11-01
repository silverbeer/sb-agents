from types import SimpleNamespace

import pytest

from sb_obs import runs
from sb_obs.runs import RunRecord, model_name, price, track_run


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[RunRecord]:
    records: list[RunRecord] = []
    monkeypatch.setattr(runs, "emit", records.append)
    return records


class UsageLimitExceeded(Exception):  # matched by name, like PydanticAI's
    pass


def test_ok_run_records_usage_and_confidence(emitted: list[RunRecord]) -> None:
    usage = SimpleNamespace(
        requests=3,
        tool_calls=2,
        input_tokens=15_000,
        output_tokens=600,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost=None,
    )
    with track_run("docs-agent", caller="cli", model="claude-haiku-5-5") as run:
        run.record_usage(usage)
        run.confidence = "high"
    [rec] = emitted
    assert (rec.agent, rec.caller, rec.model, rec.outcome) == (
        "docs-agent",
        "cli",
        "claude-haiku-5-5",
        "ok",
    )
    assert (rec.requests, rec.tool_calls, rec.input_tokens, rec.output_tokens) == (
        3,
        2,
        15_000,
        600,
    )
    assert rec.cost_usd is None  # unknown stays unknown, never 0
    assert rec.confidence == "high"
    assert rec.trace_id is None or len(rec.trace_id) == 32


def test_usage_accumulates_per_request(emitted: list[RunRecord]) -> None:
    with track_run("sb_router", caller="cli", model="m") as run:
        run.add_usage(requests=1, input_tokens=100, cost_usd=0.001)
        run.add_usage(requests=1, input_tokens=50, cost_usd=0.002)
    assert emitted[0].requests == 2
    assert emitted[0].input_tokens == 150
    assert emitted[0].cost_usd == pytest.approx(0.003)


@pytest.mark.parametrize(
    ("exc", "outcome"), [(UsageLimitExceeded("too many"), "limit"), (ValueError("x"), "error")]
)
def test_failures_are_recorded_and_reraised(
    emitted: list[RunRecord], exc: Exception, outcome: str
) -> None:
    with pytest.raises(type(exc)), track_run("a", caller="cli", model="m"):
        raise exc
    assert emitted[0].outcome == outcome
    assert type(exc).__name__ in emitted[0].error


@pytest.mark.parametrize(
    ("model", "inp", "out", "usd"),
    [
        # Pinned from Anthropic's price list (claude-api reference, cached 2026-10-06):
        # Haiku 5.5 $0.10/$0.50 per MTok under 100K prompt tokens; Sonnet 5.5 $2/$10.
        ("claude-haiku-5-5", 10_000, 1_000, 0.0015),
        ("claude-sonnet-5-5", 10_000, 1_000, 0.03),
        # Haiku 5.5 above 100K prompt tokens: $0.50/$2.50.
        ("claude-haiku-5-5", 200_000, 1_000, 0.1025),
    ],
)
def test_genai_prices_match_anthropic_price_list(
    model: str, inp: int, out: int, usd: float
) -> None:
    assert price(model, input_tokens=inp, output_tokens=out) == pytest.approx(usd)


def test_unknown_model_price_is_none() -> None:
    assert price("no-such-model-xyz", input_tokens=10, output_tokens=10) is None


def test_model_name() -> None:
    assert model_name("anthropic:claude-haiku-5-5") == "claude-haiku-5-5"
    assert model_name(SimpleNamespace(model_name="claude-sonnet-5-5")) == "claude-sonnet-5-5"


def test_emit_eval_logs_flat_scores_and_total_cost(caplog: pytest.LogCaptureFixture) -> None:
    from sb_obs.runs import EvalRecord, emit_eval

    record = EvalRecord(
        suite="docs-agent",
        model="claude-haiku-5-5",
        judge_model="claude-sonnet-5-5",
        cases=13,
        passed=12,
        pass_rate=12 / 13,
        scores={"LLMJudge": 0.9, "Calibrated": 1.0},
        agent_cost_usd=0.02,
        judge_cost_usd=0.05,
    )
    with caplog.at_level("INFO", logger="sb_obs.runs"):
        emit_eval(record)
    [entry] = caplog.records
    assert entry.event == "eval_run"
    assert entry.score_LLMJudge == 0.9
    assert entry.cost_usd == pytest.approx(0.07)


def test_eval_cost_unknown_when_nothing_priced() -> None:
    from sb_obs.runs import EvalRecord

    rec = EvalRecord(suite="s", model="m", judge_model="j", cases=1, passed=1, pass_rate=1.0)
    assert rec.cost_usd is None
