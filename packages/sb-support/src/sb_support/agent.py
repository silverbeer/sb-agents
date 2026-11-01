"""The support agent. Docs questions go to docs-agent over A2A; routing grows in SB-1476."""

import os
from decimal import Decimal

from pydantic_ai import Agent, ModelRetry
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

from sb_support.docs_client import DocsAgentError, DocsClient

DEFAULT_MODEL = "anthropic:claude-haiku-5-5"

# One call to decide, one ask_docs, one to relay (+1 retry). docs-agent's own spend is
# capped and recorded by docs-agent; these caps cover sb-support's model only.
USAGE_LIMITS = UsageLimits(
    request_limit=4, tool_calls_limit=3, total_tokens_limit=20_000, cost_limit=Decimal("0.01")
)

INSTRUCTIONS = """\
You are sb-agent, the support assistant for silverbeer (SB) projects, answering in Slack.

- For any question about an SB project (missing-table, match-scraper, trd, bet, cycle-runner,
  dotfiles, janitor, myrunstreak, ...), call `ask_docs` and relay its answer. Keep its
  sources and confidence line. Do not add facts it did not give you.
- Greetings and small talk: answer yourself, without tools.
- Be brief and concrete. Use Slack mrkdwn (*bold*, `code`), not Markdown headings.
- If ask_docs fails or doesn't know, say so plainly instead of guessing.
"""


def build_agent(
    model: Model | str | None = None, docs: DocsClient | None = None
) -> Agent[None, str]:
    """Build the support agent. `model` overrides SB_SUPPORT_MODEL / the default."""
    agent = Agent(
        model or os.environ.get("SB_SUPPORT_MODEL", DEFAULT_MODEL),
        instructions=INSTRUCTIONS,
        name="sb-support",
    )
    docs = docs or DocsClient()

    @agent.tool_plain
    async def ask_docs(question: str) -> str:
        """Ask docs-agent, which answers from SB repos' markdown docs with sources.

        Args:
            question: the user's question, self-contained (resolve "it"/"that").
        """
        try:
            return await docs.ask(question)
        except DocsAgentError as exc:
            raise ModelRetry(
                f"docs-agent failed: {exc}. Tell the user docs are unavailable."
            ) from exc

    return agent
