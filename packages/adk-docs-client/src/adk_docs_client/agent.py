"""ADK router that delegates SB docs questions to docs-agent over A2A."""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.models.anthropic_llm import AnthropicLlm

DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_DOCS_URL = "http://127.0.0.1:8101"
CARD_PATH = "/.well-known/agent-card.json"


def build_docs_remote(url: str | None = None) -> RemoteA2aAgent:
    base = (url or os.environ.get("DOCS_AGENT_A2A_URL", DEFAULT_DOCS_URL)).rstrip("/")
    return RemoteA2aAgent(
        name="docs_agent",
        description=(
            "Answers questions about SB projects from their docs, with cited sources "
            "and a confidence level."
        ),
        agent_card=base + CARD_PATH,
    )


def build_root_agent(docs: RemoteA2aAgent | None = None, model: str | None = None) -> LlmAgent:
    return LlmAgent(
        name="sb_router",
        model=AnthropicLlm(model=model or os.environ.get("ADK_MODEL", DEFAULT_MODEL)),
        description="Front door for SB questions.",
        instruction=(
            "You route questions about silverbeer (SB) projects. For any question about how "
            "an SB project works, is deployed, or is configured, transfer to `docs_agent`. "
            "Answer small talk yourself in one sentence."
        ),
        sub_agents=[docs or build_docs_remote()],
    )
