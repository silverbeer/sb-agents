"""Eval set for docs-agent. Runs against the real model; not part of CI.

    op run --env-file=.env.op -- uv run docs-agent eval

Each case has a reference answer (for the LLM judge) and the file(s) the answer
should be cited from. A case passes when every assertion passes. The run passes
when at least PASS_THRESHOLD of cases pass.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, LLMJudge

from docs_agent.agent import Deps, DocsAnswer, WithDeps, build_agent
from docs_agent.corpus import Corpus, KeywordRetriever, Retriever

PASS_THRESHOLD = 0.8
JUDGE_MODEL = os.environ.get("DOCS_AGENT_JUDGE_MODEL", "anthropic:claude-sonnet-5-5")

CLAUDE_MD = "dotfiles/dot_claude/CLAUDE.md"


@dataclass
class Meta:
    sources: list[str] = field(default_factory=list)  # any one of these counts as a hit
    answerable: bool = True


CASES: list[Case[str, str, Meta]] = [
    Case(
        name="op-agents-vault",
        inputs="Which 1Password vault can agent shells read, and why can't they use Personal?",
        expected_output=(
            "Only the `agents` vault. Agent shells use a service-account token, and a "
            "1Password service account can never be granted the Personal vault: a product "
            "constraint, so retrying will not help."
        ),
        metadata=Meta([CLAUDE_MD]),
    ),
    Case(
        name="chezmoi-source-path",
        inputs="What should `chezmoi source-path` print, and what do I do if it doesn't?",
        expected_output=(
            "It must print ~/gitrepos/dotfiles. Otherwise fix `sourceDir` in "
            "~/.config/chezmoi/chezmoi.toml before editing anything."
        ),
        metadata=Meta([CLAUDE_MD, "dotfiles/SETUP.md"]),
    ),
    Case(
        name="chezmoi-new-skill",
        inputs="I added a new Claude skill but chezmoi add synced nothing. Why?",
        expected_output=(
            ".chezmoiignore ignores .claude/skills/* wholesale; add an explicit "
            "`!.claude/skills/<name>` exception before `chezmoi add`."
        ),
        metadata=Meta([CLAUDE_MD, "dotfiles/README.md"]),
    ),
    Case(
        name="mt-prod-deploy",
        inputs="How do I deploy missing-table to production, and how do I roll back?",
        expected_output=(
            "Merge to main: the deploy-prod GitHub workflow builds images and deploys with "
            "Helm, with health checks. Roll back with `helm rollback` on the release."
        ),
        metadata=Meta(["missing-table/docs/05-deployment/production-runbook.md"]),
    ),
    Case(
        name="jt-install",
        inputs="How do I install the janitor `jt` command globally?",
        expected_output=(
            "`uv tool install --from git+https://github.com/silverbeer/janitor.git janitor-cli`"
        ),
        metadata=Meta(["janitor/README.md"]),
    ),
    Case(
        name="trd-market-data",
        inputs="Where does trd get its market data from, and where does my data live?",
        expected_output="Free market data from yfinance; trd is local-first, data stays local.",
        metadata=Meta(["trd/README.md", "trd/DESIGN.md"]),
    ),
    Case(
        name="bet-remote-mode",
        inputs="How do I make the bet CLI use the BET API instead of the local warehouse?",
        expected_output=(
            "Set `api_url`; every command then goes through the BET API (remote mode) and "
            "the local warehouse is never opened."
        ),
        metadata=Meta(["bet/README.md"]),
    ),
    Case(
        name="stk-smashrun-secret",
        inputs="Does the stk CLI need the SmashRun client secret to sync runs?",
        expected_output=(
            "No. SmashRun OAuth is handled server-side by the backend; the web app and stk "
            "CLI are thin clients and never see the client secret."
        ),
        metadata=Meta(["myrunstreak.run/docs/SMASHRUN_OAUTH.md", "myrunstreak.run/QUICKSTART.md"]),
    ),
    Case(
        name="android-release",
        inputs="How do I cut a release of the Missing Table Android app?",
        expected_output=(
            "Push a git tag (e.g. v0.1.1) from main; the Android Release workflow builds the "
            "signed APK and uploads it to Cloudflare R2."
        ),
        metadata=Meta(["missing-table-android/docs/RELEASE_SETUP.md"]),
    ),
    Case(
        name="cycle-runner-where",
        inputs="Where does cycle-runner run, and can I run it on a second machine too?",
        expected_output=(
            "It is a k3s CronJob on the machine hosting the cluster (the Mac mini). Do not "
            "deploy it on a second machine: two schedulers would race for the same tickets."
        ),
        metadata=Meta(["dotfiles/SETUP.md", "dotfiles/k3s/cycle-runner/README.md"]),
    ),
    Case(
        name="match-scraper-k3s",
        inputs="What's the one-command way to deploy match-scraper to k3s, and what does it do?",
        expected_output=(
            "`./scripts/deploy-k3s.sh`: builds the Docker image, imports it into k3s, deploys "
            "RabbitMQ and the match-scraper CronJob, and verifies they are running."
        ),
        metadata=Meta(["match-scraper/docs/deployment/k3s-deployment.md"]),
    ),
    Case(
        name="linear-done",
        inputs="What moves a Linear SB ticket to Done?",
        expected_output=(
            "Merging a PR whose body contains `Fixes SB-N` (from a branch carrying sb-N); "
            "the GitHub integration does it automatically."
        ),
        metadata=Meta([CLAUDE_MD, "dotfiles/dot_claude/skills/linear-crud/SKILL.md"]),
    ),
    Case(
        name="unanswerable-oncall",
        inputs="Who is on call for the payments service this week?",
        expected_output="The docs do not cover this; there is no payments service on-call info.",
        metadata=Meta(answerable=False),
    ),
]


@dataclass
class CitedExpectedSource(Evaluator[str, DocsAnswer, Meta]):
    """At least one expected file is cited (answerable cases only)."""

    def evaluate(self, ctx: EvaluatorContext[str, DocsAnswer, Meta]) -> bool | dict[str, bool]:
        meta = ctx.metadata
        if meta is None or not meta.answerable:
            return {}
        cited = {s.ref for s in ctx.output.sources}
        return bool(cited & set(meta.sources))


@dataclass
class Calibrated(Evaluator[str, DocsAnswer, Meta]):
    """Unanswerable questions must come back with low confidence."""

    def evaluate(self, ctx: EvaluatorContext[str, DocsAnswer, Meta]) -> bool | dict[str, bool]:
        if ctx.metadata is None or ctx.metadata.answerable:
            return {}
        return ctx.output.confidence == "low"


def build_dataset() -> Dataset[str, DocsAnswer, Meta]:
    return Dataset(
        name="docs-agent",
        cases=CASES,
        evaluators=[
            CitedExpectedSource(),
            Calibrated(),
            LLMJudge(
                rubric=(
                    "The output's `answer` is consistent with the expected output: it contains "
                    "its key facts and does not contradict it. Extra correct detail is fine. "
                    "For an unanswerable question, the answer must say the docs don't cover it."
                ),
                model=JUDGE_MODEL,
                include_input=True,
                include_expected_output=True,
            ),
        ],
    )


def check_sources(corpus: Corpus) -> list[str]:
    """Expected source files that are missing from the corpus (a broken eval, not a bad agent)."""
    return sorted({s for c in CASES for s in c.metadata.sources if s not in corpus.files})


async def run_evals(retriever: Retriever | None = None, corpus: Corpus | None = None) -> float:
    corpus = corpus or Corpus.load()
    if missing := check_sources(corpus):
        raise SystemExit(f"eval references files not in the corpus: {missing}")
    deps = Deps(corpus, retriever or KeywordRetriever(corpus))
    agent = WithDeps(build_agent(), deps, caller="eval")

    async def answer(question: str) -> DocsAnswer:
        return (await agent.run(question)).output

    report = await build_dataset().evaluate(answer, max_concurrency=4, progress=False)
    report.print(include_input=False, include_output=False, include_durations=True)

    passed = sum(1 for c in report.cases if all(a.value for a in c.assertions.values()))
    total = len(report.cases) + len(report.failures)
    rate = passed / total if total else 0.0
    print(f"\n{passed}/{total} cases passed ({rate:.0%}); threshold {PASS_THRESHOLD:.0%}")
    for f in report.failures:
        print(f"  ERROR {f.name}: {f.error_message}")
    return rate
