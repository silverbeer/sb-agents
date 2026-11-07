"""Slack side of MOTW approval (SB-1546): buttons and thread replies on a suggestion.

    [Pick #N] / `pick N ...` ─► approver? ─► GET /api/motw (week)
        ├─ unpicked ─► POST pick
        └─ picked ───► "overwrite?" in thread
    [Overwrite] (motw_overwrite_N) ─► POST pick          [Keep it] (motw_overwrite_cancel)
    [Skip] / `skip` ─► mark skipped (no MT write)
    `edit N ...` ─► update the draft in the message (no MT write)

Stateless: everything lives in the suggestion message's metadata, read back with
`conversations.history(include_all_metadata=true)`. A pick waiting for an overwrite
confirmation is stored there too (`pending`). No reply here ever reaches the model.
User-facing errors are generic; specifics go to the log.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from sb_support.motw import (
    HELP,
    Candidate,
    Command,
    GrammarError,
    MotwApi,
    MotwApiError,
    Reason,
    Suggestion,
    apply_edits,
    draft_text,
    parse,
    pick_label,
)

log = logging.getLogger("sb_support.slack_motw")

PICK_ACTION = re.compile(r"^motw_pick_(\d+)$")
OVERWRITE_ACTION = re.compile(r"^motw_overwrite_(\d+)$")
SKIP_ACTION = "motw_skip"
CANCEL_ACTION = "motw_overwrite_cancel"
MENTION_RE = re.compile(r"<@[A-Z0-9]+>")

NOT_ALLOWED = "Sorry, only Match of the Week approvers can do that."
SAVE_FAILED = "Couldn't save the pick to MissingTable. It's been logged; try again in a bit."
CHECK_FAILED = "Couldn't check this week's pick in MissingTable. It's been logged; try again."
GONE = "Couldn't find that suggestion any more."


@dataclass
class MotwConfig:
    channel: str | None = None  # #mt-admin-picks; thread replies are only read there
    approvers: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_env(cls) -> MotwConfig:
        approvers = os.environ.get("MOTW_APPROVERS", "")
        return cls(
            channel=os.environ.get("MOTW_SLACK_CHANNEL", "").strip() or None,
            approvers=frozenset(a.strip() for a in approvers.split(",") if a.strip()),
        )


class MotwSlack:
    def __init__(self, config: MotwConfig, api: MotwApi) -> None:
        self.config, self.api = config, api
        if not config.approvers:
            log.warning("MOTW_APPROVERS is empty: nobody can approve MOTW picks")

    def allowed(self, user: str | None) -> bool:
        return bool(user) and user in self.config.approvers

    # --- reading the suggestion -------------------------------------------------------

    async def fetch(
        self, client: Any, channel: str, ts: str
    ) -> tuple[dict[str, Any], Suggestion] | None:
        """The top-level message at `ts` and its suggestion, if it is one."""
        resp = await client.conversations_history(
            channel=channel,
            latest=ts,
            oldest=ts,
            inclusive=True,
            limit=1,
            include_all_metadata=True,
        )
        for msg in resp.get("messages", []):
            if msg.get("ts") == ts:
                s = Suggestion.from_metadata(msg.get("metadata"))
                return (msg, s) if s else None
        return None

    # --- message updates --------------------------------------------------------------

    @staticmethod
    def _blocks(msg: dict[str, Any], s: Suggestion, status: str | None) -> list[dict[str, Any]]:
        """The message's blocks with drafts from `s`; buttons replaced by `status` if given."""
        out: list[dict[str, Any]] = []
        for b in msg.get("blocks") or []:
            bid = b.get("block_id", "")
            if bid.startswith("motw_draft_"):
                c = s.candidate(int(bid.rsplit("_", 1)[1]))
                if c is not None:
                    b = {
                        "type": "section",
                        "block_id": bid,
                        "text": {"type": "mrkdwn", "text": draft_text(c)},
                    }
            elif bid in ("motw_actions", "motw_status") and status is not None:
                b = {
                    "type": "context",
                    "block_id": "motw_status",
                    "elements": [{"type": "mrkdwn", "text": status}],
                }
            out.append(b)
        return out

    async def _update(
        self,
        client: Any,
        channel: str,
        msg: dict[str, Any],
        s: Suggestion,
        status: str | None = None,
    ) -> None:
        await client.chat_update(
            channel=channel,
            ts=msg["ts"],
            text=msg.get("text") or "MOTW suggestion",
            blocks=self._blocks(msg, s, status),
            metadata=s.to_metadata(),
        )

    async def _tell(self, client: Any, channel: str, user: str, text: str, thread_ts: str | None):
        try:
            await client.chat_postEphemeral(
                channel=channel,
                user=user,
                text=text,
                **({"thread_ts": thread_ts} if thread_ts else {}),
            )
        except Exception:  # noqa: BLE001 - an ephemeral is a courtesy
            log.warning("ephemeral to %s failed", user, exc_info=True)

    # --- the actions ------------------------------------------------------------------

    async def pick(
        self,
        client: Any,
        channel: str,
        msg: dict[str, Any],
        s: Suggestion,
        c: Candidate,
        user: str,
    ) -> str:
        """Check the week, then write or ask to overwrite. Returns a user-facing outcome."""
        try:
            existing = await self.api.current_pick(s.week_start)
        except MotwApiError:
            log.exception("MOTW week check failed for %s", s.week_start)
            return CHECK_FAILED
        if existing:
            pending = {
                "n": c.n,
                "blurb": c.blurb,
                "by": user,
                "reasons": [r.as_dict() for r in c.reasons],
            }
            s2 = Suggestion(s.week_start, s.candidates, s.status, {**s.extra, "pending": pending})
            await self._update(client, channel, msg, s2)
            await client.chat_postMessage(
                channel=channel,
                thread_ts=msg["ts"],
                text=f"This week already has {pick_label(existing)}. Overwrite with #{c.n}?",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"<@{user}> the week of {s.week_start} already has a "
                                "Match of the Week: "
                                f"*{pick_label(existing)}*. Overwrite it with #{c.n} ({c.label})?"
                            ),
                        },
                    },
                    {
                        "type": "actions",
                        "block_id": "motw_overwrite",
                        "elements": [
                            {
                                "type": "button",
                                "action_id": f"motw_overwrite_{c.n}",
                                "style": "danger",
                                "text": {"type": "plain_text", "text": f"Overwrite with #{c.n}"},
                                "value": msg["ts"],
                            },
                            {
                                "type": "button",
                                "action_id": "motw_overwrite_cancel",
                                "text": {"type": "plain_text", "text": "Keep it"},
                                "value": msg["ts"],
                            },
                        ],
                    },
                ],
            )
            return "confirm"
        return await self.write(client, channel, msg, s, c, user)

    async def write(
        self,
        client: Any,
        channel: str,
        msg: dict[str, Any],
        s: Suggestion,
        c: Candidate,
        user: str,
    ) -> str:
        try:
            await self.api.set_pick(c)
        except MotwApiError:
            log.exception("MOTW write failed (match %s, week %s)", c.match_id, s.week_start)
            return SAVE_FAILED
        extra = {k: v for k, v in s.extra.items() if k != "pending"}
        s2 = Suggestion(
            s.week_start, s.candidates, "picked", {**extra, "picked": c.n, "by": user}
        ).with_candidate(c)
        log.info("MOTW picked", extra={"week": s.week_start, "match_id": c.match_id, "by": user})
        await self._update(client, channel, msg, s2, f"✅ Picked #{c.n} ({c.label}) by <@{user}>")
        return "picked"

    async def skip(
        self, client: Any, channel: str, msg: dict[str, Any], s: Suggestion, user: str
    ) -> str:
        extra = {k: v for k, v in s.extra.items() if k != "pending"}
        s2 = Suggestion(s.week_start, s.candidates, "skipped", {**extra, "by": user})
        await self._update(client, channel, msg, s2, f"⏭️ Skipped by <@{user}>")
        return "skipped"

    # --- entry points: buttons --------------------------------------------------------

    async def on_action(self, body: dict[str, Any], client: Any) -> None:
        """Every `motw_*` button. Bolt has already acked."""
        action = (body.get("actions") or [{}])[0]
        action_id = action.get("action_id", "")
        user = (body.get("user") or {}).get("id", "")
        channel = (body.get("channel") or {}).get("id", "")
        container = body.get("container") or {}
        if not self.allowed(user):
            await self._tell(client, channel, user, NOT_ALLOWED, container.get("thread_ts"))
            return
        # Suggestion buttons sit on the suggestion; overwrite buttons carry its ts.
        on_confirm = action_id == CANCEL_ACTION or OVERWRITE_ACTION.match(action_id)
        ts = action.get("value") if on_confirm else container.get("message_ts")
        found = await self.fetch(client, channel, ts) if ts else None
        if found is None:
            await self._tell(client, channel, user, GONE, None)
            return
        msg, s = found

        if action_id == SKIP_ACTION:
            await self.skip(client, channel, msg, s, user)
        elif m := PICK_ACTION.match(action_id):
            c = s.candidate(int(m.group(1)))
            outcome = await self.pick(client, channel, msg, s, c, user) if c else GONE
            if outcome not in ("picked", "confirm"):
                await self._tell(client, channel, user, outcome, None)
        elif on_confirm:
            await self._confirm(client, channel, container, msg, s, action_id, user)

    async def _confirm(
        self,
        client: Any,
        channel: str,
        container: dict[str, Any],
        msg: dict[str, Any],
        s: Suggestion,
        action_id: str,
        user: str,
    ) -> None:
        confirm_ts = container.get("message_ts")
        pending = s.extra.get("pending") or {}
        m = OVERWRITE_ACTION.match(action_id)
        if m and pending.get("n") == int(m.group(1)) and s.candidate(pending["n"]):
            base = s.candidate(pending["n"])
            assert base is not None
            c = Candidate(
                base.n,
                base.match_id,
                base.label,
                base.table,
                pending.get("blurb"),
                tuple(Reason(r["text"], r["kind"]) for r in pending.get("reasons") or []),
            )
            outcome = await self.write(client, channel, msg, s, c, user)
            done = f"Overwritten with #{c.n} by <@{user}>." if outcome == "picked" else outcome
        elif m:
            done = "That confirmation is out of date; pick again."
        else:
            extra = {k: v for k, v in s.extra.items() if k != "pending"}
            await self._update(
                client, channel, msg, Suggestion(s.week_start, s.candidates, s.status, extra)
            )
            done = f"Kept the existing pick (<@{user}>)."
        if confirm_ts:
            await client.chat_update(
                channel=channel,
                ts=confirm_ts,
                text=done,
                blocks=[{"type": "context", "elements": [{"type": "mrkdwn", "text": done}]}],
            )

    # --- entry points: thread replies -------------------------------------------------

    def maybe_ours(self, event: dict[str, Any]) -> bool:
        """Cheap pre-check before any Slack call: a human thread reply in the picks channel."""
        return bool(
            self.config.channel
            and event.get("channel") == self.config.channel
            and event.get("thread_ts")
            and event.get("thread_ts") != event.get("ts")
            and not event.get("bot_id")
            and not event.get("subtype")
        )

    async def on_thread_reply(self, event: dict[str, Any], client: Any) -> bool:
        """Handle a reply in a suggestion thread. True = it was ours: don't route it on."""
        if not self.maybe_ours(event):
            return False
        channel, thread_ts, user = event["channel"], event["thread_ts"], event.get("user", "")
        found = await self.fetch(client, channel, thread_ts)
        if found is None:
            return False  # some other thread in the channel
        msg, s = found
        text = MENTION_RE.sub("", event.get("text", "")).strip()
        try:
            cmd = parse(text)
        except GrammarError as exc:
            if self.allowed(user):
                await self._tell(client, channel, user, str(exc), thread_ts)
            return True
        if cmd is None:
            return True  # chatter in a suggestion thread: never to the model
        if not self.allowed(user):
            await self._tell(client, channel, user, NOT_ALLOWED, thread_ts)
            return True
        reply = await self._run(client, channel, msg, s, cmd, user)
        if reply:
            await self._tell(client, channel, user, reply, thread_ts)
        else:
            await _react(client, channel, event["ts"], "white_check_mark")
        return True

    async def _run(
        self,
        client: Any,
        channel: str,
        msg: dict[str, Any],
        s: Suggestion,
        cmd: Command,
        user: str,
    ) -> str | None:
        """Do the command; a message for the user, or None when the update says it all."""
        if cmd.action == "skip":
            await self.skip(client, channel, msg, s, user)
            return None
        c = s.candidate(cmd.n or 0)
        if c is None:
            return f"There's no #{cmd.n}: pick 1-{len(s.candidates)}."
        try:
            c = apply_edits(c, cmd.edits)
        except GrammarError as exc:
            return str(exc)
        if cmd.action == "edit":
            await self._update(client, channel, msg, s.with_candidate(c))
            return None
        outcome = await self.pick(client, channel, msg, s, c, user)
        return None if outcome in ("picked", "confirm") else outcome


async def _react(client: Any, channel: str, ts: str, name: str) -> None:
    try:
        await client.reactions_add(channel=channel, timestamp=ts, name=name)
    except Exception:  # noqa: BLE001 - cosmetic
        log.warning("reaction %s failed", name, exc_info=True)


def register(app: Any, motw: MotwSlack) -> None:
    """Bolt handlers for every MOTW button."""

    async def handler(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        try:
            await motw.on_action(body, client)
        except Exception:
            log.exception("MOTW action failed")

    for action in (PICK_ACTION, OVERWRITE_ACTION, SKIP_ACTION, CANCEL_ACTION):
        app.action(action)(handler)


__all__ = ["HELP", "MotwConfig", "MotwSlack", "register"]
