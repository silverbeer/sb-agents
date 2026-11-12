"""MOTW approval through Slack, with a fake Slack workspace and a fake MT."""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sb_support.core import SupportCore
from sb_support.motw import Candidate, MotwApiError, Reason, Suggestion
from sb_support.slack_motw import (
    CHECK_FAILED,
    NOT_ALLOWED,
    SAVE_FAILED,
    MotwConfig,
    MotwSlack,
)

CHANNEL, APPROVER, OTHER, SUGG_TS = "C_PICKS", "U_OWNER", "U_RANDO", "100.1"


def suggestion() -> Suggestion:
    def cand(n: int, match_id: int, label: str) -> Candidate:
        return Candidate(
            n,
            match_id,
            label,
            "U15 Homegrown Northeast",
            f"Blurb {n}",
            (Reason(f"r{n}a", "standings"), Reason(f"r{n}b", "form"), Reason(f"r{n}c", "stakes")),
        )

    return Suggestion(
        "2026-11-09",
        (
            cand(1, 4096, "IFA vs NEFC"),
            cand(2, 5120, "Seacoast vs Bayside"),
            cand(3, 6200, "FCW vs Met Oval"),
        ),
    )


def message() -> dict[str, Any]:
    s = suggestion()
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": "MOTW"}}
    ]
    for c in s.candidates:
        blocks += [
            {
                "type": "section",
                "block_id": f"motw_cand_{c.n}",
                "text": {"type": "mrkdwn", "text": c.label},
            },
            {
                "type": "section",
                "block_id": f"motw_draft_{c.n}",
                "text": {"type": "mrkdwn", "text": "draft"},
            },
        ]
    blocks.append({"type": "actions", "block_id": "motw_actions", "elements": []})
    return {"ts": SUGG_TS, "text": "MOTW candidates", "blocks": blocks, "metadata": s.to_metadata()}


class FakeSlack:
    """One channel's top-level messages; chat.update edits them in place."""

    def __init__(self) -> None:
        self.messages = {SUGG_TS: message()}
        self.posted: list[dict[str, Any]] = []
        self.ephemeral: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.reactions: list[dict[str, Any]] = []

    async def conversations_history(self, **kw: Any) -> dict[str, Any]:
        assert kw["include_all_metadata"] is True and kw["inclusive"] is True
        msg = self.messages.get(kw["latest"])
        return {"ok": True, "messages": [copy.deepcopy(msg)] if msg else []}

    async def chat_update(self, **kw: Any) -> dict[str, Any]:
        self.updates.append(kw)
        if kw["ts"] in self.messages:
            msg = self.messages[kw["ts"]]
            msg["blocks"] = kw["blocks"]
            if "metadata" in kw:
                msg["metadata"] = kw["metadata"]
        return {"ok": True}

    async def chat_postMessage(self, **kw: Any) -> dict[str, Any]:
        self.posted.append(kw)
        return {"ok": True, "ts": f"200.{len(self.posted)}"}

    async def chat_postEphemeral(self, **kw: Any) -> dict[str, Any]:
        self.ephemeral.append(kw)
        return {"ok": True}

    async def reactions_add(self, **kw: Any) -> dict[str, Any]:
        self.reactions.append(kw)
        return {"ok": True}

    # helpers
    def suggestion(self) -> Suggestion:
        s = Suggestion.from_metadata(self.messages[SUGG_TS]["metadata"])
        assert s is not None
        return s

    def block(self, block_id: str) -> dict[str, Any] | None:
        return next(
            (b for b in self.messages[SUGG_TS]["blocks"] if b.get("block_id") == block_id), None
        )


class FakeApi:
    def __init__(self, existing: dict | None = None, fail: str | None = None) -> None:
        self.existing, self.fail = existing, fail
        self.writes: list[Candidate] = []
        self.checks: list[str] = []
        self.slots: list[dict | None] = []

    async def current_pick(self, week_start: str, slot: dict | None = None) -> dict | None:
        self.checks.append(week_start)
        self.slots.append(slot)
        if self.fail == "check":
            raise MotwApiError("GET /api/motw: HTTP 502")
        return self.existing

    async def set_pick(self, c: Candidate) -> dict:
        if self.fail == "write":
            raise MotwApiError("POST /api/admin/motw: HTTP 403 token expired")
        self.writes.append(c)
        return {}


