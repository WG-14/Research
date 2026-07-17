"""Clock-bounded read-only market view for strategy callbacks."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .dataset_snapshot import Candle, DatasetSnapshot, TopOfBookQuote


class FutureMarketAccessError(IndexError):
    pass


@dataclass(frozen=True, slots=True)
class CausalMarketView:
    _causal_snapshot: DatasetSnapshot
    current_index: int
    decision_boundary_ts: int

    def __post_init__(self) -> None:
        snapshot = self._causal_snapshot
        candles = snapshot.candles[: self.current_index + 1]
        quotes = tuple(
            q
            for q in snapshot.execution_top_of_book_quotes()
            if int(q.ts) <= self.decision_boundary_ts
        )
        depths = tuple(
            d
            for d in snapshot.orderbook_depth_snapshots
            if int(d.ts) <= self.decision_boundary_ts
        )
        bounded = replace(
            snapshot,
            candles=candles,
            top_of_book_quotes=snapshot.top_of_book_quotes[: self.current_index + 1],
            top_of_book_event_quotes=quotes,
            orderbook_depth_snapshots=depths,
        )
        object.__setattr__(self, "_causal_snapshot", bounded)
        object.__setattr__(self, "current_index", len(candles) - 1)

    @classmethod
    def from_dataset(
        cls, dataset: DatasetSnapshot, current_index: int, decision_boundary_ts: int
    ) -> "CausalMarketView":
        """Copy only observable rows into the strategy-visible object graph."""
        candles = dataset.candles[: current_index + 1]
        quotes = tuple(
            q
            for q in dataset.execution_top_of_book_quotes()
            if int(q.ts) <= decision_boundary_ts
        )
        depths = tuple(
            d
            for d in dataset.orderbook_depth_snapshots
            if int(d.ts) <= decision_boundary_ts
        )
        aligned = dataset.top_of_book_quotes[: current_index + 1]
        bounded = replace(
            dataset,
            candles=candles,
            top_of_book_quotes=aligned,
            top_of_book_event_quotes=quotes,
            orderbook_depth_snapshots=depths,
        )
        return cls(bounded, len(candles) - 1, decision_boundary_ts)

    @property
    def current_candle(self) -> Candle:
        return self._causal_snapshot.candles[self.current_index]

    def candle(self, index: int) -> Candle:
        if index < 0 or index > self.current_index:
            raise FutureMarketAccessError("future_candle_access_rejected")
        return self._causal_snapshot.candles[index]

    def candles(self, *, lookback: int | None = None) -> tuple[Candle, ...]:
        start = (
            0 if lookback is None else max(0, self.current_index + 1 - int(lookback))
        )
        return self._causal_snapshot.candles[start : self.current_index + 1]

    def quotes(self) -> tuple[TopOfBookQuote, ...]:
        return self._causal_snapshot.execution_top_of_book_quotes()

    def feature(self, value: object, *, available_at: int) -> object:
        if int(available_at) > self.decision_boundary_ts:
            raise FutureMarketAccessError("future_feature_access_rejected")
        return value

    def causal_snapshot(self) -> DatasetSnapshot:
        return self._causal_snapshot
