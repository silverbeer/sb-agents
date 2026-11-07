# motw-agent: weekly Match of the Week suggestions (SB-1545)

MissingTable features one Match of the Week (MOTW). Picking it is a human decision; the
legwork isn't. motw-agent does the legwork on a schedule and posts a ready-to-approve
suggestion to the private Slack channel **#mt-admin-picks**. An approver picks with one
click (or a thread reply), and sb-support writes the pick to MT (SB-1546).

```
k3s CronJob (Tue 10:00 ET) ─► motw-agent suggest
  1. GET  /api/motw?week_start=<Mon>            picked already? note "leaving it alone", stop
  2. mt-mcp suggest_motw_candidates(week)        ranked fixtures + facts with ids (MT SB-1551)
  3. writer (PydanticAI, Haiku) × top 3          blurb + 3-5 reasons, each citing fact ids;
                                                 output validator rejects anything ungrounded
  4. chat.postMessage #mt-admin-picks            blocks + buttons + metadata (the candidates)

k3s CronJob (Thu 18:00 ET) ─► motw-agent remind
  picked → nothing · Tuesday's post skipped → nothing
  unpicked → thread reply on Tuesday's post, @mentioning MOTW_APPROVERS
  Tuesday's post missing (run failed) → post fresh suggestions
```

motw-agent **never writes to MT**. It reads with mt-agent's public read-only account
(`MT_AGENT_MCP_TOKEN`, `X-MT-Client: sb-agents`); `GET /api/motw` is public.

## Layout (`packages/motw-agent`)

| Module | What |
|---|---|
| `candidates.py` | pydantic models of `suggest_motw_candidates`' result; lenient (unknown fields ignored) |
| `mt.py` | `LiveMt`: REST `current_pick(week)`, mt-mcp `candidates(week)`; Eastern-time weeks (Mon-Sun) |
| `writer.py` | the PydanticAI writer, `check_draft` (the validator), `fallback_draft` |
| `slack_post.py` | Block Kit, the metadata contract, `find_suggestion` (history + metadata) |
| `run.py` | `suggest` / `remind`: the two runs, one RunRecord per `suggest` |
| `cli.py` | `motw-agent suggest|remind [--week YYYY-MM-DD] [--dry-run]` |
| `fixtures.py`, `evals.py` | bait fixtures; `motw-agent-eval` (real model, not CI) |

## The writer: model writes, code decides

One model call per candidate (plus retries). The model gets the fixture and MT's facts,
each with an id (`4096:gap`, `4096:pos:home`, `4096:rule:hg-cup`), and returns
`{blurb, reasons: [{text, kind, fact_ids}]}`. `check_draft` is the output validator; a
problem goes back to the model as a `ModelRetry` naming exactly what to fix (2 retries).
A draft that still fails is replaced by `fallback_draft`: MT's own fact texts, marked
"Plain facts" in Slack, so a bad model day never blocks the week.

| Rule | Deterministic check |
|---|---|
| Every reason grounded | cites ≥1 fact id that exists on this candidate; `kind` is the kind of a cited fact |
| No invented numbers | every number in a reason appears in its cited facts; in the blurb, in some fact or the age group / date / kickoff |
| Absent is never zero | follows from the number check (MT emits no fact for a stat it lacks; the prompt shows a team with no games as `games_played: 0` and no points/position), plus "zero/winless/scoreless/unbeaten/..." only if a cited fact says it |
| No invented rankings | a superlative in a reason ("most", "best", "toughest", ...) only if a cited fact uses it |
| Qualification only from rules | text matching `QUALIFICATION` (qualify, playoff, MLS NEXT Cup, Fest, bye, berth, seed, promotion, ...) must cite a `:rule:` fact (reason) or the candidate must have one (blurb) |
| **Unconfirmed stays unconfirmed** | a reason citing a fact with `confirmed: false` must contain a `HEDGE` word: `unconfirmed`, `not yet confirmed`, `if confirmed`, `pending confirmation`, `reportedly` or `expected` (whole words, any case). A blurb that talks qualification when no rule fact is confirmed must too |
| MT's API limits | reason 1-200 chars, kind ∈ standings/form/stakes/rivalry/storyline, 3-5 reasons (fewer only if MT gave fewer facts), blurb ≤ 280 |

The hedge check is a word list, not understanding: "expected to earn a bye" passes, and
so would a sentence that says "expected" about something else. The eval's judge is the
second line for that.

## Evals (real model, not CI)

```bash
make motw-eval                         # 4 fixture cases, ~$0.04 (Haiku writer + Sonnet judge)
op run --env-file=.env.op --env-file=.env.mt.op -- uv run motw-agent-eval --live  # + this week's real top 3
```

Fixtures each bait one failure: `top-clash` (confirmed rule, rivalry), `low-sample` (a
team with 0 games and no table row), `unconfirmed-rule` (Fest bye, `confirmed: false`),
`no-rules` (top of the table, `qualifying_line` flag, no rule fact). Checks:
`ModelDraft` (no fallback), `Grounded` (`check_draft` clean), `NoInventedQualification`,
`NoZeroForAbsent`, and an `LLMJudge` (Sonnet) on "every line is supported by the facts
it cites".

