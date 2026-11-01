from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from sb_support.app import handle_mention, strip_mentions


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

    await handle_mention(event, client, agent_saying("Missing Table is a league app."))

    client.chat_postMessage.assert_awaited_once_with(
        channel="C1", thread_ts="111.1", text="Missing Table is a league app."
    )
    client.reactions_add.assert_any_await(channel="C1", timestamp="111.1", name="eyes")
    client.reactions_remove.assert_awaited_once_with(channel="C1", timestamp="111.1", name="eyes")
    client.reactions_add.assert_any_await(channel="C1", timestamp="111.1", name="white_check_mark")


async def test_reply_stays_in_existing_thread() -> None:
    client = fake_client()
    event = {"channel": "C1", "ts": "222.2", "thread_ts": "111.1", "text": "<@U0BOT> again"}

    await handle_mention(event, client, agent_saying("ok"))

    assert client.chat_postMessage.await_args.kwargs["thread_ts"] == "111.1"


async def test_empty_mention_gets_help_without_calling_model() -> None:
    client = fake_client()
    agent = MagicMock()
    agent.run = AsyncMock()

    await handle_mention({"channel": "C1", "ts": "1.1", "text": "<@U0BOT>"}, client, agent)

    agent.run.assert_not_awaited()
    assert "Mention me" in client.chat_postMessage.await_args.kwargs["text"]


async def test_agent_failure_posts_apology_and_x() -> None:
    client = fake_client()
    agent = MagicMock()
    agent.run = AsyncMock(side_effect=RuntimeError("model down"))

    await handle_mention({"channel": "C1", "ts": "1.1", "text": "<@U0BOT> hi"}, client, agent)

    assert "went wrong" in client.chat_postMessage.await_args.kwargs["text"]
    client.reactions_add.assert_any_await(channel="C1", timestamp="1.1", name="x")
    client.reactions_remove.assert_awaited_once()


async def test_reaction_failure_does_not_block_reply() -> None:
    client = fake_client()
    client.reactions_add = AsyncMock(side_effect=RuntimeError("already_reacted"))

    await handle_mention(
        {"channel": "C1", "ts": "1.1", "text": "<@U0BOT> hi"}, client, agent_saying("still here")
    )

    client.chat_postMessage.assert_awaited_once()


async def test_answer_emits_run_record(monkeypatch: pytest.MonkeyPatch) -> None:
    from sb_obs import runs
    from sb_support.app import answer

    records: list = []
    monkeypatch.setattr(runs, "emit", records.append)
    reply = await answer(agent_saying("hi"), "hello?")
    assert (reply.text, reply.escalation) == ("hi", None)
    assert (records[0].agent, records[0].caller, records[0].outcome) == (
        "sb-support",
        "slack",
        "ok",
    )
    assert records[0].requests == 1


async def test_usage_limit_gets_a_specific_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_ai.usage import UsageLimits

    from sb_support import app

    monkeypatch.setattr(app, "USAGE_LIMITS", UsageLimits(total_tokens_limit=1))
    client = fake_client()
    event = {"channel": "C1", "ts": "1.1", "text": "<@U0BOT> big question"}

    await handle_mention(event, client, agent_saying("a long answer"))

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
