"""Test doubles for motw-agent: MT reads, a scripted writer model, a Slack client."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from motw_agent.candidates import CandidatesResult, MotwCandidate
from motw_agent.writer import Draft, build_writer


class FakeMt:
    def __init__(
        self,
        pick: dict[str, Any] | None | Exception = None,
        result: CandidatesResult | Exception | None = None,
    ) -> None:
        self.pick, self.result = pick, result
        self.calls: list[tuple[str, date]] = []

    async def current_pick(self, week: date) -> dict[str, Any] | None:
        self.calls.append(("current_pick", week))
        if isinstance(self.pick, Exception):
            raise self.pick
        return self.pick

    async def candidates(self, week: date, limit: int = 5) -> CandidatesResult:
        self.calls.append(("candidates", week))
        if isinstance(self.result, Exception):
            raise self.result
        assert self.result is not None
        return self.result


def good_draft(c: MotwCandidate) -> dict[str, Any]:
    """A draft that passes every check: each reason is a cited fact's own text."""
    reasons = [{"text": f.text[:200], "kind": f.kind, "fact_ids": [f.id]} for f in c.facts[:4]]
    return {"blurb": f"{c.home.name} host {c.away.name}. Be there!", "reasons": reasons}


def scripted_writer(*drafts: Any) -> tuple[Agent[MotwCandidate, Draft], list[list[ModelMessage]]]:
    """A writer whose model returns each draft in turn (a callable gets the candidate's
    prompt-less turn and returns a draft dict). Records every request."""
    script = list(drafts)
    seen: list[list[ModelMessage]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(messages)
        if not script:
            raise AssertionError("writer model ran out of script")
        args = script.pop(0)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    return build_writer(FunctionModel(respond, model_name="claude-haiku-5-5")), seen


def writer_by_candidate(
    *candidates: MotwCandidate,
) -> tuple[Agent[MotwCandidate, Draft], list[list[ModelMessage]]]:
    """Answers each request from the candidate named in its prompt. `suggest` writes drafts
    concurrently, so a script read in request order would hand a draft to the wrong one."""
    seen: list[list[ModelMessage]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(messages)
        prompt = str(messages[0])
        c = next(c for c in candidates if c.label in prompt)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, good_draft(c))])

    return build_writer(FunctionModel(respond, model_name="claude-haiku-5-5")), seen


def fake_slack(history: list[dict[str, Any]] | None = None) -> MagicMock:
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "999.1"})
    client.conversations_history = AsyncMock(
        return_value={"ok": True, "messages": history or [], "response_metadata": {}}
    )
    return client
