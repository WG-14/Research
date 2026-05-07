from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from bithumb_bot.research.hashing import sha256_prefixed


@dataclass(frozen=True)
class ExecutionRequest:
    signal_ts: int
    decision_ts: int
    side: str
    reference_price: float
    fee_rate: float
    order_type: str = "market"
    requested_qty: float | None = None
    requested_notional: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    spread_bps: float | None = None
    intra_candle_policy: str = "close_price_only_no_intracandle_path"


@dataclass(frozen=True)
class ExecutionFill:
    signal_ts: int
    decision_ts: int
    submit_ts_assumption: int
    side: str
    order_type: str
    reference_price: float
    requested_qty: float
    filled_qty: float
    remaining_qty: float
    avg_fill_price: float | None
    fee: float
    slippage_bps: float
    latency_ms: int
    fill_status: str
    model_name: str
    model_version: str
    model_params_hash: str
    best_bid: float | None = None
    best_ask: float | None = None
    spread_bps: float | None = None
    orderbook_depth_ref: str | None = None
    intra_candle_policy: str = "close_price_only_no_intracandle_path"
    base_seed: int | None = None
    derived_seed_hash: str | None = None
    seed_derivation_inputs: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal_ts": self.signal_ts,
            "decision_ts": self.decision_ts,
            "submit_ts_assumption": self.submit_ts_assumption,
            "side": self.side,
            "order_type": self.order_type,
            "reference_price": self.reference_price,
            "requested_qty": self.requested_qty,
            "filled_qty": self.filled_qty,
            "remaining_qty": self.remaining_qty,
            "avg_fill_price": self.avg_fill_price,
            "fee": self.fee,
            "slippage_bps": self.slippage_bps,
            "latency_ms": self.latency_ms,
            "fill_status": self.fill_status,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "model_params_hash": self.model_params_hash,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "spread_bps": self.spread_bps,
            "orderbook_depth_ref": self.orderbook_depth_ref,
            "intra_candle_policy": self.intra_candle_policy,
            "base_seed": self.base_seed,
            "derived_seed_hash": self.derived_seed_hash,
            "seed_derivation_inputs": self.seed_derivation_inputs,
        }


class ExecutionModel(Protocol):
    name: str
    version: str

    def params_payload(self) -> dict[str, Any]:
        ...

    def simulate(self, request: ExecutionRequest) -> ExecutionFill:
        ...


def model_params_hash(payload: dict[str, Any]) -> str:
    return sha256_prefixed(payload)
