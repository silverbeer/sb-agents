"""mt-agent: answers questions about MissingTable (MT) matches, teams and standings.

An ADK `LlmAgent` whose tools all come from mt-mcp over MCP (streamable HTTP). The
server decides what this caller may see; `tool_filter` repeats the cap here, so a
server bug can't widen it. Without a user, mt-mcp is called as one public, read-only
identity (`MT_AGENT_MCP_TOKEN`) and `get_my_team` is hidden. A request carrying the MT
user's delegated token (SB-1529, `mt_agent.identity`) calls mt-mcp as that user, and
the model gets `get_my_team`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.models.anthropic_llm import AnthropicLlm
from google.adk.models.base_llm import BaseLlm
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
from sb_models import resolve

from mt_agent.identity import USER_TOOLS, mcp_headers, signed_in
from mt_agent.mt_client import CLIENT_HEADER, DEFAULT_MCP_URL

# ADK probes for Google mTLS before every MCP session (google.auth.default(), which
# off-GCP waits on the metadata server for seconds). Measured in MT, SB-1302.
os.environ.setdefault("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false")

NAME = "mt_agent"  # ADK agent names must be identifiers; the A2A card says "mt-agent"
TOOLS = (
    "search_teams",
    "list_divisions",
    "get_upcoming_matches",
    "get_recent_results",
    "get_live_match",
    "get_standings",
)

INSTRUCTIONS = """\
You answer questions about MissingTable (MT), a youth soccer site: teams, results, upcoming
matches and standings. Use ONLY your tools; only state facts that appear in tool results.

- Resolve every team with search_teams first, then use its team_id and age_group id.
- Upcoming matches: get_upcoming_matches. "matches": [] means nothing is scheduled as of
  "today"; only say data is unavailable when the result has an "error". Kickoff times are
  the club's local time. A postponed match is still listed: say it was postponed.
- Results ("how did we do last weekend?", "last 5 games", "form"): get_recent_results with
  the team_id and age_group id; give each score from the team's side (e.g. "won 2-1 at home
  to X"). A shootout is a draw decided on penalties: say both. Head-to-head ("against X"):
  resolve X with search_teams too, then pass its team_id as opponent_id. "matches": [] means
  no played match was found; only an "error" means the data is unavailable. The "summary"
  covers only the matches returned.
- A match in progress or just played ("what's the score?", "who scored?", "is the game on?"):
  find it first (get_upcoming_matches for today's match, status "live"; get_recent_results for
  one already played), then call get_live_match with its match_id. Give the score and the
  minute or period ("2-1 to IFA, 63'", "half time"). For scorers, list each goal with its
  minute and the player exactly as the tool gives it ("#9 Sam K. 12'"): never add or guess a
  surname. "events": [] with meta.coverage set means the match was not tracked live: give
  the score and say who scored wasn't recorded. A match not started has no score yet.
- A match preview ("preview Saturday's game", "what should we expect against X?"): resolve
  the team with search_teams, then call match_preview with its team_id and age_group id,
  and relay the preview it returns as is. "not_found" means no upcoming match is scheduled.
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
  been played yet. For "qualifying", say it is a combined record, not a table position, and
  how many of its matches were against teams outside the table: coverage
  "matches_vs_outside_table" matches, against "teams_outside_table" teams.
- Never say who qualifies, advances or is eliminated: MT has no qualification cutoff.
- Never guess a team. If search_teams says "ambiguous", ask which one and list the
  candidates (name, age group, league). If "not_found" with "other_ages", say the team plays
  at those ages instead. Otherwise say plainly that no such team was found.
{my_team}
- If a tool result has an "error", say in one sentence that the data is unavailable right
  now. Do not invent an answer.
- Standings, results and upcoming matches reach the user as a table built from your tool
  result, with trend notes (runs, movers, games in hand) computed from it. Don't repeat
  them: no list of rows, no streaks or movement. Give a one- or two-sentence headline
  instead: the leader, or the asked-about team's position and points, plus coverage.
- Keep answers short and Slack-friendly: a sentence or two. No tables.
"""


ANONYMOUS = """\
- "My team", "we", "us": you don't know who is asking. Ask which team they mean."""
SIGNED_IN = """\
- "My team", "we", "us": the user is signed in. Call get_my_team first; it returns their
  team(s) with team_id and age_group id. If it lists more than one, ask which one. If it
  says they have no team, say so and suggest picking one in their MissingTable profile.
  A team they name explicitly wins over their own team."""


def instruction_provider(template: str):
    """ADK InstructionProvider: the "my team" rule depends on who is asking (SB-1529)."""

    def provide(_context: Any) -> str:
        return template.replace("{my_team}", SIGNED_IN if signed_in() else ANONYMOUS)

    return provide


def visible_tool(tool: Any, _context: Any = None) -> bool:
    """`tool_filter`: the read tools always; user tools only for a signed-in request."""
    return tool.name in TOOLS or (tool.name in USER_TOOLS and signed_in())


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
        connection_params=StreamableHTTPConnectionParams(**params),
        tool_filter=visible_tool,
        header_provider=mcp_headers,
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


def preview_tool(model: BaseLlm, connect_kwargs: dict[str, Any] | None = None) -> Callable:
    """`match_preview` for the agent: runs the preview Workflow (mt_agent.preview)."""
    from mt_agent.preview import run_preview
    from mt_agent.runner import CURRENT_RUN, record_usage

    def usage(event: Any) -> None:
        run = CURRENT_RUN.get()
        if run is not None:
            record_usage(run, event, model.model)

    async def match_preview(team_id: int, age_group_id: int) -> dict[str, Any]:
        """A short preview of the team's next match: fixture, both teams' recent form,
        the table and the head-to-head, from MT data. Use team_id and age_group.id from
        search_teams. Returns {"preview": text}, or "not_found" when nothing is scheduled."""
        return await run_preview(
            team_id, age_group_id, model, on_model_event=usage, connect_kwargs=connect_kwargs
        )

    return match_preview


def build_agent(
    model: BaseLlm | str | None = None,
    toolset: McpToolset | None = None,
    *,
    preview: bool = True,
    preview_connect: dict[str, Any] | None = None,
) -> LlmAgent:
    """`preview=False` drops match_preview: the agent then previews with its own tool
    calls (the single-agent baseline the eval compares against)."""
    if model is None or isinstance(model, str):
        # Registry default or $MT_AGENT_MODEL; the registry limits mt-agent to Anthropic.
        model = AnthropicLlm(model=resolve("mt-agent", model, check_credentials=False).name)
    tools: list[Any] = [toolset or build_toolset()]
    if preview:
        tools.append(preview_tool(model, preview_connect))
    instruction = INSTRUCTIONS
    if not preview:
        instruction = INSTRUCTIONS.replace(
            "then call match_preview with its team_id and age_group id,\n"
            '  and relay the preview it returns as is. "not_found" means no upcoming match is '
            "scheduled.",
            "then gather the next fixture, both teams' recent results, the\n"
            "  table and the head-to-head with your tools, and write a short preview from them.",
        )
    return LlmAgent(
        name=NAME,
        model=model,
        description="Answers questions about MissingTable teams, matches and standings.",
        instruction=instruction_provider(instruction),
        tools=tools,
        after_tool_callback=_after_tool,
    )
