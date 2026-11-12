"""The two scheduled runs: Tuesday's suggestion and Thursday's reminder (SB-1545).

One Match of the Week per slot (pool + age group, SB-1640), so each run goes slot by
slot, one Slack message per slot:

    suggest: slot picked? note + stop. Else candidates → top 3 → drafts → post.
    remind:  slot picked? nothing. Tuesday's post missing? post fresh suggestions.
             skipped? nothing. Else reply in its thread, @mentioning the approvers.

Never writes to MT. The pick itself is made by an approver in Slack and written by
sb-support (SB-1546).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from pydantic_ai import Agent

from motw_agent.candidates import MotwCandidate
from motw_agent.mt import SLOTS, MtError, MtReader, Slot
from motw_agent.slack_post import (
    TOP_N,
    find_suggestion,
    note_message,
    suggestion_message,
    week_label,
)
from motw_agent.writer import Draft, Written, write
from sb_evals import guarded
from sb_obs import model_name, track_run

log = logging.getLogger("motw_agent")

SuggestOutcome = Literal["posted", "already_picked", "none", "error"]
RemindOutcome = Literal["picked", "skipped", "reminded", SuggestOutcome]
# Scores the drafts just posted, given them and the writer's model name; returns the thread line.
PostedEval = Callable[[list[tuple[MotwCandidate, Written]], str], Awaitable[str]]
# Well inside the Job's 600s deadline: a slow judge must not get the Job killed.
EVAL_TIMEOUT_S = 180
EVAL_REPLY_TIMEOUT_S = 30
EVAL_DID_NOT_COMPLETE = "Eval: didn't complete. It's logged."


@dataclass
class Config:
    channel: str  # #mt-admin-picks channel id (MOTW_SLACK_CHANNEL)
    approvers: list[str] = field(default_factory=list)  # Slack user ids (MOTW_APPROVERS)

    @classmethod
    def from_env(cls) -> Config:
        approvers = os.environ.get("MOTW_APPROVERS", "")
        return cls(
            channel=os.environ.get("MOTW_SLACK_CHANNEL", "").strip(),
            approvers=[a.strip() for a in approvers.split(",") if a.strip()],
        )


class Out:
    """Where messages go: Slack, or stdout for --dry-run. Reads always use Slack if given."""

    def __init__(self, client: Any | None, channel: str, dry_run: bool = False) -> None:
        self.client, self.channel, self.dry_run = client, channel, dry_run
        self.sent: list[dict[str, Any]] = []

    async def post(self, **kwargs: Any) -> str | None:
        """Send one message; returns its Slack ts, or None when nothing was really posted."""
        self.sent.append(kwargs)
        if self.dry_run or self.client is None:
            json.dump(kwargs, sys.stdout, indent=1, default=str)
            sys.stdout.write("\n")
            return None
        response = await self.client.chat_postMessage(channel=self.channel, **kwargs)
        return response["ts"]

    async def find_suggestion(self, week: date, slot: Slot | None = None) -> dict[str, Any] | None:
        if self.client is None:
            return None
        return await find_suggestion(self.client, self.channel, week, slot)


def _pick_label(pick: dict[str, Any]) -> str:
    m = pick.get("match") or {}
    home, away = m.get("home_team_name"), m.get("away_team_name")
    return f"{home} vs {away}" if home and away else f"match {m.get('id', '?')}"


async def _report_eval(
    evaluate: PostedEval,
    posted: list[tuple[MotwCandidate, Written]],
    model: str,
    out: Out,
    thread_ts: str,
) -> None:
    """Score the post and reply in its thread. The post is out: nothing here may fail the run."""
    line = await guarded(
        lambda: evaluate(posted, model),
        timeout_s=EVAL_TIMEOUT_S,
        fallback=EVAL_DID_NOT_COMPLETE,
        label="evaluating the posted drafts",
    )
    await guarded(
        lambda: out.post(thread_ts=thread_ts, text=line),
        timeout_s=EVAL_REPLY_TIMEOUT_S,
        fallback=None,
        label="posting the eval result",
    )


async def suggest(
    week: date,
    mt: MtReader,
    writer: Agent[MotwCandidate, Draft],
    out: Out,
    *,
    caller: str = "cron",
    evaluate: PostedEval | None = None,
    slot: Slot | None = None,
) -> SuggestOutcome:
    label = week_label(week)
    if slot is not None:
        label = f"{slot.label}, week of {label}"
    else:
        label = f"the week of {label}"
    try:
        pick = await mt.current_pick(week, slot)
    except MtError:
        log.exception("reading the week's pick failed")
        await out.post(**note_message(f"MOTW: couldn't check {label}. It's logged."))
        return "error"
    if pick:
        await out.post(
            **note_message(
                f"MOTW: {label} is already picked ({_pick_label(pick)}). Leaving it alone."
            )
        )
        return "already_picked"

    try:
        result = await mt.candidates(week, slot=slot)
    except MtError:
        log.exception("suggest_motw_candidates failed")
        result = None
    if result is None or result.error is not None or result.candidates is None:
        if result is not None and result.error is not None:
            log.error("suggest_motw_candidates error: %s", result.error.model_dump())
        await out.post(**note_message(f"MOTW: couldn't get candidates for {label}. It's logged."))
        return "error"
    if result.current_pick is not None:  # picked between the two reads
        await out.post(**note_message(f"MOTW: {label} is already picked. Leaving it alone."))
        return "already_picked"
    if not result.candidates:
        why = f" ({result.reason})" if result.reason else ""
        await out.post(**note_message(f"MOTW: no eligible fixtures for {label}{why}."))
        return "none"

    top = result.candidates[:TOP_N]
    with track_run("motw-agent", caller=caller, model=writer.model) as run:
        written = await asyncio.gather(*(write(writer, c) for c in top))
        for w in written:
            if w.usage is not None:
                run.record_usage(w.usage)
            if w.fallback:
                log.warning("draft fell back to plain facts: %s", w.problems)
        run.confidence = "low" if any(w.fallback for w in written) else "high"
    posted = list(zip(top, written, strict=True))
    ts = await out.post(**suggestion_message(week, result, posted, slot))
    if evaluate is not None and ts is not None:
        await _report_eval(evaluate, posted, model_name(writer.model), out, ts)
    return "posted"


async def remind(
    week: date,
    mt: MtReader,
    writer: Agent[MotwCandidate, Draft],
    out: Out,
    approvers: list[str],
    *,
    evaluate: PostedEval | None = None,
    slot: Slot | None = None,
) -> RemindOutcome:
    try:
        pick = await mt.current_pick(week, slot)
    except MtError:
        log.exception("reading the week's pick failed; not reminding")
        return "error"
    if pick:
        return "picked"
    post = await out.find_suggestion(week, slot)
    if post is None:
        log.warning("no suggestion found for %s %s; posting fresh suggestions", week, slot)
        return await suggest(
            week, mt, writer, out, caller="cron-remind", evaluate=evaluate, slot=slot
        )
    payload = (post.get("metadata") or {}).get("event_payload") or {}
    if payload.get("status") == "skipped":
        return "skipped"
    who = " ".join(f"<@{a}>" for a in approvers)
    what = f"{slot.label} Match of the Week" if slot else "Match of the Week"
    await out.post(
        thread_ts=post["ts"],
        text=(
            f"{who} Reminder: no {what} picked for the week of {week_label(week)} yet. "
            "Use the buttons above, or reply `pick N` here."
        ).strip(),
    )
    return "reminded"


def overall(outcomes: list[str]) -> str:
    """One outcome for a run over several slots: any error is an error (the Job fails)."""
    for o in ("error", "posted", "reminded"):
        if o in outcomes:
            return o
    return outcomes[0] if outcomes else "none"


async def suggest_week(
    week: date,
    mt: MtReader,
    writer: Agent[MotwCandidate, Draft],
    out: Out,
    *,
    slots: tuple[Slot, ...] = SLOTS,
    evaluate: PostedEval | None = None,
) -> dict[Slot, SuggestOutcome]:
    """Tuesday: one suggestion per slot, in order. A failed slot does not stop the rest."""
    return {
        slot: await suggest(week, mt, writer, out, evaluate=evaluate, slot=slot) for slot in slots
    }


async def remind_week(
    week: date,
    mt: MtReader,
    writer: Agent[MotwCandidate, Draft],
    out: Out,
    approvers: list[str],
    *,
    slots: tuple[Slot, ...] = SLOTS,
    evaluate: PostedEval | None = None,
) -> dict[Slot, RemindOutcome]:
    """Thursday: nudge each slot that is still unpicked and not skipped."""
    return {
        slot: await remind(week, mt, writer, out, approvers, evaluate=evaluate, slot=slot)
        for slot in slots
    }
