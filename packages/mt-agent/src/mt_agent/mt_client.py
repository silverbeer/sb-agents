"""mt-mcp called directly, with no model in the loop.

The LlmAgent reaches mt-mcp through ADK's `McpToolset`; deterministic code (the match
preview workflow, the eval's ground truth) calls the same tools here, as the same caller:
`MT_AGENT_MCP_TOKEN` and `X-MT-Client: sb-agents`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

DEFAULT_MCP_URL = "https://api.missingtable.com/mcp/"
CLIENT_HEADER = "sb-agents"  # X-MT-Client: can only narrow the tiers mt-mcp lists


class McpCallError(RuntimeError):
    """A tool call mt-mcp refused or failed (protocol-level, not a tool's own `error`)."""


class MtClient:
    def __init__(self, session: ClientSession) -> None:
        self.session = session

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        """The tool's own result dict (its structuredContent)."""
        result = await self.session.call_tool(tool, args)
        if result.is_error or not isinstance(result.structured_content, dict):
            raise McpCallError(f"{tool}({args}) failed: {result.content}")
        return result.structured_content


@asynccontextmanager
async def connect(
    url: str | None = None,
    token: str | None = None,
    *,
    httpx_client_factory: Callable[..., Any] | None = None,
) -> AsyncIterator[MtClient]:
    """One MCP session. `httpx_client_factory` lets tests reach an in-process server."""
    token = token if token is not None else os.environ.get("MT_AGENT_MCP_TOKEN", "")
    headers = {"X-MT-Client": CLIENT_HEADER}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = url or os.environ.get("MT_MCP_URL", DEFAULT_MCP_URL)
    factory = httpx_client_factory or create_mcp_http_client
    async with (
        factory(headers=headers) as http,
        streamable_http_client(url, http_client=http) as (read, write, *_),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield MtClient(session)
