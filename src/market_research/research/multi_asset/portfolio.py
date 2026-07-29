"""Append-only, event-sourced accounting across spot, futures, and options.

Product engines remain authoritative for their execution and lifecycle rules.
This module supplies a Decimal-only accounting projection plus structural
adapters which consume their immutable events without importing or changing
those product modules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Mapping, Protocol, runtime_checkable

from market_research.research.hashing import sha256_prefixed
from market_research.research.multi_asset.costs import CostBreakdown


_ZERO = Decimal("0")
_ONE = Decimal("1")
_GENESIS_HASH = "sha256:" + ("0" * 64)
_CURRENCY = re.compile(r"^[A-Z][A-Z0-9]{2,11}$")


class PortfolioAccountingError(ValueError):
    """Raised when a ledger event would make the accounting ambiguous."""


class AssetClass(StrEnum):
    SPOT = "SPOT"
    FUTURE = "FUTURE"
    OPTION = "OPTION"


class PortfolioEventType(StrEnum):
    FUNDING = "FUNDING"
    SPOT_TRADE = "SPOT_TRADE"
    FUTURES_TRADE = "FUTURES_TRADE"
    FUTURES_SETTLEMENT = "FUTURES_SETTLEMENT"
    VARIATION_MARGIN = "VARIATION_MARGIN"
    OPTION_TRADE = "OPTION_TRADE"
    POSITION_MARK = "POSITION_MARK"
    OPTION_LIFECYCLE = "OPTION_LIFECYCLE"
    DIVIDEND_INCOME = "DIVIDEND_INCOME"
    SHORT_DIVIDEND_COMPENSATION = "SHORT_DIVIDEND_COMPENSATION"
    POSITION_TRANSFORMATION = "POSITION_TRANSFORMATION"
    REPLACEMENT_DELIVERY = "REPLACEMENT_DELIVERY"
    TERMINAL_SETTLEMENT = "TERMINAL_SETTLEMENT"
    COLLATERAL_TRANSFER = "COLLATERAL_TRANSFER"
    COLLATERAL_INCOME = "COLLATERAL_INCOME"
    MARGIN_REQUIREMENT = "MARGIN_REQUIREMENT"
    FX_CONVERSION = "FX_CONVERSION"
    FEE = "FEE"
    TAX = "TAX"
    BORROW_COST = "BORROW_COST"
    FINANCING_COST = "FINANCING_COST"
    EXECUTION_COST = "EXECUTION_COST"
    EXECUTION_ATTEMPT = "EXECUTION_ATTEMPT"
    CORPORATE_CASH = "CORPORATE_CASH"
    DELIVERY = "DELIVERY"
    MARGIN_CALL = "MARGIN_CALL"
    COLLATERAL_WATERFALL = "COLLATERAL_WATERFALL"
    DEFAULT = "DEFAULT"
    FORCED_LIQUIDATION = "FORCED_LIQUIDATION"
    FUNDING_FX_REVALUATION = "FUNDING_FX_REVALUATION"


_COST_EVENT_TYPES = frozenset(
    {
        PortfolioEventType.FEE,
        PortfolioEventType.TAX,
        PortfolioEventType.BORROW_COST,
        PortfolioEventType.FINANCING_COST,
        PortfolioEventType.EXECUTION_COST,
    }
)
_TRADE_EVENT_TYPES = frozenset(
    {
        PortfolioEventType.SPOT_TRADE,
        PortfolioEventType.FUTURES_TRADE,
        PortfolioEventType.OPTION_TRADE,
    }
)
_ADVANCED_AUDIT_EVENT_TYPES = frozenset(
    {
        PortfolioEventType.DELIVERY,
        PortfolioEventType.MARGIN_CALL,
        PortfolioEventType.COLLATERAL_WATERFALL,
        PortfolioEventType.DEFAULT,
        PortfolioEventType.FORCED_LIQUIDATION,
        PortfolioEventType.FUNDING_FX_REVALUATION,
    }
)


def _decimal(
    value: Decimal,
    field_name: str,
    *,
    nonnegative: bool = False,
    positive: bool = False,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise PortfolioAccountingError(f"{field_name}_must_be_decimal")
    if not value.is_finite():
        raise PortfolioAccountingError(f"{field_name}_must_be_finite")
    if positive and value <= _ZERO:
        raise PortfolioAccountingError(f"{field_name}_must_be_positive")
    if nonnegative and value < _ZERO:
        raise PortfolioAccountingError(f"{field_name}_must_be_nonnegative")
    return value


def _decimal_text(value: Decimal) -> str:
    if value == _ZERO:
        return "0"
    return format(value.normalize(), "f")


def _require_id(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise PortfolioAccountingError(f"{field_name}_invalid")


def _parse_timestamp(value: str, field_name: str) -> datetime:
    _require_id(value, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PortfolioAccountingError(f"{field_name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PortfolioAccountingError(f"{field_name}_timezone_required")
    return parsed


def _require_hash(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
    ):
        raise PortfolioAccountingError(f"{field_name}_invalid")
    try:
        int(value.removeprefix("sha256:"), 16)
    except ValueError as exc:
        raise PortfolioAccountingError(f"{field_name}_invalid") from exc


def _enum_text(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw).upper()


@dataclass(frozen=True, slots=True, order=True)
class CashDelta:
    currency: str
    amount: Decimal

    def __post_init__(self) -> None:
        if not _CURRENCY.fullmatch(self.currency):
            raise PortfolioAccountingError("cash_delta_currency_invalid")
        _decimal(self.amount, "cash_delta.amount")
        if self.amount == _ZERO:
            raise PortfolioAccountingError("cash_delta_zero_forbidden")

    def as_dict(self) -> dict[str, str]:
        return {"currency": self.currency, "amount": _decimal_text(self.amount)}


@dataclass(frozen=True, slots=True)
class ExternalFlowConversionEvidence:
    """Point-in-time conversion used to fix funding principal in base currency.

    ``fx_rate`` is base-currency units per one unit of ``currency``.  The
    evidence is embedded in the funding event hash; a later valuation rate can
    therefore change NAV, but can never rewrite contributed or withdrawn
    principal.
    """

    currency: str
    base_currency: str
    observed_at: str
    fx_rate: Decimal
    source_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not _CURRENCY.fullmatch(self.currency):
            raise PortfolioAccountingError("external_flow_conversion_currency_invalid")
        if not _CURRENCY.fullmatch(self.base_currency):
            raise PortfolioAccountingError(
                "external_flow_conversion_base_currency_invalid"
            )
        if self.currency == self.base_currency:
            raise PortfolioAccountingError(
                "external_flow_conversion_base_currency_evidence_forbidden"
            )
        _parse_timestamp(
            self.observed_at,
            "external_flow_conversion.observed_at",
        )
        _decimal(
            self.fx_rate,
            "external_flow_conversion.fx_rate",
            positive=True,
        )
        _require_hash(self.source_hash, "external_flow_conversion.source_hash")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="external_flow_conversion_evidence",
            ),
        )

    def identity_payload(self) -> dict[str, str]:
        return {
            "currency": self.currency,
            "base_currency": self.base_currency,
            "observed_at": self.observed_at,
            "fx_rate": _decimal_text(self.fx_rate),
            "source_hash": self.source_hash,
        }

    def as_dict(self) -> dict[str, str]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def _cash_amount(cash_deltas: tuple[CashDelta, ...], currency: str) -> Decimal:
    return next(
        (item.amount for item in cash_deltas if item.currency == currency),
        _ZERO,
    )


@dataclass(frozen=True, slots=True)
class PortfolioEventDraft:
    """Validated economic payload before sequence/hash-chain publication."""

    event_id: str
    event_type: PortfolioEventType
    occurred_at: str
    currency: str | None = None
    cash_deltas: tuple[CashDelta, ...] = ()
    instrument_id: str | None = None
    asset_class: AssetClass | None = None
    quantity_delta: Decimal = Decimal("0")
    price: Decimal | None = None
    multiplier: Decimal = Decimal("1")
    mark_price: Decimal | None = None
    realized_pnl: Decimal = Decimal("0")
    collateral_delta: Decimal = Decimal("0")
    margin_requirement: Decimal | None = None
    settlement_quantity: Decimal | None = None
    position_quantity_before: Decimal | None = None
    position_quantity_after: Decimal | None = None
    total_cost_basis_before: Decimal | None = None
    total_cost_basis_after: Decimal | None = None
    deliverable_asset_id: str | None = None
    deliverable_asset_class: AssetClass | None = None
    deliverable_currency: str | None = None
    deliverable_quantity_delta: Decimal = Decimal("0")
    deliverable_multiplier: Decimal | None = None
    deliverable_basis_price: Decimal | None = None
    deliverable_mark_price: Decimal | None = None
    execution_context_hash: str | None = None
    cost_breakdown: CostBreakdown | None = None
    external_flow_conversions: tuple[ExternalFlowConversionEvidence, ...] = ()
    source_hashes: tuple[str, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _normalize_and_validate_event(self)

    def identity_payload(self) -> dict[str, object]:
        return _event_identity_payload(self)


@dataclass(frozen=True, slots=True)
class PortfolioEvent:
    """One immutable event in a sequence-bound, tamper-evident stream."""

    sequence: int
    previous_hash: str
    event_id: str
    event_type: PortfolioEventType
    occurred_at: str
    currency: str | None = None
    cash_deltas: tuple[CashDelta, ...] = ()
    instrument_id: str | None = None
    asset_class: AssetClass | None = None
    quantity_delta: Decimal = Decimal("0")
    price: Decimal | None = None
    multiplier: Decimal = Decimal("1")
    mark_price: Decimal | None = None
    realized_pnl: Decimal = Decimal("0")
    collateral_delta: Decimal = Decimal("0")
    margin_requirement: Decimal | None = None
    settlement_quantity: Decimal | None = None
    position_quantity_before: Decimal | None = None
    position_quantity_after: Decimal | None = None
    total_cost_basis_before: Decimal | None = None
    total_cost_basis_after: Decimal | None = None
    deliverable_asset_id: str | None = None
    deliverable_asset_class: AssetClass | None = None
    deliverable_currency: str | None = None
    deliverable_quantity_delta: Decimal = Decimal("0")
    deliverable_multiplier: Decimal | None = None
    deliverable_basis_price: Decimal | None = None
    deliverable_mark_price: Decimal | None = None
    execution_context_hash: str | None = None
    cost_breakdown: CostBreakdown | None = None
    external_flow_conversions: tuple[ExternalFlowConversionEvidence, ...] = ()
    source_hashes: tuple[str, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or self.sequence < 0:
            raise PortfolioAccountingError("portfolio_event_sequence_invalid")
        _require_hash(self.previous_hash, "portfolio_event.previous_hash")
        _normalize_and_validate_event(self)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="portfolio_event"),
        )

    @classmethod
    def publish(
        cls,
        draft: PortfolioEventDraft,
        *,
        sequence: int,
        previous_hash: str,
    ) -> PortfolioEvent:
        if not isinstance(draft, PortfolioEventDraft):
            raise PortfolioAccountingError("portfolio_event_draft_required")
        values = {name: getattr(draft, name) for name in draft.__dataclass_fields__}
        return cls(sequence=sequence, previous_hash=previous_hash, **values)

    def identity_payload(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "previous_hash": self.previous_hash,
            **_event_identity_payload(self),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def _normalize_and_validate_event(event: PortfolioEventDraft | PortfolioEvent) -> None:
    _require_id(event.event_id, "portfolio_event.event_id")
    if not isinstance(event.event_type, PortfolioEventType):
        raise PortfolioAccountingError("portfolio_event_type_invalid")
    _parse_timestamp(event.occurred_at, "portfolio_event.occurred_at")
    if event.currency is not None and not _CURRENCY.fullmatch(event.currency):
        raise PortfolioAccountingError("portfolio_event_currency_invalid")
    normalized_cash = tuple(sorted(event.cash_deltas, key=lambda item: item.currency))
    if len({item.currency for item in normalized_cash}) != len(normalized_cash):
        raise PortfolioAccountingError("portfolio_event_cash_currency_duplicate")
    object.__setattr__(event, "cash_deltas", normalized_cash)
    if event.instrument_id is not None:
        _require_id(event.instrument_id, "portfolio_event.instrument_id")
    if event.asset_class is not None and not isinstance(event.asset_class, AssetClass):
        raise PortfolioAccountingError("portfolio_event_asset_class_invalid")
    for field_name in (
        "quantity_delta",
        "realized_pnl",
        "collateral_delta",
        "deliverable_quantity_delta",
    ):
        _decimal(getattr(event, field_name), f"portfolio_event.{field_name}")
    _decimal(event.multiplier, "portfolio_event.multiplier", positive=True)
    for field_name in (
        "price",
        "mark_price",
        "margin_requirement",
        "settlement_quantity",
        "deliverable_multiplier",
        "deliverable_basis_price",
        "deliverable_mark_price",
    ):
        value = getattr(event, field_name)
        if value is not None:
            _decimal(
                value,
                f"portfolio_event.{field_name}",
                nonnegative=field_name == "margin_requirement",
                positive=field_name != "margin_requirement",
            )
    for field_name in ("position_quantity_before", "position_quantity_after"):
        value = getattr(event, field_name)
        if value is not None:
            _decimal(value, f"portfolio_event.{field_name}")
    for field_name in ("total_cost_basis_before", "total_cost_basis_after"):
        value = getattr(event, field_name)
        if value is not None:
            _decimal(
                value,
                f"portfolio_event.{field_name}",
                nonnegative=True,
            )
    for field_name in ("deliverable_asset_id",):
        value = getattr(event, field_name)
        if value is not None:
            _require_id(value, f"portfolio_event.{field_name}")
    if event.deliverable_currency is not None and not _CURRENCY.fullmatch(
        event.deliverable_currency
    ):
        raise PortfolioAccountingError("portfolio_event_deliverable_currency_invalid")
    for field_name in ("execution_context_hash",):
        value = getattr(event, field_name)
        if value is not None:
            _require_hash(value, f"portfolio_event.{field_name}")
    normalized_sources = tuple(sorted(set(event.source_hashes)))
    if normalized_sources != event.source_hashes:
        object.__setattr__(event, "source_hashes", normalized_sources)
    for source_hash in event.source_hashes:
        _require_hash(source_hash, "portfolio_event.source_hash")
    if any(
        not isinstance(item, ExternalFlowConversionEvidence)
        for item in event.external_flow_conversions
    ):
        raise PortfolioAccountingError(
            "portfolio_event_external_flow_conversion_invalid"
        )
    normalized_conversions = tuple(
        sorted(event.external_flow_conversions, key=lambda item: item.currency)
    )
    if len({item.currency for item in normalized_conversions}) != len(
        normalized_conversions
    ):
        raise PortfolioAccountingError(
            "portfolio_event_external_flow_conversion_currency_duplicate"
        )
    object.__setattr__(
        event,
        "external_flow_conversions",
        normalized_conversions,
    )
    normalized_metadata = tuple(sorted(event.metadata))
    if len({key for key, _ in normalized_metadata}) != len(normalized_metadata):
        raise PortfolioAccountingError("portfolio_event_metadata_key_duplicate")
    for key, value in normalized_metadata:
        _require_id(key, "portfolio_event.metadata_key")
        if not isinstance(value, str):
            raise PortfolioAccountingError("portfolio_event_metadata_value_invalid")
    object.__setattr__(event, "metadata", normalized_metadata)
    _validate_event_semantics(event)


def _validate_event_semantics(event: PortfolioEventDraft | PortfolioEvent) -> None:
    if (
        event.event_type is not PortfolioEventType.FUNDING
        and event.external_flow_conversions
    ):
        raise PortfolioAccountingError(
            "external_flow_conversion_only_valid_for_funding"
        )
    if event.event_type in _TRADE_EVENT_TYPES:
        expected_class = {
            PortfolioEventType.SPOT_TRADE: AssetClass.SPOT,
            PortfolioEventType.FUTURES_TRADE: AssetClass.FUTURE,
            PortfolioEventType.OPTION_TRADE: AssetClass.OPTION,
        }[event.event_type]
        if event.asset_class is not expected_class or event.instrument_id is None:
            raise PortfolioAccountingError("portfolio_trade_instrument_invalid")
        if (
            event.currency is None
            or event.quantity_delta == _ZERO
            or event.price is None
        ):
            raise PortfolioAccountingError("portfolio_trade_economic_fields_required")
        cash = _cash_amount(event.cash_deltas, event.currency)
        if event.event_type is PortfolioEventType.FUTURES_TRADE:
            if cash != event.realized_pnl or len(event.cash_deltas) > int(
                cash != _ZERO
            ):
                raise PortfolioAccountingError("futures_trade_realized_cash_mismatch")
        else:
            expected = -(event.quantity_delta * event.price * event.multiplier)
            if cash != expected or len(event.cash_deltas) != 1:
                raise PortfolioAccountingError("portfolio_trade_cash_mismatch")
    elif event.event_type is PortfolioEventType.FUTURES_SETTLEMENT:
        if (
            event.asset_class is not AssetClass.FUTURE
            or event.instrument_id is None
            or event.currency is None
            or event.mark_price is None
            or event.settlement_quantity is None
            or event.settlement_quantity == _ZERO
        ):
            raise PortfolioAccountingError("futures_settlement_fields_required")
        if _cash_amount(event.cash_deltas, event.currency) != event.realized_pnl:
            raise PortfolioAccountingError("futures_settlement_cash_mismatch")
    elif event.event_type is PortfolioEventType.VARIATION_MARGIN:
        if (
            event.currency is None
            or len(event.cash_deltas) != 1
            or event.asset_class is not AssetClass.FUTURE
            or event.instrument_id is None
            or _cash_amount(event.cash_deltas, event.currency) != event.realized_pnl
        ):
            raise PortfolioAccountingError("variation_margin_fields_invalid")
    elif event.event_type is PortfolioEventType.POSITION_MARK:
        if (
            event.asset_class is None
            or event.instrument_id is None
            or event.currency is None
            or event.mark_price is None
            or event.cash_deltas
        ):
            raise PortfolioAccountingError("position_mark_fields_invalid")
    elif event.event_type is PortfolioEventType.COLLATERAL_TRANSFER:
        if event.currency is None or event.collateral_delta == _ZERO:
            raise PortfolioAccountingError("collateral_transfer_fields_required")
        if _cash_amount(event.cash_deltas, event.currency) != -event.collateral_delta:
            raise PortfolioAccountingError("collateral_transfer_cash_mismatch")
    elif event.event_type is PortfolioEventType.MARGIN_REQUIREMENT:
        if (
            event.asset_class is not AssetClass.FUTURE
            or event.instrument_id is None
            or event.currency is None
            or event.margin_requirement is None
            or event.cash_deltas
        ):
            raise PortfolioAccountingError("margin_requirement_fields_invalid")
    elif event.event_type is PortfolioEventType.OPTION_LIFECYCLE:
        if (
            event.asset_class is not AssetClass.OPTION
            or event.instrument_id is None
            or event.currency is None
            or event.quantity_delta == _ZERO
        ):
            raise PortfolioAccountingError("option_lifecycle_fields_required")
        has_deliverable = event.deliverable_quantity_delta != _ZERO
        delivery_fields = (
            event.deliverable_asset_id,
            event.deliverable_asset_class,
            event.deliverable_currency,
            event.deliverable_multiplier,
            event.deliverable_basis_price,
            event.deliverable_mark_price,
        )
        if has_deliverable and any(item is None for item in delivery_fields):
            raise PortfolioAccountingError("option_lifecycle_deliverable_incomplete")
        if not has_deliverable and any(item is not None for item in delivery_fields):
            raise PortfolioAccountingError("option_lifecycle_empty_deliverable_fields")
    elif event.event_type in {
        PortfolioEventType.POSITION_TRANSFORMATION,
        PortfolioEventType.REPLACEMENT_DELIVERY,
        PortfolioEventType.TERMINAL_SETTLEMENT,
    }:
        position_fields = (
            event.position_quantity_before,
            event.position_quantity_after,
            event.total_cost_basis_before,
            event.total_cost_basis_after,
        )
        if (
            event.asset_class is not AssetClass.SPOT
            or event.instrument_id is None
            or event.currency is None
            or any(item is None for item in position_fields)
        ):
            raise PortfolioAccountingError("corporate_action_position_fields_required")
        quantity_before = event.position_quantity_before
        quantity_after = event.position_quantity_after
        basis_before = event.total_cost_basis_before
        basis_after = event.total_cost_basis_after
        if (
            quantity_before is None
            or quantity_after is None
            or basis_before is None
            or basis_after is None
        ):
            raise PortfolioAccountingError("corporate_action_position_fields_required")
        if event.quantity_delta != quantity_after - quantity_before:
            raise PortfolioAccountingError(
                "corporate_action_position_quantity_delta_mismatch"
            )
        if quantity_before == _ZERO and basis_before != _ZERO:
            raise PortfolioAccountingError("corporate_action_before_basis_orphaned")
        if quantity_after == _ZERO and basis_after != _ZERO:
            raise PortfolioAccountingError("corporate_action_after_basis_orphaned")
        action_hash = dict(event.metadata).get("action_hash")
        if action_hash is None:
            raise PortfolioAccountingError("corporate_action_hash_metadata_required")
        _require_hash(action_hash, "portfolio_event.corporate_action_hash")
        if action_hash not in event.source_hashes:
            raise PortfolioAccountingError("corporate_action_hash_source_required")
        if (
            event.event_type is PortfolioEventType.POSITION_TRANSFORMATION
            and quantity_before == _ZERO
        ):
            raise PortfolioAccountingError("position_transformation_source_required")
        if (
            event.event_type is PortfolioEventType.REPLACEMENT_DELIVERY
            and quantity_after == _ZERO
        ):
            raise PortfolioAccountingError("replacement_delivery_target_required")
        if event.event_type is PortfolioEventType.TERMINAL_SETTLEMENT:
            if quantity_before == _ZERO:
                raise PortfolioAccountingError("terminal_settlement_position_required")
            if len(event.cash_deltas) > 1:
                raise PortfolioAccountingError("terminal_settlement_cash_invalid")
            if event.cash_deltas and event.cash_deltas[0].currency != event.currency:
                raise PortfolioAccountingError("terminal_settlement_currency_mismatch")
            removed_basis = basis_before - basis_after
            if removed_basis < _ZERO:
                raise PortfolioAccountingError("terminal_settlement_basis_increased")
            position_sign = _ONE if quantity_before > _ZERO else -_ONE
            expected_realized = _cash_amount(event.cash_deltas, event.currency) - (
                position_sign * removed_basis
            )
            if event.realized_pnl != expected_realized:
                raise PortfolioAccountingError(
                    "terminal_settlement_realized_pnl_mismatch"
                )
        elif event.cash_deltas or event.realized_pnl != _ZERO:
            raise PortfolioAccountingError(
                "corporate_action_transformation_must_be_non_cash"
            )
    elif event.event_type in {
        PortfolioEventType.DIVIDEND_INCOME,
        PortfolioEventType.SHORT_DIVIDEND_COMPENSATION,
        PortfolioEventType.COLLATERAL_INCOME,
    }:
        if event.currency is None or len(event.cash_deltas) != 1:
            raise PortfolioAccountingError("portfolio_income_cash_required")
        income_cash = _cash_amount(event.cash_deltas, event.currency)
        if event.event_type is PortfolioEventType.SHORT_DIVIDEND_COMPENSATION:
            if income_cash >= _ZERO:
                raise PortfolioAccountingError(
                    "short_dividend_compensation_must_reduce_cash"
                )
        elif income_cash <= _ZERO:
            raise PortfolioAccountingError("portfolio_income_must_increase_cash")
    elif event.event_type is PortfolioEventType.FUNDING:
        if not event.cash_deltas or event.instrument_id is not None:
            raise PortfolioAccountingError("funding_event_fields_invalid")
    elif event.event_type is PortfolioEventType.FX_CONVERSION:
        if len(event.cash_deltas) != 2:
            raise PortfolioAccountingError("fx_conversion_requires_two_currencies")
        amounts = [item.amount for item in event.cash_deltas]
        if not (
            any(item < _ZERO for item in amounts)
            and any(item > _ZERO for item in amounts)
        ):
            raise PortfolioAccountingError("fx_conversion_requires_opposite_cash_flows")
    elif event.event_type in _COST_EVENT_TYPES:
        if event.currency is None or len(event.cash_deltas) != 1:
            raise PortfolioAccountingError("portfolio_cost_cash_required")
        if _cash_amount(event.cash_deltas, event.currency) >= _ZERO:
            raise PortfolioAccountingError("portfolio_cost_must_reduce_cash")
        if event.cost_breakdown is not None:
            if event.cost_breakdown.currency != event.currency:
                raise PortfolioAccountingError("portfolio_cost_currency_mismatch")
            if event.execution_context_hash != event.cost_breakdown.execution_hash:
                raise PortfolioAccountingError("portfolio_cost_execution_hash_mismatch")
            component = dict(event.metadata).get("cost_component")
            if component not in event.cost_breakdown.component_names():
                raise PortfolioAccountingError("portfolio_cost_component_missing")
            expected_cost = getattr(event.cost_breakdown, component)
            if -_cash_amount(event.cash_deltas, event.currency) != expected_cost:
                raise PortfolioAccountingError("portfolio_cost_component_mismatch")
    elif event.event_type is PortfolioEventType.EXECUTION_ATTEMPT:
        if event.cash_deltas or event.quantity_delta != _ZERO:
            raise PortfolioAccountingError("execution_attempt_must_be_non_economic")
    elif event.event_type is PortfolioEventType.CORPORATE_CASH:
        if (
            event.currency is None
            or len(event.cash_deltas) != 1
            or event.quantity_delta != _ZERO
        ):
            raise PortfolioAccountingError("corporate_cash_fields_invalid")
        action_hash = dict(event.metadata).get("action_hash")
        if action_hash is None:
            raise PortfolioAccountingError("corporate_cash_action_hash_required")
        _require_hash(action_hash, "portfolio_event.corporate_cash_action_hash")
        if action_hash not in event.source_hashes:
            raise PortfolioAccountingError("corporate_cash_action_source_required")
    elif event.event_type in _ADVANCED_AUDIT_EVENT_TYPES:
        if (
            event.cash_deltas
            or event.quantity_delta != _ZERO
            or event.realized_pnl != _ZERO
            or event.collateral_delta != _ZERO
            or event.instrument_id is not None
            or event.asset_class is not None
        ):
            raise PortfolioAccountingError("advanced_audit_event_must_be_non_economic")
        metadata = dict(event.metadata)
        factory_hash = metadata.get("accounting_factory_hash")
        bundle_hash = metadata.get("economic_bundle_hash")
        if factory_hash is None or bundle_hash is None:
            raise PortfolioAccountingError(
                "advanced_audit_event_factory_receipt_required"
            )
        _require_hash(factory_hash, "portfolio_event.accounting_factory_hash")
        _require_hash(bundle_hash, "portfolio_event.economic_bundle_hash")
        if bundle_hash not in event.source_hashes:
            raise PortfolioAccountingError(
                "advanced_audit_event_bundle_source_required"
            )


def _event_identity_payload(
    event: PortfolioEventDraft | PortfolioEvent,
) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type.value,
        "occurred_at": event.occurred_at,
        "currency": event.currency,
        "cash_deltas": [item.as_dict() for item in event.cash_deltas],
        "instrument_id": event.instrument_id,
        "asset_class": None if event.asset_class is None else event.asset_class.value,
        "quantity_delta": _decimal_text(event.quantity_delta),
        "price": None if event.price is None else _decimal_text(event.price),
        "multiplier": _decimal_text(event.multiplier),
        "mark_price": (
            None if event.mark_price is None else _decimal_text(event.mark_price)
        ),
        "realized_pnl": _decimal_text(event.realized_pnl),
        "collateral_delta": _decimal_text(event.collateral_delta),
        "margin_requirement": (
            None
            if event.margin_requirement is None
            else _decimal_text(event.margin_requirement)
        ),
        "settlement_quantity": (
            None
            if event.settlement_quantity is None
            else _decimal_text(event.settlement_quantity)
        ),
        "position_quantity_before": (
            None
            if event.position_quantity_before is None
            else _decimal_text(event.position_quantity_before)
        ),
        "position_quantity_after": (
            None
            if event.position_quantity_after is None
            else _decimal_text(event.position_quantity_after)
        ),
        "total_cost_basis_before": (
            None
            if event.total_cost_basis_before is None
            else _decimal_text(event.total_cost_basis_before)
        ),
        "total_cost_basis_after": (
            None
            if event.total_cost_basis_after is None
            else _decimal_text(event.total_cost_basis_after)
        ),
        "deliverable_asset_id": event.deliverable_asset_id,
        "deliverable_asset_class": (
            None
            if event.deliverable_asset_class is None
            else event.deliverable_asset_class.value
        ),
        "deliverable_currency": event.deliverable_currency,
        "deliverable_quantity_delta": _decimal_text(event.deliverable_quantity_delta),
        "deliverable_multiplier": (
            None
            if event.deliverable_multiplier is None
            else _decimal_text(event.deliverable_multiplier)
        ),
        "deliverable_basis_price": (
            None
            if event.deliverable_basis_price is None
            else _decimal_text(event.deliverable_basis_price)
        ),
        "deliverable_mark_price": (
            None
            if event.deliverable_mark_price is None
            else _decimal_text(event.deliverable_mark_price)
        ),
        "execution_context_hash": event.execution_context_hash,
        "cost_breakdown_hash": (
            None if event.cost_breakdown is None else event.cost_breakdown.content_hash
        ),
        "external_flow_conversions": [
            item.as_dict() for item in event.external_flow_conversions
        ],
        "source_hashes": list(event.source_hashes),
        "metadata": [{"key": key, "value": value} for key, value in event.metadata],
    }


def _external_flow_base_amount(
    event: PortfolioEventDraft | PortfolioEvent,
    *,
    base_currency: str,
) -> Decimal:
    """Validate and convert a funding event at its immutable event-time rate."""

    if event.event_type is not PortfolioEventType.FUNDING:
        return _ZERO
    conversions = {item.currency: item for item in event.external_flow_conversions}
    required_nonbase = {
        item.currency for item in event.cash_deltas if item.currency != base_currency
    }
    if set(conversions) != required_nonbase:
        raise PortfolioAccountingError("funding_event_conversion_evidence_incomplete")
    event_time = _parse_timestamp(
        event.occurred_at,
        "portfolio_event.occurred_at",
    )
    total = _ZERO
    for delta in event.cash_deltas:
        if delta.currency == base_currency:
            total += delta.amount
            continue
        evidence = conversions[delta.currency]
        if evidence.base_currency != base_currency:
            raise PortfolioAccountingError(
                "funding_event_conversion_base_currency_mismatch"
            )
        if (
            _parse_timestamp(
                evidence.observed_at,
                "external_flow_conversion.observed_at",
            )
            != event_time
        ):
            raise PortfolioAccountingError("funding_event_conversion_not_event_time")
        total += delta.amount * evidence.fx_rate
    return total


@dataclass(frozen=True, slots=True, order=True)
class CurrencyBalance:
    currency: str
    amount: Decimal

    def __post_init__(self) -> None:
        if not _CURRENCY.fullmatch(self.currency):
            raise PortfolioAccountingError("currency_balance_currency_invalid")
        _decimal(self.amount, "currency_balance.amount")


@dataclass(frozen=True, slots=True, order=True)
class MarginRequirement:
    instrument_id: str
    currency: str
    amount: Decimal

    def __post_init__(self) -> None:
        _require_id(self.instrument_id, "margin_requirement.instrument_id")
        if not _CURRENCY.fullmatch(self.currency):
            raise PortfolioAccountingError("margin_requirement_currency_invalid")
        _decimal(self.amount, "margin_requirement.amount", nonnegative=True)


@dataclass(frozen=True, slots=True)
class PositionView:
    instrument_id: str
    asset_class: AssetClass
    currency: str
    quantity: Decimal
    average_price: Decimal
    mark_price: Decimal
    multiplier: Decimal

    def __post_init__(self) -> None:
        _require_id(self.instrument_id, "position.instrument_id")
        if not isinstance(self.asset_class, AssetClass):
            raise PortfolioAccountingError("position_asset_class_invalid")
        if not _CURRENCY.fullmatch(self.currency):
            raise PortfolioAccountingError("position_currency_invalid")
        _decimal(self.quantity, "position.quantity")
        if self.quantity == _ZERO:
            raise PortfolioAccountingError("position_quantity_zero")
        _decimal(self.average_price, "position.average_price", nonnegative=True)
        for name in ("mark_price", "multiplier"):
            _decimal(getattr(self, name), f"position.{name}", positive=True)

    @property
    def unrealized_pnl(self) -> Decimal:
        return (self.mark_price - self.average_price) * self.quantity * self.multiplier

    def market_value(self, mark: Decimal | None = None) -> Decimal:
        effective = self.mark_price if mark is None else mark
        _decimal(effective, "position.override_mark", positive=True)
        if self.asset_class is AssetClass.FUTURE:
            return (effective - self.average_price) * self.quantity * self.multiplier
        return effective * self.quantity * self.multiplier


@dataclass(frozen=True, slots=True)
class PortfolioValuation:
    base_currency: str
    nav: Decimal
    external_cash_flow: Decimal
    economic_pnl: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    income: Decimal
    costs: Decimal
    fx_translation_pnl: Decimal
    attributed_pnl: Decimal
    available_capital: Decimal
    reconciliation_error: Decimal

    @property
    def reconciled(self) -> bool:
        return self.reconciliation_error == _ZERO


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    ledger_id: str
    ledger_hash: str
    base_currency: str
    as_of: str | None
    cash: tuple[CurrencyBalance, ...]
    collateral: tuple[CurrencyBalance, ...]
    margins: tuple[MarginRequirement, ...]
    positions: tuple[PositionView, ...]
    external_cash_flow: tuple[CurrencyBalance, ...]
    external_cash_flow_base: Decimal
    external_flow_event_hashes: tuple[str, ...]
    realized_pnl: tuple[CurrencyBalance, ...]
    income: tuple[CurrencyBalance, ...]
    costs: tuple[CurrencyBalance, ...]
    event_count: int
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.ledger_id, "portfolio_snapshot.ledger_id")
        _require_hash(self.ledger_hash, "portfolio_snapshot.ledger_hash")
        if not _CURRENCY.fullmatch(self.base_currency):
            raise PortfolioAccountingError("portfolio_snapshot_base_currency_invalid")
        if self.as_of is not None:
            _parse_timestamp(self.as_of, "portfolio_snapshot.as_of")
        _decimal(
            self.external_cash_flow_base,
            "portfolio_snapshot.external_cash_flow_base",
        )
        for event_hash in self.external_flow_event_hashes:
            _require_hash(event_hash, "portfolio_snapshot.external_flow_event_hash")
        if len(set(self.external_flow_event_hashes)) != len(
            self.external_flow_event_hashes
        ):
            raise PortfolioAccountingError(
                "portfolio_snapshot_external_flow_event_hash_duplicate"
            )
        if (
            isinstance(self.event_count, bool)
            or not isinstance(self.event_count, int)
            or self.event_count < 0
        ):
            raise PortfolioAccountingError("portfolio_snapshot_event_count_invalid")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="portfolio_snapshot"),
        )

    def identity_payload(self) -> dict[str, object]:
        def balances(values: tuple[CurrencyBalance, ...]) -> list[dict[str, str]]:
            return [
                {"currency": item.currency, "amount": _decimal_text(item.amount)}
                for item in values
            ]

        return {
            "ledger_id": self.ledger_id,
            "ledger_hash": self.ledger_hash,
            "base_currency": self.base_currency,
            "as_of": self.as_of,
            "cash": balances(self.cash),
            "collateral": balances(self.collateral),
            "margins": [
                {
                    "instrument_id": item.instrument_id,
                    "currency": item.currency,
                    "amount": _decimal_text(item.amount),
                }
                for item in self.margins
            ],
            "positions": [
                {
                    "instrument_id": item.instrument_id,
                    "asset_class": item.asset_class.value,
                    "currency": item.currency,
                    "quantity": _decimal_text(item.quantity),
                    "average_price": _decimal_text(item.average_price),
                    "mark_price": _decimal_text(item.mark_price),
                    "multiplier": _decimal_text(item.multiplier),
                }
                for item in self.positions
            ],
            "external_cash_flow": balances(self.external_cash_flow),
            "external_cash_flow_base": _decimal_text(self.external_cash_flow_base),
            "external_flow_event_hashes": list(self.external_flow_event_hashes),
            "realized_pnl": balances(self.realized_pnl),
            "income": balances(self.income),
            "costs": balances(self.costs),
            "event_count": self.event_count,
        }

    def currency_exposures(
        self,
        *,
        marks: Mapping[str, Decimal] | None = None,
    ) -> tuple[CurrencyBalance, ...]:
        """Return local-currency NAV exposure from the immutable projection."""

        mark_overrides = marks or {}
        exposure: dict[str, Decimal] = {}
        for item in self.cash:
            _add(exposure, item.currency, item.amount)
        for item in self.collateral:
            _add(exposure, item.currency, item.amount)
        for position in self.positions:
            mark = mark_overrides.get(position.instrument_id, position.mark_price)
            _decimal(mark, "portfolio_snapshot.exposure_mark", positive=True)
            _add(
                exposure,
                position.currency,
                position.market_value(mark),
            )
        return _balances(exposure)

    @property
    def spot_positions(self) -> tuple[PositionView, ...]:
        return tuple(
            item for item in self.positions if item.asset_class is AssetClass.SPOT
        )

    @property
    def futures_positions(self) -> tuple[PositionView, ...]:
        return tuple(
            item for item in self.positions if item.asset_class is AssetClass.FUTURE
        )

    @property
    def option_positions(self) -> tuple[PositionView, ...]:
        return tuple(
            item for item in self.positions if item.asset_class is AssetClass.OPTION
        )

    def valuation(
        self,
        *,
        fx_rates: Mapping[str, Decimal],
        marks: Mapping[str, Decimal] | None = None,
        fx_translation_pnl: Decimal = Decimal("0"),
        margin_multiplier: Decimal = Decimal("1"),
        liquidity_reserve: Decimal = Decimal("0"),
    ) -> PortfolioValuation:
        """Value all balances and independently test the P&L attribution.

        ``fx_rates`` are base-currency units per one unit of each currency.
        Funding principal is already fixed in ``external_cash_flow_base`` by
        the event-time conversion evidence embedded in the ledger.  These
        current rates therefore value assets only; they never revalue external
        capital contributions or withdrawals.
        ``fx_translation_pnl`` must come from a separately evidenced FX
        attribution path, such as opening and closing currency exposures and
        their point-in-time rates.  The ledger never manufactures an FX value
        from the reconciliation residual, so missing attribution remains
        visible in ``reconciliation_error``.
        """

        _decimal(fx_translation_pnl, "valuation.fx_translation_pnl")
        _decimal(margin_multiplier, "valuation.margin_multiplier", positive=True)
        _decimal(liquidity_reserve, "valuation.liquidity_reserve", nonnegative=True)
        mark_overrides = marks or {}
        rates = dict(fx_rates)
        rates.setdefault(self.base_currency, _ONE)
        for currency, rate in rates.items():
            if not _CURRENCY.fullmatch(currency):
                raise PortfolioAccountingError("valuation_fx_currency_invalid")
            _decimal(rate, "valuation.fx_rate", positive=True)

        def convert(currency: str, amount: Decimal) -> Decimal:
            try:
                rate = rates[currency]
            except KeyError as exc:
                raise PortfolioAccountingError(
                    f"valuation_fx_rate_missing:{currency}"
                ) from exc
            return amount * rate

        cash_value = sum(
            (convert(item.currency, item.amount) for item in self.cash),
            start=_ZERO,
        )
        collateral_value = sum(
            (convert(item.currency, item.amount) for item in self.collateral),
            start=_ZERO,
        )
        position_value = _ZERO
        unrealized = _ZERO
        for position in self.positions:
            mark = mark_overrides.get(position.instrument_id, position.mark_price)
            _decimal(mark, "valuation.position_mark", positive=True)
            position_value += convert(
                position.currency,
                position.market_value(mark),
            )
            unrealized += convert(
                position.currency,
                (mark - position.average_price)
                * position.quantity
                * position.multiplier,
            )
        nav = cash_value + collateral_value + position_value
        external = self.external_cash_flow_base
        realized = sum(
            (convert(item.currency, item.amount) for item in self.realized_pnl),
            start=_ZERO,
        )
        income = sum(
            (convert(item.currency, item.amount) for item in self.income),
            start=_ZERO,
        )
        costs = sum(
            (convert(item.currency, item.amount) for item in self.costs),
            start=_ZERO,
        )
        economic_pnl = nav - external
        pre_fx_attribution = realized + unrealized + income - costs
        attributed = pre_fx_attribution + fx_translation_pnl
        margin_value = sum(
            (convert(item.currency, item.amount) for item in self.margins),
            start=_ZERO,
        )
        available = (
            cash_value
            + collateral_value
            - (margin_value * margin_multiplier)
            - liquidity_reserve
        )
        return PortfolioValuation(
            base_currency=self.base_currency,
            nav=nav,
            external_cash_flow=external,
            economic_pnl=economic_pnl,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            income=income,
            costs=costs,
            fx_translation_pnl=fx_translation_pnl,
            attributed_pnl=attributed,
            available_capital=available,
            reconciliation_error=economic_pnl - attributed,
        )


@dataclass(slots=True)
class _PositionAccumulator:
    asset_class: AssetClass
    currency: str
    quantity: Decimal
    average_price: Decimal
    mark_price: Decimal
    multiplier: Decimal

    def trade(self, quantity_delta: Decimal, price: Decimal) -> Decimal:
        old_quantity = self.quantity
        if old_quantity == _ZERO or old_quantity * quantity_delta > _ZERO:
            old_notional = abs(old_quantity) * self.average_price
            added_notional = abs(quantity_delta) * price
            self.quantity = old_quantity + quantity_delta
            self.average_price = (old_notional + added_notional) / abs(self.quantity)
            self.mark_price = price
            return _ZERO
        close_quantity = min(abs(old_quantity), abs(quantity_delta))
        realized = (
            close_quantity
            * (price - self.average_price)
            * (_ONE if old_quantity > _ZERO else -_ONE)
            * self.multiplier
        )
        new_quantity = old_quantity + quantity_delta
        self.quantity = new_quantity
        self.mark_price = price
        if new_quantity == _ZERO:
            self.average_price = price
        elif old_quantity * new_quantity < _ZERO:
            self.average_price = price
        return realized


def _add(target: dict[str, Decimal], currency: str, amount: Decimal) -> None:
    target[currency] = target.get(currency, _ZERO) + amount


def _balances(values: Mapping[str, Decimal]) -> tuple[CurrencyBalance, ...]:
    return tuple(
        CurrencyBalance(currency=currency, amount=amount)
        for currency, amount in sorted(values.items())
        if amount != _ZERO
    )


@dataclass(frozen=True, slots=True)
class UnifiedPortfolioLedger:
    """Immutable append-only stream; publishing returns a new ledger value."""

    ledger_id: str
    base_currency: str
    events: tuple[PortfolioEvent, ...] = ()
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.ledger_id, "portfolio_ledger.ledger_id")
        if not _CURRENCY.fullmatch(self.base_currency):
            raise PortfolioAccountingError("portfolio_ledger_base_currency_invalid")
        self.verify_integrity()
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="portfolio_ledger"),
        )

    @classmethod
    def open(cls, *, ledger_id: str, base_currency: str) -> UnifiedPortfolioLedger:
        return cls(ledger_id=ledger_id, base_currency=base_currency)

    @property
    def head_hash(self) -> str:
        return self.events[-1].content_hash if self.events else _GENESIS_HASH

    def publish(self, draft: PortfolioEventDraft) -> UnifiedPortfolioLedger:
        _external_flow_base_amount(draft, base_currency=self.base_currency)
        if self.events and _parse_timestamp(
            draft.occurred_at, "portfolio_event.occurred_at"
        ) < _parse_timestamp(
            self.events[-1].occurred_at, "portfolio_event.occurred_at"
        ):
            raise PortfolioAccountingError("portfolio_event_time_regression")
        if any(item.event_id == draft.event_id for item in self.events):
            raise PortfolioAccountingError("portfolio_event_id_duplicate")
        event = PortfolioEvent.publish(
            draft,
            sequence=len(self.events),
            previous_hash=self.head_hash,
        )
        return UnifiedPortfolioLedger(
            ledger_id=self.ledger_id,
            base_currency=self.base_currency,
            events=(*self.events, event),
        )

    def publish_many(
        self, drafts: tuple[PortfolioEventDraft, ...]
    ) -> UnifiedPortfolioLedger:
        ledger = self
        for draft in drafts:
            ledger = ledger.publish(draft)
        return ledger

    def verify_integrity(self) -> None:
        previous_hash = _GENESIS_HASH
        previous_time: datetime | None = None
        event_ids: set[str] = set()
        for expected_sequence, event in enumerate(self.events):
            if not isinstance(event, PortfolioEvent):
                raise PortfolioAccountingError("portfolio_ledger_event_invalid")
            if event.sequence != expected_sequence:
                raise PortfolioAccountingError("portfolio_ledger_sequence_gap")
            if event.previous_hash != previous_hash:
                raise PortfolioAccountingError("portfolio_ledger_hash_chain_broken")
            expected_hash = sha256_prefixed(
                event.identity_payload(), label="portfolio_event"
            )
            if event.content_hash != expected_hash:
                raise PortfolioAccountingError("portfolio_ledger_event_hash_mismatch")
            if event.event_id in event_ids:
                raise PortfolioAccountingError("portfolio_event_id_duplicate")
            event_ids.add(event.event_id)
            _external_flow_base_amount(event, base_currency=self.base_currency)
            event_time = _parse_timestamp(
                event.occurred_at, "portfolio_event.occurred_at"
            )
            if previous_time is not None and event_time < previous_time:
                raise PortfolioAccountingError("portfolio_event_time_regression")
            previous_time = event_time
            previous_hash = event.content_hash

    def identity_payload(self) -> dict[str, object]:
        return {
            "ledger_id": self.ledger_id,
            "base_currency": self.base_currency,
            "event_hashes": [item.content_hash for item in self.events],
        }

    def replay(self) -> PortfolioSnapshot:
        self.verify_integrity()
        cash: dict[str, Decimal] = {}
        collateral: dict[str, Decimal] = {}
        external: dict[str, Decimal] = {}
        external_base = _ZERO
        external_flow_event_hashes: list[str] = []
        realized: dict[str, Decimal] = {}
        income: dict[str, Decimal] = {}
        costs: dict[str, Decimal] = {}
        margins: dict[tuple[str, str], Decimal] = {}
        positions: dict[tuple[AssetClass, str], _PositionAccumulator] = {}
        pending_corporate_value: dict[tuple[str, str], Decimal] = {}

        def position_for(event: PortfolioEvent) -> _PositionAccumulator:
            if (
                event.asset_class is None
                or event.instrument_id is None
                or event.currency is None
                or event.price is None
            ):
                raise PortfolioAccountingError("portfolio_trade_fields_missing")
            key = (event.asset_class, event.instrument_id)
            current = positions.get(key)
            if current is None:
                current = _PositionAccumulator(
                    asset_class=event.asset_class,
                    currency=event.currency,
                    quantity=_ZERO,
                    average_price=event.price,
                    mark_price=event.price,
                    multiplier=event.multiplier,
                )
                positions[key] = current
            elif (
                current.currency != event.currency
                or current.multiplier != event.multiplier
            ):
                raise PortfolioAccountingError("portfolio_position_contract_changed")
            return current

        for event in self.events:
            for delta in event.cash_deltas:
                _add(cash, delta.currency, delta.amount)
            if event.event_type is PortfolioEventType.FUNDING:
                for delta in event.cash_deltas:
                    _add(external, delta.currency, delta.amount)
                external_base += _external_flow_base_amount(
                    event,
                    base_currency=self.base_currency,
                )
                external_flow_event_hashes.append(event.content_hash)
            elif event.event_type in _COST_EVENT_TYPES:
                if event.currency is None:
                    raise PortfolioAccountingError("portfolio_cost_currency_missing")
                _add(
                    costs,
                    event.currency,
                    -_cash_amount(event.cash_deltas, event.currency),
                )
            elif event.event_type in {
                PortfolioEventType.SPOT_TRADE,
                PortfolioEventType.OPTION_TRADE,
            }:
                position = position_for(event)
                trade_price = event.price
                if trade_price is None:
                    raise PortfolioAccountingError("portfolio_trade_price_missing")
                trade_realized = position.trade(event.quantity_delta, trade_price)
                if trade_realized != _ZERO and event.currency is not None:
                    _add(realized, event.currency, trade_realized)
                if position.quantity == _ZERO:
                    positions.pop((position.asset_class, event.instrument_id or ""))
            elif event.event_type is PortfolioEventType.FUTURES_TRADE:
                position = position_for(event)
                trade_price = event.price
                if trade_price is None:
                    raise PortfolioAccountingError("portfolio_trade_price_missing")
                position.trade(event.quantity_delta, trade_price)
                if event.realized_pnl != _ZERO and event.currency is not None:
                    _add(realized, event.currency, event.realized_pnl)
                if position.quantity == _ZERO:
                    positions.pop((position.asset_class, event.instrument_id or ""))
            elif event.event_type is PortfolioEventType.POSITION_MARK:
                if event.asset_class is None or event.instrument_id is None:
                    raise PortfolioAccountingError("position_mark_fields_missing")
                mark_key = (event.asset_class, event.instrument_id)
                marked_position = positions.get(mark_key)
                if marked_position is None or event.mark_price is None:
                    raise PortfolioAccountingError("position_mark_without_position")
                marked_position.mark_price = event.mark_price
            elif event.event_type is PortfolioEventType.FUTURES_SETTLEMENT:
                if event.instrument_id is None:
                    raise PortfolioAccountingError("futures_settlement_id_missing")
                futures_key = (AssetClass.FUTURE, event.instrument_id)
                futures_position = positions.get(futures_key)
                if futures_position is None or event.mark_price is None:
                    raise PortfolioAccountingError(
                        "futures_settlement_without_position"
                    )
                if futures_position.quantity != event.settlement_quantity:
                    raise PortfolioAccountingError(
                        "futures_settlement_quantity_mismatch"
                    )
                futures_position.average_price = event.mark_price
                futures_position.mark_price = event.mark_price
                if event.currency is not None:
                    _add(realized, event.currency, event.realized_pnl)
            elif event.event_type is PortfolioEventType.VARIATION_MARGIN:
                if event.currency is None:
                    raise PortfolioAccountingError("variation_margin_currency_missing")
                _add(realized, event.currency, event.realized_pnl)
            elif event.event_type in {
                PortfolioEventType.DIVIDEND_INCOME,
                PortfolioEventType.SHORT_DIVIDEND_COMPENSATION,
                PortfolioEventType.COLLATERAL_INCOME,
            }:
                if event.currency is None:
                    raise PortfolioAccountingError("portfolio_income_currency_missing")
                _add(
                    income,
                    event.currency,
                    _cash_amount(event.cash_deltas, event.currency),
                )
            elif event.event_type is PortfolioEventType.CORPORATE_CASH:
                if event.currency is None:
                    raise PortfolioAccountingError("corporate_cash_currency_missing")
                if event.realized_pnl != _ZERO:
                    _add(realized, event.currency, event.realized_pnl)
            elif event.event_type in {
                PortfolioEventType.POSITION_TRANSFORMATION,
                PortfolioEventType.REPLACEMENT_DELIVERY,
                PortfolioEventType.TERMINAL_SETTLEMENT,
            }:
                corporate_realized = self._apply_corporate_position_event(
                    event,
                    positions,
                    pending_corporate_value,
                )
                if corporate_realized != _ZERO and event.currency is not None:
                    _add(realized, event.currency, corporate_realized)
            elif event.event_type is PortfolioEventType.COLLATERAL_TRANSFER:
                if event.currency is None:
                    raise PortfolioAccountingError("collateral_currency_missing")
                _add(collateral, event.currency, event.collateral_delta)
                if collateral[event.currency] < _ZERO:
                    raise PortfolioAccountingError("collateral_balance_negative")
            elif event.event_type is PortfolioEventType.MARGIN_REQUIREMENT:
                if (
                    event.instrument_id is None
                    or event.currency is None
                    or event.margin_requirement is None
                ):
                    raise PortfolioAccountingError("margin_fields_missing")
                margins[(event.instrument_id, event.currency)] = (
                    event.margin_requirement
                )
            elif event.event_type is PortfolioEventType.OPTION_LIFECYCLE:
                if event.instrument_id is None:
                    raise PortfolioAccountingError("option_lifecycle_id_missing")
                option_key = (AssetClass.OPTION, event.instrument_id)
                option = positions.get(option_key)
                if option is None or event.currency is None:
                    raise PortfolioAccountingError("option_lifecycle_without_position")
                if option.quantity * event.quantity_delta >= _ZERO:
                    raise PortfolioAccountingError("option_lifecycle_does_not_close")
                if abs(event.quantity_delta) > abs(option.quantity):
                    raise PortfolioAccountingError("option_lifecycle_overclose")
                close_quantity = abs(event.quantity_delta)
                allocated_premium = (
                    -(_ONE if option.quantity > _ZERO else -_ONE)
                    * close_quantity
                    * option.average_price
                    * option.multiplier
                )
                lifecycle_realized = allocated_premium
                if event.deliverable_quantity_delta == _ZERO:
                    lifecycle_realized += _cash_amount(
                        event.cash_deltas, event.currency
                    )
                option.trade(event.quantity_delta, option.mark_price)
                _add(realized, event.currency, lifecycle_realized)
                if option.quantity == _ZERO:
                    positions.pop(option_key)
                self._apply_deliverable(event, positions)

        unresolved_transfers = {
            key: value
            for key, value in pending_corporate_value.items()
            if value != _ZERO
        }
        if unresolved_transfers:
            raise PortfolioAccountingError("corporate_action_replacement_incomplete")

        position_views = tuple(
            PositionView(
                instrument_id=instrument_id,
                asset_class=asset_class,
                currency=position.currency,
                quantity=position.quantity,
                average_price=position.average_price,
                mark_price=position.mark_price,
                multiplier=position.multiplier,
            )
            for (asset_class, instrument_id), position in sorted(
                positions.items(), key=lambda item: (item[0][0].value, item[0][1])
            )
            if position.quantity != _ZERO
        )
        margin_views = tuple(
            MarginRequirement(
                instrument_id=instrument_id,
                currency=currency,
                amount=amount,
            )
            for (instrument_id, currency), amount in sorted(margins.items())
            if amount != _ZERO
        )
        return PortfolioSnapshot(
            ledger_id=self.ledger_id,
            ledger_hash=self.content_hash,
            base_currency=self.base_currency,
            as_of=self.events[-1].occurred_at if self.events else None,
            cash=_balances(cash),
            collateral=_balances(collateral),
            margins=margin_views,
            positions=position_views,
            external_cash_flow=_balances(external),
            external_cash_flow_base=external_base,
            external_flow_event_hashes=tuple(external_flow_event_hashes),
            realized_pnl=_balances(realized),
            income=_balances(income),
            costs=_balances(costs),
            event_count=len(self.events),
        )

    @staticmethod
    def _apply_corporate_position_event(
        event: PortfolioEvent,
        positions: dict[tuple[AssetClass, str], _PositionAccumulator],
        pending_values: dict[tuple[str, str], Decimal],
    ) -> Decimal:
        if (
            event.instrument_id is None
            or event.currency is None
            or event.position_quantity_before is None
            or event.position_quantity_after is None
            or event.total_cost_basis_before is None
            or event.total_cost_basis_after is None
        ):
            raise PortfolioAccountingError("corporate_action_position_fields_missing")
        metadata = dict(event.metadata)
        action_hash = metadata.get("action_hash")
        if action_hash is None:
            raise PortfolioAccountingError("corporate_action_hash_missing")
        key = (AssetClass.SPOT, event.instrument_id)
        current = positions.get(key)
        before_quantity = event.position_quantity_before
        after_quantity = event.position_quantity_after
        before_basis = event.total_cost_basis_before
        after_basis = event.total_cost_basis_after
        if before_quantity == _ZERO:
            if current is not None:
                raise PortfolioAccountingError(
                    "corporate_action_unexpected_existing_position"
                )
            current_value = _ZERO
        else:
            if current is None:
                raise PortfolioAccountingError("corporate_action_position_missing")
            current_basis = (
                abs(current.quantity) * current.average_price * current.multiplier
            )
            if (
                current.quantity != before_quantity
                or current_basis != before_basis
                or current.currency != event.currency
                or current.multiplier != _ONE
            ):
                raise PortfolioAccountingError("corporate_action_book_before_mismatch")
            current_value = current.quantity * current.mark_price * current.multiplier

        transfer_key = (action_hash, event.currency)
        if event.event_type is PortfolioEventType.TERMINAL_SETTLEMENT:
            if current is None:
                raise PortfolioAccountingError("terminal_settlement_position_missing")
            if after_quantity == _ZERO:
                positions.pop(key)
            else:
                current.quantity = after_quantity
                current.average_price = after_basis / abs(after_quantity)
                if event.mark_price is not None:
                    current.mark_price = event.mark_price
            return event.realized_pnl

        if event.event_type is PortfolioEventType.POSITION_TRANSFORMATION:
            transferred_value = _ZERO
            retained_value = current_value
            transfer_fraction_text = metadata.get("transfer_value_fraction")
            if transfer_fraction_text is not None:
                try:
                    parsed_transfer_fraction = Decimal(transfer_fraction_text)
                except InvalidOperation as exc:
                    raise PortfolioAccountingError(
                        "corporate_action_transfer_fraction_invalid"
                    ) from exc
                transfer_fraction = _decimal(
                    parsed_transfer_fraction,
                    "corporate_action.transfer_value_fraction",
                    nonnegative=True,
                )
                if transfer_fraction > _ONE:
                    raise PortfolioAccountingError(
                        "corporate_action_transfer_fraction_above_one"
                    )
                transferred_value = current_value * transfer_fraction
                retained_value = current_value - transferred_value
            elif after_quantity == _ZERO:
                transferred_value = current_value
                retained_value = _ZERO
            elif before_basis > _ZERO and after_basis < before_basis:
                retained_value = current_value * after_basis / before_basis
                transferred_value = current_value - retained_value
            if (
                transferred_value != _ZERO
                and metadata.get("cash_settled_transfer") != "true"
            ):
                pending_values[transfer_key] = (
                    pending_values.get(transfer_key, _ZERO) + transferred_value
                )
            final_value = retained_value
        else:
            transferred_value = pending_values.pop(transfer_key, _ZERO)
            final_value = current_value + transferred_value

        if after_quantity == _ZERO:
            positions.pop(key, None)
            return _ZERO
        average_price = after_basis / abs(after_quantity)
        final_mark = event.mark_price
        if final_mark is None:
            final_mark = final_value / after_quantity
        if final_mark <= _ZERO:
            raise PortfolioAccountingError("corporate_action_mark_nonpositive")
        positions[key] = _PositionAccumulator(
            asset_class=AssetClass.SPOT,
            currency=event.currency,
            quantity=after_quantity,
            average_price=average_price,
            mark_price=final_mark,
            multiplier=_ONE,
        )
        return _ZERO

    @staticmethod
    def _apply_deliverable(
        event: PortfolioEvent,
        positions: dict[tuple[AssetClass, str], _PositionAccumulator],
    ) -> None:
        if event.deliverable_quantity_delta == _ZERO:
            return
        if (
            event.deliverable_asset_id is None
            or event.deliverable_asset_class is None
            or event.deliverable_currency is None
            or event.deliverable_multiplier is None
            or event.deliverable_basis_price is None
            or event.deliverable_mark_price is None
        ):
            raise PortfolioAccountingError("option_deliverable_fields_missing")
        key = (event.deliverable_asset_class, event.deliverable_asset_id)
        deliverable = positions.get(key)
        if deliverable is None:
            deliverable = _PositionAccumulator(
                asset_class=event.deliverable_asset_class,
                currency=event.deliverable_currency,
                quantity=_ZERO,
                average_price=event.deliverable_basis_price,
                mark_price=event.deliverable_mark_price,
                multiplier=event.deliverable_multiplier,
            )
            positions[key] = deliverable
        elif deliverable.multiplier != event.deliverable_multiplier:
            raise PortfolioAccountingError("option_deliverable_multiplier_mismatch")
        deliverable.trade(
            event.deliverable_quantity_delta,
            event.deliverable_basis_price,
        )
        deliverable.mark_price = event.deliverable_mark_price
        if deliverable.quantity == _ZERO:
            positions.pop(key)


def funding_event(
    *,
    event_id: str,
    occurred_at: str,
    cash_deltas: tuple[CashDelta, ...],
    conversion_evidence: tuple[ExternalFlowConversionEvidence, ...] = (),
    source_hashes: tuple[str, ...] = (),
) -> PortfolioEventDraft:
    return PortfolioEventDraft(
        event_id=event_id,
        event_type=PortfolioEventType.FUNDING,
        occurred_at=occurred_at,
        cash_deltas=cash_deltas,
        external_flow_conversions=conversion_evidence,
        source_hashes=source_hashes,
    )


def trade_event(
    *,
    event_id: str,
    occurred_at: str,
    asset_class: AssetClass,
    instrument_id: str,
    currency: str,
    quantity_delta: Decimal,
    price: Decimal,
    multiplier: Decimal = Decimal("1"),
    realized_pnl: Decimal = Decimal("0"),
    source_hashes: tuple[str, ...] = (),
    execution_context_hash: str | None = None,
) -> PortfolioEventDraft:
    event_type = {
        AssetClass.SPOT: PortfolioEventType.SPOT_TRADE,
        AssetClass.FUTURE: PortfolioEventType.FUTURES_TRADE,
        AssetClass.OPTION: PortfolioEventType.OPTION_TRADE,
    }[asset_class]
    cash_amount = (
        realized_pnl
        if asset_class is AssetClass.FUTURE
        else -(quantity_delta * price * multiplier)
    )
    cash_deltas = () if cash_amount == _ZERO else (CashDelta(currency, cash_amount),)
    return PortfolioEventDraft(
        event_id=event_id,
        event_type=event_type,
        occurred_at=occurred_at,
        currency=currency,
        cash_deltas=cash_deltas,
        instrument_id=instrument_id,
        asset_class=asset_class,
        quantity_delta=quantity_delta,
        price=price,
        multiplier=multiplier,
        realized_pnl=realized_pnl,
        source_hashes=source_hashes,
        execution_context_hash=execution_context_hash,
    )


def mark_event(
    *,
    event_id: str,
    occurred_at: str,
    asset_class: AssetClass,
    instrument_id: str,
    currency: str,
    mark_price: Decimal,
    source_hashes: tuple[str, ...] = (),
) -> PortfolioEventDraft:
    return PortfolioEventDraft(
        event_id=event_id,
        event_type=PortfolioEventType.POSITION_MARK,
        occurred_at=occurred_at,
        currency=currency,
        instrument_id=instrument_id,
        asset_class=asset_class,
        mark_price=mark_price,
        source_hashes=source_hashes,
    )


def cost_events_from_breakdown(
    breakdown: CostBreakdown,
    *,
    event_id_prefix: str,
    occurred_at: str,
    instrument_id: str | None = None,
    asset_class: AssetClass | None = None,
    source_hashes: tuple[str, ...] = (),
) -> tuple[PortfolioEventDraft, ...]:
    """Publish every non-zero common cost component as a typed cash event."""

    if not isinstance(breakdown, CostBreakdown):
        raise PortfolioAccountingError("cost_breakdown_required")
    type_for_component = {
        "commission": PortfolioEventType.FEE,
        "tax": PortfolioEventType.TAX,
        "borrow": PortfolioEventType.BORROW_COST,
        "financing": PortfolioEventType.FINANCING_COST,
    }
    drafts: list[PortfolioEventDraft] = []
    for component in breakdown.component_names():
        amount = getattr(breakdown, component)
        if amount == _ZERO:
            continue
        drafts.append(
            PortfolioEventDraft(
                event_id=f"{event_id_prefix}:{component}",
                event_type=type_for_component.get(
                    component, PortfolioEventType.EXECUTION_COST
                ),
                occurred_at=occurred_at,
                currency=breakdown.currency,
                cash_deltas=(CashDelta(breakdown.currency, -amount),),
                instrument_id=instrument_id,
                asset_class=asset_class,
                execution_context_hash=breakdown.execution_hash,
                cost_breakdown=breakdown,
                source_hashes=source_hashes,
                metadata=(("cost_component", component),),
            )
        )
    return tuple(drafts)


class TaxLotMethod(StrEnum):
    FIFO = "FIFO"
    AVERAGE = "AVERAGE"


@dataclass(frozen=True, slots=True)
class TaxLot:
    lot_id: str
    instrument_id: str
    asset_class: AssetClass
    currency: str
    quantity: Decimal
    unit_cost: Decimal
    multiplier: Decimal
    opened_at: str
    source_event_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("lot_id", "instrument_id"):
            _require_id(getattr(self, name), f"tax_lot.{name}")
        if not isinstance(self.asset_class, AssetClass):
            raise PortfolioAccountingError("tax_lot_asset_class_invalid")
        if not _CURRENCY.fullmatch(self.currency):
            raise PortfolioAccountingError("tax_lot_currency_invalid")
        _decimal(self.quantity, "tax_lot.quantity")
        if self.quantity == _ZERO:
            raise PortfolioAccountingError("tax_lot_zero_quantity")
        _decimal(self.unit_cost, "tax_lot.unit_cost", positive=True)
        _decimal(self.multiplier, "tax_lot.multiplier", positive=True)
        _parse_timestamp(self.opened_at, "tax_lot.opened_at")
        _require_hash(self.source_event_hash, "tax_lot.source_event_hash")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="portfolio_tax_lot"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "lot_id": self.lot_id,
            "instrument_id": self.instrument_id,
            "asset_class": self.asset_class.value,
            "currency": self.currency,
            "quantity": _decimal_text(self.quantity),
            "unit_cost": _decimal_text(self.unit_cost),
            "multiplier": _decimal_text(self.multiplier),
            "opened_at": self.opened_at,
            "source_event_hash": self.source_event_hash,
        }


@dataclass(frozen=True, slots=True)
class TaxLotRealization:
    closing_event_hash: str
    source_lot_hash: str
    instrument_id: str
    currency: str
    closed_quantity: Decimal
    realized_pnl: Decimal
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_hash(self.closing_event_hash, "tax_realization.closing_event_hash")
        _require_hash(self.source_lot_hash, "tax_realization.source_lot_hash")
        _require_id(self.instrument_id, "tax_realization.instrument_id")
        if not _CURRENCY.fullmatch(self.currency):
            raise PortfolioAccountingError("tax_realization_currency_invalid")
        _decimal(
            self.closed_quantity,
            "tax_realization.closed_quantity",
            positive=True,
        )
        _decimal(self.realized_pnl, "tax_realization.realized_pnl")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "closing_event_hash": self.closing_event_hash,
                    "source_lot_hash": self.source_lot_hash,
                    "instrument_id": self.instrument_id,
                    "currency": self.currency,
                    "closed_quantity": _decimal_text(self.closed_quantity),
                    "realized_pnl": _decimal_text(self.realized_pnl),
                },
                label="portfolio_tax_lot_realization",
            ),
        )


@dataclass(frozen=True, slots=True)
class TaxLotProjection:
    ledger_hash: str
    method: TaxLotMethod
    open_lots: tuple[TaxLot, ...]
    realizations: tuple[TaxLotRealization, ...]
    realized_pnl_by_currency: tuple[CurrencyBalance, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_hash(self.ledger_hash, "tax_projection.ledger_hash")
        if not isinstance(self.method, TaxLotMethod):
            raise PortfolioAccountingError("tax_projection_method_invalid")
        if any(not isinstance(item, TaxLot) for item in self.open_lots):
            raise PortfolioAccountingError("tax_projection_open_lot_invalid")
        expected_order = tuple(
            sorted(
                self.open_lots,
                key=lambda item: (
                    item.asset_class.value,
                    item.instrument_id,
                    item.opened_at,
                    item.lot_id,
                ),
            )
        )
        if expected_order != self.open_lots:
            raise PortfolioAccountingError("tax_projection_open_lots_not_ordered")
        if len(
            {
                (item.asset_class, item.instrument_id, item.lot_id)
                for item in self.open_lots
            }
        ) != len(self.open_lots):
            raise PortfolioAccountingError("tax_projection_open_lot_duplicate")
        if any(not isinstance(item, TaxLotRealization) for item in self.realizations):
            raise PortfolioAccountingError("tax_projection_realization_invalid")
        expected = _balances(
            {
                currency: sum(
                    (
                        item.realized_pnl
                        for item in self.realizations
                        if item.currency == currency
                    ),
                    _ZERO,
                )
                for currency in {item.currency for item in self.realizations}
            }
        )
        if expected != self.realized_pnl_by_currency:
            raise PortfolioAccountingError("tax_projection_realized_pnl_mismatch")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "ledger_hash": self.ledger_hash,
                    "method": self.method.value,
                    "open_lot_hashes": [item.content_hash for item in self.open_lots],
                    "realization_hashes": [
                        item.content_hash for item in self.realizations
                    ],
                    "realized_pnl_by_currency": [
                        {
                            "currency": item.currency,
                            "amount": _decimal_text(item.amount),
                        }
                        for item in self.realized_pnl_by_currency
                    ],
                },
                label="portfolio_tax_lot_projection",
            ),
        )


def project_tax_lots(
    ledger: UnifiedPortfolioLedger,
    *,
    method: TaxLotMethod,
) -> TaxLotProjection:
    """Replay trade events under FIFO or average-cost lot authority."""

    if not isinstance(ledger, UnifiedPortfolioLedger):
        raise PortfolioAccountingError("tax_projection_ledger_required")
    if not isinstance(method, TaxLotMethod):
        raise PortfolioAccountingError("tax_projection_method_invalid")
    ledger.verify_integrity()
    lots: dict[tuple[AssetClass, str], list[TaxLot]] = {}
    realizations: list[TaxLotRealization] = []
    for event in ledger.events:
        if event.event_type not in _TRADE_EVENT_TYPES:
            continue
        if (
            event.asset_class is None
            or event.instrument_id is None
            or event.currency is None
            or event.price is None
        ):
            raise PortfolioAccountingError("tax_projection_trade_fields_missing")
        key = (event.asset_class, event.instrument_id)
        active = lots.setdefault(key, [])
        remaining = event.quantity_delta
        while remaining != _ZERO and active and active[0].quantity * remaining < _ZERO:
            source = active[0]
            closed = min(abs(remaining), abs(source.quantity))
            direction = _ONE if source.quantity > _ZERO else -_ONE
            pnl = (
                closed
                * (event.price - source.unit_cost)
                * source.multiplier
                * direction
            )
            realizations.append(
                TaxLotRealization(
                    closing_event_hash=event.content_hash,
                    source_lot_hash=source.content_hash,
                    instrument_id=event.instrument_id,
                    currency=event.currency,
                    closed_quantity=closed,
                    realized_pnl=pnl,
                )
            )
            new_source_quantity = source.quantity - direction * closed
            remaining += direction * closed
            if new_source_quantity == _ZERO:
                active.pop(0)
            else:
                active[0] = TaxLot(
                    lot_id=source.lot_id,
                    instrument_id=source.instrument_id,
                    asset_class=source.asset_class,
                    currency=source.currency,
                    quantity=new_source_quantity,
                    unit_cost=source.unit_cost,
                    multiplier=source.multiplier,
                    opened_at=source.opened_at,
                    source_event_hash=source.source_event_hash,
                )
        if remaining != _ZERO:
            incoming = TaxLot(
                lot_id=f"{event.event_id}.lot",
                instrument_id=event.instrument_id,
                asset_class=event.asset_class,
                currency=event.currency,
                quantity=remaining,
                unit_cost=event.price,
                multiplier=event.multiplier,
                opened_at=event.occurred_at,
                source_event_hash=event.content_hash,
            )
            if (
                method is TaxLotMethod.AVERAGE
                and active
                and active[0].quantity * incoming.quantity > _ZERO
            ):
                prior = active[0]
                combined_quantity = prior.quantity + incoming.quantity
                average = (
                    abs(prior.quantity) * prior.unit_cost
                    + abs(incoming.quantity) * incoming.unit_cost
                ) / abs(combined_quantity)
                active[:] = [
                    TaxLot(
                        lot_id=prior.lot_id,
                        instrument_id=prior.instrument_id,
                        asset_class=prior.asset_class,
                        currency=prior.currency,
                        quantity=combined_quantity,
                        unit_cost=average,
                        multiplier=prior.multiplier,
                        opened_at=prior.opened_at,
                        source_event_hash=sha256_prefixed(
                            [prior.source_event_hash, incoming.source_event_hash],
                            label="average_tax_lot_sources",
                        ),
                    )
                ]
            else:
                active.append(incoming)
        if method is TaxLotMethod.AVERAGE and len(active) > 1:
            raise PortfolioAccountingError("average_tax_projection_multiple_lots")
    open_lots = tuple(
        sorted(
            (item for values in lots.values() for item in values),
            key=lambda item: (
                item.asset_class.value,
                item.instrument_id,
                item.opened_at,
                item.lot_id,
            ),
        )
    )
    realized_balances = _balances(
        {
            currency: sum(
                (
                    item.realized_pnl
                    for item in realizations
                    if item.currency == currency
                ),
                _ZERO,
            )
            for currency in {item.currency for item in realizations}
        }
    )
    return TaxLotProjection(
        ledger_hash=ledger.content_hash,
        method=method,
        open_lots=open_lots,
        realizations=tuple(realizations),
        realized_pnl_by_currency=realized_balances,
    )


@runtime_checkable
class FuturesFillLike(Protocol):
    fill_id: str
    contract_id: str
    filled_at: str
    side: object
    quantity: int
    fill_price: Decimal
    multiplier: Decimal
    commission: Decimal
    slippage_cost: Decimal
    realized_trade_pnl: Decimal
    content_hash: str


@runtime_checkable
class FuturesSettlementEventLike(Protocol):
    event_id: str
    contract_id: str
    settled_at: str
    settlement_price: Decimal
    quantity: int
    multiplier: Decimal
    variation_margin: Decimal
    content_hash: str


@runtime_checkable
class OptionFillLike(Protocol):
    fill_id: str
    contract: object
    side: object
    requested_quantity: Decimal
    filled_quantity: Decimal
    price: Decimal | None
    fee: Decimal
    filled_at: str
    status: object
    failure_code: str | None
    content_hash: str


@runtime_checkable
class OptionLifecycleEventLike(Protocol):
    @property
    def event_id(self) -> str: ...

    @property
    def event_type(self) -> object: ...

    @property
    def contract_id(self) -> str: ...

    @property
    def position_id(self) -> str: ...

    @property
    def occurred_at(self) -> str: ...

    @property
    def settlement_input(self) -> object: ...

    @property
    def exercise_fraction(self) -> Decimal: ...

    @property
    def exercised_quantity(self) -> Decimal: ...

    @property
    def intrinsic_value_per_unit(self) -> Decimal: ...

    @property
    def cash_delta(self) -> Decimal: ...

    @property
    def deliverable_quantity_delta(self) -> Decimal: ...

    @property
    def deliverable_asset_id(self) -> str | None: ...

    @property
    def deliverable_contract_multiplier(self) -> Decimal | None: ...

    @property
    def source_position_hash(self) -> str: ...

    @property
    def content_hash(self) -> str: ...


@runtime_checkable
class OptionContractLike(Protocol):
    @property
    def contract_id(self) -> str: ...

    @property
    def option_type(self) -> object: ...

    @property
    def strike(self) -> Decimal: ...

    @property
    def expiration_at(self) -> str: ...

    @property
    def settlement_type(self) -> object: ...

    @property
    def multiplier(self) -> Decimal: ...

    @property
    def currency(self) -> str: ...

    @property
    def deliverable_asset_id(self) -> str | None: ...

    @property
    def physical_settlement_convention(self) -> object | None: ...

    @property
    def deliverable_quantity_per_contract(self) -> Decimal | None: ...

    @property
    def deliverable_contract_multiplier(self) -> Decimal | None: ...


@runtime_checkable
class OptionPositionLike(Protocol):
    @property
    def position_id(self) -> str: ...

    @property
    def contract(self) -> OptionContractLike: ...

    @property
    def side(self) -> object: ...

    @property
    def quantity(self) -> Decimal: ...

    @property
    def content_hash(self) -> str: ...


def adapt_futures_fill(
    fill: FuturesFillLike,
    *,
    currency: str,
    execution_context_hash: str | None = None,
) -> tuple[PortfolioEventDraft, ...]:
    """Adapt ``FuturesFill`` while retaining its realized-P&L/cost semantics."""

    side = _enum_text(fill.side)
    if side not in {"BUY", "SELL"}:
        raise PortfolioAccountingError("futures_fill_side_invalid")
    source = (fill.content_hash,)
    drafts: list[PortfolioEventDraft] = [
        trade_event(
            event_id=f"{fill.fill_id}:trade",
            occurred_at=fill.filled_at,
            asset_class=AssetClass.FUTURE,
            instrument_id=fill.contract_id,
            currency=currency,
            quantity_delta=Decimal(fill.quantity) * (_ONE if side == "BUY" else -_ONE),
            price=fill.fill_price,
            multiplier=fill.multiplier,
            realized_pnl=fill.realized_trade_pnl,
            source_hashes=source,
            execution_context_hash=execution_context_hash,
        )
    ]
    for suffix, event_type, amount in (
        ("commission", PortfolioEventType.FEE, fill.commission),
        ("slippage", PortfolioEventType.EXECUTION_COST, fill.slippage_cost),
    ):
        if amount != _ZERO:
            drafts.append(
                PortfolioEventDraft(
                    event_id=f"{fill.fill_id}:{suffix}",
                    event_type=event_type,
                    occurred_at=fill.filled_at,
                    currency=currency,
                    cash_deltas=(CashDelta(currency, -amount),),
                    instrument_id=fill.contract_id,
                    asset_class=AssetClass.FUTURE,
                    execution_context_hash=execution_context_hash,
                    source_hashes=source,
                    metadata=(("cost_component", suffix),),
                )
            )
    return tuple(drafts)


def adapt_futures_settlement(
    event: FuturesSettlementEventLike,
    *,
    currency: str,
) -> PortfolioEventDraft:
    return PortfolioEventDraft(
        event_id=event.event_id,
        event_type=PortfolioEventType.FUTURES_SETTLEMENT,
        occurred_at=event.settled_at,
        currency=currency,
        cash_deltas=(
            ()
            if event.variation_margin == _ZERO
            else (CashDelta(currency, event.variation_margin),)
        ),
        instrument_id=event.contract_id,
        asset_class=AssetClass.FUTURE,
        multiplier=event.multiplier,
        mark_price=event.settlement_price,
        realized_pnl=event.variation_margin,
        settlement_quantity=Decimal(event.quantity),
        source_hashes=(event.content_hash,),
    )


@runtime_checkable
class FuturesContractMasterLike(Protocol):
    @property
    def contract_id(self) -> str: ...

    @property
    def contract_multiplier(self) -> Decimal: ...

    @property
    def settlement_currency(self) -> str: ...

    @property
    def settlement_mode(self) -> object: ...

    def contract_hash(self) -> str: ...


@runtime_checkable
class FuturesDeliveryPolicyLike(Protocol):
    @property
    def default_closeout_penalty_rate(self) -> Decimal: ...

    def policy_hash(self) -> str: ...


@runtime_checkable
class FuturesLifecyclePostingLike(Protocol):
    @property
    def posting_id(self) -> str: ...

    @property
    def event_type(self) -> object: ...

    @property
    def occurred_at(self) -> str: ...

    @property
    def contract_id(self) -> str: ...

    @property
    def contract_quantity_delta(self) -> Decimal: ...

    @property
    def cash_delta(self) -> Decimal: ...

    @property
    def currency(self) -> str: ...

    @property
    def delivered_instrument_id(self) -> str | None: ...

    @property
    def delivered_quantity_delta(self) -> Decimal: ...

    @property
    def source_hashes(self) -> tuple[str, ...]: ...

    @property
    def previous_posting_hash(self) -> str | None: ...

    def posting_hash(self) -> str: ...


@runtime_checkable
class CTDComparisonLike(Protocol):
    @property
    def grade_id(self) -> str: ...

    @property
    def instrument_id(self) -> str: ...

    @property
    def invoice_amount(self) -> Decimal: ...


@runtime_checkable
class CTDDecisionLike(Protocol):
    @property
    def contract_id(self) -> str: ...

    @property
    def futures_settlement_price(self) -> Decimal: ...

    @property
    def selected_grade_id(self) -> str: ...

    @property
    def comparisons(self) -> tuple[CTDComparisonLike, ...]: ...

    @property
    def contract_hash(self) -> str: ...

    @property
    def basket_hash(self) -> str: ...

    def decision_hash(self) -> str: ...


def _futures_lifecycle_audit(
    *,
    event_id: str,
    event_type: PortfolioEventType,
    occurred_at: str,
    evidence_hashes: tuple[str, ...],
    details: Mapping[str, str],
) -> PortfolioEventDraft:
    return (
        InvariantAccountingFactory(
            factory_id="futures.lifecycle.accounting",
            version="1",
        )
        .audit_bundle(
            event_id=event_id,
            event_type=event_type,
            occurred_at=occurred_at,
            economic_event_hashes=tuple(sorted(set(evidence_hashes))),
            details=details,
        )
        .audit_event
    )


def adapt_futures_lifecycle_posting(
    posting: FuturesLifecyclePostingLike,
    *,
    ledger: UnifiedPortfolioLedger,
    contract: FuturesContractMasterLike,
    policy: FuturesDeliveryPolicyLike,
    ctd: CTDDecisionLike | None = None,
) -> tuple[PortfolioEventDraft, ...]:
    """Close an actual futures contract and project delivery/default economics.

    The posting's cash amount is split into variation margin, delivery invoice,
    and default penalty without accepting an unbound caller price. Cash
    settlement derives the terminal price from the open ledger basis; physical
    delivery takes it from the posting-bound CTD decision.
    """

    if not isinstance(posting, FuturesLifecyclePostingLike):
        raise PortfolioAccountingError("futures_lifecycle_posting_invalid")
    if not isinstance(ledger, UnifiedPortfolioLedger):
        raise PortfolioAccountingError("futures_lifecycle_ledger_required")
    if not isinstance(contract, FuturesContractMasterLike):
        raise PortfolioAccountingError("futures_lifecycle_contract_invalid")
    if not isinstance(policy, FuturesDeliveryPolicyLike):
        raise PortfolioAccountingError("futures_lifecycle_policy_invalid")
    ledger.verify_integrity()
    posting_hash = posting.posting_hash()
    contract_hash = contract.contract_hash()
    policy_hash = policy.policy_hash()
    for value, name in (
        (posting_hash, "posting_hash"),
        (contract_hash, "contract_hash"),
        (policy_hash, "policy_hash"),
    ):
        _require_hash(value, f"futures_lifecycle.{name}")
    if (
        posting.contract_id != contract.contract_id
        or posting.currency != contract.settlement_currency
        or contract_hash not in posting.source_hashes
        or policy_hash not in posting.source_hashes
    ):
        raise PortfolioAccountingError("futures_lifecycle_source_binding_mismatch")
    _parse_timestamp(posting.occurred_at, "futures_lifecycle.occurred_at")
    quantity_delta = _decimal(
        posting.contract_quantity_delta,
        "futures_lifecycle.contract_quantity_delta",
    )
    cash_delta = _decimal(
        posting.cash_delta,
        "futures_lifecycle.cash_delta",
    )
    delivered_quantity = _decimal(
        posting.delivered_quantity_delta,
        "futures_lifecycle.delivered_quantity_delta",
    )
    multiplier = _decimal(
        contract.contract_multiplier,
        "futures_lifecycle.contract_multiplier",
        positive=True,
    )
    snapshot = ledger.replay()
    matches = [
        item
        for item in snapshot.positions
        if item.asset_class is AssetClass.FUTURE
        and item.instrument_id == contract.contract_id
    ]
    if len(matches) != 1:
        raise PortfolioAccountingError("futures_lifecycle_open_position_not_unique")
    position = matches[0]
    if (
        quantity_delta != -position.quantity
        or position.currency != posting.currency
        or position.multiplier != multiplier
    ):
        raise PortfolioAccountingError("futures_lifecycle_position_mismatch")
    sources = tuple(sorted({*posting.source_hashes, posting_hash}))
    lifecycle_type = _enum_text(posting.event_type)
    settlement_mode = _enum_text(contract.settlement_mode)
    terminal_price: Decimal
    invoice_cash = _ZERO
    penalty = _ZERO
    audit_drafts: list[PortfolioEventDraft] = []

    if lifecycle_type == "CASH_SETTLEMENT":
        if settlement_mode != "CASH" or ctd is not None:
            raise PortfolioAccountingError(
                "futures_lifecycle_cash_settlement_mode_mismatch"
            )
        if posting.delivered_instrument_id is not None or delivered_quantity != _ZERO:
            raise PortfolioAccountingError("futures_lifecycle_cash_delivery_forbidden")
        terminal_price = position.average_price + (
            cash_delta / (position.quantity * multiplier)
        )
    elif lifecycle_type == "DELIVERY":
        if settlement_mode != "PHYSICAL" or not isinstance(ctd, CTDDecisionLike):
            raise PortfolioAccountingError("futures_lifecycle_ctd_decision_required")
        ctd_hash = ctd.decision_hash()
        _require_hash(ctd_hash, "futures_lifecycle.ctd_hash")
        if (
            ctd.contract_id != contract.contract_id
            or ctd.contract_hash != contract_hash
            or ctd_hash not in posting.source_hashes
            or ctd.basket_hash not in posting.source_hashes
        ):
            raise PortfolioAccountingError("futures_lifecycle_ctd_binding_mismatch")
        selected = next(
            (
                item
                for item in ctd.comparisons
                if item.grade_id == ctd.selected_grade_id
            ),
            None,
        )
        if (
            selected is None
            or selected.instrument_id != posting.delivered_instrument_id
            or delivered_quantity != position.quantity * multiplier
        ):
            raise PortfolioAccountingError("futures_lifecycle_delivery_terms_mismatch")
        terminal_price = _decimal(
            ctd.futures_settlement_price,
            "futures_lifecycle.ctd_settlement_price",
            positive=True,
        )
        invoice_per_contract = _decimal(
            selected.invoice_amount,
            "futures_lifecycle.invoice_amount",
            positive=True,
        )
        direction = _ONE if position.quantity > _ZERO else -_ONE
        invoice_cash = -direction * invoice_per_contract * abs(position.quantity)
        audit_drafts.append(
            _futures_lifecycle_audit(
                event_id=f"{posting.posting_id}:delivery-audit",
                event_type=PortfolioEventType.DELIVERY,
                occurred_at=posting.occurred_at,
                evidence_hashes=(posting_hash, ctd_hash),
                details={
                    "contract_id": contract.contract_id,
                    "ctd_grade_id": ctd.selected_grade_id,
                    "delivered_instrument_id": selected.instrument_id,
                },
            )
        )
    elif lifecycle_type == "DEFAULT":
        if (
            ctd is not None
            or posting.delivered_instrument_id is not None
            or delivered_quantity != _ZERO
        ):
            raise PortfolioAccountingError(
                "futures_lifecycle_default_delivery_forbidden"
            )
        rate = _decimal(
            policy.default_closeout_penalty_rate,
            "futures_lifecycle.default_penalty_rate",
            nonnegative=True,
        )
        denominator = multiplier * (position.quantity - abs(position.quantity) * rate)
        if denominator == _ZERO:
            raise PortfolioAccountingError(
                "futures_lifecycle_default_price_not_identifiable"
            )
        terminal_price = (
            cash_delta + position.quantity * multiplier * position.average_price
        ) / denominator
        audit_drafts.extend(
            (
                _futures_lifecycle_audit(
                    event_id=f"{posting.posting_id}:default-audit",
                    event_type=PortfolioEventType.DEFAULT,
                    occurred_at=posting.occurred_at,
                    evidence_hashes=(posting_hash,),
                    details={
                        "contract_id": contract.contract_id,
                        "forced_closeout": "true",
                    },
                ),
                _futures_lifecycle_audit(
                    event_id=f"{posting.posting_id}:liquidation-audit",
                    event_type=PortfolioEventType.FORCED_LIQUIDATION,
                    occurred_at=posting.occurred_at,
                    evidence_hashes=(posting_hash,),
                    details={
                        "contract_id": contract.contract_id,
                        "reason": "futures_delivery_default",
                    },
                ),
            )
        )
    else:
        raise PortfolioAccountingError(
            f"futures_lifecycle_event_unsupported:{lifecycle_type}"
        )
    if terminal_price <= _ZERO:
        raise PortfolioAccountingError("futures_lifecycle_terminal_price_nonpositive")
    variation_margin = (
        (terminal_price - position.average_price) * multiplier * position.quantity
    )
    if lifecycle_type == "DEFAULT":
        penalty = variation_margin - cash_delta
        if penalty < _ZERO:
            raise PortfolioAccountingError("futures_lifecycle_default_penalty_negative")
    elif cash_delta != variation_margin + invoice_cash:
        raise PortfolioAccountingError("futures_lifecycle_cash_identity_mismatch")

    drafts: list[PortfolioEventDraft] = [
        PortfolioEventDraft(
            event_id=f"{posting.posting_id}:future-close",
            event_type=PortfolioEventType.FUTURES_TRADE,
            occurred_at=posting.occurred_at,
            currency=posting.currency,
            cash_deltas=(
                ()
                if variation_margin == _ZERO
                else (CashDelta(posting.currency, variation_margin),)
            ),
            instrument_id=contract.contract_id,
            asset_class=AssetClass.FUTURE,
            quantity_delta=quantity_delta,
            price=terminal_price,
            multiplier=multiplier,
            realized_pnl=variation_margin,
            source_hashes=sources,
            metadata=(("futures_lifecycle_type", lifecycle_type),),
        )
    ]
    if lifecycle_type == "DELIVERY":
        assert posting.delivered_instrument_id is not None
        delivered_basis_price = -invoice_cash / delivered_quantity
        drafts.append(
            PortfolioEventDraft(
                event_id=f"{posting.posting_id}:delivered-position",
                event_type=PortfolioEventType.SPOT_TRADE,
                occurred_at=posting.occurred_at,
                currency=posting.currency,
                cash_deltas=(CashDelta(posting.currency, invoice_cash),),
                instrument_id=posting.delivered_instrument_id,
                asset_class=AssetClass.SPOT,
                quantity_delta=delivered_quantity,
                price=delivered_basis_price,
                source_hashes=sources,
                metadata=(
                    ("futures_lifecycle_type", lifecycle_type),
                    ("source_contract_id", contract.contract_id),
                ),
            )
        )
    if penalty > _ZERO:
        drafts.append(
            PortfolioEventDraft(
                event_id=f"{posting.posting_id}:default-penalty",
                event_type=PortfolioEventType.EXECUTION_COST,
                occurred_at=posting.occurred_at,
                currency=posting.currency,
                cash_deltas=(CashDelta(posting.currency, -penalty),),
                instrument_id=contract.contract_id,
                asset_class=AssetClass.FUTURE,
                source_hashes=sources,
                metadata=(("cost_component", "default_closeout_penalty"),),
            )
        )
    drafts.extend(audit_drafts)
    if (
        sum(
            (delta.amount for draft in drafts for delta in draft.cash_deltas),
            start=_ZERO,
        )
        != cash_delta
    ):
        raise PortfolioAccountingError("futures_lifecycle_published_cash_mismatch")
    return tuple(drafts)


@runtime_checkable
class MarginWaterfallResultLike(Protocol):
    @property
    def gross_initial_requirement(self) -> Decimal: ...

    @property
    def spread_offset(self) -> Decimal: ...

    @property
    def net_initial_requirement(self) -> Decimal: ...

    @property
    def maintenance_requirement(self) -> Decimal: ...

    @property
    def eligible_collateral_value(self) -> Decimal: ...

    @property
    def variation_margin(self) -> Decimal: ...

    @property
    def collateral_income(self) -> Decimal: ...

    @property
    def margin_call(self) -> Decimal: ...

    @property
    def additional_funding(self) -> Decimal: ...

    @property
    def default_amount(self) -> Decimal: ...

    @property
    def consumed_assets(self) -> tuple[tuple[str, Decimal], ...]: ...

    @property
    def policy_hash(self) -> str: ...

    def result_hash(self) -> str: ...


def adapt_futures_margin_waterfall(
    result: MarginWaterfallResultLike,
    *,
    event_id_prefix: str,
    occurred_at: str,
    currency: str,
    contract_id: str,
    funding_conversion_evidence: tuple[ExternalFlowConversionEvidence, ...] = (),
) -> tuple[PortfolioEventDraft, ...]:
    """Project aggregate margin economics plus factory-bound exception audits."""

    if not isinstance(result, MarginWaterfallResultLike):
        raise PortfolioAccountingError("margin_waterfall_result_invalid")
    _require_id(event_id_prefix, "margin_waterfall.event_id_prefix")
    _parse_timestamp(occurred_at, "margin_waterfall.occurred_at")
    if not _CURRENCY.fullmatch(currency):
        raise PortfolioAccountingError("margin_waterfall_currency_invalid")
    _require_id(contract_id, "margin_waterfall.contract_id")
    values = {
        name: _decimal(
            getattr(result, name),
            f"margin_waterfall.{name}",
            nonnegative=name != "variation_margin",
        )
        for name in (
            "gross_initial_requirement",
            "spread_offset",
            "net_initial_requirement",
            "maintenance_requirement",
            "eligible_collateral_value",
            "variation_margin",
            "collateral_income",
            "margin_call",
            "additional_funding",
            "default_amount",
        )
    }
    if (
        values["gross_initial_requirement"] - values["spread_offset"]
        != (values["net_initial_requirement"])
    ):
        raise PortfolioAccountingError("margin_waterfall_requirement_identity")
    expected_call = max(
        values["net_initial_requirement"]
        - values["eligible_collateral_value"]
        - values["collateral_income"]
        - values["variation_margin"],
        _ZERO,
    )
    if (
        values["margin_call"] != expected_call
        or values["additional_funding"] + values["default_amount"]
        != values["margin_call"]
    ):
        raise PortfolioAccountingError("margin_waterfall_call_identity")
    consumed = tuple(result.consumed_assets)
    if (
        len({asset_id for asset_id, _amount in consumed}) != len(consumed)
        or any(
            not isinstance(asset_id, str)
            or not asset_id
            or _decimal(
                amount,
                "margin_waterfall.consumed_amount",
                positive=True,
            )
            <= _ZERO
            for asset_id, amount in consumed
        )
        or sum((amount for _asset_id, amount in consumed), start=_ZERO)
        > values["eligible_collateral_value"]
    ):
        raise PortfolioAccountingError("margin_waterfall_consumed_assets_invalid")
    result_hash = result.result_hash()
    _require_hash(result_hash, "margin_waterfall.result_hash")
    _require_hash(result.policy_hash, "margin_waterfall.policy_hash")
    sources = tuple(sorted((result_hash, result.policy_hash)))
    details = {
        "contract_id": contract_id,
        "default_amount": _decimal_text(values["default_amount"]),
        "margin_call": _decimal_text(values["margin_call"]),
        "net_initial_requirement": _decimal_text(values["net_initial_requirement"]),
    }
    drafts: list[PortfolioEventDraft] = []
    if values["variation_margin"] != _ZERO:
        drafts.append(
            PortfolioEventDraft(
                event_id=f"{event_id_prefix}:variation-margin",
                event_type=PortfolioEventType.VARIATION_MARGIN,
                occurred_at=occurred_at,
                currency=currency,
                cash_deltas=(CashDelta(currency, values["variation_margin"]),),
                instrument_id=contract_id,
                asset_class=AssetClass.FUTURE,
                realized_pnl=values["variation_margin"],
                source_hashes=sources,
            )
        )
    if values["collateral_income"] > _ZERO:
        drafts.append(
            collateral_income_event(
                event_id=f"{event_id_prefix}:collateral-income",
                occurred_at=occurred_at,
                currency=currency,
                amount=values["collateral_income"],
                source_hashes=sources,
            )
        )
    if values["additional_funding"] > _ZERO:
        drafts.append(
            funding_event(
                event_id=f"{event_id_prefix}:additional-funding",
                occurred_at=occurred_at,
                cash_deltas=(CashDelta(currency, values["additional_funding"]),),
                conversion_evidence=funding_conversion_evidence,
                source_hashes=sources,
            )
        )
    drafts.append(
        PortfolioEventDraft(
            event_id=f"{event_id_prefix}:margin-requirement",
            event_type=PortfolioEventType.MARGIN_REQUIREMENT,
            occurred_at=occurred_at,
            currency=currency,
            instrument_id=contract_id,
            asset_class=AssetClass.FUTURE,
            margin_requirement=values["net_initial_requirement"],
            source_hashes=sources,
        )
    )
    for suffix, event_type in (
        ("margin-call-audit", PortfolioEventType.MARGIN_CALL),
        ("collateral-waterfall-audit", PortfolioEventType.COLLATERAL_WATERFALL),
    ):
        drafts.append(
            _futures_lifecycle_audit(
                event_id=f"{event_id_prefix}:{suffix}",
                event_type=event_type,
                occurred_at=occurred_at,
                evidence_hashes=(result_hash,),
                details=details,
            )
        )
    if values["default_amount"] > _ZERO:
        drafts.append(
            _futures_lifecycle_audit(
                event_id=f"{event_id_prefix}:default-audit",
                event_type=PortfolioEventType.DEFAULT,
                occurred_at=occurred_at,
                evidence_hashes=(result_hash,),
                details=details,
            )
        )
    return tuple(drafts)


def adapt_option_fill(
    fill: OptionFillLike,
    *,
    execution_context_hash: str | None = None,
) -> tuple[PortfolioEventDraft, ...]:
    """Adapt ``OptionFill`` and split its embedded fee from gross premium."""

    contract = fill.contract
    contract_id = str(getattr(contract, "contract_id"))
    currency = str(getattr(contract, "currency"))
    multiplier = getattr(contract, "multiplier")
    status = _enum_text(fill.status)
    if status in {"FAILED", "UNFILLED"}:
        metadata = [("fill_status", status)]
        if fill.failure_code is not None:
            metadata.append(("failure_code", fill.failure_code))
        return (
            PortfolioEventDraft(
                event_id=f"{fill.fill_id}:attempt",
                event_type=PortfolioEventType.EXECUTION_ATTEMPT,
                occurred_at=fill.filled_at,
                currency=currency,
                instrument_id=contract_id,
                asset_class=AssetClass.OPTION,
                execution_context_hash=execution_context_hash,
                source_hashes=(fill.content_hash,),
                metadata=tuple(metadata),
            ),
        )
    side = _enum_text(fill.side)
    if side not in {"BUY", "SELL"} or fill.price is None:
        raise PortfolioAccountingError("option_fill_execution_fields_invalid")
    source = (fill.content_hash,)
    drafts: list[PortfolioEventDraft] = [
        trade_event(
            event_id=f"{fill.fill_id}:trade",
            occurred_at=fill.filled_at,
            asset_class=AssetClass.OPTION,
            instrument_id=contract_id,
            currency=currency,
            quantity_delta=fill.filled_quantity * (_ONE if side == "BUY" else -_ONE),
            price=fill.price,
            multiplier=multiplier,
            source_hashes=source,
            execution_context_hash=execution_context_hash,
        )
    ]
    if fill.fee != _ZERO:
        drafts.append(
            PortfolioEventDraft(
                event_id=f"{fill.fill_id}:fee",
                event_type=PortfolioEventType.FEE,
                occurred_at=fill.filled_at,
                currency=currency,
                cash_deltas=(CashDelta(currency, -fill.fee),),
                instrument_id=contract_id,
                asset_class=AssetClass.OPTION,
                execution_context_hash=execution_context_hash,
                source_hashes=source,
                metadata=(("cost_component", "commission"),),
            )
        )
    return tuple(drafts)


def adapt_option_lifecycle(
    event: OptionLifecycleEventLike,
    *,
    position: OptionPositionLike,
    deliverable_asset_class: AssetClass = AssetClass.SPOT,
) -> PortfolioEventDraft:
    """Bind a lifecycle event to its immutable position and recheck economics."""

    contract = position.contract
    side = _enum_text(position.side)
    if side not in {"LONG", "SHORT"}:
        raise PortfolioAccountingError("option_lifecycle_position_side_invalid")
    position_quantity = _decimal(
        position.quantity, "option_lifecycle.position_quantity", positive=True
    )
    multiplier = _decimal(
        contract.multiplier, "option_lifecycle.multiplier", positive=True
    )
    strike = _decimal(contract.strike, "option_lifecycle.strike", positive=True)
    fraction = _decimal(
        event.exercise_fraction,
        "option_lifecycle.exercise_fraction",
        nonnegative=True,
    )
    if fraction > _ONE:
        raise PortfolioAccountingError("option_lifecycle_fraction_invalid")
    _require_hash(position.content_hash, "option_lifecycle.position_hash")
    if (
        event.contract_id != contract.contract_id
        or event.position_id != position.position_id
        or event.source_position_hash != position.content_hash
        or getattr(event.settlement_input, "contract_id", None) != contract.contract_id
    ):
        raise PortfolioAccountingError("option_lifecycle_position_binding_mismatch")

    currency = contract.currency
    option_type = _enum_text(contract.option_type)
    settlement_type = _enum_text(contract.settlement_type)
    lifecycle_type = _enum_text(event.event_type)
    if option_type not in {"CALL", "PUT"}:
        raise PortfolioAccountingError("option_lifecycle_option_type_invalid")
    if settlement_type not in {"CASH", "PHYSICAL"}:
        raise PortfolioAccountingError("option_lifecycle_settlement_type_invalid")
    occurred_at = _parse_timestamp(event.occurred_at, "option_lifecycle.occurred_at")
    expiration_at = _parse_timestamp(
        contract.expiration_at, "option_lifecycle.expiration_at"
    )
    expected_lifecycle_type = (
        "EXERCISE"
        if occurred_at < expiration_at and side == "LONG"
        else "ASSIGNMENT"
        if occurred_at < expiration_at
        else "EXPIRY"
    )
    if lifecycle_type != expected_lifecycle_type:
        raise PortfolioAccountingError("option_lifecycle_type_mismatch")
    settlement_price = _decimal(
        getattr(event.settlement_input, "spot_price"),
        "option_lifecycle.settlement_price",
        nonnegative=True,
    )
    intrinsic = (
        max(_ZERO, settlement_price - strike)
        if option_type == "CALL"
        else max(_ZERO, strike - settlement_price)
    )
    if event.intrinsic_value_per_unit != intrinsic:
        raise PortfolioAccountingError("option_lifecycle_intrinsic_mismatch")
    expected_exercised = position_quantity * fraction if intrinsic > _ZERO else _ZERO
    if event.exercised_quantity != expected_exercised:
        raise PortfolioAccountingError("option_lifecycle_exercised_quantity_mismatch")

    position_sign = _ONE if side == "LONG" else -_ONE
    expected_cash = _ZERO
    expected_delivery = _ZERO
    expected_deliverable_id: str | None = None
    expected_deliverable_multiplier: Decimal | None = None
    physical_convention: str | None = None
    if expected_exercised > _ZERO:
        if settlement_type == "CASH":
            scale = expected_exercised * multiplier
            expected_cash = position_sign * intrinsic * scale
        else:
            expected_deliverable_id = contract.deliverable_asset_id
            if expected_deliverable_id is None:
                raise PortfolioAccountingError(
                    "option_lifecycle_deliverable_id_missing"
                )
            physical_convention = _enum_text(contract.physical_settlement_convention)
            if physical_convention not in {
                "SPOT_STRIKE_EXCHANGE",
                "FUTURE_POSITION_NO_PRINCIPAL",
            }:
                raise PortfolioAccountingError(
                    "option_lifecycle_settlement_convention_invalid"
                )
            deliverable_quantity = contract.deliverable_quantity_per_contract
            deliverable_multiplier = contract.deliverable_contract_multiplier
            if deliverable_quantity is None or deliverable_multiplier is None:
                raise PortfolioAccountingError(
                    "option_lifecycle_deliverable_terms_missing"
                )
            deliverable_quantity = _decimal(
                deliverable_quantity,
                "option_lifecycle.deliverable_quantity_per_contract",
                positive=True,
            )
            expected_deliverable_multiplier = _decimal(
                deliverable_multiplier,
                "option_lifecycle.deliverable_contract_multiplier",
                positive=True,
            )
            if deliverable_quantity * expected_deliverable_multiplier != multiplier:
                raise PortfolioAccountingError(
                    "option_lifecycle_deliverable_notional_mismatch"
                )
            scale = expected_exercised * deliverable_quantity
            if option_type == "CALL":
                expected_delivery = position_sign * scale
            else:
                expected_delivery = -position_sign * scale
            if physical_convention == "SPOT_STRIKE_EXCHANGE":
                expected_cash = (
                    -position_sign * strike * scale * expected_deliverable_multiplier
                    if option_type == "CALL"
                    else position_sign
                    * strike
                    * scale
                    * expected_deliverable_multiplier
                )
    if (
        event.cash_delta != expected_cash
        or event.deliverable_quantity_delta != expected_delivery
        or event.deliverable_asset_id != expected_deliverable_id
        or event.deliverable_contract_multiplier != expected_deliverable_multiplier
    ):
        raise PortfolioAccountingError("option_lifecycle_economics_mismatch")

    # Expiration terminates the full contract position even when only part of
    # an in-the-money position is exercised.  Before expiry, only the explicit
    # exercise/assignment fraction closes.
    close_quantity = (
        position_quantity
        if lifecycle_type == "EXPIRY"
        else position_quantity * fraction
    )
    if close_quantity == _ZERO:
        raise PortfolioAccountingError("option_lifecycle_zero_close")
    quantity_delta = close_quantity * (-_ONE if side == "LONG" else _ONE)
    cash_deltas = (
        () if event.cash_delta == _ZERO else (CashDelta(currency, event.cash_delta),)
    )
    delivery_quantity = event.deliverable_quantity_delta
    basis_price: Decimal | None = None
    mark_price_value: Decimal | None = None
    deliverable_currency: str | None = None
    deliverable_class: AssetClass | None = None
    ledger_deliverable_multiplier: Decimal | None = None
    if delivery_quantity != _ZERO:
        if event.deliverable_asset_id is None:
            raise PortfolioAccountingError("option_lifecycle_deliverable_id_missing")
        if event.deliverable_contract_multiplier is None:
            raise PortfolioAccountingError(
                "option_lifecycle_deliverable_multiplier_missing"
            )
        basis_price = strike
        mark_price_value = settlement_price
        deliverable_currency = currency
        deliverable_class = deliverable_asset_class
        ledger_deliverable_multiplier = event.deliverable_contract_multiplier
    return PortfolioEventDraft(
        event_id=event.event_id,
        event_type=PortfolioEventType.OPTION_LIFECYCLE,
        occurred_at=event.occurred_at,
        currency=currency,
        cash_deltas=cash_deltas,
        instrument_id=event.contract_id,
        asset_class=AssetClass.OPTION,
        quantity_delta=quantity_delta,
        multiplier=multiplier,
        deliverable_asset_id=event.deliverable_asset_id,
        deliverable_asset_class=deliverable_class,
        deliverable_currency=deliverable_currency,
        deliverable_quantity_delta=delivery_quantity,
        deliverable_multiplier=ledger_deliverable_multiplier,
        deliverable_basis_price=basis_price,
        deliverable_mark_price=mark_price_value,
        source_hashes=(event.content_hash,),
        metadata=(
            ("lifecycle_type", lifecycle_type),
            ("position_side", side),
        ),
    )


@runtime_checkable
class SpotPositionLike(Protocol):
    @property
    def instrument_id(self) -> str: ...

    @property
    def quantity(self) -> Decimal: ...

    @property
    def total_cost_basis(self) -> Decimal: ...

    @property
    def currency(self) -> str: ...


@runtime_checkable
class SpotCashBalanceLike(Protocol):
    @property
    def currency(self) -> str: ...

    @property
    def amount(self) -> Decimal: ...


@runtime_checkable
class SpotBookLike(Protocol):
    @property
    def positions(self) -> tuple[SpotPositionLike, ...]: ...

    @property
    def cash(self) -> tuple[SpotCashBalanceLike, ...]: ...


@runtime_checkable
class SpotPostingLike(Protocol):
    @property
    def posting_id(self) -> str: ...

    @property
    def posting_type(self) -> object: ...

    @property
    def occurred_at(self) -> datetime: ...

    @property
    def instrument_id(self) -> str: ...

    @property
    def quantity_delta(self) -> Decimal: ...

    @property
    def cash_delta(self) -> Decimal: ...

    @property
    def currency(self) -> str: ...

    @property
    def tax_amount(self) -> Decimal: ...

    @property
    def source_hash(self) -> str: ...

    @property
    def related_instrument_id(self) -> str | None: ...

    @property
    def related_quantity_delta(self) -> Decimal: ...

    @property
    def related_total_cost_basis(self) -> Decimal: ...

    @property
    def entitlement_quantity(self) -> Decimal | None: ...

    @property
    def content_hash(self) -> str: ...


@runtime_checkable
class CorporateActionApplicationLike(Protocol):
    @property
    def action_hash(self) -> str: ...

    @property
    def book_before_hash(self) -> str: ...

    @property
    def book_after_hash(self) -> str: ...

    @property
    def book_before(self) -> SpotBookLike: ...

    @property
    def book_after(self) -> SpotBookLike: ...

    @property
    def postings(self) -> tuple[SpotPostingLike, ...]: ...


@runtime_checkable
class BorrowRecallApplicationLike(Protocol):
    @property
    def recall_hash(self) -> str: ...

    @property
    def execution_quote_hash(self) -> str: ...

    @property
    def book_before_hash(self) -> str: ...

    @property
    def book_after_hash(self) -> str: ...

    @property
    def book_before(self) -> SpotBookLike: ...

    @property
    def book_after(self) -> SpotBookLike: ...

    @property
    def covered_quantity(self) -> Decimal: ...

    @property
    def execution_price(self) -> Decimal: ...

    @property
    def postings(self) -> tuple[SpotPostingLike, ...]: ...


def _spot_book_binding_hash(book: SpotBookLike) -> str:
    """Reproduce the spot engine's immutable book hash at the adapter boundary."""

    return sha256_prefixed(
        {
            "positions": [
                {
                    "instrument_id": item.instrument_id,
                    "quantity": _decimal_text(item.quantity),
                    "total_cost_basis": _decimal_text(item.total_cost_basis),
                    "currency": item.currency,
                }
                for item in book.positions
            ],
            "cash": [
                {
                    "currency": item.currency,
                    "amount": _decimal_text(item.amount),
                }
                for item in book.cash
            ],
        },
        label="spot-book",
    )


