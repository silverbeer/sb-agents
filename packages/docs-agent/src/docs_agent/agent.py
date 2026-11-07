"""docs-agent: answers questions about SB projects from their markdown docs, with citations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.agent import WrapperAgent
from pydantic_ai.messages import ModelMessage, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits
from sb_models import resolve

from docs_agent.corpus import Corpus, Retriever
from sb_obs import track_run

# Per-run caps. Measured (SB-1478): a question takes 3-4 requests, ~2-4 tool calls,
# ~15k tokens and ~$0.002 on Haiku, so these leave ~2-4x headroom and stop a loop.
# The token cap was 60k until SB-1490: DeepSeek-v4.1-flash read 70-74k tokens on one
# case (mt-prod-deploy) every run. `cost_limit` is the money guard; the token cap only
# stops a runaway, and 100k is ~$0.01 on Haiku, ~$0.005 on DeepSeek.
USAGE_LIMITS = UsageLimits(
    request_limit=8,
    tool_calls_limit=12,
    total_tokens_limit=100_000,
    cost_limit=Decimal("0.02"),
)

# Past this many tokens the tools stop fetching and tell the model to answer (SB-1572):
# a broad question that keeps reading long docs otherwise runs into the hard cap above
# and the asker gets nothing. Each request re-sends the whole history, so the run keeps
# growing after the cut-off. Measured on the SB-1572 question with research notes
# indexed: 60k let one run reach 76k; at 40k six runs peaked at 56k.
SOFT_TOKEN_BUDGET = 40_000
BUDGET_NOTE = (
    "Token budget nearly spent: stop searching and reading. Answer now from what you "
    "have already read; if it is not enough, say so and set confidence to low."
)

# What the asker gets when a run still exceeds USAGE_LIMITS: an answer, never an error.
OVER_BUDGET = (
    "I couldn't answer that from the docs within my budget for one question. "
    "Try a narrower question, e.g. one team, one competition or one step."
)


def over_budget_answer() -> DocsAnswer:
    return DocsAnswer(answer=OVER_BUDGET, sources=[], confidence="low")


def over_soft_budget(ctx: RunContext[Deps]) -> bool:
    return ctx.usage.total_tokens >= SOFT_TOKEN_BUDGET


INSTRUCTIONS = """\
You answer questions about silverbeer (SB) software projects using ONLY their docs.

Process:
1. Call `search_docs` with focused keyword queries. Try 2-3 phrasings if the first misses.
2. Call `read_doc` on the most promising hits to read the actual text before answering.
3. Answer from what you read. Cite every file you relied on in `sources`.

Rules:
- Never invent commands, paths, or facts that are not in the docs you read.
- If the docs do not answer the question, say so, leave the answer short, and set
  confidence to "low".
- "high" confidence only when a doc states the answer directly.
- Confidence is about the specific fact asked. If the docs cover the topic but not the fact
  itself (a person's name, a date, a value, a current schedule), confidence is "low", even
  though you are sure the docs don't contain it.
- Docs may be stale. If a doc looks like a plan or proposal rather than current state, say so.
"""


@dataclass
class Deps:
    corpus: Corpus
    retriever: Retriever


class Source(BaseModel):
    ref: str = Field(description="repo/path of the file, exactly as returned by search_docs")
    heading: str | None = Field(default=None, description="section heading, if one section")


class DocsAnswer(BaseModel):
    answer: str = Field(description="the answer, concise, in markdown")
    sources: list[Source] = Field(description="files the answer relies on; empty if none")
    confidence: Literal["low", "medium", "high"] = Field(
        description=(
            "How well the docs SUPPORT the answer, not how sure you are that they don't: "
            "high = a doc states it directly; medium = inferred from related docs; "
            "low = the docs don't answer it, including when they cover the topic but not the "
            "specific fact asked."
        )
    )


def searched(messages: list[ModelMessage]) -> bool:
    """True if the model called search_docs at least once in this run."""
    return any(
        isinstance(part, ToolCallPart) and part.tool_name == "search_docs"
        for msg in messages
        for part in getattr(msg, "parts", ())
    )


def build_agent(model: Model | str | None = None) -> Agent[Deps, DocsAnswer]:
    agent = Agent(
        # Registry default or $DOCS_AGENT_MODEL, checked against what this agent needs.
        model or resolve("docs-agent", check_credentials=False).ref,
        deps_type=Deps,
        output_type=DocsAnswer,
        instructions=INSTRUCTIONS,
        name="docs-agent",
    )

    @agent.tool
    def search_docs(
        ctx: RunContext[Deps], query: str, limit: int = 8
    ) -> list[dict[str, object]] | str:
        """Keyword search across all SB repo docs. Returns ranked sections with snippets.

        Args:
            query: keywords, e.g. "match-scraper queue failure retry".
            limit: max results (1-10).
        """
        if over_soft_budget(ctx):
            return BUDGET_NOTE
        hits = ctx.deps.retriever.search(query, max(1, min(limit, 10)))
        return [
            {
                "ref": h.section.ref,
                "heading": h.section.heading,
                "score": h.score,
                "snippet": h.snippet,
            }
            for h in hits
        ]

    @agent.tool
    def read_doc(ctx: RunContext[Deps], ref: str, heading: str | None = None) -> str:
        """Read a doc file, or just one section of it.

        Args:
            ref: repo/path exactly as returned by search_docs.
            heading: optional section heading to read only that section.
        """
        if over_soft_budget(ctx):
            return BUDGET_NOTE
        try:
            return ctx.deps.corpus.read(ref, heading)
        except KeyError as e:
            raise ModelRetry(f"no such doc or section: {e}. Use a ref from search_docs.") from e

    @agent.output_validator
    def grounded(ctx: RunContext[Deps], out: DocsAnswer) -> DocsAnswer:
        if not searched(ctx.messages):
            raise ModelRetry("Search the docs with search_docs before answering.")
        unknown = [s.ref for s in out.sources if s.ref not in ctx.deps.corpus.files]
        if unknown:
            raise ModelRetry(f"these sources are not real corpus files: {unknown}")
        return out

    return agent


class WithDeps(WrapperAgent[Deps, DocsAnswer]):
    """The one way docs-agent runs: fixed deps, usage limits, and a RunRecord.

    fasta2a's worker calls `agent.run(message_history=...)` without deps, and
    `run()`/`run_sync()` go through `iter()`, so overriding `iter()` covers the A2A
    server, the CLI and the evals alike. `caller` labels the record.
    """

    def __init__(self, wrapped: Agent[Deps, DocsAnswer], deps: Deps, caller: str = "a2a") -> None:
        super().__init__(wrapped)
        self._deps = deps
        self._caller = caller

    @asynccontextmanager
    async def iter(self, user_prompt: Any = None, **kwargs: Any) -> AsyncIterator[Any]:
        if kwargs.get("deps") is None:
            kwargs["deps"] = self._deps
        if kwargs.get("usage_limits") is None:
            kwargs["usage_limits"] = USAGE_LIMITS
        model = kwargs.get("model") or self.wrapped.model
        with track_run("docs-agent", caller=self._caller, model=model) as handle:
            async with self.wrapped.iter(user_prompt, **kwargs) as run:
                try:
                    yield run
                finally:
                    handle.record_usage(run.usage)
                    if run.result is not None:
                        handle.confidence = run.result.output.confidence
