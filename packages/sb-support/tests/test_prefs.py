"""A user's saved "my team" (SB-1564): stored per (channel, user), given to the model."""

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from sb_support.agent import NO_TEAM, NO_USER, build_agent
from sb_support.core import InboundMessage, SupportCore
from sb_support.prefs import MAX_TEAM_CHARS, PreferenceStore


def test_store_is_per_channel_and_user() -> None:
    store = PreferenceStore(":memory:")
    assert store.set_my_team("slack", "U1", "  IFA   Homegrown U15 ") == "IFA Homegrown U15"
    assert store.my_team("slack", "U1") == "IFA Homegrown U15"
    assert store.my_team("slack", "U2") is None
    assert store.my_team("telegram", "U1") is None
    assert store.my_team("slack", None) is None


def test_store_overwrites_and_forgets() -> None:
    store = PreferenceStore(":memory:")
    store.set_my_team("slack", "U1", "IFA Homegrown U15")
    store.set_my_team("slack", "U1", "IFA Academy U14")
    assert store.my_team("slack", "U1") == "IFA Academy U14"
    assert store.forget_my_team("slack", "U1") is True
    assert store.forget_my_team("slack", "U1") is False
    assert store.my_team("slack", "U1") is None


def test_store_rejects_empty_and_caps_length() -> None:
    store = PreferenceStore(":memory:")
    with pytest.raises(ValueError):
        store.set_my_team("slack", "U1", "   ")
    assert len(store.set_my_team("slack", "U1", "x" * 500)) == MAX_TEAM_CHARS


def test_store_survives_a_restart(tmp_path: Path) -> None:
    path = str(tmp_path / "prefs.db")
    PreferenceStore(path).set_my_team("slack", "U1", "IFA Homegrown U15")
    assert PreferenceStore(path).my_team("slack", "U1") == "IFA Homegrown U15"


def test_store_reads_its_path_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = str(tmp_path / "env.db")
    monkeypatch.setenv("SB_SUPPORT_DB", path)
    assert PreferenceStore().path == path


def capturing_model(seen: list[str]) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(messages[-1].instructions or "")
        return ModelResponse(parts=[TextPart("ok")])

    return FunctionModel(respond)


async def test_model_sees_only_the_asking_users_team() -> None:
    seen: list[str] = []
    core = SupportCore(build_agent(capturing_model(seen)), prefs=PreferenceStore(":memory:"))
    core.prefs.set_my_team("slack", "U1", "IFA Homegrown U15")

    await core.handle(InboundMessage("slack", "t1", "how's my team?", user_id="U1"))
    await core.handle(InboundMessage("slack", "t2", "how's my team?", user_id="U2"))
    await core.handle(InboundMessage("slack", "t3", "how's my team?"))

    assert "saved team is: IFA Homegrown U15" in seen[0]
    assert NO_TEAM in seen[1] and "saved team is" not in seen[1]
    assert NO_USER in seen[2]


async def test_remember_and_forget_tools_write_the_asking_users_team() -> None:
    prefs = PreferenceStore(":memory:")
    remember = SupportCore(build_agent(TestModel(call_tools=["remember_my_team"])), prefs=prefs)
    reply = await remember.handle(InboundMessage("slack", "t1", "my team is X", user_id="U1"))
    assert reply.outcome == "answered"
    assert prefs.my_team("slack", "U1")
    assert prefs.my_team("slack", "U2") is None

    forget = SupportCore(build_agent(TestModel(call_tools=["forget_my_team"])), prefs=prefs)
    await forget.handle(InboundMessage("slack", "t2", "forget my team", user_id="U1"))
    assert prefs.my_team("slack", "U1") is None


async def test_remember_without_a_user_saves_nothing() -> None:
    prefs = PreferenceStore(":memory:")
    core = SupportCore(build_agent(TestModel(call_tools=["remember_my_team"])), prefs=prefs)
    await core.handle(InboundMessage("slack", "t1", "my team is X"))
    assert prefs._db.execute("SELECT count(*) FROM my_team").fetchone()[0] == 0
