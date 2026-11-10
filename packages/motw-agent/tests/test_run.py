import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
from motw_fakes import FakeMt, fake_slack, scripted_writer, writer_by_candidate

from motw_agent import fixtures
from motw_agent.mt import MtError, parse_week, week_start
from motw_agent.run import EVAL_DID_NOT_COMPLETE, Out, remind, suggest
from motw_agent.slack_post import EVENT_TYPE, find_suggestion

WEEK = date(2026, 11, 9)
PICKED = {
    "week_start": "2026-11-09",
    "match": {"id": 1, "home_team_name": "A", "away_team_name": "B"},
}


def writer_for(*names: str):
    return writer_by_candidate(*(fixtures.candidate(n) for n in names))


def out(client=None) -> Out:
    return Out(client or fake_slack(), "C_PICKS")


def test_weeks_are_monday_to_sunday() -> None:
    assert week_start(date(2026, 11, 15)) == WEEK  # Sunday
    assert week_start(WEEK) == WEEK
    assert parse_week("2026-11-12") == WEEK


async def test_an_already_picked_week_gets_a_note_and_nothing_else() -> None:
    mt = FakeMt(pick=PICKED)
    writer, seen = scripted_writer()
    o = out()
    assert await suggest(WEEK, mt, writer, o) == "already_picked"
    assert mt.calls == [("current_pick", WEEK)] and not seen
    [msg] = o.sent
    assert "already picked (A vs B)" in msg["text"] and "blocks" not in msg


async def test_a_pick_mt_reports_with_the_candidates_is_respected_too() -> None:
    mt = FakeMt(result=fixtures.result("top-clash", current_pick={"match_id": 4096}))
    writer, seen = scripted_writer()
    o = out()
    assert await suggest(WEEK, mt, writer, o) == "already_picked"
    assert not seen and "blocks" not in o.sent[0]


async def test_nothing_eligible_says_why() -> None:
    mt = FakeMt(result=fixtures.result(candidates=[], reason="no fixtures this week"))
    o = out()
    assert await suggest(WEEK, mt, scripted_writer()[0], o) == "none"
    assert "no fixtures this week" in o.sent[0]["text"]


@pytest.mark.parametrize(
    "mt",
    [
        FakeMt(pick=MtError("GET /api/motw: 502")),
        FakeMt(result=MtError("mcp down")),
        FakeMt(
            result=fixtures.result(candidates=None, error={"kind": "unavailable", "message": "db"})
        ),
    ],
)
async def test_mt_failures_post_a_generic_note(mt: FakeMt) -> None:
    o = out()
    assert await suggest(WEEK, mt, scripted_writer()[0], o) == "error"
    [msg] = o.sent
    assert "logged" in msg["text"]
    assert "502" not in msg["text"] and "mcp" not in msg["text"] and "db" not in msg["text"]


async def test_the_top_three_are_posted_with_buttons_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sb_obs import runs

    records: list = []
    monkeypatch.setattr(runs, "emit", records.append)
    names = ("top-clash", "low-sample", "unconfirmed-rule", "no-rules")
    writer, seen = writer_for(*names[:3])
    client = fake_slack()
    assert (
        await suggest(WEEK, FakeMt(result=fixtures.result(*names)), writer, out(client)) == "posted"
    )
    assert len(seen) == 3  # one writer call per candidate; the 4th isn't written

    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["channel"] == "C_PICKS"
    meta = kwargs["metadata"]
    assert meta["event_type"] == EVENT_TYPE
    payload = meta["event_payload"]
    assert payload["v"] == 1 and payload["week_start"] == "2026-11-09"
    assert payload["status"] == "open"
    assert [c["match_id"] for c in payload["candidates"]] == [4096, 5120, 6200]
    first = payload["candidates"][0]
    assert first["n"] == 1 and first["blurb"] and first["label"] == "IFA vs NEFC"
    assert set(first["reasons"][0]) == {"text", "kind"}

    blocks = {b.get("block_id"): b for b in kwargs["blocks"]}
    actions = blocks["motw_actions"]["elements"]
    assert [a["action_id"] for a in actions] == [
        "motw_pick_1",
        "motw_pick_2",
        "motw_pick_3",
        "motw_skip",
    ]
    assert "motw_draft_2" in blocks
    head2 = blocks["motw_cand_2"]["text"]["text"]
    assert "Low sample: Bayside FC (0 games)" in head2 and "not in the table yet" in head2
    assert "U15 Homegrown Northeast" in blocks["motw_cand_1"]["text"]["text"]
    assert len(kwargs["text"]) < 300  # notification fallback

    [record] = records
    assert (record.agent, record.caller, record.requests) == ("motw-agent", "cron", 3)


