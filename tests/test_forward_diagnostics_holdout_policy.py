from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bithumb_bot.paths import PathConfig, PathManager
from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.diagnostic_availability import DiagnosticAvailability
from bithumb_bot.research.diagnostic_coverage import FeatureHorizonCoverage
from bithumb_bot.research.experiment_manifest import DateRange
from bithumb_bot.research.feature_bucket_metrics import FeatureBucketMetric
from bithumb_bot.research.feature_provider_registry import feature_provider_specs_for_names
from bithumb_bot.research.forward_diagnostics import (
    FINAL_HOLDOUT_WARNING_REASON,
    DatasetProvenance,
    ForwardDiagnosticsDatasetQuality,
    ForwardDiagnosticsResult,
    run_forward_diagnostics_on_snapshot,
)
from bithumb_bot.research.forward_diagnostics_report import write_forward_diagnostics_report
from bithumb_bot.research.split_usage_policy import SplitUsagePolicyError


def _manager(tmp_path: Path) -> PathManager:
    return PathManager(
        project_root=Path(__file__).resolve().parents[1],
        config=PathConfig(
            mode="paper",
            env_root=tmp_path / "env",
            run_root=tmp_path / "run",
            data_root=tmp_path / "data",
            log_root=tmp_path / "logs",
            backup_root=tmp_path / "backup",
            archive_root=tmp_path / "archive",
        ),
    )


def _manifest():
    return SimpleNamespace(experiment_id="exp1", manifest_hash=lambda: "sha256:" + "1" * 64)


def _snapshot(*, split_name: str) -> DatasetSnapshot:
    candles = tuple(
        Candle(ts=index, open=100 + index, high=102 + index, low=99 + index, close=101 + index, volume=10 + index)
        for index in range(25)
    )
    return DatasetSnapshot(
        snapshot_id=f"snapshot-{split_name}",
        source="test",
        market="BTC_KRW",
        interval="1m",
        split_name=split_name,
        date_range=DateRange("2026-01-01", "2026-01-02"),
        candles=candles,
    )


def _metric() -> FeatureBucketMetric:
    return FeatureBucketMetric(
        feature_name="sma_gap",
        bucket_id="q00",
        bucket_label="quantile 1/1",
        horizon_label="1c",
        entry_price_mode="next_open",
        path_start_policy="entry_candle",
        intrabar_included=True,
        mfe_mae_basis="ohlc_entry_to_exit_candles",
        count=1,
        mean_forward_return=0.01,
        median_forward_return=0.01,
        win_rate=1.0,
        p10_forward_return=0.01,
        p90_forward_return=0.01,
        mean_mfe=0.02,
        median_mfe=0.02,
        mean_mae=-0.01,
        median_mae=-0.01,
        mfe_mae_ratio=2.0,
        warnings=(),
    )


def _result(*, split_name: str, override: bool) -> ForwardDiagnosticsResult:
    warnings = (
        {
            "reason": FINAL_HOLDOUT_WARNING_REASON,
            "split_name": split_name,
        },
    ) if override else ()
    return ForwardDiagnosticsResult(
        experiment_id="exp1",
        split_name=split_name,
        feature_names=("sma_gap",),
        horizon_steps=(1,),
        bucket_method="quantile:1",
        entry_price_mode="next_open",
        path_start_policy="entry_candle",
        intrabar_included=True,
        mfe_mae_basis="ohlc_entry_to_exit_candles",
        sample_count=1,
        target_count=1,
        availability=DiagnosticAvailability(
            status="available",
            fail_reasons=(),
            warnings=(),
            target_count=1,
            sample_count=1,
            feature_value_count=1,
        ),
        coverage=(
            FeatureHorizonCoverage(
                feature_name="sma_gap",
                horizon_label="1c",
                requested=True,
                computed_count=1,
                missing_count=0,
                status="available",
                reasons=(),
            ),
        ),
        feature_provider_specs=feature_provider_specs_for_names(("sma_gap",)),
        dataset_quality=ForwardDiagnosticsDatasetQuality(
            quality_gate_status="PASS",
            quality_gate_reasons=(),
            dataset_quality_report_hash="sha256:" + "4" * 64,
            dataset_content_hash="sha256:" + "2" * 64,
        ),
        feature_bucket_metrics=(_metric(),),
        feature_horizon_metrics=(_metric(),),
        warnings=warnings,
        dataset=DatasetProvenance(
            snapshot_id="snapshot1",
            source="test",
            market="BTC_KRW",
            interval="1m",
            split_name=split_name,
            date_range={"start": "2026-01-01", "end": "2026-01-02"},
            content_hash="sha256:" + "2" * 64,
            source_uri=None,
            source_content_hash=None,
            source_schema_hash=None,
            adapter_provenance_hash=None,
        ),
        final_holdout_diagnostic_override=override,
    )


