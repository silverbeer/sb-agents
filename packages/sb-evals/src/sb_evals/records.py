"""The records that cross the package's boundary.

    ExecutionRecord  in:  what one agent run produced
    SuiteRecord      out: how one suite run scored, with a CaseResult per case

None mentions an agent framework, so any agent can fill in the first and any backend
can store the second.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field

REASON_CHARS = 200


def _clip(text: str) -> str:
    text = " ".join(text.split()).rstrip(".")
    return text if len(text) <= REASON_CHARS else text[: REASON_CHARS - 1].rstrip() + "…"


class ExecutionRecord(BaseModel):
    """One agent run, recorded by the agent's own code at the end of that run."""

    execution_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    name: str  # the case label in results and chat, e.g. "#2"
    agent: str
    agent_version: str | None = None
    prompt_version: str | None = None
    model: str
    inputs: Any
    output: Any
    expected_output: Any = None  # usually absent: a real run has no ground truth
    metadata: Any = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class CaseResult(BaseModel):
    """How one case scored: enough to ask whether the verdict itself was right."""

    name: str
    execution_id: str | None = None  # None for a fixture case
    checks: dict[str, bool] = Field(default_factory=dict)  # check -> passed
    reasons: dict[str, str] = Field(default_factory=dict)  # check -> why; can quote the output
    unchecked: list[str] = Field(default_factory=list)  # checks that raised
    crashed: bool = False  # the task itself raised, so no check ran
    detail: dict[str, Any] = Field(default_factory=dict)  # from `describe_case`; can hold content

    @property
    def failed(self) -> list[str]:
        return [check for check, ok in self.checks.items() if not ok]

    @property
    def passed(self) -> bool:
        return not (self.failed or self.unchecked or self.crashed)

    def describe(self) -> str:
        """What went wrong, without exception text: this can reach a chat channel."""
        if self.crashed:
            return f"{self.name} did not run"
        parts = [
            f"failed {check}: {_clip(self.reasons[check])}"
            if self.reasons.get(check)
            else f"failed {check}"
            for check in self.failed
        ]
        parts += [f"could not run {check}" for check in self.unchecked]
        return f"{self.name} " + ", ".join(parts)


class SuiteRecord(BaseModel):
    """One suite run: the quality signal to track over time."""

    suite: str
    model: str
    judge_model: str
    cases: int
    passed: int
    pass_rate: float
    scores: dict[str, float] = Field(default_factory=dict)  # check -> fraction passed
    agent_cost_usd: float | None = None
    judge_cost_usd: float | None = None
    duration_s: float = 0.0
    case_results: list[CaseResult] = Field(default_factory=list)

    @property
    def cost_usd(self) -> float | None:
        known = [c for c in (self.agent_cost_usd, self.judge_cost_usd) if c is not None]
        return sum(known) if known else None
