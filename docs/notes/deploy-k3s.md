# Deploying sb-agents to the mini's k3s (SB-1481)

The always-on home for docs-agent, mt-agent and sb-support: Rancher Desktop's k3s on
the Mac mini (context `rancher-desktop`), namespace `sb-agents`.

```bash
make deploy          # build image, provision secrets, roll out all three, prune old images
make deploy-apply    # same, without the build (manifest or secret change)
```

## Shape

| | Deployment | Service | Notes |
|---|---|---|---|
| docs-agent | `docs-agent-a2a` | `docs-agent.sb-agents.svc:8101` | Mac's `~/gitrepos` mounted read-only at `/gitrepos` |
| mt-agent | `mt-agent-a2a` | `mt-agent.sb-agents.svc:8102` | calls prod mt-mcp |
| sb-support | `sb-support` | none | Slack Socket Mode: outbound only, no ingress |

- **One image**, `sb-agents:<git sha>`, for all three (one uv workspace). ~550 MB
  unpacked, ~140 MB compressed. Built by Rancher's `nerdctl` straight into containerd's
  `k8s.io` namespace: no registry, no second copy in Docker Desktop.
  `imagePullPolicy: Never`.
- **Secrets**: `k3s/provision-secret.sh` resolves `.env.op` + `.env.mt.op` with `op run`
  and pipes Secrets to `kubectl apply` over stdin. One Secret per Deployment with only
  what it uses. No plaintext in any manifest or file.
- **Telemetry**: OTLP to otel-lgtm on the Mac (`http://host.lima.internal:4318`),
  `SB_ENV=homelab` (`deployment.environment.name`).

## Things that bit, and why the manifests look the way they do

- **`enableServiceLinks: false`.** Kubernetes injects `<SERVICE>_PORT=tcp://ip:port`
  for every Service. A Service named `mt-agent` sets `MT_AGENT_PORT`, which is
  mt-agent's own port setting: `int("tcp://…")` crash-looped it.
- **`hostAliases` for `host.lima.internal`.** The VM resolves it (192.168.5.2, the lima
  gateway to the Mac); pods don't, since CoreDNS only knows the node.
- **Discovery is once, at startup.** sb-support's init container waits up to 180s for
  both agent cards, then fails (kubelet retries) rather than starting a bot with no
  specialists. `deploy.sh` rolls the specialists out first.
- **TCP readiness, not an HTTP probe.** Every HTTP request is a trace; a 10s probe on
  the agent card is ~8.6k junk traces a day per agent.
- **`Recreate` for sb-support**: one Socket Mode connection and one in-memory
  conversation store at a time. A restart forgets thread memory.

## Don't run the bot twice

`make support` locally while the pod runs gives Slack two Socket Mode connections;
each event goes to one of them at random, and thread memory splits. For local dev:
`kubectl --context rancher-desktop -n sb-agents scale deploy/sb-support --replicas=0`,
then scale back to 1. `make docs-a2a` / `make mt-a2a` are safe to run alongside.

## Disk and logs

- `deploy.sh` keeps the two newest `sb-agents` images (rollback) and removes older ones.
- Logs go to stdout only (JSON). kubelet rotates container logs at 10 MiB × 5 files.
- Wider image hygiene for the mini lives in janitor: `jt k3s prune` then `jt k3s trim`
  (deleting inside the VM frees nothing on the Mac until trimmed).

## Rotation and changes

- **MT token expires ~2026-12-02.** Mint a new one into `op://agents/mt-agent/token`
  (human terminal), then `make deploy-apply`.
- **Docs changes**: docs-agent indexes at startup:
  `kubectl --context rancher-desktop -n sb-agents rollout restart deploy/docs-agent`.
- **Rollback**: `kubectl … set image deploy/<app> <app>=sb-agents:<previous sha>`.
