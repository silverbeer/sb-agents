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

## Run records and cost (SB-1484)

Every agent run emits one `RunRecord` (`sb_obs.track_run`): a JSON log line with
`event=agent_run`, the metrics `sb_agent_runs` / `sb_agent_cost_usd` /
`sb_agent_run_duration`, and `sb.run.*` attributes on an `agent_run <agent>` span
wrapping the framework spans. Fields: run_id, trace_id, agent, caller, model,
outcome (ok / error / limit), requests, tool_calls, input/output/cache tokens,
cost_usd, latency_ms, confidence, error. This is the row work would land in BigQuery.

- **Cost source: `genai-prices`, not a hand-kept table.** PydanticAI fills
  `usage.cost` from it; ADK runs are priced with `sb_obs.price()`. It handles
  Haiku 5.5's tiered pricing (prompts over 100K cost 5x) per request.
  `tests/test_runs.py` pins the expected prices, so a price change fails CI.
  `cost_usd` is `None` (not 0) when a model isn't priced, e.g. local Ollama.
- **Caps:** docs-agent `UsageLimits(request_limit=8, tool_calls_limit=12,
  total_tokens_limit=60_000, cost_limit=$0.02)`; sb-support
  `(request_limit=3, total_tokens_limit=20_000, cost_limit=$0.01)`; ADK router
  `RunConfig(max_llm_calls=4)`. A hit limit is outcome `limit`, and sb-support
  replies "needed more work than I'm allowed" instead of the generic error.
- **Measured:** one docs question = 4 requests, 4 tool calls, 15.4k in / 0.7k out,
  **$0.0019** on Haiku, ~4.6 s.

### Prompt caching: tried, left off

A/B on one question with `anthropic_cache=True` (automatic, moving breakpoint):

| | requests | input tokens | cache write | cache read | cost |
|---|---|---|---|---|---|
| off | 4 | 15,344 | 0 | 0 | $0.00186 |
| on | 6 | 42,757 | 10,640 | 32,103 | $0.00218 |

Per request, caching was ~21% cheaper ($0.00036 vs $0.00046), but the run took
two extra requests (model nondeterminism) and cache writes cost 1.25x, so the run
cost more. At ~4 short requests per question the write premium roughly cancels
the read discount. Revisit when Slack threads become multi-turn or prompts grow.

## One question, one trace, both hops (SB-1485)

Both A2A clients now produce a single trace with docs-agent:

| Hop | Client | How the trace crosses | Verified |
|---|---|---|---|
| ADK router → docs-agent | a2a-sdk via `RemoteA2aAgent` | httpx instrumentation injects `traceparent` | live (Tempo) |
| sb-support → docs-agent | fasta2a `A2AClient` (`sb_support.docs_client`) | `sb_obs.traced_http_client` injects in a request hook; httpx instrumentation also does on real transports | automated (`tests/test_trace_hop.py`) and live |

On the server, `instrument_asgi` (ASGI middleware) continues the incoming trace.

- **Root span:** sb-support opens `slack app_mention` (SERVER, with `slack.channel`,
  `slack.ts`, `slack.thread_ts`). Slack Bolt uses aiohttp, which isn't instrumented,
  so without this span every Slack question would start at the agent run.
- **Run records join on trace_id.** A live run: sb-support (2 requests, $0.00025) and
  docs-agent (3 requests, $0.0013, caller `a2a`) records carry the same trace_id
  from different processes. "Cost of one Slack question" = sum of records per trace_id,
  even where traces are sampled away.
- **Why inject explicitly when instrumentation already does:** the instrumentation
  only patches httpx's network transports. A hook makes propagation part of the
  client's contract, so it holds over `ASGITransport` in tests and doesn't depend
  on someone remembering to instrument at work.
- **fasta2a's server has no blocking `message/send`** (it returns `submitted`), so
  the client uses `message/stream` and waits for the final artifact, like ADK does.
- Not automated: the ADK hop over a real socket. It was verified live in SB-1478;
  an in-process test would test our hook, not ADK's client.

The `ask_docs` tool is the minimal version of SB-1476's routing: one specialist,
no thread history, no escalation.

## Dashboards (SB-1486)

`observability/grafana/build_dashboards.py` generates `dashboards/agent-overview.json`,
provisioned into otel-lgtm by `make obs-up` (folder *sb-agents*). A test fails if the
committed JSON is stale.

**Where each panel's data comes from, and why:**

- **Runs, spend, $/run, error rate, tokens, run p95: run records in Loki** (`sum_over_time
  (... | unwrap cost_usd [...])`). Prometheus counters from short-lived CLI processes
  show up once, already at their final value, so `rate()`/`increase()` never count them.
  Each record is its own row, so these totals are exact. At work this is the BigQuery query.