def _spot_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PortfolioAccountingError("spot_posting_timestamp_invalid")
    rendered = value.isoformat()
    _parse_timestamp(rendered, "spot_posting.occurred_at")
    return rendered


def _spot_evidence_hashes(
    posting: SpotPostingLike,
    additional: tuple[str, ...] = (),
) -> tuple[str, ...]:
    values = tuple(
        sorted(set((posting.source_hash, posting.content_hash, *additional)))
    )
    for value in values:
        _require_hash(value, "spot_posting.source_hash")
    return values


def adapt_spot_posting(
    posting: SpotPostingLike,
    *,
    evidence_hashes: tuple[str, ...] = (),
    evidence_metadata: tuple[tuple[str, str], ...] = (),
) -> tuple[PortfolioEventDraft, ...]:
    """Adapt a standalone spot posting without importing the spot engine.

    Position-changing and liquidation postings require the encompassing
    ``CorporateActionApplication`` so their exact before/after basis can be
    validated; use :func:`adapt_corporate_action_application` for those.
    """

    posting_type = _enum_text(posting.posting_type)
    occurred_at = _spot_timestamp(posting.occurred_at)
    _decimal(posting.cash_delta, "spot_posting.cash_delta")
    _decimal(posting.tax_amount, "spot_posting.tax_amount", nonnegative=True)
    sources = _spot_evidence_hashes(posting, evidence_hashes)
    metadata = tuple(sorted((("spot_posting_type", posting_type), *evidence_metadata)))

    def draft(
        *,
        event_id: str,
        event_type: PortfolioEventType,
        cash_deltas: tuple[CashDelta, ...] = (),
    ) -> PortfolioEventDraft:
        return PortfolioEventDraft(
            event_id=event_id,
            event_type=event_type,
            occurred_at=occurred_at,
            currency=posting.currency,
            cash_deltas=cash_deltas,
            instrument_id=posting.instrument_id,
            asset_class=AssetClass.SPOT,
            source_hashes=sources,
            metadata=metadata,
        )

    if posting_type in {"DIVIDEND_CASHFLOW", "DIVIDEND_COMPENSATION"}:
        gross_cash = posting.cash_delta + posting.tax_amount
        event_type = (
            PortfolioEventType.DIVIDEND_INCOME
            if posting_type == "DIVIDEND_CASHFLOW"
            else PortfolioEventType.SHORT_DIVIDEND_COMPENSATION
        )
        drafts: list[PortfolioEventDraft] = [
            draft(
                event_id=f"{posting.posting_id}:gross",
                event_type=event_type,
                cash_deltas=(CashDelta(posting.currency, gross_cash),),
            )
        ]
        if posting.tax_amount != _ZERO:
            drafts.append(
                draft(
                    event_id=f"{posting.posting_id}:tax",
                    event_type=PortfolioEventType.TAX,
                    cash_deltas=(CashDelta(posting.currency, -posting.tax_amount),),
                )
            )
        return tuple(drafts)
    if posting_type == "BORROW_COST":
        if posting.cash_delta >= _ZERO or posting.tax_amount != _ZERO:
            raise PortfolioAccountingError("spot_borrow_posting_invalid")
        return (
            draft(
                event_id=posting.posting_id,
                event_type=PortfolioEventType.BORROW_COST,
                cash_deltas=(CashDelta(posting.currency, posting.cash_delta),),
            ),
        )
    if posting_type == "CORPORATE_ACTION_TAX":
        tax = posting.tax_amount or -posting.cash_delta
        if tax <= _ZERO:
            raise PortfolioAccountingError("spot_tax_posting_invalid")
        return (
            draft(
                event_id=posting.posting_id,
                event_type=PortfolioEventType.TAX,
                cash_deltas=(CashDelta(posting.currency, -tax),),
            ),
        )
    if posting_type == "TRADE_REJECTION":
        return (
            draft(
                event_id=posting.posting_id,
                event_type=PortfolioEventType.EXECUTION_ATTEMPT,
            ),
        )
    if posting_type in {
        "POSITION_TRANSFORM",
        "REPLACEMENT_DELIVERY",
        "LIQUIDATION_CASHFLOW",
        "RIGHTS_ENTITLEMENT",
        "RIGHTS_SUBSCRIPTION",
        "CASH_IN_LIEU",
        "MERGER_CASH",
        "BORROW_RECALL",
        "FORCED_BUY_IN",
    }:
        raise PortfolioAccountingError(
            "spot_position_posting_requires_corporate_action_application"
        )
    raise PortfolioAccountingError(f"spot_posting_type_unsupported:{posting_type}")


