# MT app adapter (SB-1500)

MissingTable's in-app chat (`POST /api/ai/chat` in MT's backend) can answer through the
sb-agents core instead of MT's own in-app ADK assistant. MT stays the owner of the user,
their auth and the conversation record (`ai_messages`); sb-agents is the brain.

```
MT web ──▶ MT backend /api/ai/chat ──▶ sb-support-api POST /v1/chat ──▶ SupportCore
             (auth, ai_messages,          (bearer token, channel          │ A2A
              MT_AI_BACKEND=sb-agents)     "mt-app")                      ▼
                                                                mt-agent / docs-agent ──▶ mt-mcp
```

## Contract

`POST /v1/chat`, `Authorization: Bearer $SB_SUPPORT_API_TOKEN`:

```json
{"conversation_id": "<MT conversation uuid>", "user_id": "<MT user id>", "text": "..."}
```

Reply (`200` even when answering failed: failure is an `outcome`):

```json
{"text": "...", "outcome": "answered|escalated|limit|error|help",
 "escalation": null, "routed_to": ["mt-agent"],
 "tables": [{"title": "...", "columns": [], "rows": [], "notes": [], "highlight": []}]}
```

Optional `"user_token"`: the user's short-lived delegated MT token (SB-1529), forwarded
only to mt-agent; see mt-agent.md "Phase 2". `401` wrong/missing token, `400` bad body. `GET /healthz` has no auth.

- **Channel `mt-app`:** RunRecord `caller = mt-app`; memory keyed `mt-app:<conversation id>`
  (in process, lost on restart; MT keeps the durable record). The model writes plain
  Markdown for MT users instead of Slack mrkdwn (`channel_instructions`).
- **One trace:** the server continues an incoming `traceparent` (`instrument_asgi`).
- **Token:** Secret `sb-support-api-token`, made by `k3s/api-token.sh`: from
  `op://agents/sb-support-api/token` when that item exists, otherwise generated once and
  kept. MT needs the same value, so for anything beyond a local test create the 1Password
  item (human terminal; agent access is read-only) and re-run the script.

## Reaching it

ClusterIP only, in both places it runs:

- **Prod (LKE, SB-1570):** sb-support-api, mt-agent and a public-docs docs-agent run
  next to MT, so MT's backend calls
  `http://sb-support-api.sb-agents.svc.cluster.local:8103` in-cluster. Nothing is
  exposed to the internet. Runbook: [deploy-lke.md](deploy-lke.md).
- **Homelab (k3s):** MT's local backend reaches it with
  `kubectl -n sb-agents port-forward svc/sb-support-api 8103`.

## Not yet

- Token streaming: SB-1565 (the web chat shows a thinking state, then the reply).
- docs-agent's user audience: SB-1494 (until then docs answers are developer-flavoured).