def motw(api: FakeApi | None = None) -> MotwSlack:
    return MotwSlack(MotwConfig(CHANNEL, frozenset({APPROVER})), api or FakeApi())  # type: ignore[arg-type]


def click(action_id: str, user: str = APPROVER, *, value: str = "", on: str = SUGG_TS) -> dict:
    container = {"message_ts": on, "channel_id": CHANNEL}
    if on != SUGG_TS:
        container["thread_ts"] = SUGG_TS
    return {
        "type": "block_actions",
        "user": {"id": user},
        "channel": {"id": CHANNEL},
        "container": container,
        "actions": [{"action_id": action_id, "value": value}],
    }


def reply(text: str, user: str = APPROVER, ts: str = "150.1") -> dict:
    return {
        "type": "message",
        "channel": CHANNEL,
        "user": user,
        "text": text,
        "ts": ts,
        "thread_ts": SUGG_TS,
    }


# --- buttons --------------------------------------------------------------------------


async def test_pick_button_writes_and_marks_the_message() -> None:
    api, slack = FakeApi(), FakeSlack()
    await motw(api).on_action(click("motw_pick_2"), slack)

    assert api.checks == ["2026-11-09"]
    [written] = api.writes
    assert (written.match_id, written.blurb) == (5120, "Blurb 2")
    assert [r.kind for r in written.reasons] == ["standings", "form", "stakes"]
    assert slack.block("motw_actions") is None  # buttons gone
    status = slack.block("motw_status")
    assert status is not None
    assert status["elements"][0]["text"] == f"✅ Picked #2 (Seacoast vs Bayside) by <@{APPROVER}>"
    s = slack.suggestion()
    assert (s.status, s.extra["picked"], s.extra["by"]) == ("picked", 2, APPROVER)


async def test_a_non_approver_gets_an_ephemeral_and_nothing_is_written() -> None:
    api, slack = FakeApi(), FakeSlack()
    await motw(api).on_action(click("motw_pick_1", user=OTHER), slack)
    assert not api.writes and not api.checks and not slack.updates
    assert slack.ephemeral == [{"channel": CHANNEL, "user": OTHER, "text": NOT_ALLOWED}]


async def test_nobody_is_an_approver_when_the_list_is_empty() -> None:
    api, slack = FakeApi(), FakeSlack()
    await MotwSlack(MotwConfig(CHANNEL, frozenset()), api).on_action(click("motw_pick_1"), slack)  # type: ignore[arg-type]
    assert not api.writes and slack.ephemeral[0]["text"] == NOT_ALLOWED


async def test_skip_marks_the_week_skipped_without_writing() -> None:
    api, slack = FakeApi(), FakeSlack()
    await motw(api).on_action(click("motw_skip"), slack)
    assert not api.writes and not api.checks
    assert slack.suggestion().status == "skipped"
    assert slack.block("motw_status")["elements"][0]["text"] == f"⏭️ Skipped by <@{APPROVER}>"


async def test_an_existing_pick_asks_before_overwriting() -> None:
    existing = {"match": {"id": 1, "home_team_name": "A", "away_team_name": "B"}}
    api, slack = FakeApi(existing=existing), FakeSlack()
    m = motw(api)
    await m.on_action(click("motw_pick_3"), slack)

    assert not api.writes
    [confirm] = slack.posted
    assert confirm["thread_ts"] == SUGG_TS and "A vs B" in confirm["text"]
    buttons = confirm["blocks"][1]["elements"]
    assert [b["action_id"] for b in buttons] == ["motw_overwrite_3", "motw_overwrite_cancel"]
    assert all(b["value"] == SUGG_TS for b in buttons)
    assert slack.suggestion().extra["pending"]["n"] == 3
    assert slack.block("motw_actions") is not None  # still open

    await m.on_action(click("motw_overwrite_3", value=SUGG_TS, on="200.1"), slack)
    [written] = api.writes
    assert written.match_id == 6200
    s = slack.suggestion()
    assert s.status == "picked" and "pending" not in s.extra
    assert slack.updates[-1]["ts"] == "200.1" and "Overwritten with #3" in slack.updates[-1]["text"]


