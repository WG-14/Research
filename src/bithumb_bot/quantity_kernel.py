from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any, Mapping

from .decision_equivalence import sha256_prefixed


def _decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid quantity value: {value}") from exc
    if not parsed.is_finite():
        raise ValueError(f"invalid non-finite quantity value: {value}")
    return parsed


@dataclass(frozen=True)
class OrderRuleSnapshot:
    min_qty: float
    qty_step: float
    max_qty_decimals: int
    min_notional_krw: float
    order_type_buy: str = "price"
    order_type_sell: str = "market"
    qty_step_authority: str = "exchange"

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "OrderRuleSnapshot":
        raw_qty_step = payload.get("qty_step")
        qty_step = float(raw_qty_step or 0.0)
        qty_step_authority = str(payload.get("qty_step_authority") or "").strip()
        if not qty_step_authority:
            qty_step_authority = "exchange" if raw_qty_step not in {None, ""} and qty_step > 0.0 else "missing"
        return cls(
            min_qty=float(payload.get("min_qty") or 0.0),
            qty_step=qty_step,
            max_qty_decimals=int(payload.get("max_qty_decimals", payload.get("LIVE_ORDER_MAX_QTY_DECIMALS", 8)) or 0),
            min_notional_krw=float(payload.get("min_notional_krw") or 0.0),
            order_type_buy=str(payload.get("order_type_buy") or "price"),
            order_type_sell=str(payload.get("order_type_sell") or "market"),
            qty_step_authority=qty_step_authority,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "min_qty": float(self.min_qty),
            "qty_step": float(self.qty_step),
            "max_qty_decimals": int(self.max_qty_decimals),
            "min_notional_krw": float(self.min_notional_krw),
            "order_type_buy": self.order_type_buy,
            "order_type_sell": self.order_type_sell,
            "qty_step_authority": self.qty_step_authority,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict())


@dataclass(frozen=True)
class QuantityKernelResult:
    side: str
    requested_qty: float
    constrained_qty: float
    submitted_qty: float
    submitted_notional_krw: float
    residual_qty: float
    exchange_submit_field: str
    exchange_order_type: str
    allowed: bool
    block_reason: str
    quantity_contract_hash: str
    qty_step_authority: str = "exchange"
    residual_policy: str = "none"
    residual_reason: str = "none"

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "requested_qty": self.requested_qty,
            "constrained_qty": self.constrained_qty,
            "submitted_qty": self.submitted_qty,
            "submitted_notional_krw": self.submitted_notional_krw,
            "residual_qty": self.residual_qty,
            "exchange_submit_field": self.exchange_submit_field,
            "exchange_order_type": self.exchange_order_type,
            "allowed": bool(self.allowed),
            "block_reason": self.block_reason,
            "quantity_contract_hash": self.quantity_contract_hash,
            "qty_step_authority": self.qty_step_authority,
            "residual_policy": self.residual_policy,
            "residual_reason": self.residual_reason,
        }


def _effective_step(rules: OrderRuleSnapshot) -> Decimal:
    step = max(_decimal(rules.qty_step), Decimal("0"))
    if step > 0:
        return step
    min_qty = max(_decimal(rules.min_qty), Decimal("0"))
    if min_qty > 0:
        return min_qty
    decimals = max(0, int(rules.max_qty_decimals))
    return Decimal("1").scaleb(-decimals) if decimals else Decimal("0")


def _floor_qty(qty: object, rules: OrderRuleSnapshot) -> float:
    normalized = max(_decimal(qty), Decimal("0"))
    step = _effective_step(rules)
    if step > 0:
        normalized = (normalized / step).to_integral_value(rounding=ROUND_FLOOR) * step
    decimals = max(0, int(rules.max_qty_decimals))
    if decimals:
        normalized = normalized.quantize(Decimal("1").scaleb(-decimals), rounding=ROUND_FLOOR)
    return max(0.0, float(normalized))


def _quantize_decimals_floor(qty: object, *, max_qty_decimals: int) -> float:
    normalized = max(_decimal(qty), Decimal("0"))
    decimals = max(0, int(max_qty_decimals))
    if decimals:
        normalized = normalized.quantize(Decimal("1").scaleb(-decimals), rounding=ROUND_FLOOR)
    return max(0.0, float(normalized))


