"""Alerting provisioning parses after `make obs-up`'s substitution (SB-1487).

Grafana parses YAML before expanding ${VARS}, so a placeholder inside a flow list
breaks startup ("Grafana exited before becoming ready"). This catches that in CI.
"""

import re
from pathlib import Path

import yaml

FILE = Path(__file__).parents[1] / "observability/grafana/provisioning/sb-agents-alerting.yaml"
DEFAULTS = {"ALERT_FAILED_RUNS": "1", "ALERT_P95_MS": "20000", "ALERT_DAILY_USD": "1"}


def rendered() -> dict:
    text = FILE.read_text()
    for name, value in DEFAULTS.items():
        text = text.replace("${" + name + "}", value)
    return yaml.safe_load(text)


def test_only_the_slack_token_is_left_for_grafana() -> None:
    text = FILE.read_text()
    for name in DEFAULTS:
        text = text.replace("${" + name + "}", "")
    assert set(re.findall(r"\$\{(\w+)\}", text)) == {"SLACK_BOT_TOKEN"}


def test_rules_parse_with_numeric_thresholds() -> None:
    doc = rendered()
    rules = {r["uid"]: r for g in doc["groups"] for r in g["rules"]}
    assert set(rules) == {
        "sb-agent-failures",
        "sb-agent-latency",
        "sb-agent-daily-spend",
        "sb-docs-agent-down",
    }
    for rule in rules.values():
        [threshold] = [d for d in rule["data"] if d["refId"] == rule["condition"]]
        [param] = threshold["model"]["conditions"][0]["evaluator"]["params"]
        assert isinstance(param, int | float), rule["uid"]
        sources = {d["datasourceUid"] for d in rule["data"]}
        assert sources <= {"loki", "prometheus", "__expr__"}


def test_everything_routes_to_sb_alerts() -> None:
    doc = rendered()
    [point] = doc["contactPoints"]
    assert point["receivers"][0]["settings"]["recipient"] == "#sb-alerts"
    assert doc["policies"][0]["receiver"] == point["name"]
