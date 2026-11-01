"""One RunRecord per agent run: the row work would land in BigQuery via the data bus.

Traces are sampled; run records are not. Each record is emitted three ways:
- a JSON log line (`event=agent_run`), which is what the work data bus ships to GCP;
- counters/histograms (`sb_agent_runs`, `sb_agent_cost_usd`, `sb_agent_run_duration`);
- attributes on an `agent_run <agent>` span that wraps the framework's own spans,
  so every run has a trace_id even when nothing upstream started a trace.

Cost comes from the `genai-prices` package (PydanticAI fills `usage.cost` from it),
so there is no hand-kept price table here; `tests/test_runs.py` pins the prices
we expect for the models in use, so a pricing change fails CI instead of
silently changing the numbers.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from opentelemetry import metrics, trace
from pydantic import BaseModel, Field

log = logging.getLogger("sb_obs.runs")
_tracer = trace.get_tracer("sb_obs")

Outcome = Literal["ok", "error", "limit"]


class RunRecord(BaseModel):
    """What one agent run did and cost. Flat, so it maps to one table row."""

    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    trace_id: str | None = None
    agent: str
    caller: str  # slack | a2a | cli | eval
    model: str
    outcome: Outcome
    requests: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None  # None = unknown (model not priced), not zero
    latency_ms: int = 0
    confidence: str | None = None
    routed_to: str | None = None  # comma-separated specialists asked, in order
    escalated: bool = False
    error: str | None = None


@dataclass
class RunHandle:
    """Filled in by the caller while the run executes; read by `track_run` at the end."""

    agent: str
    caller: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    confidence: str | None = None
    routed_to: list[str] = field(default_factory=list)
    escalated: bool = False

    def add_usage(
        self,
        *,
        requests: int = 0,
        tool_calls: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cost_usd: float | None = None,
    ) -> None:
        """Accumulate usage (call once per request for frameworks without a run total)."""
        u = self.usage
        for key, value in (
            ("requests", requests),
            ("tool_calls", tool_calls),
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
            ("cache_read_tokens", cache_read_tokens),
            ("cache_write_tokens", cache_write_tokens),
        ):
            u[key] = u.get(key, 0) + value
        if cost_usd is not None:
            u["cost_usd"] = (u.get("cost_usd") or 0.0) + cost_usd

    def record_usage(self, usage: Any) -> None:
        """Take a PydanticAI `RunUsage` (duck-typed, so sb-obs doesn't import pydantic_ai)."""
        cost = getattr(usage, "cost", None)
        self.add_usage(
            requests=usage.requests,
            tool_calls=usage.tool_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost_usd=float(cost) if cost is not None else None,
        )


@contextmanager
def track_run(agent: str, *, caller: str, model: str) -> Iterator[RunHandle]:
    """Wrap one agent run: span, timing, outcome, and the emitted RunRecord.

    A `UsageLimitExceeded` (PydanticAI, matched by name) is outcome "limit";
    any other exception is "error". Exceptions are always re-raised.
    """
    handle = RunHandle(agent, caller, model)
    outcome: Outcome = "ok"
    error: str | None = None
    start = time.perf_counter()
    with _tracer.start_as_current_span(f"agent_run {agent}") as span:
        try:
            yield handle
        except BaseException as exc:
            outcome = "limit" if type(exc).__name__ == "UsageLimitExceeded" else "error"
            error = f"{type(exc).__name__}: {exc}"[:500]
            raise
        finally:
            ctx = span.get_span_context()
            record = RunRecord(
                trace_id=format(ctx.trace_id, "032x") if ctx.is_valid else None,
                agent=agent,
                caller=caller,
                model=model,
                outcome=outcome,
                latency_ms=int((time.perf_counter() - start) * 1000),
                confidence=handle.confidence,
                routed_to=",".join(handle.routed_to) or None,
                escalated=handle.escalated,
                error=error,
                **handle.usage,
            )
            span.set_attributes(
                {f"sb.run.{k}": v for k, v in record.model_dump().items() if v is not None}
            )
            emit(record)


def emit(record: RunRecord) -> None:
    """Log the record as one JSON line and update the run metrics."""
    level = logging.INFO if record.outcome == "ok" else logging.WARNING
    log.log(level, "agent run", extra={"event": "agent_run", **record.model_dump()})

    meter = metrics.get_meter("sb_obs")
    attrs = {"agent": record.agent, "model": record.model, "caller": record.caller}
    meter.create_counter("sb_agent_runs", description="Agent runs").add(
        1, {**attrs, "outcome": record.outcome}
    )
    if record.cost_usd is not None:
        # No `unit`: the Prometheus exporter would append it (sb_agent_cost_usd_USD_total).
        meter.create_counter("sb_agent_cost_usd", description="Agent spend in USD").add(
            record.cost_usd, attrs
        )
    meter.create_histogram("sb_agent_run_duration", unit="s", description="Run latency").record(
        record.latency_ms / 1000, {**attrs, "outcome": record.outcome}
    )


def model_name(model: Any) -> str:
    """'anthropic:claude-haiku-5-5' or a Model object → a stable model label."""
    if isinstance(model, str):
        return model.split(":", 1)[-1]
    return getattr(model, "model_name", None) or type(model).__name__


def price(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    provider: str = "anthropic",
) -> float | None:
    """USD for one request via genai-prices; None if the model isn't priced.

    For frameworks that don't compute cost themselves (ADK). `input_tokens` here is
    the total prompt, including cached tokens, as genai-prices expects.
    """
    try:
        from genai_prices import Usage, calc_price

        result = calc_price(
            Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens or None,
                cache_write_tokens=cache_write_tokens or None,
            ),
            model_ref=model,
            provider_id=provider,
        )
    except Exception:  # noqa: BLE001 - unknown model/provider: cost unknown, not an error
        return None
    return float(result.total_price)


class EvalRecord(BaseModel):
    """One eval-suite run: the quality signal over time (SB-1488)."""

    eval_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    suite: str
    model: str
    judge_model: str
    cases: int
    passed: int
    pass_rate: float
    scores: dict[str, float] = Field(default_factory=dict)  # evaluator -> fraction passed
    agent_cost_usd: float | None = None
    judge_cost_usd: float | None = None
    duration_s: float = 0.0

    @property
    def cost_usd(self) -> float | None:
        known = [c for c in (self.agent_cost_usd, self.judge_cost_usd) if c is not None]
        return sum(known) if known else None


def emit_eval(record: EvalRecord) -> None:
    """Log the eval as one JSON line (`event=eval_run`) and set the eval gauges."""
    fields = record.model_dump(exclude={"scores"})
    fields.update({f"score_{name}": value for name, value in record.scores.items()})
    fields["cost_usd"] = record.cost_usd
    log.info("eval run", extra={"event": "eval_run", **fields})

    meter = metrics.get_meter("sb_obs")
    attrs = {"suite": record.suite, "model": record.model}
    meter.create_gauge("sb_eval_pass_rate", description="Eval cases passed, 0-1").set(
        record.pass_rate, attrs
    )
    score = meter.create_gauge("sb_eval_score", description="Evaluator pass fraction, 0-1")
    for name, value in record.scores.items():
        score.set(value, {**attrs, "evaluator": name})
    if record.cost_usd is not None:
        meter.create_counter("sb_eval_cost_usd", description="Eval spend in USD").add(
            record.cost_usd, attrs
        )
