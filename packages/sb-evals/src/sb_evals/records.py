"""The two records that cross the package's boundary.

    ExecutionRecord  in:  what one agent run produced
    SuiteRecord      out: how one suite run scored

Neither mentions an agent framework, so any agent can fill in the first and any backend
can store the second.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


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
    execution_ids: list[str] = Field(default_factory=list)  # empty for a fixture suite

    @property
    def cost_usd(self) -> float | None:
        known = [c for c in (self.agent_cost_usd, self.judge_cost_usd) if c is not None]
        return sum(known) if known else None
