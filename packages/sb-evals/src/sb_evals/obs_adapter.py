"""The seam to this repo's telemetry, and the only file that imports it.

To re-implement elsewhere, replace this file with a `Sink` for your own backend.
"""

from __future__ import annotations

from sb_evals.records import SuiteRecord
from sb_obs import EvalRecord, emit_eval


def emit_to_sb_obs(record: SuiteRecord) -> None:
    """A `Sink`: log the suite run as `event=eval_run` and set the eval gauges."""
    emit_eval(EvalRecord(**record.model_dump(exclude={"execution_ids"})))
