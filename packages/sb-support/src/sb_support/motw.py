"""Match of the Week approval (SB-1546): the suggestion contract, the reply grammar, MT writes.

Channel-neutral: no Slack types here (`slack_motw.py` is the Slack side). motw-agent
posts a suggestion whose message metadata is the `Suggestion` below (contract v1,
docs/notes/motw-agent.md); this module parses it, applies an approver's edits, and
writes the pick to MT.

Thread-reply grammar: deterministic, case-insensitive, never sent to a model.

    skip
    2 | #2 | pick 2 [blurb: <text>]            pick candidate 2 (with this blurb)
    edit 2 [<edit>]                             change candidate 2's draft, don't pick
    pick 2 / edit 2, then one <edit> per line:
      blurb: <text>                             replace the blurb
      reason K [<kind>]: <text>                 replace reason K (K = count + 1 appends)
      drop reason K                             remove reason K

Reason numbers refer to the list as shown before the message; replacements apply
first, then drops, then appends. A new reason's kind defaults to `storyline`.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

log = logging.getLogger("sb_support.motw")

EVENT_TYPE = "motw_suggestion"
CONTRACT_VERSION = 2  # v2 adds "slot": {"pool", "age_group"} (SB-1640)
READABLE_VERSIONS = frozenset({1, 2})  # v1: one suggestion for the whole week
KINDS = ("standings", "form", "stakes", "rivalry", "storyline")
DEFAULT_KIND = "storyline"
REASON_MAX, BLURB_MAX, MAX_REASONS = 200, 280, 6
DEFAULT_API_URL = "https://api.missingtable.com"

HELP = (
    "MOTW replies: `pick 2`, `pick 2 blurb: <text>`, `skip`, or `edit 2` followed by lines "
    "like `blurb: <text>`, `reason 1: <text>`, `reason 4 rivalry: <text>`, `drop reason 3`."
)


# --- the suggestion (message metadata) ------------------------------------------------


@dataclass(frozen=True)
class Reason:
    text: str
    kind: str

    def as_dict(self) -> dict[str, str]:
        return {"text": self.text, "kind": self.kind}


@dataclass(frozen=True)
class Candidate:
    n: int
    match_id: int
    label: str
    table: str
    blurb: str | None
    reasons: tuple[Reason, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "match_id": self.match_id,
            "label": self.label,
            "table": self.table,
            "blurb": self.blurb,
            "reasons": [r.as_dict() for r in self.reasons],
        }


@dataclass(frozen=True)
class Suggestion:
    week_start: str
    candidates: tuple[Candidate, ...]
    status: str = "open"  # open | picked | skipped
    extra: dict[str, Any] = field(default_factory=dict)  # picked, by, pending, ...

    @property
    def slot(self) -> dict[str, str] | None:
        """{"pool", "age_group"} this suggestion is for; None for a v1 whole-week one."""
        raw = self.extra.get("slot")
        return raw if isinstance(raw, dict) and raw.get("age_group") else None

    @property
    def slot_label(self) -> str:
        slot = self.slot
        if slot is None:
            return ""
        return (
            f"{slot['age_group']} Pro Pathway"
            if slot.get("pool") == "pathway"
            else slot["age_group"]
        )

    def candidate(self, n: int) -> Candidate | None:
        return next((c for c in self.candidates if c.n == n), None)

    def with_candidate(self, c: Candidate) -> Suggestion:
        return replace(self, candidates=tuple(c if x.n == c.n else x for x in self.candidates))

    def to_metadata(self) -> dict[str, Any]:
        payload = {
            **self.extra,
            "v": CONTRACT_VERSION,
            "week_start": self.week_start,
            "status": self.status,
            "candidates": [c.as_dict() for c in self.candidates],
        }
        return {"event_type": EVENT_TYPE, "event_payload": payload}

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any] | None) -> Suggestion | None:
        """The suggestion in a message's metadata, or None (not ours, or malformed)."""
        if not metadata or metadata.get("event_type") != EVENT_TYPE:
            return None
        p = metadata.get("event_payload") or {}
        try:
            if int(p.get("v", 0)) not in READABLE_VERSIONS:
                log.warning(
                    "motw suggestion contract v%s, expected v%s", p.get("v"), CONTRACT_VERSION
                )
                return None
            cands = tuple(
                Candidate(
                    n=int(c["n"]),
                    match_id=int(c["match_id"]),
                    label=str(c.get("label", "")),
                    table=str(c.get("table", "")),
                    blurb=c.get("blurb"),
                    reasons=tuple(
                        Reason(str(r["text"]), str(r.get("kind") or DEFAULT_KIND))
                        for r in c.get("reasons") or []
                    ),
                )
                for c in p["candidates"]
            )
            extra = {
                k: v for k, v in p.items() if k not in {"v", "week_start", "status", "candidates"}
            }
            return cls(str(p["week_start"]), cands, str(p.get("status", "open")), extra)
        except (KeyError, TypeError, ValueError):
            log.warning("malformed motw suggestion metadata", exc_info=True)
            return None


