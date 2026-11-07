"""The two scheduled runs: Tuesday's suggestion and Thursday's reminder (SB-1545).

    suggest: week picked? note + stop. Else candidates → top 3 → drafts → post.
    remind:  week picked? nothing. Tuesday's post missing? post fresh suggestions.
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
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from pydantic_ai import Agent

from motw_agent.candidates import MotwCandidate
from motw_agent.mt import MtError, MtReader
from motw_agent.slack_post import (
    TOP_N,
    find_suggestion,
    note_message,
    suggestion_message,
    week_label,
)
from motw_agent.writer import Draft, write
from sb_obs import track_run

log = logging.getLogger("motw_agent")

SuggestOutcome = Literal["posted", "already_picked", "none", "error"]
RemindOutcome = Literal["picked", "skipped", "reminded", SuggestOutcome]


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

    async def post(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)
        if self.dry_run or self.client is None:
            json.dump(kwargs, sys.stdout, indent=1, default=str)
            sys.stdout.write("\n")
            return
        await self.client.chat_postMessage(channel=self.channel, **kwargs)

    async def find_suggestion(self, week: date) -> dict[str, Any] | None:
        if self.client is None:
            return None
        return await find_suggestion(self.client, self.channel, week)


def _pick_label(pick: dict[str, Any]) -> str:
    m = pick.get("match") or {}
    home, away = m.get("home_team_name"), m.get("away_team_name")
    return f"{home} vs {away}" if home and away else f"match {m.get('id', '?')}"


async def suggest(
    week: date,
    mt: MtReader,
    writer: Agent[MotwCandidate, Draft],
    out: Out,
    *,
    caller: str = "cron",
) -> SuggestOutcome:
    label = week_label(week)
    try:
        pick = await mt.current_pick(week)
    except MtError:
        log.exception("reading the week's pick failed")
        await out.post(**note_message(f"MOTW: couldn't check the week of {label}. It's logged."))
        return "error"
    if pick:
        await out.post(
            **note_message(
                f"MOTW: the week of {label} is already picked ({_pick_label(pick)}). "
                "Leaving it alone."
            )
        )
        return "already_picked"

    try:
        result = await mt.candidates(week)
    except MtError:
        log.exception("suggest_motw_candidates failed")
        result = None
    if result is None or result.error is not None or result.candidates is None:
        if result is not None and result.error is not None:
            log.error("suggest_motw_candidates error: %s", result.error.model_dump())
        await out.post(
            **note_message(f"MOTW: couldn't get candidates for the week of {label}. It's logged.")
        )
        return "error"
    if result.current_pick is not None:  # picked between the two reads
        await out.post(
            **note_message(f"MOTW: the week of {label} is already picked. Leaving it alone.")
        )
        return "already_picked"
    if not result.candidates:
        why = f" ({result.reason})" if result.reason else ""
        await out.post(**note_message(f"MOTW: no eligible fixtures for the week of {label}{why}."))
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
    await out.post(**suggestion_message(week, result, list(zip(top, written, strict=True))))
    return "posted"


async def remind(
    week: date,
    mt: MtReader,
    writer: Agent[MotwCandidate, Draft],
    out: Out,
    approvers: list[str],
) -> RemindOutcome:
    try:
        pick = await mt.current_pick(week)
    except MtError:
        log.exception("reading the week's pick failed; not reminding")
        return "error"
    if pick:
        return "picked"
    post = await out.find_suggestion(week)
    if post is None:
        log.warning("no suggestion found for %s; posting fresh suggestions", week)
        return await suggest(week, mt, writer, out, caller="cron-remind")
    payload = (post.get("metadata") or {}).get("event_payload") or {}
    if payload.get("status") == "skipped":
        return "skipped"
    who = " ".join(f"<@{a}>" for a in approvers)
    await out.post(
        thread_ts=post["ts"],
        text=(
            f"{who} Reminder: no Match of the Week picked for the week of {week_label(week)} yet. "
            "Use the buttons above, or reply `pick N` here."
        ).strip(),
    )
    return "reminded"
