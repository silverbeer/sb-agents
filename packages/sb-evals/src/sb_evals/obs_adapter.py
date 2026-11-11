"""The seam to this repo's telemetry, and the only file that imports it.

To re-implement elsewhere, replace this file with a `Sink` for your own backend.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sb_evals.records import CaseResult, SuiteRecord
from sb_obs import EvalRecord, emit_eval
from sb_obs.setup import env_flag

log = logging.getLogger("sb_evals")


def _scalar(value: Any) -> Any:
    return value if isinstance(value, str | int | float | bool) else json.dumps(value, default=str)


def _case_fields(case: CaseResult, capture_content: bool) -> dict[str, Any]:
    """Flat fields, so each is a queryable label-free field in Loki."""
    fields: dict[str, Any] = {
        "event": "eval_case",
        "case": case.name,
        "execution_id": case.execution_id,
        "passed": case.passed,
        "crashed": case.crashed,
        "unchecked": ",".join(case.unchecked),
        "failed_checks": ",".join(case.failed),
    }
    fields.update({f"check_{name}": ok for name, ok in case.checks.items()})
    if capture_content:
        fields.update({f"reason_{name}": why for name, why in case.reasons.items()})
        fields.update({f"detail_{name}": _scalar(v) for name, v in case.detail.items()})
    return fields


def emit_to_sb_obs(record: SuiteRecord) -> None:
    """A `Sink`: one `event=eval_run` for the suite, then one `event=eval_case` per case.

    Judge reasons quote the output, so they and the case detail follow the same switch
    as prompts in traces: `SB_OBS_CAPTURE_CONTENT`.
    """
    emit_eval(EvalRecord(**record.model_dump(exclude={"case_results"})))
    capture_content = env_flag("SB_OBS_CAPTURE_CONTENT")
    for case in record.case_results:
        fields = _case_fields(case, capture_content)
        log.info("eval case", extra={"suite": record.suite, "model": record.model, **fields})
