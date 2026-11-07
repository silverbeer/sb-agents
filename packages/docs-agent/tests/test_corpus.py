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


# --- SB-1572: excludes, capped reads, deduped hits -------------------------------------


def test_exclude_globs_index_files_out(tmp_path: Path) -> None:
    d = tmp_path / "mt"
    files = {"docs/guide.md": "# Guide\n\nok", "docs/research/notes/fest.md": "# Fest\n\nraw"}
    for rel, body in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(body)
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "add", *files], check=True)

    assert sorted(Corpus.load(tmp_path, repos=("mt",)).files) == ["mt/docs/guide.md"]
    assert len(Corpus.load(tmp_path, repos=("mt",), exclude=()).files) == 2


def test_exclude_env_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from docs_agent.corpus import DEFAULT_EXCLUDE, exclude_from_env

    monkeypatch.delenv("DOCS_AGENT_EXCLUDE", raising=False)
    assert exclude_from_env() == DEFAULT_EXCLUDE
    monkeypatch.setenv("DOCS_AGENT_EXCLUDE", " a/*.md , ,b/x.md")
    assert exclude_from_env() == ("a/*.md", "b/x.md")
    monkeypatch.setenv("DOCS_AGENT_EXCLUDE", "")
    assert exclude_from_env() == ()


def test_truncated_whole_file_read_lists_its_sections() -> None:
    md = "# One\n\n" + "x" * 500 + "\n\n# Two\n\nshort"
    corpus = Corpus(split_sections("r", "big.md", md), {"r/big.md": md})
    out = corpus.read("r/big.md", max_chars=100)
    assert "[truncated:" in out
    assert out.endswith("Sections (read one with heading=...):\n- One\n- Two")
    # a section read is not followed by the outline
    assert "Sections" not in corpus.read("r/big.md", "One", max_chars=50)


def test_search_drops_near_identical_sections() -> None:
    body = "# Cup\n\nFest does not qualify a team for MLS NEXT Cup."
    copy = "# cup\n\nFest  does not qualify a team\nfor MLS NEXT Cup."
    other = "# Flex\n\nFlex group winners qualify for MLS NEXT Cup."
    files = {"r/a.md": body, "r/b.md": copy, "r/c.md": other}
    sections = [s for ref, md in files.items() for s in split_sections("r", ref[2:], md)]
    hits = KeywordRetriever(Corpus(sections, files)).search("fest qualify cup", limit=3)
    assert [h.section.ref for h in hits].count("r/a.md") + [h.section.ref for h in hits].count(
        "r/b.md"
    ) == 1
    assert "r/c.md" in [h.section.ref for h in hits]
