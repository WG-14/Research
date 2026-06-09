from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bithumb_bot.paths import PathConfig, PathManager
from bithumb_bot.research.diagnostic_availability import DiagnosticAvailability
from bithumb_bot.research.diagnostic_coverage import FeatureHorizonCoverage
from bithumb_bot.research.feature_bucket_metrics import FeatureBucketMetric
from bithumb_bot.research.feature_provider_registry import FeatureProviderSpec, feature_provider_specs_for_names
from bithumb_bot.research.forward_diagnostics import (
    DatasetProvenance,
    FINAL_HOLDOUT_WARNING_REASON,
    ForwardDiagnosticsDatasetQuality,
    ForwardDiagnosticsResult,
)
from bithumb_bot.research.forward_targets import build_horizon_durations
from bithumb_bot.research.hashing import report_content_hash_payload, sha256_prefixed
from bithumb_bot.research.forward_diagnostics_report import (
    validate_forward_diagnostics_report_flags,
    write_forward_diagnostics_report,
)


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


def _metric(
    value: float,
    *,
    entry_price_mode: str = "next_open",
    path_start_policy: str = "entry_candle",
    intrabar_included: bool = True,
    mfe_mae_basis: str = "ohlc_entry_to_exit_candles",
) -> FeatureBucketMetric:
    return FeatureBucketMetric(
        feature_name="sma_gap",
        bucket_id="q00",
        bucket_label="quantile 1/1",
        horizon_label="1c",
        entry_price_mode=entry_price_mode,
        path_start_policy=path_start_policy,
        intrabar_included=intrabar_included,
        mfe_mae_basis=mfe_mae_basis,
        count=1,
        sample_start_ts=1,
        sample_end_ts=1,
        mean_gross_forward_return=value,
        median_gross_forward_return=value,
        win_rate=1.0,
        p10_gross_forward_return=value,
        p90_gross_forward_return=value,
        mean_mfe=0.02,
        median_mfe=0.02,
        mean_mae=-0.01,
        median_mae=-0.01,
        mfe_mae_ratio=2.0,
        warnings=(),
    )


def _dataset(*, content_hash: str = "sha256:" + "2" * 64, split_name: str = "train") -> DatasetProvenance:
    return DatasetProvenance(
        snapshot_id="snapshot1",
        source="test_source",
        market="BTC_KRW",
        interval="1m",
        split_name=split_name,
        date_range={"start": "2026-01-01", "end": "2026-01-02"},
        content_hash=content_hash,
        source_uri=None,
        source_content_hash=None,
        source_content_hash_status="derived_from_materialized_snapshot",
        source_schema_hash=None,
        source_schema_hash_status="not_applicable",
        source_locator_policy="source_locator_excluded_from_dataset_hash",
        adapter_provenance_hash=None,
    )


def _availability(*, status: str = "available", warnings: tuple[str, ...] = ()) -> DiagnosticAvailability:
    return DiagnosticAvailability(
        status=status,  # type: ignore[arg-type]
        fail_reasons=(),
        warnings=warnings,
        target_count=1,
        sample_count=1,
        feature_value_count=1,
    )


def _coverage(*, status: str = "available") -> tuple[FeatureHorizonCoverage, ...]:
    return (
        FeatureHorizonCoverage(
            feature_name="sma_gap",
            horizon_label="1c",
            requested=True,
            computed_count=1,
            missing_count=0,
            status=status,  # type: ignore[arg-type]
            reasons=(),
        ),
    )


def _dataset_quality(
    *,
    status: str = "PASS",
    report_hash: str = "sha256:" + "4" * 64,
    dataset_content_hash: str = "sha256:" + "2" * 64,
) -> ForwardDiagnosticsDatasetQuality:
    return ForwardDiagnosticsDatasetQuality(
        quality_gate_status=status,
        quality_gate_reasons=() if status == "PASS" else ("missing_candles",),
        dataset_quality_report_hash=report_hash,
        dataset_quality_report_payload={
            "artifact_type": "dataset_quality_report",
            "content_hash": report_hash,
            "dataset_content_hash": dataset_content_hash,
            "canonical_snapshot_hash": dataset_content_hash,
            "source_content_hash_status": "derived_from_materialized_snapshot",
            "source_schema_hash_status": "not_applicable",
            "source_locator_policy": "source_locator_excluded_from_dataset_hash",
        },
        dataset_content_hash=dataset_content_hash,
        canonical_snapshot_hash=dataset_content_hash,
        source_content_hash_status="derived_from_materialized_snapshot",
        source_schema_hash_status="not_applicable",
        source_locator_policy="source_locator_excluded_from_dataset_hash",
    )


