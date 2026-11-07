# sb-agents on prod LKE (SB-1570)

MT's app chat (SB-1500) calls sb-support-api from MT's backend on LKE. The homelab k3s
is not reachable from LKE, so the chat path runs in the prod cluster, next to MT:

| | Deployment | Service | Notes |
|---|---|---|---|
| sb-support-api | `sb-support-api` | `sb-support-api.sb-agents.svc:8103` | bearer `SB_SUPPORT_API_TOKEN`; ClusterIP only, no Ingress |
| mt-agent | `mt-agent-a2a` | `mt-agent.sb-agents.svc:8102` | prod mt-mcp at its public URL |
| docs-agent | `docs-agent-a2a` | `docs-agent.sb-agents.svc:8101` | corpus = shallow clones of **public** repos only (`docsAgent.repos`) |

**Stays on the homelab** ([deploy-k3s.md](deploy-k3s.md)): Slack sb-support with its
saved "my team" preferences, docs-agent over every repo (private ones included), and
scheduled agents. Nothing is removed there.

## Why it looks like this

- **One node, shared with MT prod.** The LKE cluster is one `g6-standard-2`. Before
  SB-1570 it had 1411Mi free (kubelet stats, 2026-11-06), after SB-1568/SB-1569 dropped
  idle replicas. sb-agents pods idle at 140–230Mi, so limits are 384Mi each (3 × 384Mi
  fits) instead of the homelab's 768Mi.
- **Evicted before MT.** PriorityClass `sb-agents-low` (value -100, never preempts):
  under memory pressure the kubelet evicts sb-agents ahead of MT's priority-0 pods.
  A second node (~$24/mo) is the fallback if evictions show up.
- **Public docs only.** Prod answers MT app users, so private repos' markdown never
  enters this corpus. Doc changes need a pod restart (the clone is made at start).
  Raw research notes (`*/docs/research/*`) are indexed out by default (SB-1572).
- **No traces.** No OTLP exporter: the homelab collector isn't reachable, and chat
  content (minors' data) must not leave the cluster. Logs only.
- **No preferences store.** MT app users are signed in; "my team" comes from their MT
  account via the delegated token (SB-1529), so `SB_SUPPORT_DB` is unset.

## Pieces

- **Chart:** `helm/sb-agents/` (this repo). ArgoCD app `sb-agents` syncs it; the
  Application is Terraform in `missingtable-platform-bootstrap`
  (`clouds/linode/environments/dev/main.tf`), like myrunstreak's.
- **Image:** CI pushes `ghcr.io/silverbeer/sb-agents:<sha7>` on every merge to main.
  With a `GH_PAT` repo secret, CI also bumps `image.tag` in `values.yaml`; without
  one, bump it by PR. Pulled with `sb-agents-ghcr` (the shared GHCR PAT, via ESO).
- **Secrets:** ESO Secret `sb-agents`, from properties in AWS Secrets Manager
  `missing-table-app-secrets` (ESO's IAM policy already covers it):

  | Property | Env | Used by |
  |---|---|---|
  | `sb_agents_anthropic_api_key` | `ANTHROPIC_API_KEY` | all three |
  | `sb_agents_mt_mcp_token` | `MT_AGENT_MCP_TOKEN` | mt-agent |
  | `sb_agents_api_token` | `SB_SUPPORT_API_TOKEN` | sb-support-api; MT's backend sends the same value as `MT_AI_SB_AGENTS_TOKEN` |

  Set or refresh them with `bash helm/sb-agents/set-aws-secret.sh` (1Password →
  AWS; generates the API token once, keeps it after). Writes to the secret store
  happen from a human terminal.

## Runbook

```bash
# 1. secrets (human terminal)
bash helm/sb-agents/set-aws-secret.sh

# 2. state
kubectl --context lke560651-ctx -n sb-agents get pods,externalsecret

# 3. health, from MT's backend pod (the path MT uses)
kubectl --context lke560651-ctx -n missing-table exec deploy/missing-table-backend -- \
  python -c "import urllib.request;print(urllib.request.urlopen('http://sb-support-api.sb-agents.svc.cluster.local:8103/healthz').read())"

# 4. real memory on the node (no metrics-server)
kubectl --context lke560651-ctx get --raw \
  "/api/v1/nodes/$(kubectl --context lke560651-ctx get nodes -o name | cut -d/ -f2)/proxy/stats/summary" \
  | jq '.node.memory.availableBytes/1048576 | floor'
```

Rollback: delete the ArgoCD Application (prunes the namespace's resources). MT is
unaffected while `MT_AI_BACKEND` is `in-app`.
