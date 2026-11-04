# mt-agent: chat with MissingTable data over A2A (SB-1493)

The first ADK specialist and the first **PydanticAI → ADK** hop: sb-support (PydanticAI)
routes MT data questions to mt-agent (ADK) over A2A, and mt-agent reads MT through
mt-mcp's tools. Versions: google-adk 2.13.0, a2a-sdk 1.3.0, fasta2a 2.1.1, mcp 2.3.0.

```
Slack ─► sb-support core ── ask_mt_agent ──A2A 1.0──► mt-agent (ADK LlmAgent, Haiku)
                                                        └─ McpToolset ──MCP──► mt-mcp
                                                             search_teams, list_divisions,
                                                             get_upcoming_matches,
                                                             get_recent_results, get_standings
```

## Layout

| Module | What |
|---|---|
| `agent.py` | `LlmAgent` (Claude via `AnthropicLlm`), instructions, `McpToolset` with `tool_filter`, MCP result unwrapping |
| `runner.py` | `TrackedRunner`: caps, the RunRecord, and the reply's data part |
| `a2a_server.py` | agent card, `to_a2a(...)`, `main()` (uvicorn, `instrument_asgi`, `setup_telemetry`) |
| `preview.py` | match preview as an ADK `Workflow`: parallel deterministic reads, one writer call |
| `mt_client.py` | mt-mcp called directly (preview workflow, eval ground truth) |
| `sessions.py` | `BoundedSessionService`: one ADK session per A2A context, bounded by count and idle time |
| `evals.py` | `mt-agent-eval`: ground truth from mt-mcp at eval time, four checks, EvalRecord |

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

A gap is also resolved by a later clean result from a **different** tool: the model
found an id and used it. The head-to-head eval caught this (SB-1526): "FC Westchester"
without an age is `ambiguous`, but every candidate is the same `team_id`, and the model
went on to read results with it; the old rule still said `low`. Errors are never
resolved that way.

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

## Follow-up questions: A2A contextId → ADK session (SB-1525)

"Standings for IFA U15?" then "and their U14 team?" works because both messages carry
the same A2A `contextId`:

```
sb-support ── contextId = uuid5("sb-support/<channel:thread>/mt-agent") ──► mt-agent
                                     ADK A2A executor: session_id = contextId,
                                     user_id = A2A_USER_<contextId> (caller unauthenticated)
                                     get_session → reuse, else create_session
```

- **ADK does the mapping.** `to_a2a`'s executor (`google/adk/a2a/converters/request_converter.py`)
  uses the request's `contextId` as the ADK session id, and creates the session on the
  first message. Nothing in mt-agent had to map it; what was missing was a caller that
  sends the same `contextId` twice.
- **Opt-in per specialist, declared in the card.** mt-agent's card lists the extension
  `urn:sb-agents:a2a:conversation:v1` under `capabilities.extensions`; sb-support's
  `discover()` reads it and only those specialists get a stable `contextId`. docs-agent
  doesn't declare it, on purpose: fasta2a replays a context's whole message history,
  tool results included, so a reused context would carry every doc it read into the next
  question and run into the token cap.
- **The id is opaque.** A uuid5 of the conversation key and the specialist name: stable
  for one Slack thread, different per specialist, and no channel or thread ids reach the
  specialist. It is also stable across an sb-support restart.
- **Sessions are bounded.** `InMemorySessionService` keeps every session forever, and a
  caller that sends no `contextId` gets a new server-generated one per message, so the
  process grew by a session per question. `BoundedSessionService` keeps at most 500 and
  drops any idle for 30 minutes (and the per-context `A2A_USER_*` shells with them).
  Lost on restart: a follow-up after a restart just starts over.
- **sb-support still rewrites follow-ups** into self-contained questions (its
  instructions say so), so the session is a second line, not the only one: it is what
  lets mt-agent reuse the team it resolved and the table it picked, without a new
  clarifying question.