def _result(
    value: float = 0.01,
    *,
    entry_price_mode: str = "next_open",
    path_start_policy: str = "entry_candle",
    intrabar_included: bool = True,
    mfe_mae_basis: str = "ohlc_entry_to_exit_candles",
    dataset: DatasetProvenance | None = None,
    dataset_quality: ForwardDiagnosticsDatasetQuality | None = None,
    availability: DiagnosticAvailability | None = None,
    coverage: tuple[FeatureHorizonCoverage, ...] | None = None,
    feature_provider_specs: tuple[FeatureProviderSpec, ...] | None = None,
    final_holdout_diagnostic_override: bool = False,
    warnings: tuple[dict[str, object], ...] = (),
    split_name: str = "train",
    horizon_steps: tuple[int, ...] = (1,),
    interval: str = "1m",
    degraded_override: bool = False,
) -> ForwardDiagnosticsResult:
    return ForwardDiagnosticsResult(
        experiment_id="exp1",
        split_name=split_name,
        feature_names=("sma_gap",),
        horizon_steps=horizon_steps,
        interval=interval,
        horizon_durations=build_horizon_durations(interval=interval, horizon_steps=horizon_steps),
        bucket_method="quantile:1",
        entry_price_mode=entry_price_mode,
        path_start_policy=path_start_policy,
        intrabar_included=intrabar_included,
        mfe_mae_basis=mfe_mae_basis,
        sample_count=1,
        target_count=1,
        availability=availability or _availability(),
        coverage=coverage or _coverage(),
        feature_provider_specs=feature_provider_specs or feature_provider_specs_for_names(("sma_gap",)),
        dataset_quality=dataset_quality or _dataset_quality(),
        feature_bucket_metrics=(
            _metric(
                value,
                entry_price_mode=entry_price_mode,
                path_start_policy=path_start_policy,
                intrabar_included=intrabar_included,
                mfe_mae_basis=mfe_mae_basis,
            ),
        ),
        feature_horizon_metrics=(
            _metric(
                value,
                entry_price_mode=entry_price_mode,
                path_start_policy=path_start_policy,
                intrabar_included=intrabar_included,
                mfe_mae_basis=mfe_mae_basis,
            ),
        ),
        warnings=warnings,
        dataset=dataset or _dataset(split_name=split_name),
        final_holdout_diagnostic_override=final_holdout_diagnostic_override,
        degraded_override=degraded_override,
    )


def test_forward_diagnostics_report_writes_diagnostic_only_flags(tmp_path: Path) -> None:
    report = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())

    assert report["artifact_type"] == "forward_return_diagnostic_report"
    assert report["diagnostic_only"] is True
    assert report["promotion_evidence"] is False
    assert report["approved_profile_evidence"] is False
    assert report["live_readiness_evidence"] is False
    assert report["capital_allocation_evidence"] is False


def test_forward_diagnostics_report_includes_non_promotable_taxonomy(tmp_path: Path) -> None:
    report = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())

    assert report["evidence_scope"] == "diagnostic_feature_mining"
    assert report["promotion_eligible"] is False
    assert report["promotion_grade"] is False
    assert report["non_promotable"] is True
    assert set(report["forbidden_uses"]) >= {
        "strategy_promotion",
        "approved_profile",
        "live_readiness",
        "capital_allocation",
    }
    assert report["operator_next_action"] == "run_research_validate_from_fixed_manifest"


