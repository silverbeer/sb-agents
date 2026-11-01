# Telemetry setup: what the frameworks give for free (SB-1478)

`sb-obs.setup_telemetry(service)` + `grafana/otel-lgtm:0.37.0` locally (`make obs-up`).
Versions: opentelemetry-sdk 1.44, pydantic-ai 2.64, google-adk 2.13, fasta2a 2.1.1,
a2a-sdk 1.x, anthropic SDK (ships `httpx2`). Date: 2026-11-01.

## Headline

**One question already produces one trace across both agents.** ADK router →
A2A → docs-agent → tools and LLM calls, joined in Tempo with no propagation code.
The httpx instrumentation injects `traceparent` on the A2A client call, and the
ASGI middleware on the fasta2a server continues it. SB-1485 still needs to cover
Slack → sb-support and a PydanticAI client, but the ADK → fasta2a hop works.

## Who emits what (instrumentation scope → spans)

| Scope | Spans | Comes from |
|---|---|---|
| `pydantic-ai` | `invoke_agent docs-agent`, `chat claude-haiku-5-5`, `execute_tool search_docs` | `Agent.instrument_all(...)` |
| `gcp.vertex.agent` | `invoke_workflow sb_router`, `invoke_agent`, `generate_content`, `execute_tool transfer_to_agent` | ADK built in: uses the global tracer provider, nothing to call |
| `com.anthropic.sdk.python` | `anthropic.messages.create` | The anthropic SDK itself, under both frameworks' LLM spans |
| `a2a-python-sdk` | `JsonRpcTransport.send_message_streaming`, ... | a2a-sdk (ADK's A2A client) built in |
| `fasta2a.worker` | `run task` | fasta2a built in |
| `opentelemetry.instrumentation.httpx` | `GET`/`POST` client spans | sb-obs (also what propagates context) |
| `opentelemetry.instrumentation.asgi` | `POST /` server span | sb-obs `instrument_asgi(app)` |

Both frameworks use the OTel GenAI semantic conventions: span names `chat <model>`,
`execute_tool <name>`, `invoke_agent <name>` and `gen_ai.usage.input_tokens` /
`output_tokens` attributes. The same queries work for ADK and PydanticAI agents.

Metrics for free (Prometheus names): `gen_ai_client_token_usage`,
`gen_ai_client_operation_duration_seconds`, `gen_ai_invoke_agent_duration_seconds`,
`gen_ai_invoke_agent_tool_calls`, `gen_ai_invoke_agent_inference_calls`,
`gen_ai_execute_tool_duration_seconds`, plus HTTP client/server histograms.

## Gotchas

- **ADK captures prompt/response content in spans by default**
  (`ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS` defaults to `true`). PydanticAI's
  `include_content` also defaults to `True`. sb-obs makes one flag
  (`SB_OBS_CAPTURE_CONTENT`, default off) drive both, plus
  `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`. **For work: set this
  explicitly. The framework defaults leak Slack message content into traces.**
- **The anthropic SDK ships its own HTTP client, `httpx2`.** The httpx
  instrumentation doesn't see Anthropic API calls (no HTTP span, no traceparent
  sent to Anthropic), and its INFO request logs come from logger `httpx2`, not `httpx`.
- **The StarletteInstrumentor (0.65b0) can't drop the per-chunk
  `http send`/`receive` spans**, about 6 noise spans per A2A call. The raw ASGI
  middleware can (`exclude_spans`), so `instrument_asgi` uses that.
- An ASGI server span is `INTERNAL`, not `SERVER`, when a span is already active
  in-process. That only matters in tests; send the request outside the caller's span.
- uvicorn configures its own log handlers. Pass `log_config=None` to get JSON.
- ADK will set up global providers from `OTEL_EXPORTER_OTLP_*` env vars itself
  (`maybe_set_otel_providers`), but only via its own entry points (`adk web`/`api_server`).
  Our CLIs call `setup_telemetry` first, and global providers are set-once.

## One docs question, measured from the trace

Router: 1 Haiku call (769 in / 59 out). docs-agent: 3–4 Haiku calls, input
growing 1.9k → 4.5k → 8.7k tokens as tool results pile into context. Prompt
caching (SB-1484) targets that growth.

## For the work build

1. Logs: Cloud Logging JSON (`severity`, `message`, `logging.googleapis.com/trace`,
   `spanId`, `trace_sampled`, `sourceLocation`) on stderr is what the data bus ships.
   Set `SB_OBS_GCP_PROJECT` for the `projects/<p>/traces/<id>` form.
2. Traces: OTLP to the new Grafana OTel pipeline. No code change, just the endpoint env var.
3. A custom sink is an OTel `SpanProcessor` / `MetricReader` / `logging.Handler`
   passed to `setup_telemetry`. Agent code stays untouched.
4. Decide content capture deliberately; never inherit the framework defaults.
