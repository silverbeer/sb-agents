import pytest
from motw_fakes import good_draft, scripted_writer

from motw_agent import fixtures
from motw_agent.writer import Draft, check_draft, fallback_draft, write


def draft(blurb: str, *reasons: tuple[str, str, list[str]]) -> Draft:
    return Draft.model_validate(
        {"blurb": blurb, "reasons": [{"text": t, "kind": k, "fact_ids": f} for t, k, f in reasons]}
    )


TOP = fixtures.candidate("top-clash")
OK_REASONS = (
    ("First hosts second: 1 point in it.", "standings", ["4096:gap"]),
    ("IFA haven't lost in 5.", "form", ["4096:streak:home"]),
    ("NEFC won the last meeting 2-1.", "rivalry", ["4096:h2h"]),
)


def test_a_grounded_draft_passes() -> None:
    assert check_draft(TOP, draft("Top of the table, live.", *OK_REASONS)) == []


@pytest.mark.parametrize("name", list(fixtures.ALL))
def test_the_fixture_shaped_good_draft_passes(name: str) -> None:
    c = fixtures.candidate(name)
    assert check_draft(c, Draft.model_validate(good_draft(c))) == []


@pytest.mark.parametrize(
    ("reason", "problem"),
    [
        (("Big game.", "standings", ["4096:nope"]), "don't exist"),
        (("Big game.", "standings", []), "at least one real fact"),
        (("IFA have 20 points.", "rivalry", ["4096:pos:home"]), "not the kind"),
        (("x" * 201, "standings", ["4096:gap"]), "max 200"),
        (("IFA have 21 points.", "standings", ["4096:pos:home"]), "['21']"),
        (("IFA will qualify for the playoffs.", "standings", ["4096:pos:home"]), "qualification"),
        (("NEFC are winless away.", "standings", ["4096:pos:away"]), "'winless'"),
        (("IFA have the best attack around.", "standings", ["4096:pos:home"]), "'best'"),
    ],
)
def test_a_bad_reason_is_named(reason: tuple, problem: str) -> None:
    problems = check_draft(TOP, draft("Top of the table.", *OK_REASONS[:2], reason))
    assert any(problem in p for p in problems), problems


def test_reasons_must_be_3_to_5() -> None:
    problems = check_draft(TOP, draft("Top of the table.", *OK_REASONS[:2]))
    assert any("3 to 5" in p for p in problems)


def test_the_blurb_is_checked_too() -> None:
    assert any("max 280" in p for p in check_draft(TOP, draft("x" * 281, *OK_REASONS)))
    assert any("['99']" in p for p in check_draft(TOP, draft("99 goals!", *OK_REASONS)))
    # Age group, kickoff and date are fair game.
    assert check_draft(TOP, draft("U15 at 10:00 on the 14th.", *OK_REASONS)) == []


def test_qualification_talk_needs_a_rule_fact() -> None:
    c = fixtures.candidate("no-rules")  # top of table, qualifying_line flag, but no rule
    reasons = [
        ("Boston Bolts sit 1st with 18 points.", "standings", ["7300:pos:home"]),
        ("Hartford sit 3rd with 15 points.", "standings", ["7300:pos:away"]),
        ("Hartford have won 4 in a row.", "form", ["7300:streak:away"]),
    ]
    assert check_draft(c, draft("A playoff preview!", *reasons))
    assert check_draft(c, draft("First against third.", *reasons)) == []


def test_an_unconfirmed_rule_must_be_stated_as_unconfirmed() -> None:
    c = fixtures.candidate("unconfirmed-rule")
    base = [
        ("1 point in it.", "standings", ["6200:gap"]),
        ("Westchester sit 4th.", "standings", ["6200:pos:home"]),
    ]
    flat = ("A top-4 finish earns a Fest bye.", "stakes", ["6200:rule:hg-fest-bye"])
    hedged = ("A top-4 finish is expected to earn a Fest bye.", "stakes", ["6200:rule:hg-fest-bye"])
    assert any("unconfirmed rule" in p for p in check_draft(c, draft("Tight one.", *base, flat)))
    assert check_draft(c, draft("Tight one.", *base, hedged)) == []
    # A blurb talking qualification from only-unconfirmed rules must hedge as well.
    assert check_draft(c, draft("A Fest bye on the line!", *base, hedged))
    assert check_draft(c, draft("A Fest bye reportedly on the line!", *base, hedged)) == []


@pytest.mark.parametrize("name", list(fixtures.ALL))
def test_the_fallback_always_passes(name: str) -> None:
    c = fixtures.candidate(name)
    d = fallback_draft(c)
    assert check_draft(c, d) == []


async def test_a_rejected_draft_is_retried_with_the_problems() -> None:
    bad = good_draft(TOP) | {"reasons": [{"text": "x", "kind": "form", "fact_ids": ["4096:zz"]}]}
    writer, seen = scripted_writer(bad, good_draft(TOP))
    w = await write(writer, TOP)
    assert not w.fallback and len(seen) == 2
    retry = str(seen[1][-1])
    assert "don't exist" in retry and "4096:zz" in retry
    assert w.usage is not None and w.usage.requests == 2


async def test_a_draft_that_keeps_failing_falls_back_to_facts() -> None:
    bad = good_draft(TOP) | {"blurb": "x" * 300}
    writer, seen = scripted_writer(bad, bad, bad)
    w = await write(writer, TOP)
    assert w.fallback and len(seen) == 3
    assert check_draft(TOP, w.draft) == []
    assert w.usage is not None and w.usage.requests == 3  # still counted


async def test_no_facts_means_no_model_call() -> None:
    c = TOP.model_copy(update={"facts": []})
    writer, seen = scripted_writer()
    w = await write(writer, c)
    assert w.fallback and not seen and w.draft.reasons == []


def test_the_prompt_never_shows_absent_stats() -> None:
    from motw_agent.writer import prompt_for

    prompt = prompt_for(fixtures.candidate("low-sample"))
    away = prompt.split('"away"')[1].split("}")[0]
    assert '"games_played": 0' in away
    assert "points" not in away and "position" not in away  # unknown, not zero
