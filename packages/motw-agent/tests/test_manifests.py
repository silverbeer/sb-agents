from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def cronjobs() -> dict[str, dict]:
    docs = yaml.safe_load_all((ROOT / "k3s/motw-agent.yaml").read_text())
    return {d["metadata"]["name"]: d for d in docs}


def test_schedules_are_eastern_and_survive_a_sleeping_mini() -> None:
    jobs = cronjobs()
    assert jobs["motw-agent-suggest"]["spec"]["schedule"] == "0 10 * * 2"
    assert jobs["motw-agent-remind"]["spec"]["schedule"] == "0 18 * * 4"
    for job in jobs.values():
        spec = job["spec"]
        assert spec["timeZone"] == "America/New_York"
        assert spec["startingDeadlineSeconds"] == 21600
        assert spec["concurrencyPolicy"] == "Forbid"
        assert spec["jobTemplate"]["spec"]["backoffLimit"] == 0  # never post twice


def test_commands_are_real_entry_points_and_secrets_are_by_reference() -> None:
    jobs = cronjobs()
    for name, sub in (("motw-agent-suggest", "suggest"), ("motw-agent-remind", "remind")):
        [c] = jobs[name]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
        assert c["command"] == ["motw-agent", sub]
        assert {"secretRef": {"name": "motw-agent"}} in c["envFrom"]
        assert all("TOKEN" not in e["name"] and "KEY" not in e["name"] for e in c["env"])
    provision = (ROOT / "k3s/provision-secret.sh").read_text()
    assert (
        '"motw-agent": ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "MT_AGENT_MCP_TOKEN")' in provision
    )
