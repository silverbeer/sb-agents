# sb-evals

An eval runner for agents, written as a **reference implementation**. It isn't meant to
be installed elsewhere. Point a coding agent at this directory and re-implement it in
your own codebase.

About 300 lines of source. Read in this order: `records.py`, `guard.py`, `suite.py`,
`obs_adapter.py`, then `tests/`.

## The ideas

An ordinary test checks that code does the same thing every time. Model output varies,
so an eval scores samples of it instead.

| Term | Meaning | Here |
|---|---|---|
| Execution | One finished agent run, as data | `ExecutionRecord` |
| Case | One input to score | pydantic-evals `Case` |
| Check (evaluator) | One pass/fail question about an output | pydantic-evals `Evaluator` |
| Judge | A check that asks a stronger model to grade against a written rubric | `LLMJudge` |
| Suite | Cases plus checks, giving one pass rate | a name passed to `run_suite` |
| Sink | Where the suite's result goes | any `SuiteRecord -> None` |

There are two ways outputs reach a suite, and the same checks serve both:

- **Offline, `run_suite`:** calls the agent on fixed fixtures, by hand, before shipping a
  prompt or model change. Fixtures can bait one specific failure each.
- **Online, `score_executions`:** scores what a real run just produced. Fixtures can't
  tell you whether this week's real output was any good.

## Decisions, and why

Keep these when re-implementing, or change them on purpose.

1. **Evals consume a record, not an agent object.** Each agent fills in an
   `ExecutionRecord` at the end of its run. Only that code knows the framework.
2. **A check that raised is not a pass.** The obvious way to count passes looks only at
   assertions that ran, so a judge outage scores 100%. Here the case fails and names the
   check. The most important test is `test_a_check_that_raises_is_not_a_pass`.
3. **Online evals can't hurt the run they score.** The output has already shipped. Wrap
   the work in `guarded`, with a timeout inside the host's own deadline: a failed Job
   might retry and send everything twice.
4. **Online and offline results use different suite names.** Real inputs differ every
   week, so a mixed series can't show whether a prompt change helped.
5. **`summary()` names the check, never exception text.** It can end up in a chat
   channel. Details go to logs. A judge's reason is included, so escape it for your
   channel's markup, as `evaluate_posted` does for Slack.
6. **Deterministic checks restate the agent's own validators on purpose.** A validator
   can be loosened by a later change. The eval records what "good" meant.
7. **An eval that runs after the output ships can flag a bad output but can't stop it.**
   To block, score before sending, and decide what happens when the judge is down.

8. **Every case gets its own log line, not only the suite.** A pass rate says something
   dropped. To ask whether a verdict was right you need the check, the reason and the
   output side by side. The agent supplies `describe_case`, because only it knows which
   parts of its output are worth logging.
9. **Reasons and output sit behind the content switch.** A judge's reason quotes what it
   graded, so it's treated like a prompt. Pass/fail per check always logs.

## What to replace

| Here | Replace with |
|---|---|
| `obs_adapter.py` | A `Sink` for your telemetry. The only file that imports `sb_obs` |
| Judge model | Anything with `model_name` and `cost_usd` (`CostedJudge`) that your eval library can call |
| pydantic-evals | Keep it, or swap it. It's used only in `suite.py`: `Dataset.evaluate` and the report shape |

## Worked example

`packages/motw-agent/src/motw_agent/evals.py` has both modes side by side: `run_evals`
(fixtures) and `evaluate_posted` (real drafts, called from `run.py` right after the Slack
post).

```python
executions = [
    ExecutionRecord(name=f"#{n}", agent="my-agent", model=model, inputs=i, output=o)
    for n, (i, o) in enumerate(finished_runs, 1)
]
result = await guarded(
    lambda: score_executions("my-agent-live", executions, checks, sink=my_sink, judge=judge),
    timeout_s=180,
    fallback=None,
    label="scoring my-agent",
)
```

## Not here yet

Persisting executions so a score can be reproduced later, human ratings, candidate-ranking
evals, YAML rubrics, weighted scoring, and a registry. Add them when an agent needs them.
