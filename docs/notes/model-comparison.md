# Model comparison: docs-agent evals (SB-1490, local half)

Same 14-case docs-agent eval (13 before SB-1476 added `partial-oncall-name`), same
Sonnet 5.5 judge, same per-run caps (8 requests, 12 tool calls, 60k tokens). The
OpenRouter rows wait on `op://agents/openrouter/api-key` (see open-questions.md).

| Model | Where | Passed | Errors | Agent $ / eval | Latency per run | Notes |
|---|---|---|---|---|---|---|
| claude-haiku-5-5 | Anthropic API | **14/14 (100%)** | 0 | $0.022 (~$0.0016/question) | ~4.6 s | baseline, default |
| gemma4:12b (Ollama default 4k ctx) | mac mini | 4/13 (31%) | 8 | $0 | p50 261 s | 5x context overflow, 2x request cap, 1x bad output |
| gemma4:12b-16k (`num_ctx 16384`) | mac mini | 11/14 (79%) | 2 | $0 | p50 516 s* | 1x bad output, 1x request cap; 1 missed expected source |
| OpenRouter cheap paid x 2-3 | — | pending key | | | | |
| OpenRouter `:free` x 1-2 | — | pending key | | | | |

\* Eval runs 4 cases at a time against one Ollama, so this includes queueing. A single
run took about 100 s (SB-1490 smoke test: 4 requests, 6.9k in / 1.5k out tokens).

Judge cost per eval: $0.046 (Haiku), $0.016 (gemma 4k: only 5 cases reached the judge),
$0.039 (gemma 16k).

## Findings

- **Ollama's default context is 4,096 tokens, and it truncates silently.** docs-agent's
  prompt grows to 8-15k tokens as tool results pile up, so the 4k model failed 5 cases
  with "model token limit exceeded before any response". A derived model with
  `num_ctx 16384` (`ollama create gemma4:12b-16k`) fixed all of them: 31% → 79%. The
  stock `gemma4:12b`, which cycle-runner uses, is untouched. **Check `num_ctx` before
  judging any local model**; cycle-runner may have the same hidden ceiling.
- **Tool calling and structured output work on gemma 12B through PydanticAI's `ollama:`
  provider** with no code change, beyond adding the `openai` extra the provider imports.
- **It's not ready to serve sb-support:** 79% is below the 80% bar, and ~100 s per
  question (20x Haiku) would also trip the p95 alert (20 s).
- **Haiku 5.5 is already very cheap**: about $0.0016 per question. A local model saves
  about $1.60 per 1,000 questions. At SB volumes, cost isn't the reason to go local;
  privacy or offline use would be.
- **Where a local model could fit:** non-interactive work such as the eval *agent*
  side, batch triage summaries, or cycle-runner's existing judgment calls, where 100 s
  is fine. Not the eval *judge*: changing the judge breaks comparability with earlier
  scores.

## How to run

```bash
OLLAMA_BASE_URL=http://localhost:11434/v1 op run --env-file=.env.op -- \
  uv run docs-agent eval --model ollama:gemma4:12b-16k
# later: --model openrouter:<model-id>  (needs OPENROUTER_API_KEY)
```
