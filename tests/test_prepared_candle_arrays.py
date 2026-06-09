from __future__ import annotations

from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.experiment_manifest import DateRange
from bithumb_bot.research.prepared_candles import PreparedCandleArrays, prepare_candle_arrays


def _dataset() -> DatasetSnapshot:
    return DatasetSnapshot(
        snapshot_id="prepared_candles_unit",
        source="unit",
        market="KRW-BTC",
        interval="1m",
        split_name="validation",
        date_range=DateRange("2026-01-01", "2026-01-02"),
        candles=(
            Candle(0, 100.0, 101.0, 99.0, 100.5, 10.0),
            Candle(60_000, 101.0, 102.5, 100.5, 102.0, 12.5),
        ),
    )


def test_prepare_candle_arrays_matches_dataset_candles() -> None:
    dataset = _dataset()

    prepared = prepare_candle_arrays(dataset)

    assert isinstance(prepared, PreparedCandleArrays)
    assert prepared.candles == dataset.candles
    assert prepared.opens == (100.0, 101.0)
    assert prepared.highs == (101.0, 102.5)
    assert prepared.lows == (99.0, 100.5)
    assert prepared.closes == (100.5, 102.0)
    assert prepared.volumes == (10.0, 12.5)


def test_prepare_candle_arrays_is_immutable_tuple_based() -> None:
    prepared = prepare_candle_arrays(_dataset())

    assert isinstance(prepared.candles, tuple)
    assert isinstance(prepared.opens, tuple)
    assert isinstance(prepared.highs, tuple)
    assert isinstance(prepared.lows, tuple)
    assert isinstance(prepared.closes, tuple)
    assert isinstance(prepared.volumes, tuple)
