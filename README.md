# sb-agents

SB support + alert-triage agents, built to learn **Google ADK**, **PydanticAI** and
**A2A** before building the same thing at work.

Epic: [AGT — Support & Triage Agents](https://linear.app/silverbeer/project/agt-support-and-triage-agents-1f1a2497e0f9)

## Layout

uv workspace, one package per agent:

| Package | Framework | What |
|---|---|---|
| `packages/sb-support` | PydanticAI + Slack Bolt | Answers @mentions in Slack threads (Socket Mode); SB questions go to docs-agent over A2A (`ask_docs`) |
| `packages/docs-agent` | PydanticAI + fasta2a | Answers from SB repo markdown docs, with citations and confidence; served over A2A |
| `packages/adk-docs-client` | ADK | Router `LlmAgent` that delegates docs questions to docs-agent via `RemoteA2aAgent` |
| `packages/sb-obs` | OpenTelemetry | Shared `setup_telemetry(service)`: traces, metrics, GCP-format JSON logs |

More agents (infra, change, ticket, triage) land as sibling packages.

## docs-agent

Searches git-tracked `*.md` in sibling repos under `$DOCS_AGENT_ROOT` (default
`~/gitrepos`); the repo list is `DEFAULT_REPOS` in `corpus.py`. Retrieval is BM25
keyword search over heading-level sections behind a `Retriever` protocol (qmd
backend: SB-1483). The agent must search before answering and may only cite real
corpus files; both are enforced by an output validator.

```bash
op run --env-file=.env.op -- uv run docs-agent ask -v "How do I deploy missing-table?"
op run --env-file=.env.op -- uv run docs-agent eval   # 13 cases, real model, exits 1 below 80%
```

Evals use `pydantic-evals`: cited-expected-source, low confidence on unanswerable
questions, and an LLM judge (`DOCS_AGENT_JUDGE_MODEL`, default Sonnet) against a
reference answer. They call the real model, so they are not in CI.

## Run locally

Secrets live in 1Password and are injected at runtime by `op run`. `.env.op` holds
only `op://` references, never values.

```bash
uv sync --all-packages
op run --env-file=.env.op -- uv run sb-support
```

Then in Slack (silverbeer.io): `@sb-agent hello` in `#sb-agents-dev`. The bot reacts 👀
while working, replies in thread, then ✅ (or ❌ on failure).

| Env | Source |
|---|---|
| `SLACK_BOT_TOKEN` | `op://agents/sb-slack/bot-token` (`xoxb-`) |
| `SLACK_APP_TOKEN` | `op://agents/sb-slack/app-token` (`xapp-`, scope `connections:write`) |
| `ANTHROPIC_API_KEY` | `op://agents/anthropic_key/password` |
| `SB_SUPPORT_MODEL` | optional, default `anthropic:claude-haiku-5-5` |

## A2A hop (ADK → docs-agent)

```bash
op run --env-file=.env.op -- uv run docs-agent-a2a          # :8101, card at /.well-known/agent-card.json
op run --env-file=.env.op -- uv run adk-docs-client "How do I release the Android app?"
```

What broke and why: [docs/notes/a2a-first-hop.md](docs/notes/a2a-first-hop.md).

## Observability

Every entry point calls `sb_obs.setup_telemetry("<service>")`. Local backend is
`grafana/otel-lgtm` (Collector, Tempo, Loki, Prometheus, Grafana in one container, $0):

```bash
make obs-up                                   # Grafana http://localhost:3000 (admin/admin)
make docs-a2a                                 # docs-agent A2A server, exporting
make adk-ask Q="How do I install janitor?"    # one trace across router -> A2A -> docs-agent
make support                                  # Slack bot, exporting (needs docs-a2a running)
make obs-down
```

Dashboards are code: edit `observability/grafana/build_dashboards.py`, run `make dashboards`,
and Grafana picks up the JSON (folder *sb-agents*). Cost and run panels read the run
records in Loki; latency panels use Tempo span metrics and the service graph.

| Env | Effect |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | export over OTLP/HTTP; unset = no export (tests, CI). `make` sets `http://localhost:4318` |
| `SB_OBS_CAPTURE_CONTENT` | `1` = prompts/answers in spans. Default off; `make` targets turn it on locally |
| `SB_OBS_LOG_FORMAT` | `json` (Cloud Logging shape, stderr) or `text` |
| `SB_OBS_GCP_PROJECT` | full `projects/<p>/traces/<id>` trace names in logs |
| `SB_ENV` | `deployment.environment.name`, default `local` |

What each framework emits, and the gotchas: [docs/notes/observability-setup.md](docs/notes/observability-setup.md).

## Develop

```bash
make test
make lint
```

Tests use PydanticAI's `TestModel` and a fake Slack client: no network, no tokens.
