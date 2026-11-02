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

**Status:** done for PydanticAI agents (Anthropic, OpenRouter, Ollama all ran unchanged).
Gaps: the ADK router is pinned to `AnthropicLlm` because ADK's LiteLLM range conflicts with
PydanticAI's `openai` version (SB-1490); `sb_obs.price()` assumes the Anthropic provider
unless told otherwise.

## 3. Framework-independent

Agents may be built with any framework. **A2A is the boundary between them**: there is no
shared in-process agent interface.

**Check:** an agent's contract is its A2A agent card + a text part + a data part
`{confidence, sources}`. Nothing outside the agent's package imports its framework.
A caller works the same against a PydanticAI or an ADK specialist.

**Status:** half done. ADK → PydanticAI works (adk-docs-client → docs-agent). PydanticAI →
ADK arrives with mt-agent (SB-1493). The `{confidence, sources}` data part is a docs-agent
convention today; mt-agent makes it the written contract.

## 4. Local-first

Everything runs on the Mac mini M4 Pro and k3s, with no required cloud service besides
model APIs.

**Check:** the whole stack (agents, Grafana/Loki/Tempo/Prometheus, evals) starts from
this repo on the mini. Secrets come from 1Password, never from files in the repo.

**Proposed definition, pending a decision:** local models (Ollama) where latency doesn't
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
| Local-first meaning | **pending** (see pillar 4) | — |

## Not part of the platform

iron-claw and match-scraper-agent are retired; don't build on them.
