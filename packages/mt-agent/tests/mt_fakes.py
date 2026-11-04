"""Test doubles for mt-agent: a scripted ADK model and an in-process stub mt-mcp.

`ScriptedLlm` is a real ADK `BaseLlm`: the runner, tool dispatch, callbacks and the
A2A executor all run for real; only the provider call is replaced by a script.
`StubMcp` is a real MCP server (the `mcp` package, streamable HTTP) reached over an
ASGI transport, so McpToolset, tool_filter and the headers are exercised too.
Also loaded by path from tests/mt_hop_scenario.py.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from mcp.server.mcpserver import MCPServer

MCP_BASE = "http://127.0.0.1:8000"
MCP_URL = f"{MCP_BASE}/mcp/"
MODEL = "claude-haiku-5-5"  # a real model name, so sb_obs.price() prices the fake usage


def usage(prompt: int = 1000, output: int = 50) -> types.GenerateContentResponseUsageMetadata:
    return types.GenerateContentResponseUsageMetadata(
        prompt_token_count=prompt, candidates_token_count=output
    )


def calls_tool(name: str, **args: Any) -> LlmResponse:
    part = types.Part(function_call=types.FunctionCall(name=name, args=args))
    return LlmResponse(content=types.Content(role="model", parts=[part]), usage_metadata=usage())


def says(text: str) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=text)]), usage_metadata=usage()
    )


class ScriptedLlm(BaseLlm):
    """Replies with the next scripted response; records every request it gets."""

    model: str = MODEL
    script: list[Any] = []
    requests: list[Any] = []

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.requests.append(llm_request)
        if not self.script:
            raise AssertionError("ScriptedLlm ran out of script")
        yield self.script.pop(0)

    def declared_tools(self) -> list[str]:
        config = self.requests[0].config
        return sorted(
            decl.name for tool in config.tools or [] for decl in tool.function_declarations or []
        )

    def tool_results(self) -> list[dict]:
        """Every function_response the model was shown in its last request, in order."""
        return [
            part.function_response.response
            for content in self.requests[-1].contents
            for part in content.parts or []
            if part.function_response
        ]


def scripted(*steps: Any) -> ScriptedLlm:
    return ScriptedLlm(script=list(steps), requests=[])


# --- stub mt-mcp ----------------------------------------------------------------------

IFA_U14 = {
    "team_id": 102,
    "name": "IFA",
    "club_name": "IFA",
    "age_group": {"id": 14, "name": "U14"},
    "registrations": [{"league": "Homegrown", "division": "Northeast"}],
}
RESOLVED = {"status": "resolved", "team": IFA_U14, "candidates": []}
AMBIGUOUS_SEARCH = {"status": "ambiguous", "team": None, "candidates": [IFA_U14, IFA_U14]}
STANDINGS = {
    "team_id": 102,
    "competition": "League",
    "rows": [{"position": 1, "team": {"id": 102, "name": "IFA"}, "played": 6, "points": 15}],
    "coverage": {"matches_counted": 18, "fixtures": 42},
    "options": [],
    "error": None,
}
AMBIGUOUS_TABLE = {
    "team_id": 102,
    "competition": "League",
    "rows": None,
    "options": [
        {"age_group": {"id": 14, "name": "U14"}, "division": {"id": 7, "name": "NE"}},
        {"age_group": {"id": 14, "name": "U14"}, "division": {"id": 9, "name": "Flex A"}},
    ],
    "error": {"kind": "ambiguous", "message": "IFA plays in 2 tables"},
}


class StubMcp:
    """mt-mcp's read tools (plus get_my_team, to prove the client filters it out).

    `results[tool]` is a dict returned every call, a list consumed one per call, or an
    Exception to raise (the MCP server turns it into an `isError` result).
    """

    def __init__(self, **results: Any) -> None:
        self.results: dict[str, Any] = {"search_teams": RESOLVED, "get_standings": STANDINGS}
        self.results.update(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.headers: list[dict[str, str]] = []
        server = MCPServer("stub-mt-mcp")

        @server.tool()
        def search_teams(query: str, age_group: str | None = None) -> dict[str, Any]:
            """Find an MT team by name."""
            return self._result("search_teams", query=query, age_group=age_group)

        @server.tool()
        def get_upcoming_matches(
            team_id: int, age_group_id: int | None = None, limit: int = 5
        ) -> dict[str, Any]:
            """A team's next matches."""
            return self._result("get_upcoming_matches", team_id=team_id)

        @server.tool()
        def get_recent_results(
            team_id: int,
            age_group_id: int | None = None,
            limit: int = 5,
            opponent_id: int | None = None,
        ) -> dict[str, Any]:
            """A team's last played matches."""
            return self._result("get_recent_results", team_id=team_id, opponent_id=opponent_id)

        @server.tool()
        def get_standings(
            team_id: int | None = None,
            age_group_id: int | None = None,
            division_id: int | None = None,
            competition: str = "League",
        ) -> dict[str, Any]:
            """One standings table and its coverage."""
            return self._result("get_standings", team_id=team_id, division_id=division_id)

        @server.tool()
        def get_my_team() -> dict[str, Any]:
            """The caller's own team."""
            return self._result("get_my_team")

        self.server = server
        inner = server.streamable_http_app(streamable_http_path="/mcp/")

        async def spy(scope: dict, receive: Callable, send: Callable) -> None:
            if scope["type"] == "http":
                self.headers.append({k.decode(): v.decode() for k, v in scope["headers"]})
            await inner(scope, receive, send)

        self.app = spy

    def _result(self, tool: str, **args: Any) -> dict[str, Any]:
        self.calls.append((tool, args))
        result = self.results.get(tool, {"status": "resolved"})
        if isinstance(result, list):
            result = result.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def client_factory(self):
        """An ADK `httpx_client_factory` that reaches this server in-process (mcp uses httpx2)."""

        def factory(headers=None, timeout=None, auth=None) -> httpx2.AsyncClient:
            return httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=self.app),
                headers=headers,
                timeout=timeout,
                auth=auth,
            )

        return factory

    @asynccontextmanager
    async def running(self) -> AsyncIterator[StubMcp]:
        async with self.server.session_manager.run():
            yield self
