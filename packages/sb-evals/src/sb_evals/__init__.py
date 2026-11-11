"""Eval runner for agents, written to be read and re-implemented. Start with README.md.

`sb_evals.obs_adapter` is left out of this namespace on purpose: it is the only module
that knows about this repo's telemetry.
"""

from sb_evals.guard import guarded
from sb_evals.records import CaseResult, ExecutionRecord, SuiteRecord
from sb_evals.suite import (
    CostedJudge,
    DescribeCase,
    Sink,
    SuiteResult,
    run_suite,
    score_executions,
)

__all__ = [
    "CaseResult",
    "CostedJudge",
    "DescribeCase",
    "ExecutionRecord",
    "Sink",
    "SuiteRecord",
    "SuiteResult",
    "guarded",
    "run_suite",
    "score_executions",
]
