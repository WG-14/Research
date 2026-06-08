from __future__ import annotations

from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.experiment_manifest import DateRange
from bithumb_bot.research.forward_diagnostics import run_forward_diagnostics_on_snapshot


def _snapshot(*, source_content_hash: str | None = "sha256:" + "a" * 64) -> DatasetSnapshot:
    candles = tuple(
        Candle(ts=index, open=100 + index, high=102 + index, low=99 + index, close=101 + index, volume=10 + index)
        for index in range(30)
    )
    return DatasetSnapshot(
        snapshot_id="snapshot1",
        source="test_source",
        market="BTC_KRW",
        interval="1m",
        split_name="train",
        date_range=DateRange("2026-01-01", "2026-01-02"),
        candles=candles,
        source_uri="sqlite:///candles",
        source_content_hash=source_content_hash,
        source_schema_hash=None,
        adapter_provenance={"adapter": "test", "version": "1"},
    )


def test_forward_diagnostics_result_carries_snapshot_content_hash() -> None:
    snapshot = _snapshot()
    result = run_forward_diagnostics_on_snapshot(
        snapshot=snapshot,
        feature_names=("rolling_return",),
        horizon_steps=(1,),
        bucket_method="quantile:2",
        min_bucket_count=1,
    )

    assert result.dataset.content_hash == snapshot.content_hash()
    assert result.dataset.snapshot_id == "snapshot1"
    assert result.dataset.market == "BTC_KRW"
    assert result.dataset.interval == "1m"
    assert result.dataset.split_name == "train"


def test_missing_source_hash_is_recorded_as_null_not_omitted() -> None:
    result = run_forward_diagnostics_on_snapshot(
        snapshot=_snapshot(source_content_hash=None),
        feature_names=("rolling_return",),
        horizon_steps=(1,),
        bucket_method="quantile:2",
        min_bucket_count=1,
    )

    payload = result.dataset.as_dict()
    assert "source_content_hash" in payload
    assert payload["source_content_hash"] is None
    assert "source_schema_hash" in payload
    assert payload["source_schema_hash"] is None
