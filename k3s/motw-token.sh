#!/usr/bin/env bash
# Ensure the Secret motw-writer exists (SB-1546): the MissingTable token sb-support
# uses to write Match of the Week picks (a service account with `manage_motw` only).
#
#   bash k3s/motw-token.sh
#
# Source: 1Password `op://agents/mt-motw-agent/token`. Until that item exists the
# Secret is left as it is (or absent): sb-support still starts, and a pick answers
# "couldn't save" with the reason in its logs. The value never reaches argv, a file,
# or this script's output.
set -euo pipefail
cd "$(dirname "$0")/.."
CONTEXT="${KUBE_CONTEXT:-rancher-desktop}"
REF="op://agents/mt-motw-agent/token"

if op read "$REF" >/dev/null 2>&1; then
    op read "$REF" | python3 -c '
import base64, json, sys
token = sys.stdin.read().strip()
if not token:
    sys.exit("empty token")
json.dump({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
           "metadata": {"name": "motw-writer", "namespace": "sb-agents"},
           "data": {"MT_MOTW_TOKEN": base64.b64encode(token.encode()).decode()}}, sys.stdout)
' | kubectl --context "$CONTEXT" apply -f - >/dev/null
    echo "motw-writer: from 1Password"
else
    echo "motw-writer: skipped (no 1Password item $REF yet; MOTW picks can't be saved)"
fi
