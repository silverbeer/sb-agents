#!/usr/bin/env bash
# Merge sb-agents' prod secrets into AWS Secrets Manager `missing-table-app-secrets`
# (SB-1570), where ESO reads them for both sb-agents and MT's backend.
#
#   bash helm/sb-agents/set-aws-secret.sh
#
# Properties written:
#   sb_agents_anthropic_api_key  ← ANTHROPIC_API_KEY  (.env.op, 1Password)
#   sb_agents_mt_mcp_token       ← MT_AGENT_MCP_TOKEN (.env.mt.op, 1Password)
#   sb_agents_api_token          bearer MT's backend sends sb-support-api; kept if
#                                already set, otherwise generated once
#
# Every other property is left as it is. Values travel env → python → a 0600 temp
# file read by `aws`; never argv, never this script's output (it prints key names).
# ESO refreshes hourly; the last lines print how to force a sync.
set -euo pipefail
cd "$(dirname "$0")/../.."
export AWS_SECRET_ID="${AWS_SECRET_ID:-missing-table-app-secrets}"
export AWS_REGION="${AWS_REGION:-us-east-2}"
aws sts get-caller-identity >/dev/null 2>&1 || { echo "✗ aws not authenticated" >&2; exit 1; }

op run --env-file .env.op --env-file .env.mt.op -- python3 - <<'PY'
import json, os, secrets, subprocess, sys, tempfile

sid, region = os.environ["AWS_SECRET_ID"], os.environ["AWS_REGION"]
src = {"sb_agents_anthropic_api_key": "ANTHROPIC_API_KEY",
       "sb_agents_mt_mcp_token": "MT_AGENT_MCP_TOKEN"}
missing = [v for v in src.values() if not os.environ.get(v, "").strip() or os.environ[v].startswith("op://")]
if missing:
    sys.exit(f"✗ unresolved or empty: {', '.join(missing)}")

current = json.loads(subprocess.run(
    ["aws", "secretsmanager", "get-secret-value", "--secret-id", sid, "--region", region,
     "--query", "SecretString", "--output", "text"],
    capture_output=True, text=True, check=True).stdout)
before = set(current)
for prop, env in src.items():
    current[prop] = os.environ[env].strip()
if not current.get("sb_agents_api_token"):
    current["sb_agents_api_token"] = secrets.token_urlsafe(32)

with tempfile.NamedTemporaryFile("w", suffix=".json", delete=True) as f:
    os.chmod(f.name, 0o600)
    json.dump(current, f)
    f.flush()
    subprocess.run(
        ["aws", "secretsmanager", "put-secret-value", "--secret-id", sid, "--region", region,
         "--secret-string", f"file://{f.name}"],
        capture_output=True, check=True)

lost = before - set(current)
if lost:
    sys.exit(f"✗ properties lost: {sorted(lost)}")
print(f"✓ {sid}: {len(current)} properties; sb_agents_* = "
      f"{sorted(k for k in current if k.startswith('sb_agents_'))}")
PY

echo "  ESO refreshes hourly; to sync now:"
echo "  kubectl annotate externalsecret sb-agents -n sb-agents force-sync=\$(date +%s) --overwrite"
echo "  kubectl annotate externalsecret missing-table-app-secrets -n missing-table force-sync=\$(date +%s) --overwrite"
