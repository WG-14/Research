from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.research_noop_success_fixture import create_success_fixture


FORBIDDEN = (
    "market_research." + "config",
    "market_research.research_profile",
    "market_research." + "broker",
    "market_research.runtime_strategy_decision",
    "market_research.runtime_strategy_set",
    "market_research.runtime_adapter_bootstrap",
    "market_research.runtime_adapters",
    "market_research.strategy_authoring",
    "market_research.research.strategy_registry",
    "market_research.strategy_plugins",
)


def test_successful_noop_backtest_stays_within_research_import_boundary(
    tmp_path: Path,
) -> None:
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
    report_path = (
        root
        / "artifacts"
        / "reports"
        / "research"
        / "noop_success_import_boundary"
        / "backtest_report.json"
    )
    assert report_path.is_file()
    assert json.loads(report_path.read_text(encoding="utf-8"))["candidate_count"] == 1
    candidate = json.loads(
        (
            root
            / "artifacts"
            / "derived"
            / "research"
            / "noop_success_import_boundary"
            / "backtest_candidates.json"
        ).read_text(encoding="utf-8")
    )["candidates"][0]
    assert candidate["strategy_name"] == "noop_baseline"
    validation = candidate["scenario_results"][0]
    assert validation["validation_metrics"]["trade_count"] == 0
    summary = validation["validation_execution_event_summary"]
    assert summary["execution_attempt_count"] == 0
    assert summary["filled_execution_count"] == 0
    assert summary["portfolio_applied_trade_count"] == 0
    assert validation["validation_resource_usage"]["noop_baseline_research_kernel"] == "research_only_v1"
    diagnostics = validation["validation_strategy_diagnostics"]["strategy_specific_diagnostics"]["noop_baseline"]
    assert diagnostics["hold_decision_count"] == 3
    assert all(diagnostics[f"{signal}_signal_count"] == 0 for signal in ("raw", "entry", "exit", "final"))


def test_catalog_resolves_noop_without_loading_legacy_bridge() -> None:
    script = """
import json, sys
from market_research.research.strategy_catalog import resolve_research_strategy
plugin = resolve_research_strategy('noop_baseline')
print(json.dumps({
    'runner': plugin.runner.__module__,
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
    assert payload["runner"].startswith("market_research.research.strategies.")
    assert payload["event_builder"].startswith("market_research.research.strategies.")
    assert payload["forbidden"] == []
