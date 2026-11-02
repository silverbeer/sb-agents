# mt-agent: chat with MissingTable data over A2A (SB-1493)

The first ADK specialist and the first **PydanticAI → ADK** hop: sb-support (PydanticAI)
routes MT data questions to mt-agent (ADK) over A2A, and mt-agent reads MT through
mt-mcp's tools. Versions: google-adk 2.13.0, a2a-sdk 1.3.0, fasta2a 2.1.1, mcp 2.3.0.

```
Slack ─► sb-support core ── ask_mt_agent ──A2A 1.0──► mt-agent (ADK LlmAgent, Haiku)
                                                        └─ McpToolset ──MCP──► mt-mcp
                                                             search_teams, get_upcoming_matches,
                                                             get_standings
```

## Layout

| Module | What |
|---|---|
| `agent.py` | `LlmAgent` (Claude via `AnthropicLlm`), instructions, `McpToolset` with `tool_filter`, MCP result unwrapping |
| `runner.py` | `TrackedRunner`: caps, the RunRecord, and the reply's data part |
| `a2a_server.py` | agent card, `to_a2a(...)`, `main()` (uvicorn, `instrument_asgi`, `setup_telemetry`) |

`to_a2a` attaches its routes in the app's **lifespan**, so a client in the same process
(tests) must enter `app.router.lifespan_context(app)` first, or every path is a 404.

## The specialist contract (written down here; docs-agent already met it)

A specialist is reachable by A2A 1.0 JSON-RPC and publishes:

1. **An agent card** at `/.well-known/agent-card.json`: `name`, `description`, and
   `skills[].examples`. sb-support turns name into the tool `ask_<name>` and description
   + the first three examples into its description. Write examples as the questions you
   want routed.
2. **A result artifact** with, in order, a **text part** (the answer, Slack-friendly) and a
   **data part** `{"confidence": "high"|"low", "sources": [...], ...}`. sb-support
   escalates to a human on `low` or on a failed task.

What `sources` holds is the specialist's own: docs-agent cites files, mt-agent names the
tools whose data it used. mt-agent's data part:

```json
{"confidence": "high", "sources": ["search_teams", "get_standings"], "tool_calls": 2, "issues": []}
```

a2a-sdk 1.x carries data parts as protobuf `Value`s, so numbers arrive as floats (`2.0`).

## Confidence is a rule over tool results, not the model's opinion

`runner.ToolLedger` watches the run's tool calls and results; the confidence is:

- **high** when at least one tool ran and every tool's **latest** result is clean;
- **low** when a tool's latest result is a gap (`status` `ambiguous` / `not_found`) or has
  an `error` (any kind, including an MCP `isError`), or when no tool ran.

*Latest*, not *any*: the instructions tell the model to re-call get_standings with a picked
option after `ambiguous`, and a search that missed and then hit is fine. Ending on a gap
means the model asked a clarifying question or said the data is unavailable: that's
`low`, and sb-support escalates. No tool call is `low` because everything routed here is
a data question; "my team" is answered with a question, so it escalates too. That is
deliberate for phase 1 (Slack users have no MT identity) and noisy; revisit with SB-1500.

**Why not ask the model** (an output schema with a `confidence` field, as docs-agent does):
the facts it would judge are already in the tool results, deterministically. A rule is
testable and can't be talked out of; docs-agent needed an instruction fix and an eval case
before its self-reported confidence was calibrated (sb-support-routing.md).

### How the data part gets onto the wire

`TrackedRunner` subclasses ADK's `Runner` and wraps `run_async`, which ADK's A2A executor
calls exactly once per request. When the final answer event passes through, it yields a
**copy** with one extra genai part: `inline_data` of mime `text/plain` wrapped in
`<a2a_datapart_json>…</a2a_datapart_json>`. ADK's part converter turns that into an A2A
data part, and the executor builds the result artifact from the parts of the **last**
status message, so text and data land in one artifact.

Rejected alternatives:

- `after_agent_callback` returning new content: becomes a separate, later event, so the
  artifact would hold the data part alone and lose the text (or the text twice).
- `after_model_callback` appending to the response: the data part would be stored in the
  ADK session, and on a follow-up turn in the same A2A context `AnthropicLlm` would be
  handed an `inline_data` text/plain part it can't convert (`NotImplementedError`). The
  copy keeps the session plain text; a test checks that.
- An a2a-sdk `ExecuteInterceptor`: works on A2A events, so it would need the run's tool
  results passed around through session state. The runner already sees them.

## Caps and telemetry

| Cap | Value | How |
|---|---|---|
| LLM calls per request | 8 | `RunConfig(max_llm_calls=8)` forced onto the executor's run config |
| Cost per request | $0.02 | checked after each model response; stops before the next tool call |

A standings question is ~3 calls (search_teams, get_standings, answer), one more for an
ambiguous table. Hitting a cap raises `runner.UsageLimitExceeded` (named like PydanticAI's,
so `track_run` records outcome `limit`), and the A2A task ends `TASK_STATE_FAILED`, which
sb-support reports as a failed specialist and escalates.

