"""Channel-neutral support core: an `InboundMessage` in, a `Reply` out.

Everything a channel shares lives here: routing (the agent), conversation memory,
the escalation decision, the RunRecord and the root span. Nothing here knows about
Slack, Telegram or the MT app; a channel adapter turns its events into
`InboundMessage`s and renders `Reply`s (see docs/notes/sb-support-routing.md).
A test asserts this module imports no channel SDK.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from opentelemetry import trace
from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessage

from sb_obs import track_run
from sb_support.agent import USAGE_LIMITS, SupportDeps
from sb_support.prefs import PreferenceStore
from sb_support.tables import Table

log = logging.getLogger("sb_support")
_tracer = trace.get_tracer("sb_support")

Outcome = Literal["answered", "escalated", "limit", "error", "help"]

HELP_TEXT = "Hi! Ask me a question about an SB project and I'll answer here."
LIMIT_TEXT = "That one needed more work than I'm allowed per question. Try narrowing it down."
ERROR_TEXT = "Sorry, something went wrong answering that. It's been logged."


@dataclass(frozen=True)
class InboundMessage:
    """One user message, normalized by a channel adapter."""

    channel: str  # "slack", "telegram", "mt-app"; also the RunRecord's `caller`
    conversation_id: str  # channel-scoped: a Slack thread, a Telegram chat, an MT conversation
    text: str  # what the user asked, channel markup (e.g. mentions) already removed
    user_id: str | None = None
    message_id: str | None = None
    # The user's delegated MT token (mt-app, SB-1529). Forwarded only to specialists that
    # declare the identity extension; never logged, never in a span, not in repr.
    credential: str | None = field(default=None, repr=False)
    # Extra span attributes from the adapter, e.g. {"slack.ts": "..."}. Not sent to the model.
    attributes: Mapping[str, str] = field(default_factory=dict)

    @property
    def conversation_key(self) -> str:
        return f"{self.channel}:{self.conversation_id}"


@dataclass
class Reply:
    """What to tell the user. The adapter decides how it looks in its channel."""

    text: str
    outcome: Outcome = "answered"
    escalation: str | None = None  # reason a human should take over
    routed_to: list[str] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)  # from the specialist's data part


class ConversationMemory:
    """Message history per conversation, in memory (lost on restart).

    Trimmed by whole runs, never by message, so a tool call is never separated from
    its result (the API rejects that). Bounded in conversations and runs per conversation.
    """

    def __init__(self, max_conversations: int = 200, max_runs: int = 6) -> None:
        self._runs: OrderedDict[str, list[list[ModelMessage]]] = OrderedDict()
        self.max_conversations, self.max_runs = max_conversations, max_runs

    def __contains__(self, key: str) -> bool:
        return key in self._runs

    def history(self, key: str) -> list[ModelMessage]:
        return [m for run in self._runs.get(key, []) for m in run]

    def add(self, key: str, messages: list[ModelMessage]) -> None:
        runs = self._runs.setdefault(key, [])
        runs.append(messages)
        del runs[: -self.max_runs]
        self._runs.move_to_end(key)
        while len(self._runs) > self.max_conversations:
            self._runs.popitem(last=False)


async def answer(
    agent: Agent[SupportDeps, str],
    question: str,
    memory: ConversationMemory | None = None,
    key: str | None = None,
    caller: str = "direct",
    user_id: str | None = None,
    prefs: PreferenceStore | None = None,
    credential: str | None = None,
) -> Reply:
    """Run under usage limits with the conversation's history; emit a RunRecord.

    Raises `UsageLimitExceeded` and any agent error; `SupportCore.handle` maps them.
    """
    deps = SupportDeps(
        conversation_key=key,
        channel=caller,
        user_id=user_id,
        prefs=prefs,
        credential=credential,
    )
    history = memory.history(key) if memory is not None and key else []
    with track_run("sb-support", caller=caller, model=agent.model) as run:
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
    if memory is not None and key:
        memory.add(key, result.new_messages())
    return Reply(
        result.output,
        "escalated" if reason else "answered",
        escalation=reason,
        routed_to=deps.routed_to,
        tables=deps.tables,
    )


class SupportCore:
    """The front door every channel adapter calls."""

    def __init__(
        self,
        agent: Agent[SupportDeps, str],
        memory: ConversationMemory | None = None,
        prefs: PreferenceStore | None = None,
    ) -> None:
        self.agent = agent
        self.memory = memory if memory is not None else ConversationMemory()
        self.prefs = prefs if prefs is not None else PreferenceStore()

    def knows(self, channel: str, conversation_id: str) -> bool:
        """True once we've answered in this conversation (adapters use it for follow-ups)."""
        return f"{channel}:{conversation_id}" in self.memory

    async def handle(self, msg: InboundMessage) -> Reply:
        """Answer one message. Never raises: failures come back as an `error`/`limit` Reply."""
        # Root span for the whole interaction: the agent run, the A2A hops and the
        # specialists' spans all hang off it.
        attributes = {
            "messaging.system": msg.channel,
            "sb.conversation_id": msg.conversation_id,
            **dict(msg.attributes),
        }
        if msg.message_id:
            attributes["messaging.message.id"] = msg.message_id
        with _tracer.start_as_current_span(
            f"support message {msg.channel}", kind=trace.SpanKind.SERVER, attributes=attributes
        ) as span:
            if not msg.text.strip():
                reply = Reply(HELP_TEXT, "help")
            else:
                try:
                    reply = await answer(
                        self.agent,
                        msg.text,
                        self.memory,
                        msg.conversation_key,
                        msg.channel,
                        user_id=msg.user_id,
                        prefs=self.prefs,
                        credential=msg.credential,
                    )
                except UsageLimitExceeded:
                    reply = Reply(LIMIT_TEXT, "limit")
                except Exception:
                    log.exception(
                        "failed to answer %s message in %s", msg.channel, msg.conversation_id
                    )
                    reply = Reply(ERROR_TEXT, "error")
            span.set_attribute("sb.outcome", reply.outcome)
        return reply
