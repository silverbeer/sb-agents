"""JSON log lines in Google Cloud Logging's structured format.

Work ships stdout/stderr JSON to GCP over its own data bus, and Cloud Logging
lifts a few special keys into the LogEntry (severity, trace, span, source
location). Writing that shape here means the same agent code logs correctly at
work with no change: https://cloud.google.com/logging/docs/structured-logging
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace

TRACE_KEY = "logging.googleapis.com/trace"
SPAN_KEY = "logging.googleapis.com/spanId"
SAMPLED_KEY = "logging.googleapis.com/trace_sampled"
SOURCE_KEY = "logging.googleapis.com/sourceLocation"

# Attributes every LogRecord has; anything else came from `extra=` and is kept.
# color_message: uvicorn's ANSI-colored duplicate of the message.
_STANDARD = frozenset(vars(logging.makeLogRecord({}))) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}


def trace_fields(project: str | None = None) -> dict[str, Any]:
    """Cloud Logging trace keys for the current span, or {} outside a span."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return {}
    trace_id = format(ctx.trace_id, "032x")
    return {
        TRACE_KEY: f"projects/{project}/traces/{trace_id}" if project else trace_id,
        SPAN_KEY: format(ctx.span_id, "016x"),
        SAMPLED_KEY: ctx.trace_flags.sampled,
    }


class GcpJsonFormatter(logging.Formatter):
    """One JSON object per line, Cloud Logging field names, trace-correlated.

    `project` (or $SB_OBS_GCP_PROJECT) turns the bare trace id into the
    `projects/<p>/traces/<id>` form Cloud Logging links to Cloud Trace.
    """

    def __init__(self, service: str, project: str | None = None) -> None:
        super().__init__()
        self.service = service
        self.project = project or os.environ.get("SB_OBS_GCP_PROJECT") or None

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            # Cloud Error Reporting reads the stack trace from the message itself.
            message = f"{message}\n{self.formatException(record.exc_info)}"
        entry: dict[str, Any] = {
            "severity": record.levelname,
            "message": message,
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "logger": record.name,
            "service": self.service,
            SOURCE_KEY: {
                "file": record.pathname,
                "line": str(record.lineno),
                "function": record.funcName,
            },
            **trace_fields(self.project),
        }
        entry.update({k: v for k, v in vars(record).items() if k not in _STANDARD})
        return json.dumps(entry, default=str)
