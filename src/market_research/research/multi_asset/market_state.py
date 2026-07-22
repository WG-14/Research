"""Shared immutable valuation-time market state for offline research.

The state contains observations, not collectors.  Every component carries an
explicit unit, currency where applicable, calendar, availability timestamp,
staleness rule, quality decision, and immutable source hash.  Product-specific
pricing engines may consume this neutral snapshot without changing their own
semantics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Callable, Iterable, TypeVar

from ..hashing import sha256_prefixed


MARKET_STATE_SCHEMA_VERSION = 2
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_CURRENCY = re.compile(r"^[A-Z][A-Z0-9]{2,11}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class MarketStateError(ValueError):
    """A valuation snapshot is ambiguous, stale, or dimensionally invalid."""


class MarketDataQuality(StrEnum):
    GOOD = "GOOD"
    INDICATIVE = "INDICATIVE"
    STALE = "STALE"
    FAILED = "FAILED"


class QuoteCondition(StrEnum):
    """Typed market condition retained independently from data quality."""

    NORMAL = "NORMAL"
    INDICATIVE = "INDICATIVE"
    OFFICIAL_SETTLEMENT = "OFFICIAL_SETTLEMENT"
    HALTED = "HALTED"
    INVALID = "INVALID"


class OptionRight(StrEnum):
    CALL = "CALL"
    PUT = "PUT"


def _timestamp(value: str, field: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise MarketStateError(f"{field}_invalid_timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise MarketStateError(f"{field}_timezone_required")
    return result.astimezone(timezone.utc)


def _timestamp_text(value: str, field: str) -> str:
    return _timestamp(value, field).isoformat()


def _require_id(value: str, field: str) -> None:
    if not isinstance(value, str) or not _STABLE_ID.fullmatch(value):
        raise MarketStateError(f"{field}_invalid_stable_id")


def _require_currency(value: str, field: str) -> None:
    if not isinstance(value, str) or not _CURRENCY.fullmatch(value):
        raise MarketStateError(f"{field}_invalid_currency")


def _require_hash(value: str, field: str) -> None:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise MarketStateError(f"{field}_invalid_hash")


def _decimal(value: Decimal, field: str, *, positive: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise MarketStateError(f"{field}_must_be_finite_decimal")
    if positive and value <= 0:
        raise MarketStateError(f"{field}_must_be_positive")


def _optional_decimal(
    value: Decimal | None,
    field: str,
    *,
    positive: bool = False,
) -> None:
    if value is not None:
        _decimal(value, field, positive=positive)


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


@dataclass(frozen=True, slots=True)
class ObservationMetadata:
    """Availability, source, quality, calendar, and freshness evidence."""

    observed_at: str
    knowledge_at: str
    source_hash: str
    calendar_id: str
    max_age_seconds: int
    quality: MarketDataQuality = MarketDataQuality.GOOD

    def __post_init__(self) -> None:
        observed = _timestamp_text(self.observed_at, "metadata.observed_at")
        knowledge = _timestamp_text(self.knowledge_at, "metadata.knowledge_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "knowledge_at", knowledge)
        if _timestamp(knowledge, "metadata.knowledge_at") < _timestamp(
            observed, "metadata.observed_at"
        ):
            raise MarketStateError("metadata_knowledge_before_observation")
        _require_hash(self.source_hash, "metadata.source_hash")
        _require_id(self.calendar_id, "metadata.calendar_id")
        if (
            isinstance(self.max_age_seconds, bool)
            or not isinstance(self.max_age_seconds, int)
            or self.max_age_seconds < 0
        ):
            raise MarketStateError("metadata.max_age_seconds_invalid")
        if not isinstance(self.quality, MarketDataQuality):
            raise MarketStateError("metadata.quality_invalid")

    def age_seconds(self, valuation_at: str) -> int:
        valuation = _timestamp(valuation_at, "metadata.valuation_at")
        observed = _timestamp(self.observed_at, "metadata.observed_at")
        if valuation < observed:
            raise MarketStateError("market_observation_after_valuation")
        return int((valuation - observed).total_seconds())

    def is_stale(self, valuation_at: str) -> bool:
        return self.age_seconds(valuation_at) > self.max_age_seconds

    def as_dict(self) -> dict[str, object]:
        return {
            "observed_at": self.observed_at,
            "knowledge_at": self.knowledge_at,
            "source_hash": self.source_hash,
            "calendar_id": self.calendar_id,
            "max_age_seconds": self.max_age_seconds,
            "quality": self.quality.value,
        }


@dataclass(frozen=True, slots=True)
class SpotQuote:
    instrument_id: str
    price: Decimal
    currency: str
    unit: str
    metadata: ObservationMetadata

    def __post_init__(self) -> None:
        _require_id(self.instrument_id, "spot.instrument_id")
        _decimal(self.price, "spot.price", positive=True)
        _require_currency(self.currency, "spot.currency")
        _require_id(self.unit, "spot.unit")

    def as_dict(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "price": _decimal_text(self.price),
            "currency": self.currency,
            "unit": self.unit,
            "metadata": self.metadata.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class CurvePoint:
    tenor_days: int
    rate: Decimal

    def __post_init__(self) -> None:
        if (
            isinstance(self.tenor_days, bool)
            or not isinstance(self.tenor_days, int)
            or self.tenor_days <= 0
        ):
            raise MarketStateError("curve_point.tenor_days_invalid")
        _decimal(self.rate, "curve_point.rate")

    def as_dict(self) -> dict[str, object]:
        return {
            "tenor_days": self.tenor_days,
            "rate": _decimal_text(self.rate),
        }


@dataclass(frozen=True, slots=True)
class YieldCurve:
    curve_id: str
    currency: str
    curve_type: str
    points: tuple[CurvePoint, ...]
    metadata: ObservationMetadata
    unit: str = "decimal_rate"

    def __post_init__(self) -> None:
        _require_id(self.curve_id, "curve.curve_id")
        _require_currency(self.currency, "curve.currency")
        _require_id(self.curve_type, "curve.curve_type")
        if self.unit != "decimal_rate":
            raise MarketStateError("curve.unit_must_be_decimal_rate")
        points = tuple(self.points)
        object.__setattr__(self, "points", points)
        if not points:
            raise MarketStateError("curve.points_required")
        tenors = [item.tenor_days for item in points]
        if len(set(tenors)) != len(tenors):
            raise MarketStateError("curve.tenor_duplicate")

    def as_dict(self) -> dict[str, object]:
        return {
            "curve_id": self.curve_id,
            "currency": self.currency,
            "curve_type": self.curve_type,
            "unit": self.unit,
            "points": [
                item.as_dict()
                for item in sorted(self.points, key=lambda item: item.tenor_days)
            ],
            "metadata": self.metadata.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class VolatilityPoint:
    expiry_at: str
    strike: Decimal
    volatility: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "expiry_at",
            _timestamp_text(self.expiry_at, "volatility_point.expiry_at"),
        )
        _decimal(self.strike, "volatility_point.strike", positive=True)
        _decimal(self.volatility, "volatility_point.volatility")
        if self.volatility < 0:
            raise MarketStateError("volatility_point.volatility_negative")

    def as_dict(self) -> dict[str, str]:
        return {
            "expiry_at": self.expiry_at,
            "strike": _decimal_text(self.strike),
            "volatility": _decimal_text(self.volatility),
        }


@dataclass(frozen=True, slots=True)
class VolatilitySurface:
    surface_id: str
    underlying_instrument_id: str
    quote_currency: str
    points: tuple[VolatilityPoint, ...]
    metadata: ObservationMetadata
    unit: str = "decimal_volatility"

    def __post_init__(self) -> None:
        _require_id(self.surface_id, "volatility_surface.surface_id")
        _require_id(
            self.underlying_instrument_id,
            "volatility_surface.underlying_instrument_id",
        )
        _require_currency(self.quote_currency, "volatility_surface.quote_currency")
        if self.unit != "decimal_volatility":
            raise MarketStateError("volatility_surface.unit_must_be_decimal_volatility")
        points = tuple(self.points)
        object.__setattr__(self, "points", points)
        if not points:
            raise MarketStateError("volatility_surface.points_required")
        keys = [(item.expiry_at, item.strike) for item in points]
        if len(set(keys)) != len(keys):
            raise MarketStateError("volatility_surface.point_duplicate")

    def as_dict(self) -> dict[str, object]:
        return {
            "surface_id": self.surface_id,
            "underlying_instrument_id": self.underlying_instrument_id,
            "quote_currency": self.quote_currency,
            "unit": self.unit,
            "points": [
                item.as_dict()
                for item in sorted(
                    self.points, key=lambda item: (item.expiry_at, item.strike)
                )
            ],
            "metadata": self.metadata.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class RateQuote:
    rate_id: str
    currency: str
    tenor_days: int
    rate: Decimal
    metadata: ObservationMetadata
    unit: str = "decimal_rate"

    def __post_init__(self) -> None:
        _require_id(self.rate_id, "rate.rate_id")
        _require_currency(self.currency, "rate.currency")
        if (
            isinstance(self.tenor_days, bool)
            or not isinstance(self.tenor_days, int)
            or self.tenor_days <= 0
        ):
            raise MarketStateError("rate.tenor_days_invalid")
        _decimal(self.rate, "rate.rate")
        if self.unit != "decimal_rate":
            raise MarketStateError("rate.unit_must_be_decimal_rate")

    def as_dict(self) -> dict[str, object]:
        return {
            "rate_id": self.rate_id,
            "currency": self.currency,
            "tenor_days": self.tenor_days,
            "rate": _decimal_text(self.rate),
            "unit": self.unit,
            "metadata": self.metadata.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class FXQuote:
    base_currency: str
    quote_currency: str
    rate: Decimal
    unit: str
    metadata: ObservationMetadata

    def __post_init__(self) -> None:
        _require_currency(self.base_currency, "fx.base_currency")
        _require_currency(self.quote_currency, "fx.quote_currency")
        if self.base_currency == self.quote_currency:
            raise MarketStateError("fx_currency_pair_must_differ")
        _decimal(self.rate, "fx.rate", positive=True)
        expected_unit = f"{self.quote_currency}_per_{self.base_currency}"
        if self.unit != expected_unit:
            raise MarketStateError(f"fx_unit_mismatch:{expected_unit}")

    @property
    def pair(self) -> tuple[str, str]:
        return (self.base_currency, self.quote_currency)

    def as_dict(self) -> dict[str, object]:
        return {
            "base_currency": self.base_currency,
            "quote_currency": self.quote_currency,
            "rate": _decimal_text(self.rate),
            "unit": self.unit,
            "metadata": self.metadata.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class BorrowQuote:
    instrument_id: str
    currency: str
    annualized_rate: Decimal
    metadata: ObservationMetadata
    available_quantity: Decimal | None = None
    quantity_unit: str | None = None
    unit: str = "decimal_rate"

    def __post_init__(self) -> None:
        _require_id(self.instrument_id, "borrow.instrument_id")
        _require_currency(self.currency, "borrow.currency")
        _decimal(self.annualized_rate, "borrow.annualized_rate")
        if self.annualized_rate < 0:
            raise MarketStateError("borrow.annualized_rate_negative")
        if self.unit != "decimal_rate":
            raise MarketStateError("borrow.unit_must_be_decimal_rate")
        if (self.available_quantity is None) != (self.quantity_unit is None):
            raise MarketStateError("borrow_availability_unit_binding_incomplete")
        if self.available_quantity is not None:
            _decimal(self.available_quantity, "borrow.available_quantity")
            if self.available_quantity < 0:
                raise MarketStateError("borrow.available_quantity_negative")
            if self.quantity_unit is None:
                raise MarketStateError("borrow_availability_unit_binding_incomplete")
            _require_id(self.quantity_unit, "borrow.quantity_unit")

    def as_dict(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "currency": self.currency,
            "annualized_rate": _decimal_text(self.annualized_rate),
            "unit": self.unit,
            "available_quantity": (
                _decimal_text(self.available_quantity)
                if self.available_quantity is not None
                else None
            ),
            "quantity_unit": self.quantity_unit,
            "metadata": self.metadata.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class LiquidityQuote:
    instrument_id: str
    currency: str
    bid: Decimal
    ask: Decimal
    price_unit: str
    depth_quantity: Decimal
    quantity_unit: str
    metadata: ObservationMetadata

    def __post_init__(self) -> None:
        _require_id(self.instrument_id, "liquidity.instrument_id")
        _require_currency(self.currency, "liquidity.currency")
        _decimal(self.bid, "liquidity.bid", positive=True)
        _decimal(self.ask, "liquidity.ask", positive=True)
        if self.ask < self.bid:
            raise MarketStateError("liquidity_crossed_quote")
        _require_id(self.price_unit, "liquidity.price_unit")
        _decimal(self.depth_quantity, "liquidity.depth_quantity")
        if self.depth_quantity < 0:
            raise MarketStateError("liquidity.depth_quantity_negative")
        _require_id(self.quantity_unit, "liquidity.quantity_unit")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    def as_dict(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "currency": self.currency,
            "bid": _decimal_text(self.bid),
            "ask": _decimal_text(self.ask),
            "spread": _decimal_text(self.spread),
            "price_unit": self.price_unit,
            "depth_quantity": _decimal_text(self.depth_quantity),
            "quantity_unit": self.quantity_unit,
            "metadata": self.metadata.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class FuturesContractQuote:
    """One tradable futures contract observed inside a curve snapshot.

    Prices are per ``contract_unit``.  Margin and collateral are deliberately
    stored per contract so the production valuation adapter can normalize them
    before the exposure engine applies the contract multiplier exactly once.
    """

    contract_id: str
    underlying_instrument_id: str
    expiry_at: str
    currency: str
    price_unit: str
    bid: Decimal
    ask: Decimal
    last: Decimal | None
    settlement: Decimal | None
    bid_size: Decimal
    ask_size: Decimal
    volume: Decimal
    open_interest: Decimal
    condition: QuoteCondition
    initial_margin_per_contract: Decimal
    collateral_per_contract: Decimal
    margin_model_hash: str
    metadata: ObservationMetadata
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.contract_id, "futures_quote.contract_id")
        _require_id(
            self.underlying_instrument_id,
            "futures_quote.underlying_instrument_id",
        )
        object.__setattr__(
            self,
            "expiry_at",
            _timestamp_text(self.expiry_at, "futures_quote.expiry_at"),
        )
        _require_currency(self.currency, "futures_quote.currency")
        _require_id(self.price_unit, "futures_quote.price_unit")
        _decimal(self.bid, "futures_quote.bid", positive=True)
        _decimal(self.ask, "futures_quote.ask", positive=True)
        if self.ask < self.bid:
            raise MarketStateError("futures_quote_crossed")
        _optional_decimal(self.last, "futures_quote.last", positive=True)
        _optional_decimal(
            self.settlement,
            "futures_quote.settlement",
            positive=True,
        )
        for field_name in (
            "bid_size",
            "ask_size",
            "volume",
            "open_interest",
            "initial_margin_per_contract",
            "collateral_per_contract",
        ):
            value = getattr(self, field_name)
            _decimal(value, f"futures_quote.{field_name}")
            if value < 0:
                raise MarketStateError(f"futures_quote.{field_name}_negative")
        if not isinstance(self.condition, QuoteCondition):
            raise MarketStateError("futures_quote.condition_invalid")
        _require_hash(self.margin_model_hash, "futures_quote.margin_model_hash")
        if not isinstance(self.metadata, ObservationMetadata):
            raise MarketStateError("futures_quote.metadata_required")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="futures_contract_quote"),
        )

    @property
    def mark_price(self) -> Decimal:
        if self.settlement is not None:
            return self.settlement
        if self.last is not None:
            return self.last
        return (self.bid + self.ask) / Decimal("2")

    def identity_payload(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "underlying_instrument_id": self.underlying_instrument_id,
            "expiry_at": self.expiry_at,
            "currency": self.currency,
            "price_unit": self.price_unit,
            "bid": _decimal_text(self.bid),
            "ask": _decimal_text(self.ask),
            "last": _decimal_text(self.last) if self.last is not None else None,
            "settlement": (
                _decimal_text(self.settlement) if self.settlement is not None else None
            ),
            "mark_price": _decimal_text(self.mark_price),
            "bid_size": _decimal_text(self.bid_size),
            "ask_size": _decimal_text(self.ask_size),
            "volume": _decimal_text(self.volume),
            "open_interest": _decimal_text(self.open_interest),
            "condition": self.condition.value,
            "initial_margin_per_contract": _decimal_text(
                self.initial_margin_per_contract
            ),
            "collateral_per_contract": _decimal_text(self.collateral_per_contract),
            "margin_model_hash": self.margin_model_hash,
            "metadata": self.metadata.as_dict(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class FuturesCurveState:
    """Immutable, point-in-time futures curve made only of actual contracts."""

    curve_id: str
    underlying_instrument_id: str
    currency: str
    price_unit: str
    contracts: tuple[FuturesContractQuote, ...]
    metadata: ObservationMetadata
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.curve_id, "futures_curve.curve_id")
        _require_id(
            self.underlying_instrument_id,
            "futures_curve.underlying_instrument_id",
        )
        _require_currency(self.currency, "futures_curve.currency")
        _require_id(self.price_unit, "futures_curve.price_unit")
        if not isinstance(self.metadata, ObservationMetadata):
            raise MarketStateError("futures_curve.metadata_required")
        contracts = tuple(self.contracts)
        object.__setattr__(self, "contracts", contracts)
        if not contracts or any(
            not isinstance(item, FuturesContractQuote) for item in contracts
        ):
            raise MarketStateError("futures_curve.contracts_required")
        if _duplicates(contracts, lambda item: item.contract_id):
            raise MarketStateError("futures_curve.contract_duplicate")
        if _duplicates(contracts, lambda item: item.expiry_at):
            raise MarketStateError("futures_curve.expiry_duplicate")
        curve_observed = _timestamp(
            self.metadata.observed_at,
            "futures_curve.metadata.observed_at",
        )
        curve_known = _timestamp(
            self.metadata.knowledge_at,
            "futures_curve.metadata.knowledge_at",
        )
        for quote in contracts:
            if (
                quote.underlying_instrument_id != self.underlying_instrument_id
                or quote.currency != self.currency
                or quote.price_unit != self.price_unit
                or quote.metadata.calendar_id != self.metadata.calendar_id
            ):
                raise MarketStateError(
                    f"futures_curve_contract_mismatch:{quote.contract_id}"
                )
            if (
                _timestamp(
                    quote.metadata.observed_at,
                    "futures_quote.metadata.observed_at",
                )
                > curve_observed
                or _timestamp(
                    quote.metadata.knowledge_at,
                    "futures_quote.metadata.knowledge_at",
                )
                > curve_known
            ):
                raise MarketStateError(
                    f"futures_curve_contract_time_mismatch:{quote.contract_id}"
                )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="futures_curve_state"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "curve_id": self.curve_id,
            "underlying_instrument_id": self.underlying_instrument_id,
            "currency": self.currency,
            "price_unit": self.price_unit,
            "contracts": [
                item.as_dict()
                for item in sorted(
                    self.contracts,
                    key=lambda item: (item.expiry_at, item.contract_id),
                )
            ],
            "metadata": self.metadata.as_dict(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class OptionContractQuote:
    """One actual option contract quote, without model-derived values."""

    contract_id: str
    underlying_instrument_id: str
    expiry_at: str
    right: OptionRight
    strike: Decimal
    currency: str
    price_unit: str
    bid: Decimal
    ask: Decimal
    last: Decimal | None
    settlement: Decimal | None
    bid_size: Decimal
    ask_size: Decimal
    volume: Decimal
    open_interest: Decimal
    condition: QuoteCondition
    metadata: ObservationMetadata
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.contract_id, "option_quote.contract_id")
        _require_id(
            self.underlying_instrument_id,
            "option_quote.underlying_instrument_id",
        )
        object.__setattr__(
            self,
            "expiry_at",
            _timestamp_text(self.expiry_at, "option_quote.expiry_at"),
        )
        if not isinstance(self.right, OptionRight):
            raise MarketStateError("option_quote.right_invalid")
        _decimal(self.strike, "option_quote.strike", positive=True)
        _require_currency(self.currency, "option_quote.currency")
        _require_id(self.price_unit, "option_quote.price_unit")
        _decimal(self.bid, "option_quote.bid", positive=True)
        _decimal(self.ask, "option_quote.ask", positive=True)
        if self.ask < self.bid:
            raise MarketStateError("option_quote_crossed")
        _optional_decimal(self.last, "option_quote.last", positive=True)
        _optional_decimal(
            self.settlement,
            "option_quote.settlement",
            positive=True,
        )
        for field_name in ("bid_size", "ask_size", "volume", "open_interest"):
            value = getattr(self, field_name)
            _decimal(value, f"option_quote.{field_name}")
            if value < 0:
                raise MarketStateError(f"option_quote.{field_name}_negative")
        if not isinstance(self.condition, QuoteCondition):
            raise MarketStateError("option_quote.condition_invalid")
        if not isinstance(self.metadata, ObservationMetadata):
            raise MarketStateError("option_quote.metadata_required")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_contract_quote"),
        )

    @property
    def midpoint(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    def identity_payload(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "underlying_instrument_id": self.underlying_instrument_id,
            "expiry_at": self.expiry_at,
            "right": self.right.value,
            "strike": _decimal_text(self.strike),
            "currency": self.currency,
            "price_unit": self.price_unit,
            "bid": _decimal_text(self.bid),
            "ask": _decimal_text(self.ask),
            "last": _decimal_text(self.last) if self.last is not None else None,
            "settlement": (
                _decimal_text(self.settlement) if self.settlement is not None else None
            ),
            "midpoint": _decimal_text(self.midpoint),
            "bid_size": _decimal_text(self.bid_size),
            "ask_size": _decimal_text(self.ask_size),
            "volume": _decimal_text(self.volume),
            "open_interest": _decimal_text(self.open_interest),
            "condition": self.condition.value,
            "metadata": self.metadata.as_dict(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class OptionAnalyticsMark:
    """Model-derived option analytics bound to one immutable market quote."""

    contract_id: str
    underlying_instrument_id: str
    expiry_at: str
    currency: str
    price_unit: str
    market_price: Decimal
    model_price: Decimal
    implied_volatility: Decimal
    delta: Decimal
    gamma: Decimal
    vega: Decimal
    theta: Decimal
    rho: Decimal
    margin_per_contract: Decimal
    collateral_per_contract: Decimal
    model_hash: str
    model_specification_hash: str
    margin_model_hash: str
    valuation_input_hash: str
    source_quote_hash: str
    metadata: ObservationMetadata
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.contract_id, "option_analytics.contract_id")
        _require_id(
            self.underlying_instrument_id,
            "option_analytics.underlying_instrument_id",
        )
        object.__setattr__(
            self,
            "expiry_at",
            _timestamp_text(self.expiry_at, "option_analytics.expiry_at"),
        )
        _require_currency(self.currency, "option_analytics.currency")
        _require_id(self.price_unit, "option_analytics.price_unit")
        for field_name in ("market_price", "model_price", "implied_volatility"):
            _decimal(
                getattr(self, field_name),
                f"option_analytics.{field_name}",
                positive=True,
            )
        for field_name in ("delta", "gamma", "vega", "theta", "rho"):
            _decimal(getattr(self, field_name), f"option_analytics.{field_name}")
        if not Decimal("-1") <= self.delta <= Decimal("1"):
            raise MarketStateError("option_analytics.delta_out_of_range")
        if self.gamma < 0 or self.vega < 0:
            raise MarketStateError("option_analytics.convexity_sign_invalid")
        for field_name in ("margin_per_contract", "collateral_per_contract"):
            value = getattr(self, field_name)
            _decimal(value, f"option_analytics.{field_name}")
            if value < 0:
                raise MarketStateError(f"option_analytics.{field_name}_negative")
        for field_name in (
            "model_hash",
            "model_specification_hash",
            "margin_model_hash",
            "valuation_input_hash",
            "source_quote_hash",
        ):
            _require_hash(getattr(self, field_name), f"option_analytics.{field_name}")
        if not isinstance(self.metadata, ObservationMetadata):
            raise MarketStateError("option_analytics.metadata_required")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_analytics_mark"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "underlying_instrument_id": self.underlying_instrument_id,
            "expiry_at": self.expiry_at,
            "currency": self.currency,
            "price_unit": self.price_unit,
            "market_price": _decimal_text(self.market_price),
            "model_price": _decimal_text(self.model_price),
            "implied_volatility": _decimal_text(self.implied_volatility),
            "delta": _decimal_text(self.delta),
            "gamma": _decimal_text(self.gamma),
            "vega": _decimal_text(self.vega),
            "theta": _decimal_text(self.theta),
            "rho": _decimal_text(self.rho),
            "margin_per_contract": _decimal_text(self.margin_per_contract),
            "collateral_per_contract": _decimal_text(self.collateral_per_contract),
            "model_hash": self.model_hash,
            "model_specification_hash": self.model_specification_hash,
            "margin_model_hash": self.margin_model_hash,
            "valuation_input_hash": self.valuation_input_hash,
            "source_quote_hash": self.source_quote_hash,
            "metadata": self.metadata.as_dict(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class OptionChainState:
    """PIT option quotes plus derived analytics with one-to-one bindings."""

    chain_id: str
    underlying_instrument_id: str
    currency: str
    price_unit: str
    quotes: tuple[OptionContractQuote, ...]
    analytics: tuple[OptionAnalyticsMark, ...]
    metadata: ObservationMetadata
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.chain_id, "option_chain.chain_id")
        _require_id(
            self.underlying_instrument_id,
            "option_chain.underlying_instrument_id",
        )
        _require_currency(self.currency, "option_chain.currency")
        _require_id(self.price_unit, "option_chain.price_unit")
        if not isinstance(self.metadata, ObservationMetadata):
            raise MarketStateError("option_chain.metadata_required")
        quotes = tuple(self.quotes)
        analytics = tuple(self.analytics)
        object.__setattr__(self, "quotes", quotes)
        object.__setattr__(self, "analytics", analytics)
        if not quotes or any(
            not isinstance(item, OptionContractQuote) for item in quotes
        ):
            raise MarketStateError("option_chain.quotes_required")
        if not analytics or any(
            not isinstance(item, OptionAnalyticsMark) for item in analytics
        ):
            raise MarketStateError("option_chain.analytics_required")
        if _duplicates(quotes, lambda item: item.contract_id):
            raise MarketStateError("option_chain.quote_duplicate")
        if _duplicates(analytics, lambda item: item.contract_id):
            raise MarketStateError("option_chain.analytics_duplicate")
        quote_by_id = {item.contract_id: item for item in quotes}
        analytics_by_id = {item.contract_id: item for item in analytics}
        if quote_by_id.keys() != analytics_by_id.keys():
            raise MarketStateError("option_chain_quote_analytics_set_mismatch")
        chain_observed = _timestamp(
            self.metadata.observed_at,
            "option_chain.metadata.observed_at",
        )
        chain_known = _timestamp(
            self.metadata.knowledge_at,
            "option_chain.metadata.knowledge_at",
        )
        for contract_id, quote in quote_by_id.items():
            mark = analytics_by_id[contract_id]
            for item in (quote, mark):
                if (
                    item.underlying_instrument_id != self.underlying_instrument_id
                    or item.currency != self.currency
                    or item.price_unit != self.price_unit
                    or item.metadata.calendar_id != self.metadata.calendar_id
                ):
                    raise MarketStateError(
                        f"option_chain_contract_mismatch:{contract_id}"
                    )
                if (
                    _timestamp(
                        item.metadata.observed_at,
                        "option_chain_item.metadata.observed_at",
                    )
                    > chain_observed
                    or _timestamp(
                        item.metadata.knowledge_at,
                        "option_chain_item.metadata.knowledge_at",
                    )
                    > chain_known
                ):
                    raise MarketStateError(
                        f"option_chain_contract_time_mismatch:{contract_id}"
                    )
            if quote.expiry_at != mark.expiry_at:
                raise MarketStateError(
                    f"option_chain_contract_expiry_mismatch:{contract_id}"
                )
            if mark.source_quote_hash != quote.content_hash:
                raise MarketStateError(
                    f"option_chain_source_quote_hash_mismatch:{contract_id}"
                )
            if not quote.bid <= mark.market_price <= quote.ask:
                raise MarketStateError(
                    f"option_chain_market_price_outside_quote:{contract_id}"
                )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_chain_state"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "chain_id": self.chain_id,
            "underlying_instrument_id": self.underlying_instrument_id,
            "currency": self.currency,
            "price_unit": self.price_unit,
            "quotes": [
                item.as_dict()
                for item in sorted(self.quotes, key=lambda item: item.contract_id)
            ],
            "analytics": [
                item.as_dict()
                for item in sorted(
                    self.analytics,
                    key=lambda item: item.contract_id,
                )
            ],
            "metadata": self.metadata.as_dict(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


_T = TypeVar("_T")


def _duplicates(items: Iterable[_T], key: Callable[[_T], object]) -> bool:
    seen: set[object] = set()
    for item in items:
        value = key(item)
        if value in seen:
            return True
        seen.add(value)
    return False


@dataclass(frozen=True, slots=True)
class MarketState:
    """A complete immutable set of inputs at one shared valuation instant."""

    state_id: str
    valuation_at: str
    base_currency: str
    calendar_ids: tuple[str, ...]
    spots: tuple[SpotQuote, ...] = ()
    curves: tuple[YieldCurve, ...] = ()
    volatility_surfaces: tuple[VolatilitySurface, ...] = ()
    rates: tuple[RateQuote, ...] = ()
    fx_quotes: tuple[FXQuote, ...] = ()
    borrow_quotes: tuple[BorrowQuote, ...] = ()
    liquidity_quotes: tuple[LiquidityQuote, ...] = ()
    schema_version: int = MARKET_STATE_SCHEMA_VERSION
    futures_curves: tuple[FuturesCurveState, ...] = field(
        default=(),
        kw_only=True,
    )
    option_chains: tuple[OptionChainState, ...] = field(
        default=(),
        kw_only=True,
    )

    def __post_init__(self) -> None:
        if self.schema_version != MARKET_STATE_SCHEMA_VERSION:
            raise MarketStateError("market_state_schema_unsupported")
        _require_id(self.state_id, "market_state.state_id")
        object.__setattr__(
            self,
            "valuation_at",
            _timestamp_text(self.valuation_at, "market_state.valuation_at"),
        )
        _require_currency(self.base_currency, "market_state.base_currency")
        calendars = tuple(sorted(self.calendar_ids))
        if not calendars or len(set(calendars)) != len(calendars):
            raise MarketStateError("market_state.calendar_ids_invalid")
        for calendar_id in calendars:
            _require_id(calendar_id, "market_state.calendar_id")
        object.__setattr__(self, "calendar_ids", calendars)
        for field_name in (
            "spots",
            "curves",
            "volatility_surfaces",
            "rates",
            "fx_quotes",
            "borrow_quotes",
            "liquidity_quotes",
            "futures_curves",
            "option_chains",
        ):
            object.__setattr__(
                self,
                field_name,
                tuple(getattr(self, field_name)),
            )
        # FX conversion walks this tuple, so retain exactly the same canonical
        # ordering that participates in the state hash.  Without this step two
        # equal hashes could select different first-match conversion routes.
        object.__setattr__(
            self,
            "fx_quotes",
            tuple(sorted(self.fx_quotes, key=lambda item: item.pair)),
        )
        self._validate_unique_components()
        self._validate_availability_and_quality()
        self._validate_component_consistency()
        self._validate_currency_coverage()

    def _validate_unique_components(self) -> None:
        definitions: tuple[
            tuple[Iterable[object], Callable[[object], object], str], ...
        ] = (
            (
                self.spots,
                lambda item: item.instrument_id,  # type: ignore[attr-defined]
                "spot",
            ),
            (
                self.curves,
                lambda item: item.curve_id,  # type: ignore[attr-defined]
                "curve",
            ),
            (
                self.volatility_surfaces,
                lambda item: item.surface_id,  # type: ignore[attr-defined]
                "volatility_surface",
            ),
            (
                self.rates,
                lambda item: item.rate_id,  # type: ignore[attr-defined]
                "rate",
            ),
            (
                self.fx_quotes,
                lambda item: item.pair,  # type: ignore[attr-defined]
                "fx",
            ),
            (
                self.borrow_quotes,
                lambda item: item.instrument_id,  # type: ignore[attr-defined]
                "borrow",
            ),
            (
                self.liquidity_quotes,
                lambda item: item.instrument_id,  # type: ignore[attr-defined]
                "liquidity",
            ),
            (
                self.futures_curves,
                lambda item: item.curve_id,  # type: ignore[attr-defined]
                "futures_curve",
            ),
            (
                self.option_chains,
                lambda item: item.chain_id,  # type: ignore[attr-defined]
                "option_chain",
            ),
        )
        for items, key, label in definitions:
            if _duplicates(items, key):
                raise MarketStateError(f"market_state_{label}_duplicate")
        undirected_fx_pairs: set[tuple[str, str]] = set()
        for quote in self.fx_quotes:
            first_currency, second_currency = sorted(
                (quote.base_currency, quote.quote_currency)
            )
            undirected_pair = (first_currency, second_currency)
            if undirected_pair in undirected_fx_pairs:
                raise MarketStateError(
                    "market_state_fx_reciprocal_pair_duplicate:"
                    f"{undirected_pair[0]}:{undirected_pair[1]}"
                )
            undirected_fx_pairs.add(undirected_pair)
        futures_contracts = tuple(
            quote for curve in self.futures_curves for quote in curve.contracts
        )
        if _duplicates(futures_contracts, lambda item: item.contract_id):
            raise MarketStateError("market_state_futures_contract_duplicate")
        option_quotes = tuple(
            quote for chain in self.option_chains for quote in chain.quotes
        )
        if _duplicates(option_quotes, lambda item: item.contract_id):
            raise MarketStateError("market_state_option_contract_duplicate")

    def _metadata_items(self) -> tuple[tuple[str, ObservationMetadata], ...]:
        result: list[tuple[str, ObservationMetadata]] = []
        for label, items in (
            ("spot", self.spots),
            ("curve", self.curves),
            ("volatility_surface", self.volatility_surfaces),
            ("rate", self.rates),
            ("fx", self.fx_quotes),
            ("borrow", self.borrow_quotes),
            ("liquidity", self.liquidity_quotes),
        ):
            result.extend((label, item.metadata) for item in items)
        for curve in self.futures_curves:
            result.append(("futures_curve", curve.metadata))
            result.extend(
                ("futures_contract_quote", item.metadata) for item in curve.contracts
            )
        for chain in self.option_chains:
            result.append(("option_chain", chain.metadata))
            result.extend(
                ("option_contract_quote", item.metadata) for item in chain.quotes
            )
            result.extend(
                ("option_analytics", item.metadata) for item in chain.analytics
            )
        return tuple(result)

    def _validate_availability_and_quality(self) -> None:
        valuation = _timestamp(self.valuation_at, "market_state.valuation_at")
        for label, metadata in self._metadata_items():
            if metadata.calendar_id not in self.calendar_ids:
                raise MarketStateError(
                    f"market_state_unregistered_calendar:{label}:{metadata.calendar_id}"
                )
            if _timestamp(metadata.knowledge_at, "metadata.knowledge_at") > valuation:
                raise MarketStateError(f"market_state_future_knowledge:{label}")
            stale = metadata.is_stale(self.valuation_at)
            if stale and metadata.quality in {
                MarketDataQuality.GOOD,
                MarketDataQuality.INDICATIVE,
            }:
                raise MarketStateError(
                    f"market_state_staleness_quality_mismatch:{label}"
                )

    def _validate_component_consistency(self) -> None:
        spot_by_id = {item.instrument_id: item for item in self.spots}
        liquidity_by_id = {item.instrument_id: item for item in self.liquidity_quotes}
        borrow_by_id = {item.instrument_id: item for item in self.borrow_quotes}
        surfaces_by_underlying = {
            item.underlying_instrument_id: item for item in self.volatility_surfaces
        }
        for instrument_id, spot in spot_by_id.items():
            liquidity = liquidity_by_id.get(instrument_id)
            if liquidity is not None and (
                liquidity.currency != spot.currency
                or liquidity.price_unit != spot.unit
                or liquidity.metadata.calendar_id != spot.metadata.calendar_id
            ):
                raise MarketStateError(
                    f"market_state_spot_liquidity_mismatch:{instrument_id}"
                )
            borrow = borrow_by_id.get(instrument_id)
            if borrow is not None and (
                borrow.currency != spot.currency
                or borrow.metadata.calendar_id != spot.metadata.calendar_id
            ):
                raise MarketStateError(
                    f"market_state_spot_borrow_mismatch:{instrument_id}"
                )
            surface = surfaces_by_underlying.get(instrument_id)
            if surface is not None and (
                surface.quote_currency != spot.currency
                or surface.metadata.calendar_id != spot.metadata.calendar_id
            ):
                raise MarketStateError(
                    f"market_state_spot_volatility_mismatch:{instrument_id}"
                )
        valuation = _timestamp(self.valuation_at, "market_state.valuation_at")
        for surface in self.volatility_surfaces:
            if any(
                _timestamp(point.expiry_at, "volatility_point.expiry_at") <= valuation
                for point in surface.points
            ):
                raise MarketStateError(
                    f"market_state_expired_volatility_point:{surface.surface_id}"
                )
        spot_ids = set(spot_by_id)
        futures_by_contract = {
            quote.contract_id: quote
            for curve in self.futures_curves
            for quote in curve.contracts
        }
        derivative_price_by_id: dict[str, tuple[str, str, str]] = {}
        for curve in self.futures_curves:
            if curve.underlying_instrument_id not in spot_ids:
                raise MarketStateError(
                    "market_state_futures_underlying_missing:"
                    f"{curve.underlying_instrument_id}"
                )
            for quote in curve.contracts:
                if _timestamp(quote.expiry_at, "futures_quote.expiry_at") <= valuation:
                    raise MarketStateError(
                        f"market_state_expired_futures_contract:{quote.contract_id}"
                    )
                derivative_price_by_id[quote.contract_id] = (
                    quote.currency,
                    quote.price_unit,
                    quote.metadata.calendar_id,
                )
        valid_option_underlyings = spot_ids | set(futures_by_contract)
        for chain in self.option_chains:
            if chain.underlying_instrument_id not in valid_option_underlyings:
                raise MarketStateError(
                    "market_state_option_underlying_missing:"
                    f"{chain.underlying_instrument_id}"
                )
            for option_quote in chain.quotes:
                if (
                    _timestamp(
                        option_quote.expiry_at,
                        "option_quote.expiry_at",
                    )
                    <= valuation
                ):
                    raise MarketStateError(
                        "market_state_expired_option_contract:"
                        f"{option_quote.contract_id}"
                    )
                derivative_price_by_id[option_quote.contract_id] = (
                    option_quote.currency,
                    option_quote.price_unit,
                    option_quote.metadata.calendar_id,
                )
        for instrument_id, dimensions in derivative_price_by_id.items():
            liquidity = liquidity_by_id.get(instrument_id)
            if liquidity is None:
                continue
            currency, price_unit, calendar_id = dimensions
            if (
                liquidity.currency != currency
                or liquidity.price_unit != price_unit
                or liquidity.metadata.calendar_id != calendar_id
            ):
                raise MarketStateError(
                    f"market_state_derivative_liquidity_mismatch:{instrument_id}"
                )

    def _validate_currency_coverage(self) -> None:
        currencies = {self.base_currency}
        currencies.update(item.currency for item in self.spots)
        currencies.update(item.currency for item in self.curves)
        currencies.update(item.quote_currency for item in self.volatility_surfaces)
        currencies.update(item.currency for item in self.rates)
        currencies.update(item.currency for item in self.borrow_quotes)
        currencies.update(item.currency for item in self.liquidity_quotes)
        currencies.update(item.currency for item in self.futures_curves)
        currencies.update(item.currency for item in self.option_chains)
        pairs = {item.pair for item in self.fx_quotes}
        for currency in currencies - {self.base_currency}:
            if (currency, self.base_currency) not in pairs and (
                self.base_currency,
                currency,
            ) not in pairs:
                raise MarketStateError(
                    f"market_state_base_fx_missing:{currency}:{self.base_currency}"
                )

    def require_usable(self) -> None:
        unusable = [
            label
            for label, metadata in self._metadata_items()
            if metadata.quality
            in {
                MarketDataQuality.STALE,
                MarketDataQuality.FAILED,
            }
            or metadata.is_stale(self.valuation_at)
        ]
        if unusable:
            raise MarketStateError(
                "market_state_unusable_quality:" + ",".join(sorted(unusable))
            )
        unusable_conditions = [
            f"future:{item.contract_id}"
            for curve in self.futures_curves
            for item in curve.contracts
            if item.condition in {QuoteCondition.HALTED, QuoteCondition.INVALID}
        ]
        unusable_conditions.extend(
            f"option:{item.contract_id}"
            for chain in self.option_chains
            for item in chain.quotes
            if item.condition in {QuoteCondition.HALTED, QuoteCondition.INVALID}
        )
        if unusable_conditions:
            raise MarketStateError(
                "market_state_unusable_quote_condition:"
                + ",".join(sorted(unusable_conditions))
            )

    def spot_price(self, instrument_id: str) -> SpotQuote:
        matches = [item for item in self.spots if item.instrument_id == instrument_id]
        if len(matches) != 1:
            raise MarketStateError(f"spot_quote_not_unique:{instrument_id}")
        return matches[0]

    def futures_curve(self, curve_id: str) -> FuturesCurveState:
        matches = [item for item in self.futures_curves if item.curve_id == curve_id]
        if len(matches) != 1:
            raise MarketStateError(f"futures_curve_not_unique:{curve_id}")
        return matches[0]

    def futures_contract_quote(self, contract_id: str) -> FuturesContractQuote:
        matches = [
            item
            for curve in self.futures_curves
            for item in curve.contracts
            if item.contract_id == contract_id
        ]
        if len(matches) != 1:
            raise MarketStateError(f"futures_contract_quote_not_unique:{contract_id}")
        return matches[0]

    def option_chain(self, chain_id: str) -> OptionChainState:
        matches = [item for item in self.option_chains if item.chain_id == chain_id]
        if len(matches) != 1:
            raise MarketStateError(f"option_chain_not_unique:{chain_id}")
        return matches[0]

    def option_contract_quote(self, contract_id: str) -> OptionContractQuote:
        matches = [
            item
            for chain in self.option_chains
            for item in chain.quotes
            if item.contract_id == contract_id
        ]
        if len(matches) != 1:
            raise MarketStateError(f"option_contract_quote_not_unique:{contract_id}")
        return matches[0]

    def option_analytics_mark(self, contract_id: str) -> OptionAnalyticsMark:
        matches = [
            item
            for chain in self.option_chains
            for item in chain.analytics
            if item.contract_id == contract_id
        ]
        if len(matches) != 1:
            raise MarketStateError(f"option_analytics_mark_not_unique:{contract_id}")
        return matches[0]

    def derivative_underlying_price(self, instrument_id: str) -> Decimal:
        spot_matches = [
            item for item in self.spots if item.instrument_id == instrument_id
        ]
        if len(spot_matches) == 1:
            return spot_matches[0].price
        futures_matches = [
            item
            for curve in self.futures_curves
            for item in curve.contracts
            if item.contract_id == instrument_id
        ]
        if len(futures_matches) == 1:
            return futures_matches[0].mark_price
        raise MarketStateError(
            f"derivative_underlying_price_not_unique:{instrument_id}"
        )

    def convert(
        self, amount: Decimal, *, from_currency: str, to_currency: str
    ) -> Decimal:
        _decimal(amount, "conversion.amount")
        _require_currency(from_currency, "conversion.from_currency")
        _require_currency(to_currency, "conversion.to_currency")
        if from_currency == to_currency:
            return amount
        direct = self._direct_conversion(
            amount, from_currency=from_currency, to_currency=to_currency
        )
        if direct is not None:
            return direct
        if from_currency != self.base_currency and to_currency != self.base_currency:
            base_amount = self._direct_conversion(
                amount,
                from_currency=from_currency,
                to_currency=self.base_currency,
            )
            if base_amount is not None:
                converted = self._direct_conversion(
                    base_amount,
                    from_currency=self.base_currency,
                    to_currency=to_currency,
                )
                if converted is not None:
                    return converted
        raise MarketStateError(
            f"fx_conversion_path_missing:{from_currency}:{to_currency}"
        )

    def _direct_conversion(
        self, amount: Decimal, *, from_currency: str, to_currency: str
    ) -> Decimal | None:
        for quote in self.fx_quotes:
            if (
                quote.base_currency == from_currency
                and quote.quote_currency == to_currency
            ):
                return amount * quote.rate
            if (
                quote.base_currency == to_currency
                and quote.quote_currency == from_currency
            ):
                return amount / quote.rate
        return None

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "state_id": self.state_id,
            "valuation_at": self.valuation_at,
            "base_currency": self.base_currency,
            "calendar_ids": list(self.calendar_ids),
            "spots": [
                item.as_dict()
                for item in sorted(self.spots, key=lambda item: item.instrument_id)
            ],
            "curves": [
                item.as_dict()
                for item in sorted(self.curves, key=lambda item: item.curve_id)
            ],
            "volatility_surfaces": [
                item.as_dict()
                for item in sorted(
                    self.volatility_surfaces, key=lambda item: item.surface_id
                )
            ],
            "rates": [
                item.as_dict()
                for item in sorted(self.rates, key=lambda item: item.rate_id)
            ],
            "fx_quotes": [
                item.as_dict()
                for item in sorted(self.fx_quotes, key=lambda item: item.pair)
            ],
            "borrow_quotes": [
                item.as_dict()
                for item in sorted(
                    self.borrow_quotes, key=lambda item: item.instrument_id
                )
            ],
            "liquidity_quotes": [
                item.as_dict()
                for item in sorted(
                    self.liquidity_quotes, key=lambda item: item.instrument_id
                )
            ],
            "futures_curves": [
                item.as_dict()
                for item in sorted(
                    self.futures_curves,
                    key=lambda item: item.curve_id,
                )
            ],
            "option_chains": [
                item.as_dict()
                for item in sorted(
                    self.option_chains,
                    key=lambda item: item.chain_id,
                )
            ],
        }

    def state_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="shared_multi_asset_market_state")

    deterministic_hash = state_hash


SpotComponent = SpotQuote
CurveComponent = YieldCurve
VolatilityComponent = VolatilitySurface
RateComponent = RateQuote
FXComponent = FXQuote
BorrowComponent = BorrowQuote
LiquidityComponent = LiquidityQuote
FuturesComponent = FuturesCurveState
OptionComponent = OptionChainState
SharedMarketState = MarketState
