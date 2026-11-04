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
from sb_support.tables import Table

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


# Block Kit limits (docs.slack.dev/reference/block-kit/blocks/table-block, read 2026-11-04):
# 100 rows, 20 cells a row, 10,000 characters across a message's table cells; a section's
# mrkdwn text is at most 3,000 characters. Every `column_settings` entry must be an object:
# Slack answered `null` with "invalid_blocks: must provide an object" (SB-1533).
TABLE_MAX_ROWS, TABLE_MAX_COLS, TABLE_MAX_CHARS, SECTION_MAX = 100, 20, 10_000, 3_000
_NUMERIC = re.compile(r"^[+-]?\d+$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")  # specialists write CommonMark bold


def mrkdwn(text: str) -> str:
    """CommonMark **bold** → Slack's *bold*."""
    return _BOLD.sub(r"*\1*", text)


def _bold_cell(text: str) -> dict[str, Any]:
    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_section",
                "elements": [{"type": "text", "text": text, "style": {"bold": True}}],
            }
        ],
    }


def _cell(text: str, bold: bool) -> dict[str, Any]:
    return _bold_cell(text or " ") if bold else {"type": "raw_text", "text": text or " "}


def table_block(table: Table) -> dict[str, Any] | None:
    """A Block Kit table (first row is the header), or None if it breaks a limit."""
    rows = [table.columns, *table.rows]
    chars = sum(len(c) for r in rows for c in r)
    if len(rows) > TABLE_MAX_ROWS or len(table.columns) > TABLE_MAX_COLS or chars > TABLE_MAX_CHARS:
        return None
    numeric = [
        bool(table.rows) and all(_NUMERIC.match(r[i] or "0") for r in table.rows)
        for i in range(len(table.columns))
    ]
    return {
        "type": "table",
        "column_settings": [{"align": "right" if n else "left"} for n in numeric],
        "rows": [[_cell(c, i - 1 in table.highlight) for c in r] for i, r in enumerate(rows)],
    }


def table_as_code(table: Table) -> str:
    """The fallback: an aligned monospace table, for when blocks can't be sent."""
    rows = [table.columns, *table.rows]
    widths = [max(len(r[i]) for r in rows) for i in range(len(table.columns))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in rows]
    title = f"*{table.title}*\n" if table.title else ""
    notes = "".join(f"\n{mrkdwn(n)}" for n in table.notes)
    return f"{title}```\n" + "\n".join(lines) + "\n```" + notes


def blocks_for(reply: Reply, text: str) -> list[dict[str, Any]] | None:
    """Text + the first table as Block Kit, or None (send plain text). One table per reply."""
    if reply.outcome == "help" or not reply.tables or len(text) > SECTION_MAX:
        return None
    table = reply.tables[0]
    block = table_block(table)
    if block is None:
        return None
    out: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if table.title:
        out.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"*{table.title}*"}]}
        )
    out.append(block)
    if table.notes:
        notes = "\n".join(f"• {mrkdwn(n)}" for n in table.notes)
        out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": notes}]})
    return out


async def post_reply(client: Any, channel: str, thread_ts: str, reply: Reply) -> None:
    """Post the reply; a table goes as a Block Kit table, else as a code block."""
    text = render(reply)
    blocks = blocks_for(reply, text)
    if blocks is not None:
        try:
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=text, blocks=blocks
            )
            return
        except Exception:  # noqa: BLE001 - e.g. invalid_blocks: fall back, don't lose the reply
            log.warning("table blocks rejected; posting a code block instead", exc_info=True)
    if reply.tables and reply.outcome != "help":
        text = f"{text}\n\n{table_as_code(reply.tables[0])}"
    await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


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
        await post_reply(client, channel, thread_ts, reply)
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
