from __future__ import annotations

from dataclasses import dataclass

from .dataset_snapshot import Candle, DatasetSnapshot


@dataclass(frozen=True)
class PreparedCandleArrays:
    candles: tuple[Candle, ...]
    opens: tuple[float, ...]
    highs: tuple[float, ...]
    lows: tuple[float, ...]
    closes: tuple[float, ...]
    volumes: tuple[float, ...]


def prepare_candle_arrays(dataset: DatasetSnapshot) -> PreparedCandleArrays:
    candles = tuple(dataset.candles)
    return PreparedCandleArrays(
        candles=candles,
        opens=tuple(float(item.open) for item in candles),
        highs=tuple(float(item.high) for item in candles),
        lows=tuple(float(item.low) for item in candles),
        closes=tuple(float(item.close) for item in candles),
        volumes=tuple(float(item.volume) for item in candles),
    )
