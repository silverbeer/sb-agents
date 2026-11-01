"""Generate Grafana dashboard JSON for sb-agents.

    uv run python observability/grafana/build_dashboards.py   # writes dashboards/*.json

Edit this file, not the JSON; tests fail if the committed JSON is stale.

Where each number comes from:
- Run counts, spend, tokens, run latency: the run records in Loki (`event="agent_run"`).
  Exact per run, the same rows work would query in BigQuery. Prometheus counters from
  short-lived CLI processes appear once at their final value, so `increase()` misses them.
- LLM call latency: Tempo span metrics (`traces_spanmetrics_latency`), the same for
  every framework. PydanticAI emits no duration histogram, only token counts.
- A2A edges: Tempo's service graph (`traces_service_graph_request_total`), built from
  client/server span pairs, so it shows which agent calls which with no extra code.
- A2A server latency: the ASGI middleware's histogram (long-running server).
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent / "dashboards"
LOKI = {"type": "loki", "uid": "loki"}
PROM = {"type": "prometheus", "uid": "prometheus"}
RUNS = '{service_namespace="sb-agents"} | event="agent_run"'


def _target(ds: dict, expr: str, legend: str = "", instant: bool = False) -> dict:
    t = {"datasource": ds, "expr": expr, "refId": "A", "legendFormat": legend}
    if instant:  # stat panels: one number for the whole time range
        t["queryType"] = "instant"
    return t


def stat(title: str, expr: str, x: int, unit: str = "short", decimals: int | None = None) -> dict:
    p = {
        "type": "stat",
        "title": title,
        "datasource": LOKI,
        "gridPos": {"x": x, "y": 0, "w": 6, "h": 4},
        "targets": [_target(LOKI, expr, instant=True)],
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "none"},
    }
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    return p


def series(
    title: str,
    ds: dict,
    exprs: list[tuple[str, str]],
    pos: tuple[int, int],
    unit: str = "short",
    stack: bool = False,
) -> dict:
    x, y = pos
    targets = [
        dict(_target(ds, e, legend), refId=chr(65 + i)) for i, (e, legend) in enumerate(exprs)
    ]
    custom = {"drawStyle": "bars" if stack else "line", "fillOpacity": 60 if stack else 10}
    if stack:
        custom["stacking"] = {"mode": "normal"}
    return {
        "type": "timeseries",
        "title": title,
        "datasource": ds,
        "gridPos": {"x": x, "y": y, "w": 12, "h": 8},
        "targets": targets,
        "fieldConfig": {"defaults": {"unit": unit, "custom": custom}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
    }


def agent_overview() -> dict:
    panels = [
        stat("Runs", f"sum(count_over_time({RUNS} [$__range]))", 0),
        stat(
            "Spend",
            f"sum(sum_over_time({RUNS} | unwrap cost_usd [$__range]))",
            6,
            unit="currencyUSD",
            decimals=4,
        ),
        stat(
            "Avg $ / run",
            f"sum(sum_over_time({RUNS} | unwrap cost_usd [$__range])) / "
            f'sum(count_over_time({RUNS} | cost_usd != "" [$__range]))',
            12,
            unit="currencyUSD",
            decimals=5,
        ),
        stat(
            "Not ok (error + limit)",
            f'(sum(count_over_time({RUNS} | outcome != "ok" [$__range])) or vector(0)) / '
            f"sum(count_over_time({RUNS} [$__range]))",
            18,
            unit="percentunit",
            decimals=1,
        ),
        series(
            "Runs by agent",
            LOKI,
            [(f"sum by (agent) (count_over_time({RUNS} [$__interval]))", "{{agent}}")],
            (0, 4),
            stack=True,
        ),
        series(
            "Spend by agent",
            LOKI,
            [
                (
                    f"sum by (agent) (sum_over_time({RUNS} | unwrap cost_usd [$__interval]))",
                    "{{agent}}",
                )
            ],
            (12, 4),
            unit="currencyUSD",
            stack=True,
        ),
        series(
            "Run latency p95 by agent",
            LOKI,
            [
                (
                    f"quantile_over_time(0.95, {RUNS} | unwrap latency_ms [$__interval])"
                    " by (agent)",
                    "{{agent}}",
                )
            ],
            (0, 12),
            unit="ms",
        ),
        series(
            "Tokens by agent",
            LOKI,
            [
                (
                    f"sum by (agent) (sum_over_time({RUNS} | unwrap input_tokens [$__interval]))",
                    "{{agent}} in",
                ),
                (
                    f"sum by (agent) (sum_over_time({RUNS} | unwrap output_tokens [$__interval]))",
                    "{{agent}} out",
                ),
            ],
            (12, 12),
        ),
        series(
            "LLM call latency p95 (per request, from spans)",
            PROM,
            [
                (
                    "histogram_quantile(0.95, sum by (le, service, span_name) "
                    "(rate(traces_spanmetrics_latency_bucket"
                    '{span_name=~"chat .*|generate_content .*"}[$__rate_interval])))',
                    "{{service}} {{span_name}}",
                )
            ],
            (0, 20),
            unit="s",
        ),
        series(
            "A2A server latency p95 (docs-agent)",
            PROM,
            [
                (
                    "histogram_quantile(0.95, sum by (le, service_name) "
                    "(rate(http_server_duration_milliseconds_bucket"
                    '{service_namespace="sb-agents"}[$__rate_interval])))',
                    "{{service_name}}",
                )
            ],
            (12, 20),
            unit="ms",
        ),
        {
            "type": "bargauge",
            "title": "A2A calls in range (client -> server, from the trace service graph)",
            "datasource": PROM,
            "gridPos": {"x": 0, "y": 28, "w": 24, "h": 5},
            "targets": [
                dict(
                    _target(
                        PROM,
                        "sum by (client, server) "
                        "(increase(traces_service_graph_request_total[$__range]))",
                        "{{client}} -> {{server}}",
                    ),
                    instant=True,
                )
            ],
            "fieldConfig": {"defaults": {"unit": "short", "decimals": 0}, "overrides": []},
            "options": {
                "orientation": "horizontal",
                "displayMode": "basic",
                "reduceOptions": {"calcs": ["lastNotNull"]},
            },
        },
        {
            "type": "logs",
            "title": "Expensive (> $0.002) or failed runs: expand a line, click trace_id",
            "datasource": LOKI,
            "gridPos": {"x": 0, "y": 33, "w": 24, "h": 10},
            "targets": [_target(LOKI, f'{RUNS} | cost_usd > 0.002 or outcome != "ok"')],
            "options": {
                "showTime": True,
                "wrapLogMessage": False,
                "enableLogDetails": True,
                "sortOrder": "Descending",
            },
        },
    ]
    for i, panel in enumerate(panels, start=1):
        panel["id"] = i
    return {
        "uid": "sb-agents-overview",
        "title": "sb-agents: overview & cost",
        "tags": ["sb-agents"],
        "schemaVersion": 39,
        "time": {"from": "now-24h", "to": "now"},
        "refresh": "30s",
        "panels": panels,
    }


DASHBOARDS = {"agent-overview.json": agent_overview}


def render() -> dict[str, str]:
    return {name: json.dumps(build(), indent=2) + "\n" for name, build in DASHBOARDS.items()}


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, text in render().items():
        (OUT / name).write_text(text)
        print(f"wrote {OUT / name}")
