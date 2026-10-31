"""Load SB repo markdown into heading-level sections and search them.

Only git-tracked `*.md` files are indexed, so vendored and generated files
(node_modules, build output) never enter the corpus.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

DEFAULT_REPOS: tuple[str, ...] = (
    "missing-table",
    "match-scraper",
    "trd",
    "bet",
    "cycle-runner",
    "dotfiles",
    "mt-ios",
    "missing-table-android",
    "myrunstreak.run",
    "janitor",
    "qualityplaybook.dev",
)

HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_\-.]*[a-z0-9]|[a-z0-9]")
FENCE_RE = re.compile(r"^\s*(```|~~~)")

STOPWORDS = frozenset(
    "a an and are as at be by can do does for from how i in is it my of on or "
    "the this to was what when where which who why will with you your".split()
)


@dataclass(frozen=True)
class Section:
    repo: str
    path: str  # relative to the repo root
    heading: str  # "" for text before the first heading
    line: int  # 1-indexed line of the heading
    text: str

    @property
    def ref(self) -> str:
        return f"{self.repo}/{self.path}"


@dataclass(frozen=True)
class Hit:
    section: Section
    score: float
    snippet: str


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN_RE.findall(text.lower()) if t not in STOPWORDS]


def split_sections(repo: str, path: str, markdown: str) -> list[Section]:
    """Split a markdown file at #, ## and ### headings (ignoring headings inside code fences)."""
    sections: list[Section] = []
    heading, start, buf = "", 1, []
    in_fence = False

    def flush() -> None:
        text = "\n".join(buf).strip()
        if text or heading:
            sections.append(Section(repo, path, heading, start, text))

    for n, line in enumerate(markdown.splitlines(), 1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
        m = None if in_fence else HEADING_RE.match(line)
        if m:
            flush()
            heading, start, buf = m.group(2), n, []
        else:
            buf.append(line)
    flush()
    return sections


def tracked_markdown(repo_dir: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo_dir), "ls-files", "-z", "*.md"],
        capture_output=True,
        check=True,
    ).stdout
    return [p for p in out.decode().split("\0") if p]


@dataclass
class Corpus:
    sections: list[Section]
    files: dict[str, str] = field(default_factory=dict)  # ref -> full markdown

    @classmethod
    def load(cls, root: Path | None = None, repos: tuple[str, ...] = DEFAULT_REPOS) -> Corpus:
        root = root or Path(os.environ.get("DOCS_AGENT_ROOT", Path.home() / "gitrepos"))
        sections: list[Section] = []
        files: dict[str, str] = {}
        for repo in repos:
            repo_dir = root / repo
            if not (repo_dir / ".git").exists():
                continue
            for rel in tracked_markdown(repo_dir):
                try:
                    md = (repo_dir / rel).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                files[f"{repo}/{rel}"] = md
                sections.extend(split_sections(repo, rel, md))
        return cls(sections, files)

    def read(self, ref: str, heading: str | None = None, max_chars: int = 12_000) -> str:
        """Return a whole file, or one section of it, truncated to `max_chars`."""
        if ref not in self.files:
            raise KeyError(ref)
        if heading:
            wanted = heading.strip().lower()
            for s in self.sections:
                if s.ref == ref and s.heading.lower() == wanted:
                    text = f"## {s.heading}\n{s.text}"
                    break
            else:
                raise KeyError(f"{ref}#{heading}")
        else:
            text = self.files[ref]
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[truncated: {len(text) - max_chars} more chars]"
        return text


class Retriever(Protocol):
    """Anything that can rank corpus sections for a query (keyword now; qmd in SB-1483)."""

    def search(self, query: str, limit: int = 8) -> list[Hit]: ...


class KeywordRetriever:
    """BM25 over sections, with extra weight for terms in the heading or file path."""

    def __init__(self, corpus: Corpus, k1: float = 1.4, b: float = 0.75) -> None:
        self.corpus = corpus
        self.k1, self.b = k1, b
        self.docs: list[Counter[str]] = []
        self.meta: list[set[str]] = []
        df: Counter[str] = Counter()
        for s in corpus.sections:
            tf = Counter(tokenize(s.text))
            self.docs.append(tf)
            self.meta.append(set(tokenize(f"{s.heading} {s.path}")))
            df.update(tf.keys() | self.meta[-1])
        n = max(len(self.docs), 1)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self.avg_len = sum(sum(d.values()) for d in self.docs) / n if self.docs else 1.0

    def search(self, query: str, limit: int = 8) -> list[Hit]:
        terms = set(tokenize(query))
        if not terms:
            return []
        scored: list[tuple[float, int]] = []
        for i, tf in enumerate(self.docs):
            length = sum(tf.values()) or 1
            score = 0.0
            for t in terms:
                idf = self.idf.get(t)
                if idf is None:
                    continue
                f = tf.get(t, 0)
                if f:
                    norm = self.k1 * (1 - self.b + self.b * length / self.avg_len)
                    score += idf * f * (self.k1 + 1) / (f + norm)
                if t in self.meta[i]:
                    score += 1.5 * idf
            if score > 0:
                scored.append((score, i))
        scored.sort(reverse=True)
        return [
            Hit(self.corpus.sections[i], round(s, 3), snippet(self.corpus.sections[i].text, terms))
            for s, i in scored[:limit]
        ]


def snippet(text: str, terms: set[str], width: int = 300) -> str:
    """The `width`-char window of `text` around the first query-term match."""
    low = text.lower()
    pos = min((p for t in terms if (p := low.find(t)) >= 0), default=0)
    start = max(0, pos - width // 3)
    out = text[start : start + width].replace("\n", " ").strip()
    return ("…" if start else "") + out + ("…" if start + width < len(text) else "")
