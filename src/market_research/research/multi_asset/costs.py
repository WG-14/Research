"""Deterministic cross-product execution-cost contracts.

The objects in this module are deliberately independent of any one execution
simulator.  Product adapters can retain their authoritative fill semantics while
publishing one common, hash-bound description of capacity and economic costs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import StrEnum
from typing import Protocol, runtime_checkable

from market_research.research.hashing import sha256_prefixed


_ZERO = Decimal("0")
_ONE = Decimal("1")
_BASIS_POINTS = Decimal("10000")
_CURRENCY = re.compile(r"^[A-Z][A-Z0-9]{2,11}$")


class ExecutionCostError(ValueError):
    """Raised when execution evidence is ambiguous or internally inconsistent."""


class ExecutionSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class FillDisposition(StrEnum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    UNFILLED = "UNFILLED"


def _decimal(
    value: Decimal,
    field_name: str,
    *,
    nonnegative: bool = False,
    positive: bool = False,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise ExecutionCostError(f"{field_name}_must_be_decimal")
    if not value.is_finite():
        raise ExecutionCostError(f"{field_name}_must_be_finite")
    if positive and value <= _ZERO:
        raise ExecutionCostError(f"{field_name}_must_be_positive")
    if nonnegative and value < _ZERO:
        raise ExecutionCostError(f"{field_name}_must_be_nonnegative")
    return value


def _decimal_text(value: Decimal) -> str:
    if value == _ZERO:
        return "0"
    return format(value.normalize(), "f")


def _require_id(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ExecutionCostError(f"{field_name}_invalid")


def _timestamp(value: str, field_name: str) -> datetime:
    _require_id(value, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionCostError(f"{field_name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExecutionCostError(f"{field_name}_timezone_required")
    return parsed


def _require_timestamp(value: str, field_name: str) -> None:
    _timestamp(value, field_name)


def _enum_text(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw).upper()


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """One immutable execution decision, including capacity and fill outcome."""

    execution_id: str
    instrument_id: str
    instrument_kind: str
    currency: str
    side: ExecutionSide
    requested_quantity: Decimal
    filled_quantity: Decimal
    reference_price: Decimal
    execution_price: Decimal | None
    observed_at: str
    multiplier: Decimal = Decimal("1")
    capacity_quantity: Decimal | None = None
    participation_rate: Decimal | None = None
    borrow_notional: Decimal = Decimal("0")
    financing_notional: Decimal = Decimal("0")
    fx_notional: Decimal = Decimal("0")
    option_leg_count: int = 0
    source_hashes: tuple[str, ...] = ()
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for field_name in ("execution_id", "instrument_id", "instrument_kind"):
            _require_id(getattr(self, field_name), f"execution_context.{field_name}")
        if not _CURRENCY.fullmatch(self.currency):
            raise ExecutionCostError("execution_context_currency_invalid")
        if not isinstance(self.side, ExecutionSide):
            raise ExecutionCostError("execution_context_side_invalid")
        _require_timestamp(self.observed_at, "execution_context.observed_at")
        for field_name, positive, nonnegative in (
            ("requested_quantity", True, False),
            ("filled_quantity", False, True),
            ("reference_price", True, False),
            ("multiplier", True, False),
            ("borrow_notional", False, True),
            ("financing_notional", False, True),
            ("fx_notional", False, True),
        ):
            _decimal(
                getattr(self, field_name),
                f"execution_context.{field_name}",
                positive=positive,
                nonnegative=nonnegative,
            )
        if self.filled_quantity > self.requested_quantity:
            raise ExecutionCostError("execution_context_overfilled")
        if self.execution_price is None:
            if self.filled_quantity != _ZERO:
                raise ExecutionCostError("execution_context_execution_price_required")
        else:
            _decimal(
                self.execution_price,
                "execution_context.execution_price",
                positive=True,
            )
            if self.filled_quantity == _ZERO:
                raise ExecutionCostError("execution_context_unfilled_price_forbidden")
        if self.capacity_quantity is not None:
            _decimal(
                self.capacity_quantity,
                "execution_context.capacity_quantity",
                nonnegative=True,
            )
            if self.filled_quantity > self.capacity_quantity:
                raise ExecutionCostError("execution_context_capacity_exceeded")
        if self.participation_rate is not None:
            _decimal(
                self.participation_rate,
                "execution_context.participation_rate",
                nonnegative=True,
            )
            if self.participation_rate > _ONE:
                raise ExecutionCostError("execution_context_participation_above_one")
        if (
            isinstance(self.option_leg_count, bool)
            or not isinstance(self.option_leg_count, int)
            or self.option_leg_count < 0
        ):
            raise ExecutionCostError("execution_context_option_leg_count_invalid")
        if tuple(sorted(set(self.source_hashes))) != self.source_hashes:
            raise ExecutionCostError(
                "execution_context_source_hashes_not_unique_sorted"
            )
        for source_hash in self.source_hashes:
            if not source_hash.startswith("sha256:") or len(source_hash) != 71:
                raise ExecutionCostError("execution_context_source_hash_invalid")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="execution_context"),
        )

    @property
    def disposition(self) -> FillDisposition:
        if self.filled_quantity == _ZERO:
            return FillDisposition.UNFILLED
        if self.filled_quantity == self.requested_quantity:
            return FillDisposition.FILLED
        return FillDisposition.PARTIAL

    @property
    def unfilled_quantity(self) -> Decimal:
        return self.requested_quantity - self.filled_quantity

    @property
    def gross_notional(self) -> Decimal:
        price = self.execution_price or self.reference_price
        return self.filled_quantity * price * self.multiplier

    @property
    def capacity_utilization(self) -> Decimal | None:
        if self.capacity_quantity is None or self.capacity_quantity == _ZERO:
            return None
        return self.filled_quantity / self.capacity_quantity

    def identity_payload(self) -> dict[str, object]:
        return {
            "execution_id": self.execution_id,
            "instrument_id": self.instrument_id,
            "instrument_kind": self.instrument_kind,
            "currency": self.currency,
            "side": self.side.value,
            "requested_quantity": _decimal_text(self.requested_quantity),
            "filled_quantity": _decimal_text(self.filled_quantity),
            "unfilled_quantity": _decimal_text(self.unfilled_quantity),
            "disposition": self.disposition.value,
            "reference_price": _decimal_text(self.reference_price),
            "execution_price": (
                None
                if self.execution_price is None
                else _decimal_text(self.execution_price)
            ),
            "observed_at": self.observed_at,
            "multiplier": _decimal_text(self.multiplier),
            "capacity_quantity": (
                None
                if self.capacity_quantity is None
                else _decimal_text(self.capacity_quantity)
            ),
            "participation_rate": (
                None
                if self.participation_rate is None
                else _decimal_text(self.participation_rate)
            ),
            "borrow_notional": _decimal_text(self.borrow_notional),
            "financing_notional": _decimal_text(self.financing_notional),
            "fx_notional": _decimal_text(self.fx_notional),
            "option_leg_count": self.option_leg_count,
            "source_hashes": list(self.source_hashes),
        }


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """An additive cost decomposition bound to one :class:`ExecutionContext`."""

    execution_hash: str
    currency: str
    spread: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")
    market_impact: Decimal = Decimal("0")
    participation: Decimal = Decimal("0")
    borrow: Decimal = Decimal("0")
    financing: Decimal = Decimal("0")
    fx: Decimal = Decimal("0")
    option_leg: Decimal = Decimal("0")
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not self.execution_hash.startswith("sha256:")
            or len(self.execution_hash) != 71
        ):
            raise ExecutionCostError("cost_breakdown_execution_hash_invalid")
        if not _CURRENCY.fullmatch(self.currency):
            raise ExecutionCostError("cost_breakdown_currency_invalid")
        for field_name in self.component_names():
            _decimal(
                getattr(self, field_name),
                f"cost_breakdown.{field_name}",
                nonnegative=True,
            )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="execution_cost_breakdown"),
        )

    @staticmethod
    def component_names() -> tuple[str, ...]:
        return (
            "spread",
            "commission",
            "tax",
            "market_impact",
            "participation",
            "borrow",
            "financing",
            "fx",
            "option_leg",
        )

    @property
    def total(self) -> Decimal:
        return sum(
            (getattr(self, name) for name in self.component_names()),
            start=_ZERO,
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "execution_hash": self.execution_hash,
            "currency": self.currency,
            "components": {
                name: _decimal_text(getattr(self, name))
                for name in self.component_names()
            },
            "total": _decimal_text(self.total),
        }


@runtime_checkable
class ExecutionCostModel(Protocol):
    """Structural adapter boundary for product-specific cost models."""

    def estimate(self, context: ExecutionContext) -> CostBreakdown: ...


@dataclass(frozen=True, slots=True)
class LinearExecutionCostModel:
    """Simple deterministic model suitable for reproducible capacity studies.

    Every rate is expressed in basis points.  The observed execution/reference
    gap is retained as spread cost; configured rates add explicit modeled costs.
    """

    commission_per_unit: Decimal = Decimal("0")
    minimum_commission: Decimal = Decimal("0")
    tax_bps: Decimal = Decimal("0")
    impact_bps: Decimal = Decimal("0")
    participation_bps: Decimal = Decimal("0")
    borrow_bps: Decimal = Decimal("0")
    financing_bps: Decimal = Decimal("0")
    fx_bps: Decimal = Decimal("0")
    option_leg_fee: Decimal = Decimal("0")
    tax_on_sell_only: bool = True

    def __post_init__(self) -> None:
        for field_name in (
            "commission_per_unit",
            "minimum_commission",
            "tax_bps",
            "impact_bps",
            "participation_bps",
            "borrow_bps",
            "financing_bps",
            "fx_bps",
            "option_leg_fee",
        ):
            _decimal(
                getattr(self, field_name),
                f"linear_cost_model.{field_name}",
                nonnegative=True,
            )

    def estimate(self, context: ExecutionContext) -> CostBreakdown:
        if not isinstance(context, ExecutionContext):
            raise ExecutionCostError("linear_cost_model_context_required")
        if context.disposition is FillDisposition.UNFILLED:
            return CostBreakdown(
                execution_hash=context.content_hash,
                currency=context.currency,
            )
        if context.execution_price is None:  # guarded by ExecutionContext
            raise ExecutionCostError("linear_cost_model_execution_price_required")
        quantity_scale = context.filled_quantity * context.multiplier
        notional = context.gross_notional
        spread = abs(context.execution_price - context.reference_price) * quantity_scale
        commission = max(
            self.minimum_commission,
            context.filled_quantity * self.commission_per_unit,
        )
        taxable = not self.tax_on_sell_only or context.side is ExecutionSide.SELL
        tax = notional * self.tax_bps / _BASIS_POINTS if taxable else _ZERO
        participation_rate = context.participation_rate or _ZERO
        return CostBreakdown(
            execution_hash=context.content_hash,
            currency=context.currency,
            spread=spread,
            commission=commission,
            tax=tax,
            market_impact=notional * self.impact_bps / _BASIS_POINTS,
            participation=(
                notional * self.participation_bps * participation_rate / _BASIS_POINTS
            ),
            borrow=context.borrow_notional * self.borrow_bps / _BASIS_POINTS,
            financing=(context.financing_notional * self.financing_bps / _BASIS_POINTS),
            fx=context.fx_notional * self.fx_bps / _BASIS_POINTS,
            option_leg=Decimal(context.option_leg_count) * self.option_leg_fee,
        )


@dataclass(frozen=True, slots=True)
class LiquidityImpactCalibration:
    """Point-in-time liquidity and nonlinear-impact calibration.

    Capacity is expressed in the same quantity unit as ``ExecutionContext``;
    volatility is a one-day fractional return.  The input source and policy
    hashes are retained so a capacity result cannot outlive its calibration.
    """

    calibration_id: str
    instrument_id: str
    instrument_kind: str
    currency: str
    observed_at: str
    known_at: str
    capacity_quantity: Decimal
    daily_volatility: Decimal
    half_spread_bps: Decimal
    square_root_coefficient: Decimal
    maximum_participation_rate: Decimal
    source_hashes: tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for field_name in ("calibration_id", "instrument_id", "instrument_kind"):
            _require_id(
                str(getattr(self, field_name)),
                f"liquidity_calibration.{field_name}",
            )
        if not _CURRENCY.fullmatch(self.currency):
            raise ExecutionCostError("liquidity_calibration_currency_invalid")
        observed = _timestamp(
            self.observed_at,
            "liquidity_calibration.observed_at",
        )
        known = _timestamp(self.known_at, "liquidity_calibration.known_at")
        if known < observed:
            raise ExecutionCostError("liquidity_calibration_known_before_observed")
        for field_name, positive in (
            ("capacity_quantity", True),
            ("daily_volatility", False),
            ("half_spread_bps", False),
            ("square_root_coefficient", False),
            ("maximum_participation_rate", True),
        ):
            _decimal(
                getattr(self, field_name),
                f"liquidity_calibration.{field_name}",
                positive=positive,
                nonnegative=not positive,
            )
        if self.daily_volatility > _ONE:
            raise ExecutionCostError("liquidity_calibration_volatility_above_one")
        if self.maximum_participation_rate > _ONE:
            raise ExecutionCostError(
                "liquidity_calibration_maximum_participation_above_one"
            )
        if tuple(sorted(set(self.source_hashes))) != self.source_hashes:
            raise ExecutionCostError(
                "liquidity_calibration_source_hashes_not_unique_sorted"
            )
        if not self.source_hashes:
            raise ExecutionCostError("liquidity_calibration_source_hashes_required")
        for source_hash in self.source_hashes:
            if not source_hash.startswith("sha256:") or len(source_hash) != 71:
                raise ExecutionCostError("liquidity_calibration_source_hash_invalid")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "calibration_id": self.calibration_id,
                    "instrument_id": self.instrument_id,
                    "instrument_kind": self.instrument_kind,
                    "currency": self.currency,
                    "observed_at": self.observed_at,
                    "known_at": self.known_at,
                    "capacity_quantity": _decimal_text(self.capacity_quantity),
                    "daily_volatility": _decimal_text(self.daily_volatility),
                    "half_spread_bps": _decimal_text(self.half_spread_bps),
                    "square_root_coefficient": _decimal_text(
                        self.square_root_coefficient
                    ),
                    "maximum_participation_rate": _decimal_text(
                        self.maximum_participation_rate
                    ),
                    "source_hashes": list(self.source_hashes),
                },
                label="liquidity_impact_calibration",
            ),
        )


@dataclass(frozen=True, slots=True)
class CalibratedImpactCostModel:
    """Nonlinear square-root impact with point-in-time calibration lookup."""

    calibrations: tuple[LiquidityImpactCalibration, ...]
    base_model: LinearExecutionCostModel = field(
        default_factory=LinearExecutionCostModel
    )
    model_version: str = "square_root_impact_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.calibrations, tuple) or any(
            not isinstance(item, LiquidityImpactCalibration)
            for item in self.calibrations
        ):
            raise ExecutionCostError("impact_cost_model_calibrations_invalid")
        if not isinstance(self.base_model, LinearExecutionCostModel):
            raise ExecutionCostError("impact_cost_model_base_model_invalid")
        _require_id(self.model_version, "impact_cost_model.model_version")
        ordered = tuple(
            sorted(
                self.calibrations,
                key=lambda item: (item.instrument_id, item.known_at),
            )
        )
        if ordered != self.calibrations:
            object.__setattr__(self, "calibrations", ordered)
        if not ordered:
            raise ExecutionCostError("impact_cost_model_calibrations_required")
        if len({item.instrument_id for item in ordered}) != len(ordered):
            raise ExecutionCostError("impact_cost_model_duplicate_instrument")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "model_version": self.model_version,
                    "calibration_hashes": [item.content_hash for item in ordered],
                    "base_model": {
                        field_name: _decimal_text(getattr(self.base_model, field_name))
                        if isinstance(getattr(self.base_model, field_name), Decimal)
                        else getattr(self.base_model, field_name)
                        for field_name in (
                            "commission_per_unit",
                            "minimum_commission",
                            "tax_bps",
                            "impact_bps",
                            "participation_bps",
                            "borrow_bps",
                            "financing_bps",
                            "fx_bps",
                            "option_leg_fee",
                            "tax_on_sell_only",
                        )
                    },
                },
                label="calibrated_impact_cost_model",
            ),
        )

    def estimate(self, context: ExecutionContext) -> CostBreakdown:
        calibration = self._calibration(context)
        base = self.base_model.estimate(context)
        if context.disposition is FillDisposition.UNFILLED:
            return base
        decision_at = _timestamp(context.observed_at, "execution_context.observed_at")
        known_at = _timestamp(
            calibration.known_at,
            "liquidity_calibration.known_at",
        )
        if known_at > decision_at:
            raise ExecutionCostError("impact_cost_model_future_calibration")
        participation = context.filled_quantity / calibration.capacity_quantity
        if participation > calibration.maximum_participation_rate:
            raise ExecutionCostError("impact_cost_model_participation_limit_exceeded")
        modeled_spread = (
            context.gross_notional * calibration.half_spread_bps / _BASIS_POINTS
        )
        with localcontext() as context_policy:
            context_policy.prec = 50
            context_policy.rounding = ROUND_HALF_EVEN
            modeled_impact = +(
                context.gross_notional
                * calibration.square_root_coefficient
                * calibration.daily_volatility
                * participation.sqrt()
            )
        return CostBreakdown(
            execution_hash=context.content_hash,
            currency=context.currency,
            spread=max(base.spread, modeled_spread),
            commission=base.commission,
            tax=base.tax,
            market_impact=base.market_impact + modeled_impact,
            participation=base.participation,
            borrow=base.borrow,
            financing=base.financing,
            fx=base.fx,
            option_leg=base.option_leg,
        )

    def _calibration(
        self,
        context: ExecutionContext,
    ) -> LiquidityImpactCalibration:
        matches = [
            item
            for item in self.calibrations
            if item.instrument_id == context.instrument_id
        ]
        if len(matches) != 1:
            raise ExecutionCostError(
                f"impact_cost_model_calibration_not_unique:{context.instrument_id}"
            )
        calibration = matches[0]
        if (
            calibration.instrument_kind != context.instrument_kind
            or calibration.currency != context.currency
        ):
            raise ExecutionCostError("impact_cost_model_calibration_dimension_mismatch")
        return calibration


@dataclass(frozen=True, slots=True)
class CapacitySweepPoint:
    execution_hash: str
    requested_notional: Decimal
    filled_notional: Decimal
    gross_edge: Decimal
    total_cost: Decimal
    net_edge: Decimal
    fill_ratio: Decimal
    disposition: FillDisposition
    cost_hash: str

    def __post_init__(self) -> None:
        for field_name in ("execution_hash", "cost_hash"):
            value = str(getattr(self, field_name))
            if not value.startswith("sha256:") or len(value) != 71:
                raise ExecutionCostError(f"capacity_point_{field_name}_invalid")
        for field_name in (
            "requested_notional",
            "filled_notional",
            "gross_edge",
            "total_cost",
            "fill_ratio",
        ):
            _decimal(
                getattr(self, field_name),
                f"capacity_point.{field_name}",
                nonnegative=True,
            )
        _decimal(self.net_edge, "capacity_point.net_edge")
        if self.fill_ratio > _ONE:
            raise ExecutionCostError("capacity_point_fill_ratio_above_one")
        if not isinstance(self.disposition, FillDisposition):
            raise ExecutionCostError("capacity_point_disposition_invalid")


@dataclass(frozen=True, slots=True)
class CapacityStudyResult:
    instrument_id: str
    currency: str
    gross_edge_bps: Decimal
    points: tuple[CapacitySweepPoint, ...]
    maximum_profitable_filled_notional: Decimal
    model_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.instrument_id, "capacity_study.instrument_id")
        if not _CURRENCY.fullmatch(self.currency):
            raise ExecutionCostError("capacity_study_currency_invalid")
        _decimal(
            self.gross_edge_bps,
            "capacity_study.gross_edge_bps",
            nonnegative=True,
        )
        if not self.points:
            raise ExecutionCostError("capacity_study_points_required")
        if tuple(sorted(self.points, key=lambda item: item.requested_notional)) != (
            self.points
        ):
            raise ExecutionCostError("capacity_study_points_not_sorted")
        _decimal(
            self.maximum_profitable_filled_notional,
            "capacity_study.maximum_profitable_filled_notional",
            nonnegative=True,
        )
        _require_id(self.model_hash, "capacity_study.model_hash")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "instrument_id": self.instrument_id,
                    "currency": self.currency,
                    "gross_edge_bps": _decimal_text(self.gross_edge_bps),
                    "points": [
                        {
                            "execution_hash": item.execution_hash,
                            "requested_notional": _decimal_text(
                                item.requested_notional
                            ),
                            "filled_notional": _decimal_text(item.filled_notional),
                            "gross_edge": _decimal_text(item.gross_edge),
                            "total_cost": _decimal_text(item.total_cost),
                            "net_edge": _decimal_text(item.net_edge),
                            "fill_ratio": _decimal_text(item.fill_ratio),
                            "disposition": item.disposition.value,
                            "cost_hash": item.cost_hash,
                        }
                        for item in self.points
                    ],
                    "maximum_profitable_filled_notional": _decimal_text(
                        self.maximum_profitable_filled_notional
                    ),
                    "model_hash": self.model_hash,
                },
                label="capacity_study_result",
            ),
        )


def analyze_capacity(
    contexts: tuple[ExecutionContext, ...],
    *,
    model: CalibratedImpactCostModel,
    gross_edge_bps: Decimal,
) -> CapacityStudyResult:
    """Evaluate fill, nonlinear cost, and net edge over an ordered size sweep."""

    if not isinstance(contexts, tuple) or any(
        not isinstance(item, ExecutionContext) for item in contexts
    ):
        raise ExecutionCostError("capacity_study_contexts_invalid")
    if not contexts:
        raise ExecutionCostError("capacity_study_contexts_required")
    if not isinstance(model, CalibratedImpactCostModel):
        raise ExecutionCostError("capacity_study_model_invalid")
    _decimal(gross_edge_bps, "capacity_study.gross_edge_bps", nonnegative=True)
    first = contexts[0]
    if any(
        item.instrument_id != first.instrument_id
        or item.instrument_kind != first.instrument_kind
        or item.currency != first.currency
        or item.reference_price != first.reference_price
        or item.multiplier != first.multiplier
        for item in contexts
    ):
        raise ExecutionCostError("capacity_study_context_dimensions_mismatch")
    if tuple(sorted(contexts, key=lambda item: item.requested_quantity)) != contexts:
        raise ExecutionCostError("capacity_study_contexts_not_sorted")
    points: list[CapacitySweepPoint] = []
    profitable: list[Decimal] = []
    for context in contexts:
        cost = model.estimate(context)
        requested_notional = (
            context.requested_quantity * context.reference_price * context.multiplier
        )
        filled_notional = context.gross_notional
        gross_edge = filled_notional * gross_edge_bps / _BASIS_POINTS
        net_edge = gross_edge - cost.total
        fill_ratio = context.filled_quantity / context.requested_quantity
        point = CapacitySweepPoint(
            execution_hash=context.content_hash,
            requested_notional=requested_notional,
            filled_notional=filled_notional,
            gross_edge=gross_edge,
            total_cost=cost.total,
            net_edge=net_edge,
            fill_ratio=fill_ratio,
            disposition=context.disposition,
            cost_hash=cost.content_hash,
        )
        points.append(point)
        if net_edge >= _ZERO:
            profitable.append(filled_notional)
    return CapacityStudyResult(
        instrument_id=first.instrument_id,
        currency=first.currency,
        gross_edge_bps=gross_edge_bps,
        points=tuple(points),
        maximum_profitable_filled_notional=max(profitable, default=_ZERO),
        model_hash=model.content_hash,
    )


def execution_context_from_fill(
    fill: object,
    *,
    instrument_id: str,
    instrument_kind: str,
    currency: str,
    reference_price: Decimal | None = None,
    capacity_quantity: Decimal | None = None,
    participation_rate: Decimal | None = None,
) -> ExecutionContext:
    """Adapt a futures/option-like fill without importing product modules.

    ``reference_price`` is required for an unfilled product event that does not
    itself retain a quote/reference scalar (notably ``OptionFill``).
    """

    side = ExecutionSide(_enum_text(getattr(fill, "side")))
    requested = getattr(fill, "requested_quantity", None)
    if requested is None:
        requested = Decimal(getattr(fill, "quantity"))
    filled = getattr(fill, "filled_quantity", None)
    if filled is None:
        filled = Decimal(getattr(fill, "quantity"))
    execution_price = getattr(fill, "price", None)
    if execution_price is None:
        execution_price = getattr(fill, "fill_price", None)
    effective_reference = reference_price
    if effective_reference is None:
        effective_reference = getattr(fill, "reference_price", None)
    if effective_reference is None:
        effective_reference = execution_price
    if effective_reference is None:
        raise ExecutionCostError("fill_reference_price_required")
    multiplier = getattr(fill, "multiplier", None)
    contract = getattr(fill, "contract", None)
    if multiplier is None and contract is not None:
        multiplier = getattr(contract, "multiplier")
    observed_at = getattr(fill, "filled_at")
    source_hash = getattr(fill, "content_hash")
    return ExecutionContext(
        execution_id=str(getattr(fill, "fill_id")),
        instrument_id=instrument_id,
        instrument_kind=instrument_kind,
        currency=currency,
        side=side,
        requested_quantity=requested,
        filled_quantity=filled,
        reference_price=effective_reference,
        execution_price=execution_price,
        observed_at=observed_at,
        multiplier=multiplier or Decimal("1"),
        capacity_quantity=capacity_quantity,
        participation_rate=participation_rate,
        option_leg_count=1 if instrument_kind.upper() == "OPTION" else 0,
        source_hashes=(source_hash,),
    )
