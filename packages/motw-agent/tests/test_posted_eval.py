"""`evaluate_posted`: real checks and a scripted judge, on one fixture draft (SB-1620)."""

import pytest
from motw_fakes import good_draft
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from motw_agent import evals, fixtures
from motw_agent.writer import Draft, Written


def judge(passes: bool, reason: str) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        args = {"reason": reason, "pass": passes, "score": 1.0 if passes else 0.0}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    return FunctionModel(respond, model_name="claude-sonnet-5-5")


def posted(*names: str) -> list[tuple]:
    pairs = []
    for name in names:
        c = fixtures.candidate(name)
        pairs.append((c, Written(draft=Draft.model_validate(good_draft(c)), fallback=False)))
    return pairs


@pytest.fixture
def records(monkeypatch: pytest.MonkeyPatch) -> list:
    emitted: list = []
    monkeypatch.setattr(evals, "emit_to_sb_obs", emitted.append)
    return emitted


async def test_a_clean_post_passes_under_its_own_suite(
    monkeypatch: pytest.MonkeyPatch, records: list
) -> None:
    monkeypatch.setattr(evals, "JUDGE_MODEL", judge(True, "supported"))
    line = await evals.evaluate_posted(posted("top-clash", "no-rules"), "claude-haiku-5-5")
    assert line == "Eval: 2/2 passed."
    [record] = records
    assert (record.suite, record.cases, record.model) == (
        "motw-agent-posted",
        2,
        "claude-haiku-5-5",
    )


async def test_the_judges_reason_reaches_the_line_as_plain_text(
    monkeypatch: pytest.MonkeyPatch, records: list
) -> None:
    monkeypatch.setattr(evals, "JUDGE_MODEL", judge(False, "claims <!channel> & more"))
    line = await evals.evaluate_posted(posted("top-clash"), "m")
    assert line == "Eval: 0/1 passed. #1 failed LLMJudge: claims &lt;!channel&gt; &amp; more."
