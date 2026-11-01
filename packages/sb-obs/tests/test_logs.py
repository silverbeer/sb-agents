import json
import logging

from opentelemetry.sdk.trace import TracerProvider

from sb_obs.logs import SAMPLED_KEY, SOURCE_KEY, SPAN_KEY, TRACE_KEY, GcpJsonFormatter

tracer = TracerProvider().get_tracer("test")


def record(msg: str = "hello", level: int = logging.INFO, **extra) -> logging.LogRecord:
    rec = logging.getLogger("t").makeRecord("t", level, "f.py", 7, msg, (), None, "fn")
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_cloud_logging_shape_outside_span() -> None:
    entry = json.loads(GcpJsonFormatter("svc").format(record(level=logging.WARNING)))
    assert entry["severity"] == "WARNING"
    assert entry["message"] == "hello"
    assert entry["service"] == "svc"
    assert entry[SOURCE_KEY] == {"file": "f.py", "line": "7", "function": "fn"}
    assert TRACE_KEY not in entry


def test_trace_ids_inside_span() -> None:
    with tracer.start_as_current_span("s") as span:
        entry = json.loads(GcpJsonFormatter("svc").format(record()))
    ctx = span.get_span_context()
    assert entry[TRACE_KEY] == format(ctx.trace_id, "032x")
    assert entry[SPAN_KEY] == format(ctx.span_id, "016x")
    assert entry[SAMPLED_KEY] is True


def test_project_gives_full_trace_resource_name() -> None:
    with tracer.start_as_current_span("s") as span:
        entry = json.loads(GcpJsonFormatter("svc", project="p1").format(record()))
    assert entry[TRACE_KEY] == f"projects/p1/traces/{span.get_span_context().trace_id:032x}"


def test_extra_fields_kept_and_exception_in_message() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = record(task_id="t1")
        rec.exc_info = sys.exc_info()
    entry = json.loads(GcpJsonFormatter("svc").format(rec))
    assert entry["task_id"] == "t1"
    assert "ValueError: boom" in entry["message"]