def test_final_holdout_override_is_recorded_in_report(tmp_path: Path) -> None:
    report = write_forward_diagnostics_report(
        manager=_manager(tmp_path),
        manifest=_manifest(),
        result=_result(split_name="final_holdout", override=True),
    )

    assert report["final_holdout_diagnostic_override"] is True
    assert {warning["reason"] for warning in report["warnings"]} == {FINAL_HOLDOUT_WARNING_REASON}


def test_train_split_does_not_record_holdout_override_warning(tmp_path: Path) -> None:
    report = write_forward_diagnostics_report(
        manager=_manager(tmp_path),
        manifest=_manifest(),
        result=_result(split_name="train", override=False),
    )

    assert report["final_holdout_diagnostic_override"] is False
    assert FINAL_HOLDOUT_WARNING_REASON not in str(report["warnings"])


def test_core_rejects_final_holdout_without_override() -> None:
    with pytest.raises(SplitUsagePolicyError) as exc:
        run_forward_diagnostics_on_snapshot(
            snapshot=_snapshot(split_name="final_holdout"),
            feature_names=("sma_gap",),
            horizon_steps=(1,),
            bucket_method="quantile:1",
            final_holdout_diagnostic_override=False,
            min_bucket_count=1,
        )

    assert exc.value.reason == "final_holdout_diagnostic_override_required"


def test_snapshot_core_rejects_final_holdout_without_override() -> None:
    test_core_rejects_final_holdout_without_override()


def test_core_allows_final_holdout_only_with_override_and_warning() -> None:
    result = run_forward_diagnostics_on_snapshot(
        snapshot=_snapshot(split_name="final_holdout"),
        feature_names=("sma_gap",),
        horizon_steps=(1,),
        bucket_method="quantile:1",
        final_holdout_diagnostic_override=True,
        min_bucket_count=1,
    )

    assert result.final_holdout_diagnostic_override is True
    assert {warning["reason"] for warning in result.warnings} >= {FINAL_HOLDOUT_WARNING_REASON}


def test_final_holdout_diagnostic_override_is_report_only_by_policy(tmp_path: Path) -> None:
    report = write_forward_diagnostics_report(
        manager=_manager(tmp_path),
        manifest=_manifest(),
        result=_result(split_name="final_holdout", override=True),
    )

    assert report["final_holdout_diagnostic_override"] is True
    assert {warning["reason"] for warning in report["warnings"]} == {FINAL_HOLDOUT_WARNING_REASON}
    assert "experiment_registry_path" not in report
    assert "experiment_registry_row_hash" not in report
    assert "final_holdout_diagnostic_override" not in report.get("artifact_paths", {})


def test_train_and_validation_do_not_require_override() -> None:
    for split_name in ("train", "validation"):
        result = run_forward_diagnostics_on_snapshot(
            snapshot=_snapshot(split_name=split_name),
            feature_names=("sma_gap",),
            horizon_steps=(1,),
            bucket_method="quantile:1",
            final_holdout_diagnostic_override=False,
            min_bucket_count=1,
        )
        assert result.split_name == split_name
        assert result.final_holdout_diagnostic_override is False