def draft_text(c: Candidate) -> str:
    """One candidate's draft block, as motw-agent renders it (minus its fallback note)."""
    lines = [f"*#{c.n} pitch:* {c.blurb}" if c.blurb else f"*#{c.n} pitch:* _no blurb_"]
    lines += [f"{i}. {r.text}  _({r.kind})_" for i, r in enumerate(c.reasons, 1)]
    return "\n".join(lines)


# --- the grammar ----------------------------------------------------------------------


@dataclass(frozen=True)
class SetBlurb:
    text: str


@dataclass(frozen=True)
class SetReason:
    k: int
    text: str
    kind: str | None = None


@dataclass(frozen=True)
class DropReason:
    k: int


Edit = SetBlurb | SetReason | DropReason


@dataclass(frozen=True)
class Command:
    action: str  # pick | edit | skip
    n: int | None = None
    edits: tuple[Edit, ...] = ()


class GrammarError(ValueError):
    """A reply that looks like a MOTW command but isn't valid. The message is for the user."""


_KIND = "|".join(KINDS)
_HEAD = re.compile(r"^(?:(pick|edit)\s+#?(\d+)|#?(\d+))(?:\s+(.*))?$", re.IGNORECASE)
_BLURB = re.compile(r"^blurb\s*:\s*(.*)$", re.IGNORECASE | re.DOTALL)
_REASON = re.compile(rf"^reason\s+#?(\d+)(?:\s+({_KIND}))?\s*:\s*(.*)$", re.IGNORECASE)
_DROP = re.compile(r"^(?:drop|delete|remove)\s+reason\s+#?(\d+)$", re.IGNORECASE)
_LOOKS_LIKE = re.compile(r"^(pick|edit|skip|blurb|reason|drop|#?\d+)\b", re.IGNORECASE)


def _edit(line: str) -> Edit:
    if m := _BLURB.match(line):
        return SetBlurb(m.group(1).strip())
    if m := _REASON.match(line):
        kind = m.group(2).lower() if m.group(2) else None
        return SetReason(int(m.group(1)), m.group(3).strip(), kind)
    if m := _DROP.match(line):
        return DropReason(int(m.group(1)))
    if re.match(r"^reason\s+#?\d+\s+\w+\s*:", line, re.IGNORECASE):
        raise GrammarError(f"A reason's kind is one of: {', '.join(KINDS)}.")
    raise GrammarError(f"I don't understand `{line[:60]}`. {HELP}")


def parse(text: str) -> Command | None:
    """A command, or None when the text isn't addressed to MOTW at all.

    Raises GrammarError when it clearly is (starts with pick/edit/skip/blurb/reason/a
    number) but is malformed.
    """
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return None
    first = lines[0]
    if first.lower() in ("skip", "skip it", "skip this week") and len(lines) == 1:
        return Command("skip")
    if not _LOOKS_LIKE.match(first):
        return None
    m = _HEAD.match(first)
    if not m:
        if _BLURB.match(first) or _REASON.match(first) or _DROP.match(first):
            raise GrammarError("Say which candidate first: `edit 2`, then the edit on a new line.")
        raise GrammarError(HELP)
    action = (m.group(1) or "pick").lower()
    n = int(m.group(2) or m.group(3))
    rest = (m.group(4) or "").strip()
    if not m.group(1) and rest:  # "2 something": a bare number followed by text
        raise GrammarError(HELP)
    edits = [_edit(rest)] if rest else []
    edits += [_edit(ln) for ln in lines[1:]]
    if action == "edit" and not edits:
        raise GrammarError("Nothing to change. Put edits on the next lines. " + HELP)
    return Command(action, n, tuple(edits))