def test_forward_diagnostics_report_writes_under_research_report_and_derived_paths(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    write_forward_diagnostics_report(manager=manager, manifest=_manifest(), result=_result())
    base = manager.data_dir()

    assert (base / "reports/research/exp1/forward_diagnostics_report.json").exists()
    assert (base / "derived/research/exp1/forward_diagnostics/feature_bucket_metrics.csv").exists()
    assert (base / "derived/research/exp1/forward_diagnostics/feature_horizon_metrics.csv").exists()
    assert (base / "derived/research/exp1/forward_diagnostics/warnings.json").exists()


def test_forward_diagnostics_report_does_not_use_candidate_report_fields(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    write_forward_diagnostics_report(manager=manager, manifest=_manifest(), result=_result())
    payload = json.loads((manager.data_dir() / "reports/research/exp1/forward_diagnostics_report.json").read_text())

    assert "candidate_count" not in payload
    assert "derived_candidates_hash" not in payload


def test_forward_diagnostics_report_content_hash_changes_when_metrics_change(tmp_path: Path) -> None:
    first = write_forward_diagnostics_report(manager=_manager(tmp_path / "a"), manifest=_manifest(), result=_result(0.01))
    second = write_forward_diagnostics_report(manager=_manager(tmp_path / "b"), manifest=_manifest(), result=_result(0.02))

    assert first["content_hash"] != second["content_hash"]


def test_forward_diagnostics_report_content_hash_changes_when_measurement_contract_changes(tmp_path: Path) -> None:
    report = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())
    changed = dict(report)
    changed.pop("content_hash")
    changed["measurement_contract"] = dict(report["measurement_contract"])
    changed["measurement_contract"]["return_basis"] = "unit_changed_return_basis"

    assert report["content_hash"] != sha256_prefixed(report_content_hash_payload(changed))


def test_forward_diagnostics_report_includes_path_policy(tmp_path: Path) -> None:
    report = write_forward_diagnostics_report(
        manager=_manager(tmp_path),
        manifest=_manifest(),
        result=_result(
            entry_price_mode="signal_close",
            path_start_policy="next_candle_after_signal_close",
            intrabar_included=False,
            mfe_mae_basis="ohlc_future_candles_only",
        ),
    )

    assert report["calculation_policy"] == {
        "entry_price_mode": "signal_close",
        "path_start_policy": "next_candle_after_signal_close",
        "intrabar_included": False,
        "mfe_mae_basis": "ohlc_future_candles_only",
    }


def test_forward_diagnostics_report_includes_dataset_provenance(tmp_path: Path) -> None:
    report = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())

    assert report["dataset"] == {
        "snapshot_id": "snapshot1",
        "source": "test_source",
        "market": "BTC_KRW",
        "interval": "1m",
        "split_name": "train",
        "date_range": {"start": "2026-01-01", "end": "2026-01-02"},
        "content_hash": "sha256:" + "2" * 64,
        "source_uri": None,
        "source_content_hash": None,
        "source_content_hash_status": "derived_from_materialized_snapshot",
        "source_schema_hash": None,
        "source_schema_hash_status": "not_applicable",
        "source_locator_policy": "source_locator_excluded_from_dataset_hash",
        "adapter_provenance_hash": None,
    }


def test_report_includes_feature_provider_specs(tmp_path: Path) -> None:
    report = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())

    assert report["feature_provider_specs"][0]["name"] == "sma_gap"
    assert report["feature_provider_specs"][0]["value_type"] == "float"
    assert report["feature_provider_specs"][0]["required_history"] == 20
    assert report["feature_provider_specs"][0]["bucketizer_type"] == "quantile"
    assert report["feature_provider_specs"][0]["definition_hash"].startswith("sha256:")
    assert report["feature_provider_specs"][0]["causal_inputs"] == ["candle.close"]
    assert report["feature_provider_specs"][0]["category_universe"] == []


def test_report_includes_horizon_duration_semantics(tmp_path: Path) -> None:
    report = write_forward_diagnostics_report(
        manager=_manager(tmp_path),
        manifest=_manifest(),
        result=_result(horizon_steps=(5,), interval="5m"),
    )

    assert report["interval"] == "5m"
    assert report["horizon_steps"] == [5]
    assert report["horizon_durations"][0]["horizon_label"] == "5c"
    assert report["horizon_durations"][0]["horizon_duration_label"] == "25m"


def test_report_includes_dataset_quality_status_and_hash(tmp_path: Path) -> None:
    report = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())

    assert report["dataset_quality"]["quality_gate_status"] == "PASS"
    assert report["dataset_quality"]["quality_gate_reasons"] == []
    assert report["dataset_quality"]["dataset_quality_report_hash"] == "sha256:" + "4" * 64
    assert "dataset_quality_report_payload" in report["dataset_quality"]
    assert report["dataset_quality"]["canonical_snapshot_hash"] == "sha256:" + "2" * 64


