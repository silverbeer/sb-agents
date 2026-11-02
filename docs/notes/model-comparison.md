# Model comparison: docs-agent evals (SB-1490)

Same 14-case docs-agent eval (13 before SB-1476 added `partial-oncall-name`), same
Sonnet 5.5 judge, same per-run caps (8 requests, 12 tool calls, 60k tokens).
OpenRouter rows measured 2026-11-01: `:free` first on a key with no credits, then paid
models after a $25 credit purchase.

| Model | Where | Passed | Errors | Agent $ / eval | Latency per run | Notes |
|---|---|---|---|---|---|---|
| claude-haiku-5-5 | Anthropic API | **14/14 (100%)** | 0 | $0.022 (~$0.0016/question) | ~4.6 s | baseline, default |
| gemma4:12b (Ollama default 4k ctx) | mac mini | 4/13 (31%) | 8 | $0 | p50 261 s | 5x context overflow, 2x request cap, 1x bad output |
| gemma4:12b-16k (`num_ctx 16384`) | mac mini | 11/14 (79%) | 2 | $0 | p50 516 s* | 1x bad output, 1x request cap; 1 missed expected source |
| nvidia/nemotron-3.5-super-vl:free | OpenRouter | **13/14 (93%)** | 0 | $0 | p50 8.2 s, p95 22.4 s | 1 missed expected source (`linear-done`); ~3.2 requests/case |
| google/gemma-4-31b-it:free | OpenRouter | — | — | — | — | 429 on first request, 2 tries: "rate-limited upstream" (shared Google AI Studio pool) |
| google/gemma-4-26b-a4b-it:free | OpenRouter | — | — | — | — | same 429, upstream shared pool |
| z-ai/glm-5.3-flash | OpenRouter | **14/14 (100%)** | 0 | $0.021 (~$0.0015/question) | p50 6.8 s, p95 26.6 s | matches Haiku on score and price, slower tail |
| deepseek/deepseek-v4.1-flash | OpenRouter | **13/14 (93%)** | 1 | $0.0025 (~$0.0002/question) | p50 3.4 s, p95 5.3 s | fastest and ~9x cheaper than Haiku; the 1 error was our 60k token cap (66k used), not a wrong answer |
| openai/gpt-6-luna | OpenRouter | inconclusive (4/5 that ran) | 9-10 | ~$0.002 for 5 cases | p50 ~6 s | 429 "new accounts are limited to 20 requests per minute"; 1 tool-retry failure (`read_doc`) |
| google/gemma-4-31b-it:free (with credits) | OpenRouter | 0/14 | 14 | — | — | every case 429'd upstream; credits don't help a shared provider pool |

\* Eval runs 4 cases at a time against one Ollama, so this includes queueing. A single
run took about 100 s (SB-1490 smoke test: 4 requests, 6.9k in / 1.5k out tokens).

Judge cost per eval: $0.046 (Haiku), $0.016 (gemma 4k: only 5 cases reached the judge),
$0.039 (gemma 16k).

## Findings

- **Two cheap paid models match Haiku.** GLM-5.3-flash scored 14/14 at Haiku's price.
  DeepSeek-v4.1-flash scored 13/14 at about 1/9 of Haiku's cost and was the fastest model
  tested (p50 3.4 s); its one failure was docs-agent's 60k-token cap, not a bad answer.
  One eval run each, so this is a signal, not a verdict: run 2-3 more before changing a default.
- **Computed cost is close to the bill.** genai-prices priced every paid OpenRouter run;
  the agent-side total was $0.028 against $0.0307 billed by OpenRouter (~9% low; the
  difference isn't explained yet). Good enough for dashboards; reconcile monthly.
- **New OpenRouter accounts are rate-limited per model** (20 requests/minute on
  gpt-6-luna). The eval runs 4 cases at once at ~3.4 requests each, so it trips that
  limit; a Slack bot at SB volume wouldn't.

- **A free OpenRouter model passed the bar.** nemotron-3.5-super-vl `:free` scored 13/14
  (Calibrated 100%, LLMJudge 100%, CitedExpectedSource 92%) at p50 8.2 s, about 2x
  Haiku's latency and 50x faster than local gemma. Tool calling and structured output
  worked through PydanticAI's `openrouter:` provider with no code change.
- **Free is not a serving tier.** Without credits the key gets 50 free requests/day
  (`GET /api/v1/key` → `free_model_daily_requests`). One full eval is ~45 agent requests,
  so it's one eval a day, and a Slack bot doing ~4 requests per question gets ~12
  questions a day. Popular free models (both Gemma 4 variants) were 429'd by the
  upstream provider's shared pool before our quota mattered. Free models also come and go
  from the catalog; 8 of them support tools + structured output today.
- **ADK can't use OpenRouter or Ollama in this workspace.** ADK reaches non-Google,
  non-Anthropic models only through LiteLLM (`google.adk.models.lite_llm`). ADK 2.13 pins
  `litellm>=1.84,<1.106.3`, those releases need `openai<3`, and `pydantic-ai-slim[openai]`
  2.64 needs `openai>=3.31`. One uv workspace = one environment, so they can't coexist.
  Options: a separate (non-workspace) env for ADK + LiteLLM, a small custom ADK `BaseLlm`
  for OpenAI-compatible APIs, or wait for ADK to lift its LiteLLM cap. The ADK router
  stays on Claude direct for now.

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
op run --env-file=.env.op -- uv run docs-agent eval --model openrouter:nvidia/nemotron-3.5-super-vl:free
```

Check the free quota before a run (prints counts only, never the key):

```bash
op run --env-file=.env.op -- sh -c 'curl -s https://openrouter.ai/api/v1/key \
  -H "Authorization: Bearer $OPENROUTER_API_KEY"' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['free_model_daily_requests'])"
```

## Paid candidates (once credits exist)

From the 2026-11-01 catalog, filtered to tools + structured output, per million tokens
(input / output): deepseek/deepseek-v4.1-flash $0.045 / $0.50, z-ai/glm-5.3-flash
$0.029 / $0.092, openai/gpt-6-luna $0.10 / $0.50, qwen/qwen3.8-flash $0.15 / $0.47.
Haiku 5.5 is $0.10 / $0.50 direct, so the cheap paid tier is about the same price; only
GLM flash is clearly cheaper. The reason to run them is quality per dollar, not price.

## Recommendation per role (so far)

- **Specialist (docs-agent):** keep Haiku for now. DeepSeek-v4.1-flash is the candidate to
  replace it (9x cheaper, faster, 13/14), pending repeat evals and a token cap decision.
  GLM-5.3-flash is a like-for-like fallback. nemotron `:free` is fine for experiments.
- **Router (sb-support / ADK):** keep Haiku; the ADK side can't switch yet (LiteLLM conflict).
- **Judge:** keep Sonnet 5.5 so scores stay comparable.
