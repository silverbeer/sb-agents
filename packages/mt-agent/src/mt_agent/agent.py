"""mt-agent: answers questions about MissingTable (MT) matches, teams and standings.

An ADK `LlmAgent` whose tools all come from mt-mcp over MCP (streamable HTTP). The
server decides what this caller may see; `tool_filter` repeats the cap here, so a
server bug can't widen it. Phase 1 calls mt-mcp as one public, read-only identity
(`MT_AGENT_MCP_TOKEN`): Slack users have no MT identity yet, so `get_my_team` is out.
"""

from __future__ import annotations

import json
import os
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.models.anthropic_llm import AnthropicLlm
from google.adk.models.base_llm import BaseLlm
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams

# ADK probes for Google mTLS before every MCP session (google.auth.default(), which
# off-GCP waits on the metadata server for seconds). Measured in MT, SB-1302.
os.environ.setdefault("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false")

NAME = "mt_agent"  # ADK agent names must be identifiers; the A2A card says "mt-agent"
DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_MCP_URL = "https://api.missingtable.com/mcp/"
CLIENT_HEADER = "sb-agents"  # X-MT-Client: can only narrow the tiers mt-mcp lists
TOOLS = ("search_teams", "list_divisions", "get_upcoming_matches", "get_standings")

INSTRUCTIONS = """\
You answer questions about MissingTable (MT), a youth soccer site: teams, upcoming matches
and standings. Use ONLY your tools; only state facts that appear in tool results.

- Resolve every team with search_teams first, then use its team_id and age_group id.
- Upcoming matches: get_upcoming_matches. "matches": [] means nothing is scheduled as of
  "today"; only say data is unavailable when the result has an "error". Kickoff times are
  the club's local time. A postponed match is still listed: say it was postponed.
- A table named by its division or league, not a team ("U15 Northeast", "the Flex New
  England table"): find it with list_divisions (filter by age_group and query), then call
  get_standings with that division id and age_group id. If it says "not_found", tell them
  which divisions exist at that age.
- Standings: get_standings with the team_id and age_group id. Competition is "League" unless
  the question asks for Flex, or "qualifying" for the combined League + Flex record. If it
  says "ambiguous", pick from "options" when the question decides it (a Flex table is the
  option whose division league is Flex; plain "standings" means League) and call again with
  that age_group id and division id; otherwise ask which one in one short question.
- Always report coverage honestly: how many matches the table counts of how many are
  scheduled ("X played of Y scheduled"). "rows": [] with fixtures above 0 means no match has
  been played yet. For "qualifying", say it is a record that includes matches against teams
  outside the table.
- Never say who qualifies, advances or is eliminated: MT has no qualification cutoff.
- Never guess a team. If search_teams says "ambiguous", ask which one and list the
  candidates (name, age group, league). If "not_found" with "other_ages", say the team plays
  at those ages instead. Otherwise say plainly that no such team was found.
- "My team", "we", "us": you don't know who is asking. Ask which team they mean.
- If a tool result has an "error", say in one sentence that the data is unavailable right
  now. Do not invent an answer.
- Keep answers short and Slack-friendly: a sentence or two, or a compact list. No tables.
"""


def build_toolset(
    url: str | None = None,
    token: str | None = None,
    *,
    httpx_client_factory: Any = None,
    timeout_s: float = 10.0,
) -> McpToolset:
    """mt-mcp's read tools. `httpx_client_factory` lets tests reach an in-process server."""
    token = token if token is not None else os.environ.get("MT_AGENT_MCP_TOKEN", "")
    headers = {"X-MT-Client": CLIENT_HEADER}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params: dict[str, Any] = {
        "url": url or os.environ.get("MT_MCP_URL", DEFAULT_MCP_URL),
        "headers": headers,
        "timeout": timeout_s,
        "sse_read_timeout": timeout_s,
    }
    if httpx_client_factory is not None:
        params["httpx_client_factory"] = httpx_client_factory
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(**params), tool_filter=list(TOOLS)
    )


def unwrap_mcp_result(tool_response: Any) -> Any:
    """An MCP CallToolResult → the tool's own dict (same rule as MT's `mt_ai.agent`).

    The model then sees one copy of the result, not a text copy plus
    structuredContent, and the confidence rule reads the tool's own `status`/`error`.
    A protocol-level error (refused call, unknown tool) becomes a tool error result.
    """
    if not isinstance(tool_response, dict) or "content" not in tool_response:
        return tool_response
    structured = tool_response.get("structuredContent")
    if isinstance(structured, dict) and not tool_response.get("isError"):
        return structured
    content = tool_response.get("content") or []
    text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    if tool_response.get("isError"):
        return {"error": {"kind": "unavailable", "message": text or "Tool failed."}}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"value": text}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _after_tool(tool: Any, args: dict[str, Any], tool_context: Any, tool_response: Any) -> Any:
    result = unwrap_mcp_result(tool_response)
    return None if result is tool_response else result


def build_agent(model: BaseLlm | str | None = None, toolset: McpToolset | None = None) -> LlmAgent:
    if model is None or isinstance(model, str):
        model = AnthropicLlm(model=model or os.environ.get("MT_AGENT_MODEL", DEFAULT_MODEL))
    return LlmAgent(
        name=NAME,
        model=model,
        description="Answers questions about MissingTable teams, matches and standings.",
        instruction=INSTRUCTIONS,
        tools=[toolset or build_toolset()],
        after_tool_callback=_after_tool,
    )
