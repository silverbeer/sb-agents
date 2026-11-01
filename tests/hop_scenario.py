"""Slack mention → sb-support → A2A → docs-agent, in one process, real telemetry setup.

Run by test_trace_hop.py in a subprocess (OTel global providers are set-once).
Prints JSON: spans (service-less, so tagged by scope) and the run records logged.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai.models.test import TestModel

from sb_obs import instrument_asgi, setup_telemetry, traced_http_client

spans = InMemorySpanExporter()
setup_telemetry("hop-test", span_processors=[SimpleSpanProcessor(spans)])

from docs_agent.a2a_server import build_app  # noqa: E402
from docs_agent.agent import Deps, build_agent  # noqa: E402
from docs_agent.corpus import Corpus, KeywordRetriever, split_sections  # noqa: E402
from sb_support.agent import build_agent as build_support  # noqa: E402
from sb_support.app import handle_mention  # noqa: E402
from sb_support.docs_client import DocsClient  # noqa: E402

BASE = "http://127.0.0.1:8101"
MD = "# Install\n\nRun `uv tool install janitor-cli`."
ANSWER = {
    "answer": "Run `uv tool install janitor-cli`.",
    "sources": [{"ref": "janitor/README.md", "heading": "Install"}],
    "confidence": "high",
}


async def main() -> str:
    corpus = Corpus(split_sections("janitor", "README.md", MD), {"janitor/README.md": MD})
    docs_model = TestModel(call_tools=["search_docs"], custom_output_args=ANSWER)
    app = instrument_asgi(
        build_app(Deps(corpus, KeywordRetriever(corpus)), build_agent(docs_model), url=BASE)
    )
    slack = MagicMock(
        chat_postMessage=AsyncMock(), reactions_add=AsyncMock(), reactions_remove=AsyncMock()
    )
    async with app.router.lifespan_context(app):
        http = traced_http_client(transport=httpx.ASGITransport(app=app), base_url=BASE)
        async with http:
            support = build_support(TestModel(call_tools=["ask_docs"]), docs=DocsClient(BASE, http))
            event = {"channel": "C1", "ts": "1.1", "text": "<@U0BOT> how do I install janitor?"}
            await handle_mention(event, slack, support)
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
