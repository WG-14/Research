from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from market_research.paths import ResearchPathManager
from market_research.settings import ResearchSettings
from market_research.research.dataset_freeze import freeze_sqlite_candles_dataset
from market_research.research.experiment_manifest import parse_manifest
from market_research.research.validation_protocol import run_research_backtest
from market_research.research.reproduction import load_reproduction_receipt


def _ts(day: str, minute: int = 0) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000) + minute * 60_000


def frozen_manifest_and_manager(tmp_path: Path, *, walk_forward: bool = False, execution_mode: str = "serial"):
    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE candles (pair TEXT, interval TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL)")
        for day_index in range(4):
            day = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day_index)).date().isoformat()
            for minute in range(1440):
                price = 100.0 + day_index + minute / 10_000
                db.execute("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?)", ("KRW-BTC", "1m", _ts(day, minute), price, price, price, price, 1.0))
    frozen = freeze_sqlite_candles_dataset(source_db=source, market="KRW-BTC", interval="1m", start_ts=_ts("2026-01-01"), end_ts=_ts("2026-01-04", 1439), out_dir=tmp_path / "frozen")
    payload = {
        "experiment_id": "frozen_integration", "hypothesis": "frozen artifact integration", "strategy_name": "noop_baseline",
        "research_classification": "research_only", "market": "KRW-BTC", "interval": "1m",
        "dataset": {"source": "frozen_sqlite_candles", "snapshot_id": "frozen-integration",
                    "artifact_manifest_uri": frozen["artifact_manifest_uri"], "artifact_manifest_hash": frozen["artifact_manifest_hash"],
                    "train": {"start": "2026-01-01", "end": "2026-01-01"},
                    "validation": {"start": "2026-01-02", "end": "2026-01-02"},
                    "final_holdout": {"start": "2026-01-04", "end": "2026-01-04"}},
        "parameter_space": {"NOOP_DECISION_START_INDEX": [0]}, "cost_model": {"fee_rate": 0.0, "slippage_bps": [0.0]},
        "acceptance_gate": {"min_trade_count": 1, "max_mdd_pct": 100, "min_profit_factor": 0.1,
                            "oos_return_must_be_positive": False, "parameter_stability_required": False,
                            "final_holdout_required_for_validation": False, "metrics_contract_required": False,
                            "reject_open_position_at_end": False},
        "research_run": {"execution": {"mode": execution_mode, "max_workers": 2 if execution_mode == "parallel" else 1, "process_start_method": "auto_safe", "work_unit": "candidate_scenario"}},
    }
    if walk_forward:
        payload["walk_forward"] = {"train_window_days": 1, "test_window_days": 1, "step_days": 1, "min_windows": 2}
    manifest = parse_manifest(payload)
    settings = ResearchSettings(data_root=tmp_path / "data", artifact_root=tmp_path / "artifacts", report_root=tmp_path / "reports", cache_root=tmp_path / "cache", db_path=None, max_workers=1, random_seed=0)
    return frozen, manifest, ResearchPathManager.from_settings(settings, project_root=Path.cwd())


def test_one_frozen_artifact_runs_backtest_train_validation_holdout(tmp_path) -> None:
    frozen, manifest, manager = frozen_manifest_and_manager(tmp_path)
    report = run_research_backtest(manifest=manifest, db_path=None, manager=manager)
    splits = report["dataset_splits"]
    assert {splits[name]["artifact_manifest_hash"] for name in ("train", "validation", "final_holdout")} == {frozen["artifact_manifest_hash"]}
    assert all(splits[name]["verification_status"] == "VERIFIED" for name in splits)
    assert report["reproduction_receipt_path"]
    receipt = load_reproduction_receipt(report["reproduction_receipt_path"])
    receipt_splits = {item["split_name"]: item for item in receipt["stable_fingerprint"]["dataset_split_hashes"]}
    assert set(receipt_splits) == {"train", "validation", "final_holdout"}
    for split_name, row in report["dataset_splits"].items():
        for field in ("artifact_id", "artifact_manifest_hash", "artifact_content_hash", "artifact_schema_hash", "requested_range", "snapshot_data_hash", "snapshot_query_hash", "snapshot_fingerprint_hash", "quality_hash", "verification_status", "verification"):
            assert receipt_splits[split_name][field] == row[field]


def test_parallel_frozen_backtest_without_db(tmp_path) -> None:
    _, manifest, manager = frozen_manifest_and_manager(tmp_path, execution_mode="parallel")
    assert run_research_backtest(manifest=manifest, db_path=None, manager=manager)["dataset_splits"]["train"]["verification_status"] == "VERIFIED"