async def test_a_fallback_draft_is_marked_in_the_message() -> None:
    bad = {"blurb": "x" * 300, "reasons": []}
    writer, _ = scripted_writer(bad, bad, bad)
    client = fake_slack()
    await suggest(WEEK, FakeMt(result=fixtures.result("top-clash")), writer, out(client))
    draft = next(
        b
        for b in client.chat_postMessage.await_args.kwargs["blocks"]
        if b.get("block_id") == "motw_draft_1"
    )
    assert "Plain facts" in draft["text"]["text"]


async def test_dry_run_prints_instead_of_posting(capsys: pytest.CaptureFixture) -> None:
    client = fake_slack()
    o = Out(client, "C_PICKS", dry_run=True)
    writer, _ = writer_for("top-clash")
    await suggest(WEEK, FakeMt(result=fixtures.result("top-clash")), writer, o)
    client.chat_postMessage.assert_not_awaited()
    assert '"motw_pick_1"' in capsys.readouterr().out


# --- the eval after the post (SB-1620) ------------------------------------------------


def evaluator(line: str = "Eval: 1/1 passed.") -> tuple[AsyncMock, Out, MagicMock]:
    client = fake_slack()
    return AsyncMock(return_value=line), out(client), client


async def test_the_posted_drafts_are_evaluated_and_reported_in_the_thread() -> None:
    evaluate, o, client = evaluator("Eval: 0/1 passed. #1 failed LLMJudge: unsupported.")
    writer, _ = writer_for("top-clash")
    mt = FakeMt(result=fixtures.result("top-clash"))
    assert await suggest(WEEK, mt, writer, o, evaluate=evaluate) == "posted"

    [(posted, model)] = [c.args for c in evaluate.await_args_list]
    assert [c.match_id for c, _ in posted] == [4096] and model == "claude-haiku-5-5"
    first, reply = client.chat_postMessage.await_args_list
    assert "thread_ts" not in first.kwargs and reply.kwargs["thread_ts"] == "999.1"
    assert reply.kwargs["text"] == "Eval: 0/1 passed. #1 failed LLMJudge: unsupported."


async def test_an_eval_error_says_so_and_the_run_still_succeeds() -> None:
    evaluate, o, client = evaluator()
    evaluate.side_effect = RuntimeError("anthropic 529 secret detail")
    writer, _ = writer_for("top-clash")
    mt = FakeMt(result=fixtures.result("top-clash"))
    assert await suggest(WEEK, mt, writer, o, evaluate=evaluate) == "posted"
    assert client.chat_postMessage.await_count == 2  # the post once, the note once
    assert client.chat_postMessage.await_args.kwargs["text"] == EVAL_DID_NOT_COMPLETE


async def test_a_hung_eval_is_cut_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("motw_agent.run.EVAL_TIMEOUT_S", 0.01)

    async def hang(posted: list, model: str) -> str:
        await asyncio.sleep(30)
        return "never"

    client = fake_slack()
    writer, _ = writer_for("top-clash")
    mt = FakeMt(result=fixtures.result("top-clash"))
    assert await suggest(WEEK, mt, writer, out(client), evaluate=hang) == "posted"
    assert client.chat_postMessage.await_args.kwargs["text"] == EVAL_DID_NOT_COMPLETE


