"""The support agent: routes SB questions to A2A specialists, answers small talk itself."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from opentelemetry import trace
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits
from sb_models import resolve

from sb_support.prefs import PreferenceStore
from sb_support.specialists import Specialist, SpecialistError
from sb_support.tables import Table, parse_tables

log = logging.getLogger("sb_support.routing")


# Decide, ask one or two specialists, relay (+1 retry). Specialists' own spend is capped and
# recorded by them; these caps cover sb-support's model only.
USAGE_LIMITS = UsageLimits(
    request_limit=5, tool_calls_limit=4, total_tokens_limit=30_000, cost_limit=Decimal("0.01")
)

INSTRUCTIONS = """\
You are sb-agent, the support assistant for silverbeer (SB) projects, answering in Slack.

- For questions about an SB project, call the specialist tool that fits and relay its
  answer. Keep any sources its answer cites. Do not add facts it did not give you, and
  never add a confidence or source line of your own: confidence is reported separately.
- A specialist's answer may say a table is shown under your reply: keep to its headline;
  never list the rows, say the data is incomplete, or suggest asking again for it.
- Pass the user's own words for names and abbreviations ("HG", "NEFC"); don't expand or
  reinterpret them, the specialist resolves them.
- Follow-up questions in a thread refer to the earlier conversation: make the question
  you pass to a specialist self-contained (resolve "it", "that", "the other one").
- Greetings and small talk: answer yourself, without tools.
- If no specialist fits, or one fails or doesn't know, say so plainly. A human will be
  pulled in automatically; don't promise anything on their behalf.
- Be brief and concrete. Use Slack mrkdwn (*bold*, `code`), not Markdown headings.
- "My team is X" / "remember my team is X": call remember_my_team with X in their words,
  then confirm in one line. "Forget my team": call forget_my_team.
"""

NO_USER = "You can't save a team for this user; if asked, say saving a team isn't available here."
NO_TEAM = (
    'This user has no saved team. If they say "my team", "we" or "us" about a team, '
    'ask which team they mean, and offer to remember it ("my team is IFA Homegrown U15").'
)


def my_team_instructions(ctx: RunContext[SupportDeps]) -> str:
    """The user's saved team, so "how's my team?" needs no follow-up question (SB-1564)."""
    deps = ctx.deps
    if deps.prefs is None or not deps.user_id:
        return NO_USER
    team = deps.prefs.my_team(deps.channel, deps.user_id)
    if not team:
        return NO_TEAM
    return (
        f'This user\'s saved team is: {team}. When they say "my team", "we" or "us" '
        "about a team, put that team's name in the question you send a specialist instead, "
        "in those words. If they name a different team, use the one they name."
    )


@dataclass
class SpecialistCall:
    specialist: str
    question: str
    confidence: str | None = None
    error: str | None = None
    tables: list[Table] = field(default_factory=list)


@dataclass
class SupportDeps:
    """Per-run record of which specialists were asked and how sure they were."""

    calls: list[SpecialistCall] = field(default_factory=list)
    conversation_key: str | None = None  # "slack:<thread>": scopes specialist contexts
    channel: str = "direct"
    user_id: str | None = None  # the channel's user id; keys the user's preferences
    prefs: PreferenceStore | None = None

    @property
    def routed_to(self) -> list[str]:
        return list(dict.fromkeys(c.specialist for c in self.calls))

    @property
    def tables(self) -> list[Table]:
        """The latest successful call's tables: the data the reply is about."""
        for call in reversed(self.calls):
            if call.tables and not call.error:
                return call.tables
        return []

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


def shown_note(tables: list[Table]) -> str:
    """Tell the model what the user will see under its reply, so it doesn't call it missing."""
    if not tables:
        return ""
    table = tables[0]  # the channel shows one table a reply
    notes = f" and {len(table.notes)} trend notes" if table.notes else ""
    return (
        f'\n\n[Shown to the user under your reply: the table "{table.title}" with all '
        f"{len(table.rows)} rows{notes}. Don't repeat them, and don't say they are missing.]"
    )


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
            context_id = specialist.context_id(ctx.deps.conversation_key)
            answer = await specialist.client.ask(question, context_id=context_id)
        except SpecialistError as exc:
            # Returned, not raised: the model can still reply ("docs are unavailable"), and
            # the failed call triggers escalation. A retry would just fail again.
            call.error = str(exc)
            log.warning(
                "specialist failed", extra={"specialist": specialist.name, "error": str(exc)}
            )
            return f"{specialist.name} is unavailable right now ({exc})."
        call.confidence = answer.confidence
        call.tables = parse_tables(answer.data)
        return answer.text + shown_note(call.tables)

    return Tool(
        ask, name=specialist.tool_name, description=specialist.tool_description, takes_ctx=True
    )


async def remember_my_team(ctx: RunContext[SupportDeps], team: str) -> str:
    """Save the user's team, used whenever they later say "my team", "we" or "us".

    Args:
        team: the team in the user's words, with club, league and age group when given,
            e.g. "IFA Homegrown U15".
    """
    deps = ctx.deps
    if deps.prefs is None or not deps.user_id:
        return "Saving a team isn't available here."
    try:
        saved = deps.prefs.set_my_team(deps.channel, deps.user_id, team)
    except ValueError:
        return "No team given: ask which team to save."
    log.info("my team saved", extra={"channel": deps.channel})
    return f"Saved: {saved}."


async def forget_my_team(ctx: RunContext[SupportDeps]) -> str:
    """Clear the user's saved team."""
    deps = ctx.deps
    if deps.prefs is None or not deps.user_id:
        return "Saving a team isn't available here."
    if deps.prefs.forget_my_team(deps.channel, deps.user_id):
        return "Forgotten."
    return "No team was saved."


def build_agent(
    model: Model | str | None = None, specialists: list[Specialist] | None = None
) -> Agent[SupportDeps, str]:
    """Build the support agent. `model` overrides SB_SUPPORT_MODEL / the default."""
    return Agent(
        model or resolve("sb-support", check_credentials=False).ref,
        deps_type=SupportDeps,
        instructions=[INSTRUCTIONS, my_team_instructions],
        name="sb-support",
        tools=[
            *(specialist_tool(s) for s in specialists or []),
            Tool(remember_my_team, takes_ctx=True),
            Tool(forget_my_team, takes_ctx=True),
        ],
    )