async def test_keep_it_clears_the_pending_pick() -> None:
    api, slack = FakeApi(existing={"match": {}}), FakeSlack()
    m = motw(api)
    await m.on_action(click("motw_pick_1"), slack)
    await m.on_action(click("motw_overwrite_cancel", value=SUGG_TS, on="200.1"), slack)
    assert not api.writes
    assert "pending" not in slack.suggestion().extra
    assert "Kept the existing pick" in slack.updates[-1]["text"]


async def test_a_stale_overwrite_button_writes_nothing() -> None:
    api, slack = FakeApi(existing={"match": {}}), FakeSlack()
    m = motw(api)
    await m.on_action(click("motw_pick_1"), slack)
    await m.on_action(click("motw_overwrite_2", value=SUGG_TS, on="200.1"), slack)  # not pending
    assert not api.writes and "out of date" in slack.updates[-1]["text"]


@pytest.mark.parametrize(("fail", "text"), [("check", CHECK_FAILED), ("write", SAVE_FAILED)])
async def test_mt_failures_are_generic_to_the_user(fail: str, text: str, caplog) -> None:
    api, slack = FakeApi(fail=fail), FakeSlack()
    await motw(api).on_action(click("motw_pick_1"), slack)
    [eph] = slack.ephemeral
    assert eph["text"] == text
    assert "403" not in eph["text"] and "MT_MOTW_TOKEN" not in eph["text"]
    assert slack.block("motw_actions") is not None  # still pickable
    assert any("HTTP" in r.getMessage() or r.exc_info for r in caplog.records)


# --- thread replies -------------------------------------------------------------------


async def test_pick_with_a_new_blurb_from_a_thread_reply() -> None:
    api, slack = FakeApi(), FakeSlack()
    assert await motw(api).on_thread_reply(reply("pick 2 blurb: Louder!"), slack)
    [written] = api.writes
    assert written.blurb == "Louder!" and written.match_id == 5120
    assert slack.suggestion().candidate(2).blurb == "Louder!"
    assert "Louder!" in slack.block("motw_draft_2")["text"]["text"]
    assert slack.reactions == [
        {"channel": CHANNEL, "timestamp": "150.1", "name": "white_check_mark"}
    ]


async def test_edit_changes_the_draft_without_writing() -> None:
    api, slack = FakeApi(), FakeSlack()
    m = motw(api)
    assert await m.on_thread_reply(reply("edit 1\nreason 2: Rewritten.\ndrop reason 3"), slack)
    assert not api.writes and not api.checks
    c = slack.suggestion().candidate(1)
    assert [r.text for r in c.reasons] == ["r1a", "Rewritten."]
    assert "2. Rewritten.  _(form)_" in slack.block("motw_draft_1")["text"]["text"]
    assert slack.block("motw_actions") is not None

    assert await m.on_thread_reply(reply("1", ts="160.1"), slack)  # then pick the edited one
    assert [r.text for r in api.writes[0].reasons] == ["r1a", "Rewritten."]


async def test_skip_by_reply() -> None:
    slack = FakeSlack()
    assert await motw().on_thread_reply(reply("skip"), slack)
    assert slack.suggestion().status == "skipped"


async def test_a_bad_edit_is_explained_to_the_approver_only() -> None:
    api, slack = FakeApi(), FakeSlack()
    assert await motw(api).on_thread_reply(reply("edit 2\nreason 9: nope"), slack)
    assert not api.writes and not slack.updates
    [eph] = slack.ephemeral
    assert eph["user"] == APPROVER and eph["thread_ts"] == SUGG_TS and "add #4" in eph["text"]