def test_forward_diagnostics_metrics_csv_includes_sample_time_range(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    write_forward_diagnostics_report(manager=manager, manifest=_manifest(), result=_result())
    csv_text = (
        manager.data_dir()
        / "derived/research/exp1/forward_diagnostics/feature_bucket_metrics.csv"
    ).read_text(encoding="utf-8")

    assert "sample_start_ts" in csv_text.splitlines()[0]
    assert "sample_end_ts" in csv_text.splitlines()[0]


def test_report_content_hash_changes_when_sample_time_range_changes(tmp_path: Path) -> None:
    first = write_forward_diagnostics_report(manager=_manager(tmp_path / "a"), manifest=_manifest(), result=_result())
    metric = _metric(0.01)
    changed_metric = FeatureBucketMetric(
        feature_name=metric.feature_name,
        bucket_id=metric.bucket_id,
        bucket_label=metric.bucket_label,
        horizon_label=metric.horizon_label,
        entry_price_mode=metric.entry_price_mode,
        path_start_policy=metric.path_start_policy,
        intrabar_included=metric.intrabar_included,
        mfe_mae_basis=metric.mfe_mae_basis,
        count=metric.count,
        sample_start_ts=10,
        sample_end_ts=10,
        mean_gross_forward_return=metric.mean_gross_forward_return,
        median_gross_forward_return=metric.median_gross_forward_return,
        win_rate=metric.win_rate,
        p10_gross_forward_return=metric.p10_gross_forward_return,
        p90_gross_forward_return=metric.p90_gross_forward_return,
        mean_mfe=metric.mean_mfe,
        median_mfe=metric.median_mfe,
        mean_mae=metric.mean_mae,
        median_mae=metric.median_mae,
        mfe_mae_ratio=metric.mfe_mae_ratio,
        warnings=metric.warnings,
    )
    result = _result()
    object.__setattr__(result, "feature_bucket_metrics", (changed_metric,))
    object.__setattr__(result, "feature_horizon_metrics", (changed_metric,))
    second = write_forward_diagnostics_report(manager=_manager(tmp_path / "b"), manifest=_manifest(), result=result)

    assert first["content_hash"] != second["content_hash"]


def test_feature_horizon_metrics_csv_has_aggregate_schema(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    write_forward_diagnostics_report(manager=manager, manifest=_manifest(), result=_result())
    header = (
        manager.data_dir()
        / "derived/research/exp1/forward_diagnostics/feature_horizon_metrics.csv"
    ).read_text(encoding="utf-8").splitlines()[0].split(",")

    assert "bucket_id" not in header
    assert "bucket_label" not in header
    assert "mean_gross_forward_return" in header
    assert "return_basis" in header


def test_report_includes_requested_feature_horizon_coverage(tmp_path: Path) -> None:
    report = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())

    assert report["coverage"]["feature_horizon"] == [
        {
            "feature_name": "sma_gap",
            "horizon_label": "1c",
            "requested": True,
            "computed_count": 1,
            "missing_count": 0,
            "status": "available",
            "reasons": [],
        }
    ]


def test_report_content_hash_changes_when_dataset_content_hash_changes(tmp_path: Path) -> None:
    first = write_forward_diagnostics_report(
        manager=_manager(tmp_path / "a"),
        manifest=_manifest(),
        result=_result(dataset=_dataset(content_hash="sha256:" + "2" * 64)),
    )
    second = write_forward_diagnostics_report(
        manager=_manager(tmp_path / "b"),
        manifest=_manifest(),
        result=_result(dataset=_dataset(content_hash="sha256:" + "3" * 64)),
    )

    assert first["content_hash"] != second["content_hash"]


def test_report_content_hash_changes_when_dataset_quality_hash_changes(tmp_path: Path) -> None:
    first = write_forward_diagnostics_report(
        manager=_manager(tmp_path / "a"),
        manifest=_manifest(),
        result=_result(dataset_quality=_dataset_quality(report_hash="sha256:" + "4" * 64)),
    )
    second = write_forward_diagnostics_report(
        manager=_manager(tmp_path / "b"),
        manifest=_manifest(),
        result=_result(dataset_quality=_dataset_quality(report_hash="sha256:" + "5" * 64)),
    )

    assert first["content_hash"] != second["content_hash"]


def test_coverage_rows_are_included_in_report_content_hash(tmp_path: Path) -> None:
    first = write_forward_diagnostics_report(
        manager=_manager(tmp_path / "a"),
        manifest=_manifest(),
        result=_result(coverage=_coverage(status="available")),
    )
    second = write_forward_diagnostics_report(
        manager=_manager(tmp_path / "b"),
        manifest=_manifest(),
        result=_result(
            availability=_availability(status="degraded", warnings=("feature_horizon_coverage_incomplete",)),
            coverage=(
                FeatureHorizonCoverage(
                    feature_name="sma_gap",
                    horizon_label="1c",
                    requested=True,
                    computed_count=0,
                    missing_count=1,
                    status="unavailable",
                    reasons=("feature_history_unavailable",),
                ),
            ),
        ),
    )

    assert first["content_hash"] != second["content_hash"]


def test_forward_diagnostics_metrics_csv_includes_path_policy_columns(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    write_forward_diagnostics_report(manager=manager, manifest=_manifest(), result=_result())
    csv_text = (
        manager.data_dir()
        / "derived/research/exp1/forward_diagnostics/feature_bucket_metrics.csv"
    ).read_text(encoding="utf-8")

    header = csv_text.splitlines()[0].split(",")
    assert "entry_price_mode" in header
    assert "path_start_policy" in header
    assert "intrabar_included" in header
    assert "mfe_mae_basis" in header


def test_report_content_hash_changes_when_path_policy_changes(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    first = write_forward_diagnostics_report(manager=manager, manifest=_manifest(), result=_result())
    second = write_forward_diagnostics_report(
        manager=manager,
        manifest=_manifest(),
        result=_result(
            entry_price_mode="signal_close",
            path_start_policy="next_candle_after_signal_close",
            intrabar_included=False,
            mfe_mae_basis="ohlc_future_candles_only",
        ),
    )

    assert first["content_hash"] != second["content_hash"]


def test_forward_diagnostics_report_rejects_promotion_evidence_true(tmp_path: Path) -> None:
    payload = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())
    payload["promotion_evidence"] = True

    with pytest.raises(ValueError, match="diagnostic-only"):
        validate_forward_diagnostics_report_flags(payload)


def test_report_writer_rejects_final_holdout_without_override(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    with pytest.raises(ValueError, match="final_holdout_diagnostic_override_required"):
        write_forward_diagnostics_report(
            manager=manager,
            manifest=_manifest(),
            result=_result(split_name="final_holdout", final_holdout_diagnostic_override=False),
        )

    assert not (manager.data_dir() / "reports/research/exp1/forward_diagnostics_report.json").exists()
    assert not (manager.data_dir() / "derived/research/exp1/forward_diagnostics/warnings.json").exists()


def test_report_writer_rejects_final_holdout_override_without_warning(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    with pytest.raises(ValueError, match=FINAL_HOLDOUT_WARNING_REASON):
        write_forward_diagnostics_report(
            manager=manager,
            manifest=_manifest(),
            result=_result(split_name="final_holdout", final_holdout_diagnostic_override=True, warnings=()),
        )

    assert not (manager.data_dir() / "reports/research/exp1/forward_diagnostics_report.json").exists()


def test_report_writer_accepts_final_holdout_with_override_warning(tmp_path: Path) -> None:
    report = write_forward_diagnostics_report(
        manager=_manager(tmp_path),
        manifest=_manifest(),
        result=_result(
            split_name="final_holdout",
            final_holdout_diagnostic_override=True,
            warnings=({"reason": FINAL_HOLDOUT_WARNING_REASON, "split_name": "final_holdout"},),
        ),
    )

    assert report["split_name"] == "final_holdout"
    assert report["final_holdout_diagnostic_override"] is True
    assert {warning["reason"] for warning in report["warnings"]} == {FINAL_HOLDOUT_WARNING_REASON}


def test_report_writer_accepts_train_without_holdout_warning(tmp_path: Path) -> None:
    report = write_forward_diagnostics_report(
        manager=_manager(tmp_path),
        manifest=_manifest(),
        result=_result(split_name="train", final_holdout_diagnostic_override=False, warnings=()),
    )

    assert report["split_name"] == "train"
    assert report["warnings"] == []


def test_report_flag_validator_rejects_all_forbidden_evidence_flags(tmp_path: Path) -> None:
    base = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())

    for field in (
        "promotion_evidence",
        "approved_profile_evidence",
        "live_readiness_evidence",
        "capital_allocation_evidence",
    ):
        payload = dict(base)
        payload[field] = True
        with pytest.raises(ValueError, match="diagnostic-only"):
            validate_forward_diagnostics_report_flags(payload)


def test_forward_diagnostics_report_validator_rejects_promotable_taxonomy(tmp_path: Path) -> None:
    payload = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())
    payload["non_promotable"] = False

    with pytest.raises(ValueError, match="non_promotable"):
        validate_forward_diagnostics_report_flags(payload)
