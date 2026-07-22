"""Point-in-time multi-asset exposure revaluation for offline research.

Unlike the legacy aggregation DTO, this engine accepts economic positions and
revalues every one against an immutable :class:`MarketState`.  Spot valuation
is supplied here; futures and options retain their product-specific semantics
behind a typed adapter protocol.  The engine, rather than the caller, applies
signed quantity, contract multiplier, FX conversion, bucketing, offsets, and
portfolio invariants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, Sequence, runtime_checkable

from ..hashing import sha256_prefixed
from ..instrument_kinds import InstrumentKind
from .domain import (
    ContractSpecification,
    Instrument,
    InstrumentRegistry,
    InstrumentRelationshipType,
)
from .market_state import MarketState, MarketStateError


MULTI_ASSET_EXPOSURE_SCHEMA_VERSION = 2
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_BUCKET_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,254}$")
_CURRENCY = re.compile(r"^[A-Z][A-Z0-9]{2,11}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_ZERO = Decimal("0")
_ONE = Decimal("1")


ProductCatalog = InstrumentRegistry


class ExposureEngineError(ValueError):
    """A position, valuation, or portfolio invariant is not reproducible."""


class ExposureDimension(StrEnum):
    UNDERLYING = "UNDERLYING"
    CURRENCY = "CURRENCY"
    EXPIRY = "EXPIRY"
    LIQUIDITY = "LIQUIDITY"


class LiquidityBucket(StrEnum):
    DEEP = "DEEP"
    ADEQUATE = "ADEQUATE"
    CONSTRAINED = "CONSTRAINED"
    INSUFFICIENT = "INSUFFICIENT"
    UNAVAILABLE = "UNAVAILABLE"


GREEK_CASH_UNIT_CONTRACT = {
    "contract_id": "multi_asset.cash_greeks.v1",
    "delta": "valuation_currency_per_1pct_underlying_move",
    "gamma": "valuation_currency_per_1pct_underlying_move_squared",
    "vega": "valuation_currency_per_1_volatility_point",
    "theta": "valuation_currency_per_calendar_day",
    "rho": "valuation_currency_per_100_basis_point_rate_move",
}
GREEK_CASH_UNIT_CONTRACT_HASH = sha256_prefixed(
    GREEK_CASH_UNIT_CONTRACT, label="multi_asset_greek_cash_unit_contract"
)


def _timestamp(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ExposureEngineError(f"{field_name}_invalid_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExposureEngineError(f"{field_name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: str, field_name: str) -> str:
    return _timestamp(value, field_name).isoformat()


def _require_id(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _STABLE_ID.fullmatch(value):
        raise ExposureEngineError(f"{field_name}_invalid_stable_id")


def _require_bucket_key(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _BUCKET_KEY.fullmatch(value):
        raise ExposureEngineError(f"{field_name}_invalid")


def _require_currency(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _CURRENCY.fullmatch(value):
        raise ExposureEngineError(f"{field_name}_invalid_currency")


def _require_hash(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ExposureEngineError(f"{field_name}_invalid_hash")


def _decimal(
    value: Decimal,
    field_name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ExposureEngineError(f"{field_name}_must_be_finite_decimal")
    if positive and value <= _ZERO:
        raise ExposureEngineError(f"{field_name}_must_be_positive")
    if nonnegative and value < _ZERO:
        raise ExposureEngineError(f"{field_name}_must_be_nonnegative")


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _hash_payload(items: Sequence[object], *, label: str) -> str:
    return sha256_prefixed(list(items), label=label)


@dataclass(frozen=True, slots=True)
class ExposurePosition:
    """A source position before valuation; quantity is signed."""

    position_id: str
    instrument_id: str
    quantity: Decimal
    quantity_unit: str
    multiplier: Decimal
    currency: str
    source_hash: str
    opened_at: str
    closed_at: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.position_id, "position.position_id")
        _require_id(self.instrument_id, "position.instrument_id")
        _decimal(self.quantity, "position.quantity")
        if self.quantity == _ZERO:
            raise ExposureEngineError("position.quantity_must_be_nonzero")
        _require_id(self.quantity_unit, "position.quantity_unit")
        _decimal(self.multiplier, "position.multiplier", positive=True)
        _require_currency(self.currency, "position.currency")
        _require_hash(self.source_hash, "position.source_hash")
        opened = _timestamp_text(self.opened_at, "position.opened_at")
        object.__setattr__(self, "opened_at", opened)
        if self.closed_at is not None:
            closed = _timestamp_text(self.closed_at, "position.closed_at")
            if _timestamp(closed, "position.closed_at") <= _timestamp(
                opened, "position.opened_at"
            ):
                raise ExposureEngineError("position_active_range_invalid")
            object.__setattr__(self, "closed_at", closed)

    def active_at(self, as_of: str) -> bool:
        instant = _timestamp(as_of, "position.as_of")
        return _timestamp(self.opened_at, "position.opened_at") <= instant and (
            self.closed_at is None
            or instant < _timestamp(self.closed_at, "position.closed_at")
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "position_id": self.position_id,
            "instrument_id": self.instrument_id,
            "quantity": _decimal_text(self.quantity),
            "quantity_unit": self.quantity_unit,
            "multiplier": _decimal_text(self.multiplier),
            "currency": self.currency,
            "source_hash": self.source_hash,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
        }

    def position_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="multi_asset_source_position")


@dataclass(frozen=True, slots=True)
class UnitValuation:
    """Adapter output per one position unit and one multiplier unit.

    Greeks use the cash-sensitivity convention bound by
    ``GREEK_CASH_UNIT_CONTRACT_HASH``.  The engine applies signed quantity,
    multiplier, and base-currency FX only after validating all bindings.
    """

    instrument_id: str
    instrument_kind: InstrumentKind
    valuation_at: str
    currency: str
    quantity_unit: str
    price_unit: str
    mark_price: Decimal
    market_value: Decimal
    notional: Decimal
    delta: Decimal
    gamma: Decimal
    vega: Decimal
    theta: Decimal
    rho: Decimal
    margin: Decimal
    collateral: Decimal
    market_state_hash: str
    product_catalog_hash: str
    model_hash: str
    greek_unit_contract_hash: str = GREEK_CASH_UNIT_CONTRACT_HASH

    def __post_init__(self) -> None:
        _require_id(self.instrument_id, "unit_valuation.instrument_id")
        if self.instrument_kind not in {
            InstrumentKind.SPOT,
            InstrumentKind.FUTURE,
            InstrumentKind.OPTION,
        }:
            raise ExposureEngineError("unit_valuation.instrument_kind_unsupported")
        object.__setattr__(
            self,
            "valuation_at",
            _timestamp_text(self.valuation_at, "unit_valuation.valuation_at"),
        )
        _require_currency(self.currency, "unit_valuation.currency")
        _require_id(self.quantity_unit, "unit_valuation.quantity_unit")
        _require_id(self.price_unit, "unit_valuation.price_unit")
        for name in (
            "mark_price",
            "market_value",
            "notional",
            "delta",
            "gamma",
            "vega",
            "theta",
            "rho",
            "margin",
            "collateral",
        ):
            _decimal(
                getattr(self, name),
                f"unit_valuation.{name}",
                nonnegative=name in {"mark_price", "notional", "margin", "collateral"},
            )
        for name in (
            "market_state_hash",
            "product_catalog_hash",
            "model_hash",
            "greek_unit_contract_hash",
        ):
            _require_hash(getattr(self, name), f"unit_valuation.{name}")
        if self.greek_unit_contract_hash != GREEK_CASH_UNIT_CONTRACT_HASH:
            raise ExposureEngineError("unit_valuation_greek_contract_mismatch")

    def as_dict(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "instrument_kind": self.instrument_kind.value,
            "valuation_at": self.valuation_at,
            "currency": self.currency,
            "quantity_unit": self.quantity_unit,
            "price_unit": self.price_unit,
            "mark_price": _decimal_text(self.mark_price),
            "market_value": _decimal_text(self.market_value),
            "notional": _decimal_text(self.notional),
            "delta": _decimal_text(self.delta),
            "gamma": _decimal_text(self.gamma),
            "vega": _decimal_text(self.vega),
            "theta": _decimal_text(self.theta),
            "rho": _decimal_text(self.rho),
            "margin": _decimal_text(self.margin),
            "collateral": _decimal_text(self.collateral),
            "market_state_hash": self.market_state_hash,
            "product_catalog_hash": self.product_catalog_hash,
            "model_hash": self.model_hash,
            "greek_unit_contract_hash": self.greek_unit_contract_hash,
            "greek_units": dict(GREEK_CASH_UNIT_CONTRACT),
        }

    def valuation_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="multi_asset_unit_valuation")


@runtime_checkable
class ProductValuationAdapter(Protocol):
    """Typed boundary retaining product-specific pricing semantics."""

    @property
    def adapter_id(self) -> str: ...

    @property
    def adapter_version(self) -> str: ...

    @property
    def instrument_kind(self) -> InstrumentKind: ...

    @property
    def content_hash(self) -> str: ...

    def value(
        self,
        *,
        position: ExposurePosition,
        instrument: Instrument,
        contract_specification: ContractSpecification | None,
        market_state: MarketState,
        product_catalog_hash: str,
    ) -> UnitValuation: ...


@dataclass(frozen=True, slots=True)
class SpotValuationAdapter:
    """Direct spot mark adapter backed by ``MarketState.spots``."""

    adapter_id: str = "multi_asset.spot_mark"
    adapter_version: str = "2"
    instrument_kind: InstrumentKind = InstrumentKind.SPOT
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "adapter_id": self.adapter_id,
                    "adapter_version": self.adapter_version,
                    "instrument_kind": self.instrument_kind.value,
                    "market_value": "quantity_x_multiplier_x_spot_mark",
                    "delta": "cash_change_for_1pct_spot_move",
                },
                label="multi_asset_spot_valuation_adapter",
            ),
        )

    def value(
        self,
        *,
        position: ExposurePosition,
        instrument: Instrument,
        contract_specification: ContractSpecification | None,
        market_state: MarketState,
        product_catalog_hash: str,
    ) -> UnitValuation:
        if instrument.kind is not InstrumentKind.SPOT:
            raise ExposureEngineError("spot_adapter_instrument_kind_mismatch")
        if contract_specification is not None:
            raise ExposureEngineError("spot_adapter_contract_specification_forbidden")
        quote = market_state.spot_price(instrument.instrument_id)
        if quote.currency != instrument.currency:
            raise ExposureEngineError("spot_adapter_quote_currency_mismatch")
        expected_unit = f"{quote.currency}_per_{instrument.unit}"
        if quote.unit != expected_unit:
            raise ExposureEngineError(
                f"spot_adapter_quote_unit_mismatch:{expected_unit}"
            )
        return UnitValuation(
            instrument_id=instrument.instrument_id,
            instrument_kind=instrument.kind,
            valuation_at=market_state.valuation_at,
            currency=quote.currency,
            quantity_unit=instrument.unit,
            price_unit=quote.unit,
            mark_price=quote.price,
            market_value=quote.price,
            notional=quote.price,
            delta=quote.price * Decimal("0.01"),
            gamma=_ZERO,
            vega=_ZERO,
            theta=_ZERO,
            rho=_ZERO,
            margin=_ZERO,
            collateral=_ZERO,
            market_state_hash=market_state.state_hash(),
            product_catalog_hash=product_catalog_hash,
            model_hash=self.content_hash,
        )


@dataclass(frozen=True, slots=True)
class FuturesValuationAdapter:
    """Production futures adapter backed only by typed ``MarketState`` data."""

    margin_model_hash: str
    adapter_id: str = "multi_asset.futures_market_state"
    adapter_version: str = "2"
    instrument_kind: InstrumentKind = InstrumentKind.FUTURE
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_hash(self.margin_model_hash, "futures_adapter.margin_model_hash")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "adapter_id": self.adapter_id,
                    "adapter_version": self.adapter_version,
                    "instrument_kind": self.instrument_kind.value,
                    "margin_model_hash": self.margin_model_hash,
                    "mark_precedence": "settlement_then_last_then_mid",
                    "market_value": "zero_after_daily_variation_margin",
                    "notional": "mark_per_contract_unit",
                    "delta": "cash_change_for_1pct_underlying_move",
                    "multiplier_application": "exposure_engine_only",
                },
                label="multi_asset_futures_valuation_adapter",
            ),
        )

    def value(
        self,
        *,
        position: ExposurePosition,
        instrument: Instrument,
        contract_specification: ContractSpecification | None,
        market_state: MarketState,
        product_catalog_hash: str,
    ) -> UnitValuation:
        if instrument.kind is not InstrumentKind.FUTURE:
            raise ExposureEngineError("futures_adapter_instrument_kind_mismatch")
        if position.instrument_id != instrument.instrument_id:
            raise ExposureEngineError("futures_adapter_position_instrument_mismatch")
        if contract_specification is None:
            raise ExposureEngineError("futures_adapter_contract_specification_required")
        try:
            quote = market_state.futures_contract_quote(instrument.instrument_id)
        except MarketStateError as exc:
            raise ExposureEngineError(str(exc)) from exc
        if quote.expiry_at != _timestamp_text(
            contract_specification.expiry_at,
            "futures_adapter.contract_expiry_at",
        ):
            raise ExposureEngineError("futures_adapter_contract_expiry_mismatch")
        if (
            quote.currency != instrument.currency
            or quote.currency != contract_specification.settlement_currency
        ):
            raise ExposureEngineError("futures_adapter_quote_currency_mismatch")
        expected_unit = f"{quote.currency}_per_{contract_specification.contract_unit}"
        if quote.price_unit != expected_unit:
            raise ExposureEngineError(
                f"futures_adapter_quote_unit_mismatch:{expected_unit}"
            )
        if quote.margin_model_hash != self.margin_model_hash:
            raise ExposureEngineError("futures_adapter_margin_model_hash_mismatch")
        multiplier = contract_specification.contract_multiplier
        mark = quote.mark_price
        return UnitValuation(
            instrument_id=instrument.instrument_id,
            instrument_kind=instrument.kind,
            valuation_at=market_state.valuation_at,
            currency=quote.currency,
            quantity_unit=instrument.unit,
            price_unit=quote.price_unit,
            mark_price=mark,
            market_value=_ZERO,
            notional=mark,
            delta=mark * Decimal("0.01"),
            gamma=_ZERO,
            vega=_ZERO,
            theta=_ZERO,
            rho=_ZERO,
            margin=quote.initial_margin_per_contract / multiplier,
            collateral=quote.collateral_per_contract / multiplier,
            market_state_hash=market_state.state_hash(),
            product_catalog_hash=product_catalog_hash,
            model_hash=self.content_hash,
        )


@dataclass(frozen=True, slots=True)
class OptionValuationAdapter:
    """Production option adapter for model-bound cash Greeks in MarketState."""

    pricing_model_hash: str
    model_specification_hash: str
    margin_model_hash: str
    adapter_id: str = "multi_asset.option_market_state"
    adapter_version: str = "2"
    instrument_kind: InstrumentKind = InstrumentKind.OPTION
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "pricing_model_hash",
            "model_specification_hash",
            "margin_model_hash",
        ):
            _require_hash(getattr(self, field_name), f"option_adapter.{field_name}")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "adapter_id": self.adapter_id,
                    "adapter_version": self.adapter_version,
                    "instrument_kind": self.instrument_kind.value,
                    "pricing_model_hash": self.pricing_model_hash,
                    "model_specification_hash": self.model_specification_hash,
                    "margin_model_hash": self.margin_model_hash,
                    "market_value": "market_price_per_contract_unit",
                    "delta": "raw_delta_x_underlying_x_1pct",
                    "gamma": "half_raw_gamma_x_underlying_1pct_squared",
                    "vega": "per_one_volatility_point",
                    "theta": "per_calendar_day",
                    "rho": "per_one_rate_point",
                    "multiplier_application": "exposure_engine_only",
                },
                label="multi_asset_option_valuation_adapter",
            ),
        )

    def value(
        self,
        *,
        position: ExposurePosition,
        instrument: Instrument,
        contract_specification: ContractSpecification | None,
        market_state: MarketState,
        product_catalog_hash: str,
    ) -> UnitValuation:
        if instrument.kind is not InstrumentKind.OPTION:
            raise ExposureEngineError("option_adapter_instrument_kind_mismatch")
        if position.instrument_id != instrument.instrument_id:
            raise ExposureEngineError("option_adapter_position_instrument_mismatch")
        if contract_specification is None:
            raise ExposureEngineError("option_adapter_contract_specification_required")
        try:
            quote = market_state.option_contract_quote(instrument.instrument_id)
            analytics = market_state.option_analytics_mark(instrument.instrument_id)
            underlying_price = market_state.derivative_underlying_price(
                analytics.underlying_instrument_id
            )
        except MarketStateError as exc:
            raise ExposureEngineError(str(exc)) from exc
        expected_expiry = _timestamp_text(
            contract_specification.expiry_at,
            "option_adapter.contract_expiry_at",
        )
        if quote.expiry_at != expected_expiry or analytics.expiry_at != expected_expiry:
            raise ExposureEngineError("option_adapter_contract_expiry_mismatch")
        if (
            quote.currency != instrument.currency
            or analytics.currency != instrument.currency
            or quote.currency != contract_specification.settlement_currency
        ):
            raise ExposureEngineError("option_adapter_quote_currency_mismatch")
        expected_unit = f"{quote.currency}_per_{contract_specification.contract_unit}"
        if quote.price_unit != expected_unit or analytics.price_unit != expected_unit:
            raise ExposureEngineError(
                f"option_adapter_quote_unit_mismatch:{expected_unit}"
            )
        if analytics.model_hash != self.pricing_model_hash:
            raise ExposureEngineError("option_adapter_pricing_model_hash_mismatch")
        if analytics.model_specification_hash != self.model_specification_hash:
            raise ExposureEngineError(
                "option_adapter_model_specification_hash_mismatch"
            )
        if analytics.margin_model_hash != self.margin_model_hash:
            raise ExposureEngineError("option_adapter_margin_model_hash_mismatch")
        multiplier = contract_specification.contract_multiplier
        one_percent_move = underlying_price * Decimal("0.01")
        return UnitValuation(
            instrument_id=instrument.instrument_id,
            instrument_kind=instrument.kind,
            valuation_at=market_state.valuation_at,
            currency=analytics.currency,
            quantity_unit=instrument.unit,
            price_unit=analytics.price_unit,
            mark_price=analytics.market_price,
            market_value=analytics.market_price,
            notional=underlying_price,
            delta=analytics.delta * one_percent_move,
            gamma=(
                analytics.gamma * one_percent_move * one_percent_move / Decimal("2")
            ),
            vega=analytics.vega,
            theta=analytics.theta,
            rho=analytics.rho,
            margin=analytics.margin_per_contract / multiplier,
            collateral=analytics.collateral_per_contract / multiplier,
            market_state_hash=market_state.state_hash(),
            product_catalog_hash=product_catalog_hash,
            model_hash=self.content_hash,
        )


@dataclass(frozen=True, slots=True)
class ExposurePolicy:
    deep_capacity_multiple: Decimal = Decimal("10")
    adequate_capacity_multiple: Decimal = Decimal("2")
    constrained_capacity_multiple: Decimal = Decimal("1")
    schema_version: int = MULTI_ASSET_EXPOSURE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MULTI_ASSET_EXPOSURE_SCHEMA_VERSION:
            raise ExposureEngineError("exposure_policy_schema_unsupported")
        for name in (
            "deep_capacity_multiple",
            "adequate_capacity_multiple",
            "constrained_capacity_multiple",
        ):
            _decimal(getattr(self, name), f"exposure_policy.{name}", positive=True)
        if not (
            self.deep_capacity_multiple
            > self.adequate_capacity_multiple
            > self.constrained_capacity_multiple
        ):
            raise ExposureEngineError("exposure_policy_liquidity_threshold_order")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "deep_capacity_multiple": _decimal_text(self.deep_capacity_multiple),
            "adequate_capacity_multiple": _decimal_text(
                self.adequate_capacity_multiple
            ),
            "constrained_capacity_multiple": _decimal_text(
                self.constrained_capacity_multiple
            ),
            "greek_unit_contract_hash": GREEK_CASH_UNIT_CONTRACT_HASH,
            "notional_offset": ("sum_underlying_gross_minus_absolute_underlying_net"),
            "concentration": "bucket_gross_notional_divided_by_portfolio_gross",
        }

    def policy_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="multi_asset_exposure_policy")


@dataclass(frozen=True, slots=True)
class EvaluatedPositionExposure:
    position_id: str
    instrument_id: str
    instrument_kind: InstrumentKind
    underlying_id: str
    native_currency: str
    base_currency: str
    valuation_at: str
    quantity: Decimal
    quantity_unit: str
    multiplier: Decimal
    expiry_at: str | None
    liquidity_bucket: LiquidityBucket
    liquidity_days: Decimal | None
    market_value_native: Decimal
    signed_notional_native: Decimal
    market_value_base: Decimal
    gross_notional_base: Decimal
    net_notional_base: Decimal
    delta_base: Decimal
    gamma_base: Decimal
    vega_base: Decimal
    theta_base: Decimal
    rho_base: Decimal
    margin_base: Decimal
    collateral_base: Decimal
    position_hash: str
    valuation_hash: str
    adapter_hash: str
    market_state_hash: str
    product_catalog_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "position_id",
            "instrument_id",
            "underlying_id",
            "quantity_unit",
        ):
            _require_id(getattr(self, field_name), f"position_exposure.{field_name}")
        if self.instrument_kind not in {
            InstrumentKind.SPOT,
            InstrumentKind.FUTURE,
            InstrumentKind.OPTION,
        }:
            raise ExposureEngineError("position_exposure_kind_unsupported")
        _require_currency(self.native_currency, "position_exposure.native_currency")
        _require_currency(self.base_currency, "position_exposure.base_currency")
        object.__setattr__(
            self,
            "valuation_at",
            _timestamp_text(self.valuation_at, "position_exposure.valuation_at"),
        )
        _decimal(self.quantity, "position_exposure.quantity")
        _decimal(self.multiplier, "position_exposure.multiplier", positive=True)
        if self.expiry_at is not None:
            object.__setattr__(
                self,
                "expiry_at",
                _timestamp_text(self.expiry_at, "position_exposure.expiry_at"),
            )
        if not isinstance(self.liquidity_bucket, LiquidityBucket):
            raise ExposureEngineError("position_exposure.liquidity_bucket_invalid")
        if self.liquidity_days is not None:
            _decimal(
                self.liquidity_days,
                "position_exposure.liquidity_days",
                nonnegative=True,
            )
        for name in (
            "market_value_native",
            "signed_notional_native",
            "market_value_base",
            "gross_notional_base",
            "net_notional_base",
            "delta_base",
            "gamma_base",
            "vega_base",
            "theta_base",
            "rho_base",
            "margin_base",
            "collateral_base",
        ):
            _decimal(
                getattr(self, name),
                f"position_exposure.{name}",
                nonnegative=name
                in {"gross_notional_base", "margin_base", "collateral_base"},
            )
        if self.gross_notional_base != abs(self.net_notional_base):
            raise ExposureEngineError("position_exposure_gross_net_mismatch")
        for name in (
            "position_hash",
            "valuation_hash",
            "adapter_hash",
            "market_state_hash",
            "product_catalog_hash",
        ):
            _require_hash(getattr(self, name), f"position_exposure.{name}")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(), label="multi_asset_position_exposure"
            ),
        )

    @property
    def delta(self) -> Decimal:
        return self.delta_base

    @property
    def gamma(self) -> Decimal:
        return self.gamma_base

    @property
    def vega(self) -> Decimal:
        return self.vega_base

    @property
    def theta(self) -> Decimal:
        return self.theta_base

    @property
    def rho(self) -> Decimal:
        return self.rho_base

    def identity_payload(self) -> dict[str, object]:
        return {
            "position_id": self.position_id,
            "instrument_id": self.instrument_id,
            "instrument_kind": self.instrument_kind.value,
            "underlying_id": self.underlying_id,
            "native_currency": self.native_currency,
            "base_currency": self.base_currency,
            "valuation_at": self.valuation_at,
            "quantity": _decimal_text(self.quantity),
            "quantity_unit": self.quantity_unit,
            "multiplier": _decimal_text(self.multiplier),
            "expiry_at": self.expiry_at,
            "liquidity_bucket": self.liquidity_bucket.value,
            "liquidity_days": (
                _decimal_text(self.liquidity_days)
                if self.liquidity_days is not None
                else None
            ),
            "market_value_native": _decimal_text(self.market_value_native),
            "signed_notional_native": _decimal_text(self.signed_notional_native),
            "market_value_base": _decimal_text(self.market_value_base),
            "gross_notional_base": _decimal_text(self.gross_notional_base),
            "net_notional_base": _decimal_text(self.net_notional_base),
            "delta_base": _decimal_text(self.delta_base),
            "gamma_base": _decimal_text(self.gamma_base),
            "vega_base": _decimal_text(self.vega_base),
            "theta_base": _decimal_text(self.theta_base),
            "rho_base": _decimal_text(self.rho_base),
            "margin_base": _decimal_text(self.margin_base),
            "collateral_base": _decimal_text(self.collateral_base),
            "position_hash": self.position_hash,
            "valuation_hash": self.valuation_hash,
            "adapter_hash": self.adapter_hash,
            "market_state_hash": self.market_state_hash,
            "product_catalog_hash": self.product_catalog_hash,
            "greek_unit_contract_hash": GREEK_CASH_UNIT_CONTRACT_HASH,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class SourcePositionSum:
    instrument_id: str
    currency: str
    quantity_unit: str
    multiplier: Decimal
    signed_quantity: Decimal
    gross_quantity: Decimal
    position_count: int

    def __post_init__(self) -> None:
        _require_id(self.instrument_id, "source_sum.instrument_id")
        _require_currency(self.currency, "source_sum.currency")
        _require_id(self.quantity_unit, "source_sum.quantity_unit")
        _decimal(self.multiplier, "source_sum.multiplier", positive=True)
        _decimal(self.signed_quantity, "source_sum.signed_quantity")
        _decimal(self.gross_quantity, "source_sum.gross_quantity", nonnegative=True)
        if self.position_count < 1:
            raise ExposureEngineError("source_sum.position_count_invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "currency": self.currency,
            "quantity_unit": self.quantity_unit,
            "multiplier": _decimal_text(self.multiplier),
            "signed_quantity": _decimal_text(self.signed_quantity),
            "gross_quantity": _decimal_text(self.gross_quantity),
            "position_count": self.position_count,
        }


@dataclass(frozen=True, slots=True)
class ExposureTotals:
    base_currency: str
    position_count: int
    market_value: Decimal
    gross_notional: Decimal
    net_notional: Decimal
    delta: Decimal
    gamma: Decimal
    vega: Decimal
    theta: Decimal
    rho: Decimal
    margin: Decimal
    collateral: Decimal
    offset_notional: Decimal
    offset_ratio: Decimal

    def __post_init__(self) -> None:
        _require_currency(self.base_currency, "exposure_totals.base_currency")
        if self.position_count < 1:
            raise ExposureEngineError("exposure_totals.position_count_invalid")
        for name in (
            "market_value",
            "gross_notional",
            "net_notional",
            "delta",
            "gamma",
            "vega",
            "theta",
            "rho",
            "margin",
            "collateral",
            "offset_notional",
            "offset_ratio",
        ):
            _decimal(
                getattr(self, name),
                f"exposure_totals.{name}",
                nonnegative=name
                in {
                    "gross_notional",
                    "margin",
                    "collateral",
                    "offset_notional",
                    "offset_ratio",
                },
            )
        if self.offset_notional > self.gross_notional:
            raise ExposureEngineError("exposure_totals_offset_mismatch")
        expected_ratio = (
            self.offset_notional / self.gross_notional
            if self.gross_notional > _ZERO
            else _ZERO
        )
        if self.offset_ratio != expected_ratio:
            raise ExposureEngineError("exposure_totals_offset_ratio_mismatch")

    def as_dict(self) -> dict[str, object]:
        return {
            "base_currency": self.base_currency,
            "position_count": self.position_count,
            "market_value": _decimal_text(self.market_value),
            "gross_notional": _decimal_text(self.gross_notional),
            "net_notional": _decimal_text(self.net_notional),
            "delta": _decimal_text(self.delta),
            "gamma": _decimal_text(self.gamma),
            "vega": _decimal_text(self.vega),
            "theta": _decimal_text(self.theta),
            "rho": _decimal_text(self.rho),
            "margin": _decimal_text(self.margin),
            "collateral": _decimal_text(self.collateral),
            "offset_notional": _decimal_text(self.offset_notional),
            "offset_ratio": _decimal_text(self.offset_ratio),
        }

    def totals_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="multi_asset_exposure_totals")


@dataclass(frozen=True, slots=True)
class ExposureBucket:
    dimension: ExposureDimension
    key: str
    totals: ExposureTotals

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, ExposureDimension):
            raise ExposureEngineError("exposure_bucket.dimension_invalid")
        _require_bucket_key(self.key, "exposure_bucket.key")

    def as_dict(self) -> dict[str, object]:
        return {
            "dimension": self.dimension.value,
            "key": self.key,
            "totals": self.totals.as_dict(),
            "totals_hash": self.totals.totals_hash(),
        }


@dataclass(frozen=True, slots=True)
class Concentration:
    dimension: ExposureDimension
    largest_bucket_key: str
    largest_bucket_gross_notional: Decimal
    portfolio_gross_notional: Decimal
    ratio: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, ExposureDimension):
            raise ExposureEngineError("concentration.dimension_invalid")
        _require_bucket_key(self.largest_bucket_key, "concentration.largest_bucket_key")
        _decimal(
            self.largest_bucket_gross_notional,
            "concentration.largest_bucket_gross_notional",
            nonnegative=True,
        )
        _decimal(
            self.portfolio_gross_notional,
            "concentration.portfolio_gross_notional",
            nonnegative=True,
        )
        _decimal(self.ratio, "concentration.ratio", nonnegative=True)
        expected = (
            self.largest_bucket_gross_notional / self.portfolio_gross_notional
            if self.portfolio_gross_notional > _ZERO
            else _ZERO
        )
        if self.ratio != expected or self.ratio > _ONE:
            raise ExposureEngineError("concentration_ratio_mismatch")

    def as_dict(self) -> dict[str, object]:
        return {
            "dimension": self.dimension.value,
            "largest_bucket_key": self.largest_bucket_key,
            "largest_bucket_gross_notional": _decimal_text(
                self.largest_bucket_gross_notional
            ),
            "portfolio_gross_notional": _decimal_text(self.portfolio_gross_notional),
            "ratio": _decimal_text(self.ratio),
        }


@dataclass(frozen=True, slots=True)
class InvariantCheck:
    name: str
    expected: Decimal
    actual: Decimal
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.name, "invariant.name")
        _decimal(self.expected, "invariant.expected")
        _decimal(self.actual, "invariant.actual")
        if self.expected != self.actual:
            raise ExposureEngineError(f"exposure_invariant_failed:{self.name}")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="exposure_invariant_check"),
        )

    def identity_payload(self) -> dict[str, str]:
        return {
            "name": self.name,
            "expected": _decimal_text(self.expected),
            "actual": _decimal_text(self.actual),
        }

    def as_dict(self) -> dict[str, str]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class ExposureEvidence:
    product_catalog_hash: str
    market_state_hash: str
    policy_hash: str
    adapter_set_hash: str
    source_positions_hash: str
    source_position_sums_hash: str
    evaluated_positions_hash: str
    totals_hash: str
    buckets_hash: str
    concentrations_hash: str
    invariant_checks: tuple[InvariantCheck, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "product_catalog_hash",
            "market_state_hash",
            "policy_hash",
            "adapter_set_hash",
            "source_positions_hash",
            "source_position_sums_hash",
            "evaluated_positions_hash",
            "totals_hash",
            "buckets_hash",
            "concentrations_hash",
        ):
            _require_hash(getattr(self, name), f"exposure_evidence.{name}")
        checks = tuple(self.invariant_checks)
        object.__setattr__(self, "invariant_checks", checks)
        if not checks or len({item.name for item in checks}) != len(checks):
            raise ExposureEngineError("exposure_evidence_invariant_checks_invalid")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(), label="multi_asset_exposure_evidence"
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "product_catalog_hash": self.product_catalog_hash,
            "market_state_hash": self.market_state_hash,
            "policy_hash": self.policy_hash,
            "adapter_set_hash": self.adapter_set_hash,
            "source_positions_hash": self.source_positions_hash,
            "source_position_sums_hash": self.source_position_sums_hash,
            "evaluated_positions_hash": self.evaluated_positions_hash,
            "totals_hash": self.totals_hash,
            "buckets_hash": self.buckets_hash,
            "concentrations_hash": self.concentrations_hash,
            "invariant_checks": [item.as_dict() for item in self.invariant_checks],
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def _sum(
    position_exposures: Sequence[EvaluatedPositionExposure], field_name: str
) -> Decimal:
    return sum(
        (getattr(item, field_name) for item in position_exposures),
        start=_ZERO,
    )


def _build_totals(
    positions: Sequence[EvaluatedPositionExposure], base_currency: str
) -> ExposureTotals:
    gross = _sum(positions, "gross_notional_base")
    net = _sum(positions, "net_notional_base")
    by_underlying: dict[str, list[EvaluatedPositionExposure]] = {}
    for position in positions:
        by_underlying.setdefault(position.underlying_id, []).append(position)
    offset = sum(
        (
            _sum(items, "gross_notional_base") - abs(_sum(items, "net_notional_base"))
            for items in by_underlying.values()
        ),
        start=_ZERO,
    )
    return ExposureTotals(
        base_currency=base_currency,
        position_count=len(positions),
        market_value=_sum(positions, "market_value_base"),
        gross_notional=gross,
        net_notional=net,
        delta=_sum(positions, "delta_base"),
        gamma=_sum(positions, "gamma_base"),
        vega=_sum(positions, "vega_base"),
        theta=_sum(positions, "theta_base"),
        rho=_sum(positions, "rho_base"),
        margin=_sum(positions, "margin_base"),
        collateral=_sum(positions, "collateral_base"),
        offset_notional=offset,
        offset_ratio=offset / gross if gross > _ZERO else _ZERO,
    )


def _bucket_key(
    position: EvaluatedPositionExposure, dimension: ExposureDimension
) -> str:
    if dimension is ExposureDimension.UNDERLYING:
        return position.underlying_id
    if dimension is ExposureDimension.CURRENCY:
        return position.native_currency
    if dimension is ExposureDimension.EXPIRY:
        return position.expiry_at or "NON_EXPIRING"
    return position.liquidity_bucket.value


def _build_buckets(
    positions: Sequence[EvaluatedPositionExposure], base_currency: str
) -> tuple[ExposureBucket, ...]:
    result: list[ExposureBucket] = []
    for dimension in ExposureDimension:
        grouped: dict[str, list[EvaluatedPositionExposure]] = {}
        for position in positions:
            grouped.setdefault(_bucket_key(position, dimension), []).append(position)
        result.extend(
            ExposureBucket(
                dimension=dimension,
                key=key,
                totals=_build_totals(items, base_currency),
            )
            for key, items in sorted(grouped.items())
        )
    return tuple(result)


def _build_concentrations(
    buckets: Sequence[ExposureBucket], portfolio_gross: Decimal
) -> tuple[Concentration, ...]:
    result: list[Concentration] = []
    for dimension in ExposureDimension:
        candidates = [item for item in buckets if item.dimension is dimension]
        largest = max(
            candidates,
            key=lambda item: (item.totals.gross_notional, item.key),
        )
        result.append(
            Concentration(
                dimension=dimension,
                largest_bucket_key=largest.key,
                largest_bucket_gross_notional=largest.totals.gross_notional,
                portfolio_gross_notional=portfolio_gross,
                ratio=(
                    largest.totals.gross_notional / portfolio_gross
                    if portfolio_gross > _ZERO
                    else _ZERO
                ),
            )
        )
    return tuple(result)


def _source_position_sums(
    positions: Sequence[ExposurePosition | EvaluatedPositionExposure],
) -> tuple[SourcePositionSum, ...]:
    grouped: dict[
        tuple[str, str, str, Decimal],
        list[ExposurePosition | EvaluatedPositionExposure],
    ] = {}
    for position in positions:
        currency = (
            position.currency
            if isinstance(position, ExposurePosition)
            else position.native_currency
        )
        key = (
            position.instrument_id,
            currency,
            position.quantity_unit,
            position.multiplier,
        )
        grouped.setdefault(key, []).append(position)
    result = [
        SourcePositionSum(
            instrument_id=key[0],
            currency=key[1],
            quantity_unit=key[2],
            multiplier=key[3],
            signed_quantity=sum((item.quantity for item in items), start=_ZERO),
            gross_quantity=sum((abs(item.quantity) for item in items), start=_ZERO),
            position_count=len(items),
        )
        for key, items in grouped.items()
    ]
    return tuple(
        sorted(
            result,
            key=lambda item: (
                item.instrument_id,
                item.currency,
                item.quantity_unit,
                item.multiplier,
            ),
        )
    )


def _source_positions_binding(
    positions: Sequence[EvaluatedPositionExposure],
) -> str:
    return _hash_payload(
        [
            {
                "position_id": item.position_id,
                "position_hash": item.position_hash,
            }
            for item in sorted(positions, key=lambda item: item.position_id)
        ],
        label="multi_asset_source_positions",
    )


@dataclass(frozen=True, slots=True)
class PortfolioExposureSnapshot:
    snapshot_id: str
    valuation_at: str
    base_currency: str
    source_position_sums: tuple[SourcePositionSum, ...]
    positions: tuple[EvaluatedPositionExposure, ...]
    totals: ExposureTotals
    buckets: tuple[ExposureBucket, ...]
    concentrations: tuple[Concentration, ...]
    evidence: ExposureEvidence
    content_hash: str = field(init=False)
    schema_version: int = MULTI_ASSET_EXPOSURE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MULTI_ASSET_EXPOSURE_SCHEMA_VERSION:
            raise ExposureEngineError("exposure_snapshot_schema_unsupported")
        _require_id(self.snapshot_id, "exposure_snapshot.snapshot_id")
        object.__setattr__(
            self,
            "valuation_at",
            _timestamp_text(self.valuation_at, "exposure_snapshot.valuation_at"),
        )
        _require_currency(self.base_currency, "exposure_snapshot.base_currency")
        for field_name in (
            "source_position_sums",
            "positions",
            "buckets",
            "concentrations",
        ):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))
        if not self.positions or len(
            {item.position_id for item in self.positions}
        ) != len(self.positions):
            raise ExposureEngineError("exposure_snapshot_positions_invalid")
        if any(item.valuation_at != self.valuation_at for item in self.positions):
            raise ExposureEngineError("exposure_snapshot_valuation_time_mismatch")
        if any(item.base_currency != self.base_currency for item in self.positions):
            raise ExposureEngineError("exposure_snapshot_base_currency_mismatch")
        expected_source_sums = _source_position_sums(self.positions)
        if expected_source_sums != self.source_position_sums:
            raise ExposureEngineError(
                "exposure_snapshot_source_position_sums_invariant_failed"
            )
        if any(
            item.market_state_hash != self.evidence.market_state_hash
            for item in self.positions
        ):
            raise ExposureEngineError("exposure_snapshot_market_state_binding_failed")
        if any(
            item.product_catalog_hash != self.evidence.product_catalog_hash
            for item in self.positions
        ):
            raise ExposureEngineError("exposure_snapshot_catalog_binding_failed")
        expected_totals = _build_totals(self.positions, self.base_currency)
        if expected_totals != self.totals:
            raise ExposureEngineError("exposure_snapshot_totals_invariant_failed")
        expected_buckets = _build_buckets(self.positions, self.base_currency)
        if expected_buckets != self.buckets:
            raise ExposureEngineError("exposure_snapshot_buckets_invariant_failed")
        expected_concentrations = _build_concentrations(
            self.buckets, self.totals.gross_notional
        )
        if expected_concentrations != self.concentrations:
            raise ExposureEngineError(
                "exposure_snapshot_concentration_invariant_failed"
            )
        expected_checks = ExposureEngine._invariant_checks(
            source_position_count=len(self.positions),
            evaluated=self.positions,
            totals=self.totals,
            buckets=self.buckets,
        )
        if expected_checks != self.evidence.invariant_checks:
            raise ExposureEngineError("exposure_snapshot_invariant_evidence_mismatch")
        hashes = self._component_hashes()
        for name, expected in hashes.items():
            if getattr(self.evidence, name) != expected:
                raise ExposureEngineError(
                    f"exposure_snapshot_evidence_binding_mismatch:{name}"
                )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(), label="multi_asset_exposure_snapshot"
            ),
        )

    def _component_hashes(self) -> dict[str, str]:
        return {
            "source_positions_hash": _source_positions_binding(self.positions),
            "source_position_sums_hash": _hash_payload(
                [item.as_dict() for item in self.source_position_sums],
                label="multi_asset_source_position_sums",
            ),
            "evaluated_positions_hash": _hash_payload(
                [item.as_dict() for item in self.positions],
                label="multi_asset_evaluated_positions",
            ),
            "totals_hash": self.totals.totals_hash(),
            "buckets_hash": _hash_payload(
                [item.as_dict() for item in self.buckets],
                label="multi_asset_exposure_buckets",
            ),
            "concentrations_hash": _hash_payload(
                [item.as_dict() for item in self.concentrations],
                label="multi_asset_exposure_concentrations",
            ),
        }

    def bucket(self, dimension: ExposureDimension, key: str) -> ExposureBucket:
        matches = [
            item
            for item in self.buckets
            if item.dimension is dimension and item.key == key
        ]
        if len(matches) != 1:
            raise ExposureEngineError(f"exposure_bucket_not_unique:{dimension}:{key}")
        return matches[0]

    def concentration(self, dimension: ExposureDimension) -> Concentration:
        matches = [item for item in self.concentrations if item.dimension is dimension]
        if len(matches) != 1:
            raise ExposureEngineError(f"exposure_concentration_not_unique:{dimension}")
        return matches[0]

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "valuation_at": self.valuation_at,
            "base_currency": self.base_currency,
            "source_position_sums": [
                item.as_dict() for item in self.source_position_sums
            ],
            "positions": [item.as_dict() for item in self.positions],
            "totals": self.totals.as_dict(),
            "buckets": [item.as_dict() for item in self.buckets],
            "concentrations": [item.as_dict() for item in self.concentrations],
            "evidence": self.evidence.as_dict(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class ExposureEngine:
    product_catalog: ProductCatalog
    adapters: tuple[ProductValuationAdapter, ...]
    policy: ExposurePolicy = ExposurePolicy()

    def __post_init__(self) -> None:
        adapters = tuple(self.adapters)
        object.__setattr__(self, "adapters", adapters)
        kinds: set[InstrumentKind] = set()
        for adapter in adapters:
            if not isinstance(adapter, ProductValuationAdapter):
                raise ExposureEngineError("valuation_adapter_protocol_invalid")
            _require_id(adapter.adapter_id, "valuation_adapter.adapter_id")
            _require_id(adapter.adapter_version, "valuation_adapter.adapter_version")
            _require_hash(adapter.content_hash, "valuation_adapter.content_hash")
            if adapter.instrument_kind in kinds:
                raise ExposureEngineError(
                    f"valuation_adapter_kind_duplicate:{adapter.instrument_kind}"
                )
            kinds.add(adapter.instrument_kind)

    @classmethod
    def with_default_spot(
        cls,
        *,
        product_catalog: ProductCatalog,
        derivative_adapters: Sequence[ProductValuationAdapter] = (),
        policy: ExposurePolicy | None = None,
    ) -> ExposureEngine:
        return cls(
            product_catalog=product_catalog,
            adapters=(SpotValuationAdapter(), *tuple(derivative_adapters)),
            policy=policy or ExposurePolicy(),
        )

    def evaluate(
        self,
        *,
        snapshot_id: str,
        positions: Sequence[ExposurePosition],
        market_state: MarketState,
    ) -> PortfolioExposureSnapshot:
        _require_id(snapshot_id, "exposure.snapshot_id")
        market_state.require_usable()
        source_positions = tuple(positions)
        if not source_positions:
            raise ExposureEngineError("exposure.positions_required")
        if len({item.position_id for item in source_positions}) != len(
            source_positions
        ):
            raise ExposureEngineError("exposure.position_id_duplicate")
        for position in source_positions:
            if not position.active_at(market_state.valuation_at):
                raise ExposureEngineError(
                    f"exposure_position_not_active:{position.position_id}"
                )
        catalog_hash = self.product_catalog.contract_hash()
        state_hash = market_state.state_hash()
        evaluated = tuple(
            sorted(
                (
                    self._evaluate_position(
                        position=position,
                        market_state=market_state,
                        catalog_hash=catalog_hash,
                        state_hash=state_hash,
                    )
                    for position in source_positions
                ),
                key=lambda item: item.position_id,
            )
        )
        source_sums = _source_position_sums(source_positions)
        totals = _build_totals(evaluated, market_state.base_currency)
        buckets = _build_buckets(evaluated, market_state.base_currency)
        concentrations = _build_concentrations(buckets, totals.gross_notional)
        component_hashes = {
            "source_position_sums_hash": _hash_payload(
                [item.as_dict() for item in source_sums],
                label="multi_asset_source_position_sums",
            ),
            "evaluated_positions_hash": _hash_payload(
                [item.as_dict() for item in evaluated],
                label="multi_asset_evaluated_positions",
            ),
            "totals_hash": totals.totals_hash(),
            "buckets_hash": _hash_payload(
                [item.as_dict() for item in buckets],
                label="multi_asset_exposure_buckets",
            ),
            "concentrations_hash": _hash_payload(
                [item.as_dict() for item in concentrations],
                label="multi_asset_exposure_concentrations",
            ),
        }
        evidence = ExposureEvidence(
            product_catalog_hash=catalog_hash,
            market_state_hash=state_hash,
            policy_hash=self.policy.policy_hash(),
            adapter_set_hash=self._adapter_set_hash(),
            source_positions_hash=_source_positions_binding(evaluated),
            invariant_checks=self._invariant_checks(
                source_position_count=len(source_positions),
                evaluated=evaluated,
                totals=totals,
                buckets=buckets,
            ),
            **component_hashes,
        )
        return PortfolioExposureSnapshot(
            snapshot_id=snapshot_id,
            valuation_at=market_state.valuation_at,
            base_currency=market_state.base_currency,
            source_position_sums=source_sums,
            positions=evaluated,
            totals=totals,
            buckets=buckets,
            concentrations=concentrations,
            evidence=evidence,
        )

    def _evaluate_position(
        self,
        *,
        position: ExposurePosition,
        market_state: MarketState,
        catalog_hash: str,
        state_hash: str,
    ) -> EvaluatedPositionExposure:
        instrument = self.product_catalog.instrument_as_of(
            position.instrument_id, market_state.valuation_at
        )
        if instrument is None:
            raise ExposureEngineError(
                f"exposure_instrument_not_active:{position.instrument_id}"
            )
        if instrument.kind not in {
            InstrumentKind.SPOT,
            InstrumentKind.FUTURE,
            InstrumentKind.OPTION,
        }:
            raise ExposureEngineError(
                f"exposure_instrument_kind_unsupported:{instrument.kind}"
            )
        if position.quantity_unit != instrument.unit:
            raise ExposureEngineError(
                f"position_quantity_unit_mismatch:{position.position_id}"
            )
        if position.currency != instrument.currency:
            raise ExposureEngineError(
                f"position_currency_mismatch:{position.position_id}"
            )
        specification = self._contract_specification(
            instrument, market_state.valuation_at
        )
        expected_multiplier = (
            specification.contract_multiplier if specification is not None else _ONE
        )
        if position.multiplier != expected_multiplier:
            raise ExposureEngineError(
                f"position_multiplier_mismatch:{position.position_id}"
            )
        self._validate_derivative_market_state_binding(
            instrument=instrument,
            market_state=market_state,
        )
        adapter = self._adapter(instrument.kind)
        unit = adapter.value(
            position=position,
            instrument=instrument,
            contract_specification=specification,
            market_state=market_state,
            product_catalog_hash=catalog_hash,
        )
        self._validate_unit_valuation(
            unit=unit,
            position=position,
            instrument=instrument,
            state_hash=state_hash,
            catalog_hash=catalog_hash,
            adapter=adapter,
            valuation_at=market_state.valuation_at,
        )
        signed_factor = position.quantity * position.multiplier
        absolute_factor = abs(signed_factor)
        market_native = signed_factor * unit.market_value
        notional_native = signed_factor * unit.notional
        market_base = market_state.convert(
            market_native,
            from_currency=unit.currency,
            to_currency=market_state.base_currency,
        )
        net_base = market_state.convert(
            notional_native,
            from_currency=unit.currency,
            to_currency=market_state.base_currency,
        )

        def signed_base(value: Decimal) -> Decimal:
            return market_state.convert(
                signed_factor * value,
                from_currency=unit.currency,
                to_currency=market_state.base_currency,
            )

        def absolute_base(value: Decimal) -> Decimal:
            return market_state.convert(
                absolute_factor * value,
                from_currency=unit.currency,
                to_currency=market_state.base_currency,
            )

        liquidity_bucket, liquidity_days = self._liquidity(
            position=position, market_state=market_state
        )
        return EvaluatedPositionExposure(
            position_id=position.position_id,
            instrument_id=instrument.instrument_id,
            instrument_kind=instrument.kind,
            underlying_id=instrument.economic_underlying_id,
            native_currency=unit.currency,
            base_currency=market_state.base_currency,
            valuation_at=market_state.valuation_at,
            quantity=position.quantity,
            quantity_unit=position.quantity_unit,
            multiplier=position.multiplier,
            expiry_at=specification.expiry_at if specification is not None else None,
            liquidity_bucket=liquidity_bucket,
            liquidity_days=liquidity_days,
            market_value_native=market_native,
            signed_notional_native=notional_native,
            market_value_base=market_base,
            gross_notional_base=abs(net_base),
            net_notional_base=net_base,
            delta_base=signed_base(unit.delta),
            gamma_base=signed_base(unit.gamma),
            vega_base=signed_base(unit.vega),
            theta_base=signed_base(unit.theta),
            rho_base=signed_base(unit.rho),
            margin_base=absolute_base(unit.margin),
            collateral_base=absolute_base(unit.collateral),
            position_hash=position.position_hash(),
            valuation_hash=unit.valuation_hash(),
            adapter_hash=adapter.content_hash,
            market_state_hash=state_hash,
            product_catalog_hash=catalog_hash,
        )

    def _validate_derivative_market_state_binding(
        self,
        *,
        instrument: Instrument,
        market_state: MarketState,
    ) -> None:
        if instrument.kind is InstrumentKind.SPOT:
            return
        if instrument.kind is InstrumentKind.FUTURE:
            relationship_type = InstrumentRelationshipType.FUTURE_UNDERLYING
            try:
                market_underlying_id = market_state.futures_contract_quote(
                    instrument.instrument_id
                ).underlying_instrument_id
            except MarketStateError as exc:
                raise ExposureEngineError(str(exc)) from exc
        elif instrument.kind is InstrumentKind.OPTION:
            relationship_type = InstrumentRelationshipType.OPTION_UNDERLYING
            try:
                market_underlying_id = market_state.option_contract_quote(
                    instrument.instrument_id
                ).underlying_instrument_id
            except MarketStateError as exc:
                raise ExposureEngineError(str(exc)) from exc
        else:
            return
        targets = self.product_catalog.relationship_targets(
            source_instrument_id=instrument.instrument_id,
            relationship_type=relationship_type,
            as_of=market_state.valuation_at,
        )
        if len(targets) != 1 or targets[0].instrument_id != market_underlying_id:
            raise ExposureEngineError(
                f"derivative_market_state_underlying_mismatch:{instrument.instrument_id}"
            )

    def _adapter(self, kind: InstrumentKind) -> ProductValuationAdapter:
        matches = [item for item in self.adapters if item.instrument_kind is kind]
        if len(matches) != 1:
            raise ExposureEngineError(f"valuation_adapter_not_unique:{kind}")
        return matches[0]

    def _contract_specification(
        self, instrument: Instrument, valuation_at: str
    ) -> ContractSpecification | None:
        matches = [
            item
            for item in self.product_catalog.contract_specifications
            if item.instrument_id == instrument.instrument_id
            and item.validity.contains(valuation_at)
        ]
        if instrument.kind in {InstrumentKind.FUTURE, InstrumentKind.OPTION}:
            if len(matches) != 1:
                raise ExposureEngineError(
                    f"contract_specification_not_unique:{instrument.instrument_id}"
                )
            return matches[0]
        if matches:
            raise ExposureEngineError(
                f"spot_contract_specification_forbidden:{instrument.instrument_id}"
            )
        return None

    @staticmethod
    def _validate_unit_valuation(
        *,
        unit: UnitValuation,
        position: ExposurePosition,
        instrument: Instrument,
        state_hash: str,
        catalog_hash: str,
        adapter: ProductValuationAdapter,
        valuation_at: str,
    ) -> None:
        if unit.instrument_id != instrument.instrument_id:
            raise ExposureEngineError("unit_valuation_instrument_mismatch")
        if unit.instrument_kind is not instrument.kind:
            raise ExposureEngineError("unit_valuation_kind_mismatch")
        if unit.valuation_at != _timestamp_text(valuation_at, "valuation_at"):
            raise ExposureEngineError("unit_valuation_time_mismatch")
        if unit.currency != position.currency:
            raise ExposureEngineError("unit_valuation_currency_mismatch")
        if unit.quantity_unit != position.quantity_unit:
            raise ExposureEngineError("unit_valuation_quantity_unit_mismatch")
        if unit.market_state_hash != state_hash:
            raise ExposureEngineError("unit_valuation_market_state_hash_mismatch")
        if unit.product_catalog_hash != catalog_hash:
            raise ExposureEngineError("unit_valuation_catalog_hash_mismatch")
        if unit.model_hash != adapter.content_hash:
            raise ExposureEngineError("unit_valuation_model_hash_mismatch")

    def _liquidity(
        self, *, position: ExposurePosition, market_state: MarketState
    ) -> tuple[LiquidityBucket, Decimal | None]:
        matches = [
            item
            for item in market_state.liquidity_quotes
            if item.instrument_id == position.instrument_id
        ]
        if not matches or matches[0].depth_quantity == _ZERO:
            return LiquidityBucket.UNAVAILABLE, None
        quote = matches[0]
        if quote.currency != position.currency:
            raise ExposureEngineError("liquidity_currency_mismatch")
        if quote.quantity_unit != position.quantity_unit:
            raise ExposureEngineError("liquidity_quantity_unit_mismatch")
        capacity_multiple = quote.depth_quantity / abs(position.quantity)
        days = abs(position.quantity) / quote.depth_quantity
        if capacity_multiple >= self.policy.deep_capacity_multiple:
            bucket = LiquidityBucket.DEEP
        elif capacity_multiple >= self.policy.adequate_capacity_multiple:
            bucket = LiquidityBucket.ADEQUATE
        elif capacity_multiple >= self.policy.constrained_capacity_multiple:
            bucket = LiquidityBucket.CONSTRAINED
        else:
            bucket = LiquidityBucket.INSUFFICIENT
        return bucket, days

    def _adapter_set_hash(self) -> str:
        return _hash_payload(
            [
                {
                    "adapter_id": item.adapter_id,
                    "adapter_version": item.adapter_version,
                    "instrument_kind": item.instrument_kind.value,
                    "content_hash": item.content_hash,
                }
                for item in sorted(self.adapters, key=lambda item: item.adapter_id)
            ],
            label="multi_asset_valuation_adapter_set",
        )

    @staticmethod
    def _invariant_checks(
        *,
        source_position_count: int,
        evaluated: Sequence[EvaluatedPositionExposure],
        totals: ExposureTotals,
        buckets: Sequence[ExposureBucket],
    ) -> tuple[InvariantCheck, ...]:
        checks = [
            InvariantCheck(
                "source_position_count",
                Decimal(source_position_count),
                Decimal(len(evaluated)),
            )
        ]
        mappings = (
            ("market_value", "market_value_base"),
            ("gross_notional", "gross_notional_base"),
            ("net_notional", "net_notional_base"),
            ("delta", "delta_base"),
            ("gamma", "gamma_base"),
            ("vega", "vega_base"),
            ("theta", "theta_base"),
            ("rho", "rho_base"),
            ("margin", "margin_base"),
            ("collateral", "collateral_base"),
        )
        checks.extend(
            InvariantCheck(
                f"portfolio_{total_name}",
                _sum(evaluated, position_name),
                getattr(totals, total_name),
            )
            for total_name, position_name in mappings
        )
        for dimension in ExposureDimension:
            dimensional = [item for item in buckets if item.dimension is dimension]
            checks.append(
                InvariantCheck(
                    f"bucket_{dimension.value.lower()}_gross_notional",
                    totals.gross_notional,
                    sum(
                        (item.totals.gross_notional for item in dimensional),
                        start=_ZERO,
                    ),
                )
            )
            checks.append(
                InvariantCheck(
                    f"bucket_{dimension.value.lower()}_market_value",
                    totals.market_value,
                    sum(
                        (item.totals.market_value for item in dimensional),
                        start=_ZERO,
                    ),
                )
            )
        return tuple(checks)


CommonPosition = ExposurePosition
ProductionExposureEngine = ExposureEngine
