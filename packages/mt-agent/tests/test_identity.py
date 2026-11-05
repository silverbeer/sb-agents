"""The signed-in MT user's delegated token (SB-1529): per request, never kept."""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager

import httpx
from mt_fakes import MCP_URL, StubMcp, calls_tool, says, scripted

from mt_agent.a2a_server import build_app
from mt_agent.agent import build_agent, build_toolset, instruction_provider, visible_tool
from mt_agent.identity import (
    IDENTITY_EXTENSION,
    USER_CLIENT,
    USER_TOKEN,
    mcp_headers,
    take_token,
)
from sb_support.specialists import SpecialistClient

BASE = "http://127.0.0.1:8102"
MY_TEAM = {"status": "ok", "teams": [{"team_id": 19, "name": "IFA", "age_group": "U15"}]}


@asynccontextmanager
async def served(model, stub: StubMcp):
    toolset = build_toolset(MCP_URL, "t0k", httpx_client_factory=stub.client_factory())
    app = build_app(build_agent(model, toolset), url=BASE)
    async with stub.running(), app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as http:
            yield http


def identity(token: str) -> dict:
    return {IDENTITY_EXTENSION: {"token": token}}


def test_take_token_removes_the_entry_and_keeps_other_metadata() -> None:
    metadata = {**identity("tok"), "other": 1}
    assert take_token(metadata) == "tok"
    assert metadata == {"other": 1}
    for bad in ({IDENTITY_EXTENSION: "tok"}, {IDENTITY_EXTENSION: {"token": ""}}, {}):
        assert take_token(dict(bad)) is None
    assert take_token({IDENTITY_EXTENSION: {"token": "x" * 5000}}) is None


def test_headers_tools_and_instructions_follow_the_token() -> None:
    class Tool:
        def __init__(self, name: str) -> None:
            self.name = name

    provide = instruction_provider("rules\n{my_team}\n")
    assert mcp_headers() == {}
    assert not visible_tool(Tool("get_my_team")) and visible_tool(Tool("get_standings"))
    assert "Ask which team they mean" in provide(None)
    reset = USER_TOKEN.set("tok")
    try:
        assert mcp_headers() == {"Authorization": "Bearer tok", "X-MT-Client": USER_CLIENT}
        assert visible_tool(Tool("get_my_team"))
        assert "Call get_my_team first" in provide(None)
    finally:
        USER_TOKEN.reset(reset)


async def test_tokens_do_not_cross_concurrent_requests() -> None:
    async def request(token: str | None) -> dict:
        USER_TOKEN.set(token)
        await asyncio.sleep(0)
        return mcp_headers()

    a, b, none = await asyncio.gather(request("a"), request("b"), request(None))
    assert a["Authorization"] == "Bearer a" and b["Authorization"] == "Bearer b"
    assert none == {}


async def test_signed_in_request_calls_mt_mcp_as_the_user_then_forgets() -> None:
    stub = StubMcp(get_my_team=MY_TEAM)
    model = scripted(
        calls_tool("get_my_team"),
        says("Your team is IFA U15."),
        calls_tool("search_teams", query="IFA", age_group="U15"),
        says("Which IFA team?"),
    )
    async with served(model, stub) as http:
        client = SpecialistClient(BASE, http)
        answer = await client.ask("how is my team?", metadata=identity("user-tok-1"))
        user_headers = list(stub.headers)
        user_tools = model.declared_tools()
        stub.headers.clear()
        model.requests.clear()
        await client.ask("how is my team?")  # same server, next request: no token
        # Snapshot before shutdown: closing the pool sends each session's DELETE with
        # the headers it was opened with (ADK also evicts idle sessions after a TTL).
        public_headers = list(stub.headers)

    assert answer.text == "Your team is IFA U15."
    assert answer.data["sources"] == ["get_my_team"]
    assert {h.get("authorization") for h in user_headers} == {"Bearer user-tok-1"}
    assert {h.get("x-mt-client") for h in user_headers} == {USER_CLIENT}
    assert "get_my_team" in user_tools
    # The next request is phase 1 again: public token, no get_my_team.
    assert {h.get("authorization") for h in public_headers} == {"Bearer t0k"}
    assert {h.get("x-mt-client") for h in public_headers} == {"sb-agents"}
    assert "get_my_team" not in model.declared_tools()


async def test_token_never_reaches_the_model_or_the_reply() -> None:
    stub = StubMcp(get_my_team=MY_TEAM)
    model = scripted(calls_tool("get_my_team"), says("Your team is IFA U15."))
    request = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendStreamingMessage",
        "params": {
            "message": {
                "messageId": uuid.uuid4().hex,
                "role": "ROLE_USER",
                "parts": [{"text": "how is my team?"}],
            },
            "metadata": identity("secret-user-tok"),
        },
    }
    headers = {"A2A-Version": "1.0", "Accept": "text/event-stream"}
    async with served(model, stub) as http:
        response = await http.post("/", json=request, headers=headers)
    assert "Your team is IFA U15." in response.text
    assert "secret-user-tok" not in response.text
    seen_by_model = json.dumps([r.model_dump(mode="json") for r in model.requests], default=str)
    assert "secret-user-tok" not in seen_by_model
    assert USER_TOKEN.get() is None
