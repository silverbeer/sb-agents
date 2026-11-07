from collections.abc import Callable
from datetime import date
from typing import Any

import httpx
import httpx2
import pytest
from mcp.server.mcpserver import MCPServer

from motw_agent import fixtures
from motw_agent.mt import LiveMt, MtError

WEEK = date(2026, 11, 9)


def rest(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_current_pick_reads_the_week_and_treats_null_as_unpicked() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"week_start": "2026-11-09", "motw": None})

    async with rest(handler) as http:
        mt = LiveMt(api_url="https://mt.test/", http=http)
        assert await mt.current_pick(WEEK) is None
    assert str(seen[0].url) == "https://mt.test/api/motw?week_start=2026-11-09"


async def test_current_pick_failure_is_an_mt_error() -> None:
    async with rest(lambda r: httpx.Response(502)) as http:
        with pytest.raises(MtError):
            await LiveMt(api_url="https://mt.test", http=http).current_pick(WEEK)


class StubMcp:
    def __init__(self, result: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        server = MCPServer("stub-mt-mcp")

        @server.tool()
        def suggest_motw_candidates(week: str | None = None, limit: int = 5) -> dict[str, Any]:
            """This week's MOTW candidates."""
            self.calls.append({"week": week, "limit": limit})
            if isinstance(result, Exception):
                raise result
            return result

        self.server = server
        inner = server.streamable_http_app(streamable_http_path="/mcp/")

        async def spy(scope: dict, receive: Callable, send: Callable) -> None:
            if scope["type"] == "http":
                self.headers.append({k.decode(): v.decode() for k, v in scope["headers"]})
            await inner(scope, receive, send)

        self.app = spy

    def factory(self, headers=None, timeout=None, auth=None) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app),
            headers=headers,
            timeout=timeout,
            auth=auth,
        )


def payload() -> dict[str, Any]:
    return fixtures.result("top-clash", "low-sample").model_dump(mode="json")


async def test_candidates_come_from_mt_mcp_as_the_read_account() -> None:
    stub = StubMcp(payload())
    mt = LiveMt(mcp_url="http://127.0.0.1:8000/mcp/", token="tok", mcp_http_factory=stub.factory)
    async with stub.server.session_manager.run():
        result = await mt.candidates(WEEK)
    assert [c.match_id for c in result.candidates or []] == [4096, 5120]
    assert result.candidates[0].fact("4096:gap") is not None
    assert stub.calls == [{"week": "2026-11-09", "limit": 5}]
    assert stub.headers[0]["authorization"] == "Bearer tok"
    assert stub.headers[0]["x-mt-client"] == "sb-agents"


async def test_a_tool_error_is_an_mt_error() -> None:
    stub = StubMcp(RuntimeError("boom"))
    mt = LiveMt(mcp_url="http://127.0.0.1:8000/mcp/", token="tok", mcp_http_factory=stub.factory)
    async with stub.server.session_manager.run():
        with pytest.raises(MtError):
            await mt.candidates(WEEK)


def test_unknown_fields_from_mt_are_ignored() -> None:
    from motw_agent.candidates import CandidatesResult

    data = payload() | {"weights": {"quality": 0.4}, "something_new": 1}
    data["candidates"][0]["new_field"] = "x"
    assert CandidatesResult.model_validate(data).candidates