def collateral_income_event(
    *,
    event_id: str,
    occurred_at: str,
    currency: str,
    amount: Decimal,
    source_hashes: tuple[str, ...],
) -> PortfolioEventDraft:
    """Post explicitly modeled interest/rebate earned on collateral."""

    _decimal(amount, "collateral_income.amount", positive=True)
    return PortfolioEventDraft(
        event_id=event_id,
        event_type=PortfolioEventType.COLLATERAL_INCOME,
        occurred_at=occurred_at,
        currency=currency,
        cash_deltas=(CashDelta(currency, amount),),
        source_hashes=source_hashes,
        metadata=(("income_type", "COLLATERAL_INCOME"),),
    )


def adapt_borrow_recall_application(
    application: BorrowRecallApplicationLike,
) -> tuple[PortfolioEventDraft, ...]:
    """Project a forced buy-in as notice, trade principal, and explicit cost."""

    if not isinstance(application, BorrowRecallApplicationLike):
        raise PortfolioAccountingError("borrow_recall_application_protocol_invalid")
    for name in (
        "recall_hash",
        "execution_quote_hash",
        "book_before_hash",
        "book_after_hash",
    ):
        _require_hash(getattr(application, name), f"borrow_recall.{name}")
    covered = _decimal(
        application.covered_quantity,
        "borrow_recall.covered_quantity",
        positive=True,
    )
    execution_price = _decimal(
        application.execution_price,
        "borrow_recall.execution_price",
        positive=True,
    )
    if (
        _spot_book_binding_hash(application.book_before) != application.book_before_hash
        or _spot_book_binding_hash(application.book_after)
        != application.book_after_hash
    ):
        raise PortfolioAccountingError("borrow_recall_book_hash_mismatch")
    postings = tuple(application.postings)
    if len(postings) != 2:
        raise PortfolioAccountingError("borrow_recall_posting_count_invalid")
    notice = next(
        (item for item in postings if _enum_text(item.posting_type) == "BORROW_RECALL"),
        None,
    )
    buy_in = next(
        (item for item in postings if _enum_text(item.posting_type) == "FORCED_BUY_IN"),
        None,
    )
    if notice is None or buy_in is None:
        raise PortfolioAccountingError("borrow_recall_posting_types_invalid")
    if (
        notice.instrument_id != buy_in.instrument_id
        or notice.currency != buy_in.currency
        or notice.quantity_delta != _ZERO
        or notice.cash_delta != _ZERO
        or notice.tax_amount != _ZERO
        or buy_in.quantity_delta != covered
        or buy_in.tax_amount != _ZERO
        or buy_in.cash_delta >= _ZERO
        or notice.source_hash != application.recall_hash
        or buy_in.source_hash != application.execution_quote_hash
    ):
        raise PortfolioAccountingError("borrow_recall_posting_economics_invalid")
    before_positions = {
        item.instrument_id: item for item in application.book_before.positions
    }
    after_positions = {
        item.instrument_id: item for item in application.book_after.positions
    }
    before = before_positions.get(buy_in.instrument_id)
    if before is None or before.quantity >= _ZERO:
        raise PortfolioAccountingError("borrow_recall_short_position_required")
    if covered > abs(before.quantity):
        raise PortfolioAccountingError("borrow_recall_quantity_exceeds_short")
    after = after_positions.get(buy_in.instrument_id)
    after_quantity = _ZERO if after is None else after.quantity
    after_basis = _ZERO if after is None else after.total_cost_basis
    expected_after_quantity = before.quantity + covered
    expected_after_basis = before.total_cost_basis * (
        abs(expected_after_quantity) / abs(before.quantity)
    )
    if (
        after_quantity != expected_after_quantity
        or after_basis != expected_after_basis
        or (after is not None and after.currency != before.currency)
        or set(before_positions) - {buy_in.instrument_id}
        != set(after_positions) - {buy_in.instrument_id}
        or any(
            before_positions[key] != after_positions[key]
            for key in set(before_positions) - {buy_in.instrument_id}
        )
    ):
        raise PortfolioAccountingError("borrow_recall_book_diff_invalid")
    before_cash = {item.currency: item.amount for item in application.book_before.cash}
    after_cash = {item.currency: item.amount for item in application.book_after.cash}
    expected_cash_diff = {
        currency: after_cash.get(currency, _ZERO) - before_cash.get(currency, _ZERO)
        for currency in set(before_cash) | set(after_cash)
    }
    if expected_cash_diff.get(buy_in.currency, _ZERO) != buy_in.cash_delta or any(
        amount != _ZERO
        for currency, amount in expected_cash_diff.items()
        if currency != buy_in.currency
    ):
        raise PortfolioAccountingError("borrow_recall_cash_diff_invalid")
    principal = covered * execution_price
    total_cost = -buy_in.cash_delta
    if total_cost < principal:
        raise PortfolioAccountingError("borrow_recall_cost_below_principal")
    additional_cost = total_cost - principal
    all_sources = tuple(
        sorted(
            {
                application.recall_hash,
                application.execution_quote_hash,
                application.book_before_hash,
                application.book_after_hash,
                notice.content_hash,
                buy_in.content_hash,
            }
        )
    )
    metadata = (
        ("book_after_hash", application.book_after_hash),
        ("book_before_hash", application.book_before_hash),
        ("recall_hash", application.recall_hash),
    )
    drafts: list[PortfolioEventDraft] = [
        PortfolioEventDraft(
            event_id=notice.posting_id,
            event_type=PortfolioEventType.EXECUTION_ATTEMPT,
            occurred_at=_spot_timestamp(notice.occurred_at),
            instrument_id=notice.instrument_id,
            asset_class=AssetClass.SPOT,
            source_hashes=all_sources,
            metadata=(
                *metadata,
                ("spot_posting_type", "BORROW_RECALL"),
            ),
        ),
        trade_event(
            event_id=buy_in.posting_id,
            occurred_at=_spot_timestamp(buy_in.occurred_at),
            asset_class=AssetClass.SPOT,
            instrument_id=buy_in.instrument_id,
            currency=buy_in.currency,
            quantity_delta=covered,
            price=execution_price,
            source_hashes=all_sources,
        ),
    ]
    if additional_cost > _ZERO:
        drafts.append(
            PortfolioEventDraft(
                event_id=f"{buy_in.posting_id}:recall-cost",
                event_type=PortfolioEventType.EXECUTION_COST,
                occurred_at=_spot_timestamp(buy_in.occurred_at),
                currency=buy_in.currency,
                cash_deltas=(CashDelta(buy_in.currency, -additional_cost),),
                instrument_id=buy_in.instrument_id,
                asset_class=AssetClass.SPOT,
                source_hashes=all_sources,
                metadata=(
                    *metadata,
                    ("cost_component", "borrow_recall_penalty_and_commission"),
                    ("spot_posting_type", "FORCED_BUY_IN"),
                ),
            )
        )
    published_cash = sum(
        (
            delta.amount
            for draft in drafts
            for delta in draft.cash_deltas
            if delta.currency == buy_in.currency
        ),
        start=_ZERO,
    )
    if published_cash != buy_in.cash_delta:
        raise PortfolioAccountingError("borrow_recall_published_cash_mismatch")
    return tuple(drafts)


