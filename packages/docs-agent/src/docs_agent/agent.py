"""docs-agent: answers questions about SB projects from their markdown docs, with citations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.messages import ModelMessage, ToolCallPart
from pydantic_ai.models import Model

from docs_agent.corpus import Corpus, Retriever

DEFAULT_MODEL = "anthropic:claude-haiku-5-5"

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
            "low = the docs don't answer it."
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
        model or os.environ.get("DOCS_AGENT_MODEL", DEFAULT_MODEL),
        deps_type=Deps,
        output_type=DocsAnswer,
        instructions=INSTRUCTIONS,
        name="docs-agent",
    )

    @agent.tool
    def search_docs(ctx: RunContext[Deps], query: str, limit: int = 8) -> list[dict[str, object]]:
        """Keyword search across all SB repo docs. Returns ranked sections with snippets.

        Args:
            query: keywords, e.g. "match-scraper queue failure retry".
            limit: max results (1-15).
        """
        hits = ctx.deps.retriever.search(query, max(1, min(limit, 15)))
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