async def test_non_approver_commands_are_refused_and_chatter_is_ignored() -> None:
    api, slack = FakeApi(), FakeSlack()
    m = motw(api)
    assert await m.on_thread_reply(reply("pick 1", user=OTHER), slack)
    assert slack.ephemeral[-1]["text"] == NOT_ALLOWED and not api.writes
    assert await m.on_thread_reply(reply("nice, go IFA!", user=OTHER), slack)
    assert await m.on_thread_reply(reply("pick two", user=OTHER), slack)  # malformed: silent
    assert len(slack.ephemeral) == 1


@pytest.mark.parametrize(
    "event",
    [
        {
            "channel": "C_OTHER",
            "thread_ts": SUGG_TS,
            "ts": "1.1",
            "text": "pick 1",
            "user": APPROVER,
        },
        {"channel": CHANNEL, "ts": SUGG_TS, "text": "pick 1", "user": APPROVER},  # not a reply
        {"channel": CHANNEL, "thread_ts": SUGG_TS, "ts": "1.1", "text": "pick 1", "bot_id": "B1"},
        {"channel": CHANNEL, "thread_ts": SUGG_TS, "ts": "1.1", "subtype": "message_changed"},
    ],
)
async def test_other_messages_are_not_ours_and_cost_no_slack_call(event: dict) -> None:
    slack = MagicMock()
    slack.conversations_history = AsyncMock()
    assert not await motw().on_thread_reply(event, slack)
    slack.conversations_history.assert_not_awaited()


async def test_a_thread_that_is_not_a_suggestion_is_left_alone() -> None:
    slack = FakeSlack()
    slack.messages[SUGG_TS]["metadata"] = None
    assert not await motw().on_thread_reply(reply("pick 1"), slack)


# --- never the model ------------------------------------------------------------------


async def test_suggestion_thread_replies_never_reach_the_core(monkeypatch) -> None:
    """Through the Bolt app's own handlers: a reply in a suggestion thread is answered by
    the grammar, even in a thread the core has answered before."""
    from slack_bolt.async_app import AsyncApp

    from sb_support import slack as slack_mod

    agent = MagicMock()
    agent.run = AsyncMock()
    core = SupportCore(agent)
    monkeypatch.setattr(core, "knows", lambda *a: True)  # make it look like a follow-up
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    handlers: dict[str, Any] = {}

    def fake_event(self, name):  # capture the listener functions
        def deco(fn):
            handlers[name] = fn
            return fn

        return deco

    monkeypatch.setattr(AsyncApp, "event", fake_event)
    monkeypatch.setattr(AsyncApp, "action", lambda self, a: lambda fn: fn)
    api, slack = FakeApi(), FakeSlack()
    slack_mod.build_app(core, motw(api))

    await handlers["message"](reply("pick 1"), slack, {"bot_user_id": "U0BOT"})
    await handlers["message"](reply("great pick", ts="151.1"), slack, {"bot_user_id": "U0BOT"})
    mention = reply("<@U0BOT> pick 2", ts="152.1")
    await handlers["app_mention"](mention, slack)
    agent.run.assert_not_awaited()
    assert [w.match_id for w in api.writes] == [4096, 5120]


def test_the_deployment_starts_without_the_motw_secret_or_config() -> None:
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[3]
    deploy = yaml.safe_load((root / "k3s/sb-support.yaml").read_text().split("\n---\n")[0])
    [c] = deploy["spec"]["template"]["spec"]["containers"]
    assert {"configMapRef": {"name": "motw-agent", "optional": True}} in c["envFrom"]
    [token] = [e for e in c["env"] if e["name"] == "MT_MOTW_TOKEN"]
    assert token["valueFrom"]["secretKeyRef"] == {
        "name": "motw-writer",
        "key": "MT_MOTW_TOKEN",
        "optional": True,
    }
