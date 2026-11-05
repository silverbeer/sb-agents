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

from a2a.types import AgentCapabilities, AgentCard, AgentExtension, AgentInterface, AgentSkill
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.a2a.executor.config import A2aAgentExecutorConfig
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.tools.base_toolset import BaseToolset
from sb_models import ModelConfigError, resolve
from starlette.applications import Starlette

from mt_agent.agent import build_agent
from mt_agent.identity import IDENTITY_EXTENSION, request_converter
from mt_agent.runner import TrackedRunner
from sb_obs import instrument_asgi, setup_telemetry

log = logging.getLogger("mt_agent.a2a")

DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 8102

DESCRIPTION = (
    "Answers questions about MissingTable (MT) youth soccer data: finding teams, their "
    "recent results and head-to-head record, upcoming matches, match previews, and league "
    "standings with how many matches have been played. "
    "Reads MT's live data. Returns a short answer and a confidence level."
)
EXAMPLES = [
    "When does IFA U14 play next?",
    "What are the standings for NEFC U15 in the League?",
    "How is Sporting U13 doing in Flex?",
    "How did IFA U15 do last weekend?",
]


# Declares "reuse contextId for follow-ups" (SB-1525). Callers send a stable contextId
# only to specialists that declare it: fasta2a (docs-agent) replays the whole history,
# tool results included, for a reused context. Same URI as sb_support.specialists.
CONVERSATION_EXTENSION = "urn:sb-agents:a2a:conversation:v1"


def build_card(url: str) -> AgentCard:
    return AgentCard(
        name="mt-agent",
        description=DESCRIPTION,
        version="0.1.0",
        supported_interfaces=[
            AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        capabilities=AgentCapabilities(
            streaming=True,
            extensions=[
                AgentExtension(
                    uri=CONVERSATION_EXTENSION,
                    description=(
                        "Send the same contextId for follow-up messages: each context is "
                        "one conversation (an ADK session, kept 30 minutes idle)."
                    ),
                ),
                AgentExtension(
                    uri=IDENTITY_EXTENSION,
                    description=(
                        "Send the MT user's delegated token in params.metadata as "
                        '{"<this uri>": {"token": ...}} to answer as that user ("my team"). '
                        "Used for that request only; never stored."
                    ),
                ),
            ],
        ),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain", "application/json"],
        skills=[
            AgentSkill(
                id="mt-data",
                name="Answer from MissingTable data",
                description="Teams, results, upcoming matches and standings, with coverage stated.",
                tags=["missingtable", "soccer", "standings", "matches", "results"],
                examples=EXAMPLES,
                input_modes=["text/plain"],
                output_modes=["text/plain", "application/json"],
            ),
            AgentSkill(
                id="match-preview",
                name="Preview a team's next match",
                description=(
                    "Fixture, both teams' recent form, the table and head-to-head, from MT "
                    "data. No predictions."
                ),
                tags=["missingtable", "soccer", "preview"],
                examples=["Preview IFA U15's next game", "What should we expect against Valeo?"],
                input_modes=["text/plain"],
                output_modes=["text/plain", "application/json"],
            ),
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
        agent_executor_factory=executor,
    )


def executor(runner: Runner) -> A2aAgentExecutor:
    """ADK's executor with the identity-stripping request converter (SB-1529)."""
    return A2aAgentExecutor(
        runner=runner, config=A2aAgentExecutorConfig(request_converter=request_converter)
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
