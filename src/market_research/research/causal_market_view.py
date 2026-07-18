"""Clock-bounded read-only market view for strategy callbacks."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from .dataset_snapshot import Candle, DatasetSnapshot, TopOfBookQuote
from .experiment_manifest import DateRange


class FutureMarketAccessError(IndexError):
    pass


@dataclass(frozen=True, slots=True)
class CausalMarketView:
    _causal_snapshot: DatasetSnapshot
    current_index: int
    decision_boundary_ts: int

    def __post_init__(self) -> None:
        snapshot = self._causal_snapshot
        if self.current_index < 0 or self.current_index >= len(snapshot.candles):
            raise FutureMarketAccessError("current_candle_index_out_of_range")
        candles = snapshot.candles[: self.current_index + 1]
        if any(
            candle.available_at_ms(interval=snapshot.interval)
            > int(self.decision_boundary_ts)
            for candle in candles
        ):
            raise FutureMarketAccessError("future_candle_knowledge_time_rejected")
        quotes = tuple(
            q
            for q in snapshot.execution_top_of_book_quotes()
            if q.availability_basis() == "observed_at_epoch_sec"
            and q.available_at_ms() <= self.decision_boundary_ts
        )
        depths = tuple(
            d
            for d in snapshot.orderbook_depth_snapshots
            if d.availability_basis() == "observed_at_epoch_sec"
            and d.available_at_ms() <= self.decision_boundary_ts
        )
        aligned_quotes = tuple(
            quote
            if quote is None
            or (
                quote.availability_basis() == "observed_at_epoch_sec"
                and quote.available_at_ms() <= self.decision_boundary_ts
            )
            else None
            for quote in snapshot.top_of_book_quotes[: self.current_index + 1]
        )
        first_visible_date = _utc_date(candles[0].ts)
        last_visible_date = _utc_date(candles[-1].ts)
        bounded = replace(
            snapshot,
            # Strategy callbacks receive row-local market facts, never whole-
            # split identities or provenance whose hashes/scopes can encode a
            # future suffix. The authoritative snapshot remains outside this
            # object graph for execution and evidence production.
            snapshot_id="strategy_causal_view",
            split_name="causal_visible_prefix",
            date_range=DateRange(first_visible_date, last_visible_date),
            candles=candles,
            source_uri=None,
            source_content_hash=None,
            source_schema_hash=None,
            artifact_id=None,
            artifact_content_hash=None,
            artifact_schema_hash=None,
            artifact_manifest_hash=None,
            source_provenance_hash=None,
            adapter_version=None,
            locator=None,
            options=None,
            adapter_provenance=None,
            verification=None,
            top_of_book_quotes=aligned_quotes,
            top_of_book_event_quotes=quotes,
            top_of_book_source_content_hash=None,
            top_of_book_source_schema_hash=None,
            top_of_book_adapter_provenance=None,
            orderbook_depth_snapshots=depths,
            orderbook_depth_source_content_hash=None,
            orderbook_depth_source_schema_hash=None,
            orderbook_depth_adapter_provenance=None,
        )
        object.__setattr__(self, "_causal_snapshot", bounded)
        object.__setattr__(self, "current_index", len(candles) - 1)

    @classmethod
    def from_dataset(
        cls, dataset: DatasetSnapshot, current_index: int, decision_boundary_ts: int
    ) -> "CausalMarketView":
        """Copy only observable rows into the strategy-visible object graph."""
        return cls(dataset, current_index, int(decision_boundary_ts))

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

    def current_knowledge_time_evidence(self) -> dict[str, object]:
        candle = self.current_candle
        available_at = candle.available_at_ms(interval=self._causal_snapshot.interval)
        return {
            "schema_version": 1,
            "event_time_ts": int(candle.ts),
            "available_at_ts": int(available_at),
            "decision_boundary_ts": int(self.decision_boundary_ts),
            "available_at_lte_decision": available_at <= self.decision_boundary_ts,
            "availability_policy": "ohlcv_interval_close",
            "interval": self._causal_snapshot.interval,
        }

    def causal_snapshot(self) -> DatasetSnapshot:
        return self._causal_snapshot


def _utc_date(ts_ms: int) -> str:
    return datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=UTC).date().isoformat()
