import subprocess
from pathlib import Path

import pytest

from docs_agent.corpus import Corpus, KeywordRetriever, split_sections, tokenize

DOC = """\
Intro text.

# Deploy

Merge to main to deploy.

## Rollback

Run `helm rollback`.

```bash
# not a heading
echo hi
```
"""


def test_split_sections_by_heading_ignoring_code_fences() -> None:
    secs = split_sections("r", "a.md", DOC)
    assert [s.heading for s in secs] == ["", "Deploy", "Rollback"]
    assert secs[2].line == 7
    assert "# not a heading" in secs[2].text


def test_tokenize_drops_stopwords_and_keeps_dotted_names() -> None:
    assert tokenize("How do I deploy myrunstreak.run?") == ["deploy", "myrunstreak.run"]


@pytest.fixture
def corpus(tmp_path: Path) -> Corpus:
    for repo, files in {
        "alpha": {"README.md": DOC, "docs/queue.md": "# Queue\n\nRabbitMQ retries failed jobs."},
        "beta": {"README.md": "# Beta\n\nNothing about deploys here."},
    }.items():
        d = tmp_path / repo
        for rel, body in files.items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(body)
        (d / "untracked.md").write_text("# Secret\n\nnot in git")
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        subprocess.run(["git", "-C", str(d), "add", *files], check=True)
    return Corpus.load(tmp_path, repos=("alpha", "beta", "missing"))


def test_load_indexes_only_git_tracked_markdown(corpus: Corpus) -> None:
    assert sorted(corpus.files) == ["alpha/README.md", "alpha/docs/queue.md", "beta/README.md"]


def test_keyword_search_ranks_relevant_section_first(corpus: Corpus) -> None:
    hits = KeywordRetriever(corpus).search("rabbitmq retries")
    assert hits[0].section.ref == "alpha/docs/queue.md"
    assert "RabbitMQ" in hits[0].snippet


def test_heading_match_boosts_score(corpus: Corpus) -> None:
    hits = KeywordRetriever(corpus).search("rollback")
    assert hits[0].section.heading == "Rollback"


def test_search_with_only_stopwords_returns_nothing(corpus: Corpus) -> None:
    assert KeywordRetriever(corpus).search("how do I") == []


def test_read_whole_file_section_and_truncation(corpus: Corpus) -> None:
    assert corpus.read("alpha/README.md").startswith("Intro text.")
    assert corpus.read("alpha/README.md", "rollback").startswith("## Rollback")
    assert "[truncated:" in corpus.read("alpha/README.md", max_chars=10)


@pytest.mark.parametrize(("ref", "heading"), [("nope.md", None), ("alpha/README.md", "Nope")])
def test_read_unknown_raises_keyerror(corpus: Corpus, ref: str, heading: str | None) -> None:
    with pytest.raises(KeyError):
        corpus.read(ref, heading)