The eval's `follow-up` case runs "Where are IFA U15 in the league standings?" then
"And their U14 team?" in one session and grades only the second reply.

## Match preview: an ADK Workflow (SB-1527)

"Preview IFA U15's next game" needs five mt-mcp reads, and none of them needs the
model's judgment once the team is known. So they run as a graph, and the model only writes:

```
START ─► fixture ─┬─► team_form ─────┐
                  ├─► opponent_form ─┤
                  ├─► table ─────────┼─► gather (JoinNode) ─► brief ─► preview_writer (LlmAgent)
                  ├─► head_to_head ──┤
                  └─► carry ─────────┘   (the fixture itself, so the writer sees date and venue)
```

- **`Workflow`, not `ParallelAgent`.** In ADK 2.13 `ParallelAgent` and `SequentialAgent`
  are marked deprecated in favor of `google.adk.workflow.Workflow`, a graph of nodes.
  `@node` turns an async function into a `FunctionNode` (deterministic, no model); a
  tuple in an edge fans out; a `JoinNode` waits for every branch and outputs
  `{branch_name: output}`; an `LlmAgent` as a node gets its predecessor's output as the
  user message. Branches ran concurrently (checked in a spike before building on it).
- **Exposed as a tool.** A Workflow can't be an LlmAgent sub-agent yet, so
  `match_preview(team_id, age_group_id)` is a plain function tool that runs the workflow
  in a nested `Runner(node=workflow)`. The main agent resolves the team as usual
  (`search_teams`), calls it, and relays the preview.
- **One RunRecord.** The nested runner's events never reach `TrackedRunner`, so the
  tool adds the writer's usage to the request's run through a `ContextVar`
  (`runner.CURRENT_RUN`). The eval caught the gap first: its agent cost priced only the
  main runner's events.
- **A failed read doesn't sink the preview.** Each read returns `{"error": ...}` on
  failure and the writer is told to say that part is unavailable. No upcoming match →
  `not_found`, and the branches skip their reads.

### Workflow vs single agent (2026-11-04, `match-preview` case, Haiku, 3 runs each)

`uv run mt-agent-eval --case match-preview [--no-preview]` (inside `op run`).
`--no-preview` drops the tool, and the agent gathers the same data with its own calls.

| | Workflow | Single agent |
|---|---|---|
| Passed | 3/3 | 3/3 |
| Model requests | 4 (3 main + 1 writer) | 5 |
| Tool calls the model makes | 2 (`search_teams`, `match_preview`) | 7 |
| Input tokens | 14.3k | 29.8k |
| Cost / question | **$0.0024** | $0.0038 |
| Latency | ~12 s | ~12 s |

Half the input tokens and 38% cheaper at the same quality: the single agent re-sends
every tool result on each round trip, the workflow sends the data once to the writer.
Latency is a wash: the reads run in parallel, but the main agent's relay adds a model
call. Returning the preview without the relay (`skip_summarization`) would cut that;
not done, because the final event would then carry no text part for the data part.

## Recent results and head-to-head (SB-1526)

mt-mcp's `get_recent_results` (missing-table PR #726) returns a team's last played
matches scored from its side (W/D/L, goals for/against, shootout alongside a draw) and a
`summary`; `opponent_id` narrows it to head-to-head. mt-agent lists it in `TOOLS` and its
instructions: give scores from the team's side, resolve the opponent with `search_teams`
first, and treat `matches: []` as "none played", not "unavailable". The eval gained
`last-result`, `recent-form` and `head-to-head`, with truth from the same tool. First
run 12/14: one fact choice too strict (qualifying asks for a record, not points), and
the confidence-rule gap below. After the fix: 14/14 twice, ~$0.022 agent + $0.045 judge.

## Evals: ground truth read live (SB-1493)

```bash
make mt-eval                                  # ~$0.04 (Haiku agent + Sonnet judge), ~20 s
uv run mt-agent-eval --case flex-table -v     # one case, print the answer (inside op run)
```

