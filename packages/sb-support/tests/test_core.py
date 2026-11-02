"""Channel-neutral core (SB-1498): no channel SDKs; a second channel needs no core change."""

import ast
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.test import TestModel

from sb_support import core as core_module
from sb_support.core import ERROR_TEXT, HELP_TEXT, InboundMessage, Reply, SupportCore

CHANNEL_SDKS = ("slack", "slack_bolt", "slack_sdk", "telegram", "aiogram")


def test_core_imports_no_channel_sdk() -> None:
    tree = ast.parse(Path(core_module.__file__).read_text())
    imported = {
        (node.module if isinstance(node, ast.ImportFrom) else alias.name).split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
        if isinstance(node, ast.Import) or node.module
    }
    imported |= {
        node.module.split(".")[1]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("sb_support.")
    }
    assert not imported & set(CHANNEL_SDKS), imported


@dataclass
class FakeChannel:
    """A whole second channel adapter in a few lines: no change to the core needed."""

    core: SupportCore
    sent: list[tuple[str, str]] = field(default_factory=list)

    async def receive(self, chat_id: str, text: str) -> Reply:
        reply = await self.core.handle(InboundMessage("fake", chat_id, text, user_id="u1"))
        self.sent.append((chat_id, reply.text))
        return reply


def agent_saying(text: str) -> Agent:
    return Agent(TestModel(custom_output_text=text))


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list:
    from sb_obs import runs

    records: list = []
    monkeypatch.setattr(runs, "emit", records.append)
    return records


async def test_second_channel_works_through_the_same_core(emitted: list) -> None:
    channel = FakeChannel(SupportCore(agent_saying("hello from the core")))
    reply = await channel.receive("chat-1", "hi?")
    assert (reply.text, reply.outcome) == ("hello from the core", "answered")
    assert channel.sent == [("chat-1", "hello from the core")]
    assert emitted[0].caller == "fake"  # RunRecord.caller = channel


async def test_memory_is_per_channel_and_conversation(emitted: list) -> None:
    core = SupportCore(agent_saying("ok"))
    await core.handle(InboundMessage("fake", "c1", "first"))
    await core.handle(InboundMessage("fake", "c1", "second"))
    await core.handle(InboundMessage("other", "c1", "same id, other channel"))

    prompts = [
        p.content
        for m in core.memory.history("fake:c1")
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    ]
    assert prompts == ["first", "second"]
    assert core.knows("fake", "c1") and core.knows("other", "c1")
    assert not core.knows("fake", "c2")


async def test_empty_message_gets_help_without_the_model() -> None:
    agent = MagicMock()
    agent.run = AsyncMock()
    reply = await SupportCore(agent).handle(InboundMessage("fake", "c1", "   "))
    assert (reply.text, reply.outcome) == (HELP_TEXT, "help")
    agent.run.assert_not_awaited()


async def test_agent_failure_is_an_error_reply_not_an_exception() -> None:
    agent = MagicMock(model="test")
    agent.run = AsyncMock(side_effect=RuntimeError("model down"))
    reply = await SupportCore(agent).handle(InboundMessage("fake", "c1", "hi"))
    assert (reply.text, reply.outcome) == (ERROR_TEXT, "error")


async def test_root_span_is_channel_neutral(monkeypatch: pytest.MonkeyPatch, emitted: list) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(core_module, "_tracer", provider.get_tracer("test"))

    msg = InboundMessage("fake", "c1", "hi", message_id="m1", attributes={"fake.room": "r"})
    await SupportCore(agent_saying("ok")).handle(msg)

    (span,) = [s for s in exporter.get_finished_spans() if s.name.startswith("support message")]
    assert span.name == "support message fake"
    assert span.attributes["messaging.system"] == "fake"
    assert span.attributes["messaging.message.id"] == "m1"
    assert span.attributes["fake.room"] == "r"
    assert span.attributes["sb.outcome"] == "answered"