def adapt_corporate_action_application(
    application: CorporateActionApplicationLike,
    *,
    mark_prices_after: Mapping[str, Decimal] | None = None,
) -> tuple[PortfolioEventDraft, ...]:
    """Publish an exact ``SpotBook`` before/after diff as ledger events.

    Cash postings are split gross-versus-tax.  Position events retain absolute
    before/after quantity and total basis, so replay rejects application to the
    wrong source book.  When an observed post-action mark is not provided,
    replay conserves the pre-action market value across split, spin-off, and
    replacement legs under the common action hash.
    """

    for field_name in ("action_hash", "book_before_hash", "book_after_hash"):
        _require_hash(
            getattr(application, field_name),
            f"corporate_action_application.{field_name}",
        )
    if (
        _spot_book_binding_hash(application.book_before) != application.book_before_hash
        or _spot_book_binding_hash(application.book_after)
        != application.book_after_hash
    ):
        raise PortfolioAccountingError(
            "corporate_action_application_book_hash_mismatch"
        )
    postings = tuple(application.postings)
    if len({item.posting_id for item in postings}) != len(postings):
        raise PortfolioAccountingError("corporate_action_posting_id_duplicate")
    if any(item.source_hash != application.action_hash for item in postings):
        raise PortfolioAccountingError("corporate_action_posting_action_hash_mismatch")
    before = {item.instrument_id: item for item in application.book_before.positions}
    after = {item.instrument_id: item for item in application.book_after.positions}
    before_cash = {item.currency: item.amount for item in application.book_before.cash}
    after_cash = {item.currency: item.amount for item in application.book_after.cash}
    changed_ids = {
        instrument_id
        for instrument_id in set(before) | set(after)
        if before.get(instrument_id) != after.get(instrument_id)
    }
    if not postings:
        if changed_ids or before_cash != after_cash:
            raise PortfolioAccountingError(
                "corporate_action_application_changed_without_posting"
            )
        return ()
    all_sources = tuple(
        sorted(
            {
                application.action_hash,
                application.book_before_hash,
                application.book_after_hash,
                *(item.source_hash for item in postings),
                *(item.content_hash for item in postings),
            }
        )
    )
    for source_hash in all_sources:
        _require_hash(source_hash, "corporate_action_application.source_hash")
    mark_prices = dict(mark_prices_after or {})
    for mark in mark_prices.values():
        _decimal(mark, "corporate_action_application.mark_price", positive=True)

    def posting_for(instrument_id: str) -> SpotPostingLike:
        candidates = [
            item
            for item in postings
            if item.instrument_id == instrument_id
            or item.related_instrument_id == instrument_id
        ]
        if not candidates:
            raise PortfolioAccountingError(
                f"corporate_action_position_posting_missing:{instrument_id}"
            )
        return candidates[0]

    terminal_postings = {
        item.instrument_id: item
        for item in postings
        if _enum_text(item.posting_type) == "LIQUIDATION_CASHFLOW"
    }
    corporate_cash_postings = tuple(
        item
        for item in postings
        if _enum_text(item.posting_type)
        in {"RIGHTS_SUBSCRIPTION", "CASH_IN_LIEU", "MERGER_CASH"}
    )
    positive_gross_cash = {
        item.posting_id: item.cash_delta + item.tax_amount
        for item in corporate_cash_postings
        if item.cash_delta + item.tax_amount > _ZERO
    }
    total_positive_gross_cash = sum(positive_gross_cash.values(), start=_ZERO)
    total_before_basis = sum(
        (item.total_cost_basis for item in before.values()), start=_ZERO
    )
    total_after_basis = sum(
        (item.total_cost_basis for item in after.values()), start=_ZERO
    )
    disposed_basis = max(total_before_basis - total_after_basis, _ZERO)
    drafts: list[PortfolioEventDraft] = []
    ordered_ids = sorted(
        changed_ids,
        key=lambda instrument_id: (instrument_id not in before, instrument_id),
    )
    for instrument_id in ordered_ids:
        previous = before.get(instrument_id)
        final = after.get(instrument_id)
        posting = posting_for(instrument_id)
        before_quantity = _ZERO if previous is None else previous.quantity
        after_quantity = _ZERO if final is None else final.quantity
        before_basis = _ZERO if previous is None else previous.total_cost_basis
        after_basis = _ZERO if final is None else final.total_cost_basis
        currency = (
            final.currency
            if final is not None
            else previous.currency
            if previous is not None
            else posting.currency
        )
        terminal = terminal_postings.get(instrument_id)
        if terminal is not None:
            event_type = PortfolioEventType.TERMINAL_SETTLEMENT
            gross_cash = terminal.cash_delta + terminal.tax_amount
            cash_deltas = (
                ()
                if gross_cash == _ZERO
                else (CashDelta(terminal.currency, gross_cash),)
            )
            removed_basis = before_basis - after_basis
            sign = _ONE if before_quantity > _ZERO else -_ONE
            realized_pnl = gross_cash - (sign * removed_basis)
        else:
            event_type = (
                PortfolioEventType.REPLACEMENT_DELIVERY
                if previous is None or posting.related_instrument_id == instrument_id
                else PortfolioEventType.POSITION_TRANSFORMATION
            )
            cash_deltas = ()
            realized_pnl = _ZERO
        posting_type = _enum_text(posting.posting_type)
        metadata_values: list[tuple[str, str]] = [
            ("action_hash", application.action_hash),
            ("book_after_hash", application.book_after_hash),
            ("book_before_hash", application.book_before_hash),
            ("spot_posting_type", posting_type),
        ]
        related_id = posting.related_instrument_id
        associated_cash = [
            item
            for item in corporate_cash_postings
            if item.instrument_id == instrument_id
        ]
        if (
            event_type is PortfolioEventType.POSITION_TRANSFORMATION
            and associated_cash
            and related_id is None
        ):
            metadata_values.append(("cash_settled_transfer", "true"))
        if (
            event_type is PortfolioEventType.POSITION_TRANSFORMATION
            and before_quantity != _ZERO
            and related_id is not None
        ):
            if posting_type == "RIGHTS_SUBSCRIPTION":
                transfer_fraction = min(
                    abs(posting.quantity_delta / before_quantity), _ONE
                )
            elif before_basis > _ZERO:
                transfer_fraction = min(
                    posting.related_total_cost_basis / before_basis,
                    _ONE,
                )
            else:
                transfer_fraction = _ONE
            metadata_values.append(
                ("transfer_value_fraction", _decimal_text(transfer_fraction))
            )
        metadata = tuple(sorted(metadata_values))
        explicit_mark = mark_prices.get(instrument_id)
        if (
            explicit_mark is None
            and previous is None
            and final is not None
            and after_basis > _ZERO
        ):
            explicit_mark = after_basis / abs(after_quantity)
        drafts.append(
            PortfolioEventDraft(
                event_id=f"{posting.posting_id}:book:{instrument_id}",
                event_type=event_type,
                occurred_at=_spot_timestamp(posting.occurred_at),
                currency=currency,
                cash_deltas=cash_deltas,
                instrument_id=instrument_id,
                asset_class=AssetClass.SPOT,
                quantity_delta=after_quantity - before_quantity,
                mark_price=explicit_mark,
                realized_pnl=realized_pnl,
                position_quantity_before=before_quantity,
                position_quantity_after=after_quantity,
                total_cost_basis_before=before_basis,
                total_cost_basis_after=after_basis,
                source_hashes=all_sources,
                metadata=metadata,
            )
        )
        if terminal is not None and terminal.tax_amount != _ZERO:
            drafts.append(
                PortfolioEventDraft(
                    event_id=f"{terminal.posting_id}:tax",
                    event_type=PortfolioEventType.TAX,
                    occurred_at=_spot_timestamp(terminal.occurred_at),
                    currency=terminal.currency,
                    cash_deltas=(CashDelta(terminal.currency, -terminal.tax_amount),),
                    instrument_id=terminal.instrument_id,
                    asset_class=AssetClass.SPOT,
                    source_hashes=all_sources,
                    metadata=metadata,
                )
            )

    for posting in postings:
        posting_type = _enum_text(posting.posting_type)
        if posting_type in {"DIVIDEND_CASHFLOW", "DIVIDEND_COMPENSATION"}:
            evidence_metadata = (
                ("action_hash", application.action_hash),
                ("book_after_hash", application.book_after_hash),
                ("book_before_hash", application.book_before_hash),
            )
            drafts.extend(
                adapt_spot_posting(
                    posting,
                    evidence_hashes=all_sources,
                    evidence_metadata=evidence_metadata,
                )
            )
        elif posting_type in {
            "RIGHTS_SUBSCRIPTION",
            "CASH_IN_LIEU",
            "MERGER_CASH",
        }:
            gross_cash = posting.cash_delta + posting.tax_amount
            if gross_cash == _ZERO:
                continue
            allocated_basis = (
                disposed_basis
                * positive_gross_cash.get(posting.posting_id, _ZERO)
                / total_positive_gross_cash
                if total_positive_gross_cash > _ZERO
                else _ZERO
            )
            realized_pnl = gross_cash - allocated_basis if gross_cash > _ZERO else _ZERO
            metadata = (
                ("action_hash", application.action_hash),
                ("book_after_hash", application.book_after_hash),
                ("book_before_hash", application.book_before_hash),
                ("spot_posting_type", posting_type),
            )
            drafts.append(
                PortfolioEventDraft(
                    event_id=f"{posting.posting_id}:cash",
                    event_type=PortfolioEventType.CORPORATE_CASH,
                    occurred_at=_spot_timestamp(posting.occurred_at),
                    currency=posting.currency,
                    cash_deltas=(CashDelta(posting.currency, gross_cash),),
                    realized_pnl=realized_pnl,
                    source_hashes=all_sources,
                    metadata=metadata,
                )
            )
            if posting.tax_amount != _ZERO:
                drafts.append(
                    PortfolioEventDraft(
                        event_id=f"{posting.posting_id}:tax",
                        event_type=PortfolioEventType.TAX,
                        occurred_at=_spot_timestamp(posting.occurred_at),
                        currency=posting.currency,
                        cash_deltas=(CashDelta(posting.currency, -posting.tax_amount),),
                        instrument_id=posting.instrument_id,
                        asset_class=AssetClass.SPOT,
                        source_hashes=all_sources,
                        metadata=metadata,
                    )
                )

    expected_cash = {
        currency: after_cash.get(currency, _ZERO) - before_cash.get(currency, _ZERO)
        for currency in set(before_cash) | set(after_cash)
    }
    published_cash: dict[str, Decimal] = {}
    for draft in drafts:
        for delta in draft.cash_deltas:
            _add(published_cash, delta.currency, delta.amount)
    if any(
        published_cash.get(currency, _ZERO) != amount
        for currency, amount in expected_cash.items()
    ) or any(
        currency not in expected_cash and amount != _ZERO
        for currency, amount in published_cash.items()
    ):
        raise PortfolioAccountingError(
            "corporate_action_application_cash_diff_mismatch"
        )
    return tuple(drafts)


