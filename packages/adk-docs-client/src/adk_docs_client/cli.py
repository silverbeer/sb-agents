"""Ask the ADK router a question and print every event, so the A2A hop is visible."""

from __future__ import annotations

import argparse
import asyncio
import time
from typing import Any

from google.adk.agents.run_config import RunConfig
from google.adk.runners import InMemoryRunner
from google.genai import types

from adk_docs_client.agent import build_root_agent
from sb_obs import RunHandle, model_name, price, setup_telemetry, track_run

ROUTER = "sb_router"

# Router caps: one routing call, maybe a retry; transfer to docs_agent is not an LLM call here.
RUN_CONFIG = RunConfig(max_llm_calls=4)


def record_router_usage(run: RunHandle, event: Any, model: str) -> None:
    """Add one router LLM response to the run (docs_agent's events carry its own cost)."""
    usage = getattr(event, "usage_metadata", None)
    if usage is None or event.author != ROUTER:
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


async def ask(question: str) -> None:
    agent = build_root_agent()
    model = model_name(agent.model.model)
    runner = InMemoryRunner(agent=agent, app_name="adk-docs-client")
    session = await runner.session_service.create_session(app_name="adk-docs-client", user_id="cli")
    msg = types.Content(role="user", parts=[types.Part(text=question)])
    start = time.perf_counter()
    with track_run(ROUTER, caller="cli", model=model) as run:
        async for event in runner.run_async(
            user_id="cli", session_id=session.id, new_message=msg, run_config=RUN_CONFIG
        ):
            record_router_usage(run, event, model)
            for part in (event.content.parts if event.content else None) or []:
                if call := part.function_call:
                    print(f"[{event.author}] call {call.name}({call.args})")
                elif part.function_response:
                    print(f"[{event.author}] result {part.function_response.name}")
                elif part.text:
                    print(f"[{event.author}] {part.text}")
            if event.error_message:
                print(f"[{event.author}] ERROR {event.error_code}: {event.error_message}")
    print(f"\n[{time.perf_counter() - start:.1f}s]")


def main() -> None:
    parser = argparse.ArgumentParser(prog="adk-docs-client")
    parser.add_argument("question")
    setup_telemetry("adk-docs-client")
    asyncio.run(ask(parser.parse_args().question))