- **LLM call latency: Tempo span metrics** (`traces_spanmetrics_latency`, by `span_name`
  `chat <model>` / `generate_content <model>`). ADK emits
  `gen_ai.client.operation.duration`; PydanticAI 2.64 only emits token usage. Span
  metrics give one latency source for both frameworks.
- **A2A edges: Tempo service graph** (`traces_service_graph_request_total{client,server}`),
  built from client/server span pairs. `adk-docs-client -> docs-agent` and
  `sb-support -> docs-agent` showed up on their own once propagation worked.
- **Log → trace:** otel-lgtm's Loki datasource already links the `trace_id` field to Tempo.

Gotcha: an OTel counter with `unit="USD"` became `sb_agent_cost_usd_USD_total`; the
Prometheus translation appends the unit. The counter now has no unit.

Not built: the optional SQL panel (run records in SQLite/Postgres). Loki's `unwrap`
covered the same "aggregate the rows" practice without another store.

## Alerts to #sb-alerts (SB-1487)

`observability/grafana/provisioning/sb-agents-alerting.yaml`: one Slack contact point
(`#sb-alerts`, the existing `sb-agent` bot token), one policy, four Grafana-managed rules
(folder *sb-agents*, evaluated every 1m):

| Rule | Query | Default |
|---|---|---|
| Agent runs failing | Loki: run records with `outcome != "ok"`, last 15m | > 1 |
| Agent run latency p95 high | Loki: `quantile_over_time(0.95, ... unwrap latency_ms [15m])`, max by agent | > 20000 ms |
| Agent spend over daily budget | Loki: `sum_over_time(... unwrap cost_usd [24h])` | > $1 |
| docs-agent down | Prometheus: `absent_over_time(target_info{service_name="docs-agent"}[3m])`, for 1m | any |

**Fired on purpose, all four, delivered to Slack with no error** (2026-11-01): low
thresholds via `make obs-up ALERT_DAILY_USD=0.0001 ALERT_P95_MS=1000 ALERT_FAILED_RUNS=0`,
one real question, one run against a model that doesn't exist (error record), and
docs-agent stopped. Failing, latency and spend fired within ~45s; docs-agent down
fired 4.5 min after the stop (3m absence + 1m pending).

- **Liveness without scraping:** with OTLP push there is no Prometheus `up` metric. A
  running server re-exports `target_info` every 60s, so its absence means the process
  is gone. A short-lived CLI run also exports it, which briefly hides a down server;
  fine for now, but a real deployment wants a health check (k3s, SB-1481).
- **Grafana parses YAML before expanding `${VARS}`.** A placeholder inside a flow list
  (`params: [${X}]`) stops Grafana starting, and otel-lgtm only says "Grafana exited
  before becoming ready" until you set `ENABLE_LOGS_GRAFANA=true`. `make obs-up`
  substitutes the thresholds itself; only `${SLACK_BOT_TOKEN}` is left for Grafana.
  `tests/test_alerting.py` checks the file parses.
- **The old receiver-test endpoint is gone in Grafana 13:** use
  `POST /apis/notifications.alerting.grafana.app/v1beta1/namespaces/default/receivers/<uid>/test`
  with the integration's settings in the body.
- These alerts are sb-triage's input (SB-1475): the agents' own failures are the
  first thing the triage agent gets to triage.

## Eval scores as a metric (SB-1488)

`make eval` runs the 13-case docs-agent suite against the real model, then emits one
`EvalRecord`: a log line (`event=eval_run`, `pass_rate`, `score_<evaluator>`,
`agent_cost_usd`, `judge_cost_usd`, `cost_usd`, `duration_s`), the gauges
`sb_eval_pass_rate` / `sb_eval_score{evaluator}`, and an `sb_eval_cost_usd` counter.
The dashboard plots pass rate (80% threshold line) and eval cost; the alert
*docs-agent eval below threshold* fires when the latest run in 7 days is under 0.8.

**Measured, 2026-11-01:** 13/13 passed (Calibrated, CitedExpectedSource and LLMJudge
all 100%). **$0.069 per run: agent $0.023, Sonnet judge $0.046.** The judge is two
thirds of the cost, so a cheaper judge is the first lever if evals run often; the judge
model must stay fixed across runs or scores stop being comparable (SB-1490).

- pydantic-evals reports no cost, so the judge is wrapped in `PricedModel` (a PydanticAI
  `WrapperModel` that prices each request with genai-prices), and the agent's cost
  comes from each run's `usage.cost`.
- Alert fired on purpose with a synthetic record (`suite=alert-test`, pass_rate 0.5,
  no LLM calls) and delivered to `#sb-alerts`.
- On demand only. A weekly schedule needs somewhere that is always on (open question).