@dataclass(frozen=True, slots=True)
class CollateralAssetBalance:
    asset_id: str
    currency: str
    market_value: Decimal
    haircut: Decimal
    eligible: bool
    priority: int
    source_hash: str

    def __post_init__(self) -> None:
        _require_id(self.asset_id, "collateral_asset.asset_id")
        if not _CURRENCY.fullmatch(self.currency):
            raise PortfolioAccountingError("collateral_asset_currency_invalid")
        _decimal(self.market_value, "collateral_asset.market_value", nonnegative=True)
        _decimal(self.haircut, "collateral_asset.haircut", nonnegative=True)
        if self.haircut > _ONE:
            raise PortfolioAccountingError("collateral_asset_haircut_above_one")
        if not isinstance(self.eligible, bool):
            raise PortfolioAccountingError("collateral_asset_eligible_invalid")
        if (
            isinstance(self.priority, bool)
            or not isinstance(self.priority, int)
            or self.priority < 0
        ):
            raise PortfolioAccountingError("collateral_asset_priority_invalid")
        _require_hash(self.source_hash, "collateral_asset.source_hash")

    @property
    def eligible_value_native(self) -> Decimal:
        return self.market_value * (_ONE - self.haircut) if self.eligible else _ZERO


@dataclass(frozen=True, slots=True)
class CollateralWaterfallPolicy:
    policy_id: str
    version: str
    maximum_single_asset_fraction: Decimal = _ONE
    allow_default_shortfall: bool = False
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.policy_id, "collateral_policy.policy_id")
        _require_id(self.version, "collateral_policy.version")
        _decimal(
            self.maximum_single_asset_fraction,
            "collateral_policy.maximum_single_asset_fraction",
            positive=True,
        )
        if self.maximum_single_asset_fraction > _ONE:
            raise PortfolioAccountingError(
                "collateral_policy_single_asset_fraction_above_one"
            )
        if not isinstance(self.allow_default_shortfall, bool):
            raise PortfolioAccountingError(
                "collateral_policy_allow_default_shortfall_invalid"
            )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "policy_id": self.policy_id,
                    "version": self.version,
                    "maximum_single_asset_fraction": _decimal_text(
                        self.maximum_single_asset_fraction
                    ),
                    "allow_default_shortfall": self.allow_default_shortfall,
                },
                label="collateral_waterfall_policy",
            ),
        )


