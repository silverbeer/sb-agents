"""Slack Bolt app (Socket Mode) that answers @mentions in a thread."""

import asyncio
import logging
import os
import re
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from sb_obs import model_name, setup_telemetry, track_run
from sb_support.agent import USAGE_LIMITS, build_agent

log = logging.getLogger("sb_support")

MENTION_RE = re.compile(r"<@[A-Z0-9]+>")

WORKING, DONE, FAILED = "eyes", "white_check_mark", "x"

RUN_HINT = "op run --env-file=.env.op -- uv run sb-support"


def strip_mentions(text: str) -> str:
    """Remove `<@U123>` user mentions and surrounding whitespace."""
    return MENTION_RE.sub("", text).strip()


async def _react(client: Any, channel: str, ts: str, name: str, *, remove: bool = False) -> None:
    """Best-effort reaction; a failed emoji must never break the reply."""
    try:
        fn = client.reactions_remove if remove else client.reactions_add
        await fn(channel=channel, timestamp=ts, name=name)
    except Exception:  # noqa: BLE001 - reactions are cosmetic
        log.warning("reaction %s %s failed", "remove" if remove else "add", name, exc_info=True)


async def answer(agent: Agent[None, str], question: str) -> str:
    """Run under usage limits and emit a RunRecord. A hit limit becomes a reply, not a crash."""
    with track_run("sb-support", caller="slack", model=model_name(agent.model)) as run:
        try:
            result = await agent.run(question, usage_limits=USAGE_LIMITS)
        except UsageLimitExceeded:
            log.warning("usage limit hit answering %r", question[:80])
            raise
        run.record_usage(result.usage)
        return result.output


async def handle_mention(event: dict[str, Any], client: Any, agent: Agent[None, str]) -> None:
    """Answer an app_mention in its thread, with 👀 while working and ✅/❌ when done."""
    channel, ts = event["channel"], event["ts"]
    thread_ts = event.get("thread_ts", ts)
    question = strip_mentions(event.get("text", ""))

    await _react(client, channel, ts, WORKING)
    try:
        if not question:
            reply = "Hi! Mention me with a question and I'll answer in this thread."
        else:
            reply = await answer(agent, question)
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=reply)
        outcome = DONE
    except UsageLimitExceeded:
        outcome = FAILED
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="That one needed more work than I'm allowed per question. Try narrowing it down.",
        )
    except Exception:
        log.exception("failed to answer mention in %s at %s", channel, ts)
        outcome = FAILED
        try:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="Sorry, something went wrong answering that. It's been logged.",
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to post error reply")
    finally:
        await _react(client, channel, ts, WORKING, remove=True)
    await _react(client, channel, ts, outcome)


def build_app(agent: Agent[None, str] | None = None) -> AsyncApp:
    agent = agent or build_agent()
    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])

    @app.event("app_mention")
    async def on_mention(event: dict[str, Any], client: Any) -> None:
        await handle_mention(event, client, agent)

    @app.event("message")
    async def on_message(event: dict[str, Any]) -> None:
        # Subscribed via the manifest; ignored until sb-support handles channel threads (SB-1476).
        return None

    return app


async def _run() -> None:
    handler = AsyncSocketModeHandler(build_app(), os.environ["SLACK_APP_TOKEN"])
    await handler.start_async()


def main() -> None:
    setup_telemetry("sb-support")
    required = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ANTHROPIC_API_KEY")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"missing env: {', '.join(missing)}. Run via: {RUN_HINT}")
    asyncio.run(_run())
