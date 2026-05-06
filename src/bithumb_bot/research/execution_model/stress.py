from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from .base import ExecutionFill, ExecutionRequest, model_params_hash


@dataclass
class StressExecutionModel:
    fee_rate: float
    slippage_bps: float
    latency_ms: int = 0
    partial_fill_rate: float = 0.0
    order_failure_rate: float = 0.0
    market_order_extra_cost_bps: float = 0.0
    seed: int | None = None
    partial_fill_fraction: float = 0.5

    name: str = "stress"
    version: str = "research_stress_v1"

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def params_payload(self) -> dict[str, Any]:
        return {
            "type": self.name,
            "version": self.version,
            "fee_rate": float(self.fee_rate),
            "slippage_bps": float(self.slippage_bps),
            "latency_ms": int(self.latency_ms),
            "partial_fill_rate": float(self.partial_fill_rate),
            "order_failure_rate": float(self.order_failure_rate),
            "market_order_extra_cost_bps": float(self.market_order_extra_cost_bps),
            "seed": self.seed,
            "partial_fill_fraction": float(self.partial_fill_fraction),
        }

    def simulate(self, request: ExecutionRequest) -> ExecutionFill:
        side = str(request.side).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"unsupported execution side: {request.side}")
        total_slippage_bps = float(self.slippage_bps)
        if str(request.order_type).lower() == "market":
            total_slippage_bps += float(self.market_order_extra_cost_bps)
        slip = total_slippage_bps / 10_000.0
        if side == "BUY":
            avg_fill_price = request.reference_price * (1.0 + slip)
            requested_qty = (
                (float(request.requested_notional or 0.0) * (1.0 - float(self.fee_rate))) / avg_fill_price
                if avg_fill_price > 0.0
                else 0.0
            )
        else:
            avg_fill_price = request.reference_price * (1.0 - slip)
            requested_qty = float(request.requested_qty or 0.0)

        fill_status = "filled"
        fill_ratio = 1.0
        if self._rng.random() < float(self.order_failure_rate):
            fill_status = "failed"
            fill_ratio = 0.0
        elif self._rng.random() < float(self.partial_fill_rate):
            fill_status = "partial"
            fill_ratio = min(max(float(self.partial_fill_fraction), 0.0), 1.0)

        filled_qty = requested_qty * fill_ratio
        if side == "BUY":
            fee = float(request.requested_notional or 0.0) * float(self.fee_rate) * fill_ratio
        else:
            fee = filled_qty * avg_fill_price * float(self.fee_rate)
        return ExecutionFill(
            signal_ts=int(request.signal_ts),
            decision_ts=int(request.decision_ts),
            submit_ts_assumption=int(request.decision_ts) + int(self.latency_ms),
            side=side,
            order_type=request.order_type,
            reference_price=float(request.reference_price),
            requested_qty=float(requested_qty),
            filled_qty=float(filled_qty),
            remaining_qty=max(0.0, float(requested_qty) - float(filled_qty)),
            avg_fill_price=(float(avg_fill_price) if fill_ratio > 0.0 else None),
            fee=float(fee),
            slippage_bps=float(total_slippage_bps),
            latency_ms=int(self.latency_ms),
            fill_status=fill_status,
            model_name=self.name,
            model_version=self.version,
            model_params_hash=model_params_hash(self.params_payload()),
            best_bid=request.best_bid,
            best_ask=request.best_ask,
            spread_bps=request.spread_bps,
            intra_candle_policy=request.intra_candle_policy,
        )
