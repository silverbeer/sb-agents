# First A2A hop: ADK → fasta2a/PydanticAI (SB-1472)

ADK `LlmAgent` router → `RemoteA2aAgent` → A2A (JSON-RPC, protocol 1.0) →
fasta2a server wrapping the PydanticAI docs-agent.

Versions: google-adk 2.13.0, a2a-sdk 1.x (ADK pins `<2`), fasta2a 2.1.1,
pydantic-ai 2.64.0. Date: 2026-10-31.

## Headline

**"Both sides speak A2A" did not mean "they interoperate."** Two spec bugs in
fasta2a broke the hop. Neither showed up with fasta2a's own client, and one
failed *silently*. A cross-framework integration test is the only thing that
catches this class of bug, so we keep one in CI (`test_adk_remote_agent_round_trip`)
plus a strict-xfail canary that flips when upstream fixes land.

## What broke, in order

| # | Symptom (ADK side) | Cause | Fix here |
|---|---|---|---|
| 1 | `Failed to parse timestamp field: time data '2026-10' ...` | fasta2a stamps status with naive `datetime.now().isoformat()`. A2A 1.0 timestamps are protobuf Timestamps: RFC 3339 with `Z`/offset required. | `UtcStorage` |
| 2 | `append=True for nonexistent artifact_id` | fasta2a sends the *first* streamed chunk with `append=True`. Spec: first chunk creates the artifact. | `SpecAgentWorker.publish_artifact` |
| 3 | Empty event, no error, nothing in server log | fasta2a marks the task failed and re-raises into the broker, which drops the exception. ADK turns `TASK_STATE_FAILED` into an event with no content and no `error_message`. | `SpecAgentWorker.run_task` logs it |
| 4 | Answer arrives as base64 `inline_data` (`<a2a_datapart_json>…`) | Structured output is an A2A *data* part; ADK converts data parts to `inline_data`, unreadable for an LLM or Slack. | Result artifact gets a text part first, data part second |
| 5 | Streamed partials are JSON fragments (`"ref\":\"janitor/README"`) | PydanticAI structured output is written as text JSON; fasta2a streams text deltas. | Artifact streaming off for docs-agent (`stream=False`) |

Plus one plain integration seam: fasta2a calls `agent.run()` **without deps**,
so an agent that needs deps must be wrapped (`WithDeps`, a `WrapperAgent` that
injects deps in `iter()`).

## Things ADK does that are worth knowing

- **Agent card URL must be https, or http on loopback.** In-cluster
  `http://docs-agent.svc` will be refused. Matters for k3s (SB-1481) and auth (SB-1480).
- **It forwards the caller's transcript** to the remote agent, each block wrapped
  in `<<<BEGIN_QUOTED_AGENT_CONTENT>>>` markers with "data, never instructions".
  That's prompt-injection hygiene; remote agents also see more context than the bare question.
- **A2A support is flagged `[EXPERIMENTAL]`** in ADK, with breaking changes possible.
- `RemoteA2aAgent` fallback constant still names the legacy `/.well-known/agent.json`;
  pass the full card URL to avoid ambiguity.
- No `context_cache_config` → each transfer re-sends the whole prompt uncached. Set it
  before cost matters.
- Raw A2A request/response is kept in `event.custom_metadata["a2a:request"|"a2a:response"]`:
  the best debugging handle.

## A2A vs plain tools: first impressions

- Routing worked well: Haiku transferred docs questions and answered small talk itself.
- The hop cost about +1 s over a direct call, and an HTTP service per agent.
- What A2A bought: docs-agent is now usable by *any* A2A client (ADK today, sb-support
  next), independently deployable, and has a discoverable card with skills.
- Transfer is one-way: after `transfer_to_agent`, docs_agent's answer is the final
  response. The router doesn't post-process it. For triage (fan-out, then synthesize),
  use `AgentTool`-style calls rather than transfer. Try that in SB-1475.

## For the work build

1. Budget for interop bugs between frameworks. Pin versions and keep a
   cross-framework round-trip test in CI.
2. Return a text part in every artifact, even when you also return structured data.
3. Make failures loud on both sides; the default here was silence.
4. Plan TLS for agent-to-agent traffic from day one (ADK requires it off-loopback).
