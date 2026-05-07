from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bithumb_bot.paths import PathManager
from bithumb_bot.research.backtest_engine import run_sma_backtest
from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.execution_calibration import build_calibration_artifact
from bithumb_bot.research.execution_model import FixedBpsExecutionModel, StressExecutionModel
from bithumb_bot.research.experiment_manifest import parse_manifest
from bithumb_bot.research.parameter_space import candidate_id
from bithumb_bot.research.promotion_gate import PromotionGateError, promote_candidate
from bithumb_bot.research.validation_protocol import run_research_backtest


def _ts(day: str, minute: int) -> int:
    base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(base.timestamp() * 1000) + minute * 60_000


def _create_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE candles(
                ts INTEGER PRIMARY KEY,
                pair TEXT,
                interval TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL
            )
            """
        )
        for day in ("2023-01-01", "2023-01-02", "2023-01-03"):
            closes = [100, 99, 98, 99, 101, 103, 102, 100, 98, 97, 99, 102, 104, 103]
            for index, close in enumerate(closes):
                conn.execute(
                    """
                    INSERT INTO candles(ts, pair, interval, open, high, low, close, volume)
                    VALUES (?, 'KRW-BTC', '1m', ?, ?, ?, ?, 1.0)
                    """,
                    (_ts(day, index), close, close * 1.01, close * 0.99, close),
                )
        conn.commit()
    finally:
        conn.close()


def _manifest() -> dict[str, object]:
    return {
        "experiment_id": "deterministic_sma",
        "hypothesis": "SMA candidate remains deterministic across repeated research runs.",
        "strategy_name": "sma_with_filter",
        "market": "KRW-BTC",
        "interval": "1m",
        "dataset": {
            "source": "sqlite_candles",
            "snapshot_id": "unit_candles_v1",
            "train": {"start": "2023-01-01", "end": "2023-01-01"},
            "validation": {"start": "2023-01-02", "end": "2023-01-02"},
            "final_holdout": {"start": "2023-01-03", "end": "2023-01-03"},
        },
        "parameter_space": {
            "SMA_SHORT": [2],
            "SMA_LONG": [4],
            "SMA_FILTER_GAP_MIN_RATIO": [0.0],
            "SMA_FILTER_VOL_MIN_RANGE_RATIO": [0.0],
        },
        "cost_model": {"fee_rate": 0.0, "slippage_bps": [0]},
        "acceptance_gate": {
            "min_trade_count": 1,
            "max_mdd_pct": 90,
            "min_profit_factor": 0.1,
            "oos_return_must_be_positive": False,
            "parameter_stability_required": False,
        },
    }


def test_same_manifest_and_dataset_produce_same_content_hash(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "candles.sqlite"
    _create_db(db_path)
    for key in ("ENV_ROOT", "RUN_ROOT", "DATA_ROOT", "LOG_ROOT", "BACKUP_ROOT", "ARCHIVE_ROOT"):
        monkeypatch.setenv(key, str(tmp_path / f"{key.lower()}_root"))
    monkeypatch.setenv("MODE", "paper")
    manager = PathManager.from_env(Path.cwd())
    manifest = parse_manifest(_manifest())

    first = run_research_backtest(
        manifest=manifest,
        db_path=db_path,
        manager=manager,
        generated_at="2026-05-03T00:00:00+00:00",
    )
    second = run_research_backtest(
        manifest=manifest,
        db_path=db_path,
        manager=manager,
        generated_at="2026-05-03T00:00:00+00:00",
    )

    assert first["content_hash"] == second["content_hash"]
    assert first["candidates"][0]["candidate_profile_hash"] == second["candidates"][0]["candidate_profile_hash"]
    assert first["candidates"][0]["regime_classifier_version"] == "market_regime_v2"
    assert first["candidates"][0]["market_regime_bucket_performance"]
    assert first["candidates"][0]["market_regime_coverage"]
    assert "regime_gate_result" in first["candidates"][0]
    assert Path(first["artifact_paths"]["report_path"]).exists()


def test_sma_backtest_attaches_entry_and_exit_regime_snapshots() -> None:
    candles = tuple(
        Candle(
            ts=1_700_000_000_000 + index * 60_000,
            open=float(close),
            high=float(close) * 1.02,
            low=float(close) * 0.98,
            close=float(close),
            volume=float(100 + index * 10),
        )
        for index, close in enumerate([100, 99, 98, 97, 99, 102, 105, 104, 103, 100, 98, 96])
    )
    manifest = parse_manifest(_manifest())
    snapshot = DatasetSnapshot(
        snapshot_id="unit",
        source="sqlite_candles",
        market="KRW-BTC",
        interval="1m",
        split_name="validation",
        date_range=manifest.dataset.split.validation,
        candles=candles,
    )

    result = run_sma_backtest(
        dataset=snapshot,
        parameter_values={"SMA_SHORT": 2, "SMA_LONG": 4, "SMA_FILTER_GAP_MIN_RATIO": 0.0, "SMA_FILTER_VOL_MIN_RANGE_RATIO": 0.0},
        fee_rate=0.0,
        slippage_bps=0.0,
    )

    closed = [trade for trade in result.trades if trade["side"] == "SELL"]
    assert closed
    assert closed[0]["entry_regime"]
    assert closed[0]["exit_regime"]
    assert isinstance(closed[0]["entry_regime_snapshot"], dict)
    assert isinstance(closed[0]["exit_regime_snapshot"], dict)
    assert result.regime_performance
    assert result.regime_coverage


def test_fixed_bps_execution_model_preserves_legacy_backtest_metrics() -> None:
    manifest = parse_manifest(_manifest())
    snapshot = DatasetSnapshot(
        snapshot_id="unit",
        source="sqlite_candles",
        market="KRW-BTC",
        interval="1m",
        split_name="validation",
        date_range=manifest.dataset.split.validation,
        candles=tuple(
            Candle(
                ts=1_700_000_000_000 + index * 60_000,
                open=float(close),
                high=float(close) * 1.01,
                low=float(close) * 0.99,
                close=float(close),
                volume=1.0,
            )
            for index, close in enumerate([100, 99, 98, 99, 101, 103, 102, 100, 98, 97, 99, 102])
        ),
    )

    legacy = run_sma_backtest(
        dataset=snapshot,
        parameter_values={"SMA_SHORT": 2, "SMA_LONG": 4},
        fee_rate=0.001,
        slippage_bps=5.0,
    )
    modeled = run_sma_backtest(
        dataset=snapshot,
        parameter_values={"SMA_SHORT": 2, "SMA_LONG": 4},
        fee_rate=0.001,
        slippage_bps=5.0,
        execution_model=FixedBpsExecutionModel(fee_rate=0.001, slippage_bps=5.0),
    )

    assert modeled.metrics.as_dict() == legacy.metrics.as_dict()
    assert modeled.trades[0]["execution"]["model_name"] == "fixed_bps"
    assert modeled.trades[0]["execution"]["model_params_hash"].startswith("sha256:")


def test_seeded_stress_execution_model_is_deterministic_and_auditable() -> None:
    manifest = parse_manifest(_manifest())
    snapshot = DatasetSnapshot(
        snapshot_id="unit",
        source="sqlite_candles",
        market="KRW-BTC",
        interval="1m",
        split_name="validation",
        date_range=manifest.dataset.split.validation,
        candles=tuple(
            Candle(
                ts=1_700_000_000_000 + index * 60_000,
                open=float(close),
                high=float(close) * 1.01,
                low=float(close) * 0.99,
                close=float(close),
                volume=1.0,
            )
            for index, close in enumerate([100, 99, 98, 99, 101, 103, 102, 100, 98, 97, 99, 102])
        ),
    )
    def _run():
        return run_sma_backtest(
            dataset=snapshot,
            parameter_values={"SMA_SHORT": 2, "SMA_LONG": 4},
            fee_rate=0.001,
            slippage_bps=20.0,
            execution_model=StressExecutionModel(
            fee_rate=0.001,
            slippage_bps=20.0,
            latency_ms=500,
            partial_fill_rate=1.0,
            order_failure_rate=0.0,
            market_order_extra_cost_bps=5.0,
            seed=42,
            ),
        )

    first = _run()
    second = _run()

    assert first.trades == second.trades
    execution = first.trades[0]["execution"]
    assert execution["fill_status"] == "partial"
    assert execution["latency_ms"] == 500
    assert execution["slippage_bps"] == 25.0
    assert execution["fee"] >= 0.0
    assert execution["filled_qty"] > 0.0
    assert execution["remaining_qty"] > 0.0


def test_research_backtest_fails_candidate_when_calibration_breaches_assumptions(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "candles.sqlite"
    _create_db(db_path)
    for key in ("ENV_ROOT", "RUN_ROOT", "DATA_ROOT", "LOG_ROOT", "BACKUP_ROOT", "ARCHIVE_ROOT"):
        monkeypatch.setenv(key, str(tmp_path / f"{key.lower()}_root"))
    monkeypatch.setenv("MODE", "paper")
    manager = PathManager.from_env(Path.cwd())
    payload = _manifest()
    payload["execution_model"] = {
        "type": "stress",
        "fee_rate": [0.0],
        "slippage_bps": [5],
        "latency_ms": [100],
        "calibration_required": True,
    }
    manifest = parse_manifest(payload)
    calibration = build_calibration_artifact(
        summary={
            "sample_count": 50,
            "median_slippage_vs_signal_bps": 8.0,
            "p90_slippage_vs_signal_bps": 12.0,
            "p95_slippage_vs_signal_bps": 20.0,
            "p95_submit_to_fill_ms": 200,
            "partial_fill_rate": 0.0,
            "unfilled_rate": 0.0,
            "model_breach_rate": 0.0,
            "quality_gate_status": "PASS",
        },
        market="KRW-BTC",
        interval="1m",
        generated_at="2026-05-03T00:00:00+00:00",
    )

    report = run_research_backtest(
        manifest=manifest,
        db_path=db_path,
        manager=manager,
        generated_at="2026-05-03T00:00:00+00:00",
        execution_calibration=calibration,
    )

    assert report["gate_result"] == "FAIL"
    assert "execution_calibration_p90_slippage_exceeds_assumption" in report["candidates"][0]["gate_fail_reasons"]


def test_research_backtest_aggregates_scenarios_and_promotion_refuses_failed_stress(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "candles.sqlite"
    _create_db(db_path)
    for key in ("ENV_ROOT", "RUN_ROOT", "DATA_ROOT", "LOG_ROOT", "BACKUP_ROOT", "ARCHIVE_ROOT"):
        monkeypatch.setenv(key, str(tmp_path / f"{key.lower()}_root"))
    monkeypatch.setenv("MODE", "paper")
    manager = PathManager.from_env(Path.cwd())
    payload = _manifest()
    payload["experiment_id"] = "scenario_aggregation_integration"
    payload["execution_model"] = {
        "type": "stress",
        "fee_rate": [0.0],
        "slippage_bps": [0.0],
        "order_failure_rate": [0.0, 1.0],
        "seed": 42,
    }
    manifest = parse_manifest(payload)

    report = run_research_backtest(
        manifest=manifest,
        db_path=db_path,
        manager=manager,
        generated_at="2026-05-03T00:00:00+00:00",
    )

    assert report["candidate_count"] == 1
    candidate = report["candidates"][0]
    assert candidate["scenario_policy"] == "must_pass_base_and_survive_stress"
    assert len(candidate["scenario_results"]) == 2
    assert [result["scenario_role"] for result in candidate["scenario_results"]] == ["base", "stress"]
    assert [result["scenario_role_source"] for result in candidate["scenario_results"]] == ["derived", "derived"]
    assert candidate["acceptance_gate_result"] == "FAIL"
    assert candidate["scenario_fail_count"] > 0
    assert "scenario_policy_no_passing_stress_scenario" in candidate["gate_fail_reasons"]
    assert candidate["candidate_profile_hash"].startswith("sha256:")
    assert Path(report["artifact_paths"]["report_path"]).exists()

    with pytest.raises(PromotionGateError, match="scenario_policy"):
        promote_candidate(
            experiment_id="scenario_aggregation_integration",
            candidate_id=candidate["parameter_candidate_id"],
            manager=manager,
        )


def test_stress_report_is_candidate_order_independent(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "candles.sqlite"
    _create_db(db_path)
    for key in ("ENV_ROOT", "RUN_ROOT", "DATA_ROOT", "LOG_ROOT", "BACKUP_ROOT", "ARCHIVE_ROOT"):
        monkeypatch.setenv(key, str(tmp_path / f"{key.lower()}_root"))
    monkeypatch.setenv("MODE", "paper")
    manager = PathManager.from_env(Path.cwd())
    payload = _manifest()
    payload["parameter_space"] = {
        "SMA_SHORT": [2, 3],
        "SMA_LONG": [4],
        "SMA_FILTER_GAP_MIN_RATIO": [0.0],
        "SMA_FILTER_VOL_MIN_RANGE_RATIO": [0.0],
    }
    payload["execution_model"] = {
        "type": "stress",
        "fee_rate": [0.0],
        "slippage_bps": [5, 10],
        "partial_fill_rate": [0.5],
        "order_failure_rate": [0.1],
        "scenario_policy": "must_pass_base_and_survive_stress",
        "seed": 42,
    }
    reordered = dict(payload)
    reordered["parameter_space"] = {
        "SMA_SHORT": [3, 2],
        "SMA_LONG": [4],
        "SMA_FILTER_GAP_MIN_RATIO": [0.0],
        "SMA_FILTER_VOL_MIN_RANGE_RATIO": [0.0],
    }
    target_params = {
        "SMA_SHORT": 2,
        "SMA_LONG": 4,
        "SMA_FILTER_GAP_MIN_RATIO": 0.0,
        "SMA_FILTER_VOL_MIN_RANGE_RATIO": 0.0,
    }
    target_id = candidate_id(target_params, 0)

    first = run_research_backtest(
        manifest=parse_manifest(payload),
        db_path=db_path,
        manager=manager,
        generated_at="2026-05-03T00:00:00+00:00",
    )
    second = run_research_backtest(
        manifest=parse_manifest(reordered),
        db_path=db_path,
        manager=manager,
        generated_at="2026-05-03T00:00:00+00:00",
    )

    first_candidate = {item["parameter_candidate_id"]: item for item in first["candidates"]}[target_id]
    second_candidate = {item["parameter_candidate_id"]: item for item in second["candidates"]}[target_id]
    for first_scenario, second_scenario in zip(
        first_candidate["scenario_results"],
        second_candidate["scenario_results"],
        strict=True,
    ):
        assert first_scenario["scenario_id"] == second_scenario["scenario_id"]
        assert first_scenario["validation_metrics"] == second_scenario["validation_metrics"]
        assert first_scenario["validation_execution_metadata"] == second_scenario["validation_execution_metadata"]
    execution = first_candidate["scenario_results"][0]["validation_execution_metadata"][0]
    assert execution["base_seed"] == 42
    assert execution["derived_seed_hash"].startswith("sha256:")
    assert execution["seed_derivation_inputs"]["parameter_candidate_id"] == target_id


def test_different_stress_seed_changes_auditable_seed_hash(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "candles.sqlite"
    _create_db(db_path)
    for key in ("ENV_ROOT", "RUN_ROOT", "DATA_ROOT", "LOG_ROOT", "BACKUP_ROOT", "ARCHIVE_ROOT"):
        monkeypatch.setenv(key, str(tmp_path / f"{key.lower()}_root"))
    monkeypatch.setenv("MODE", "paper")
    manager = PathManager.from_env(Path.cwd())
    payload = _manifest()
    payload["execution_model"] = {
        "type": "stress",
        "fee_rate": [0.0],
        "slippage_bps": [5, 10],
        "partial_fill_rate": [0.5],
        "order_failure_rate": [0.1],
        "scenario_policy": "must_pass_base_and_survive_stress",
        "seed": 42,
    }
    changed_seed = dict(payload)
    changed_seed["execution_model"] = dict(payload["execution_model"])
    changed_seed["execution_model"]["seed"] = 43

    first = run_research_backtest(
        manifest=parse_manifest(payload),
        db_path=db_path,
        manager=manager,
        generated_at="2026-05-03T00:00:00+00:00",
    )
    second = run_research_backtest(
        manifest=parse_manifest(changed_seed),
        db_path=db_path,
        manager=manager,
        generated_at="2026-05-03T00:00:00+00:00",
    )

    first_execution = first["candidates"][0]["scenario_results"][0]["validation_execution_metadata"][0]
    second_execution = second["candidates"][0]["scenario_results"][0]["validation_execution_metadata"][0]
    assert first_execution["base_seed"] == 42
    assert second_execution["base_seed"] == 43
    assert first_execution["derived_seed_hash"] != second_execution["derived_seed_hash"]
