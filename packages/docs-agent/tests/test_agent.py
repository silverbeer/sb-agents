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
