from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base import ExecutionFill, ExecutionRequest, model_params_hash


@dataclass
class FixedBpsExecutionModel:
    fee_rate: float
    slippage_bps: float

    name: str = "fixed_bps"
    version: str = "research_fixed_bps_v1"

    def params_payload(self) -> dict[str, Any]:
        return {
            "type": self.name,
            "version": self.version,
            "fee_rate": float(self.fee_rate),
            "slippage_bps": float(self.slippage_bps),
        }

    def simulate(self, request: ExecutionRequest) -> ExecutionFill:
        side = str(request.side).upper()
        slip = float(self.slippage_bps) / 10_000.0
        if side == "BUY":
            avg_fill_price = request.reference_price * (1.0 + slip)
            requested_qty = (
                (float(request.requested_notional or 0.0) * (1.0 - float(self.fee_rate))) / avg_fill_price
                if avg_fill_price > 0.0
                else 0.0
            )
            filled_qty = requested_qty
            fee = float(request.requested_notional or 0.0) * float(self.fee_rate)
        elif side == "SELL":
            avg_fill_price = request.reference_price * (1.0 - slip)
            requested_qty = float(request.requested_qty or 0.0)
            filled_qty = requested_qty
            fee = filled_qty * avg_fill_price * float(self.fee_rate)
        else:
            raise ValueError(f"unsupported execution side: {request.side}")
        return ExecutionFill(
            signal_ts=int(request.signal_ts),
            decision_ts=int(request.decision_ts),
            submit_ts_assumption=int(request.decision_ts),
            side=side,
            order_type=request.order_type,
            reference_price=float(request.reference_price),
            requested_qty=float(requested_qty),
            filled_qty=float(filled_qty),
            remaining_qty=max(0.0, float(requested_qty) - float(filled_qty)),
            avg_fill_price=float(avg_fill_price),
            fee=float(fee),
            slippage_bps=float(self.slippage_bps),
            latency_ms=0,
            fill_status="filled",
            model_name=self.name,
            model_version=self.version,
            model_params_hash=model_params_hash(self.params_payload()),
            best_bid=request.best_bid,
            best_ask=request.best_ask,
            spread_bps=request.spread_bps,
            intra_candle_policy=request.intra_candle_policy,
            base_seed=None,
            derived_seed_hash=None,
            seed_derivation_inputs=None,
        )
