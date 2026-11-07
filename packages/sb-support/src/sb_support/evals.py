"""Routing eval: does sb-support ask the right specialist? (SB-1548)

    SB_SUPPORT_SPECIALISTS=http://...:8101,http://...:8102 uv run sb-support-eval

Each case is a question and the set of specialists it must reach; the eval runs the real
router against live specialists and compares `Reply.routed_to`. It checks routing only:
answer quality is docs-agent's and mt-agent's own evals. One run costs every specialist's
answers too, ~$0.05.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

from sb_support.agent import build_agent
from sb_support.core import InboundMessage, SupportCore
from sb_support.specialists import discover

PASS_THRESHOLD = 0.8


@dataclass(frozen=True)
class RoutingCase:
    name: str
    question: str
    expected: frozenset[str]  # exactly these specialists


def case(name: str, question: str, *expected: str) -> RoutingCase:
    return RoutingCase(name, question, frozenset(expected))


CASES = [
    # Rules are documented, not data (SB-1548): docs-agent.
    case(
        "fest-cup", "Does playing at MLS NEXT Fest qualify a team for MLS NEXT Cup?", "docs-agent"
    ),
    case("flex-win", "What does winning a group at MLS NEXT Flex get you?", "docs-agent"),
    case("cup-routes", "How does a team qualify for MLS NEXT Cup?", "docs-agent"),
    # Live data: mt-agent.
    case("standings", "What are the U14 HG standings in the Northeast?", "mt-agent"),
    case("next-game", "When does IFA U15 play next?", "mt-agent"),
    case("flex-conference", "Which Flex conference is IFA U15 in?", "mt-agent"),
    case("live-score", "What's the score of the IFA U15 HG match? Who scored?", "mt-agent"),
    # A team against the rule: both.
    case(
        "team-qualify",
        "Can IFA U15 still qualify for MLS NEXT Cup?",
        "mt-agent",
        "docs-agent",
    ),
    # App how-to: docs-agent.
    case("invite", "How do I get invited to join a team on Missing Table?", "docs-agent"),
]


@dataclass
class Result:
    case: RoutingCase
    routed: frozenset[str]

    @property
    def passed(self) -> bool:
        return self.routed == self.case.expected


async def run_case(core: SupportCore, c: RoutingCase) -> Result:
    reply = await core.handle(
        InboundMessage(channel="eval", conversation_id=c.name, text=c.question)
    )
    return Result(c, frozenset(reply.routed_to))


async def run_evals(core: SupportCore | None = None, cases: list[RoutingCase] = CASES) -> float:
    if core is None:
        core = SupportCore(build_agent(specialists=await discover()))
    results = await asyncio.gather(*(run_case(core, c) for c in cases))
    for r in results:
        mark = "✔" if r.passed else "✗"
        print(
            f"{mark} {r.case.name:16} expected {sorted(r.case.expected)}, routed {sorted(r.routed)}"
        )
    passed = sum(r.passed for r in results)
    print(f"\n{passed}/{len(results)} routed as expected; threshold {PASS_THRESHOLD:.0%}")
    return passed / len(results) if results else 0.0


def main() -> None:
    rate = asyncio.run(run_evals())
    sys.exit(0 if rate >= PASS_THRESHOLD else 1)
