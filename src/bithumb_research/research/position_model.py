"""Small venue-neutral position model used by research backtests."""

from __future__ import annotations

from dataclasses import dataclass

from .hashing import sha256_prefixed


@dataclass(frozen=True, slots=True)
class ResearchPosition:
    cash: float
    asset_qty: float
    entry_price: float | None
    entry_ts: int | None
    sellable_qty: float

    @property
    def in_position(self) -> bool:
        return self.sellable_qty > 1e-12

    @property
    def flat(self) -> bool:
        return not self.in_position

    def holding_duration(self, candle_ts: int) -> float:
        if self.entry_ts is None:
            return 0.0
        return max(0.0, (int(candle_ts) - int(self.entry_ts)) / 1000.0)

    def unrealized_pnl(self, market_price: float) -> float:
        if not self.in_position or self.entry_price is None:
            return 0.0
        return (float(market_price) - float(self.entry_price)) * float(self.sellable_qty)

    def unrealized_pnl_ratio(self, market_price: float) -> float:
        if not self.in_position or self.entry_price in (None, 0.0):
            return 0.0
        return (float(market_price) - float(self.entry_price)) / float(self.entry_price)

    def as_dict(self, *, market_price: float | None = None, candle_ts: int | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            "cash": float(self.cash),
            "asset_qty": float(self.asset_qty),
            "sellable_qty": float(self.sellable_qty),
            "entry_price": self.entry_price,
            "entry_ts": self.entry_ts,
            "in_position": self.in_position,
            "flat": self.flat,
        }
        if market_price is not None:
            payload["unrealized_pnl"] = self.unrealized_pnl(float(market_price))
            payload["unrealized_pnl_ratio"] = self.unrealized_pnl_ratio(float(market_price))
        if candle_ts is not None:
            payload["holding_duration_sec"] = self.holding_duration(int(candle_ts))
        return payload

    def position_state_hash(self, *, market_price: float | None = None, candle_ts: int | None = None) -> str:
        return sha256_prefixed(self.as_dict(market_price=market_price, candle_ts=candle_ts))
