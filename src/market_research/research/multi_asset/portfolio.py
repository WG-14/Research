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
from decimal import Decimal
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
            if after_quantity == _ZERO:
                transferred_value = current_value
                retained_value = _ZERO
            elif before_basis > _ZERO and after_basis < before_basis:
                retained_value = current_value * after_basis / before_basis
                transferred_value = current_value - retained_value
            if transferred_value != _ZERO:
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
                multiplier=_ONE,
            )
            positions[key] = deliverable
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
) -> PortfolioEventDraft:
    return PortfolioEventDraft(
        event_id=event_id,
        event_type=PortfolioEventType.FUNDING,
        occurred_at=occurred_at,
        cash_deltas=cash_deltas,
        external_flow_conversions=conversion_evidence,
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
    if expected_exercised > _ZERO:
        scale = expected_exercised * multiplier
        if settlement_type == "CASH":
            expected_cash = position_sign * intrinsic * scale
        else:
            expected_deliverable_id = contract.deliverable_asset_id
            if expected_deliverable_id is None:
                raise PortfolioAccountingError(
                    "option_lifecycle_deliverable_id_missing"
                )
            if option_type == "CALL":
                expected_delivery = position_sign * scale
                expected_cash = -position_sign * strike * scale
            else:
                expected_delivery = -position_sign * scale
                expected_cash = position_sign * strike * scale
    if (
        event.cash_delta != expected_cash
        or event.deliverable_quantity_delta != expected_delivery
        or event.deliverable_asset_id != expected_deliverable_id
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
    if delivery_quantity != _ZERO:
        if event.deliverable_asset_id is None:
            raise PortfolioAccountingError("option_lifecycle_deliverable_id_missing")
        basis_price = (
            abs(event.cash_delta / delivery_quantity)
            if event.cash_delta != _ZERO
            else settlement_price
        )
        mark_price_value = settlement_price
        deliverable_currency = currency
        deliverable_class = deliverable_asset_class
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
    postings = tuple(application.postings)
    if len({item.posting_id for item in postings}) != len(postings):
        raise PortfolioAccountingError("corporate_action_posting_id_duplicate")
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
        metadata = (
            ("action_hash", application.action_hash),
            ("book_after_hash", application.book_after_hash),
            ("book_before_hash", application.book_before_hash),
            ("spot_posting_type", _enum_text(posting.posting_type)),
        )
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
                mark_price=mark_prices.get(instrument_id),
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
