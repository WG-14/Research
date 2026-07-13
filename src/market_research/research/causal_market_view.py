"""Clock-bounded read-only market view for strategy callbacks."""
from __future__ import annotations

from dataclasses import dataclass, replace

from .dataset_snapshot import Candle, DatasetSnapshot, TopOfBookQuote


class FutureMarketAccessError(IndexError):
    pass


@dataclass(frozen=True, slots=True)
class CausalMarketView:
    _dataset: DatasetSnapshot
    current_index: int
    decision_boundary_ts: int

    @property
    def current_candle(self) -> Candle:
        return self._dataset.candles[self.current_index]

    def candle(self, index: int) -> Candle:
        if index < 0 or index > self.current_index:
            raise FutureMarketAccessError("future_candle_access_rejected")
        return self._dataset.candles[index]

    def candles(self, *, lookback: int | None = None) -> tuple[Candle, ...]:
        start = 0 if lookback is None else max(0, self.current_index + 1 - int(lookback))
        return self._dataset.candles[start:self.current_index + 1]

    def quotes(self) -> tuple[TopOfBookQuote, ...]:
        return tuple(q for q in self._dataset.execution_top_of_book_quotes() if int(q.ts) <= self.decision_boundary_ts)

    def feature(self, value: object, *, available_at: int) -> object:
        if int(available_at) > self.decision_boundary_ts:
            raise FutureMarketAccessError("future_feature_access_rejected")
        return value

    def causal_snapshot(self) -> DatasetSnapshot:
        candles = self._dataset.candles[:self.current_index + 1]
        quotes = tuple(q for q in self._dataset.execution_top_of_book_quotes() if int(q.ts) <= self.decision_boundary_ts)
        depths = tuple(d for d in self._dataset.orderbook_depth_snapshots if int(d.ts) <= self.decision_boundary_ts)
        aligned = self._dataset.top_of_book_quotes[:self.current_index + 1]
        return replace(self._dataset, candles=candles, top_of_book_quotes=aligned,
                       top_of_book_event_quotes=quotes, orderbook_depth_snapshots=depths)
