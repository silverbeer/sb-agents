# sb-agents

SB support + alert-triage agents, built to learn **Google ADK**, **PydanticAI** and
**A2A** before building the same thing at work.

Epic: [AGT — Support & Triage Agents](https://linear.app/silverbeer/project/agt-support-and-triage-agents-1f1a2497e0f9)

## Layout

uv workspace, one package per agent:

| Package | Framework | What |
|---|---|---|
| `packages/sb-support` | PydanticAI + Slack Bolt | Answers @mentions in Slack threads (Socket Mode) |

More agents (docs, infra, change, ticket, triage) land as sibling packages.

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

## Develop

```bash
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
```

Tests use PydanticAI's `TestModel` and a fake Slack client: no network, no tokens.
