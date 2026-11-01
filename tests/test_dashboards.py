"""Dashboards are generated; the committed JSON must match the generator (SB-1486)."""

import importlib.util
import json
from pathlib import Path

GRAFANA = Path(__file__).parents[1] / "observability" / "grafana"


def load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_dashboards", GRAFANA / "build_dashboards.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_dashboards_match_generator() -> None:
    for name, text in load_builder().render().items():
        committed = (GRAFANA / "dashboards" / name).read_text()
        assert committed == text, f"{name} is stale: run `make dashboards`"


def test_panels_have_unique_ids_and_known_datasources() -> None:
    for name in load_builder().DASHBOARDS:
        dash = json.loads((GRAFANA / "dashboards" / name).read_text())
        ids = [p["id"] for p in dash["panels"]]
        assert len(ids) == len(set(ids))
        for panel in dash["panels"]:
            for target in panel["targets"]:
                assert target["datasource"]["uid"] in {"loki", "prometheus", "tempo"}
                assert target["expr"].strip()
