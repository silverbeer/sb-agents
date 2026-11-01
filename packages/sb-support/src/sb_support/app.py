"""Slack Bolt app (Socket Mode): answers @mentions and follow-ups in its threads."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace
from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessage
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from sb_obs import model_name, setup_telemetry, track_run
from sb_support.agent import USAGE_LIMITS, SupportDeps, build_agent
from sb_support.specialists import discover

log = logging.getLogger("sb_support")
_tracer = trace.get_tracer("sb_support")

MENTION_RE = re.compile(r"<@[A-Z0-9]+>")

WORKING, DONE, FAILED, ESCALATED = "eyes", "white_check_mark", "x", "raising_hand"

RUN_HINT = "op run --env-file=.env.op -- uv run sb-support"


def strip_mentions(text: str) -> str:
    """Remove `<@U123>` user mentions and surrounding whitespace."""
    return MENTION_RE.sub("", text).strip()


class ThreadMemory:
    """Conversation history per Slack thread, in memory (lost on restart).

    Trimmed by whole runs, never by message, so a tool call is never separated from
    its result (the API rejects that). Bounded in threads and runs per thread.
    """

    def __init__(self, max_threads: int = 200, max_runs: int = 6) -> None:
        self._threads: OrderedDict[str, list[list[ModelMessage]]] = OrderedDict()
        self.max_threads, self.max_runs = max_threads, max_runs

    def __contains__(self, thread_ts: str) -> bool:
        return thread_ts in self._threads

    def history(self, thread_ts: str) -> list[ModelMessage]:
        return [m for run in self._threads.get(thread_ts, []) for m in run]

    def add(self, thread_ts: str, messages: list[ModelMessage]) -> None:
        runs = self._threads.setdefault(thread_ts, [])
        runs.append(messages)
        del runs[: -self.max_runs]
        self._threads.move_to_end(thread_ts)
        while len(self._threads) > self.max_threads:
            self._threads.popitem(last=False)


@dataclass
class Reply:
    text: str
    escalation: str | None = None  # reason, when a human should take over


async def answer(
    agent: Agent[SupportDeps, str],
    question: str,
    memory: ThreadMemory | None = None,
    thread_ts: str | None = None,
) -> Reply:
    """Run under usage limits with the thread's history; emit a RunRecord."""
    deps = SupportDeps()
    history = memory.history(thread_ts) if memory is not None and thread_ts else []
    with track_run("sb-support", caller="slack", model=model_name(agent.model)) as run:
        try:
            result = await agent.run(
                question, deps=deps, message_history=history or None, usage_limits=USAGE_LIMITS
            )
        except UsageLimitExceeded:
            log.warning("usage limit hit answering %r", question[:80])
            raise
        finally:
            run.routed_to = deps.routed_to
        run.record_usage(result.usage)
        reason = deps.escalation_reason
        run.escalated = reason is not None
    if memory is not None and thread_ts:
        memory.add(thread_ts, result.new_messages())
    return Reply(result.output, reason)


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


async def handle_mention(
    event: dict[str, Any],
    client: Any,
    agent: Agent[SupportDeps, str],
    memory: ThreadMemory | None = None,
) -> None:
    """Answer a mention (or a follow-up) in its thread, with 👀 while working and ✅/❌/🙋 after."""
    channel, ts = event["channel"], event["ts"]
    thread_ts = event.get("thread_ts", ts)
    question = strip_mentions(event.get("text", ""))

    await _react(client, channel, ts, WORKING)
    # Root span for the whole Slack interaction: the agent run, the A2A hops and the
    # specialists' spans all hang off it.
    with _tracer.start_as_current_span(
        "slack app_mention",
        kind=trace.SpanKind.SERVER,
        attributes={"slack.channel": channel, "slack.ts": ts, "slack.thread_ts": thread_ts},
    ):
        await _reply(client, agent, memory, channel, ts, thread_ts, question)


async def _reply(
    client: Any,
    agent: Agent[SupportDeps, str],
    memory: ThreadMemory | None,
    channel: str,
    ts: str,
    thread_ts: str,
    question: str,
) -> None:
    try:
        if not question:
            reply = Reply("Hi! Mention me with a question and I'll answer in this thread.")
        else:
            reply = await answer(agent, question, memory, thread_ts)
        text = reply.text + (escalation_text(reply.escalation) if reply.escalation else "")
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
        outcome = ESCALATED if reply.escalation else DONE
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


def is_follow_up(event: dict[str, Any], memory: ThreadMemory, bot_user_id: str | None) -> bool:
    """A human reply, without a mention of us, in a thread we've already answered.

    Messages that mention the bot arrive as app_mention too; answering both would
    reply twice.
    """
    thread_ts = event.get("thread_ts")
    return bool(
        thread_ts
        and thread_ts in memory
        and not event.get("bot_id")
        and not event.get("subtype")
        and (bot_user_id is None or f"<@{bot_user_id}>" not in event.get("text", ""))
    )


def build_app(
    agent: Agent[SupportDeps, str] | None = None, memory: ThreadMemory | None = None
) -> AsyncApp:
    agent = agent or build_agent()
    memory = memory or ThreadMemory()
    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])

    @app.event("app_mention")
    async def on_mention(event: dict[str, Any], client: Any) -> None:
        await handle_mention(event, client, agent, memory)

    @app.event("message")
    async def on_message(event: dict[str, Any], client: Any, context: Any) -> None:
        if is_follow_up(event, memory, context.get("bot_user_id")):
            await handle_mention(event, client, agent, memory)

    return app


async def _run() -> None:
    specialists = await discover()
    log.info("specialists: %s", [s.name for s in specialists] or "none")
    handler = AsyncSocketModeHandler(
        build_app(build_agent(specialists=specialists)), os.environ["SLACK_APP_TOKEN"]
    )
    await handler.start_async()


def main() -> None:
    setup_telemetry("sb-support")
    required = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ANTHROPIC_API_KEY")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"missing env: {', '.join(missing)}. Run via: {RUN_HINT}")
    asyncio.run(_run())
