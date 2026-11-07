"""The suggestion message: Block Kit for people, message metadata for the approval step.

The metadata is the contract with sb-support's approval handler (SB-1546), which reads
it back instead of keeping state. Change it in both places (and bump `v`):

    metadata.event_type = "motw_suggestion"
    metadata.event_payload = {
      "v": 1, "week_start": "YYYY-MM-DD", "status": "open" | "picked" | "skipped",
      "candidates": [{"n": 1, "match_id": 4096, "label": "A vs B", "table": "U15 ...",
                      "blurb": "...", "reasons": [{"text": "...", "kind": "standings"}]}],
    }

Block ids sb-support relies on: `motw_draft_<n>` (one candidate's blurb + reasons, so an
edit replaces just that block) and `motw_actions` (the buttons, replaced by a status
line once someone picks or skips). Button action ids: `motw_pick_<n>`, `motw_skip`.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from motw_agent.candidates import CandidatesResult, MotwCandidate
from motw_agent.mt import TZ
from motw_agent.writer import Draft, Written

EVENT_TYPE = "motw_suggestion"
CONTRACT_VERSION = 1
TOP_N = 3

FLAG_LABELS = {
    "low_sample": "low sample",
    "adjacent": "neighbours in the table",
    "top_of_table": "top of the table",
    "qualifying_line": "on the qualifying line",
    "recently_featured": "recently featured",
}
REPLY_HELP = (
    "Approvers: click a button, or reply in this thread: `pick 2`, "
    "`pick 2 blurb: <text>`, `edit 2` then `reason 1: <text>` / `drop reason 3` / "
    "`blurb: <text>` on the next lines, or `skip`."
)


def week_label(week: date) -> str:
    return week.strftime("%b %-d")


def draft_text(n: int, blurb: str | None, reasons: list[dict[str, Any]], fallback: bool) -> str:
    """One candidate's blurb + numbered reasons (sb-support renders edits the same way)."""
    lines = [f"*#{n} pitch:* {blurb}" if blurb else f"*#{n} pitch:* _no blurb_"]
    lines += [f"{i}. {r['text']}  _({r['kind']})_" for i, r in enumerate(reasons, 1)]
    if fallback:
        lines.append("_Plain facts: the written draft failed its checks._")
    return "\n".join(lines)


def _when(c: MotwCandidate) -> str:
    when = c.match_date.strftime("%a %b %-d")
    return f"{when}, {c.scheduled_kickoff}" if c.scheduled_kickoff else when


def _team_note(t: Any) -> str:
    if t.position is None or not t.table_size:
        return f"{t.name} (not in the table yet)"
    return f"{t.name} ({t.position}/{t.table_size})"


def candidate_blocks(n: int, c: MotwCandidate, w: Written) -> list[dict[str, Any]]:
    flags = [FLAG_LABELS.get(f, f) for f in c.flags]
    head = (
        f"*#{n}  {_team_note(c.home)} vs {_team_note(c.away)}*\n"
        f"{c.table} · {c.competition} · {_when(c)} · score {c.score.total:.2f}"
        + (f" · {', '.join(flags)}" if flags else "")
    )
    low = [t for t in (c.home, c.away) if t.low_sample]
    if low:
        who = ", ".join(f"{t.name} ({t.played} games)" for t in low)
        head += f"\n:warning: _Low sample: {who}. Their numbers are early-season._"
    reasons = [r.model_dump(include={"text", "kind"}) for r in w.draft.reasons]
    return [
        {"type": "section", "block_id": f"motw_cand_{n}", "text": {"type": "mrkdwn", "text": head}},
        {
            "type": "section",
            "block_id": f"motw_draft_{n}",
            "text": {"type": "mrkdwn", "text": draft_text(n, w.draft.blurb, reasons, w.fallback)},
        },
        {"type": "divider"},
    ]


def actions_block(count: int) -> dict[str, Any]:
    buttons: list[dict[str, Any]] = [
        {
            "type": "button",
            "action_id": f"motw_pick_{n}",
            "text": {"type": "plain_text", "text": f"Pick #{n}"},
            "value": str(n),
        }
        | ({"style": "primary"} if n == 1 else {})
        for n in range(1, count + 1)
    ]
    buttons.append(
        {
            "type": "button",
            "action_id": "motw_skip",
            "text": {"type": "plain_text", "text": "Skip"},
            "value": "skip",
        }
    )
    return {"type": "actions", "block_id": "motw_actions", "elements": buttons}


def metadata(week: date, picks: list[tuple[MotwCandidate, Draft]]) -> dict[str, Any]:
    return {
        "event_type": EVENT_TYPE,
        "event_payload": {
            "v": CONTRACT_VERSION,
            "week_start": week.isoformat(),
            "status": "open",
            "candidates": [
                {
                    "n": n,
                    "match_id": c.match_id,
                    "label": c.label,
                    "table": c.table,
                    "blurb": d.blurb,
                    "reasons": [r.model_dump(include={"text", "kind"}) for r in d.reasons],
                }
                for n, (c, d) in enumerate(picks, 1)
            ],
        },
    }


def suggestion_message(
    week: date, result: CandidatesResult, written: list[tuple[MotwCandidate, Written]]
) -> dict[str, Any]:
    """kwargs for chat.postMessage (without `channel`)."""
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Match of the Week: week of {week_label(week)}",
            },
        }
    ]
    cov = result.coverage
    if cov:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"Top {len(written)} of {cov.eligible} eligible fixtures "
                            f"({cov.fixtures_in_week} this week across {cov.divisions} divisions)."
                        ),
                    }
                ],
            }
        )
    for n, (c, w) in enumerate(written, 1):
        blocks += candidate_blocks(n, c, w)
    blocks.append(actions_block(len(written)))
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": REPLY_HELP}]})
    text = f"MOTW candidates for the week of {week_label(week)}: " + "; ".join(
        f"#{n} {c.label}" for n, (c, _) in enumerate(written, 1)
    )
    return {
        "text": text,
        "blocks": blocks,
        "metadata": metadata(week, [(c, w.draft) for c, w in written]),
    }


def note_message(text: str) -> dict[str, Any]:
    return {"text": text}


def week_oldest_ts(week: date) -> str:
    """Slack `oldest` for the week's Monday 00:00 Eastern."""
    return f"{datetime.combine(week, time(), tzinfo=TZ).timestamp():.6f}"


async def find_suggestion(client: Any, channel: str, week: date) -> dict[str, Any] | None:
    """This week's suggestion message (newest), found by its metadata."""
    cursor = None
    found: list[dict[str, Any]] = []
    for _ in range(5):  # 5 x 200 messages: far more than a picks channel sees in a week
        resp = await client.conversations_history(
            channel=channel,
            oldest=week_oldest_ts(week),
            include_all_metadata=True,
            limit=200,
            cursor=cursor,
        )
        for msg in resp.get("messages", []):
            meta = msg.get("metadata") or {}
            payload = meta.get("event_payload") or {}
            if (
                meta.get("event_type") == EVENT_TYPE
                and payload.get("week_start") == week.isoformat()
            ):
                found.append(msg)
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return max(found, key=lambda m: float(m["ts"])) if found else None