def plan_buy_notional(
    *,
    requested_notional_krw: float,
    reference_price: float,
    rules: OrderRuleSnapshot,
) -> QuantityKernelResult:
    price = float(reference_price)
    requested_notional = max(0.0, float(requested_notional_krw))
    requested_qty = requested_notional / price if price > 0.0 else 0.0
    constrained_qty = _floor_qty(requested_qty, rules)
    submitted_notional = constrained_qty * price
    allowed = bool(constrained_qty >= float(rules.min_qty) and submitted_notional >= float(rules.min_notional_krw))
    reason = "none" if allowed else "quantity_kernel_buy_below_exchange_minimum"
    contract = {
        "operation": "buy_notional",
        "requested_notional_krw": requested_notional,
        "reference_price": price,
        "rules": rules.as_dict(),
    }
    return QuantityKernelResult(
        side="BUY",
        requested_qty=float(requested_qty),
        constrained_qty=float(constrained_qty),
        submitted_qty=float(constrained_qty if allowed else 0.0),
        submitted_notional_krw=float(submitted_notional if allowed else 0.0),
        residual_qty=max(0.0, float(requested_qty) - float(constrained_qty)),
        exchange_submit_field="price",
        exchange_order_type=rules.order_type_buy,
        allowed=allowed,
        block_reason=reason,
        quantity_contract_hash=sha256_prefixed(contract),
        qty_step_authority=rules.qty_step_authority,
    )


def plan_sell_qty(
    *,
    requested_qty: float,
    reference_price: float,
    rules: OrderRuleSnapshot,
) -> QuantityKernelResult:
    price = float(reference_price)
    requested = max(0.0, float(requested_qty))
    constrained_qty = _floor_qty(requested, rules)
    submitted_notional = constrained_qty * price
    allowed = bool(constrained_qty >= float(rules.min_qty) and submitted_notional >= float(rules.min_notional_krw))
    reason = "none" if allowed else "quantity_kernel_sell_below_exchange_minimum"
    contract = {
        "operation": "sell_qty",
        "requested_qty": requested,
        "reference_price": price,
        "rules": rules.as_dict(),
    }
    return QuantityKernelResult(
        side="SELL",
        requested_qty=float(requested),
        constrained_qty=float(constrained_qty),
        submitted_qty=float(constrained_qty if allowed else 0.0),
        submitted_notional_krw=float(submitted_notional if allowed else 0.0),
        residual_qty=max(0.0, float(requested) - float(constrained_qty)),
        exchange_submit_field="volume",
        exchange_order_type=rules.order_type_sell,
        allowed=allowed,
        block_reason=reason,
        quantity_contract_hash=sha256_prefixed(contract),
        qty_step_authority=rules.qty_step_authority,
        residual_policy="exchange_step_remainder_tracked" if max(0.0, float(requested) - float(constrained_qty)) > 0 else "none",
        residual_reason="exchange_step_constraint" if max(0.0, float(requested) - float(constrained_qty)) > 0 else "none",
    )


def plan_h74_closeout_qty(
    *,
    remaining_qty: float,
    reference_price: float,
    rules: OrderRuleSnapshot,
) -> QuantityKernelResult:
    price = float(reference_price)
    requested = max(0.0, float(remaining_qty))
    if float(rules.qty_step) > 0.0 and str(rules.qty_step_authority or "").strip() == "exchange":
        constrained_qty = _floor_qty(requested, rules)
        residual = max(0.0, requested - constrained_qty)
        residual_policy = "exchange_step_residual_tracked" if residual > 0.0 else "none"
        residual_reason = "exchange_step_constraint" if residual > 0.0 else "none"
    else:
        constrained_qty = _quantize_decimals_floor(
            requested,
            max_qty_decimals=int(rules.max_qty_decimals),
        )
        residual = max(0.0, requested - constrained_qty)
        residual_policy = "decimal_precision_residual_tracked" if residual > 0.0 else "none"
        residual_reason = "max_qty_decimals_constraint" if residual > 0.0 else "none"
    submitted_notional = constrained_qty * price
    allowed = bool(constrained_qty >= float(rules.min_qty) and submitted_notional >= float(rules.min_notional_krw))
    reason = "none" if allowed else "quantity_kernel_h74_closeout_below_exchange_minimum"
    contract = {
        "operation": "h74_closeout_qty",
        "remaining_qty": requested,
        "reference_price": price,
        "rules": rules.as_dict(),
        "qty_authority": "h74_cycle_remaining",
    }
    return QuantityKernelResult(
        side="SELL",
        requested_qty=float(requested),
        constrained_qty=float(constrained_qty),
        submitted_qty=float(constrained_qty if allowed else 0.0),
        submitted_notional_krw=float(submitted_notional if allowed else 0.0),
        residual_qty=float(residual if allowed else requested),
        exchange_submit_field="volume",
        exchange_order_type=rules.order_type_sell,
        allowed=allowed,
        block_reason=reason,
        quantity_contract_hash=sha256_prefixed(contract),
        qty_step_authority=rules.qty_step_authority,
        residual_policy=residual_policy,
        residual_reason=residual_reason,
    )


__all__ = [
    "OrderRuleSnapshot",
    "QuantityKernelResult",
    "plan_buy_notional",
    "plan_h74_closeout_qty",
    "plan_sell_qty",
]
