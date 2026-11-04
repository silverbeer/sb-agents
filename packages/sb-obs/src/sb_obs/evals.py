"""Shared eval helpers for pydantic-evals suites (docs-agent, mt-agent).

Needs the `evals` extra (`sb-obs[evals]`): pydantic-ai and pydantic-evals. Kept out of
`sb_obs/__init__` so the base package stays framework-free.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai.models import Model, infer_model
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_evals.reporting import EvaluationReport

from sb_obs.runs import price

__all__ = ["PricedModel", "percentile", "summarize"]


class PricedModel(WrapperModel):
    """Wraps the judge model and prices each request (pydantic-evals reports no cost)."""

    def __init__(self, wrapped: Model | str) -> None:
        super().__init__(infer_model(wrapped) if isinstance(wrapped, str) else wrapped)
        self.cost_usd: float | None = None
        self.requests = 0

    async def request(self, messages: Any, model_settings: Any, params: Any) -> Any:
        response = await super().request(messages, model_settings, params)
        u = response.usage
        cost = price(
            self.model_name,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_tokens,
            cache_write_tokens=u.cache_write_tokens,
        )
        self.requests += 1
        if cost is not None:
            self.cost_usd = (self.cost_usd or 0.0) + cost
        return response


def summarize(report: EvaluationReport) -> tuple[int, int, dict[str, float]]:
    """(cases passed, total cases, fraction passed per assertion name)."""
    passed = sum(1 for c in report.cases if all(a.value for a in c.assertions.values()))
    total = len(report.cases) + len(report.failures)
    results: dict[str, list[bool]] = {}
    for case in report.cases:
        for name, assertion in case.assertions.items():
            results.setdefault(name, []).append(bool(assertion.value))
    scores = {name: sum(vals) / len(vals) for name, vals in sorted(results.items())}
    return passed, total, scores


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100); None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * q // 100))  # ceil(n * q / 100), at least 1
    return float(ordered[int(rank) - 1])
