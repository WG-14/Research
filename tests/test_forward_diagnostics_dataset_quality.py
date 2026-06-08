from __future__ import annotations

from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.experiment_manifest import DateRange
from bithumb_bot.research.forward_diagnostics import (
    DATASET_QUALITY_FAIL_POLICY,
    ForwardDiagnosticsDatasetQuality,
    run_forward_diagnostics_on_snapshot,
)
from bithumb_bot.research.forward_diagnostics_report import write_forward_diagnostics_report
from tests.test_forward_diagnostics_report import _dataset_quality, _manager, _manifest, _result


def _snapshot(count: int = 25) -> DatasetSnapshot:
    candles = tuple(
        Candle(ts=index, open=100 + index, high=102 + index, low=99 + index, close=101 + index, volume=10 + index)
        for index in range(count)
    )
    return DatasetSnapshot(
        snapshot_id="snapshot",
        source="test",
        market="BTC_KRW",
        interval="1m",
        split_name="train",
        date_range=DateRange("2026-01-01", "2026-01-02"),
        candles=candles,
    )


def test_report_includes_dataset_quality_status_and_hash(tmp_path) -> None:
    report = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())

    assert report["dataset_quality"]["quality_gate_status"] == "PASS"
    assert report["dataset_quality"]["quality_gate_reasons"] == []
    assert report["dataset_quality"]["dataset_quality_report_hash"].startswith("sha256:")


def test_report_content_hash_changes_when_dataset_quality_hash_changes(tmp_path) -> None:
    first = write_forward_diagnostics_report(
        manager=_manager(tmp_path / "a"),
        manifest=_manifest(),
        result=_result(dataset_quality=_dataset_quality(report_hash="sha256:" + "1" * 64)),
    )
    second = write_forward_diagnostics_report(
        manager=_manager(tmp_path / "b"),
        manifest=_manifest(),
        result=_result(dataset_quality=_dataset_quality(report_hash="sha256:" + "2" * 64)),
    )

    assert first["content_hash"] != second["content_hash"]


def test_dataset_quality_fail_is_recorded_in_diagnostic_report(tmp_path) -> None:
    quality = ForwardDiagnosticsDatasetQuality(
        quality_gate_status="FAIL",
        quality_gate_reasons=("missing_candles",),
        dataset_quality_report_hash="sha256:" + "3" * 64,
        dataset_content_hash="sha256:" + "4" * 64,
    )
    report = write_forward_diagnostics_report(
        manager=_manager(tmp_path),
        manifest=_manifest(),
        result=_result(
            availability=_result().availability.__class__(
                status="degraded",
                fail_reasons=(),
                warnings=("dataset_quality_failed",),
                target_count=1,
                sample_count=1,
                feature_value_count=1,
            ),
            dataset_quality=quality,
        ),
    )

    assert report["diagnostic_status"] == "degraded"
    assert report["dataset_quality"]["quality_gate_status"] == "FAIL"
    assert report["dataset_quality"]["quality_gate_reasons"] == ["missing_candles"]


def test_dataset_quality_fail_policy_is_degraded() -> None:
    result = run_forward_diagnostics_on_snapshot(
        snapshot=_snapshot(),
        feature_names=("range_ratio",),
        horizon_steps=(1,),
        bucket_method="quantile:1",
        min_bucket_count=1,
        dataset_quality=ForwardDiagnosticsDatasetQuality(
            quality_gate_status="FAIL",
            quality_gate_reasons=("missing_candles",),
            dataset_quality_report_hash="sha256:" + "5" * 64,
            dataset_content_hash=_snapshot().content_hash(),
        ),
    )

    assert DATASET_QUALITY_FAIL_POLICY == "degraded"
    assert result.diagnostic_status == "degraded"
    assert result.dataset_quality.quality_gate_reasons == ("missing_candles",)
