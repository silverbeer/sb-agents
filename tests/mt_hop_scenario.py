"""Slack mention → sb-support → A2A → mt-agent (ADK) → MCP → stub mt-mcp, one process.

Run by test_trace_hop.py in a subprocess (OTel global providers are set-once).
Each server runs in an empty OTel context, as it would in its own process, so the
trace can only cross a hop through what goes on the wire: `traceparent` on the A2A
request, `_meta.traceparent` on the MCP call. Prints JSON like hop_scenario.py.
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
from opentelemetry import context as otel_context
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai.models.test import TestModel

from sb_obs import instrument_asgi, setup_telemetry, traced_http_client

spans = InMemorySpanExporter()
setup_telemetry("hop-test", span_processors=[SimpleSpanProcessor(spans)])

from mt_agent.a2a_server import build_app  # noqa: E402
from mt_agent.agent import build_agent, build_toolset  # noqa: E402
from sb_support.agent import build_agent as build_support  # noqa: E402
from sb_support.core import SupportCore  # noqa: E402
from sb_support.slack import handle_event  # noqa: E402
from sb_support.specialists import discover  # noqa: E402

FAKES = Path(__file__).parents[1] / "packages" / "mt-agent" / "tests" / "mt_fakes.py"
_spec = importlib.util.spec_from_file_location("mt_fakes", FAKES)
fakes = sys.modules["mt_fakes"] = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fakes)

BASE = "http://127.0.0.1:8102"


def own_process(app):
    """Run an ASGI app with no inherited OTel context, like a separate server."""

    async def isolated(scope, receive, send):
        token = otel_context.attach(otel_context.Context())
        try:
            await app(scope, receive, send)
        finally:
            otel_context.detach(token)

    return isolated


async def main() -> str:
    stub = fakes.StubMcp()
    stub.app = own_process(stub.app)
    model = fakes.scripted(
        fakes.calls_tool("search_teams", query="IFA", age_group="U14"),
        fakes.says("IFA U14 found."),
    )
    toolset = build_toolset(fakes.MCP_URL, "t", httpx_client_factory=stub.client_factory())
    app = instrument_asgi(build_app(build_agent(model, toolset), url=BASE))
    slack = MagicMock(
        chat_postMessage=AsyncMock(), reactions_add=AsyncMock(), reactions_remove=AsyncMock()
    )
    async with stub.running(), app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=own_process(app))
        async with traced_http_client(transport=transport, base_url=BASE) as http:
            specialists = await discover([BASE], http)  # from mt-agent's agent card
            support = build_support(TestModel(call_tools=["ask_mt_agent"]), specialists)
            event = {"channel": "C1", "ts": "1.1", "text": "<@U0BOT> IFA U14?"}
            await handle_event(event, slack, SupportCore(support))
    return slack.chat_postMessage.await_args.kwargs["text"]


reply = asyncio.run(main())
print(
    json.dumps(
        {
            "reply": reply,
            "spans": [
                {
                    "name": s.name,
                    "trace": format(s.context.trace_id, "032x"),
                    "scope": s.instrumentation_scope.name if s.instrumentation_scope else None,
                }
                for s in spans.get_finished_spans()
            ],
        }
    )
)
