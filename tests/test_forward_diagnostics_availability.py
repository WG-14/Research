from __future__ import annotations

import pytest

from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.diagnostic_availability import DiagnosticAvailability
from bithumb_bot.research.experiment_manifest import DateRange
from bithumb_bot.research.forward_diagnostics import (
    ForwardDiagnosticsResult,
    ForwardDiagnosticsUnavailableError,
    run_forward_diagnostics_on_snapshot,
)


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


def test_availability_unavailable_when_no_forward_targets() -> None:
    with pytest.raises(ForwardDiagnosticsUnavailableError) as exc:
        run_forward_diagnostics_on_snapshot(
            snapshot=_snapshot(1),
            feature_names=("range_ratio",),
            horizon_steps=(1,),
            bucket_method="quantile:2",
            min_bucket_count=1,
        )

    assert exc.value.availability is not None
    assert exc.value.availability.status == "unavailable"
    assert "no_forward_targets" in exc.value.fail_reasons


def test_availability_unavailable_when_no_feature_observations(monkeypatch) -> None:
    import bithumb_bot.research.forward_diagnostics as diagnostics

    calls = 0

    def fake_compute_feature_bucket_metrics(**kwargs):
        nonlocal calls
        calls += 1
        return ()

    monkeypatch.setattr(diagnostics, "compute_feature_bucket_metrics", fake_compute_feature_bucket_metrics)

    with pytest.raises(ForwardDiagnosticsUnavailableError) as exc:
        run_forward_diagnostics_on_snapshot(
            snapshot=_snapshot(3),
            feature_names=("rolling_return",),
            horizon_steps=(1,),
            bucket_method="quantile:2",
            min_bucket_count=1,
        )

    assert exc.value.availability is not None
    assert exc.value.availability.status == "unavailable"
    assert "no_feature_observations" in exc.value.fail_reasons
    assert calls == 0


def test_availability_unavailable_when_horizon_exceeds_dataset() -> None:
    with pytest.raises(ForwardDiagnosticsUnavailableError) as exc:
        run_forward_diagnostics_on_snapshot(
            snapshot=_snapshot(3),
            feature_names=("range_ratio",),
            horizon_steps=(10,),
            bucket_method="quantile:2",
            min_bucket_count=1,
        )

    assert exc.value.availability is not None
    assert exc.value.availability.status == "unavailable"
    assert "horizon_exceeds_dataset" in exc.value.fail_reasons


def test_forward_diagnostics_result_cannot_be_available_with_zero_samples() -> None:
    with pytest.raises(ValueError, match="available diagnostics require positive"):
        DiagnosticAvailability(
            status="available",
            fail_reasons=(),
            warnings=(),
            target_count=1,
            sample_count=0,
            feature_value_count=0,
        )

    assert "ForwardDiagnosticsResult" in ForwardDiagnosticsResult.__name__