MT's data moves every weekend, so an expected answer written down today is wrong by
Monday. Each case is a question plus a `truth` function that calls mt-mcp **directly,
with no model in the loop** (same token and `X-MT-Client` as the agent), and builds the
expected answer from what comes back. The eval measures whether the agent reads and
reports MT's data correctly, not whether MT's data is right.

Four checks per case; a case passes when every check that applies passes:

| Check | Kind | What |
|---|---|---|
| FactsMentioned | deterministic | key facts from the truth (position, points, opponent) appear in the answer; a number must stand alone (`10` matches "10th", not "110") |
| ConfidenceMatches | deterministic | the data part's confidence: `high` for clean lookups, `low` when the agent should ask or found nothing |
| ToolPath | ADK `TrajectoryEvaluator` | expected tools ran in order (`IN_ORDER`, args ignored); retries and extra calls are fine |
| LLMJudge | Sonnet 5.5 | agrees with the truth; asks instead of guessing when it should |

**When a premise breaks, the run stops before any model call.** Each truth function
checks its premise ("Metropolitan Oval is ambiguous", "Zxqv Rovers is not found", "the
table has rows") and raises `TruthError`; all broken cases are reported together. That
fired on the first run: the U14 Academy New England table had no games played yet, so
"who's top" had no answer. The case moved to U15 Homegrown Northeast.

**ADK eval vs pydantic-evals.** ADK 2.13 ships an eval framework
(`google.adk.evaluation`): eval sets of fixed conversations with expected responses and
tool trajectories, scored by metrics such as `tool_trajectory_avg_score`. Fixed expected
responses don't fit live data, so the suite runs on pydantic-evals (the same harness,
judge and `EvalRecord` as docs-agent, so the two compare) and borrows ADK's
`TrajectoryEvaluator` for the tool-path check by building one `Invocation` per answer.

### Results (2026-11-04, 10 cases)

| Model | Runs | Passed | Agent $ / eval | Judge $ | Avg per case | Notes |
|---|---|---|---|---|---|---|
| claude-haiku-5-5 | 2 | **10/10, 10/10** (11/11 with `follow-up`, SB-1525; 14/14 x2 with results cases, SB-1526; 15/15 with `match-preview`, SB-1527) | $0.012 (~$0.0012/question) | $0.03 | 3.3 s | default |
| claude-sonnet-5-5 | 1 | 9/10 | $0.19 (~16x Haiku) | $0.03 | 4.1 s | `flex-table` hit the $0.02 per-run cost cap |

Two things the eval caught while being built, both fixed:

- **An ambiguous coverage string.** mt-mcp's qualifying coverage reads "79 played of 258
  scheduled; 5 against 1 teams outside this table", and the agent sometimes reported
  "1 team" as the count of outside matches (judge failed 1 of 2 runs). The instructions
  now point at the structured fields (`matches_vs_outside_table`, `teams_outside_table`).
  Worth fixing at the source in mt-mcp's wording too.
- **An underspecified case.** "Will FC Westchester make the playoffs?" fairly reads as the
  League table or the qualifying record; the truth now carries both, and the case checks
  only that the answer doesn't call qualification.

mt-agent stays Claude-only (ADK + LiteLLM conflict, SB-1490), so the second model is
Sonnet, not a second provider. SB-1520 moves the model choice to the resolver.

## Run it

```bash
make obs-up                 # optional: Grafana on :3000
make mt-a2a                 # mt-agent on :8102, MT_AGENT_MCP_TOKEN via .env.mt.op
SB_SUPPORT_SPECIALISTS=http://127.0.0.1:8101,http://127.0.0.1:8102 make support
```

Then in Slack: `@sb-agent what are the standings for IFA U14?`

Deployed in k3s since SB-1481 (docs/notes/deploy-k3s.md).
