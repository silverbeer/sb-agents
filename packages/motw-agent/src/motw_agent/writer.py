"""The one model step: a blurb and 3-5 "Why this is the Match of the Week" reasons.

The model writes; deterministic checks decide whether what it wrote may be shown.
`check_draft` is pure and is the agent's output validator: any problem goes back to
the model as a `ModelRetry`, and a draft that still fails after the retries is replaced
by `fallback_draft`, built straight from MT's fact texts. What the checks enforce
(docs/notes/motw-agent.md):

- **Grounded.** Every reason cites at least one fact id from this candidate, and its
  `kind` is the kind of a fact it cites. Every number in a reason appears in a cited
  fact; every number in the blurb appears in some fact (or the age group / date).
- **No absent-as-zero.** Follows from number grounding (MT never emits a fact for a
  stat it doesn't have), plus a ban on words that claim zero ("winless", "scoreless",
  ...) unless a cited fact says them.
- **No invented rankings.** A superlative in a reason ("most", "best", "toughest", ...)
  needs a cited fact using the same word.
- **No invented qualification claims.** Text that talks qualification (qualify,
  playoff, MLS NEXT Cup, Fest, bye, berth, ...) must cite a rule fact (reasons), or the
  candidate must have one (blurb).
- **Unconfirmed stays unconfirmed.** A reason citing a fact with `confirmed: false`
  must contain one of the HEDGE words. A blurb that talks qualification when no rule
  fact is confirmed must too.
- Limits from MT's API: reason text 1-200 chars, kind one of the five fact kinds,
  at most 6 reasons (we ask for 3-5), blurb at most 280 chars.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage, UsageLimits
from sb_models import resolve

from motw_agent.candidates import FACT_KINDS, FactKind, MotwCandidate

BLURB_MAX = 280
REASON_MAX = 200
MIN_REASONS, MAX_REASONS = 3, 5

# A writer run is 1 request, plus one per retry. Haiku: ~2-3k tokens, ~$0.002.
OUTPUT_RETRIES = 2
USAGE_LIMITS = UsageLimits(
    request_limit=1 + OUTPUT_RETRIES,
    total_tokens_limit=40_000,
    cost_limit=Decimal("0.03"),
)

# The deterministic "stated as unconfirmed" check: one of these, any case, as words.
HEDGE = re.compile(
    r"\b(unconfirmed|not yet confirmed|if confirmed|pending confirmation|reportedly|expected)\b",
    re.IGNORECASE,
)
# Talk of qualification / progression. Only a rule fact may back it.
QUALIFICATION = re.compile(
    r"qualif|play-?offs?|mls next cup|next cup|\bfest\b|showcase|\bbyes?\b|promot|relegat|"
    r"\bberths?\b|\bseed(?:ed|ing|s)?\b|advanc(?:e|es|ed|ing) to|\bnationals?\b",
    re.IGNORECASE,
)
# Words that state a zero. Allowed only when a cited fact says the same word.
ZERO_WORDS = re.compile(
    r"\b(zero|winless|pointless|scoreless|goalless|unbeaten|undefeated|perfect record)\b",
    re.IGNORECASE,
)
# Superlatives make a ranking claim ("the most dangerous attack in the division") that no
# fact supports. Allowed in a reason only when a cited fact uses the same word; the blurb
# may be as excited as it likes ("the best game of the weekend" is opinion, not a stat).
SUPERLATIVES = re.compile(
    r"\b(most|best|worst|fewest|least|highest|lowest|strongest|weakest|deadliest|stingiest|"
    r"toughest|meanest|unmatched|league-best|division-best)\b",
    re.IGNORECASE,
)
NUMBER = re.compile(r"\d+(?:\.\d+)?")


class Reason(BaseModel):
    text: str = Field(description="one punchy line, at most 200 characters")
    kind: FactKind = Field(description="the kind of the fact(s) it cites")
    fact_ids: list[str] = Field(description="ids of the facts this line is based on, at least one")


class Draft(BaseModel):
    blurb: str = Field(description="the hero line, at most 280 characters")
    reasons: list[Reason] = Field(description="3 to 5 reasons, strongest first")


INSTRUCTIONS = """\
You write the Match of the Week pitch for MissingTable, a youth soccer (MLS NEXT) site.
You get one fixture and the facts MissingTable knows about it, each with an id.

Write:
- `blurb`: one fired-up hero line (max 280 characters) that makes a parent want to watch.
- `reasons`: 3 to 5 "Why this is the Match of the Week" lines, strongest first, each max
  200 characters, each citing the fact ids it is based on, with `kind` = that fact's kind.

