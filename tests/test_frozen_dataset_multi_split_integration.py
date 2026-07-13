from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.settings import ResearchSettings
from market_research.research.dataset_freeze import freeze_sqlite_candles_dataset
from market_research.research.final_selection import apply_final_selection_contract, build_selection_artifact
from market_research.research_composition import parse_builtin_manifest as parse_manifest
from market_research.research.validation_protocol import (
    ResearchValidationError,
    run_final_holdout_confirmation,
    run_research_backtest,
)
from market_research.research.reproduction import load_reproduction_receipt
from market_research.research_composition import builtin_strategy_registry
from market_research.research.validation_pipeline import run_research_validation


def _ts(day: str, minute: int = 0) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000) + minute * 60_000


def frozen_manifest_and_manager(
    tmp_path: Path,
    *,
    walk_forward: bool = False,
    execution_mode: str = "serial",
    final_selection: bool = False,
    strategy_name: str = "noop_baseline",
):
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
        "experiment_id": "frozen_integration", "hypothesis": "frozen artifact integration", "strategy_name": strategy_name,
        "research_classification": "research_only", "market": "KRW-BTC", "interval": "1m",
        "dataset": {"source": "frozen_sqlite_candles", "snapshot_id": "frozen-integration",
                    "artifact_manifest_uri": frozen["artifact_manifest_uri"], "artifact_manifest_hash": frozen["artifact_manifest_hash"],
                    "train": {"start": "2026-01-01", "end": "2026-01-01"},
                    "validation": {"start": "2026-01-02", "end": "2026-01-03"},
                    "final_holdout": {"start": "2026-01-04", "end": "2026-01-04"}},
        "parameter_space": (
            {"BUY_HOLD_BUY_INDEX": [1], "BUY_HOLD_DECISION_REASON": ["frozen_confirmation"]}
            if strategy_name == "buy_and_hold_baseline"
            else {"NOOP_DECISION_START_INDEX": [0]}
        ),
        "cost_model": {"fee_rate": 0.0, "slippage_bps": [0.0]},
        "acceptance_gate": {"min_trade_count": 1, "max_mdd_pct": 100, "min_profit_factor": 0.1,
                            "oos_return_must_be_positive": False, "parameter_stability_required": False,
                            "final_holdout_required_for_validation": False, "metrics_contract_required": False,
                            "reject_open_position_at_end": False},
        "research_run": {"execution": {"mode": execution_mode, "max_workers": 2 if execution_mode == "parallel" else 1, "process_start_method": "auto_safe", "work_unit": "candidate_scenario"}},
    }
    if walk_forward:
        payload["walk_forward"] = {"train_window_days": 1, "test_window_days": 1, "step_days": 1, "min_windows": 2}
    if final_selection:
        payload["final_selection"] = {
            "schema_version": 2,
            "required_for_validation": False,
            "candidate_universe": "acceptance_gate_passed_required_scenarios",
            "must_pass": {"dataset_quality_gate_status": "PASS"},
            "selection_exposure_policy": {
                "final_holdout_usage": "prohibited_during_selection",
                "counts_as_holdout_reuse": False,
            },
            "method": "lexicographic",
            "null_metric_policy": "fail_if_required_else_worst_rank",
            "ranking": [
                {"metric": "validation.metrics_v2.return_risk.total_return_pct", "order": "desc", "required": True},
                {"metric": "parameter_candidate_id", "order": "asc", "required": True},
            ],
            "unsupported_metric_policy": {
                "sharpe_ratio": "fail_if_required",
                "sortino_ratio": "fail_if_required",
            },
        }
    manifest = parse_manifest(payload)
    settings = ResearchSettings(data_root=tmp_path / "data", artifact_root=tmp_path / "artifacts", report_root=tmp_path / "reports", cache_root=tmp_path / "cache", db_path=None, max_workers=1, random_seed=0)
    return frozen, manifest, ResearchPathManager.from_settings(settings, project_root=Path.cwd())


def test_backtest_candidate_search_does_not_materialize_final_holdout(tmp_path) -> None:
    frozen, manifest, manager = frozen_manifest_and_manager(tmp_path)
    report = run_research_backtest(manifest=manifest, db_path=None, manager=manager,
                                   strategy_registry=builtin_strategy_registry())
    splits = report["dataset_splits"]
    assert set(splits) == {"train", "validation"}
    assert {splits[name]["artifact_manifest_hash"] for name in ("train", "validation")} == {frozen["artifact_manifest_hash"]}
    assert all(splits[name]["verification_status"] == "VERIFIED" for name in splits)
    assert report["reproduction_receipt_path"]
    receipt = load_reproduction_receipt(report["reproduction_receipt_path"])
    receipt_splits = {item["split_name"]: item for item in receipt["stable_fingerprint"]["dataset_split_hashes"]}
    assert set(receipt_splits) == {"train", "validation"}
    for split_name, row in report["dataset_splits"].items():
        for field in ("artifact_id", "artifact_manifest_hash", "artifact_content_hash", "artifact_schema_hash", "requested_range", "snapshot_data_hash", "snapshot_query_hash", "snapshot_fingerprint_hash", "quality_hash", "verification_status", "verification"):
            assert receipt_splits[split_name][field] == row[field]


