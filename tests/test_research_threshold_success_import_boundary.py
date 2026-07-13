from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.research_threshold_success_fixture import create_success_fixture


FORBIDDEN = (
    "market_research." + "config",
    "market_research.research_profile",
    "market_research." + "broker",
    "market_research.runtime_strategy_decision",
    "market_research.runtime_strategy_set",
    "market_research.runtime_adapter_bootstrap",
    "market_research.runtime_adapters",
    "market_research.strategy_authoring",
    "market_research.research.strategies.legacy_compat",
    "market_research.strategy_plugins",
)


def test_successful_threshold_backtest_stays_within_research_import_boundary(tmp_path: Path) -> None:
    db_path, manifest_path = create_success_fixture(tmp_path)
    root = tmp_path / "research-runtime"
    script = """
import json, sys
from market_research.research_cli.main import main
rc = main(['research-backtest', '--manifest', sys.argv[1]])
print(json.dumps({'rc': rc, 'forbidden': [name for name in sys.argv[2:] if name in sys.modules]}))
raise SystemExit(rc)
"""
    env = os.environ | {
        "RESEARCH_DATA_ROOT": str(root / "data"),
        "RESEARCH_ARTIFACT_ROOT": str(root / "artifacts"),
        "RESEARCH_REPORT_ROOT": str(root / "reports"),
        "RESEARCH_CACHE_ROOT": str(root / "cache"),
        "RESEARCH_DB_PATH": str(db_path),
    }
    result = subprocess.run(
        [sys.executable, "-c", script, str(manifest_path), *FORBIDDEN],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout.splitlines()[-1]) == {"rc": 0, "forbidden": []}
    report = root / "artifacts" / "reports" / "research" / "threshold_success_import_boundary" / "backtest_report.json"
    assert report.is_file()
    assert json.loads(report.read_text(encoding="utf-8"))["candidate_count"] == 1
    candidate = json.loads(
        (
            root
            / "artifacts"
            / "derived"
            / "research"
            / "threshold_success_import_boundary"
            / "backtest_candidates.json"
        ).read_text(encoding="utf-8")
    )["candidates"][0]
    assert candidate["strategy_name"] == "threshold_research_only"
    validation = candidate["scenario_results"][0]
    assert validation["validation_resource_usage"]["common_execution_authority"] == "common_simulation_engine"
    assert validation["validation_resource_usage"]["open_position_at_end"] is True
    assert validation["validation_resource_usage"]["final_position_marked_to_market"] is True
    assert validation["validation_metrics"]["trade_count"] == 0


def test_catalog_resolves_threshold_without_loading_legacy_bridge() -> None:
    script = """
import json, sys
from market_research.research.strategy_catalog import resolve_research_strategy
plugin = resolve_research_strategy('threshold_research_only')
print(json.dumps({
    'execution_authority': plugin.execution_authority,
    'event_builder': plugin.event_builder.__module__,
    'forbidden': [name for name in sys.argv[1:] if name in sys.modules],
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, *FORBIDDEN],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["execution_authority"] == "common_simulation_engine"
    assert payload["event_builder"].startswith("market_research.builtin_strategies.")
    assert payload["forbidden"] == []
