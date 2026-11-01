import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.models.test import TestModel

from docs_agent.agent import Deps, build_agent
from docs_agent.corpus import Corpus, KeywordRetriever, split_sections


@pytest.fixture
def deps() -> Deps:
    md = "# Deploy\n\nMerge to main to deploy."
    corpus = Corpus(split_sections("alpha", "README.md", md), {"alpha/README.md": md})
    return Deps(corpus, KeywordRetriever(corpus))


def answer(refs: list[str]) -> dict:
    return {
        "answer": "Merge to main.",
        "sources": [{"ref": r} for r in refs],
        "confidence": "high",
    }


async def test_searches_then_returns_cited_answer(deps: Deps) -> None:
    model = TestModel(call_tools=["search_docs"], custom_output_args=answer(["alpha/README.md"]))
    result = await build_agent(model).run("how to deploy?", deps=deps)
    assert result.output.sources[0].ref == "alpha/README.md"
    assert result.output.confidence == "high"


async def test_answer_without_searching_is_rejected(deps: Deps) -> None:
    model = TestModel(call_tools=[], custom_output_args=answer(["alpha/README.md"]))
    with pytest.raises(UnexpectedModelBehavior):
        await build_agent(model).run("how to deploy?", deps=deps)


async def test_invented_source_is_rejected(deps: Deps) -> None:
    model = TestModel(call_tools=["search_docs"], custom_output_args=answer(["made/up.md"]))
    with pytest.raises(UnexpectedModelBehavior):
        await build_agent(model).run("how to deploy?", deps=deps)


# --- run records + usage limits (SB-1484) ----------------------------------------------


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list:
    from sb_obs import runs

    records: list = []
    monkeypatch.setattr(runs, "emit", records.append)
    return records


async def test_with_deps_emits_run_record(emitted: list) -> None:
    from docs_agent.agent import WithDeps

    md = "# Install\n\nRun `uv tool install janitor-cli`."
    corpus = Corpus(split_sections("janitor", "README.md", md), {"janitor/README.md": md})
    model = TestModel(
        call_tools=["search_docs"],
        custom_output_args={
            "answer": "uv tool install janitor-cli",
            "sources": [{"ref": "janitor/README.md"}],
            "confidence": "high",
        },
    )
    await WithDeps(build_agent(model), Deps(corpus, KeywordRetriever(corpus)), caller="cli").run(
        "install?"
    )
    [rec] = emitted
    assert (rec.agent, rec.caller, rec.outcome, rec.confidence) == (
        "docs-agent",
        "cli",
        "ok",
        "high",
    )
    assert rec.requests == 2 and rec.tool_calls == 1


async def test_usage_limit_is_recorded_as_limit(emitted: list) -> None:
    from pydantic_ai.exceptions import UsageLimitExceeded
    from pydantic_ai.usage import UsageLimits

    from docs_agent.agent import WithDeps

    md = "# Install\n\nx"
    corpus = Corpus(split_sections("janitor", "README.md", md), {"janitor/README.md": md})
    agent = WithDeps(
        build_agent(TestModel(call_tools=["search_docs"])), Deps(corpus, KeywordRetriever(corpus))
    )
    with pytest.raises(UsageLimitExceeded):
        await agent.run("q", usage_limits=UsageLimits(request_limit=1))
    assert emitted[0].outcome == "limit"


def test_default_usage_limits_are_set() -> None:
    from docs_agent.agent import USAGE_LIMITS

    assert USAGE_LIMITS.request_limit and USAGE_LIMITS.cost_limit