def test_parallel_frozen_backtest_without_db(tmp_path) -> None:
    _, manifest, manager = frozen_manifest_and_manager(tmp_path, execution_mode="parallel")
    assert run_research_backtest(manifest=manifest, db_path=None, manager=manager,
        strategy_registry=builtin_strategy_registry())["dataset_splits"]["train"]["verification_status"] == "VERIFIED"


def test_final_holdout_confirmation_executes_only_receipt_candidate(tmp_path, monkeypatch) -> None:
    _, manifest, manager = frozen_manifest_and_manager(
        tmp_path,
        final_selection=True,
        strategy_name="buy_and_hold_baseline",
    )
    manifest = replace(manifest, raw={**manifest.raw, "objective_metric": "total_return_pct"})
    report = run_research_backtest(
        manifest=manifest,
        db_path=None,
        manager=manager,
        strategy_registry=builtin_strategy_registry(),
    )
    candidate = report["candidates"][0]
    candidate["aggregate_acceptance_gate_result"] = "PASS"
    candidate["acceptance_gate_result"] = "PASS"
    selection = apply_final_selection_contract(
        contract=manifest.final_selection,
        candidates=report["candidates"],
        report_context={"dataset_quality_gate_status": "PASS"},
        validation_required=False,
    )
    report["selection_artifact"] = build_selection_artifact(
        manifest_hash=manifest.manifest_hash(),
        selection_result=selection,
        candidates=report["candidates"],
    )
    report["selection_artifact_hash"] = report["selection_artifact"]["content_hash"]

    confirmation = run_final_holdout_confirmation(
        manifest=manifest,
        selection_report=report,
        db_path=None,
        manager=manager,
        strategy_registry=builtin_strategy_registry(),
    )

    assert len(confirmation["candidate_results"]) == 1
    assert confirmation["candidate_results"][0]["candidate_id"] == report["selection_artifact"]["selected_candidate_id"]
    assert confirmation["selection_artifact_hash"] == report["selection_artifact_hash"]

    monkeypatch.setattr(
        "market_research.research.validation_protocol.load_dataset_split",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("holdout loaded before authorization")),
    )
    with pytest.raises(ResearchValidationError, match="pre_exposure_authorization_failed"):
        run_final_holdout_confirmation(
            manifest=manifest,
            selection_report=report,
            db_path=None,
            manager=manager,
            strategy_registry=builtin_strategy_registry(),
        )


def test_selection_report_contains_no_final_holdout_metrics(tmp_path) -> None:
    _, manifest, manager = frozen_manifest_and_manager(
        tmp_path,
        final_selection=True,
        strategy_name="buy_and_hold_baseline",
    )
    report = run_research_backtest(
        manifest=manifest,
        db_path=None,
        manager=manager,
        strategy_registry=builtin_strategy_registry(),
    )

    def keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield str(key)
                yield from keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from keys(item)

    assert all("final_holdout" not in key for candidate in report["candidates"] for key in keys(candidate))
    assert "best_final_holdout_metrics_v2" not in report


def test_research_validate_executes_final_holdout_exactly_once(tmp_path, monkeypatch) -> None:
    _, manifest, manager = frozen_manifest_and_manager(
        tmp_path,
        final_selection=True,
        strategy_name="buy_and_hold_baseline",
    )
    manifest = replace(manifest, raw={**manifest.raw, "objective_metric": "total_return_pct"})
    selection_report = run_research_backtest(
        manifest=manifest,
        db_path=None,
        manager=manager,
        strategy_registry=builtin_strategy_registry(),
    )
    candidate = selection_report["candidates"][0]
    candidate["aggregate_acceptance_gate_result"] = "PASS"
    candidate["acceptance_gate_result"] = "PASS"
    selection = apply_final_selection_contract(
        contract=manifest.final_selection,
        candidates=selection_report["candidates"],
        report_context={"dataset_quality_gate_status": "PASS"},
        validation_required=False,
    )
    selection_report["selection_artifact"] = build_selection_artifact(
        manifest_hash=manifest.manifest_hash(),
        selection_result=selection,
        candidates=selection_report["candidates"],
    )
    calls = []
    actual_confirmation = run_final_holdout_confirmation

    monkeypatch.setattr(
        "market_research.research.validation_pipeline.run_research_backtest",
        lambda **_kwargs: selection_report,
    )

    def confirm_once(**kwargs):
        calls.append(kwargs["selection_report"]["selection_artifact"]["selected_candidate_id"])
        return actual_confirmation(**kwargs)

    monkeypatch.setattr(
        "market_research.research.validation_pipeline.run_final_holdout_confirmation",
        confirm_once,
    )

    summary = run_research_validation(
        manifest=manifest,
        db_path=None,
        manager=manager,
        manifest_path=str(tmp_path / "manifest.json"),
        strategy_registry=builtin_strategy_registry(),
    )

    assert calls == [selection_report["selection_artifact"]["selected_candidate_id"]]
    assert summary["final_holdout_confirmation"]["candidate_results"][0]["candidate_id"] == calls[0]
