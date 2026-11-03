"""Slack adapter (Bolt, Socket Mode): Slack events → `InboundMessage`, `Reply` → thread reply.

Slack-only concerns live here: mention markup, threads, 👀/✅/❌/🙋 reactions, and
who to @-mention on escalation. Routing, memory and escalation decisions are in
`sb_support.core`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

from sb_models import ModelConfigError, resolve
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from sb_obs import setup_telemetry
from sb_support.agent import build_agent
from sb_support.core import InboundMessage, Reply, SupportCore
from sb_support.specialists import discover

log = logging.getLogger("sb_support.slack")

CHANNEL = "slack"
MENTION_RE = re.compile(r"<@[A-Z0-9]+>")

WORKING = "eyes"
OUTCOME_REACTION = {
    "answered": "white_check_mark",
    "help": "white_check_mark",
    "escalated": "raising_hand",
    "limit": "x",
    "error": "x",
}
FAILED = "x"
SLACK_HELP = "Hi! Mention me with a question and I'll answer in this thread."

RUN_HINT = "op run --env-file=.env.op -- uv run sb-support"


def strip_mentions(text: str) -> str:
    """Remove `<@U123>` user mentions and surrounding whitespace."""
    return MENTION_RE.sub("", text).strip()


def conversation_id(channel: str, thread_ts: str) -> str:
    return f"{channel}:{thread_ts}"


def to_inbound(event: dict[str, Any]) -> InboundMessage:
    """An app_mention or thread message event → InboundMessage (thread = conversation)."""
    channel, ts = event["channel"], event["ts"]
    thread_ts = event.get("thread_ts", ts)
    return InboundMessage(
        channel=CHANNEL,
        conversation_id=conversation_id(channel, thread_ts),
        text=strip_mentions(event.get("text", "")),
        user_id=event.get("user"),
        message_id=ts,
        attributes={"slack.channel": channel, "slack.ts": ts, "slack.thread_ts": thread_ts},
    )


def render(reply: Reply) -> str:
    if reply.outcome == "help":
        return SLACK_HELP
    if reply.escalation:
        return reply.text + escalation_text(reply.escalation)
    return reply.text


def escalation_text(reason: str) -> str:
    who = os.environ.get("SB_SUPPORT_ESCALATE_TO")  # Slack user ID, e.g. U0123ABC
    mention = f"<@{who}>" if who else "a human"
    return f"\n\n:raising_hand: Escalating to {mention}: {reason}."


async def _react(client: Any, channel: str, ts: str, name: str, *, remove: bool = False) -> None:
    """Best-effort reaction; a failed emoji must never break the reply."""
    try:
        fn = client.reactions_remove if remove else client.reactions_add
        await fn(channel=channel, timestamp=ts, name=name)
    except Exception:  # noqa: BLE001 - reactions are cosmetic
        log.warning("reaction %s %s failed", "remove" if remove else "add", name, exc_info=True)


async def handle_event(event: dict[str, Any], client: Any, core: SupportCore) -> None:
    """Answer a mention (or a follow-up) in its thread, with 👀 while working and ✅/❌/🙋 after."""
    channel, ts = event["channel"], event["ts"]
    thread_ts = event.get("thread_ts", ts)
    await _react(client, channel, ts, WORKING)
    try:
        reply = await core.handle(to_inbound(event))
        reaction = OUTCOME_REACTION[reply.outcome]
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=render(reply))
    except Exception:
        log.exception("failed to post reply in %s at %s", channel, ts)
        reaction = FAILED
    finally:
        await _react(client, channel, ts, WORKING, remove=True)
    await _react(client, channel, ts, reaction)


def is_follow_up(event: dict[str, Any], core: SupportCore, bot_user_id: str | None) -> bool:
    """A human reply, without a mention of us, in a thread we've already answered.

    Messages that mention the bot arrive as app_mention too; answering both would
    reply twice.
    """
    thread_ts = event.get("thread_ts")
    return bool(
        thread_ts
        and core.knows(CHANNEL, conversation_id(event.get("channel", ""), thread_ts))
        and not event.get("bot_id")
        and not event.get("subtype")
        and (bot_user_id is None or f"<@{bot_user_id}>" not in event.get("text", ""))
    )


def build_app(core: SupportCore | None = None) -> AsyncApp:
    core = core or SupportCore(build_agent())
    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])

    @app.event("app_mention")
    async def on_mention(event: dict[str, Any], client: Any) -> None:
        await handle_event(event, client, core)

    @app.event("message")
    async def on_message(event: dict[str, Any], client: Any, context: Any) -> None:
        if is_follow_up(event, core, context.get("bot_user_id")):
            await handle_event(event, client, core)

    return app


async def _run() -> None:
    specialists = await discover()
    log.info("specialists: %s", [s.name for s in specialists] or "none")
    core = SupportCore(build_agent(specialists=specialists))
    handler = AsyncSocketModeHandler(build_app(core), os.environ["SLACK_APP_TOKEN"])
    await handler.start_async()


def main() -> None:
    setup_telemetry("sb-support")
    required = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"missing env: {', '.join(missing)}. Run via: {RUN_HINT}")
    try:
        resolve("sb-support")  # the model's own credential, whichever provider it is
    except ModelConfigError as exc:
        raise SystemExit(f"{exc}. Run via: {RUN_HINT}") from exc
    asyncio.run(_run())