@dataclass(frozen=True, slots=True)
class CollateralAllocation:
    asset_id: str
    currency: str
    pledged_market_value: Decimal
    collateral_credit_base: Decimal
    haircut: Decimal
    fx_rate: Decimal
    priority: int
    source_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.asset_id, "collateral_allocation.asset_id")
        if not _CURRENCY.fullmatch(self.currency):
            raise PortfolioAccountingError("collateral_allocation_currency_invalid")
        for name in (
            "pledged_market_value",
            "collateral_credit_base",
            "fx_rate",
        ):
            _decimal(
                getattr(self, name),
                f"collateral_allocation.{name}",
                positive=True,
            )
        _decimal(
            self.haircut,
            "collateral_allocation.haircut",
            nonnegative=True,
        )
        if self.haircut >= _ONE:
            raise PortfolioAccountingError("collateral_allocation_haircut_invalid")
        if (
            isinstance(self.priority, bool)
            or not isinstance(self.priority, int)
            or self.priority < 0
        ):
            raise PortfolioAccountingError("collateral_allocation_priority_invalid")
        _require_hash(self.source_hash, "collateral_allocation.source_hash")
        if self.collateral_credit_base != (
            self.pledged_market_value * self.fx_rate * (_ONE - self.haircut)
        ):
            raise PortfolioAccountingError(
                "collateral_allocation_credit_identity_failed"
            )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "asset_id": self.asset_id,
                    "currency": self.currency,
                    "pledged_market_value": _decimal_text(self.pledged_market_value),
                    "collateral_credit_base": _decimal_text(
                        self.collateral_credit_base
                    ),
                    "haircut": _decimal_text(self.haircut),
                    "fx_rate": _decimal_text(self.fx_rate),
                    "priority": self.priority,
                    "source_hash": self.source_hash,
                },
                label="collateral_allocation",
            ),
        )


