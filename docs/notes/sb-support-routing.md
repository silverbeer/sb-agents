# sb-support: specialists, escalation, thread follow-ups (SB-1476)

```
Slack mention / thread reply
  └─ sb-support (PydanticAI, Haiku)
       ├─ ask_docs_agent ──A2A──► docs-agent      (tool built from docs-agent's agent card)
       ├─ ask_<next>     ──A2A──► infra/change/…  (just add a URL)
       └─ deterministic escalation ──► "<@human> can you take this?"
```

## Specialists come from agent cards

`SB_SUPPORT_SPECIALISTS` (comma-separated A2A base URLs, default docs-agent) is read at
startup. Each `/.well-known/agent-card.json` becomes one tool: `ask_<card name>`, with
the card's description and skill examples as the tool description. Adding infra-agent
(SB-1473) will be a URL, not code. An unreachable specialist is logged and skipped;
sb-support still starts. Discovery is a separate trace, since it runs before any question.

## Routing is logged three ways

- a `routed` log line per specialist call (specialist, question);
- `sb.routed_to` on the tool span;
- `routed_to` on the RunRecord (comma-separated, in order), so "which specialist
  answers what" is a Loki or BigQuery query.

## Escalation is deterministic, not the model's call

If any specialist **failed** or answered with **`low` confidence** (read from the A2A
data part), the reply gets `:raising_hand: Escalating to <@SB_SUPPORT_ESCALATE_TO>:
<reason>`, the message gets 🙋 instead of ✅, and the RunRecord has `escalated=true`.
A model deciding to escalate would be one more thing to get wrong; a confidence field
plus a rule is testable.

A failed specialist call returns an "unavailable" string to the model instead of
raising, so the user still gets a reply. Retrying would only fail again.

**Calibration matters more than the rule.** Live, docs-agent answered "who is on call
for missing-table this week?" with *"the docs don't name anyone"* at **high** confidence:
it was sure about the absence. The rule never fired. Fix: docs-agent's instructions and
field description now say confidence is about the specific fact asked, so "topic covered,
fact missing" is `low`. New eval case `partial-oncall-name`. After the fix: low 3/3 on
on-call phrasings, high on an answerable question, 14/14 evals ($0.069).

## Thread follow-ups

- **Memory:** per-thread history in process (`ThreadMemory`), trimmed by whole runs
  (never between a tool call and its result) and capped at 200 threads x 6 runs. It's
  lost on restart; a persistent store can come later if it's ever needed.
- **No re-mention needed:** the manifest already subscribed to channel `message` events.
  A human reply in a thread sb-support has answered is handled as a follow-up. Messages
  that mention the bot are skipped there, because they also arrive as `app_mention`.
- Live: "how do I install janitor?" then "and how do I verify that worked?" got `jt --help`
  / `jt doctor` from the thread's earlier docs-agent answer (both in the README) without
  a second A2A call.
