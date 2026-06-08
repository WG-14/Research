from __future__ import annotations

import pytest

from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.experiment_manifest import DateRange
from bithumb_bot.research.forward_diagnostics import (
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


def test_forward_diagnostics_fails_when_no_targets() -> None:
    with pytest.raises(ForwardDiagnosticsUnavailableError) as exc:
        run_forward_diagnostics_on_snapshot(
            snapshot=_snapshot(1),
            feature_names=("range_ratio",),
            horizon_steps=(1,),
            bucket_method="quantile:2",
            min_bucket_count=1,
        )

    assert "no_forward_targets" in exc.value.fail_reasons


def test_forward_diagnostics_fails_when_no_feature_observations() -> None:
    with pytest.raises(ForwardDiagnosticsUnavailableError) as exc:
        run_forward_diagnostics_on_snapshot(
            snapshot=_snapshot(3),
            feature_names=("rolling_return",),
            horizon_steps=(1,),
            bucket_method="quantile:2",
            min_bucket_count=1,
        )

    assert "no_feature_observations" in exc.value.fail_reasons
    assert "all_features_missing" in exc.value.fail_reasons


def test_forward_diagnostics_fails_when_horizon_exceeds_dataset() -> None:
    with pytest.raises(ForwardDiagnosticsUnavailableError) as exc:
        run_forward_diagnostics_on_snapshot(
            snapshot=_snapshot(3),
            feature_names=("range_ratio",),
            horizon_steps=(10,),
            bucket_method="quantile:2",
            min_bucket_count=1,
        )

    assert "horizon_exceeds_dataset" in exc.value.fail_reasons


def test_cli_returns_nonzero_for_empty_diagnostic_result(monkeypatch) -> None:
    import bithumb_bot.research.forward_diagnostics_cli as cli

    report_calls: list[object] = []

    def fake_load_manifest(path):
        return object()

    def fake_run_forward_diagnostics(**kwargs):
        raise ForwardDiagnosticsUnavailableError(("no_forward_targets",))

    def fake_write_forward_diagnostics_report(**kwargs):
        report_calls.append(kwargs)
        return {}

    monkeypatch.setattr(cli, "load_manifest", fake_load_manifest)
    monkeypatch.setattr(cli, "run_forward_diagnostics", fake_run_forward_diagnostics)
    monkeypatch.setattr(cli, "write_forward_diagnostics_report", fake_write_forward_diagnostics_report)

    code = cli.cmd_research_forward_diagnostics(
        manifest_path="manifest.json",
        split_name="train",
        features=("range_ratio",),
        horizons=(1,),
        bucket="quantile:2",
    )

    assert code == 1
    assert report_calls == []