### Results (2026-11-07, Haiku 5.5)

| Prompt | Runs | Passed | What the judge caught |
|---|---|---|---|
| first | 1 | 3/4 | "one of the most dangerous attacks in the division" (a ranking no fact states) → `SUPERLATIVES` check |
| + superlatives | 2 | 3/4, 4/4 | blurb "3rd place at stake" (a stake no fact states) → blurb rule in the prompt |
| + blurb stakes | 2 | 4/4, 2/4 | "5-0-2" read as 5 wins, 2 draws (it is W-D-L); a claim from an uncited fact |
| + records, cite every fact | 2 | **4/4, 4/4** | — |

Writer cost ~$0.001 per candidate, so ~$0.003 per weekly run. The deterministic checks
passed 100% on every run; every failure was the judge's. The W-D-L misread would be
removed at the source if MT wrote records as "5W 0D 2L" (open question for MT).

## The Slack message and its metadata (contract with sb-support)

Blocks: a header, coverage ("Top 3 of 22 eligible fixtures"), then per candidate a
`motw_cand_<n>` section (teams with table position, table, competition, date/kickoff,
score, flags, and a :warning: **low-sample** caveat naming the team and its games), a
`motw_draft_<n>` section (blurb + numbered reasons with their kind), and a divider; then
the `motw_actions` buttons `Pick #1..#3` (`motw_pick_<n>`) and `Skip` (`motw_skip`), and
a context line with the thread-reply grammar.

Message metadata carries what approval needs, so sb-support keeps no state:

```json
{"event_type": "motw_suggestion",
 "event_payload": {"v": 1, "week_start": "2026-11-09", "status": "open",
   "candidates": [{"n": 1, "match_id": 4096, "label": "IFA vs NEFC",
                   "table": "U15 Homegrown Northeast", "blurb": "...",
                   "reasons": [{"text": "...", "kind": "standings"}]}]}}
```

`status` becomes `picked` or `skipped` when sb-support updates the message; Thursday's
reminder skips a `skipped` week. Change the contract in both packages and bump `v`.
Reading it back needs `conversations.history` with `include_all_metadata=true`.

## Running it

```bash
op run --env-file=.env.op --env-file=.env.mt.op -- uv run motw-agent suggest --dry-run
op run --env-file=.env.op --env-file=.env.mt.op -- uv run motw-agent suggest --week 2026-11-12 --dry-run
```

`--dry-run` prints the `chat.postMessage` arguments (blocks + metadata) instead of
posting; it still reads MT and calls the model. `remind --dry-run` still reads Slack
history when `SLACK_BOT_TOKEN` is set. Exit 0 for every expected outcome (posted,
already picked, nothing eligible, reminded, skipped), 1 when MT or Slack failed; MT
failures also post a generic note to the channel (details in the logs only).

| Env | Source |
|---|---|
| `SLACK_BOT_TOKEN` | `op://agents/sb-slack/bot-token` (sb-support's bot, so its approval handler owns the message) |
| `ANTHROPIC_API_KEY` | `op://agents/anthropic_key/password` |
| `MT_AGENT_MCP_TOKEN` | `op://agents/mt-agent/token` (MT read account; expires ~2026-12-02) |
| `MOTW_SLACK_CHANNEL` | #mt-admin-picks channel id (`C...`); ConfigMap `motw-agent` |
| `MOTW_APPROVERS` | comma-separated Slack user ids to @mention; ConfigMap `motw-agent` |
| `MOTW_AGENT_MODEL` | optional, default `anthropic:claude-haiku-5-5` (registry) |
| `MT_API_URL` / `MT_MCP_URL` | optional, default `https://api.missingtable.com` / `.../mcp/` |

## Deploying (k3s on the mini)

`k3s/motw-agent.yaml`: two CronJobs, `motw-agent-suggest` (`0 10 * * 2`) and
`motw-agent-remind` (`0 18 * * 4`), both `timeZone: America/New_York`,
`startingDeadlineSeconds: 21600` (a run missed while the mini slept fires on wake, up
to 6 h late), `concurrencyPolicy: Forbid`, and `backoffLimit: 0`: a retried suggest
would post twice, so a failure is a note in the channel plus the Job's logs.

Secret `motw-agent` (Anthropic, Slack bot, MT read token) comes from
`k3s/provision-secret.sh` like the others. The channel and approvers are a ConfigMap,
created once by hand; `deploy.sh` applies the CronJobs only when it exists:

```bash
kubectl --context rancher-desktop -n sb-agents create configmap motw-agent \
  --from-literal=MOTW_SLACK_CHANNEL=C0XXXXXXX --from-literal=MOTW_APPROVERS=U0XXXXXXX
make deploy-apply
# a run now, without waiting for Tuesday:
kubectl --context rancher-desktop -n sb-agents create job motw-test --from=cronjob/motw-agent-suggest
```

Slack: the bot must be **invited to #mt-admin-picks** (private) and have `chat:write`
and `groups:history` (reading the channel for Thursday's lookup).
