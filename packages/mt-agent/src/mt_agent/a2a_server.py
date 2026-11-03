"""Serve mt-agent over A2A with ADK's `to_a2a` (a2a-sdk server).

    op run --env-file=.env.op -- uv run mt-agent-a2a     # http://127.0.0.1:8102

Agent card: GET /.well-known/agent-card.json. Routes are attached in the app's
lifespan (ADK builds them there), so an in-process client must run the lifespan.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.agents import LlmAgent
from google.adk.tools.base_toolset import BaseToolset
from sb_models import ModelConfigError, resolve
from starlette.applications import Starlette

from mt_agent.agent import build_agent
from mt_agent.runner import TrackedRunner
from sb_obs import instrument_asgi, setup_telemetry

log = logging.getLogger("mt_agent.a2a")

DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 8102

DESCRIPTION = (
    "Answers questions about MissingTable (MT) youth soccer data: finding teams, their "
    "upcoming matches, and league standings with how many matches have been played. "
    "Reads MT's live data. Returns a short answer and a confidence level."
)
EXAMPLES = [
    "When does IFA U14 play next?",
    "What are the standings for NEFC U15 in the League?",
    "How is Sporting U13 doing in Flex?",
    "Which age groups does Bayside FC play in?",
]


def build_card(url: str) -> AgentCard:
    return AgentCard(
        name="mt-agent",
        description=DESCRIPTION,
        version="0.1.0",
        supported_interfaces=[
            AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain", "application/json"],
        skills=[
            AgentSkill(
                id="mt-data",
                name="Answer from MissingTable data",
                description="Teams, upcoming matches and standings, with coverage stated.",
                tags=["missingtable", "soccer", "standings", "matches"],
                examples=EXAMPLES,
                input_modes=["text/plain"],
                output_modes=["text/plain", "application/json"],
            )
        ],
    )


def build_app(agent: LlmAgent | None = None, url: str | None = None) -> Starlette:
    host = os.environ.get("MT_AGENT_HOST", DEFAULT_HOST)
    port = int(os.environ.get("MT_AGENT_PORT", DEFAULT_PORT))
    agent = agent or build_agent()

    @asynccontextmanager
    async def close_toolsets(app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            for tool in agent.tools:
                if isinstance(tool, BaseToolset):
                    await tool.close()

    return to_a2a(
        agent,
        runner=TrackedRunner(agent),
        agent_card=build_card(url or os.environ.get("MT_AGENT_A2A_URL", f"http://{host}:{port}")),
        lifespan=close_toolsets,
    )


def main() -> None:
    import uvicorn

    host = os.environ.get("MT_AGENT_HOST", DEFAULT_HOST)
    port = int(os.environ.get("MT_AGENT_PORT", DEFAULT_PORT))
    setup_telemetry("mt-agent")
    try:
        resolve("mt-agent")  # fail at startup, not on the first question
    except ModelConfigError as exc:
        raise SystemExit(str(exc)) from exc
    if not os.environ.get("MT_AGENT_MCP_TOKEN"):
        log.warning("MT_AGENT_MCP_TOKEN is not set: mt-mcp will refuse every tool call")
    # log_config=None: uvicorn's loggers propagate to our JSON root handler.
    uvicorn.run(instrument_asgi(build_app()), host=host, port=port, log_config=None)
