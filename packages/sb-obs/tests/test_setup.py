"""setup_telemetry sets process-global state, so each case runs in a subprocess."""

import json
import subprocess
import sys
import textwrap

SCRIPT = textwrap.dedent(
    """
    import json, logging, os, sys
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel
    from sb_obs import setup_telemetry

    spans, reader = InMemorySpanExporter(), InMemoryMetricReader()
    t = setup_telemetry("svc-test", span_processors=[SimpleSpanProcessor(spans)],
                        metric_readers=[reader])
    assert setup_telemetry("svc-test") is t  # idempotent

    agent = Agent(TestModel(), name="probe")

    @agent.tool_plain
    def lookup(q: str) -> str:
        logging.getLogger("probe").info("in tool")
        return "SECRET-ANSWER"

    agent.run_sync("SECRET-QUESTION")
    from sb_obs import track_run
    with track_run("probe", caller="test", model="test") as run:
        run.add_usage(requests=1, cost_usd=0.001)
    t.shutdown()
    done = spans.get_finished_spans()
    names = [s.name for s in done]
    attrs = json.dumps([dict(s.attributes or {}) for s in spans.get_finished_spans()], default=str)
    metric_names = [m.name for rm in reader.get_metrics_data().resource_metrics
                    for sm in rm.scope_metrics for m in sm.metrics]
    print(json.dumps({
        "names": names,
        "content": "SECRET-QUESTION" in attrs,
        "service": spans.get_finished_spans()[0].resource.attributes["service.name"],
        "metrics": metric_names,
        "adk": os.environ["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"],
        "trace_ids": sorted({format(s.context.trace_id, "032x") for s in done}),
        "exporting": t.exporting,
    }))
    """
)


def run(**env: str) -> tuple[dict, list[dict]]:
    base = {"PATH": "/usr/bin:/bin", "PYDANTIC_AI_NO_BANNER": "1", **env}
    proc = subprocess.run(
        [sys.executable, "-c", SCRIPT], capture_output=True, text=True, env=base, check=False
    )
    assert proc.returncode == 0, proc.stderr
    logs = [json.loads(line) for line in proc.stderr.splitlines() if line.startswith("{")]
    return json.loads(proc.stdout), logs


def test_agent_spans_metrics_and_correlated_logs_without_content() -> None:
    out, logs = run()
    assert out["service"] == "svc-test"
    # OTel gen_ai semantic-convention span names
    assert {"invoke_agent probe", "execute_tool lookup", "chat test"} <= set(out["names"])
    assert "gen_ai.client.token.usage" in out["metrics"]
    assert {"sb_agent_runs", "sb_agent_cost_usd", "sb_agent_run_duration"} <= set(out["metrics"])
    record = next(entry for entry in logs if entry.get("event") == "agent_run")
    assert (record["agent"], record["outcome"], record["cost_usd"]) == ("probe", "ok", 0.001)
    assert record["trace_id"] == record["logging.googleapis.com/trace"]
    assert out["content"] is False
    assert out["adk"] == "false"
    assert out["exporting"] is False

    tool_log = next(entry for entry in logs if entry["message"] == "in tool")
    assert tool_log["logging.googleapis.com/trace"] in out["trace_ids"]


def test_capture_content_flag_turns_content_on() -> None:
    out, _ = run(SB_OBS_CAPTURE_CONTENT="1")
    assert out["content"] is True
    assert out["adk"] == "true"


def test_text_log_format() -> None:
    _, logs = run(SB_OBS_LOG_FORMAT="text")
    assert logs == []


ASGI_SCRIPT = textwrap.dedent(
    """
    import asyncio, json
    import httpx
    from opentelemetry import propagate, trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from sb_obs import instrument_asgi, setup_telemetry

    spans = InMemorySpanExporter()
    setup_telemetry("svc-asgi", span_processors=[SimpleSpanProcessor(spans)])
    app = instrument_asgi(Starlette(routes=[Route("/", lambda r: PlainTextResponse("ok"))]))

    async def main():
        with trace.get_tracer("client").start_as_current_span("caller") as caller:
            headers = {}
            propagate.inject(headers)  # what the httpx instrumentation does on real transports
        # Request sent outside the caller's span: only the header can carry the trace,
        # as across a real process boundary.
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            await c.get("/", headers=headers)
        return format(caller.get_span_context().trace_id, "032x")

    caller_trace = asyncio.run(main())
    server = [s for s in spans.get_finished_spans() if s.kind == trace.SpanKind.SERVER]
    print(json.dumps({
        "caller_trace": caller_trace,
        "server_traces": [format(s.context.trace_id, "032x") for s in server],
        "names": [s.name for s in spans.get_finished_spans()],
    }))
    """
)


def test_asgi_server_continues_incoming_trace_without_chunk_spans() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", ASGI_SCRIPT],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["server_traces"] == [out["caller_trace"]]
    assert not [n for n in out["names"] if n.endswith((" send", " receive"))], out["names"]
