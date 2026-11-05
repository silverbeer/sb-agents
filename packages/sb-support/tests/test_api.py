"""MT app adapter (SB-1500): auth, validation, and the Reply as JSON."""

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from starlette.testclient import TestClient

from sb_support.agent import MT_APP_FORMAT, SLACK_FORMAT, build_agent
from sb_support.api import MAX_TEXT, build_app, parse_message
from sb_support.core import InboundMessage, SupportCore
from sb_support.prefs import PreferenceStore

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BODY = {"conversation_id": "c1", "user_id": "u1", "text": "hello"}


def client(core: SupportCore) -> TestClient:
    return TestClient(build_app(core, token=TOKEN))


def core_saying(text: str) -> SupportCore:
    return SupportCore(Agent(TestModel(custom_output_text=text)), prefs=PreferenceStore(":memory:"))


def test_token_is_required_to_build() -> None:
    with pytest.raises(ValueError):
        build_app(core_saying("x"), token="")


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": TOKEN}]
)
def test_chat_rejects_a_missing_or_wrong_token(headers: dict) -> None:
    with client(core_saying("x")) as c:
        response = c.post("/v1/chat", json=BODY, headers=headers)
    assert response.status_code == 401


def test_chat_returns_the_reply() -> None:
    with client(core_saying("hi there")) as c:
        assert c.get("/healthz").json() == {"status": "ok"}
        response = c.post("/v1/chat", json=BODY, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "text": "hi there",
        "outcome": "answered",
        "escalation": None,
        "routed_to": [],
        "tables": [],
    }


def test_conversation_memory_is_per_mt_conversation() -> None:
    core = core_saying("ok")
    with client(core) as c:
        c.post("/v1/chat", json=BODY, headers=AUTH)
    assert core.knows("mt-app", "c1")
    assert not core.knows("mt-app", "c2")


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ([], "JSON object"),
        ({"user_id": "u1", "text": "x"}, "conversation_id"),
        ({"conversation_id": "", "text": "x"}, "conversation_id"),
        ({"conversation_id": "c", "text": "   "}, "text is required"),
        ({"conversation_id": "c", "text": "x" * (MAX_TEXT + 1)}, "over"),
        ({"conversation_id": "c", "user_id": 5, "text": "x"}, "user_id"),
    ],
)
def test_parse_message_rejects_bad_bodies(body: object, error: str) -> None:
    result = parse_message(body)
    assert isinstance(result, str) and error in result


def test_parse_message_builds_an_mt_app_message() -> None:
    msg = parse_message({"conversation_id": "c", "user_id": "u", "text": " hi "})
    assert msg == InboundMessage("mt-app", "c", "hi", user_id="u")


def test_bad_body_is_400() -> None:
    with client(core_saying("x")) as c:
        response = c.post("/v1/chat", content=b"not json", headers=AUTH)
    assert response.status_code == 400


def test_reply_says_which_channel_it_writes_for() -> None:
    seen: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(messages[-1].instructions or "")
        return ModelResponse(parts=[TextPart("ok")])

    core = SupportCore(build_agent(FunctionModel(respond)), prefs=PreferenceStore(":memory:"))
    with client(core) as c:
        c.post("/v1/chat", json=BODY, headers=AUTH)
    assert MT_APP_FORMAT in seen[0] and SLACK_FORMAT not in seen[0]


async def test_slack_messages_still_get_slack_formatting() -> None:
    seen: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(messages[-1].instructions or "")
        return ModelResponse(parts=[TextPart("ok")])

    core = SupportCore(build_agent(FunctionModel(respond)), prefs=PreferenceStore(":memory:"))
    await core.handle(InboundMessage("slack", "t", "hi", user_id="U1"))
    assert SLACK_FORMAT in seen[0]


def test_user_token_becomes_the_messages_credential() -> None:
    msg = parse_message({"conversation_id": "c", "user_id": "u", "text": "q", "user_token": "t"})
    assert isinstance(msg, InboundMessage) and msg.credential == "t"
    assert "user_token" in parse_message({"conversation_id": "c", "text": "q", "user_token": ""})
    assert "user_token" in parse_message({"conversation_id": "c", "text": "q", "user_token": 5})


async def test_signed_in_user_keeps_my_team_for_mt_agent() -> None:
    from sb_support.agent import SIGNED_IN

    seen: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(messages[-1].instructions or "")
        return ModelResponse(parts=[TextPart("ok")])

    core = SupportCore(build_agent(FunctionModel(respond)), prefs=PreferenceStore(":memory:"))
    await core.handle(InboundMessage("mt-app", "c", "my team?", user_id="u", credential="t"))
    assert SIGNED_IN in seen[0]