Rules, all checked by code; a draft that breaks one is sent back:
- Use ONLY the facts given. Every number you write must appear in a fact you cite, and a
  reason cites EVERY fact it draws on (position + rule = both ids).
- A record like (5-0-2) is wins-draws-losses. Quote it as written ("5-0-2"); don't
  spell it out in words.
- A stat with no fact is unknown, not zero. Never write "0 goals", "winless", "no points"
  or similar unless a fact says exactly that. Low-sample teams have played few games:
  say "early in the season" rather than guessing.
- Qualification, playoffs, MLS NEXT Cup, Fest, byes, berths, promotion: ONLY from a fact
  whose id contains ":rule:", and cite it. Never infer qualification from standings.
- A fact with "confirmed": false (its text starts "Unconfirmed:") may only be stated as
  unconfirmed: the line must say "unconfirmed", "reportedly" or "expected".
- The blurb follows the same rules: no stakes the facts don't state ("3rd place on the
  line" when a fact only gives the current position is an invented stake).
- No rankings the facts don't state: not "the most dangerous attack", "the best defence",
  "the toughest test". Bring the energy with verbs and stakes, not with new claims.
- Name teams as given. Energetic, never insulting to any team or player; no emojis.
"""


@dataclass
class Written:
    draft: Draft
    fallback: bool  # True: the model's draft failed the checks; built from facts instead
    usage: RunUsage | None = None
    problems: list[str] | None = None


def _numbers(text: str) -> set[str]:
    return {n.rstrip("0").rstrip(".") if "." in n else n for n in NUMBER.findall(text)}


def _context_numbers(c: MotwCandidate) -> set[str]:
    """Numbers the blurb may use besides facts: age group (U15), date, kickoff."""
    extra = f"{c.age_group.name} {c.match_date.day} {c.match_date.year} {c.scheduled_kickoff or ''}"
    return _numbers(extra)


def check_draft(c: MotwCandidate, d: Draft) -> list[str]:
    """Every rule the draft breaks, as instructions the model can act on. [] = ok."""
    problems: list[str] = []
    facts = {f.id: f for f in c.facts}
    all_fact_text = " ".join(f.text for f in c.facts)

    blurb = d.blurb.strip()
    if not blurb:
        problems.append("blurb is empty")
    elif len(blurb) > BLURB_MAX:
        problems.append(f"blurb is {len(blurb)} characters; max {BLURB_MAX}")
    stray = _numbers(blurb) - _numbers(all_fact_text) - _context_numbers(c)
    if stray:
        problems.append(f"blurb has numbers no fact states: {sorted(stray)}")
    if QUALIFICATION.search(blurb):
        rules = [f for f in c.facts if f.is_rule]
        if not rules:
            problems.append("blurb talks qualification but this fixture has no rule fact")
        elif not any(f.confirmed for f in rules) and not HEDGE.search(blurb):
            problems.append("blurb talks qualification from unconfirmed rules: say 'unconfirmed'")
    for word in {m.lower() for m in ZERO_WORDS.findall(blurb)}:
        if word not in all_fact_text.lower():
            problems.append(f"blurb says '{word}' but no fact does")

    want_min = min(MIN_REASONS, len(facts)) or 1
    if not want_min <= len(d.reasons) <= MAX_REASONS:
        problems.append(f"write {want_min} to {MAX_REASONS} reasons, not {len(d.reasons)}")

    for i, r in enumerate(d.reasons, 1):
        text = r.text.strip()
        where = f"reason {i}"
        if not text:
            problems.append(f"{where} is empty")
            continue
        if len(text) > REASON_MAX:
            problems.append(f"{where} is {len(text)} characters; max {REASON_MAX}")
        if r.kind not in FACT_KINDS:
            problems.append(f"{where}: kind must be one of {', '.join(FACT_KINDS)}")
        unknown = [f for f in r.fact_ids if f not in facts]
        if unknown:
            problems.append(f"{where} cites fact ids that don't exist: {unknown}")
        cited = [facts[f] for f in r.fact_ids if f in facts]
        if not cited:
            problems.append(f"{where} must cite at least one real fact id")
            continue
        if r.kind not in {f.kind for f in cited}:
            problems.append(f"{where}: kind '{r.kind}' is not the kind of any fact it cites")
        cited_text = " ".join(f.text for f in cited)
        stray = _numbers(text) - _numbers(cited_text)
        if stray:
            problems.append(f"{where} has numbers its cited facts don't state: {sorted(stray)}")
        if QUALIFICATION.search(text) and not any(f.is_rule for f in cited):
            problems.append(f"{where} talks qualification without citing a ':rule:' fact")
        if any(f.confirmed is False for f in cited) and not HEDGE.search(text):
            problems.append(
                f"{where} cites an unconfirmed rule: it must say 'unconfirmed', "
                "'reportedly' or 'expected'"
            )
        for word in {m.lower() for m in ZERO_WORDS.findall(text)}:
            if word not in cited_text.lower():
                problems.append(f"{where} says '{word}' but no cited fact does")
        for word in {m.lower() for m in SUPERLATIVES.findall(text)}:
            if word not in cited_text.lower():
                problems.append(
                    f"{where} says '{word}': a ranking no cited fact states. Drop the comparison"
                )
    return problems


def prompt_for(c: MotwCandidate) -> str:
    """The candidate as the model sees it: what it may use, nothing else."""

    def team(t: Any) -> dict[str, Any]:
        out = {"name": t.name, "games_played": t.played, "low_sample": t.low_sample}
        if t.position is not None and t.table_size:
            out["position"] = f"{t.position} of {t.table_size}"
        if t.points is not None:
            out["points"] = t.points
        return out

    data = {
        "fixture": c.label,
        "competition": c.competition,
        "table": c.table,
        "date": c.match_date.strftime("%A %b %-d"),
        "kickoff": c.scheduled_kickoff,
        "home": team(c.home),
        "away": team(c.away),
        "flags": c.flags,
        "facts": [
            {"id": f.id, "kind": f.kind, "text": f.text}
            | ({"confirmed": f.confirmed} if f.confirmed is not None else {})
            for f in c.facts
        ],
    }
    return json.dumps(data, indent=1)


def build_writer(model: Model | str | None = None) -> Agent[MotwCandidate, Draft]:
    agent = Agent(
        model or resolve("motw-agent", check_credentials=False).ref,
        deps_type=MotwCandidate,
        output_type=Draft,
        instructions=INSTRUCTIONS,
        retries=OUTPUT_RETRIES,
        name="motw-agent",
    )

    @agent.output_validator
    def grounded(ctx: RunContext[MotwCandidate], out: Draft) -> Draft:
        problems = check_draft(ctx.deps, out)
        if problems:
            raise ModelRetry("Fix these and resend the whole draft:\n- " + "\n- ".join(problems))
        return out

    return agent


# Order the fallback uses facts in: the matchup first, then form and stakes.
_FALLBACK_ORDER = ("gap", "pos", "h2h", "streak", "rule", "common", "rate", "left")


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def fallback_draft(c: MotwCandidate) -> Draft:
    """A plain draft from MT's own fact texts: always passes `check_draft`."""

    def rank(fact_id: str) -> int:
        slug = fact_id.split(":", 2)[1] if ":" in fact_id else ""
        return _FALLBACK_ORDER.index(slug) if slug in _FALLBACK_ORDER else len(_FALLBACK_ORDER)

    facts = sorted(c.facts, key=lambda f: rank(f.id))[:MAX_REASONS]
    reasons = [Reason(text=_clip(f.text, REASON_MAX), kind=f.kind, fact_ids=[f.id]) for f in facts]
    blurb = f"{c.label}: {c.competition}, {c.table}."
    return Draft(blurb=_clip(blurb, BLURB_MAX), reasons=reasons)


async def write(agent: Agent[MotwCandidate, Draft], c: MotwCandidate) -> Written:
    """The model's draft, or the fallback when it can't produce a valid one."""
    if not c.facts:
        return Written(fallback_draft(c), fallback=True, problems=["no facts"])
    usage = RunUsage()  # filled as the run goes, so a failed run is still counted
    try:
        result = await agent.run(prompt_for(c), deps=c, usage_limits=USAGE_LIMITS, usage=usage)
    except Exception as exc:  # noqa: BLE001 - retries exhausted, limit, provider: fall back
        return Written(fallback_draft(c), fallback=True, usage=usage, problems=[repr(exc)[:300]])
    draft = result.output
    # Normalise: trimmed text, at most MAX_REASONS (the validator already enforced it).
    draft = Draft(
        blurb=draft.blurb.strip(),
        reasons=[r.model_copy(update={"text": r.text.strip()}) for r in draft.reasons],
    )
    return Written(draft, fallback=False, usage=usage)
