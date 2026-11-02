# Open questions (auto-mode session, 2026-11-01)

Decisions made while working through the AGT epic unattended. Each one has the
default I chose, so nothing is blocked except where marked **BLOCKED**.

## Needs you

1. **OpenRouter credits for SB-1490's paid rows.** Key works (added 2026-11-01), but it has
   $0 credits, so only `:free` models run (50 requests/day). nemotron `:free` scored 13/14;
   both Gemma `:free` models were 429'd upstream. Buy $10 to run 2-3 cheap paid models
   and lift the free cap to 1,000/day? (The key's own limit is $20/month.) Also: the ADK
   half of SB-1490 is blocked by a LiteLLM/openai version conflict; pick an option in
   model-comparison.md or drop that AC. Ollama model `gemma4:12b-16k` still exists
   (`ollama rm gemma4:12b-16k`).
2. **fasta2a upstream bugs** (naive timestamps; first streamed chunk `append=True`).
   Filing them is public, so still waiting on your yes.
3. **Doc contradictions cleanup ticket** (match-scraper cron 6h vs daily 14:00; MT roles
   3 vs 4). Not filed; want it?
4. **cycle-runner follow-ups**: (a) adopt sb-obs (CR, 3); (b) coding executor as an A2A
   "propose a fix" specialist (CR, 5, after SB-1475). Not filed.

## Defaults I picked: confirm or change

5. **SB-1485 scope.** The ticket assumed sb-support already called docs-agent; it
   didn't. I added a minimal `ask_docs` A2A tool to sb-support (one specialist, no
   thread history, no escalation) so the trace had a real hop. Routing, thread
   context and escalation stay in SB-1476.
6. **Per-run caps** (SB-1484): docs-agent 8 requests / 12 tool calls / 60k tokens / $0.02;
   sb-support 4 requests / 3 tool calls / 20k tokens / $0.01; ADK router 4 LLM calls.
   Measured docs question: 4 requests, 15k tokens, $0.0019. Gemma used similar request
   counts, so the caps also fit local models.
7. **Alert thresholds** (SB-1487/1488): >1 failed run in 15m; run p95 >20s; >$1 spend per
   24h; docs-agent absent 3m; latest eval <80%. **Note: local gemma's ~100 s runs would
   trip the p95 alert**, so if a local model goes into service the threshold needs to be
   per model, or set by model choice.
8. **Prompt caching left off.** One A/B: ~21% cheaper per request, but a 6-request run
   cost more than a 4-request one. Revisit with multi-turn Slack threads (SB-1476).
9. **Slack token in the otel-lgtm container env** (`-e SLACK_BOT_TOKEN`, value from `op
   run`). Anyone on this machine can see it with `docker inspect`. Fine for a local dev
   box? The alternative is a file mount plus Grafana's `$__file{}` (then the token is a
   file on disk instead).
10. **otel-lgtm data isn't persisted.** `make obs-down obs-up` (needed to change alert
    thresholds or mounts) wipes traces, logs and metrics. Add a volume? It's cheap, but
    Loki and Tempo will keep growing on disk.
11. **genai-prices as the price source** instead of a hand-kept table. Tests pin the
    expected Anthropic prices, so a package update that changes them fails CI. New
    models need a package bump (cost shows as `null`, not 0, until then).
12. **Eval judge = 2/3 of eval cost** ($0.046 of $0.069). A Haiku judge would cut that,
    but scores would stop being comparable with earlier runs. I left Sonnet.

- **cycle-runner may be running gemma at a 4k context too** (Ollama's default). The
  docs-agent eval went from 31% to 79% with `num_ctx 16384`. Worth checking there.
- **SB-1476 decisions**: thread history is in memory (lost on restart); escalation is a
  rule (failed or low-confidence specialist), not the model's call; follow-ups work
  without a re-mention. docs-agent calibration changed ("topic covered, fact missing" =
  low), plus a new eval case; still 14/14 on Haiku.

## Later

13. **Weekly eval schedule: where does it run?** launchd on the mac mini (simplest),
    a k3s CronJob (after SB-1481), or GitHub Actions (needs `ANTHROPIC_API_KEY` as a
    repo secret on a public repo, about $0.07 per run). On demand (`make eval`) for now.
14. **Liveness = `target_info` absence.** It works (alert fired after 4.5 min), but a
    short CLI run under the same service name hides a dead server. Real health checks
    come with k3s (SB-1481).
15. **Logfire POC (SB-1489)**: nothing so far needed it. Grafana covered traces, cost and
    alerts. Keep watching.
16. **Grafana Cloud free tier** for SB-1481. The limits I found were third-party only;
    check grafana.com/pricing before moving.

## What auto mode spent and posted

- Real API calls: about $0.45 total (smoke tests, the caching A/B, two Haiku evals, two
  gemma evals' Sonnet judging, SB-1476 live checks).
- `#sb-alerts`: 1 contact-point test, 4 rules fired on purpose (SB-1487), 1 synthetic
  eval-drop alert (SB-1488). Resolved notices were never sent, because the stack was
  recreated to restore the thresholds.
