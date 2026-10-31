"""Serve docs-agent over A2A with fasta2a.

    op run --env-file=.env.op -- uv run docs-agent-a2a     # http://127.0.0.1:8101

Agent card: GET /.well-known/agent-card.json
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any

from fasta2a import FastA2A
from fasta2a.broker import InMemoryBroker
from fasta2a.pydantic_ai import AgentWorker, worker_lifespan
from fasta2a.schema import Artifact, Message, Part, Skill, Task, TaskSendParams, TaskState
from fasta2a.storage import InMemoryStorage
from pydantic_ai import Agent
from pydantic_ai.agent import WrapperAgent

from docs_agent.agent import Deps, DocsAnswer, build_agent
from docs_agent.corpus import Corpus, KeywordRetriever

log = logging.getLogger("docs_agent.a2a")

DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 8101

DESCRIPTION = (
    "Answers questions about silverbeer (SB) projects (missing-table, match-scraper, "
    "myrunstreak, trd, bet, cycle-runner, dotfiles, janitor, ...) from their markdown docs. "
    "Returns an answer, the source files cited, and a confidence level."
)


class UtcStorage(InMemoryStorage):
    """InMemoryStorage with timezone-aware task timestamps.

    fasta2a 2.1.1 stamps task status with naive `datetime.now().isoformat()`
    (no offset). A2A 1.0 timestamps are protobuf Timestamps, which require
    RFC 3339 with `Z` or an offset, so a strict client (ADK via a2a-sdk) fails
    with "Failed to parse timestamp field: time data '2026-10' ...".
    """

    @staticmethod
    def _stamp(task: Task) -> Task:
        task["status"]["timestamp"] = datetime.now(UTC).isoformat()
        return task

    async def submit_task(self, context_id: str, message: Message) -> Task:
        return self._stamp(await super().submit_task(context_id, message))

    async def update_task(
        self,
        task_id: str,
        state: TaskState,
        new_artifacts: list[Artifact] | None = None,
        new_messages: list[Message] | None = None,
    ) -> Task:
        return self._stamp(await super().update_task(task_id, state, new_artifacts, new_messages))


def render_text(answer: DocsAnswer) -> str:
    """Human/LLM-readable form of a DocsAnswer (Slack mrkdwn-friendly)."""
    lines = [answer.answer, "", f"_confidence: {answer.confidence}_"]
    if answer.sources:
        lines.append("Sources:")
        lines += [f"• {s.ref}" + (f" § {s.heading}" if s.heading else "") for s in answer.sources]
    return "\n".join(lines)


@dataclass
class SpecAgentWorker(AgentWorker):
    """fasta2a AgentWorker with the fixes ADK interop needed.

    1. Streamed artifacts start with append=False. fasta2a 2.1.1 sends every chunk
       with append=True, including the first; A2A requires the first chunk to create
       the artifact, and a2a-sdk (ADK's client) rejects the stream with
       "append=True for nonexistent artifact_id".
    2. Failed tasks are logged. fasta2a marks the task failed and re-raises into the
       broker, which drops it: the server logs nothing and ADK shows an empty event.
    3. The result artifact carries a text part as well as the data part. ADK turns a
       data part into base64 `inline_data`, which neither an LLM nor Slack can read.

    `stream` defaults to False: docs-agent's streamed "text" is its JSON output being
    written, which is noise to a client. Agents with prose output can enable it.
    """

    stream: bool = False
    _open: set[tuple[str, str]] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.stream:
            self._streaming = False  # fasta2a then runs the agent without streaming

    async def run_task(self, params: TaskSendParams) -> None:
        # fasta2a marks the task failed and re-raises into the broker, which drops it:
        # without this, the server logs nothing and ADK shows an empty event.
        try:
            await super().run_task(params)
        except Exception:
            log.exception("A2A task %s failed", params.get("id"))
            raise

    async def publish_artifact(
        self,
        task_id: str,
        context_id: str,
        artifact: Artifact,
        *,
        append: bool = False,
        last_chunk: bool = True,
    ) -> None:
        key = (task_id, artifact["artifact_id"])
        if append and key not in self._open:
            append = False
        if last_chunk:
            self._open.discard(key)
        else:
            self._open.add(key)
        await super().publish_artifact(
            task_id, context_id, artifact, append=append, last_chunk=last_chunk
        )

    def build_artifacts(self, result: Any) -> list[Artifact]:
        artifacts = super().build_artifacts(result)
        if isinstance(result, DocsAnswer):
            artifacts[0]["parts"].insert(0, Part(text=render_text(result)))
        return artifacts


class WithDeps(WrapperAgent[Deps, DocsAnswer]):
    """Inject fixed deps into every run.

    fasta2a's worker calls `agent.run(message_history=...)` without deps, and
    `run()` goes through `iter()`, so overriding `iter()` covers both.
    """

    def __init__(self, wrapped: Agent[Deps, DocsAnswer], deps: Deps) -> None:
        super().__init__(wrapped)
        self._deps = deps

    @asynccontextmanager
    async def iter(self, user_prompt: Any = None, **kwargs: Any) -> AsyncIterator[Any]:
        if kwargs.get("deps") is None:
            kwargs["deps"] = self._deps
        async with self.wrapped.iter(user_prompt, **kwargs) as run:
            yield run


def build_app(
    deps: Deps | None = None,
    agent: Agent[Deps, DocsAnswer] | None = None,
    url: str | None = None,
):
    if deps is None:
        corpus = Corpus.load()
        deps = Deps(corpus, KeywordRetriever(corpus))
    host = os.environ.get("DOCS_AGENT_A2A_HOST", DEFAULT_HOST)
    port = int(os.environ.get("DOCS_AGENT_A2A_PORT", DEFAULT_PORT))
    # Same wiring as fasta2a.pydantic_ai.agent_to_a2a, with our storage and worker.
    wrapped = WithDeps(agent or build_agent(), deps)
    storage, broker = UtcStorage(), InMemoryBroker()
    worker = SpecAgentWorker(agent=wrapped, broker=broker, storage=storage)
    return FastA2A(
        storage=storage,
        broker=broker,
        lifespan=partial(worker_lifespan, worker=worker, agent=wrapped),
        name="docs-agent",
        description=DESCRIPTION,
        url=url or os.environ.get("DOCS_AGENT_A2A_URL", f"http://{host}:{port}"),
        version="0.1.0",
        skills=[
            Skill(
                id="answer-from-docs",
                name="Answer from SB docs",
                description="Answer a question about an SB project, citing the docs used.",
                tags=["docs", "support", "sb"],
                examples=[
                    "How do I deploy missing-table to production?",
                    "Which 1Password vault can agents read?",
                ],
                input_modes=["text/plain"],
                output_modes=["text/plain", "application/json"],
            )
        ],
    )


def main() -> None:
    import uvicorn

    host = os.environ.get("DOCS_AGENT_A2A_HOST", DEFAULT_HOST)
    port = int(os.environ.get("DOCS_AGENT_A2A_PORT", DEFAULT_PORT))
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    uvicorn.run(build_app(), host=host, port=port)
