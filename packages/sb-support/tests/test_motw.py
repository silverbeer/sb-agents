import httpx
import pytest

from sb_support.motw import (
    Candidate,
    Command,
    DropReason,
    GrammarError,
    MotwApi,
    MotwApiError,
    Reason,
    SetBlurb,
    SetReason,
    Suggestion,
    apply_edits,
    parse,
)

C2 = Candidate(
    2,
    4096,
    "IFA vs NEFC",
    "U15 Homegrown Northeast",
    "Top two collide!",
    (
        Reason("1 point apart.", "standings"),
        Reason("IFA 5 unbeaten.", "form"),
        Reason("NEFC won 2-1 in Sep.", "rivalry"),
    ),
)


@pytest.mark.parametrize(
    ("text", "cmd"),
    [
        ("skip", Command("skip")),
        ("Skip", Command("skip")),
        ("2", Command("pick", 2)),
        ("#2", Command("pick", 2)),
        ("pick 2", Command("pick", 2)),
        ("PICK #3", Command("pick", 3)),
        ("pick 2 blurb: Huge one!", Command("pick", 2, (SetBlurb("Huge one!"),))),
        ("edit 1 drop reason 3", Command("edit", 1, (DropReason(3),))),
        (
            "edit 2\nblurb: New\nreason 1: Level on points.\nreason 4 rivalry: Derby day.\n"
            "drop reason 2",
            Command(
                "edit",
                2,
                (
                    SetBlurb("New"),
                    SetReason(1, "Level on points."),
                    SetReason(4, "Derby day.", "rivalry"),
                    DropReason(2),
                ),
            ),
        ),
        ("pick 1\nreason 2: Better line", Command("pick", 1, (SetReason(2, "Better line"),))),
    ],
)
def test_the_grammar(text: str, cmd: Command) -> None:
    assert parse(text) == cmd


@pytest.mark.parametrize("text", ["", "great game!", "who's playing?", "lol", "picked up"])
def test_chatter_is_not_a_command(text: str) -> None:
    assert parse(text) is None


@pytest.mark.parametrize(
    ("text", "hint"),
    [
        ("pick two", "MOTW replies"),
        ("reason 1: hi", "Say which candidate"),
        ("edit 2", "Nothing to change"),
        ("edit 2\nreason 1 vibes: hi", "kind is one of"),
        ("pick 2\nmake it louder", "don't understand"),
        ("2 is the one", "MOTW replies"),
    ],
)
def test_malformed_commands_explain_themselves(text: str, hint: str) -> None:
    with pytest.raises(GrammarError, match=hint):
        parse(text)


def test_edits_refer_to_the_list_as_shown() -> None:
    cmd = parse("edit 2\nreason 3: Rematch!\ndrop reason 1\nreason 4 stakes: Big stakes.")
    assert cmd is not None
    c = apply_edits(C2, cmd.edits)
    assert [(r.text, r.kind) for r in c.reasons] == [
        ("IFA 5 unbeaten.", "form"),
        ("Rematch!", "rivalry"),  # replaced reasons keep their kind
        ("Big stakes.", "stakes"),
    ]
    assert c.blurb == C2.blurb and c.match_id == 4096


@pytest.mark.parametrize(
    ("edit", "hint"),
    [
        (SetReason(6, "x"), "change 1-3, or add #4"),
        (DropReason(4), "no reason 4"),
        (SetBlurb("x" * 281), "max 280"),
        (SetBlurb(""), "can't be empty"),
        (SetReason(1, "x" * 201), "1-200"),
    ],
)
def test_bad_edits_are_refused(edit, hint: str) -> None:
    with pytest.raises(GrammarError, match=hint):
        apply_edits(C2, (edit,))


def test_at_most_six_reasons() -> None:
    edits = tuple(SetReason(k, f"r{k}") for k in range(4, 8))
    with pytest.raises(GrammarError, match="At most 6"):
        apply_edits(C2, edits)


def test_new_reasons_default_to_storyline() -> None:
    assert apply_edits(C2, (SetReason(4, "x"),)).reasons[-1].kind == "storyline"


def test_the_suggestion_round_trips_and_keeps_unknown_fields() -> None:
    s = Suggestion("2026-11-09", (C2,), "open", {"pending": {"n": 2}})
    meta = s.to_metadata()
    assert meta["event_type"] == "motw_suggestion"
    assert meta["event_payload"]["v"] == 1
    assert Suggestion.from_metadata(meta) == s


@pytest.mark.parametrize(
    "meta",
    [
        None,
        {"event_type": "something_else", "event_payload": {}},
        {"event_type": "motw_suggestion", "event_payload": {"v": 2, "week_start": "x"}},
        {"event_type": "motw_suggestion", "event_payload": {"v": 1, "candidates": [{"n": 1}]}},
    ],
)
def test_other_or_broken_metadata_is_not_a_suggestion(meta) -> None:
    assert Suggestion.from_metadata(meta) is None


# --- MT -------------------------------------------------------------------------------


def api(handler, token: str = "secret-token") -> MotwApi:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return MotwApi("https://mt.test", token=token, http=http)


async def test_the_week_check_reads_the_public_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json={"week_start": "2026-11-09", "motw": None})

    assert await api(handler).current_pick("2026-11-09") is None
    assert str(seen[0].url) == "https://mt.test/api/motw?week_start=2026-11-09"
    assert "authorization" not in seen[0].headers


async def test_a_pick_posts_match_blurb_and_reasons_with_the_motw_token() -> None:
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json={"week_start": "2026-11-09"})

    await api(handler).set_pick(C2)
    import json

    req = seen[0]
    assert req.method == "POST" and req.url.path == "/api/admin/motw"
    assert req.headers["authorization"] == "Bearer secret-token"
    assert json.loads(req.content) == {
        "match_id": 4096,
        "blurb": "Top two collide!",
        "reasons": [r.as_dict() for r in C2.reasons],
    }


async def test_mt_errors_and_a_missing_token_are_api_errors() -> None:
    with pytest.raises(MotwApiError, match="HTTP 403"):
        await api(lambda r: httpx.Response(403, text="forbidden")).set_pick(C2)
    with pytest.raises(MotwApiError, match="not set"):
        await api(lambda r: httpx.Response(200), token="").set_pick(C2)
    assert "secret" not in repr(api(lambda r: httpx.Response(200)))
