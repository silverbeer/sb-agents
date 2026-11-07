#!/usr/bin/env bash
# Build the sb-agents image into k3s and roll out docs-agent, mt-agent, sb-support and
# sb-support-api; apply the motw-agent CronJobs.
#
#   bash k3s/deploy.sh               # build, secrets, apply, wait, prune old images
#   bash k3s/deploy.sh --skip-build  # re-apply manifests/secrets on the current image
#
# The image is built by Rancher Desktop's nerdctl straight into containerd's k8s.io
# namespace: no registry, and no second copy in Docker Desktop. Tag = git short SHA
# (+ -dirty). The two newest sb-agents images are kept for rollback; older ones are
# removed after a successful rollout (disk on the mini is the constraint).
set -euo pipefail
cd "$(dirname "$0")/.."
CONTEXT="${KUBE_CONTEXT:-rancher-desktop}"
KEEP=2
k() { kubectl --context "$CONTEXT" "$@"; }

SKIP_BUILD=false
[[ "${1:-}" == "--skip-build" ]] && SKIP_BUILD=true

if $SKIP_BUILD; then
    TAG=$(k -n sb-agents get deploy mt-agent -o jsonpath='{.spec.template.spec.containers[0].image}' | cut -d: -f2)
    echo "reusing sb-agents:$TAG"
else
    TAG=$(git rev-parse --short HEAD)
    git diff --quiet HEAD || TAG="$TAG-dirty"
    echo "building sb-agents:$TAG"
    nerdctl --namespace k8s.io build -t "sb-agents:$TAG" .
fi

k apply -f k3s/namespace.yaml
bash k3s/provision-secret.sh
bash k3s/api-token.sh
# Specialists first: sb-support discovers them once, at startup.
for app in docs-agent mt-agent sb-support sb-support-api; do
    sed "s/IMAGE_TAG/$TAG/g" "k3s/$app.yaml" | k apply -f -
    # Secrets and env are read at start; a re-apply with the same image must still restart.
    k -n sb-agents rollout restart "deploy/$app" >/dev/null
    k -n sb-agents rollout status "deploy/$app" --timeout=300s
done

# CronJobs (SB-1545): no rollout, the next scheduled run uses the new image. Only once
# their ConfigMap (picks channel + approvers) exists; see docs/notes/motw-agent.md.
if k -n sb-agents get configmap motw-agent >/dev/null 2>&1; then
    sed "s/IMAGE_TAG/$TAG/g" k3s/motw-agent.yaml | k apply -f -
else
    echo "skipping motw-agent CronJobs: no ConfigMap motw-agent (docs/notes/motw-agent.md)"
fi

# Keep the newest $KEEP sb-agents images; nerdctl refuses any a container still uses.
nerdctl --namespace k8s.io images --filter reference=sb-agents \
    --format '{{.CreatedAt}}|{{.Repository}}:{{.Tag}}' | grep -v '<none>' | sort -r | tail -n +$((KEEP + 1)) | cut -d'|' -f2 \
    | while read -r old; do nerdctl --namespace k8s.io rmi "$old" >/dev/null && echo "removed $old"; done
echo "deployed sb-agents:$TAG"
