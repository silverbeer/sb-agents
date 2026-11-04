"""The one way mt-agent runs: usage caps, a RunRecord, and the reply's data part.

ADK's A2A executor calls `Runner.run_async` once per A2A request, so wrapping that
call covers each request exactly once, whatever the transport:

- **Caps.** `max_llm_calls` is forced onto the executor's `RunConfig`; a cost cap is
  checked after every model response. Hitting either raises `UsageLimitExceeded`,
  which `track_run` records as outcome "limit" and the A2A task reports as failed.
- **RunRecord.** One `track_run("mt-agent", caller="a2a")` per request, priced with
  `sb_obs.price()` (ADK doesn't compute cost).
- **Data part.** The final answer event gets a data part `{confidence, sources, ...}`
  appended, built from the tool results seen in this run (`ToolLedger`), not from
  anything the model says about itself. The event is copied first, so the ADK session
  keeps plain text: a data part in history would reach AnthropicLlm, which can't
  convert it.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

from google.adk.a2a.converters.part_converter import (
    A2A_DATA_PART_END_TAG,
    A2A_DATA_PART_START_TAG,
    A2A_DATA_PART_TEXT_MIME_TYPE,
)
from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.agents.run_config import RunConfig
from google.adk.events import Event
from google.adk.runners import Runner
from google.genai import types

from mt_agent.sessions import BoundedSessionService
from sb_obs import RunHandle, price, track_run

AGENT = "mt-agent"

# Per-request caps. A standings question is ~3 model calls (search_teams, get_standings,
# answer); an ambiguous table adds one. 8 calls leaves room for one retry of each step.
# $0.02 is docs-agent's cap; Haiku runs here cost well under that.
MAX_LLM_CALLS = 8
MAX_COST_USD = 0.02

Confidence = Literal["high", "low"]
GAPS = ("ambiguous", "not_found")


# The request's RunHandle, for usage from nested runs (the match preview workflow's writer)
# that never reach this runner's event stream.
CURRENT_RUN: ContextVar[RunHandle | None] = ContextVar("mt_agent_current_run", default=None)


class UsageLimitExceeded(RuntimeError):
    """A per-run cap was hit. Named like PydanticAI's, so `track_run` records "limit"."""


def tool_outcome(result: Any) -> str:
    """ "ok", an error kind ("unavailable", "invalid_args", ...), or a gap status."""
    if not isinstance(result, dict):
        return "ok"
    error = result.get("error")
    if error:
        return (error.get("kind") if isinstance(error, dict) else None) or "error"
    status = result.get("status")
    return status if status in GAPS else "ok"


@dataclass
class ToolLedger:
    """What the tools returned in one run, and the confidence rule over it.

    Confidence is "high" only when at least one tool ran and each tool's **latest**
    result is clean. Latest, not any: the instructions tell the model to re-call
    get_standings with a picked option after "ambiguous", and a search that missed then
    hit is fine. A gap is also resolved by a later clean result from a *different* tool
    (the model found an id and used it: SB-1526's head-to-head eval); an error is not.
    A gap or error the model ended on (asked to clarify, or said the data is
    unavailable) is "low"; so is an answer that used no tool at all, since every
    question routed here is a data question.
    """

    calls: list[str] = field(default_factory=list)
    latest: dict[str, str] = field(default_factory=dict)

    def see(self, event: Event) -> None:
        for part in (event.content.parts if event.content else None) or []:
            if part.function_call and part.function_call.name:
                self.calls.append(part.function_call.name)
            if part.function_response and part.function_response.name:
                name = part.function_response.name
                outcome = tool_outcome(part.function_response.response)
                if outcome == "ok":
                    # A gap the model moved past: a clean result from another tool means
                    # it found an id to use (e.g. an "ambiguous" search whose candidates
                    # were one team at several ages). Errors stay: they're not resolved.
                    for tool, seen in list(self.latest.items()):
                        if tool != name and seen in GAPS:
                            del self.latest[tool]
                self.latest[name] = outcome

    @property
    def confidence(self) -> Confidence:
        clean = all(outcome == "ok" for outcome in self.latest.values())
        return "high" if self.calls and clean else "low"

    def report(self) -> dict[str, Any]:
        return {
            "confidence": self.confidence,
            "sources": list(dict.fromkeys(self.calls)),
            "tool_calls": len(self.calls),
            "issues": [f"{t}: {o}" for t, o in self.latest.items() if o != "ok"],
        }


def data_part(data: dict[str, Any]) -> types.Part:
    """A genai Part that ADK's A2A converter turns into an A2A data part."""
    raw = json.dumps(data).encode()
    return types.Part(
        inline_data=types.Blob(
            mime_type=A2A_DATA_PART_TEXT_MIME_TYPE,
            data=A2A_DATA_PART_START_TAG + raw + A2A_DATA_PART_END_TAG,
        )
    )


def is_answer(event: Event, author: str) -> bool:
    return (
        event.author == author
        and event.is_final_response()
        and event.content is not None
        and any(p.text for p in event.content.parts or [])
    )


def record_usage(run: RunHandle, event: Event, model: str) -> None:
    """Add one model response to the run (same accounting as adk-docs-client)."""
    usage = event.usage_metadata
    if usage is None:
        return
    prompt = usage.prompt_token_count or 0
    output = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
    cached = usage.cached_content_token_count or 0
    run.add_usage(
        requests=1,
        input_tokens=prompt,
        output_tokens=output,
        cache_read_tokens=cached,
        cost_usd=price(model, input_tokens=prompt, output_tokens=output, cache_read_tokens=cached),
    )


class TrackedRunner(Runner):
    def __init__(
        self,
        agent: LlmAgent,
        *,
        caller: str = "a2a",
        max_llm_calls: int = MAX_LLM_CALLS,
        max_cost_usd: float = MAX_COST_USD,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("session_service", BoundedSessionService())
        super().__init__(app_name=agent.name, agent=agent, **kwargs)
        self.caller = caller
        self.last_usage: dict[str, Any] = {}  # the last run's usage (one request at a time)
        self.max_llm_calls = max_llm_calls
        self.max_cost_usd = max_cost_usd
        llm = agent.model
        self.model = llm if isinstance(llm, str) else llm.model

    async def run_async(self, **kwargs: Any):  # type: ignore[override]
        config = kwargs.get("run_config") or RunConfig()
        kwargs["run_config"] = config.model_copy(update={"max_llm_calls": self.max_llm_calls})
        ledger = ToolLedger()
        with track_run(AGENT, caller=self.caller, model=self.model) as run:
            token = CURRENT_RUN.set(run)
            try:
                async for event in super().run_async(**kwargs):
                    if event.author == self.agent.name:
                        record_usage(run, event, self.model)
                    ledger.see(event)
                    if is_answer(event, self.agent.name):
                        report = ledger.report()
                        run.confidence = report["confidence"]
                        event = event.model_copy(deep=True)
                        event.content.parts.append(data_part(report))
                    elif (run.usage.get("cost_usd") or 0) > self.max_cost_usd:
                        raise UsageLimitExceeded(f"cost over ${self.max_cost_usd}")
                    yield event
            except LlmCallsLimitExceededError as exc:
                raise UsageLimitExceeded(str(exc)) from exc
            finally:
                CURRENT_RUN.reset(token)
                run.add_usage(tool_calls=len(ledger.calls))
                self.last_usage = dict(run.usage)
