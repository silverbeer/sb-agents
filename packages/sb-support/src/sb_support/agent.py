"""The support agent: routes SB questions to A2A specialists, answers small talk itself."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal

from opentelemetry import trace
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

from sb_support.specialists import Specialist, SpecialistError

log = logging.getLogger("sb_support.routing")

DEFAULT_MODEL = "anthropic:claude-haiku-5-5"

# Decide, ask one or two specialists, relay (+1 retry). Specialists' own spend is capped and
# recorded by them; these caps cover sb-support's model only.
USAGE_LIMITS = UsageLimits(
    request_limit=5, tool_calls_limit=4, total_tokens_limit=30_000, cost_limit=Decimal("0.01")
)

INSTRUCTIONS = """\
You are sb-agent, the support assistant for silverbeer (SB) projects, answering in Slack.

- For questions about an SB project, call the specialist tool that fits and relay its
  answer. Keep its sources and confidence line. Do not add facts it did not give you.
- Follow-up questions in a thread refer to the earlier conversation: make the question
  you pass to a specialist self-contained (resolve "it", "that", "the other one").
- Greetings and small talk: answer yourself, without tools.
- If no specialist fits, or one fails or doesn't know, say so plainly. A human will be
  pulled in automatically; don't promise anything on their behalf.
- Be brief and concrete. Use Slack mrkdwn (*bold*, `code`), not Markdown headings.
"""


@dataclass
class SpecialistCall:
    specialist: str
    question: str
    confidence: str | None = None
    error: str | None = None


@dataclass
class SupportDeps:
    """Per-run record of which specialists were asked and how sure they were."""

    calls: list[SpecialistCall] = field(default_factory=list)

    @property
    def routed_to(self) -> list[str]:
        return list(dict.fromkeys(c.specialist for c in self.calls))

    @property
    def escalation_reason(self) -> str | None:
        """Deterministic, not the model's call: a failed or unsure specialist pulls in a human."""
        for call in self.calls:
            if call.error:
                return f"{call.specialist} failed"
        for call in self.calls:
            if call.confidence == "low":
                return f"{call.specialist} wasn't confident"
        return None


def specialist_tool(specialist: Specialist) -> Tool[SupportDeps]:
    async def ask(ctx: RunContext[SupportDeps], question: str) -> str:
        """Args:
        question: self-contained question for the specialist.
        """
        call = SpecialistCall(specialist.name, question)
        ctx.deps.calls.append(call)
        log.info("routed", extra={"specialist": specialist.name, "question": question[:200]})
        trace.get_current_span().set_attribute("sb.routed_to", specialist.name)
        try:
            answer = await specialist.client.ask(question)
        except SpecialistError as exc:
            # Returned, not raised: the model can still reply ("docs are unavailable"), and
            # the failed call triggers escalation. A retry would just fail again.
            call.error = str(exc)
            log.warning(
                "specialist failed", extra={"specialist": specialist.name, "error": str(exc)}
            )
            return f"{specialist.name} is unavailable right now ({exc})."
        call.confidence = answer.confidence
        return answer.text

    return Tool(
        ask, name=specialist.tool_name, description=specialist.tool_description, takes_ctx=True
    )


def build_agent(
    model: Model | str | None = None, specialists: list[Specialist] | None = None
) -> Agent[SupportDeps, str]:
    """Build the support agent. `model` overrides SB_SUPPORT_MODEL / the default."""
    return Agent(
        model or os.environ.get("SB_SUPPORT_MODEL", DEFAULT_MODEL),
        deps_type=SupportDeps,
        instructions=INSTRUCTIONS,
        name="sb-support",
        tools=[specialist_tool(s) for s in specialists or []],
    )
