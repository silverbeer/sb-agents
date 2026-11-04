from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from sb_support.core import SupportCore
from sb_support.slack import handle_event, strip_mentions


def fake_client() -> MagicMock:
    client = MagicMock()
    client.chat_postMessage = AsyncMock()
    client.reactions_add = AsyncMock()
    client.reactions_remove = AsyncMock()
    return client


def agent_saying(text: str) -> Agent[None, str]:
    return Agent(TestModel(custom_output_text=text))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<@U0ABC123> hello", "hello"),
        ("hi <@U0ABC123> there", "hi  there"),
        ("<@U0ABC123>", ""),
        ("no mention", "no mention"),
    ],
)
def test_strip_mentions(raw: str, expected: str) -> None:
    assert strip_mentions(raw) == expected


async def test_replies_in_new_thread_with_reactions() -> None:
    client = fake_client()
    event = {"channel": "C1", "ts": "111.1", "text": "<@U0BOT> what is MT?"}

    await handle_event(event, client, SupportCore(agent_saying("Missing Table is a league app.")))

    client.chat_postMessage.assert_awaited_once_with(
        channel="C1", thread_ts="111.1", text="Missing Table is a league app."
    )
    client.reactions_add.assert_any_await(channel="C1", timestamp="111.1", name="eyes")
    client.reactions_remove.assert_awaited_once_with(channel="C1", timestamp="111.1", name="eyes")
    client.reactions_add.assert_any_await(channel="C1", timestamp="111.1", name="white_check_mark")


async def test_reply_stays_in_existing_thread() -> None:
    client = fake_client()
    event = {"channel": "C1", "ts": "222.2", "thread_ts": "111.1", "text": "<@U0BOT> again"}

    await handle_event(event, client, SupportCore(agent_saying("ok")))

    assert client.chat_postMessage.await_args.kwargs["thread_ts"] == "111.1"


async def test_empty_mention_gets_help_without_calling_model() -> None:
    client = fake_client()
    agent = MagicMock()
    agent.run = AsyncMock()

    await handle_event(
        {"channel": "C1", "ts": "1.1", "text": "<@U0BOT>"}, client, SupportCore(agent)
    )

    agent.run.assert_not_awaited()
    assert "Mention me" in client.chat_postMessage.await_args.kwargs["text"]


async def test_agent_failure_posts_apology_and_x() -> None:
    client = fake_client()
    agent = MagicMock()
    agent.run = AsyncMock(side_effect=RuntimeError("model down"))

    await handle_event(
        {"channel": "C1", "ts": "1.1", "text": "<@U0BOT> hi"}, client, SupportCore(agent)
    )

    assert "went wrong" in client.chat_postMessage.await_args.kwargs["text"]
    client.reactions_add.assert_any_await(channel="C1", timestamp="1.1", name="x")
    client.reactions_remove.assert_awaited_once()


async def test_reaction_failure_does_not_block_reply() -> None:
    client = fake_client()
    client.reactions_add = AsyncMock(side_effect=RuntimeError("already_reacted"))

    await handle_event(
        {"channel": "C1", "ts": "1.1", "text": "<@U0BOT> hi"},
        client,
        SupportCore(agent_saying("still here")),
    )

    client.chat_postMessage.assert_awaited_once()


async def test_slack_run_record_has_slack_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    from sb_obs import runs

    records: list = []
    monkeypatch.setattr(runs, "emit", records.append)
    client = fake_client()
    event = {"channel": "C1", "ts": "1.1", "text": "<@U0BOT> hello?"}
    await handle_event(event, client, SupportCore(agent_saying("hi")))
    assert client.chat_postMessage.await_args.kwargs["text"] == "hi"
    assert (records[0].agent, records[0].caller, records[0].outcome) == (
        "sb-support",
        "slack",
        "ok",
    )
    assert records[0].requests == 1


async def test_usage_limit_gets_a_specific_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_ai.usage import UsageLimits

    from sb_support import core

    monkeypatch.setattr(core, "USAGE_LIMITS", UsageLimits(total_tokens_limit=1))
    client = fake_client()
    event = {"channel": "C1", "ts": "1.1", "text": "<@U0BOT> big question"}

    await handle_event(event, client, SupportCore(agent_saying("a long answer")))

    assert "more work than I'm allowed" in client.chat_postMessage.await_args.kwargs["text"]
    client.reactions_add.assert_any_await(channel="C1", timestamp="1.1", name="x")


async def test_docs_client_unreachable_raises_docs_agent_error() -> None:
    import httpx

    from sb_support.specialists import SpecialistClient, SpecialistError

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
        with pytest.raises(SpecialistError, match="unreachable"):
            await SpecialistClient("http://127.0.0.1:8101", http).ask("q")


# --- tables (SB-1528) ----------------------------------------------------------------


def _reply_with_table(text: str = "IFA are 10th.", **kw):
    from sb_support.core import Reply
    from sb_support.tables import Table

    table = Table(
        "Homegrown Northeast U15 (League)",
        ["#", "Team", "Pts"],
        [["1", "Metropolitan Oval", "22"], ["10", "IFA", "6"]],
    )
    return Reply(text, tables=[table], **kw)


async def test_a_table_is_posted_as_a_block_kit_table() -> None:
    from sb_support.slack import post_reply

    client = fake_client()
    await post_reply(client, "C1", "111.1", _reply_with_table())

    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "IFA are 10th."  # notification / accessibility fallback
    section, context, table = kwargs["blocks"]
    assert section["text"]["text"] == "IFA are 10th."
    assert "Homegrown Northeast U15" in context["elements"][0]["text"]
    assert table["type"] == "table"
    assert [c["text"] for c in table["rows"][0]] == ["#", "Team", "Pts"]  # header row
    assert [c["text"] for c in table["rows"][2]] == ["10", "IFA", "6"]
    assert table["column_settings"] == [{"align": "right"}, None, {"align": "right"}]


async def test_rejected_blocks_fall_back_to_a_code_block() -> None:
    from sb_support.slack import post_reply

    client = fake_client()
    client.chat_postMessage = AsyncMock(side_effect=[RuntimeError("invalid_blocks"), None])
    await post_reply(client, "C1", "111.1", _reply_with_table())

    retry = client.chat_postMessage.await_args_list[1].kwargs
    assert "blocks" not in retry
    assert retry["text"].startswith("IFA are 10th.")
    assert "```" in retry["text"] and "Metropolitan Oval  22" in retry["text"]


async def test_too_long_text_skips_blocks_and_uses_a_code_block() -> None:
    from sb_support.slack import post_reply

    client = fake_client()
    await post_reply(client, "C1", "111.1", _reply_with_table("x" * 3001))

    kwargs = client.chat_postMessage.await_args.kwargs
    assert "blocks" not in kwargs and "```" in kwargs["text"]


async def test_a_reply_without_tables_is_plain_text() -> None:
    from sb_support.core import Reply
    from sb_support.slack import post_reply

    client = fake_client()
    await post_reply(client, "C1", "111.1", Reply("hi"))
    client.chat_postMessage.assert_awaited_once_with(channel="C1", thread_ts="111.1", text="hi")


def test_a_table_over_slacks_limits_is_not_a_block() -> None:
    from sb_support.slack import table_block
    from sb_support.tables import Table

    assert table_block(Table("t", ["a"], [["x"]] * 100)) is None  # 101 rows with the header
    assert table_block(Table("t", ["a"] * 21, [["x"] * 21])) is None
    assert table_block(Table("t", ["a"], [["x" * 10_001]])) is None