@dataclass(frozen=True, slots=True)
class CollateralWaterfallResult:
    required_credit_base: Decimal
    allocations: tuple[CollateralAllocation, ...]
    provided_credit_base: Decimal
    default_shortfall_base: Decimal
    policy_hash: str
    source_hashes: tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "required_credit_base",
            "provided_credit_base",
            "default_shortfall_base",
        ):
            _decimal(
                getattr(self, name),
                f"collateral_waterfall.{name}",
                nonnegative=True,
            )
        if self.provided_credit_base + self.default_shortfall_base != (
            self.required_credit_base
        ):
            raise PortfolioAccountingError("collateral_waterfall_identity_failed")
        if any(not isinstance(item, CollateralAllocation) for item in self.allocations):
            raise PortfolioAccountingError("collateral_waterfall_allocation_invalid")
        if (
            tuple(
                sorted(
                    self.allocations, key=lambda item: (item.priority, item.asset_id)
                )
            )
            != self.allocations
        ):
            raise PortfolioAccountingError(
                "collateral_waterfall_allocations_not_ordered"
            )
        if len({item.asset_id for item in self.allocations}) != len(self.allocations):
            raise PortfolioAccountingError("collateral_waterfall_allocation_duplicate")
        if (
            sum(
                (item.collateral_credit_base for item in self.allocations),
                start=_ZERO,
            )
            != self.provided_credit_base
        ):
            raise PortfolioAccountingError(
                "collateral_waterfall_allocation_sum_mismatch"
            )
        _require_hash(self.policy_hash, "collateral_waterfall.policy_hash")
        if tuple(sorted(set(self.source_hashes))) != self.source_hashes:
            raise PortfolioAccountingError("collateral_waterfall_source_hashes_invalid")
        for source_hash in self.source_hashes:
            _require_hash(source_hash, "collateral_waterfall.source_hash")
        if not {item.source_hash for item in self.allocations}.issubset(
            self.source_hashes
        ):
            raise PortfolioAccountingError(
                "collateral_waterfall_allocation_source_unbound"
            )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "required_credit_base": _decimal_text(self.required_credit_base),
                    "allocations": [
                        {
                            "asset_id": item.asset_id,
                            "currency": item.currency,
                            "pledged_market_value": _decimal_text(
                                item.pledged_market_value
                            ),
                            "collateral_credit_base": _decimal_text(
                                item.collateral_credit_base
                            ),
                            "haircut": _decimal_text(item.haircut),
                            "fx_rate": _decimal_text(item.fx_rate),
                            "priority": item.priority,
                            "source_hash": item.source_hash,
                        }
                        for item in self.allocations
                    ],
                    "provided_credit_base": _decimal_text(self.provided_credit_base),
                    "default_shortfall_base": _decimal_text(
                        self.default_shortfall_base
                    ),
                    "policy_hash": self.policy_hash,
                    "source_hashes": list(self.source_hashes),
                },
                label="collateral_waterfall_result",
            ),
        )