One `track_run("mt-agent", caller="a2a", model=...)` per A2A request: tokens from each
model event's `usage_metadata`, cost from `sb_obs.price()`, `tool_calls`, `confidence`.
`routed_to` is not set (mt-agent routes nowhere).

**One trace, measured in `tests/test_trace_hop.py`:** Slack → `support message slack` →
`execute_tool ask_mt_agent` → mt-agent `POST /` → `agent_run mt-agent` →
`invoke_agent mt_agent` → `execute_tool search_teams` → mt-mcp `tools/call search_teams`.
Each server runs in an empty OTel context, so the trace only joins through what crosses
the wire: `traceparent` on the A2A request (`instrument_asgi` continues it, and a2a-sdk's
executor task inherits it), and `_meta.traceparent` on the MCP request (the mcp SDK
injects and its server middleware extracts it). With a client that sends no
`traceparent`, the same scenario splits into separate traces.

a2a-sdk traces its own internals: 72 spans per request under scope `a2a-python-sdk`,
65 of them `EventQueueSource.*` (one tool call, measured in the hop scenario). Noise, not wrong; dropping them is a follow-up for sb-obs.

## Interop: what the two A2A servers actually send

Both servers answer **A2A 1.0** the same way when asked in 1.0 form, so sb-support's
`SpecialistClient` now sends exactly that and nothing else:

```http
POST /                       A2A-Version: 1.0
{"jsonrpc":"2.0","method":"SendStreamingMessage",
 "params":{"message":{"messageId":"…","role":"ROLE_USER","parts":[{"text":"…"}]}}}
```

SSE events, both servers: `{"result": {"task"|"statusUpdate"|"artifactUpdate"|"message": …}}`,
camelCase, states `TASK_STATE_*`, parts `{"text"}` / `{"data"}`. Differences that remain:
fasta2a wraps structured output as `{"data": {"result": {…}}}` (ADK sends it bare) and
ships a `json_schema` in part metadata; ADK adds `adk_*` metadata. The client reads
`data.result` when present.

What the old request (fasta2a's `A2AClient`, method `message/stream`, no version header)
got back, measured in-process:

| Server | Answer to `message/stream` |
|---|---|
| fasta2a (docs-agent) | 1.0 objects, old names: `{"result": {"artifactUpdate": …}}`, lowercase states (`completed`) |
| a2a-sdk (mt-agent) | **A2A 0.3**: `{"result": {"kind": "artifact-update", …}}`, parts `{"kind": "text", …}` |

a2a-sdk treats a request without `A2A-Version` as 0.3 (spec default) and routes it to its
0.3 adapter, so the old client found no `artifact_update` key and raised "returned no
answer". `SendStreamingMessage` without the header is refused by a2a-sdk as a version
mismatch (JSON-RPC `-32009`, as plain JSON, not SSE; the client reads that too).
sb-support no longer depends on fasta2a.

The client still uses the base URL from `SB_SUPPORT_SPECIALISTS`, not the card's
`supportedInterfaces[].url`: a card built from host/port env can advertise an address the
caller can't reach (127.0.0.1 inside a pod).

## Auth: phase 1 and phase 2

**Phase 1 (this):** mt-agent calls mt-mcp as **one public, read-only MT account**:
`Authorization: Bearer $MT_AGENT_MCP_TOKEN` (1Password `op://agents/mt-agent/token`; the
account is created by the owner, read tier only) and `X-MT-Client: sb-agents`. The tool filter is
`search_teams`, `get_upcoming_matches`, `get_standings`: no `get_my_team`, since the
account has no team and a Slack user has no MT identity. mt-mcp re-checks scope on every
call regardless.

Unset token: `main()` logs a warning and starts; mt-mcp answers 401, every tool call
fails, and every answer is `low`.

**Phase 2 (vision.md, "who is asking"):** the end user's identity flows to the data layer.
The MT Chat adapter (SB-1500) already holds the user's MT token; it travels on the A2A
request (as an auth header or message metadata, decided then), and mt-agent builds the
toolset per request with that token through `McpToolset(header_provider=...)`. Then
`get_my_team` and the `my` tier can be enabled for that caller, and Slack keeps the
public identity until a Slack ↔ MT account link exists. Also open: auth between agents
(SB-1480).

## Run it

```bash
make obs-up                 # optional: Grafana on :3000
make mt-a2a                 # mt-agent on :8102, MT_AGENT_MCP_TOKEN via .env.mt.op
SB_SUPPORT_SPECIALISTS=http://127.0.0.1:8101,http://127.0.0.1:8102 make support
```

Then in Slack: `@sb-agent what are the standings for IFA U14?`

Not yet done: a live run against prod mt-mcp (needs the 1Password item), evals for
mt-agent, and an end-to-end Slack check.
