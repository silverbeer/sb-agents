"""Routing eval scoring, offline: a fake core stands in for the router (SB-1548)."""

import pytest

from sb_support.core import InboundMessage, Reply
from sb_support.evals import CASES, case, run_evals


class FakeCore:
    def __init__(self, routes: dict[str, list[str]]) -> None:
        self.routes = routes

    async def handle(self, msg: InboundMessage) -> Reply:
        return Reply(text="ok", routed_to=self.routes[msg.text])


async def test_exact_specialist_set_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    cases = [
        case("one", "q1", "docs-agent"),
        case("both", "q2", "mt-agent", "docs-agent"),
        case("extra", "q3", "mt-agent"),
    ]
    core = FakeCore(
        {"q1": ["docs-agent"], "q2": ["docs-agent", "mt-agent"], "q3": ["mt-agent", "docs-agent"]}
    )
    rate = await run_evals(core, cases)  # type: ignore[arg-type]
    assert rate == pytest.approx(2 / 3)
    out = capsys.readouterr().out
    assert "✗ extra" in out and "2/3 routed as expected" in out


def test_cases_cover_rules_data_and_both() -> None:
    targets = {c.expected for c in CASES}
    assert frozenset({"docs-agent"}) in targets
    assert frozenset({"mt-agent"}) in targets
    assert frozenset({"mt-agent", "docs-agent"}) in targets
    assert len({c.name for c in CASES}) == len(CASES)
