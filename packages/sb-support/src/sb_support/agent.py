"""The support agent. An echo-grade PydanticAI agent for now; specialists arrive via A2A later."""

import os
from decimal import Decimal

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

DEFAULT_MODEL = "anthropic:claude-haiku-5-5"

# sb-support has no tools yet: one request per question. Caps stop a runaway.
USAGE_LIMITS = UsageLimits(request_limit=3, total_tokens_limit=20_000, cost_limit=Decimal("0.01"))

INSTRUCTIONS = """\
You are sb-agent, the support assistant for silverbeer (SB) projects, answering in Slack.
Be brief and concrete. Use Slack mrkdwn (*bold*, `code`), not Markdown headings.
If you do not know something, say so plainly instead of guessing.
"""


def build_agent(model: Model | str | None = None) -> Agent[None, str]:
    """Build the support agent. `model` overrides SB_SUPPORT_MODEL / the default."""
    return Agent(
        model or os.environ.get("SB_SUPPORT_MODEL", DEFAULT_MODEL),
        instructions=INSTRUCTIONS,
        name="sb-support",
    )
