"""Ask the ADK router a question and print every event, so the A2A hop is visible."""

from __future__ import annotations

import argparse
import asyncio
import time

from google.adk.runners import InMemoryRunner
from google.genai import types

from adk_docs_client.agent import build_root_agent
from sb_obs import setup_telemetry


async def ask(question: str) -> None:
    runner = InMemoryRunner(agent=build_root_agent(), app_name="adk-docs-client")
    session = await runner.session_service.create_session(app_name="adk-docs-client", user_id="cli")
    msg = types.Content(role="user", parts=[types.Part(text=question)])
    start = time.perf_counter()
    async for event in runner.run_async(user_id="cli", session_id=session.id, new_message=msg):
        for part in (event.content.parts if event.content else None) or []:
            if part.function_call:
                print(f"[{event.author}] call {part.function_call.name}({part.function_call.args})")
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
