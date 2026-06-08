from __future__ import annotations

from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.experiment_manifest import DateRange
from bithumb_bot.research.forward_diagnostics import run_forward_diagnostics_on_snapshot


def _snapshot(count: int) -> DatasetSnapshot:
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


def test_report_includes_requested_feature_horizon_coverage() -> None:
    result = run_forward_diagnostics_on_snapshot(
        snapshot=_snapshot(25),
        feature_names=("range_ratio", "sma_gap"),
        horizon_steps=(1,),
        bucket_method="quantile:1",
        min_bucket_count=1,
    )

    rows = {(row.feature_name, row.horizon_label): row for row in result.coverage}
    assert set(rows) == {("range_ratio", "1c"), ("sma_gap", "1c")}
    assert rows[("range_ratio", "1c")].status == "available"
    assert rows[("sma_gap", "1c")].status == "degraded"


def test_partial_feature_missing_is_recorded_in_coverage() -> None:
    result = run_forward_diagnostics_on_snapshot(
        snapshot=_snapshot(5),
        feature_names=("range_ratio", "sma_gap"),
        horizon_steps=(1,),
        bucket_method="quantile:1",
        min_bucket_count=1,
    )

    rows = {(row.feature_name, row.horizon_label): row for row in result.coverage}
    assert result.diagnostic_status == "degraded"
    assert rows[("range_ratio", "1c")].status == "available"
    assert rows[("sma_gap", "1c")].status == "unavailable"
    assert rows[("sma_gap", "1c")].reasons == ("feature_history_unavailable",)


def test_feature_names_cannot_silently_disappear_from_metrics() -> None:
    result = run_forward_diagnostics_on_snapshot(
        snapshot=_snapshot(5),
        feature_names=("range_ratio", "sma_gap"),
        horizon_steps=(1,),
        bucket_method="quantile:1",
        min_bucket_count=1,
    )

    metric_feature_names = {metric.feature_name for metric in result.feature_bucket_metrics}
    coverage_feature_names = {row.feature_name for row in result.coverage}
    assert "sma_gap" not in metric_feature_names
    assert "sma_gap" in coverage_feature_names
    assert set(result.feature_names) <= coverage_feature_names | metric_feature_names


def test_coverage_rows_are_included_in_report_content_hash(tmp_path) -> None:
    from tests.test_forward_diagnostics_report import _availability, _coverage, _manager, _manifest, _result
    from bithumb_bot.research.diagnostic_coverage import FeatureHorizonCoverage
    from bithumb_bot.research.forward_diagnostics_report import write_forward_diagnostics_report

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
