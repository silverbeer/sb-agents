#!/usr/bin/env bash
# Create or update the sb-agents Secrets from .env.op / .env.mt.op (1Password refs).
#
#   bash k3s/provision-secret.sh
#
# One Secret per Deployment, holding only what it uses (mt-agent's MT token never
# reaches sb-support; Slack tokens never reach the specialists). `op run` resolves the
# references with the agent service account; values go from the environment to
# `kubectl apply` over stdin only: never argv, a file, or this script's output.
# Values are base64 in `data` (not `stringData`) so op's output masking can't mangle them.
set -euo pipefail
cd "$(dirname "$0")/.."
CONTEXT="${KUBE_CONTEXT:-rancher-desktop}"
op run --env-file .env.op --env-file .env.mt.op -- python3 - <<'PY' | kubectl --context "$CONTEXT" apply -f -
import base64, json, os, sys

SECRETS = {
    "docs-agent": ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"),  # OpenRouter: SB-1490
    "mt-agent": ("ANTHROPIC_API_KEY", "MT_AGENT_MCP_TOKEN"),
    "sb-support": ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
}
keys = {k for ks in SECRETS.values() for k in ks}
missing = sorted(k for k in keys if not os.environ.get(k, "").strip() or os.environ[k].startswith("op://"))
if missing:
    sys.exit(f"unresolved or empty: {', '.join(missing)}")
items = [
    {
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        "metadata": {"name": name, "namespace": "sb-agents"},
        "data": {k: base64.b64encode(os.environ[k].strip().encode()).decode() for k in ks},
    }
    for name, ks in SECRETS.items()
]
json.dump({"apiVersion": "v1", "kind": "List", "items": items}, sys.stdout)
PY
