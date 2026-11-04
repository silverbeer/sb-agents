# Model comparison: docs-agent evals (SB-1490)

Same 14-case docs-agent eval (13 before SB-1476 added `partial-oncall-name`), same
Sonnet 5.5 judge, same per-run caps (8 requests, 12 tool calls, 60k tokens; 100k from 2026-11-04).
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

### Repeat runs (2026-11-04)

Two more runs each before changing a default, as the findings below asked.

| Model | Token cap | Run | Passed | Agent $ / eval | Avg latency per case | Failure |
|---|---|---|---|---|---|---|
| deepseek/deepseek-v4.1-flash | 60k | 2 | 13/14 | $0.0027 | 5.6 s | `mt-prod-deploy`: 70,254 tokens > 60k cap |
| deepseek/deepseek-v4.1-flash | 60k | 3 | 13/14 | $0.0020 | 4.8 s | `mt-prod-deploy`: 73,852 tokens > 60k cap |
| deepseek/deepseek-v4.1-flash | 100k | 4 | **14/14** | $0.0026 | — | — |
| deepseek/deepseek-v4.1-flash | 100k | 5 | **14/14** | $0.0025 | — | — |
| z-ai/glm-5.3-flash | 60k | 2 | **14/14** | $0.0178 | 5.6 s | — |
| z-ai/glm-5.3-flash | 60k | 3 | **14/14** | $0.0161 | 5.4 s | — |

DeepSeek's only failure, three runs out of three, was the same case hitting the token
cap: it reads more doc text than Haiku on `mt-prod-deploy`, then answers correctly.
The cap went to 100k (`docs_agent/agent.py`): `cost_limit` ($0.02) is the money guard,
and the token cap only stops a runaway loop. All judge scores (Calibrated,
CitedExpectedSource, LLMJudge) were 100% on every run that finished. Haiku's 14/14 was at
60k and never came near the cap, so the change doesn't affect its result.

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

### Live check after deploy (2026-11-04): reverted

DeepSeek went live as docs-agent's default and was rolled back within minutes. Two real
questions, outside the eval set, against the same corpus:

| Question | Haiku | DeepSeek |
|---|---|---|
| How do I deploy sb-agents to k3s? | 4 requests, 19k in tokens | 7 requests, 37k in tokens (in k3s: 12 tool calls, cap hit) |
| How does mt-agent authenticate to MissingTable? | 5 requests, 28k in tokens | **failed:** 15 tool calls > 12 cap |

DeepSeek searches wide (several parallel `search_docs` / `read_doc` calls per turn).
The eval's cases are narrow enough that it stays under the caps, so 14/14 hid this.
**The eval measures answer quality, not tool-call budget on open questions.** Before a
model replaces Haiku again: add open-ended cases (e.g. these two) to the eval and record
tool calls per case (p50/p95) next to the pass rate. Raising `tool_calls_limit` to fit
one model would hide the same signal.

### Tool-call budget in the eval (SB-1530, 2026-11-04)

The eval now has four open-ended cases (18 in all) and reports what each case cost in
model requests and tool calls: per case in the report, p50/p95 in the summary line and
the `EvalRecord` (`requests_p50/p95`, `tool_calls_p50/p95`, `limit_failures`). Two of the
new cases are the questions DeepSeek failed live; neither is in the corpus (sb-agents
isn't indexed), so passing means saying "not covered" within budget.

| Model | Passed | Requests p50 / p95 | Tool calls p50 / p95 | Hit a cap | Agent $ / eval |
|---|---|---|---|---|---|
| claude-haiku-5-5 (run 1) | **18/18** | 3 / 7 | 4 / 9 | 0 | $0.034 |
| claude-haiku-5-5 (run 2) | 17/18 | 3 / 5 | 3 / 8 | 0 | $0.033 |
| deepseek/deepseek-v4.1-flash | 15/18 | 3 / 6 | 4 / 9 | **3** | $0.003 |
| z-ai/glm-5.3-flash | 15/18 | 3 / 4 | 4 / 5 | **2** | $0.017 |

- **The eval now reproduces the live failure.** DeepSeek and GLM both hit the 12-call
  cap on `open-sb-agents-k3s` and `open-mt-agent-auth` (13-14 calls): when the answer
  isn't in the docs they keep searching, where Haiku gives up within budget. DeepSeek
  also blew the 100k token cap on `chezmoi-source-path` (230k tokens), a narrow case it
  had passed before.
- p50 is the same for all three; the difference is the tail, which a pass rate alone
  hid. That is why p95 and cap hits are now in the record.
- Haiku's one miss in run 2 was the judge on `open-mt-deploy-healthy`, not budget.
- **Default stays Haiku.** Neither cheaper model passes on both quality and budget.
  Raising the caps to fit them would hide exactly this.

## Recommendation per role (2026-11-04)

- **Specialist (docs-agent): keep Haiku.** DeepSeek-v4.1-flash and GLM-5.3-flash both
  overrun the tool-call cap on open questions the docs don't cover (SB-1530 eval, above).
  A prompt rule like "after N searches with no hit, say the docs don't cover it" is the
  thing to try before switching. Either is
  one env var away: `DOCS_AGENT_MODEL=openrouter:<id>`; the k3s Secret already has the key.
- **Router (sb-support):** keep Haiku. It drives the Slack conversation and escalation,
  and has no eval of its own yet; switch only after one exists.
- **Router (ADK / mt-agent):** keep Haiku; the ADK side can't switch (LiteLLM conflict).
- **Judge:** keep Sonnet 5.5 so scores stay comparable.
