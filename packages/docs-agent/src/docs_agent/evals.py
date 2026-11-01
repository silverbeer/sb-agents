"""Eval set for docs-agent. Runs against the real model; not part of CI.

    op run --env-file=.env.op -- uv run docs-agent eval

Each case has a reference answer (for the LLM judge) and the file(s) the answer
should be cited from. A case passes when every assertion passes. The run passes
when at least PASS_THRESHOLD of cases pass.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai.models import Model, infer_model
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, LLMJudge
from pydantic_evals.reporting import EvaluationReport

from docs_agent.agent import Deps, DocsAnswer, WithDeps, build_agent
from docs_agent.corpus import Corpus, KeywordRetriever, Retriever
from sb_obs import EvalRecord, emit_eval, model_name, price

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
    Case(
        # The docs cover on-call (a placeholder rotation link) but not the fact asked.
        # Caught live in SB-1476: docs-agent said "the docs don't name anyone" at high
        # confidence, so sb-support didn't escalate.
        name="partial-oncall-name",
        inputs="Who is on call for missing-table this week?",
        expected_output="The docs don't say who is on call; they only point at a rotation.",
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


class PricedModel(WrapperModel):
    """Wraps the judge model and prices each request (pydantic-evals reports no cost)."""

    def __init__(self, wrapped: Model | str) -> None:
        super().__init__(infer_model(wrapped) if isinstance(wrapped, str) else wrapped)
        self.cost_usd: float | None = None
        self.requests = 0

    async def request(self, messages: Any, model_settings: Any, params: Any) -> Any:
        response = await super().request(messages, model_settings, params)
        u = response.usage
        cost = price(
            self.model_name,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_tokens,
            cache_write_tokens=u.cache_write_tokens,
        )
        self.requests += 1
        if cost is not None:
            self.cost_usd = (self.cost_usd or 0.0) + cost
        return response


def summarize(report: EvaluationReport) -> tuple[int, int, dict[str, float]]:
    """(cases passed, total cases, fraction passed per assertion name)."""
    passed = sum(1 for c in report.cases if all(a.value for a in c.assertions.values()))
    total = len(report.cases) + len(report.failures)
    results: dict[str, list[bool]] = {}
    for case in report.cases:
        for name, assertion in case.assertions.items():
            results.setdefault(name, []).append(bool(assertion.value))
    scores = {name: sum(vals) / len(vals) for name, vals in sorted(results.items())}
    return passed, total, scores


def build_dataset(judge: Model | str | None = None) -> Dataset[str, DocsAnswer, Meta]:
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
                model=judge or JUDGE_MODEL,
                include_input=True,
                include_expected_output=True,
            ),
        ],
    )


def check_sources(corpus: Corpus) -> list[str]:
    """Expected source files that are missing from the corpus (a broken eval, not a bad agent)."""
    return sorted({s for c in CASES for s in c.metadata.sources if s not in corpus.files})


async def run_evals(retriever: Retriever | None = None, corpus: Corpus | None = None) -> float:
    """Run the eval set against the real model; print the report and emit an EvalRecord."""
    corpus = corpus or Corpus.load()
    if missing := check_sources(corpus):
        raise SystemExit(f"eval references files not in the corpus: {missing}")
    deps = Deps(corpus, retriever or KeywordRetriever(corpus))
    agent = WithDeps(build_agent(), deps, caller="eval")
    judge = PricedModel(JUDGE_MODEL)
    agent_cost: list[float] = []

    async def answer(question: str) -> DocsAnswer:
        result = await agent.run(question)
        if result.usage.cost is not None:
            agent_cost.append(float(result.usage.cost))
        return result.output

    start = time.perf_counter()
    report = await build_dataset(judge).evaluate(answer, max_concurrency=4, progress=False)
    report.print(include_input=False, include_output=False, include_durations=True)

    passed, total, scores = summarize(report)
    rate = passed / total if total else 0.0
    record = EvalRecord(
        suite="docs-agent",
        model=model_name(agent.wrapped.model),
        judge_model=judge.model_name,
        cases=total,
        passed=passed,
        pass_rate=rate,
        scores=scores,
        agent_cost_usd=sum(agent_cost) if agent_cost else None,
        judge_cost_usd=judge.cost_usd,
        duration_s=round(time.perf_counter() - start, 1),
    )
    emit_eval(record)
    print(f"\n{passed}/{total} cases passed ({rate:.0%}); threshold {PASS_THRESHOLD:.0%}")
    print("per evaluator: " + ", ".join(f"{k} {v:.0%}" for k, v in scores.items()))
    cost = record.cost_usd
    print(f"cost: ${cost:.4f}" if cost is not None else "cost: unknown", end="")
    agent_cost = "unpriced" if record.agent_cost_usd is None else f"${record.agent_cost_usd:.4f}"
    print(f" (agent {agent_cost}, judge ${judge.cost_usd or 0:.4f})")
    for f in report.failures:
        print(f"  ERROR {f.name}: {f.error_message}")
    return rate