def allocate_collateral_waterfall(
    *,
    required_credit_base: Decimal,
    assets: tuple[CollateralAssetBalance, ...],
    fx_rates: Mapping[str, Decimal],
    policy: CollateralWaterfallPolicy,
) -> CollateralWaterfallResult:
    _decimal(
        required_credit_base,
        "collateral_waterfall.required_credit_base",
        nonnegative=True,
    )
    if not isinstance(policy, CollateralWaterfallPolicy):
        raise PortfolioAccountingError("collateral_waterfall_policy_required")
    if any(not isinstance(item, CollateralAssetBalance) for item in assets):
        raise PortfolioAccountingError("collateral_waterfall_asset_invalid")
    if len({item.asset_id for item in assets}) != len(assets):
        raise PortfolioAccountingError("collateral_waterfall_asset_duplicate")
    remaining = required_credit_base
    allocations: list[CollateralAllocation] = []
    ordered = tuple(sorted(assets, key=lambda item: (item.priority, item.asset_id)))
    cap = required_credit_base * policy.maximum_single_asset_fraction
    for asset in ordered:
        if remaining == _ZERO or not asset.eligible:
            continue
        rate = fx_rates.get(asset.currency)
        if rate is None:
            raise PortfolioAccountingError(
                f"collateral_waterfall_fx_missing:{asset.currency}"
            )
        _decimal(rate, "collateral_waterfall.fx_rate", positive=True)
        available_credit = asset.eligible_value_native * rate
        credit = min(available_credit, remaining, cap)
        if credit == _ZERO:
            continue
        if asset.haircut == _ONE:
            continue
        pledged = credit / (rate * (_ONE - asset.haircut))
        allocations.append(
            CollateralAllocation(
                asset_id=asset.asset_id,
                currency=asset.currency,
                pledged_market_value=pledged,
                collateral_credit_base=credit,
                haircut=asset.haircut,
                fx_rate=rate,
                priority=asset.priority,
                source_hash=asset.source_hash,
            )
        )
        remaining -= credit
    if remaining > _ZERO and not policy.allow_default_shortfall:
        raise PortfolioAccountingError("collateral_waterfall_insufficient")
    return CollateralWaterfallResult(
        required_credit_base=required_credit_base,
        allocations=tuple(allocations),
        provided_credit_base=required_credit_base - remaining,
        default_shortfall_base=remaining,
        policy_hash=policy.content_hash,
        source_hashes=tuple(sorted({item.source_hash for item in ordered})),
    )


@dataclass(frozen=True, slots=True)
class FundingFxCurrencyRevaluation:
    currency: str
    principal_native: Decimal
    locked_principal_base: Decimal
    current_principal_base: Decimal
    translation_pnl: Decimal

    def __post_init__(self) -> None:
        if not _CURRENCY.fullmatch(self.currency):
            raise PortfolioAccountingError("funding_fx_currency_invalid")
        for name in (
            "principal_native",
            "locked_principal_base",
            "current_principal_base",
            "translation_pnl",
        ):
            _decimal(getattr(self, name), f"funding_fx.{name}")
        if self.current_principal_base - self.locked_principal_base != (
            self.translation_pnl
        ):
            raise PortfolioAccountingError(
                "funding_fx_currency_translation_identity_failed"
            )


@dataclass(frozen=True, slots=True)
class FundingFxRevaluation:
    ledger_hash: str
    current_fx_source_hash: str
    currencies: tuple[FundingFxCurrencyRevaluation, ...]
    total_translation_pnl: Decimal
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_hash(self.ledger_hash, "funding_fx.ledger_hash")
        _require_hash(self.current_fx_source_hash, "funding_fx.current_fx_source_hash")
        if any(
            not isinstance(item, FundingFxCurrencyRevaluation)
            for item in self.currencies
        ):
            raise PortfolioAccountingError("funding_fx_currency_row_invalid")
        if tuple(sorted(self.currencies, key=lambda item: item.currency)) != (
            self.currencies
        ) or len({item.currency for item in self.currencies}) != len(self.currencies):
            raise PortfolioAccountingError("funding_fx_currencies_invalid")
        expected = sum((item.translation_pnl for item in self.currencies), start=_ZERO)
        if expected != self.total_translation_pnl:
            raise PortfolioAccountingError("funding_fx_translation_identity_failed")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "ledger_hash": self.ledger_hash,
                    "current_fx_source_hash": self.current_fx_source_hash,
                    "currencies": [
                        {
                            "currency": item.currency,
                            "principal_native": _decimal_text(item.principal_native),
                            "locked_principal_base": _decimal_text(
                                item.locked_principal_base
                            ),
                            "current_principal_base": _decimal_text(
                                item.current_principal_base
                            ),
                            "translation_pnl": _decimal_text(item.translation_pnl),
                        }
                        for item in self.currencies
                    ],
                    "total_translation_pnl": _decimal_text(self.total_translation_pnl),
                },
                label="funding_fx_revaluation",
            ),
        )


def revalue_funding_fx(
    ledger: UnifiedPortfolioLedger,
    *,
    current_fx_rates: Mapping[str, Decimal],
    current_fx_source_hash: str,
) -> FundingFxRevaluation:
    ledger.verify_integrity()
    _require_hash(current_fx_source_hash, "funding_fx.current_fx_source_hash")
    native: dict[str, Decimal] = {}
    locked: dict[str, Decimal] = {}
    for event in ledger.events:
        if event.event_type is not PortfolioEventType.FUNDING:
            continue
        conversions = {item.currency: item for item in event.external_flow_conversions}
        for delta in event.cash_deltas:
            _add(native, delta.currency, delta.amount)
            if delta.currency == ledger.base_currency:
                _add(locked, delta.currency, delta.amount)
            else:
                evidence = conversions.get(delta.currency)
                if evidence is None:
                    raise PortfolioAccountingError(
                        "funding_fx_conversion_evidence_missing"
                    )
                _add(locked, delta.currency, delta.amount * evidence.fx_rate)
    rows: list[FundingFxCurrencyRevaluation] = []
    for currency, principal in sorted(native.items()):
        rate = (
            _ONE if currency == ledger.base_currency else current_fx_rates.get(currency)
        )
        if rate is None:
            raise PortfolioAccountingError(f"funding_fx_rate_missing:{currency}")
        _decimal(rate, "funding_fx.current_rate", positive=True)
        current = principal * rate
        locked_value = locked.get(currency, _ZERO)
        rows.append(
            FundingFxCurrencyRevaluation(
                currency=currency,
                principal_native=principal,
                locked_principal_base=locked_value,
                current_principal_base=current,
                translation_pnl=current - locked_value,
            )
        )
    return FundingFxRevaluation(
        ledger_hash=ledger.content_hash,
        current_fx_source_hash=current_fx_source_hash,
        currencies=tuple(rows),
        total_translation_pnl=sum((item.translation_pnl for item in rows), start=_ZERO),
    )


@dataclass(frozen=True, slots=True)
class AdvancedAccountingBundle:
    factory_hash: str
    economic_bundle_hash: str
    audit_event: PortfolioEventDraft
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_hash(self.factory_hash, "accounting_bundle.factory_hash")
        _require_hash(
            self.economic_bundle_hash, "accounting_bundle.economic_bundle_hash"
        )
        if self.audit_event.event_type not in _ADVANCED_AUDIT_EVENT_TYPES:
            raise PortfolioAccountingError("accounting_bundle_event_type_invalid")
        metadata = dict(self.audit_event.metadata)
        if (
            metadata.get("accounting_factory_hash") != self.factory_hash
            or metadata.get("economic_bundle_hash") != self.economic_bundle_hash
        ):
            raise PortfolioAccountingError("accounting_bundle_receipt_mismatch")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "factory_hash": self.factory_hash,
                    "economic_bundle_hash": self.economic_bundle_hash,
                    "audit_event": self.audit_event.identity_payload(),
                },
                label="advanced_accounting_bundle",
            ),
        )


@dataclass(frozen=True, slots=True)
class InvariantAccountingFactory:
    factory_id: str
    version: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.factory_id, "accounting_factory.factory_id")
        _require_id(self.version, "accounting_factory.version")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {"factory_id": self.factory_id, "version": self.version},
                label="invariant_accounting_factory",
            ),
        )

    def audit_bundle(
        self,
        *,
        event_id: str,
        event_type: PortfolioEventType,
        occurred_at: str,
        economic_event_hashes: tuple[str, ...],
        details: Mapping[str, str],
    ) -> AdvancedAccountingBundle:
        if event_type not in _ADVANCED_AUDIT_EVENT_TYPES:
            raise PortfolioAccountingError("accounting_factory_advanced_event_required")
        if tuple(sorted(set(economic_event_hashes))) != economic_event_hashes:
            raise PortfolioAccountingError(
                "accounting_factory_economic_hashes_not_sorted_unique"
            )
        for item in economic_event_hashes:
            _require_hash(item, "accounting_factory.economic_event_hash")
        normalized_details = tuple(sorted(details.items()))
        for key, value in normalized_details:
            _require_id(key, "accounting_factory.detail_key")
            if not isinstance(value, str):
                raise PortfolioAccountingError(
                    "accounting_factory_detail_value_invalid"
                )
        economic_bundle_hash = sha256_prefixed(
            {
                "event_type": event_type.value,
                "economic_event_hashes": list(economic_event_hashes),
                "details": dict(normalized_details),
            },
            label="advanced_economic_event_bundle",
        )
        metadata = tuple(
            sorted(
                (
                    ("accounting_factory_hash", self.content_hash),
                    ("economic_bundle_hash", economic_bundle_hash),
                    *normalized_details,
                )
            )
        )
        audit = PortfolioEventDraft(
            event_id=event_id,
            event_type=event_type,
            occurred_at=occurred_at,
            source_hashes=tuple(sorted((*economic_event_hashes, economic_bundle_hash))),
            metadata=metadata,
        )
        return AdvancedAccountingBundle(
            factory_hash=self.content_hash,
            economic_bundle_hash=economic_bundle_hash,
            audit_event=audit,
        )


def publish_advanced_accounting_bundle(
    ledger: UnifiedPortfolioLedger,
    bundle: AdvancedAccountingBundle,
) -> UnifiedPortfolioLedger:
    if not isinstance(bundle, AdvancedAccountingBundle):
        raise PortfolioAccountingError("advanced_accounting_bundle_required")
    return ledger.publish(bundle.audit_event)
