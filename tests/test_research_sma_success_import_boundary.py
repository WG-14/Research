from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.research_sma_success_fixture import create_success_fixture


FORBIDDEN = ("bithumb_research.config", "bithumb_research.research_profile", "bithumb_research.broker", "bithumb_research.runtime_strategy_decision", "bithumb_research.runtime_strategy_set", "bithumb_research.runtime_adapter_bootstrap", "bithumb_research.runtime_adapters", "bithumb_research.strategy_authoring", "bithumb_research.research.strategy_registry", "bithumb_research.strategy_plugins")


def test_successful_sma_backtest_does_not_load_operational_modules(tmp_path: Path) -> None:
    db_path, manifest_path = create_success_fixture(tmp_path)
    root = tmp_path / "research-runtime"
    script = """
import json, sys
from bithumb_research.research_cli.main import main
rc = main(['research-backtest', '--manifest', sys.argv[1]])
print(json.dumps({'rc': rc, 'forbidden': [name for name in sys.argv[2:] if name in sys.modules]}))
raise SystemExit(rc)
"""
    env = os.environ | {"RESEARCH_DATA_ROOT": str(root / "data"), "RESEARCH_ARTIFACT_ROOT": str(root / "artifacts"), "RESEARCH_REPORT_ROOT": str(root / "reports"), "RESEARCH_CACHE_ROOT": str(root / "cache"), "RESEARCH_DB_PATH": str(db_path)}
    result = subprocess.run([sys.executable, "-c", script, str(manifest_path), *FORBIDDEN], text=True, capture_output=True, env=env, check=False)
    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(result.stdout.splitlines()[-1])
    assert payload == {"rc": 0, "forbidden": []}
    report = root / "artifacts" / "reports" / "research" / "sma_success_import_boundary" / "backtest_report.json"
    assert report.is_file()
    report_payload = json.loads(report.read_text(encoding="utf-8"))
    assert report_payload["candidate_count"] == 1
    derived = root / "artifacts" / "derived" / "research" / "sma_success_import_boundary" / "backtest_candidates.json"
    candidate = json.loads(derived.read_text(encoding="utf-8"))["candidates"][0]
    assert candidate["strategy_name"] == "sma_with_filter"
    assert candidate["scenario_results"][0]["validation_metrics"]["trade_count"] > 0
