#!/usr/bin/env bash
# Ensure the Secret sb-support-api-token exists (SB-1500): the bearer token MT's
# backend sends to sb-support-api.
#
#   bash k3s/api-token.sh
#
# Source, in order: 1Password `op://agents/sb-support-api/token` if that item exists;
# otherwise the Secret already in the cluster is kept; otherwise a random token is
# generated once. The value never reaches argv, a file, or this script's output.
# To give MT the same token, read it from 1Password (create the item first, from a
# human terminal: agent access to 1Password is read-only).
set -euo pipefail
cd "$(dirname "$0")/.."
CONTEXT="${KUBE_CONTEXT:-rancher-desktop}"
k() { kubectl --context "$CONTEXT" "$@"; }
REF="op://agents/sb-support-api/token"

apply() {  # token on stdin → Secret
    python3 -c '
import base64, json, sys
token = sys.stdin.read().strip()
if len(token) < 32:
    sys.exit("token too short")
json.dump({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
           "metadata": {"name": "sb-support-api-token", "namespace": "sb-agents"},
           "data": {"SB_SUPPORT_API_TOKEN": base64.b64encode(token.encode()).decode()}}, sys.stdout)
' | k apply -f - >/dev/null
}

if op read "$REF" >/dev/null 2>&1; then
    op read "$REF" | apply
    echo "sb-support-api-token: from 1Password"
elif k -n sb-agents get secret sb-support-api-token >/dev/null 2>&1; then
    echo "sb-support-api-token: kept (no 1Password item $REF)"
else
    python3 -c 'import secrets; print(secrets.token_urlsafe(48))' | apply
    echo "sb-support-api-token: generated (create $REF to share it with MT)"
fi
