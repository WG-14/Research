from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast

from market_research.research.hashing import sha256_prefixed
from market_research.research.immutable_contract import canonical_mutable, deep_freeze


@dataclass(frozen=True)
class ExecutionCostBreakdown:
    """Common cost evidence without double-debiting price-embedded costs.

    The current repository contract is unlevered spot research.  Fees are cash
    debits; spread and slippage are embedded in the execution price; tax,
    borrow, and rollover are explicitly not applicable to this supported scope.
    Market impact and, without two-sided quotes, spread remain unavailable
    rather than being silently reported as modeled zero.
    """

    fee_cash_debit: float
    tax_cash_debit: float
    spread_embedded: float | None
    slippage_embedded: float
    market_impact_embedded: float | None
    borrow_cash_debit: float
    rollover_cash_debit: float
    not_applicable_components: tuple[str, ...]
    unavailable_components: tuple[str, ...]

    @classmethod
    def from_fill(cls, fill: Any) -> "ExecutionCostBreakdown":
        qty = max(0.0, float(fill.filled_qty or 0.0))
        reference = float(fill.reference_price or 0.0)
        execution = float(
            fill.avg_fill_price if fill.avg_fill_price is not None else reference
        )
        slippage = abs(execution - reference) * qty
        spread: float | None = None
        unavailable: list[str] = []
        if fill.best_bid is not None and fill.best_ask is not None:
            midpoint = (float(fill.best_bid) + float(fill.best_ask)) / 2.0
            spread = (
                max(0.0, reference - midpoint) * qty
                if str(fill.side).upper() == "BUY"
                else max(0.0, midpoint - reference) * qty
            )
        else:
            unavailable.append("spread")
        market_impact: float | None = None
        if str(fill.market_impact_mode or "unavailable") == "unavailable":
            unavailable.append("market_impact")
        return cls(
            fee_cash_debit=max(0.0, float(fill.fee or 0.0)),
            tax_cash_debit=0.0,
            spread_embedded=spread,
            slippage_embedded=slippage,
            market_impact_embedded=market_impact,
            borrow_cash_debit=0.0,
            rollover_cash_debit=0.0,
            not_applicable_components=("tax", "borrow", "rollover"),
            unavailable_components=tuple(sorted(unavailable)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "accounting_semantics": "cash_debits_plus_price_embedded_costs_no_double_debit",
            "fee_cash_debit": self.fee_cash_debit,
            "tax_cash_debit": self.tax_cash_debit,
            "spread_embedded": self.spread_embedded,
            "slippage_embedded": self.slippage_embedded,
            "market_impact_embedded": self.market_impact_embedded,
            "borrow_cash_debit": self.borrow_cash_debit,
            "rollover_cash_debit": self.rollover_cash_debit,
            "cash_debit_total": (
                self.fee_cash_debit
                + self.tax_cash_debit
                + self.borrow_cash_debit
                + self.rollover_cash_debit
            ),
            "not_applicable_components": list(self.not_applicable_components),
            "unavailable_components": list(self.unavailable_components),
        }


@dataclass(frozen=True)
class ExecutionRequest:
    """Authoritative request after timing resolution; timestamps are causal.

    ``signal_candle_*`` describe the observed candle, ``decision_ts`` the
    decision, ``submit_ts_assumption`` the simulated submission, and
    ``fill_reference_ts`` the market data reference used by the model.
    """

    signal_ts: int
    decision_ts: int
    side: str
    reference_price: float
    fee_rate: float
    order_intent_ts: int = 0
    order_type: str = "market"
    requested_qty: float | None = None
    requested_notional: float | None = None
    submit_ts_assumption: int | None = None
    fill_reference_ts: int | None = None
    fill_reference_price: float | None = None
    fill_reference_source: str | None = None
    signal_candle_start_ts: int | None = None
    signal_candle_close_ts: int | None = None
    signal_reference_price: float | None = None
    signal_reference_source: str | None = None
    quote_ts: int | None = None
    quote_available_at_ts: int | None = None
    quote_availability_basis: str | None = None
    quote_age_ms: int | None = None
    quote_source: str | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    spread_bps: float | None = None
    execution_reality_level: str | None = None
    allow_same_candle_close_fill: bool | None = None
    quote_selection: str | None = None
    fill_reference_policy: str | None = None
    top_of_book_source: str | None = None
    top_of_book_is_full_depth: bool | None = None
    orderbook_depth_snapshot: Any | None = None
    orderbook_depth_ref: str | None = None
    depth_snapshot_ts: int | None = None
    depth_snapshot_available_at_ts: int | None = None
    depth_snapshot_availability_basis: str | None = None
    depth_snapshot_age_ms: int | None = None
    depth_levels_consumed: int | None = None
    depth_available: bool = False
    depth_sufficient: bool | None = None
    queue_position_mode: str = "unavailable"
    market_impact_mode: str = "unavailable"
    execution_liquidity_evidence_type: str = "top_of_book_quote_only"
    execution_realism_limitations: tuple[str, ...] = (
        "full_orderbook_depth_unavailable",
        "queue_position_unavailable",
        "market_impact_model_unavailable",
    )
    execution_reference_failure_reason: str | None = None
    latency_applied_to_reference: bool | None = None
    latency_applied_to_submit_ts: bool | None = None
    latency_applied_to_fill_reference: bool | None = None
    latency_reference_policy_warning: str | None = None
    execution_reference_target_ts: int | None = None
    execution_reference_deadline_ts: int | None = None
    execution_reference_resolution_ts: int | None = None
    execution_resolution_ts: int | None = None
    depth_reference_target_ts: int | None = None
    depth_reference_deadline_ts: int | None = None
    depth_resolution_ts: int | None = None
    feature_snapshot: dict[str, Any] | None = None
    regime_snapshot: dict[str, Any] | None = None
    entry_signal_source: str | None = None
    entry_sizing_source: str | None = None
    intra_candle_policy: str = "close_price_only_no_intracandle_path"
    run_id: str = ""
    decision_id: str = ""
    intent_id: str = ""
    request_id: str = ""

    def __post_init__(self) -> None:
        for field_name in ("feature_snapshot", "regime_snapshot"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, deep_freeze(value))
        object.__setattr__(
            self,
            "execution_realism_limitations",
            tuple(self.execution_realism_limitations),
        )
        payload = self.as_dict()
        recorded = str(payload.pop("request_id") or "")
        calculated = sha256_prefixed(payload)
        if recorded and recorded != calculated:
            raise ValueError("execution_request_id_content_mismatch")
        object.__setattr__(self, "request_id", calculated)

    def as_dict(self) -> dict[str, Any]:
        """Canonical evidence representation; excludes no authoritative field."""
        payload = cast(dict[str, Any], canonical_mutable(self.__dict__))
        depth = self.orderbook_depth_snapshot
        if depth is not None:
            payload["orderbook_depth_snapshot"] = {
                "ts": int(depth.ts),
                "available_at_ts": int(depth.available_at_ms()),
                "pair": str(depth.pair),
                "source": str(depth.source),
                "observed_at_epoch_sec": depth.observed_at_epoch_sec,
                "bids": [
                    {
                        "level_index": int(level.level_index),
                        "price": float(level.price),
                        "size": float(level.size),
                        "cumulative_size": float(level.cumulative_size),
                        "cumulative_notional": float(level.cumulative_notional),
                    }
                    for level in depth.bids
                ],
                "asks": [
                    {
                        "level_index": int(level.level_index),
                        "price": float(level.price),
                        "size": float(level.size),
                        "cumulative_size": float(level.cumulative_size),
                        "cumulative_notional": float(level.cumulative_notional),
                    }
                    for level in depth.asks
                ],
            }
        return payload


@dataclass(frozen=True)
class ExecutionFill:
    """Model result.  Portfolio application is a separate ledger operation."""

    signal_ts: int
    decision_ts: int
    submit_ts_assumption: int
    side: str
    order_type: str
    reference_price: float
    fill_reference_ts: int | None = None
    fill_reference_price: float | None = None
    fill_reference_source: str | None = None
    signal_candle_start_ts: int | None = None
    signal_candle_close_ts: int | None = None
    signal_reference_price: float | None = None
    signal_reference_source: str | None = None
    quote_ts: int | None = None
    quote_available_at_ts: int | None = None
    quote_availability_basis: str | None = None
    quote_age_ms: int | None = None
    quote_source: str | None = None
    requested_qty: float = 0.0
    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    avg_fill_price: float | None = None
    fee: float = 0.0
    slippage_bps: float = 0.0
    latency_ms: int = 0
    fill_status: str = "filled"
    model_name: str = ""
    model_version: str = ""
    model_params_hash: str = ""
    best_bid: float | None = None
    best_ask: float | None = None
    spread_bps: float | None = None
    orderbook_depth_ref: str | None = None
    requested_notional: float | None = None
    filled_notional: float | None = None
    depth_snapshot_ts: int | None = None
    depth_snapshot_available_at_ts: int | None = None
    depth_snapshot_availability_basis: str | None = None
    depth_snapshot_age_ms: int | None = None
    depth_levels_consumed: int | None = None
    depth_available: bool = False
    depth_sufficient: bool | None = None
    queue_position_mode: str = "unavailable"
    market_impact_mode: str = "unavailable"
    execution_liquidity_evidence_type: str = "top_of_book_quote_only"
    execution_realism_limitations: tuple[str, ...] = (
        "full_orderbook_depth_unavailable",
        "queue_position_unavailable",
        "market_impact_model_unavailable",
    )
    execution_reality_level: str | None = None
    allow_same_candle_close_fill: bool | None = None
    quote_selection: str | None = None
    fill_reference_policy: str | None = None
    top_of_book_source: str | None = None
    top_of_book_is_full_depth: bool | None = None
    execution_reference_failure_reason: str | None = None
    latency_applied_to_reference: bool | None = None
    latency_applied_to_submit_ts: bool | None = None
    latency_applied_to_fill_reference: bool | None = None
    latency_reference_policy_warning: str | None = None
    execution_reference_target_ts: int | None = None
    execution_reference_deadline_ts: int | None = None
    execution_reference_resolution_ts: int | None = None
    execution_resolution_ts: int | None = None
    depth_reference_target_ts: int | None = None
    depth_reference_deadline_ts: int | None = None
    depth_resolution_ts: int | None = None
    feature_snapshot: dict[str, Any] | None = None
    regime_snapshot: dict[str, Any] | None = None
    entry_signal_source: str | None = None
    entry_sizing_source: str | None = None
    intra_candle_policy: str = "close_price_only_no_intracandle_path"
    base_seed: int | None = None
    derived_seed_hash: str | None = None
    seed_derivation_inputs: dict[str, Any] | None = None
    request_id: str = ""
    fill_id: str = ""
    portfolio_effective_ts: int | None = None
    order_intent_ts: int = 0
    decision_id: str = ""
    intent_id: str = ""
    exit_rule: str | None = None
    exit_reason: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "feature_snapshot",
            "regime_snapshot",
            "seed_derivation_inputs",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, deep_freeze(value))
        object.__setattr__(
            self,
            "execution_realism_limitations",
            tuple(self.execution_realism_limitations),
        )
        if self.request_id:
            payload = self.as_dict()
            recorded = str(payload.pop("fill_id") or "")
            calculated = sha256_prefixed(payload)
            if recorded and recorded != calculated:
                raise ValueError("execution_fill_id_content_mismatch")
            object.__setattr__(self, "fill_id", calculated)

    def cost_breakdown(self) -> ExecutionCostBreakdown:
        return ExecutionCostBreakdown.from_fill(self)

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "fill_id": self.fill_id,
            "signal_ts": self.signal_ts,
            "decision_ts": self.decision_ts,
            "order_intent_ts": self.order_intent_ts,
            "submit_ts_assumption": self.submit_ts_assumption,
            "side": self.side,
            "order_type": self.order_type,
            "reference_price": self.reference_price,
            "fill_reference_ts": self.fill_reference_ts,
            "fill_reference_price": self.fill_reference_price,
            "fill_reference_source": self.fill_reference_source,
            "signal_candle_start_ts": self.signal_candle_start_ts,
            "signal_candle_close_ts": self.signal_candle_close_ts,
            "signal_reference_price": self.signal_reference_price,
            "signal_reference_source": self.signal_reference_source,
            "quote_ts": self.quote_ts,
            "quote_available_at_ts": self.quote_available_at_ts,
            "quote_availability_basis": self.quote_availability_basis,
            "quote_age_ms": self.quote_age_ms,
            "quote_source": self.quote_source,
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
            "requested_notional": self.requested_notional,
            "filled_notional": self.filled_notional,
            "depth_snapshot_ts": self.depth_snapshot_ts,
            "depth_snapshot_available_at_ts": self.depth_snapshot_available_at_ts,
            "depth_snapshot_availability_basis": self.depth_snapshot_availability_basis,
            "depth_snapshot_age_ms": self.depth_snapshot_age_ms,
            "depth_levels_consumed": self.depth_levels_consumed,
            "depth_available": self.depth_available,
            "depth_sufficient": self.depth_sufficient,
            "queue_position_mode": self.queue_position_mode,
            "market_impact_mode": self.market_impact_mode,
            "execution_liquidity_evidence_type": self.execution_liquidity_evidence_type,
            "execution_realism_limitations": list(self.execution_realism_limitations),
            "execution_reality_level": self.execution_reality_level,
            "allow_same_candle_close_fill": self.allow_same_candle_close_fill,
            "quote_selection": self.quote_selection,
            "fill_reference_policy": self.fill_reference_policy,
            "top_of_book_source": self.top_of_book_source,
            "top_of_book_is_full_depth": self.top_of_book_is_full_depth,
            "execution_reference_failure_reason": self.execution_reference_failure_reason,
            "latency_applied_to_reference": self.latency_applied_to_reference,
            "latency_applied_to_submit_ts": self.latency_applied_to_submit_ts,
            "latency_applied_to_fill_reference": self.latency_applied_to_fill_reference,
            "latency_reference_policy_warning": self.latency_reference_policy_warning,
            "execution_reference_target_ts": self.execution_reference_target_ts,
            "execution_reference_deadline_ts": self.execution_reference_deadline_ts,
            "execution_reference_resolution_ts": self.execution_reference_resolution_ts,
            "execution_resolution_ts": self.execution_resolution_ts,
            "depth_reference_target_ts": self.depth_reference_target_ts,
            "depth_reference_deadline_ts": self.depth_reference_deadline_ts,
            "depth_resolution_ts": self.depth_resolution_ts,
            "feature_snapshot": canonical_mutable(self.feature_snapshot),
            "regime_snapshot": canonical_mutable(self.regime_snapshot),
            "entry_signal_source": self.entry_signal_source,
            "entry_sizing_source": self.entry_sizing_source,
            "intra_candle_policy": self.intra_candle_policy,
            "base_seed": self.base_seed,
            "derived_seed_hash": self.derived_seed_hash,
            "seed_derivation_inputs": canonical_mutable(self.seed_derivation_inputs),
            "portfolio_effective_ts": self.portfolio_effective_ts,
            "decision_id": self.decision_id,
            "intent_id": self.intent_id,
            "exit_rule": self.exit_rule,
            "exit_reason": self.exit_reason,
            "cost_breakdown": self.cost_breakdown().as_dict(),
        }


class ExecutionModel(Protocol):
    name: str
    version: str

    def params_payload(self) -> dict[str, Any]: ...

    def simulate(self, request: ExecutionRequest) -> ExecutionFill: ...


def model_params_hash(payload: dict[str, Any]) -> str:
    return sha256_prefixed(payload)