async def test_failing_to_post_the_eval_result_does_not_fail_the_run() -> None:
    evaluate, o, client = evaluator()
    client.chat_postMessage.side_effect = [{"ok": True, "ts": "999.1"}, RuntimeError("slack")]
    writer, _ = writer_for("top-clash")
    mt = FakeMt(result=fixtures.result("top-clash"))
    assert await suggest(WEEK, mt, writer, o, evaluate=evaluate) == "posted"


async def test_nothing_posted_means_nothing_evaluated() -> None:
    evaluate, _, client = evaluator()
    o = Out(client, "C_PICKS", dry_run=True)
    writer, _ = writer_for("top-clash")
    await suggest(WEEK, FakeMt(result=fixtures.result("top-clash")), writer, o, evaluate=evaluate)
    evaluate.assert_not_awaited()

    await suggest(WEEK, FakeMt(pick=PICKED), scripted_writer()[0], out(client), evaluate=evaluate)
    evaluate.assert_not_awaited()


async def test_the_thursday_fallback_post_is_evaluated_too() -> None:
    evaluate, o, _ = evaluator()
    writer, _ = writer_for("top-clash")
    mt = FakeMt(result=fixtures.result("top-clash"))
    assert await remind(WEEK, mt, writer, o, ["U1"], evaluate=evaluate) == "posted"
    evaluate.assert_awaited_once()


# --- remind ---------------------------------------------------------------------------


def suggestion(ts: str, week: str = "2026-11-09", status: str = "open") -> dict:
    return {
        "ts": ts,
        "metadata": {
            "event_type": EVENT_TYPE,
            "event_payload": {"v": 1, "week_start": week, "status": status, "candidates": []},
        },
    }


async def test_remind_does_nothing_once_picked() -> None:
    client = fake_slack([suggestion("100.1")])
    assert (
        await remind(WEEK, FakeMt(pick=PICKED), scripted_writer()[0], out(client), ["U1"])
        == "picked"
    )
    client.chat_postMessage.assert_not_awaited()


async def test_remind_replies_in_the_suggestion_thread_and_mentions_approvers() -> None:
    client = fake_slack([{"ts": "150.1", "text": "chatter"}, suggestion("100.1")])
    outcome = await remind(WEEK, FakeMt(), scripted_writer()[0], out(client), ["U1", "U2"])
    assert outcome == "reminded"
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["thread_ts"] == "100.1"
    assert kwargs["text"].startswith("<@U1> <@U2> Reminder")
    history = client.conversations_history.await_args.kwargs
    assert history["include_all_metadata"] is True and history["channel"] == "C_PICKS"


async def test_remind_respects_a_skip() -> None:
    client = fake_slack([suggestion("100.1", status="skipped")])
    assert await remind(WEEK, FakeMt(), scripted_writer()[0], out(client), ["U1"]) == "skipped"
    client.chat_postMessage.assert_not_awaited()


async def test_remind_posts_fresh_suggestions_when_tuesday_is_missing() -> None:
    client = fake_slack([suggestion("100.1", week="2026-11-02")])  # last week's
    writer, _ = writer_for("top-clash")
    mt = FakeMt(result=fixtures.result("top-clash"))
    assert await remind(WEEK, mt, writer, out(client), ["U1"]) == "posted"
    assert "thread_ts" not in client.chat_postMessage.await_args.kwargs
    assert client.chat_postMessage.await_args.kwargs["metadata"]["event_type"] == EVENT_TYPE


async def test_find_suggestion_takes_the_newest_for_the_week_across_pages() -> None:
    client = fake_slack()
    client.conversations_history.side_effect = [
        {"messages": [suggestion("100.1")], "response_metadata": {"next_cursor": "c2"}},
        {"messages": [suggestion("200.1"), {"ts": "300.1"}], "response_metadata": {}},
    ]
    found = await find_suggestion(client, "C_PICKS", WEEK)
    assert found is not None and found["ts"] == "200.1"
    assert client.conversations_history.await_args_list[1].kwargs["cursor"] == "c2"
