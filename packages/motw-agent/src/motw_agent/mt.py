"""MissingTable reads: the week's pick (REST) and the week's candidates (mt-mcp).

Read-only. The read account is mt-agent's (`MT_AGENT_MCP_TOKEN`, a public read-only MT
account) with `X-MT-Client: sb-agents`; `GET /api/motw` is public and needs no token.
Writing a pick is sb-support's job (SB-1546), with a different, write-scoped token.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from motw_agent.candidates import CandidatesResult

DEFAULT_MCP_URL = "https://api.missingtable.com/mcp/"
DEFAULT_API_URL = "https://api.missingtable.com"
CLIENT_HEADER = "sb-agents"
TOOL = "suggest_motw_candidates"
# MT's MOTW pool is defined in Eastern time; so is "this week".
TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Slot:
    """One Match of the Week a week: a pool and an age group (MT SB-1638)."""

    pool: str  # "main" | "pathway"
    age_group: str  # "U15", ...

    @property
    def label(self) -> str:
        return f"{self.age_group} Pro Pathway" if self.pool == "pathway" else self.age_group

    def as_dict(self) -> dict[str, str]:
        return {"pool": self.pool, "age_group": self.age_group}

    @classmethod
    def parse(cls, value: str) -> Slot:
        """`U16` (main pool) or `pathway:U16`."""
        pool, _, age = value.rpartition(":")
        return cls(pool or "main", age)


# Main pool: Homegrown Northeast League + Flex Empire/Turnpike/New England.
# Pathway: Northeast Pro Player Pathway, which has no U15.
SLOTS: tuple[Slot, ...] = (
    Slot("main", "U15"),
    Slot("main", "U16"),
    Slot("main", "U17"),
    Slot("main", "U19"),
    Slot("pathway", "U16"),
    Slot("pathway", "U17"),
    Slot("pathway", "U19"),
)


def slot_pick(body: dict[str, Any], slot: Slot | None) -> dict[str, Any] | None:
    """The slot's pick from a `GET /api/motw` body.

    An MT from before SB-1638 has no `picks`: its one `motw` was every slot's.
    """
    picks = body.get("picks")
    if slot is None or picks is None:
        return body.get("motw")
    for p in picks:
        age = p.get("age_group") or (p.get("match") or {}).get("age_group_name")
        if (p.get("pool") or "main") == slot.pool and age == slot.age_group:
            return p
    return None


class MtError(RuntimeError):
    """MT could not be read. The message is for logs, never for Slack."""


def today_eastern(now: datetime | None = None) -> date:
    return (now or datetime.now(TZ)).astimezone(TZ).date()


def week_start(day: date) -> date:
    """The Monday of `day`'s week (MT's MOTW weeks are Mon-Sun)."""
    return day - timedelta(days=day.weekday())


def parse_week(value: str | None, now: datetime | None = None) -> date:
    """`--week` (any YYYY-MM-DD in the week) or this week, as the week's Monday."""
    return week_start(date.fromisoformat(value) if value else today_eastern(now))


class MtReader(Protocol):
    async def current_pick(self, week: date, slot: Slot | None = None) -> dict[str, Any] | None: ...

    async def candidates(
        self, week: date, limit: int = 5, slot: Slot | None = None
    ) -> CandidatesResult: ...


class LiveMt:
    """The real MT: REST for the week's pick, mt-mcp for the candidates."""

    def __init__(
        self,
        api_url: str | None = None,
        mcp_url: str | None = None,
        token: str | None = None,
        http: httpx.AsyncClient | None = None,
        mcp_http_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.api_url = (api_url or os.environ.get("MT_API_URL", DEFAULT_API_URL)).rstrip("/")
        self.mcp_url = mcp_url or os.environ.get("MT_MCP_URL", DEFAULT_MCP_URL)
        self.token = token if token is not None else os.environ.get("MT_AGENT_MCP_TOKEN", "")
        self.http = http
        self.mcp_http_factory = mcp_http_factory  # tests: reach an in-process mt-mcp

    async def current_pick(self, week: date, slot: Slot | None = None) -> dict[str, Any] | None:
        """The slot's pick for the week, or None. MT answers 200 with `motw: null` when unpicked."""
        url = f"{self.api_url}/api/motw"
        try:
            if self.http is not None:
                resp = await self.http.get(url, params={"week_start": week.isoformat()})
            else:
                async with httpx.AsyncClient(timeout=20) as http:
                    resp = await http.get(url, params={"week_start": week.isoformat()})
            resp.raise_for_status()
            return slot_pick(resp.json(), slot)
        except (httpx.HTTPError, ValueError) as exc:
            raise MtError(f"GET /api/motw?week_start={week}: {exc}") from exc

    async def candidates(
        self, week: date, limit: int = 5, slot: Slot | None = None
    ) -> CandidatesResult:
        # Imported here: the mcp client stack is only needed for this call.
        from mcp import ClientSession
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        headers = {"X-MT-Client": CLIENT_HEADER}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            factory = self.mcp_http_factory or create_mcp_http_client
            async with (
                factory(headers=headers) as http,
                streamable_http_client(self.mcp_url, http_client=http) as (read, write, *_),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                args: dict[str, Any] = {"week": week.isoformat(), "limit": limit}
                if slot is not None:
                    args |= slot.as_dict()
                result = await session.call_tool(TOOL, args)
        except Exception as exc:  # noqa: BLE001 - transport, auth, protocol: all "MT down"
            raise MtError(f"{TOOL}: {type(exc).__name__}: {exc}") from exc
        if result.is_error or not isinstance(result.structured_content, dict):
            raise MtError(f"{TOOL} failed: {result.content}")
        return CandidatesResult.model_validate(result.structured_content)
