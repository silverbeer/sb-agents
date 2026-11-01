"""One Slack question is one trace across sb-support and docs-agent (SB-1485)."""

import json
import subprocess
import sys
from pathlib import Path

SCENARIO = Path(__file__).with_name("hop_scenario.py")


def test_slack_question_is_one_trace_across_the_a2a_hop() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCENARIO)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYDANTIC_AI_NO_BANNER": "1"},
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    logs = [json.loads(line) for line in proc.stderr.splitlines() if line.startswith("{")]

    by_name = {s["name"]: s["trace"] for s in out["spans"]}
    root = by_name["slack app_mention"]
    # Both agents' spans, the A2A server span, and docs-agent's tool calls: one trace.
    for name in (
        "agent_run sb-support",
        "invoke_agent sb-support",
        "execute_tool ask_docs",
        "POST /",
        "agent_run docs-agent",
        "invoke_agent docs-agent",
        "execute_tool search_docs",
    ):
        assert by_name.get(name) == root, (name, sorted(by_name))
    assert len({s["trace"] for s in out["spans"]}) == 1

    # Run records of both agents carry the same trace_id: joinable in SQL without traces.
    records = {r["agent"]: r for r in logs if r.get("event") == "agent_run"}
    assert records["sb-support"]["trace_id"] == records["docs-agent"]["trace_id"] == root
    assert records["docs-agent"]["caller"] == "a2a"
