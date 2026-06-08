from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from bithumb_bot.paths import PathConfig, PathManager
from bithumb_bot.research.diagnostic_availability import DiagnosticAvailability
from bithumb_bot.research.diagnostic_coverage import FeatureHorizonCoverage
from bithumb_bot.research.feature_bucket_metrics import FeatureBucketMetric
from bithumb_bot.research.feature_provider_registry import feature_provider_specs_for_names
from bithumb_bot.research.forward_diagnostics import (
    FINAL_HOLDOUT_WARNING_REASON,
    DatasetProvenance,
    ForwardDiagnosticsDatasetQuality,
    ForwardDiagnosticsResult,
)
from bithumb_bot.research.forward_diagnostics_report import write_forward_diagnostics_report


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
