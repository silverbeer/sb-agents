# Vision: what sb-agents is building toward

sb-agents is a small **agent platform**, not one app with agents in it. MissingTable is
the first real user (chat with MT data, MT user guides, a coach for kids learning tech),
and the same pieces should carry over to work (Slack support, alert triage on Grafana +
GCP).

Every PR is checked against the six pillars below. Each has a **check**: a concrete
question with a yes/no answer. "Status" says where the code is today, with the ticket
that closes the gap.

## 1. Messaging-agnostic

Channels (Slack, Telegram, the MT web and mobile apps) are adapters. Agents and the
support core never see a channel's types.

**Check:** a new channel is one adapter module plus config, with no change to
`sb_support/core.py` or to any agent.

**Status:** done for the front door (SB-1498): `core.py` takes an `InboundMessage` and
returns a `Reply`, and a test fails if it imports a channel SDK. Slack is the first adapter.
Next: MT Chat (SB-1500), Telegram (not filed).

## 2. Model-agnostic

The model is configuration. Agents are written against their framework's model interface,
never a provider SDK.

**Check:** switching an agent's model is an env var plus an eval run, with no code change.
No default changes without an eval showing it holds (docs/notes/model-comparison.md).

**Status** (section D of the platform assessment, 2026-11-03; SB-1518):

| Criterion | State | Where |
|---|---|---|
| Provider abstraction | 🟡 PydanticAI agents: any provider by config. ADK agents: Claude-only until ADK lifts its LiteLLM cap (SB-1490); the registry refuses others with that reason | framework model interfaces |
| Model selection | 🟢 one registry; each agent's default + env var (`DOCS_AGENT_MODEL`, `SB_SUPPORT_MODEL`, `MT_AGENT_MODEL`, ...) | `packages/sb-models/src/sb_models/models.toml` |
| Capabilities / metadata | 🟢 context window, tools, structured output, local/hosted, credential, last eval; checked at startup against what the agent needs | `sb_models.resolve()` |
| Credentials | 🟢 per provider (`ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `OLLAMA_BASE_URL`), checked at startup; 1Password refs locally, one Secret per Deployment in k3s | `k3s/provision-secret.sh` (a new provider's key is still added there by hand) |
| Usage and cost | 🟢 RunRecord + metrics carry `provider` and `local`; local runs cost $0, unknown stays unknown | `sb_obs.track_run` |
| Streaming | 🟡 A2A streams events between agents; no token streaming to users (needed by MT Chat, SB-1500) | |
| Provider leakage | 🟡 `AnthropicLlm` in mt-agent, ADK's empty text part handled in the A2A client, Ollama `num_ctx` in a model tag (SB-1520) | |
| Local-first default | 🔴 no workload runs on Ollama yet; chat stays hosted by measurement (gemma 12B: 11/14, p50 516 s). Batch profile + fallback: SB-1519 | |

## 3. Framework-independent

Agents may be built with any framework. **A2A is the boundary between them**: there is no
shared in-process agent interface.

**Check:** an agent's contract is its A2A agent card + a text part + a data part
`{confidence, sources}`. Nothing outside the agent's package imports its framework.
A caller works the same against a PydanticAI or an ADK specialist.

**Status:** both directions work. ADK → PydanticAI (adk-docs-client → docs-agent) and
PydanticAI → ADK (sb-support → mt-agent, SB-1493). sb-support's client speaks A2A 1.0
(`SendStreamingMessage`, `A2A-Version: 1.0`) and handles both fasta2a and a2a-sdk servers;
both agents reply with text + a `{confidence, sources}` data part
(docs/notes/mt-agent.md).

## 4. Local-first

Everything runs on the Mac mini M4 Pro and k3s, with no required cloud service besides
model APIs.

**Check:** the whole stack (agents, Grafana/Loki/Tempo/Prometheus, evals) starts from
this repo on the mini. Secrets come from 1Password, never from files in the repo.

**Definition (decided 2026-11-02):** local models (Ollama) where latency doesn't
matter (evals, batch, triage summaries); hosted models allowed for interactive chat.
Measured: local gemma4:12b 11/14 at ~100 s per question, vs Haiku 14/14 at ~5 s.

**Status:** runs locally via `make`; not deployed to k3s (SB-1481). Blockers for k3s:
docs-agent reads `~/gitrepos` from the host; conversation memory and A2A tasks are
in memory only.

## 5. Extensible: many agents, agent-to-agent

New specialists join by URL. Routing uses what agents publish about themselves.

**Check:** adding a specialist is a new package serving A2A plus a URL in
`SB_SUPPORT_SPECIALISTS`, with no change to sb-support. Escalation stays a rule
(failed or low-confidence specialist), not a model's choice.

**Status:** done for discovery (agent cards → `ask_<name>` tools) and escalation (SB-1476).
Gaps: each PydanticAI A2A server repeats ~180 lines of setup (extract a helper when a
second one appears); no auth between agents (SB-1480).

## 6. Observable

Traces, metrics and logs are part of the design, not an add-on.

**Check:** one user message is one trace across every agent and tool it touches, and one
`RunRecord` per agent run carries tokens, cost, outcome, routing and the trace id.
Dashboards and alerts live in the repo as code.

**Status:** done (SB-1478, SB-1484 to SB-1488): sb-obs, Grafana dashboards, alerts to
`#sb-alerts`, eval scores as metrics. Gaps: no tool latency/failure panel; retries aren't
recorded.

## Decisions so far

| Decision | Choice | Where |
|---|---|---|
| Boundary between frameworks | A2A, not a shared interface | SB-1493 |
| Front door | channel-neutral core + per-channel adapters | SB-1498 |
| MT Chat | MT's `/api/ai/chat` becomes an adapter into this core | SB-1500 |
| MT agents | one MT data agent; split only for a real boundary (data, credentials, model, owner, framework) | SB-1493 |
| Cost tracking | genai-prices, pinned in tests; RunRecord per run | SB-1484 |
| Local-first meaning | local models for latency-tolerant work; hosted allowed for chat | pillar 4 |
| ADK model path | Claude-only until ADK lifts its LiteLLM cap | SB-1490 |
| Who is asking | the end user's identity flows to the data layer (MT tools run as the user, not a service account) | SB-1493 |

## Not part of the platform

iron-claw and match-scraper-agent are retired; don't build on them.
