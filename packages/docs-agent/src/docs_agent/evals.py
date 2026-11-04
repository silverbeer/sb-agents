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

from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models import Model
from pydantic_evals import Case, Dataset
from pydantic_evals.dataset import increment_eval_metric
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, LLMJudge

from docs_agent.agent import USAGE_LIMITS, Deps, DocsAnswer, WithDeps, build_agent
from docs_agent.corpus import Corpus, KeywordRetriever, Retriever
from sb_obs import EvalRecord, emit_eval, model_name
from sb_obs.evals import PricedModel, percentile, summarize

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
    # Open-ended (SB-1530): broad questions where a model that searches wide runs into
    # the request / tool-call caps. DeepSeek-v4.1-flash passed the 14 narrow cases but hit
    # the 12-call cap live on the first two (SB-1490). Neither is in the corpus:
    # sb-agents isn't indexed, so the right answer is "the docs don't cover it", found
    # within budget.
    Case(
        name="open-sb-agents-k3s",
        inputs="How do I deploy sb-agents to k3s?",
        expected_output="The docs don't cover deploying sb-agents.",
        metadata=Meta(answerable=False),
    ),
    Case(
        name="open-mt-agent-auth",
        inputs="How does mt-agent authenticate to MissingTable?",
        expected_output="The docs don't describe how mt-agent authenticates.",
        metadata=Meta(answerable=False),
    ),
    Case(
        name="open-janitor-overview",
        inputs="What can the janitor CLI do for my machine?",
        expected_output=(
            "`jt` does workstation and platform housekeeping: doctor (health summary), docker "
            "(usage, prune), disk (usage, large files), brew (outdated, cleanup), logs (large "
            "and stale), supabase (status, backups), secrets, and k3s (health, cleanup). "
            "Destructive commands honour --dry-run and --yes."
        ),
        metadata=Meta(["janitor/README.md"]),
    ),
    Case(
        name="open-mt-deploy-healthy",
        inputs="After a missing-table production deploy, how do I check it's healthy?",
        expected_output=(
            "Check the health and version endpoints (missingtable.com/health, /api/version), "
            "kubectl get deployments and pods in missing-table-prod, run "
            "./scripts/health-check.sh prod, and check the version footer in the browser."
        ),
        metadata=Meta(["missing-table/docs/05-deployment/production-runbook.md"]),
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
    requests: list[float] = []
    tool_calls: list[float] = []
    limits = 0

    async def answer(question: str) -> DocsAnswer:
        nonlocal limits
        try:
            result = await agent.run(question)
        except UsageLimitExceeded:
            limits += 1  # over budget: no usage to report, the case fails
            raise
        usage = result.usage
        if usage.cost is not None:
            agent_cost.append(float(usage.cost))
        requests.append(usage.requests)
        tool_calls.append(usage.tool_calls)
        increment_eval_metric("requests", usage.requests)
        increment_eval_metric("tool_calls", usage.tool_calls)
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
        requests_p50=percentile(requests, 50),
        requests_p95=percentile(requests, 95),
        tool_calls_p50=percentile(tool_calls, 50),
        tool_calls_p95=percentile(tool_calls, 95),
        limit_failures=limits,
    )
    emit_eval(record)
    print(f"\n{passed}/{total} cases passed ({rate:.0%}); threshold {PASS_THRESHOLD:.0%}")
    print("per evaluator: " + ", ".join(f"{k} {v:.0%}" for k, v in scores.items()))
    print(
        f"budget: requests p50 {record.requests_p50} p95 {record.requests_p95}, tool calls "
        f"p50 {record.tool_calls_p50} p95 {record.tool_calls_p95}; "
        f"{limits} case(s) hit a usage cap (caps: {USAGE_LIMITS.request_limit} requests, "
        f"{USAGE_LIMITS.tool_calls_limit} tool calls)"
    )
    cost = record.cost_usd
    print(f"cost: ${cost:.4f}" if cost is not None else "cost: unknown", end="")
    agent_cost = "unpriced" if record.agent_cost_usd is None else f"${record.agent_cost_usd:.4f}"
    print(f" (agent {agent_cost}, judge ${judge.cost_usd or 0:.4f})")
    for f in report.failures:
        print(f"  ERROR {f.name}: {f.error_message}")
    return rate