def apply_edits(c: Candidate, edits: tuple[Edit, ...]) -> Candidate:
    """The candidate with edits applied; GrammarError (user-facing) if one doesn't fit."""
    blurb = c.blurb
    reasons: list[Reason | None] = list(c.reasons)
    shown = len(c.reasons)
    appended: list[Reason] = []
    drops: set[int] = set()
    for e in edits:
        if isinstance(e, SetBlurb):
            if not e.text:
                raise GrammarError("The blurb can't be empty.")
            if len(e.text) > BLURB_MAX:
                raise GrammarError(f"The blurb is {len(e.text)} characters; max {BLURB_MAX}.")
            blurb = e.text
        elif isinstance(e, SetReason):
            if not 1 <= len(e.text) <= REASON_MAX:
                raise GrammarError(f"A reason is 1-{REASON_MAX} characters.")
            if 1 <= e.k <= shown:
                old = c.reasons[e.k - 1]
                reasons[e.k - 1] = Reason(e.text, e.kind or old.kind)
            elif e.k == shown + 1 + len(appended):
                appended.append(Reason(e.text, e.kind or DEFAULT_KIND))
            else:
                raise GrammarError(
                    f"#{c.n} has {shown} reasons: change 1-{shown}, or add #{shown + 1}."
                )
        else:
            if not 1 <= e.k <= shown:
                raise GrammarError(f"#{c.n} has no reason {e.k}.")
            drops.add(e.k)
    kept = [r for i, r in enumerate(reasons, 1) if r is not None and i not in drops]
    final = tuple(kept + appended)
    if len(final) > MAX_REASONS:
        raise GrammarError(f"At most {MAX_REASONS} reasons.")
    return replace(c, blurb=blurb, reasons=final)


# --- MT -------------------------------------------------------------------------------


class MotwApiError(RuntimeError):
    """MT refused or failed. The message is for logs only, never for users."""


class MotwApi:
    """GET /api/motw (public) and POST /api/admin/motw (MT_MOTW_TOKEN, `manage_motw` only)."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("MT_API_URL", DEFAULT_API_URL)).rstrip("/")
        self._token = token if token is not None else os.environ.get("MT_MOTW_TOKEN", "")
        self._http = http

    def __repr__(self) -> str:  # never the token
        return f"MotwApi({self.base_url!r})"

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        url = f"{self.base_url}{path}"
        try:
            if self._http is not None:
                resp = await self._http.request(method, url, **kw)
            else:
                async with httpx.AsyncClient(timeout=20) as http:
                    resp = await http.request(method, url, **kw)
        except httpx.HTTPError as exc:
            raise MotwApiError(f"{method} {path}: {type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            raise MotwApiError(f"{method} {path}: HTTP {resp.status_code}: {resp.text[:300]}")
        return resp

    async def current_pick(
        self, week_start: str, slot: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        """The slot's pick, or the week's one pick for a v1 suggestion (no slot)."""
        resp = await self._request("GET", "/api/motw", params={"week_start": week_start})
        try:
            body = resp.json()
        except ValueError as exc:
            raise MotwApiError(f"GET /api/motw: not JSON: {exc}") from exc
        picks = body.get("picks")
        if slot is None or picks is None:  # v1 suggestion, or an MT from before SB-1638
            return body.get("motw")
        for p in picks:
            age = p.get("age_group") or (p.get("match") or {}).get("age_group_name")
            if (p.get("pool") or "main") == slot.get("pool", "main") and age == slot["age_group"]:
                return p
        return None

    async def set_pick(self, c: Candidate) -> dict[str, Any]:
        if not self._token:
            raise MotwApiError("MT_MOTW_TOKEN is not set")
        body = {
            "match_id": c.match_id,
            "blurb": c.blurb,
            "reasons": [r.as_dict() for r in c.reasons],
        }
        resp = await self._request(
            "POST",
            "/api/admin/motw",
            json=body,
            headers={"Authorization": f"Bearer {self._token}", "X-MT-Client": "sb-agents"},
        )
        try:
            return resp.json()
        except ValueError:  # saved (2xx); the body is only informational
            return {}


def pick_label(pick: dict[str, Any]) -> str:
    m = pick.get("match") or {}
    home, away = m.get("home_team_name"), m.get("away_team_name")
    return f"{home} vs {away}" if home and away else "another match"
