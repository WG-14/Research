"""Deterministic, source-owned option analytics authority.

This module is deliberately independent of market-data collection.  It accepts
immutable, externally prepared provider rows, normalizes their conventions,
retains raw observations, calibrates a reproducible surface, and derives option
analytics through a closed model registry.  Supplier IVs and Greeks are
comparison observations only; they can never become authoritative analytics.
"""

from __future__ import annotations

import math
import random
from dataclasses import InitVar, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from market_research.research.derivatives.common import (
    decimal_text,
    parse_timestamp,
    require_hash,
    require_stable_id,
)
from market_research.research.derivatives.options import (
    ExerciseStyle,
    OptionType,
    QuoteState,
    ValuationInputSnapshot,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.multi_asset.market_state import (
    MarketDataQuality,
    OptionAnalyticsMark,
    OptionContractQuote,
    OptionRight as MarketStateOptionRight,
    QuoteCondition,
)
from market_research.research.multi_asset.option_pricing import (
    BlackScholesPricingAdapter,
)


OPTION_ANALYTICS_SCHEMA_VERSION = 1
_ZERO = Decimal("0")
_ONE = Decimal("1")
_TWO = Decimal("2")
_ONE_PERCENT = Decimal("0.01")
_CALENDAR_DAYS = Decimal("365.25")
_RECEIPT_FACTORY_TOKEN = object()


class OptionAnalyticsError(ValueError):
    """Raised when option evidence or model semantics are incomplete."""


class ProviderPriceConvention(StrEnum):
    """How a provider encodes an option premium."""

    CURRENCY_PER_UNDERLYING_UNIT = "CURRENCY_PER_UNDERLYING_UNIT"
    CURRENCY_PER_CONTRACT = "CURRENCY_PER_CONTRACT"
    INDEX_POINTS = "INDEX_POINTS"


class MissingValueConvention(StrEnum):
    NULL = "NULL"
    ZERO_IS_MISSING = "ZERO_IS_MISSING"
    SENTINEL_IS_MISSING = "SENTINEL_IS_MISSING"


class TimestampConvention(StrEnum):
    UTC_INSTANT = "UTC_INSTANT"
    EXCHANGE_LOCAL = "EXCHANGE_LOCAL"


class DayCountConvention(StrEnum):
    ACT_365_25 = "ACT_365.25"
    ACT_365 = "ACT_365"
    ACT_360 = "ACT_360"


class ForwardMethod(StrEnum):
    SPOT_CARRY = "SPOT_CARRY"
    FUTURES_PRICE = "FUTURES_PRICE"


class SurfaceCoordinate(StrEnum):
    STRIKE = "STRIKE"
    SPOT_MONEYNESS = "SPOT_MONEYNESS"
    FORWARD_MONEYNESS = "FORWARD_MONEYNESS"
    LOG_FORWARD_MONEYNESS = "LOG_FORWARD_MONEYNESS"
    DELTA = "DELTA"
    TOTAL_VARIANCE = "TOTAL_VARIANCE"


class SurfaceExtrapolation(StrEnum):
    REJECT = "REJECT"
    FLAT_VOLATILITY = "FLAT_VOLATILITY"


class SurfaceDiagnosticKind(StrEnum):
    VERTICAL_BOUND = "VERTICAL_BOUND"
    STRIKE_MONOTONICITY = "STRIKE_MONOTONICITY"
    BUTTERFLY_CONVEXITY = "BUTTERFLY_CONVEXITY"
    CALENDAR_TOTAL_VARIANCE = "CALENDAR_TOTAL_VARIANCE"


class OptionModelKind(StrEnum):
    EUROPEAN_BLACK_SCHOLES = "EUROPEAN_BLACK_SCHOLES"
    FUTURES_BLACK_76 = "FUTURES_BLACK_76"
    AMERICAN_CRR_BINOMIAL = "AMERICAN_CRR_BINOMIAL"
    AMERICAN_RICHARDSON_BINOMIAL = "AMERICAN_RICHARDSON_BINOMIAL"
    ASIAN_ARITHMETIC_MONTE_CARLO = "ASIAN_ARITHMETIC_MONTE_CARLO"


class AnalyticsComparisonAction(StrEnum):
    REJECT = "REJECT"
    DEGRADED = "DEGRADED"


class AnalyticsComparisonStatus(StrEnum):
    NOT_PROVIDED = "NOT_PROVIDED"
    MATCHED = "MATCHED"
    DEGRADED = "DEGRADED"


class AnalyticsVolatilitySource(StrEnum):
    MARKET_QUOTE_INVERSION = "MARKET_QUOTE_INVERSION"
    CALIBRATED_SURFACE = "CALIBRATED_SURFACE"


class QuoteQualityAction(StrEnum):
    REJECT = "REJECT"
    ACCEPT_WITH_FLAG = "ACCEPT_WITH_FLAG"
    REPAIR = "REPAIR"


class QuoteQualityDisposition(StrEnum):
    INCLUDED = "INCLUDED"
    MODIFIED = "MODIFIED"
    EXCLUDED = "EXCLUDED"


def _hash(label: str, payload: object) -> str:
    return sha256_prefixed(payload, label=label)


def _finite_decimal(
    value: Decimal | str | int,
    field_name: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise OptionAnalyticsError(f"{field_name}_invalid_decimal") from exc
    if not parsed.is_finite():
        raise OptionAnalyticsError(f"{field_name}_non_finite")
    if positive and parsed <= 0:
        raise OptionAnalyticsError(f"{field_name}_must_be_positive")
    if non_negative and parsed < 0:
        raise OptionAnalyticsError(f"{field_name}_must_be_non_negative")
    return parsed


def _optional_decimal(
    value: Decimal | str | int | None,
    field_name: str,
    *,
    non_negative: bool = False,
) -> Decimal | None:
    if value is None:
        return None
    return _finite_decimal(value, field_name, non_negative=non_negative)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _normal_pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / math.sqrt(2.0 * math.pi)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _provider_raw_timestamp(value: str, field_name: str) -> datetime:
    """Parse a raw provider clock without assuming UTC or exchange-local time."""

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise OptionAnalyticsError(f"{field_name}_invalid_timestamp") from exc


def _day_count_denominator(convention: DayCountConvention) -> Decimal:
    return {
        DayCountConvention.ACT_365_25: Decimal("365.25"),
        DayCountConvention.ACT_365: Decimal("365"),
        DayCountConvention.ACT_360: Decimal("360"),
    }[convention]


def year_fraction(
    start_at: str,
    end_at: str,
    convention: DayCountConvention,
) -> Decimal:
    start = parse_timestamp(start_at, "option_year_fraction.start_at")
    end = parse_timestamp(end_at, "option_year_fraction.end_at")
    if end <= start:
        raise OptionAnalyticsError("option_year_fraction_non_positive")
    seconds = Decimal(str((end - start).total_seconds()))
    return seconds / Decimal("86400") / _day_count_denominator(convention)


@dataclass(frozen=True, slots=True)
class ProviderQuoteConvention:
    """Versioned contract for one provider's price, time, and missing semantics."""

    provider_id: str
    convention_version: str
    price_convention: ProviderPriceConvention
    timestamp_convention: TimestampConvention
    exchange_timezone: str
    missing_value_convention: MissingValueConvention
    missing_sentinel: Decimal | None
    quote_currency: str
    price_scale: Decimal = _ONE
    contract_multiplier_required: bool = True
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.provider_id, "provider_quote_convention.provider_id")
        require_stable_id(
            self.convention_version,
            "provider_quote_convention.convention_version",
        )
        try:
            ZoneInfo(self.exchange_timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise OptionAnalyticsError(
                "provider_quote_convention_timezone_unknown"
            ) from exc
        if len(self.quote_currency) != 3 or not self.quote_currency.isupper():
            raise OptionAnalyticsError("provider_quote_convention_currency_invalid")
        scale = _finite_decimal(
            self.price_scale,
            "provider_quote_convention.price_scale",
            positive=True,
        )
        object.__setattr__(self, "price_scale", scale)
        sentinel = _optional_decimal(
            self.missing_sentinel,
            "provider_quote_convention.missing_sentinel",
        )
        object.__setattr__(self, "missing_sentinel", sentinel)
        if (
            self.missing_value_convention is MissingValueConvention.SENTINEL_IS_MISSING
        ) != (sentinel is not None):
            raise OptionAnalyticsError(
                "provider_quote_convention_missing_sentinel_binding"
            )
        object.__setattr__(
            self,
            "content_hash",
            _hash("provider_option_quote_convention", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
            "provider_id": self.provider_id,
            "convention_version": self.convention_version,
            "price_convention": self.price_convention.value,
            "timestamp_convention": self.timestamp_convention.value,
            "exchange_timezone": self.exchange_timezone,
            "missing_value_convention": self.missing_value_convention.value,
            "missing_sentinel": (
                None
                if self.missing_sentinel is None
                else decimal_text(self.missing_sentinel)
            ),
            "quote_currency": self.quote_currency,
            "price_scale": decimal_text(self.price_scale),
            "contract_multiplier_required": self.contract_multiplier_required,
        }


@dataclass(frozen=True, slots=True)
class ProviderOptionQuoteRow:
    """Raw provider row.  Values are never overwritten during normalization."""

    provider_id: str
    provider_record_id: str
    contract_id: str
    observed_at: str
    published_at: str
    available_at: str
    bid: Decimal | None
    ask: Decimal | None
    bid_size: Decimal
    ask_size: Decimal
    volume: int
    open_interest: int
    source_artifact_hash: str
    provider_schema_hash: str
    supplier_implied_volatility: Decimal | None = None
    supplier_delta: Decimal | None = None
    supplier_gamma: Decimal | None = None
    supplier_vega: Decimal | None = None
    supplier_theta: Decimal | None = None
    supplier_rho: Decimal | None = None
    quote_condition: str = "NORMAL"
    corporate_action_adjustment: str = "UNADJUSTED_STANDARD_CONTRACT"
    raw_payload_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "provider_id",
            "provider_record_id",
            "contract_id",
            "quote_condition",
            "corporate_action_adjustment",
        ):
            require_stable_id(
                str(getattr(self, field_name)),
                f"provider_option_row.{field_name}",
            )
        observed = _provider_raw_timestamp(
            self.observed_at,
            "provider_option_row.observed_at",
        )
        published = _provider_raw_timestamp(
            self.published_at, "provider_option_row.published_at"
        )
        available = _provider_raw_timestamp(
            self.available_at, "provider_option_row.available_at"
        )
        awareness = tuple(
            item.tzinfo is not None for item in (observed, published, available)
        )
        if len(set(awareness)) != 1:
            raise OptionAnalyticsError("provider_option_row_clock_awareness_mismatch")
        if published < observed or available < published:
            raise OptionAnalyticsError("provider_option_row_clock_order_invalid")
        require_hash(
            self.source_artifact_hash,
            "provider_option_row.source_artifact_hash",
        )
        require_hash(
            self.provider_schema_hash,
            "provider_option_row.provider_schema_hash",
        )
        for field_name in ("bid", "ask"):
            object.__setattr__(
                self,
                field_name,
                _optional_decimal(
                    getattr(self, field_name),
                    f"provider_option_row.{field_name}",
                ),
            )
        for field_name in ("bid_size", "ask_size"):
            object.__setattr__(
                self,
                field_name,
                _finite_decimal(
                    getattr(self, field_name),
                    f"provider_option_row.{field_name}",
                    non_negative=True,
                ),
            )
        if (
            isinstance(self.volume, bool)
            or self.volume < 0
            or isinstance(self.open_interest, bool)
            or self.open_interest < 0
        ):
            raise OptionAnalyticsError("provider_option_row_liquidity_invalid")
        for field_name in (
            "supplier_implied_volatility",
            "supplier_delta",
            "supplier_gamma",
            "supplier_vega",
            "supplier_theta",
            "supplier_rho",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_decimal(
                    getattr(self, field_name),
                    f"provider_option_row.{field_name}",
                ),
            )
        object.__setattr__(
            self,
            "raw_payload_hash",
            _hash("provider_option_quote_raw_row", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
            "provider_id": self.provider_id,
            "provider_record_id": self.provider_record_id,
            "contract_id": self.contract_id,
            "observed_at": self.observed_at,
            "published_at": self.published_at,
            "available_at": self.available_at,
            "bid": None if self.bid is None else decimal_text(self.bid),
            "ask": None if self.ask is None else decimal_text(self.ask),
            "bid_size": decimal_text(self.bid_size),
            "ask_size": decimal_text(self.ask_size),
            "volume": self.volume,
            "open_interest": self.open_interest,
            "source_artifact_hash": self.source_artifact_hash,
            "provider_schema_hash": self.provider_schema_hash,
            "supplier_implied_volatility": (
                None
                if self.supplier_implied_volatility is None
                else decimal_text(self.supplier_implied_volatility)
            ),
            "supplier_delta": (
                None
                if self.supplier_delta is None
                else decimal_text(self.supplier_delta)
            ),
            "supplier_gamma": (
                None
                if self.supplier_gamma is None
                else decimal_text(self.supplier_gamma)
            ),
            "supplier_vega": (
                None if self.supplier_vega is None else decimal_text(self.supplier_vega)
            ),
            "supplier_theta": (
                None
                if self.supplier_theta is None
                else decimal_text(self.supplier_theta)
            ),
            "supplier_rho": (
                None if self.supplier_rho is None else decimal_text(self.supplier_rho)
            ),
            "quote_condition": self.quote_condition,
            "corporate_action_adjustment": self.corporate_action_adjustment,
        }


@dataclass(frozen=True, slots=True)
class NormalizedOptionQuote:
    raw_row: ProviderOptionQuoteRow
    contract_id: str
    provider_id: str
    provider_record_id: str
    observed_at_utc: str
    published_at_utc: str
    available_at_utc: str
    currency: str
    price_unit: str
    bid: Decimal | None
    ask: Decimal | None
    bid_size: Decimal
    ask_size: Decimal
    volume: int
    open_interest: int
    quote_condition: str
    corporate_action_adjustment: str
    raw_row_hash: str
    convention_hash: str
    transformation_hash: str
    supplier_implied_volatility: Decimal | None
    supplier_delta: Decimal | None
    supplier_gamma: Decimal | None
    supplier_vega: Decimal | None
    supplier_theta: Decimal | None
    supplier_rho: Decimal | None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.raw_row, ProviderOptionQuoteRow)
            or self.raw_row.raw_payload_hash != self.raw_row_hash
            or self.raw_row.contract_id != self.contract_id
            or self.raw_row.provider_id != self.provider_id
            or self.raw_row.provider_record_id != self.provider_record_id
        ):
            raise OptionAnalyticsError(
                "normalized_option_quote_raw_row_binding_mismatch"
            )
        for field_name in (
            "contract_id",
            "provider_id",
            "provider_record_id",
            "price_unit",
            "quote_condition",
            "corporate_action_adjustment",
        ):
            require_stable_id(
                str(getattr(self, field_name)),
                f"normalized_option_quote.{field_name}",
            )
        observed = parse_timestamp(
            self.observed_at_utc,
            "normalized_option_quote.observed_at_utc",
        )
        published = parse_timestamp(
            self.published_at_utc,
            "normalized_option_quote.published_at_utc",
        )
        available = parse_timestamp(
            self.available_at_utc,
            "normalized_option_quote.available_at_utc",
        )
        if published < observed or available < published:
            raise OptionAnalyticsError("normalized_option_quote_clock_order_invalid")
        if len(self.currency) != 3 or not self.currency.isupper():
            raise OptionAnalyticsError("normalized_option_quote_currency_invalid")
        for field_name in ("bid", "ask"):
            object.__setattr__(
                self,
                field_name,
                _optional_decimal(
                    getattr(self, field_name),
                    f"normalized_option_quote.{field_name}",
                    non_negative=True,
                ),
            )
        for field_name in ("bid_size", "ask_size"):
            object.__setattr__(
                self,
                field_name,
                _finite_decimal(
                    getattr(self, field_name),
                    f"normalized_option_quote.{field_name}",
                    non_negative=True,
                ),
            )
        if (
            isinstance(self.volume, bool)
            or not isinstance(self.volume, int)
            or self.volume < 0
            or isinstance(self.open_interest, bool)
            or not isinstance(self.open_interest, int)
            or self.open_interest < 0
        ):
            raise OptionAnalyticsError("normalized_option_quote_liquidity_invalid")
        for field_name in (
            "supplier_implied_volatility",
            "supplier_delta",
            "supplier_gamma",
            "supplier_vega",
            "supplier_theta",
            "supplier_rho",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_decimal(
                    getattr(self, field_name),
                    f"normalized_option_quote.{field_name}",
                ),
            )
        for value, name in (
            (self.raw_row_hash, "normalized_option_quote.raw_row_hash"),
            (self.convention_hash, "normalized_option_quote.convention_hash"),
            (
                self.transformation_hash,
                "normalized_option_quote.transformation_hash",
            ),
        ):
            require_hash(value, name)
        object.__setattr__(
            self,
            "content_hash",
            _hash("normalized_option_quote", self.identity_payload()),
        )

    @property
    def midpoint(self) -> Decimal:
        if self.bid is None or self.ask is None:
            raise OptionAnalyticsError("normalized_option_quote_two_sided_required")
        return (self.bid + self.ask) / _TWO

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
            "raw_row": self.raw_row.identity_payload(),
            "contract_id": self.contract_id,
            "provider_id": self.provider_id,
            "provider_record_id": self.provider_record_id,
            "observed_at_utc": self.observed_at_utc,
            "published_at_utc": self.published_at_utc,
            "available_at_utc": self.available_at_utc,
            "currency": self.currency,
            "price_unit": self.price_unit,
            "bid": None if self.bid is None else decimal_text(self.bid),
            "ask": None if self.ask is None else decimal_text(self.ask),
            "bid_size": decimal_text(self.bid_size),
            "ask_size": decimal_text(self.ask_size),
            "volume": self.volume,
            "open_interest": self.open_interest,
            "quote_condition": self.quote_condition,
            "corporate_action_adjustment": self.corporate_action_adjustment,
            "raw_row_hash": self.raw_row_hash,
            "convention_hash": self.convention_hash,
            "transformation_hash": self.transformation_hash,
            "supplier_implied_volatility": (
                None
                if self.supplier_implied_volatility is None
                else decimal_text(self.supplier_implied_volatility)
            ),
            "supplier_delta": (
                None
                if self.supplier_delta is None
                else decimal_text(self.supplier_delta)
            ),
            "supplier_gamma": (
                None
                if self.supplier_gamma is None
                else decimal_text(self.supplier_gamma)
            ),
            "supplier_vega": (
                None if self.supplier_vega is None else decimal_text(self.supplier_vega)
            ),
            "supplier_theta": (
                None
                if self.supplier_theta is None
                else decimal_text(self.supplier_theta)
            ),
            "supplier_rho": (
                None if self.supplier_rho is None else decimal_text(self.supplier_rho)
            ),
        }


class ProviderNormalizationAdapter(Protocol):
    @property
    def convention(self) -> ProviderQuoteConvention: ...

    def normalize(
        self,
        row: ProviderOptionQuoteRow,
        *,
        contract_multiplier: Decimal,
    ) -> NormalizedOptionQuote: ...


@dataclass(frozen=True, slots=True)
class DeterministicProviderNormalizationAdapter:
    convention: ProviderQuoteConvention
    adapter_version: str = "deterministic_provider_option_normalizer_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(
            self.adapter_version,
            "provider_normalization_adapter.adapter_version",
        )
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "provider_option_normalization_adapter",
                {
                    "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
                    "adapter_version": self.adapter_version,
                    "convention_hash": self.convention.content_hash,
                },
            ),
        )

    def _missing(self, value: Decimal | None) -> bool:
        if value is None:
            return True
        if (
            self.convention.missing_value_convention
            is MissingValueConvention.ZERO_IS_MISSING
            and value == 0
        ):
            return True
        return (
            self.convention.missing_value_convention
            is MissingValueConvention.SENTINEL_IS_MISSING
            and value == self.convention.missing_sentinel
        )

    def _utc(self, value: str) -> str:
        if self.convention.timestamp_convention is TimestampConvention.UTC_INSTANT:
            return _utc_text(parse_timestamp(value, "provider_option_quote.timestamp"))
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            raise OptionAnalyticsError("provider_local_timestamp_must_be_naive")
        zone = ZoneInfo(self.convention.exchange_timezone)
        localized = parsed.replace(tzinfo=zone, fold=0)
        if (
            localized.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
            != parsed
        ):
            raise OptionAnalyticsError("provider_local_timestamp_invalid")
        return _utc_text(localized)

    def normalize(
        self,
        row: ProviderOptionQuoteRow,
        *,
        contract_multiplier: Decimal,
    ) -> NormalizedOptionQuote:
        if row.provider_id != self.convention.provider_id:
            raise OptionAnalyticsError("provider_normalization_provider_mismatch")
        multiplier = _finite_decimal(
            contract_multiplier,
            "provider_normalization.contract_multiplier",
            positive=True,
        )

        def normalize_price(value: Decimal | None) -> Decimal | None:
            if self._missing(value):
                return None
            assert value is not None
            scaled = value * self.convention.price_scale
            if (
                self.convention.price_convention
                is ProviderPriceConvention.CURRENCY_PER_CONTRACT
            ):
                scaled /= multiplier
            if scaled < 0:
                raise OptionAnalyticsError("provider_normalized_price_negative")
            return scaled

        bid = normalize_price(row.bid)
        ask = normalize_price(row.ask)
        transformation_hash = _hash(
            "provider_option_normalization_transform",
            {
                "adapter_hash": self.content_hash,
                "raw_row_hash": row.raw_payload_hash,
                "contract_multiplier": decimal_text(multiplier),
            },
        )
        return NormalizedOptionQuote(
            raw_row=row,
            contract_id=row.contract_id,
            provider_id=row.provider_id,
            provider_record_id=row.provider_record_id,
            observed_at_utc=self._utc(row.observed_at),
            published_at_utc=self._utc(row.published_at),
            available_at_utc=self._utc(row.available_at),
            currency=self.convention.quote_currency,
            price_unit=f"{self.convention.quote_currency}_per_underlying_unit",
            bid=bid,
            ask=ask,
            bid_size=row.bid_size,
            ask_size=row.ask_size,
            volume=row.volume,
            open_interest=row.open_interest,
            quote_condition=row.quote_condition,
            corporate_action_adjustment=row.corporate_action_adjustment,
            raw_row_hash=row.raw_payload_hash,
            convention_hash=self.convention.content_hash,
            transformation_hash=transformation_hash,
            supplier_implied_volatility=row.supplier_implied_volatility,
            supplier_delta=row.supplier_delta,
            supplier_gamma=row.supplier_gamma,
            supplier_vega=row.supplier_vega,
            supplier_theta=row.supplier_theta,
            supplier_rho=row.supplier_rho,
        )


def standard_provider_adapters() -> tuple[
    DeterministicProviderNormalizationAdapter, ...
]:
    """Two deliberately different provider conventions for contract tests."""

    return (
        DeterministicProviderNormalizationAdapter(
            ProviderQuoteConvention(
                provider_id="provider_unit_utc",
                convention_version="v1",
                price_convention=(ProviderPriceConvention.CURRENCY_PER_UNDERLYING_UNIT),
                timestamp_convention=TimestampConvention.UTC_INSTANT,
                exchange_timezone="UTC",
                missing_value_convention=MissingValueConvention.NULL,
                missing_sentinel=None,
                quote_currency="USD",
            )
        ),
        DeterministicProviderNormalizationAdapter(
            ProviderQuoteConvention(
                provider_id="provider_contract_local",
                convention_version="v1",
                price_convention=ProviderPriceConvention.CURRENCY_PER_CONTRACT,
                timestamp_convention=TimestampConvention.EXCHANGE_LOCAL,
                exchange_timezone="America/New_York",
                missing_value_convention=MissingValueConvention.ZERO_IS_MISSING,
                missing_sentinel=None,
                quote_currency="USD",
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class OptionQuoteQualityPolicy:
    """Fail-closed quote policy with explicit repair and candidate semantics."""

    policy_id: str
    policy_version: str
    maximum_age_seconds: int
    maximum_alignment_seconds: int
    maximum_relative_spread: Decimal
    minimum_quote_size: Decimal
    minimum_volume: int
    minimum_open_interest: int
    minimum_price_tick: Decimal
    crossed_market_action: QuoteQualityAction
    locked_market_action: QuoteQualityAction
    zero_bid_action: QuoteQualityAction
    arbitrage_candidate_action: QuoteQualityAction
    allowed_exercise_styles: tuple[str, ...]
    allowed_settlement_styles: tuple[str, ...]
    reject_adjusted_contracts: bool = True
    intrinsic_bound_tolerance: Decimal = Decimal("0.00000001")
    arbitrage_tolerance: Decimal = Decimal("0.00000001")
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.policy_id, "option_quote_quality.policy_id")
        require_stable_id(
            self.policy_version,
            "option_quote_quality.policy_version",
        )
        for field_name in ("maximum_age_seconds", "maximum_alignment_seconds"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise OptionAnalyticsError(f"option_quote_quality_{field_name}_invalid")
        for field_name in (
            "maximum_relative_spread",
            "minimum_price_tick",
        ):
            object.__setattr__(
                self,
                field_name,
                _finite_decimal(
                    getattr(self, field_name),
                    f"option_quote_quality.{field_name}",
                    positive=True,
                ),
            )
        object.__setattr__(
            self,
            "minimum_quote_size",
            _finite_decimal(
                self.minimum_quote_size,
                "option_quote_quality.minimum_quote_size",
                non_negative=True,
            ),
        )
        for field_name in ("minimum_volume", "minimum_open_interest"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise OptionAnalyticsError(f"option_quote_quality_{field_name}_invalid")
        for field_name in ("intrinsic_bound_tolerance", "arbitrage_tolerance"):
            object.__setattr__(
                self,
                field_name,
                _finite_decimal(
                    getattr(self, field_name),
                    f"option_quote_quality.{field_name}",
                    non_negative=True,
                ),
            )
        if self.crossed_market_action is QuoteQualityAction.ACCEPT_WITH_FLAG:
            raise OptionAnalyticsError(
                "option_quote_quality_crossed_market_cannot_be_accepted"
            )
        if self.zero_bid_action is QuoteQualityAction.ACCEPT_WITH_FLAG:
            raise OptionAnalyticsError(
                "option_quote_quality_zero_bid_cannot_be_accepted"
            )
        if self.locked_market_action is QuoteQualityAction.REPAIR:
            raise OptionAnalyticsError(
                "option_quote_quality_locked_market_cannot_be_repaired"
            )
        exercise_styles = tuple(sorted(set(self.allowed_exercise_styles)))
        settlement_styles = tuple(sorted(set(self.allowed_settlement_styles)))
        if not exercise_styles or not settlement_styles:
            raise OptionAnalyticsError("option_quote_quality_contract_styles_required")
        for value in (*exercise_styles, *settlement_styles):
            require_stable_id(value, "option_quote_quality.contract_style")
        object.__setattr__(self, "allowed_exercise_styles", exercise_styles)
        object.__setattr__(self, "allowed_settlement_styles", settlement_styles)
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "option_quote_quality_policy",
                {
                    "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
                    "policy_id": self.policy_id,
                    "policy_version": self.policy_version,
                    "maximum_age_seconds": self.maximum_age_seconds,
                    "maximum_alignment_seconds": self.maximum_alignment_seconds,
                    "maximum_relative_spread": decimal_text(
                        self.maximum_relative_spread
                    ),
                    "minimum_quote_size": decimal_text(self.minimum_quote_size),
                    "minimum_volume": self.minimum_volume,
                    "minimum_open_interest": self.minimum_open_interest,
                    "minimum_price_tick": decimal_text(self.minimum_price_tick),
                    "crossed_market_action": self.crossed_market_action.value,
                    "locked_market_action": self.locked_market_action.value,
                    "zero_bid_action": self.zero_bid_action.value,
                    "arbitrage_candidate_action": (
                        self.arbitrage_candidate_action.value
                    ),
                    "allowed_exercise_styles": list(exercise_styles),
                    "allowed_settlement_styles": list(settlement_styles),
                    "reject_adjusted_contracts": self.reject_adjusted_contracts,
                    "intrinsic_bound_tolerance": decimal_text(
                        self.intrinsic_bound_tolerance
                    ),
                    "arbitrage_tolerance": decimal_text(self.arbitrage_tolerance),
                },
            ),
        )


def default_option_quote_quality_policy() -> OptionQuoteQualityPolicy:
    return OptionQuoteQualityPolicy(
        policy_id="authoritative_option_quote_quality",
        policy_version="v1",
        maximum_age_seconds=60,
        maximum_alignment_seconds=60,
        maximum_relative_spread=Decimal("0.25"),
        minimum_quote_size=Decimal("1"),
        minimum_volume=1,
        minimum_open_interest=1,
        minimum_price_tick=Decimal("0.01"),
        crossed_market_action=QuoteQualityAction.REJECT,
        locked_market_action=QuoteQualityAction.ACCEPT_WITH_FLAG,
        zero_bid_action=QuoteQualityAction.REJECT,
        arbitrage_candidate_action=QuoteQualityAction.ACCEPT_WITH_FLAG,
        allowed_exercise_styles=("AMERICAN", "EUROPEAN"),
        allowed_settlement_styles=("CASH", "PHYSICAL"),
    )


@dataclass(frozen=True, slots=True)
class OptionQuoteQualityContext:
    contract_id: str
    underlying_id: str
    quote_underlying_id: str
    option_type: OptionType
    exercise_style: str
    settlement_style: str
    expiry_at: str
    strike: Decimal
    spot_price: Decimal
    discount_factor: Decimal
    decision_at: str
    underlying_observed_at: str
    rate_observed_at: str
    dividend_observed_at: str
    market_state_hash: str
    underlying_hash: str
    rate_hash: str
    dividend_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "contract_id",
            "underlying_id",
            "quote_underlying_id",
            "exercise_style",
            "settlement_style",
        ):
            require_stable_id(
                str(getattr(self, field_name)),
                f"option_quote_quality_context.{field_name}",
            )
        expiry = parse_timestamp(
            self.expiry_at,
            "option_quote_quality_context.expiry_at",
        )
        decision = parse_timestamp(
            self.decision_at,
            "option_quote_quality_context.decision_at",
        )
        if expiry <= decision:
            raise OptionAnalyticsError("option_quote_quality_context_contract_expired")
        for field_name in (
            "underlying_observed_at",
            "rate_observed_at",
            "dividend_observed_at",
        ):
            parse_timestamp(
                str(getattr(self, field_name)),
                f"option_quote_quality_context.{field_name}",
            )
        for field_name in ("strike", "spot_price", "discount_factor"):
            object.__setattr__(
                self,
                field_name,
                _finite_decimal(
                    getattr(self, field_name),
                    f"option_quote_quality_context.{field_name}",
                    positive=True,
                ),
            )
        if self.discount_factor > 1:
            raise OptionAnalyticsError(
                "option_quote_quality_context_discount_factor_exceeds_one"
            )
        for field_name in (
            "market_state_hash",
            "underlying_hash",
            "rate_hash",
            "dividend_hash",
        ):
            require_hash(
                str(getattr(self, field_name)),
                f"option_quote_quality_context.{field_name}",
            )
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "option_quote_quality_context",
                {
                    "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
                    "contract_id": self.contract_id,
                    "underlying_id": self.underlying_id,
                    "quote_underlying_id": self.quote_underlying_id,
                    "option_type": self.option_type.value,
                    "exercise_style": self.exercise_style,
                    "settlement_style": self.settlement_style,
                    "expiry_at": self.expiry_at,
                    "strike": decimal_text(self.strike),
                    "spot_price": decimal_text(self.spot_price),
                    "discount_factor": decimal_text(self.discount_factor),
                    "decision_at": self.decision_at,
                    "underlying_observed_at": self.underlying_observed_at,
                    "rate_observed_at": self.rate_observed_at,
                    "dividend_observed_at": self.dividend_observed_at,
                    "market_state_hash": self.market_state_hash,
                    "underlying_hash": self.underlying_hash,
                    "rate_hash": self.rate_hash,
                    "dividend_hash": self.dividend_hash,
                },
            ),
        )


@dataclass(frozen=True, slots=True)
class OptionQuoteQualityCandidate:
    quote: NormalizedOptionQuote
    context: OptionQuoteQualityContext

    def __post_init__(self) -> None:
        if self.quote.contract_id != self.context.contract_id:
            raise OptionAnalyticsError(
                "option_quote_quality_candidate_contract_mismatch"
            )


@dataclass(frozen=True, slots=True)
class OptionQuoteQualityRecord:
    raw_quote: NormalizedOptionQuote
    context_hash: str
    policy_hash: str
    disposition: QuoteQualityDisposition
    corrected_bid: Decimal | None
    corrected_ask: Decimal | None
    reasons: tuple[str, ...]
    quote_age_seconds: int
    lineage_hashes: tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_hash(self.context_hash, "option_quote_quality_record.context_hash")
        require_hash(self.policy_hash, "option_quote_quality_record.policy_hash")
        for field_name in ("corrected_bid", "corrected_ask"):
            object.__setattr__(
                self,
                field_name,
                _optional_decimal(
                    getattr(self, field_name),
                    f"option_quote_quality_record.{field_name}",
                    non_negative=True,
                ),
            )
        reasons = tuple(sorted(set(self.reasons)))
        for reason in reasons:
            require_stable_id(reason, "option_quote_quality_record.reason")
        object.__setattr__(self, "reasons", reasons)
        if self.disposition is QuoteQualityDisposition.INCLUDED and reasons:
            raise OptionAnalyticsError("option_quote_quality_included_with_reasons")
        if self.disposition is not QuoteQualityDisposition.INCLUDED and not reasons:
            raise OptionAnalyticsError(
                "option_quote_quality_nonincluded_without_reason"
            )
        if self.disposition is not QuoteQualityDisposition.EXCLUDED and (
            self.corrected_bid is None
            or self.corrected_ask is None
            or self.corrected_bid <= 0
            or self.corrected_ask < self.corrected_bid
        ):
            raise OptionAnalyticsError("option_quote_quality_included_market_invalid")
        if isinstance(self.quote_age_seconds, bool) or not isinstance(
            self.quote_age_seconds, int
        ):
            raise OptionAnalyticsError("option_quote_quality_age_invalid")
        lineage = tuple(self.lineage_hashes)
        if len(lineage) != len(set(lineage)):
            raise OptionAnalyticsError("option_quote_quality_lineage_duplicate")
        for value in lineage:
            require_hash(value, "option_quote_quality_record.lineage_hash")
        object.__setattr__(self, "lineage_hashes", lineage)
        object.__setattr__(
            self,
            "content_hash",
            _hash("option_quote_quality_record", self.identity_payload()),
        )

    @property
    def contract_id(self) -> str:
        return self.raw_quote.contract_id

    @property
    def included(self) -> bool:
        return self.disposition is not QuoteQualityDisposition.EXCLUDED

    @property
    def midpoint(self) -> Decimal:
        if self.corrected_bid is None or self.corrected_ask is None:
            raise OptionAnalyticsError("option_quote_quality_record_two_sided_required")
        return (self.corrected_bid + self.corrected_ask) / _TWO

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
            "raw_quote": self.raw_quote.identity_payload(),
            "raw_quote_hash": self.raw_quote.content_hash,
            "context_hash": self.context_hash,
            "policy_hash": self.policy_hash,
            "disposition": self.disposition.value,
            "corrected_bid": (
                None if self.corrected_bid is None else decimal_text(self.corrected_bid)
            ),
            "corrected_ask": (
                None if self.corrected_ask is None else decimal_text(self.corrected_ask)
            ),
            "reasons": list(self.reasons),
            "quote_age_seconds": self.quote_age_seconds,
            "lineage_hashes": list(self.lineage_hashes),
        }


@dataclass(frozen=True, slots=True)
class QualityScreenedOptionChain:
    chain_id: str
    decision_at: str
    underlying_id: str
    market_state_hash: str
    policy_hash: str
    records: tuple[OptionQuoteQualityRecord, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.chain_id, "quality_screened_chain.chain_id")
        parse_timestamp(self.decision_at, "quality_screened_chain.decision_at")
        require_stable_id(
            self.underlying_id,
            "quality_screened_chain.underlying_id",
        )
        require_hash(
            self.market_state_hash,
            "quality_screened_chain.market_state_hash",
        )
        require_hash(self.policy_hash, "quality_screened_chain.policy_hash")
        records = tuple(sorted(self.records, key=lambda item: item.contract_id))
        if not records:
            raise OptionAnalyticsError("quality_screened_chain_records_required")
        if len({item.contract_id for item in records}) != len(records):
            raise OptionAnalyticsError("quality_screened_chain_contract_duplicate")
        if any(item.policy_hash != self.policy_hash for item in records):
            raise OptionAnalyticsError("quality_screened_chain_policy_binding_mismatch")
        object.__setattr__(self, "records", records)
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "quality_screened_option_chain",
                {
                    "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
                    "chain_id": self.chain_id,
                    "decision_at": self.decision_at,
                    "underlying_id": self.underlying_id,
                    "market_state_hash": self.market_state_hash,
                    "policy_hash": self.policy_hash,
                    "record_hashes": [item.content_hash for item in records],
                },
            ),
        )

    @property
    def included_records(self) -> tuple[OptionQuoteQualityRecord, ...]:
        return tuple(item for item in self.records if item.included)

    @property
    def modified_records(self) -> tuple[OptionQuoteQualityRecord, ...]:
        return tuple(
            item
            for item in self.records
            if item.disposition is QuoteQualityDisposition.MODIFIED
        )

    @property
    def excluded_records(self) -> tuple[OptionQuoteQualityRecord, ...]:
        return tuple(
            item
            for item in self.records
            if item.disposition is QuoteQualityDisposition.EXCLUDED
        )


def _quality_action(
    *,
    action: QuoteQualityAction,
    rejection_reason: str,
    flag_reason: str,
    reasons: list[str],
) -> bool:
    if action is QuoteQualityAction.REJECT:
        reasons.append(rejection_reason)
        return True
    reasons.append(flag_reason)
    return False


def screen_option_quote_quality(
    *,
    chain_id: str,
    candidates: Sequence[OptionQuoteQualityCandidate],
    policy: OptionQuoteQualityPolicy,
) -> QualityScreenedOptionChain:
    """Screen a synchronized chain while retaining every raw and corrected row."""

    ordered = tuple(sorted(candidates, key=lambda item: item.quote.contract_id))
    if not ordered:
        raise OptionAnalyticsError("option_quote_quality_candidates_required")
    if len({item.quote.contract_id for item in ordered}) != len(ordered):
        raise OptionAnalyticsError("option_quote_quality_contract_duplicate")
    decisions = {item.context.decision_at for item in ordered}
    underlyings = {item.context.underlying_id for item in ordered}
    market_states = {item.context.market_state_hash for item in ordered}
    if len(decisions) != 1 or len(underlyings) != 1 or len(market_states) != 1:
        raise OptionAnalyticsError("option_quote_quality_chain_context_inconsistent")

    records: dict[str, OptionQuoteQualityRecord] = {}
    for candidate in ordered:
        quote = candidate.quote
        context = candidate.context
        reasons: list[str] = []
        excluded = False
        bid = quote.bid
        ask = quote.ask
        decision = parse_timestamp(
            context.decision_at,
            "option_quote_quality.decision_at",
        )
        observed = parse_timestamp(
            quote.observed_at_utc,
            "option_quote_quality.observed_at",
        )
        available = parse_timestamp(
            quote.available_at_utc,
            "option_quote_quality.available_at",
        )
        quote_age = int((decision - observed).total_seconds())
        if available > decision:
            reasons.append("FUTURE_KNOWLEDGE")
            excluded = True
        if quote_age < 0:
            reasons.append("FUTURE_OBSERVATION")
            excluded = True
        elif quote_age > policy.maximum_age_seconds:
            reasons.append("STALE_QUOTE")
            excluded = True
        synchronized = [
            observed,
            parse_timestamp(
                context.underlying_observed_at,
                "option_quote_quality.underlying_observed_at",
            ),
            parse_timestamp(
                context.rate_observed_at,
                "option_quote_quality.rate_observed_at",
            ),
            parse_timestamp(
                context.dividend_observed_at,
                "option_quote_quality.dividend_observed_at",
            ),
        ]
        if (max(synchronized) - min(synchronized)).total_seconds() > (
            policy.maximum_alignment_seconds
        ):
            reasons.append("MARKET_INPUT_TIME_MISALIGNMENT")
            excluded = True
        if context.quote_underlying_id != context.underlying_id:
            reasons.append("UNDERLYING_MISMATCH")
            excluded = True
        if context.exercise_style not in policy.allowed_exercise_styles:
            reasons.append("EXERCISE_STYLE_UNSUPPORTED")
            excluded = True
        if context.settlement_style not in policy.allowed_settlement_styles:
            reasons.append("SETTLEMENT_STYLE_UNSUPPORTED")
            excluded = True
        if (
            quote.corporate_action_adjustment != "UNADJUSTED_STANDARD_CONTRACT"
            and policy.reject_adjusted_contracts
        ):
            reasons.append("ADJUSTED_CONTRACT_EXCLUDED")
            excluded = True
        if quote.quote_condition not in {"NORMAL", "INDICATIVE"}:
            reasons.append("QUOTE_CONDITION_UNUSABLE")
            excluded = True
        elif quote.quote_condition == "INDICATIVE":
            reasons.append("INDICATIVE_QUOTE_ACCEPTED_WITH_FLAG")

        if bid is None or ask is None:
            reasons.append("MISSING_TWO_SIDED_QUOTE")
            excluded = True
        else:
            if bid > ask:
                if policy.crossed_market_action is QuoteQualityAction.REPAIR:
                    bid, ask = ask, bid
                    reasons.append("CROSSED_MARKET_REPAIRED_BY_SWAP")
                else:
                    excluded = (
                        _quality_action(
                            action=policy.crossed_market_action,
                            rejection_reason="CROSSED_MARKET_EXCLUDED",
                            flag_reason="CROSSED_MARKET_ACCEPTED_WITH_FLAG",
                            reasons=reasons,
                        )
                        or excluded
                    )
            if bid == 0:
                if policy.zero_bid_action is QuoteQualityAction.REPAIR:
                    if ask >= policy.minimum_price_tick:
                        bid = policy.minimum_price_tick
                        reasons.append("ZERO_BID_REPAIRED_TO_MINIMUM_TICK")
                    else:
                        reasons.append("ZERO_BID_REPAIR_OUTSIDE_MARKET")
                        excluded = True
                else:
                    excluded = (
                        _quality_action(
                            action=policy.zero_bid_action,
                            rejection_reason="ZERO_BID_EXCLUDED",
                            flag_reason="ZERO_BID_ACCEPTED_WITH_FLAG",
                            reasons=reasons,
                        )
                        or excluded
                    )
            if bid == ask and bid > 0:
                excluded = (
                    _quality_action(
                        action=policy.locked_market_action,
                        rejection_reason="LOCKED_MARKET_EXCLUDED",
                        flag_reason="LOCKED_MARKET_ACCEPTED_WITH_FLAG",
                        reasons=reasons,
                    )
                    or excluded
                )
            if bid < 0 or ask <= 0:
                reasons.append("NON_POSITIVE_QUOTE")
                excluded = True
            elif bid <= ask:
                midpoint = (bid + ask) / _TWO
                if (ask - bid) / midpoint > policy.maximum_relative_spread:
                    reasons.append("RELATIVE_SPREAD_EXCEEDED")
                    excluded = True
                discounted_strike = context.strike * context.discount_factor
                intrinsic = (
                    max(_ZERO, context.spot_price - discounted_strike)
                    if context.option_type is OptionType.CALL
                    else max(_ZERO, discounted_strike - context.spot_price)
                )
                if midpoint + policy.intrinsic_bound_tolerance < intrinsic:
                    reasons.append("INTRINSIC_LOWER_BOUND_VIOLATION")
                    excluded = True
        if min(quote.bid_size, quote.ask_size) < policy.minimum_quote_size:
            reasons.append("MINIMUM_QUOTE_SIZE_NOT_MET")
            excluded = True
        if quote.volume < policy.minimum_volume:
            reasons.append("MINIMUM_VOLUME_NOT_MET")
            excluded = True
        if quote.open_interest < policy.minimum_open_interest:
            reasons.append("MINIMUM_OPEN_INTEREST_NOT_MET")
            excluded = True

        disposition = (
            QuoteQualityDisposition.EXCLUDED
            if excluded
            else (
                QuoteQualityDisposition.MODIFIED
                if reasons or bid != quote.bid or ask != quote.ask
                else QuoteQualityDisposition.INCLUDED
            )
        )
        records[quote.contract_id] = OptionQuoteQualityRecord(
            raw_quote=quote,
            context_hash=context.content_hash,
            policy_hash=policy.content_hash,
            disposition=disposition,
            corrected_bid=bid,
            corrected_ask=ask,
            reasons=tuple(reasons),
            quote_age_seconds=quote_age,
            lineage_hashes=(
                quote.raw_row_hash,
                quote.convention_hash,
                quote.transformation_hash,
                context.content_hash,
            ),
        )

    def mark_candidate(contract_ids: Sequence[str], reason: str) -> None:
        for contract_id in contract_ids:
            record = records[contract_id]
            reasons = tuple(sorted((*record.reasons, reason)))
            disposition: QuoteQualityDisposition
            if record.disposition is QuoteQualityDisposition.EXCLUDED:
                disposition = record.disposition
            elif policy.arbitrage_candidate_action is QuoteQualityAction.REJECT:
                disposition = QuoteQualityDisposition.EXCLUDED
            else:
                disposition = QuoteQualityDisposition.MODIFIED
            records[contract_id] = replace(
                record,
                disposition=disposition,
                reasons=reasons,
            )

    usable = [
        item
        for item in ordered
        if records[item.quote.contract_id].included
        and records[item.quote.contract_id].corrected_bid is not None
        and records[item.quote.contract_id].corrected_ask is not None
    ]
    strike_groups: dict[tuple[str, OptionType], list[OptionQuoteQualityCandidate]] = {}
    for item in usable:
        strike_groups.setdefault(
            (item.context.expiry_at, item.context.option_type),
            [],
        ).append(item)
    tolerance = policy.arbitrage_tolerance
    for rows in strike_groups.values():
        rows.sort(key=lambda item: item.context.strike)
        for left, right in zip(rows, rows[1:]):
            left_price = records[left.quote.contract_id].midpoint
            right_price = records[right.quote.contract_id].midpoint
            violated = (
                right_price > left_price + tolerance
                if left.context.option_type is OptionType.CALL
                else right_price + tolerance < left_price
            )
            if violated:
                mark_candidate(
                    (left.quote.contract_id, right.quote.contract_id),
                    "VERTICAL_ARBITRAGE_CANDIDATE",
                )
        for left, middle, right in zip(rows, rows[1:], rows[2:]):
            left_price = records[left.quote.contract_id].midpoint
            middle_price = records[middle.quote.contract_id].midpoint
            right_price = records[right.quote.contract_id].midpoint
            left_slope = (middle_price - left_price) / (
                middle.context.strike - left.context.strike
            )
            right_slope = (right_price - middle_price) / (
                right.context.strike - middle.context.strike
            )
            if right_slope + tolerance < left_slope:
                mark_candidate(
                    (
                        left.quote.contract_id,
                        middle.quote.contract_id,
                        right.quote.contract_id,
                    ),
                    "BUTTERFLY_ARBITRAGE_CANDIDATE",
                )

    calendar_groups: dict[
        tuple[Decimal, OptionType], list[OptionQuoteQualityCandidate]
    ] = {}
    for item in usable:
        calendar_groups.setdefault(
            (item.context.strike, item.context.option_type),
            [],
        ).append(item)
    for rows in calendar_groups.values():
        rows.sort(
            key=lambda item: parse_timestamp(
                item.context.expiry_at,
                "option_quote_quality.expiry_at",
            )
        )
        for near, far in zip(rows, rows[1:]):
            if records[far.quote.contract_id].midpoint + tolerance < (
                records[near.quote.contract_id].midpoint
            ):
                mark_candidate(
                    (near.quote.contract_id, far.quote.contract_id),
                    "CALENDAR_ARBITRAGE_CANDIDATE",
                )

    return QualityScreenedOptionChain(
        chain_id=chain_id,
        decision_at=next(iter(decisions)),
        underlying_id=next(iter(underlyings)),
        market_state_hash=next(iter(market_states)),
        policy_hash=policy.content_hash,
        records=tuple(records.values()),
    )


@dataclass(frozen=True, slots=True)
class DiscreteDividend:
    ex_at: str
    amount: Decimal
    source_hash: str

    def __post_init__(self) -> None:
        parse_timestamp(self.ex_at, "discrete_dividend.ex_at")
        object.__setattr__(
            self,
            "amount",
            _finite_decimal(
                self.amount,
                "discrete_dividend.amount",
                non_negative=True,
            ),
        )
        require_hash(self.source_hash, "discrete_dividend.source_hash")


@dataclass(frozen=True, slots=True)
class ForwardInput:
    valuation_at: str
    expiry_at: str
    spot_price: Decimal | None
    futures_price: Decimal | None
    risk_free_rate: Decimal
    dividend_yield: Decimal
    borrow_rate: Decimal
    day_count: DayCountConvention
    settlement_convention: str
    discrete_dividends: tuple[DiscreteDividend, ...]
    market_state_hash: str
    policy_hash: str
    input_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.day_count, DayCountConvention):
            raise OptionAnalyticsError("forward_input_day_count_invalid")
        year_fraction(self.valuation_at, self.expiry_at, self.day_count)
        require_stable_id(
            self.settlement_convention,
            "forward_input.settlement_convention",
        )
        if self.spot_price is not None:
            object.__setattr__(
                self,
                "spot_price",
                _finite_decimal(
                    self.spot_price, "forward_input.spot_price", positive=True
                ),
            )
        if self.futures_price is not None:
            object.__setattr__(
                self,
                "futures_price",
                _finite_decimal(
                    self.futures_price,
                    "forward_input.futures_price",
                    positive=True,
                ),
            )
        for name in ("risk_free_rate", "dividend_yield", "borrow_rate"):
            object.__setattr__(
                self,
                name,
                _finite_decimal(getattr(self, name), f"forward_input.{name}"),
            )
        dividends = tuple(sorted(self.discrete_dividends, key=lambda item: item.ex_at))
        if len({item.ex_at for item in dividends}) != len(dividends):
            raise OptionAnalyticsError("forward_input_dividend_time_duplicate")
        valuation = parse_timestamp(self.valuation_at, "forward_input.valuation_at")
        expiry = parse_timestamp(self.expiry_at, "forward_input.expiry_at")
        if any(
            not valuation
            < parse_timestamp(item.ex_at, "discrete_dividend.ex_at")
            <= expiry
            for item in dividends
        ):
            raise OptionAnalyticsError("forward_input_dividend_outside_horizon")
        object.__setattr__(self, "discrete_dividends", dividends)
        require_hash(self.market_state_hash, "forward_input.market_state_hash")
        require_hash(self.policy_hash, "forward_input.policy_hash")
        if not self.input_hashes:
            raise OptionAnalyticsError("forward_input_lineage_required")
        for value in self.input_hashes:
            require_hash(value, "forward_input.input_hash")
        if len(set(self.input_hashes)) != len(self.input_hashes):
            raise OptionAnalyticsError("forward_input_hash_duplicate")


@dataclass(frozen=True, slots=True)
class ForwardReceipt:
    method: ForwardMethod
    value: Decimal
    time_years: Decimal
    prepaid_spot: Decimal | None
    discounted_dividends: Decimal
    market_state_hash: str
    policy_hash: str
    input_hashes: tuple[str, ...]
    assumptions_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "value",
            _finite_decimal(self.value, "forward_receipt.value", positive=True),
        )
        require_hash(self.market_state_hash, "forward_receipt.market_state_hash")
        require_hash(self.policy_hash, "forward_receipt.policy_hash")
        require_hash(self.assumptions_hash, "forward_receipt.assumptions_hash")
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "option_forward_receipt",
                {
                    "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
                    "method": self.method.value,
                    "value": decimal_text(self.value),
                    "time_years": decimal_text(self.time_years),
                    "prepaid_spot": (
                        None
                        if self.prepaid_spot is None
                        else decimal_text(self.prepaid_spot)
                    ),
                    "discounted_dividends": decimal_text(self.discounted_dividends),
                    "market_state_hash": self.market_state_hash,
                    "policy_hash": self.policy_hash,
                    "input_hashes": list(self.input_hashes),
                    "assumptions_hash": self.assumptions_hash,
                },
            ),
        )


def estimate_forward(
    inputs: ForwardInput,
    *,
    method: ForwardMethod,
) -> ForwardReceipt:
    time_years = year_fraction(
        inputs.valuation_at,
        inputs.expiry_at,
        inputs.day_count,
    )
    discounted_dividends = _ZERO
    prepaid_spot: Decimal | None = None
    if method is ForwardMethod.FUTURES_PRICE:
        if inputs.futures_price is None:
            raise OptionAnalyticsError("futures_forward_price_required")
        value = inputs.futures_price
    else:
        if inputs.spot_price is None:
            raise OptionAnalyticsError("spot_forward_price_required")
        valuation = parse_timestamp(inputs.valuation_at, "forward_input.valuation_at")
        for dividend in inputs.discrete_dividends:
            days = Decimal(
                str(
                    (
                        parse_timestamp(dividend.ex_at, "discrete_dividend.ex_at")
                        - valuation
                    ).total_seconds()
                )
            ) / Decimal("86400")
            years = days / _day_count_denominator(inputs.day_count)
            discounted_dividends += dividend.amount * Decimal(
                str(math.exp(-float(inputs.risk_free_rate * years)))
            )
        prepaid_spot = inputs.spot_price - discounted_dividends
        if prepaid_spot <= 0:
            raise OptionAnalyticsError("forward_prepaid_spot_non_positive")
        carry = inputs.risk_free_rate - inputs.dividend_yield + inputs.borrow_rate
        value = prepaid_spot * Decimal(str(math.exp(float(carry * time_years))))
    assumptions_hash = _hash(
        "option_forward_assumptions",
        {
            "method": method.value,
            "valuation_at": inputs.valuation_at,
            "expiry_at": inputs.expiry_at,
            "risk_free_rate": decimal_text(inputs.risk_free_rate),
            "dividend_yield": decimal_text(inputs.dividend_yield),
            "borrow_rate": decimal_text(inputs.borrow_rate),
            "day_count": inputs.day_count.value,
            "settlement_convention": inputs.settlement_convention,
            "market_state_hash": inputs.market_state_hash,
            "policy_hash": inputs.policy_hash,
            "dividends": [
                {
                    "ex_at": item.ex_at,
                    "amount": decimal_text(item.amount),
                    "source_hash": item.source_hash,
                }
                for item in inputs.discrete_dividends
            ],
        },
    )
    return ForwardReceipt(
        method=method,
        value=value,
        time_years=time_years,
        prepaid_spot=prepaid_spot,
        discounted_dividends=discounted_dividends,
        market_state_hash=inputs.market_state_hash,
        policy_hash=inputs.policy_hash,
        input_hashes=inputs.input_hashes,
        assumptions_hash=assumptions_hash,
    )


@dataclass(frozen=True, slots=True)
class SurfaceObservation:
    """One raw-preserving IV point used by the calibration authority."""

    contract_id: str
    option_type: OptionType
    expiry_at: str
    time_years: Decimal
    strike: Decimal
    spot: Decimal
    forward: Decimal
    discount_factor: Decimal
    raw_implied_volatility: Decimal
    bid_implied_volatility: Decimal | None
    ask_implied_volatility: Decimal | None
    delta: Decimal
    liquidity_weight: Decimal
    normalized_quote_hash: str
    own_analytics_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.contract_id, "surface_observation.contract_id")
        parse_timestamp(self.expiry_at, "surface_observation.expiry_at")
        for field_name in (
            "time_years",
            "strike",
            "spot",
            "forward",
            "discount_factor",
            "raw_implied_volatility",
        ):
            object.__setattr__(
                self,
                field_name,
                _finite_decimal(
                    getattr(self, field_name),
                    f"surface_observation.{field_name}",
                    positive=True,
                ),
            )
        if self.discount_factor > 1:
            raise OptionAnalyticsError("surface_discount_factor_exceeds_one")
        for field_name in ("bid_implied_volatility", "ask_implied_volatility"):
            value = _optional_decimal(
                getattr(self, field_name),
                f"surface_observation.{field_name}",
                non_negative=True,
            )
            object.__setattr__(self, field_name, value)
        if (
            self.bid_implied_volatility is not None
            and self.ask_implied_volatility is not None
            and self.bid_implied_volatility > self.ask_implied_volatility
        ):
            raise OptionAnalyticsError("surface_bid_iv_exceeds_ask_iv")
        delta = _finite_decimal(self.delta, "surface_observation.delta")
        if not -1 <= delta <= 1:
            raise OptionAnalyticsError("surface_delta_out_of_range")
        object.__setattr__(self, "delta", delta)
        weight = _finite_decimal(
            self.liquidity_weight,
            "surface_observation.liquidity_weight",
            positive=True,
        )
        if weight > 1:
            raise OptionAnalyticsError("surface_liquidity_weight_exceeds_one")
        object.__setattr__(self, "liquidity_weight", weight)
        require_hash(
            self.normalized_quote_hash,
            "surface_observation.normalized_quote_hash",
        )
        require_hash(
            self.own_analytics_hash,
            "surface_observation.own_analytics_hash",
        )
        object.__setattr__(
            self,
            "content_hash",
            _hash("raw_option_surface_observation", self.identity_payload()),
        )

    @property
    def spot_moneyness(self) -> Decimal:
        return self.strike / self.spot

    @property
    def forward_moneyness(self) -> Decimal:
        return self.strike / self.forward

    @property
    def log_forward_moneyness(self) -> Decimal:
        return Decimal(str(math.log(float(self.forward_moneyness))))

    @property
    def total_variance(self) -> Decimal:
        return self.raw_implied_volatility**2 * self.time_years

    def coordinate(self, coordinate: SurfaceCoordinate) -> Decimal:
        return {
            SurfaceCoordinate.STRIKE: self.strike,
            SurfaceCoordinate.SPOT_MONEYNESS: self.spot_moneyness,
            SurfaceCoordinate.FORWARD_MONEYNESS: self.forward_moneyness,
            SurfaceCoordinate.LOG_FORWARD_MONEYNESS: self.log_forward_moneyness,
            SurfaceCoordinate.DELTA: self.delta,
            SurfaceCoordinate.TOTAL_VARIANCE: self.total_variance,
        }[coordinate]

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
            "contract_id": self.contract_id,
            "option_type": self.option_type.value,
            "expiry_at": self.expiry_at,
            "time_years": decimal_text(self.time_years),
            "strike": decimal_text(self.strike),
            "spot": decimal_text(self.spot),
            "forward": decimal_text(self.forward),
            "discount_factor": decimal_text(self.discount_factor),
            "raw_implied_volatility": decimal_text(self.raw_implied_volatility),
            "bid_implied_volatility": (
                None
                if self.bid_implied_volatility is None
                else decimal_text(self.bid_implied_volatility)
            ),
            "ask_implied_volatility": (
                None
                if self.ask_implied_volatility is None
                else decimal_text(self.ask_implied_volatility)
            ),
            "delta": decimal_text(self.delta),
            "liquidity_weight": decimal_text(self.liquidity_weight),
            "normalized_quote_hash": self.normalized_quote_hash,
            "own_analytics_hash": self.own_analytics_hash,
            "coordinates": {
                item.value: decimal_text(self.coordinate(item))
                for item in SurfaceCoordinate
            },
        }


@dataclass(frozen=True, slots=True)
class RejectedSurfacePoint:
    """Excluded surface input retained with deterministic rejection evidence."""

    contract_id: str
    source_quote_hash: str
    quality_record_hash: str
    rejection_reasons: tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.contract_id, "rejected_surface_point.contract_id")
        require_hash(
            self.source_quote_hash,
            "rejected_surface_point.source_quote_hash",
        )
        require_hash(
            self.quality_record_hash,
            "rejected_surface_point.quality_record_hash",
        )
        reasons = tuple(sorted(set(self.rejection_reasons)))
        if not reasons:
            raise OptionAnalyticsError("rejected_surface_point_reasons_required")
        for reason in reasons:
            require_stable_id(reason, "rejected_surface_point.reason")
        object.__setattr__(self, "rejection_reasons", reasons)
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "rejected_option_surface_point",
                {
                    "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
                    "contract_id": self.contract_id,
                    "source_quote_hash": self.source_quote_hash,
                    "quality_record_hash": self.quality_record_hash,
                    "rejection_reasons": list(reasons),
                },
            ),
        )


@dataclass(frozen=True, slots=True)
class SurfaceDiagnostic:
    kind: SurfaceDiagnosticKind
    passed: bool
    affected_contract_ids: tuple[str, ...]
    maximum_violation: Decimal
    phase: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.phase, "surface_diagnostic.phase")
        ids = tuple(sorted(self.affected_contract_ids))
        if len(ids) != len(set(ids)):
            raise OptionAnalyticsError("surface_diagnostic_contract_duplicate")
        object.__setattr__(self, "affected_contract_ids", ids)
        violation = _finite_decimal(
            self.maximum_violation,
            "surface_diagnostic.maximum_violation",
            non_negative=True,
        )
        if self.passed != (not ids and violation == 0):
            raise OptionAnalyticsError("surface_diagnostic_status_mismatch")
        object.__setattr__(self, "maximum_violation", violation)
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "option_surface_diagnostic",
                {
                    "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
                    "kind": self.kind.value,
                    "passed": self.passed,
                    "affected_contract_ids": list(ids),
                    "maximum_violation": decimal_text(violation),
                    "phase": self.phase,
                },
            ),
        )


@dataclass(frozen=True, slots=True)
class CalibratedSurfaceNode:
    contract_id: str
    expiry_at: str
    time_years: Decimal
    strike: Decimal
    forward: Decimal
    discount_factor: Decimal
    raw_implied_volatility: Decimal
    calibrated_implied_volatility: Decimal
    raw_call_equivalent_price: Decimal
    calibrated_call_equivalent_price: Decimal
    liquidity_weight: Decimal
    observation_hash: str
    repair_reasons: tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "time_years",
            "strike",
            "forward",
            "discount_factor",
            "raw_implied_volatility",
            "calibrated_implied_volatility",
            "raw_call_equivalent_price",
            "calibrated_call_equivalent_price",
            "liquidity_weight",
        ):
            object.__setattr__(
                self,
                field_name,
                _finite_decimal(
                    getattr(self, field_name),
                    f"calibrated_surface_node.{field_name}",
                    non_negative=True,
                ),
            )
        require_hash(
            self.observation_hash,
            "calibrated_surface_node.observation_hash",
        )
        reasons = tuple(sorted(set(self.repair_reasons)))
        object.__setattr__(self, "repair_reasons", reasons)
        object.__setattr__(
            self,
            "content_hash",
            _hash("calibrated_option_surface_node", self.identity_payload()),
        )

    @property
    def raw_total_variance(self) -> Decimal:
        return self.raw_implied_volatility**2 * self.time_years

    @property
    def calibrated_total_variance(self) -> Decimal:
        return self.calibrated_implied_volatility**2 * self.time_years

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
            "contract_id": self.contract_id,
            "expiry_at": self.expiry_at,
            "time_years": decimal_text(self.time_years),
            "strike": decimal_text(self.strike),
            "forward": decimal_text(self.forward),
            "discount_factor": decimal_text(self.discount_factor),
            "raw_implied_volatility": decimal_text(self.raw_implied_volatility),
            "calibrated_implied_volatility": decimal_text(
                self.calibrated_implied_volatility
            ),
            "raw_call_equivalent_price": decimal_text(self.raw_call_equivalent_price),
            "calibrated_call_equivalent_price": decimal_text(
                self.calibrated_call_equivalent_price
            ),
            "liquidity_weight": decimal_text(self.liquidity_weight),
            "observation_hash": self.observation_hash,
            "repair_reasons": list(self.repair_reasons),
        }


def _black_forward_price(
    option_type: OptionType,
    *,
    forward: Decimal,
    strike: Decimal,
    discount_factor: Decimal,
    time_years: Decimal,
    volatility: Decimal,
) -> Decimal:
    if volatility <= 0 or time_years <= 0:
        intrinsic = max(
            _ZERO,
            forward - strike if option_type is OptionType.CALL else strike - forward,
        )
        return discount_factor * intrinsic
    root = math.sqrt(float(time_years))
    sigma = float(volatility)
    d1 = (
        math.log(float(forward / strike)) + 0.5 * sigma * sigma * float(time_years)
    ) / (sigma * root)
    d2 = d1 - sigma * root
    if option_type is OptionType.CALL:
        value = float(discount_factor) * (
            float(forward) * _normal_cdf(d1) - float(strike) * _normal_cdf(d2)
        )
    else:
        value = float(discount_factor) * (
            float(strike) * _normal_cdf(-d2) - float(forward) * _normal_cdf(-d1)
        )
    return Decimal(str(value))


def _call_equivalent(observation: SurfaceObservation) -> Decimal:
    price = _black_forward_price(
        observation.option_type,
        forward=observation.forward,
        strike=observation.strike,
        discount_factor=observation.discount_factor,
        time_years=observation.time_years,
        volatility=observation.raw_implied_volatility,
    )
    if observation.option_type is OptionType.CALL:
        return price
    return price + observation.discount_factor * (
        observation.forward - observation.strike
    )


def _implied_black_call_volatility(
    *,
    call_price: Decimal,
    forward: Decimal,
    strike: Decimal,
    discount_factor: Decimal,
    time_years: Decimal,
) -> Decimal:
    intrinsic = discount_factor * max(_ZERO, forward - strike)
    upper = discount_factor * forward
    tolerance = Decimal("0.0000000001")
    if call_price < intrinsic - tolerance or call_price > upper + tolerance:
        raise OptionAnalyticsError("surface_repaired_price_outside_bounds")
    if call_price <= intrinsic + tolerance:
        return Decimal("0.000001")
    low = Decimal("0.000001")
    high = Decimal("5")
    for _ in range(160):
        middle = (low + high) / _TWO
        priced = _black_forward_price(
            OptionType.CALL,
            forward=forward,
            strike=strike,
            discount_factor=discount_factor,
            time_years=time_years,
            volatility=middle,
        )
        if abs(priced - call_price) <= tolerance:
            return middle
        if priced < call_price:
            low = middle
        else:
            high = middle
    result = (low + high) / _TWO
    if abs(
        _black_forward_price(
            OptionType.CALL,
            forward=forward,
            strike=strike,
            discount_factor=discount_factor,
            time_years=time_years,
            volatility=result,
        )
        - call_price
    ) > Decimal("0.000001"):
        raise OptionAnalyticsError("surface_repaired_iv_not_converged")
    return result


def _surface_diagnostics(
    nodes: Sequence[CalibratedSurfaceNode],
    *,
    raw: bool,
    phase: str,
    tolerance: Decimal,
) -> tuple[SurfaceDiagnostic, ...]:
    by_expiry: dict[str, list[CalibratedSurfaceNode]] = {}
    for node in nodes:
        by_expiry.setdefault(node.expiry_at, []).append(node)
    affected: dict[SurfaceDiagnosticKind, set[str]] = {
        item: set() for item in SurfaceDiagnosticKind
    }
    maxima: dict[SurfaceDiagnosticKind, Decimal] = {
        item: _ZERO for item in SurfaceDiagnosticKind
    }

    def price(node: CalibratedSurfaceNode) -> Decimal:
        return (
            node.raw_call_equivalent_price
            if raw
            else node.calibrated_call_equivalent_price
        )

    for rows in by_expiry.values():
        ordered = sorted(rows, key=lambda item: item.strike)
        for node in ordered:
            value = price(node)
            lower = node.discount_factor * max(_ZERO, node.forward - node.strike)
            upper = node.discount_factor * node.forward
            violation = max(_ZERO, lower - value, value - upper)
            if violation > tolerance:
                affected[SurfaceDiagnosticKind.VERTICAL_BOUND].add(node.contract_id)
                maxima[SurfaceDiagnosticKind.VERTICAL_BOUND] = max(
                    maxima[SurfaceDiagnosticKind.VERTICAL_BOUND], violation
                )
        for left, right in zip(ordered, ordered[1:]):
            violation = price(right) - price(left)
            if violation > tolerance:
                affected[SurfaceDiagnosticKind.STRIKE_MONOTONICITY].update(
                    (left.contract_id, right.contract_id)
                )
                maxima[SurfaceDiagnosticKind.STRIKE_MONOTONICITY] = max(
                    maxima[SurfaceDiagnosticKind.STRIKE_MONOTONICITY],
                    violation,
                )
        for left, middle, right in zip(
            ordered,
            ordered[1:],
            ordered[2:],
        ):
            weight = (middle.strike - left.strike) / (right.strike - left.strike)
            chord = price(left) + weight * (price(right) - price(left))
            violation = price(middle) - chord
            if violation > tolerance:
                affected[SurfaceDiagnosticKind.BUTTERFLY_CONVEXITY].update(
                    (
                        left.contract_id,
                        middle.contract_id,
                        right.contract_id,
                    )
                )
                maxima[SurfaceDiagnosticKind.BUTTERFLY_CONVEXITY] = max(
                    maxima[SurfaceDiagnosticKind.BUTTERFLY_CONVEXITY],
                    violation,
                )
    by_strike: dict[Decimal, list[CalibratedSurfaceNode]] = {}
    for node in nodes:
        by_strike.setdefault(node.strike, []).append(node)
    for rows in by_strike.values():
        ordered = sorted(rows, key=lambda item: item.time_years)
        for near, far in zip(ordered, ordered[1:]):
            near_variance = (
                near.raw_total_variance if raw else near.calibrated_total_variance
            )
            far_variance = (
                far.raw_total_variance if raw else far.calibrated_total_variance
            )
            violation = near_variance - far_variance
            if violation > tolerance:
                affected[SurfaceDiagnosticKind.CALENDAR_TOTAL_VARIANCE].update(
                    (near.contract_id, far.contract_id)
                )
                maxima[SurfaceDiagnosticKind.CALENDAR_TOTAL_VARIANCE] = max(
                    maxima[SurfaceDiagnosticKind.CALENDAR_TOTAL_VARIANCE],
                    violation,
                )
    return tuple(
        SurfaceDiagnostic(
            kind=kind,
            passed=not affected[kind],
            affected_contract_ids=tuple(affected[kind]),
            maximum_violation=maxima[kind],
            phase=phase,
        )
        for kind in SurfaceDiagnosticKind
    )


@dataclass(frozen=True, slots=True)
class SurfaceCalibrationPolicy:
    policy_id: str
    policy_version: str
    coordinate: SurfaceCoordinate
    extrapolation: SurfaceExtrapolation
    arbitrage_tolerance: Decimal = Decimal("0.00000001")
    maximum_iterations: int = 32
    maximum_repair_price_residual: Decimal = Decimal("0.25")
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.policy_id, "surface_policy.policy_id")
        require_stable_id(self.policy_version, "surface_policy.policy_version")
        object.__setattr__(
            self,
            "arbitrage_tolerance",
            _finite_decimal(
                self.arbitrage_tolerance,
                "surface_policy.arbitrage_tolerance",
                positive=True,
            ),
        )
        object.__setattr__(
            self,
            "maximum_repair_price_residual",
            _finite_decimal(
                self.maximum_repair_price_residual,
                "surface_policy.maximum_repair_price_residual",
                non_negative=True,
            ),
        )
        if (
            isinstance(self.maximum_iterations, bool)
            or self.maximum_iterations < 1
            or self.maximum_iterations > 512
        ):
            raise OptionAnalyticsError("surface_policy_iterations_invalid")
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "option_surface_calibration_policy",
                {
                    "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
                    "policy_id": self.policy_id,
                    "policy_version": self.policy_version,
                    "coordinate": self.coordinate.value,
                    "extrapolation": self.extrapolation.value,
                    "arbitrage_tolerance": decimal_text(self.arbitrage_tolerance),
                    "maximum_iterations": self.maximum_iterations,
                    "maximum_repair_price_residual": decimal_text(
                        self.maximum_repair_price_residual
                    ),
                },
            ),
        )


@dataclass(frozen=True, slots=True)
class CalibratedVolatilitySurface:
    surface_id: str
    calibrated_at: str
    underlying_id: str
    raw_observations: tuple[SurfaceObservation, ...]
    rejected_points: tuple[RejectedSurfacePoint, ...]
    nodes: tuple[CalibratedSurfaceNode, ...]
    pre_repair_diagnostics: tuple[SurfaceDiagnostic, ...]
    post_repair_diagnostics: tuple[SurfaceDiagnostic, ...]
    policy: SurfaceCalibrationPolicy
    weighted_rmse: Decimal
    maximum_price_residual: Decimal
    repair_count: int
    stability_hash: str
    calibrator_version: str = "deterministic_static_arbitrage_projection_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.surface_id, "calibrated_surface.surface_id")
        parse_timestamp(self.calibrated_at, "calibrated_surface.calibrated_at")
        require_stable_id(
            self.underlying_id,
            "calibrated_surface.underlying_id",
        )
        require_stable_id(
            self.calibrator_version,
            "calibrated_surface.calibrator_version",
        )
        if not self.raw_observations or not self.nodes:
            raise OptionAnalyticsError("calibrated_surface_points_required")
        if len(self.raw_observations) != len(self.nodes):
            raise OptionAnalyticsError("calibrated_surface_point_count_mismatch")
        if {item.contract_id for item in self.raw_observations} != {
            item.contract_id for item in self.nodes
        }:
            raise OptionAnalyticsError("calibrated_surface_contract_coverage_mismatch")
        rejected = tuple(
            sorted(self.rejected_points, key=lambda item: item.contract_id)
        )
        if len({item.contract_id for item in rejected}) != len(rejected):
            raise OptionAnalyticsError("calibrated_surface_rejected_contract_duplicate")
        if {item.contract_id for item in self.raw_observations} & {
            item.contract_id for item in rejected
        }:
            raise OptionAnalyticsError("calibrated_surface_included_rejected_overlap")
        object.__setattr__(self, "rejected_points", rejected)
        if any(not item.passed for item in self.post_repair_diagnostics):
            raise OptionAnalyticsError("calibrated_surface_arbitrage_remaining")
        for field_name in ("weighted_rmse", "maximum_price_residual"):
            object.__setattr__(
                self,
                field_name,
                _finite_decimal(
                    getattr(self, field_name),
                    f"calibrated_surface.{field_name}",
                    non_negative=True,
                ),
            )
        if self.maximum_price_residual > (self.policy.maximum_repair_price_residual):
            raise OptionAnalyticsError("calibrated_surface_repair_residual_exceeded")
        require_hash(self.stability_hash, "calibrated_surface.stability_hash")
        object.__setattr__(
            self,
            "content_hash",
            _hash("calibrated_option_volatility_surface", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
            "surface_id": self.surface_id,
            "calibrated_at": self.calibrated_at,
            "underlying_id": self.underlying_id,
            "calibrator_version": self.calibrator_version,
            "raw_observation_hashes": [
                item.content_hash for item in self.raw_observations
            ],
            "rejected_point_hashes": [
                item.content_hash for item in self.rejected_points
            ],
            "node_hashes": [item.content_hash for item in self.nodes],
            "pre_repair_diagnostic_hashes": [
                item.content_hash for item in self.pre_repair_diagnostics
            ],
            "post_repair_diagnostic_hashes": [
                item.content_hash for item in self.post_repair_diagnostics
            ],
            "policy_hash": self.policy.content_hash,
            "weighted_rmse": decimal_text(self.weighted_rmse),
            "maximum_price_residual": decimal_text(self.maximum_price_residual),
            "repair_count": self.repair_count,
            "stability_hash": self.stability_hash,
        }

    def implied_volatility(
        self,
        *,
        expiry_at: str,
        strike: Decimal,
    ) -> Decimal:
        target_expiry = parse_timestamp(expiry_at, "surface_query.expiry_at")
        target_strike = _finite_decimal(
            strike,
            "surface_query.strike",
            positive=True,
        )
        expiries = sorted({item.expiry_at for item in self.nodes})
        expiry_times = [
            parse_timestamp(item, "surface_query.expiry_grid") for item in expiries
        ]

        def strike_iv(expiry: str) -> Decimal:
            rows = sorted(
                (item for item in self.nodes if item.expiry_at == expiry),
                key=lambda item: item.strike,
            )
            exact = next(
                (
                    item.calibrated_implied_volatility
                    for item in rows
                    if item.strike == target_strike
                ),
                None,
            )
            if exact is not None:
                return exact
            if target_strike < rows[0].strike:
                if self.policy.extrapolation is SurfaceExtrapolation.REJECT:
                    raise OptionAnalyticsError("surface_strike_extrapolation_forbidden")
                return rows[0].calibrated_implied_volatility
            if target_strike > rows[-1].strike:
                if self.policy.extrapolation is SurfaceExtrapolation.REJECT:
                    raise OptionAnalyticsError("surface_strike_extrapolation_forbidden")
                return rows[-1].calibrated_implied_volatility
            for left, right in zip(rows, rows[1:]):
                if left.strike < target_strike < right.strike:
                    weight = (target_strike - left.strike) / (
                        right.strike - left.strike
                    )
                    return left.calibrated_implied_volatility + weight * (
                        right.calibrated_implied_volatility
                        - left.calibrated_implied_volatility
                    )
            raise OptionAnalyticsError("surface_strike_bracket_missing")

        if expiry_at in expiries:
            return strike_iv(expiry_at)
        if target_expiry < expiry_times[0] or target_expiry > expiry_times[-1]:
            if self.policy.extrapolation is SurfaceExtrapolation.REJECT:
                raise OptionAnalyticsError("surface_term_extrapolation_forbidden")
            return (
                strike_iv(expiries[0])
                if target_expiry < expiry_times[0]
                else strike_iv(expiries[-1])
            )
        for index in range(len(expiries) - 1):
            if expiry_times[index] < target_expiry < expiry_times[index + 1]:
                near_expiry = expiries[index]
                far_expiry = expiries[index + 1]
                near_iv = strike_iv(near_expiry)
                far_iv = strike_iv(far_expiry)
                near_time = next(
                    item.time_years
                    for item in self.nodes
                    if item.expiry_at == near_expiry
                )
                far_time = next(
                    item.time_years
                    for item in self.nodes
                    if item.expiry_at == far_expiry
                )
                total_span = Decimal(
                    str((expiry_times[index + 1] - expiry_times[index]).total_seconds())
                )
                partial = Decimal(
                    str((target_expiry - expiry_times[index]).total_seconds())
                )
                weight = partial / total_span
                target_time = near_time + weight * (far_time - near_time)
                total_variance = near_iv**2 * near_time + weight * (
                    far_iv**2 * far_time - near_iv**2 * near_time
                )
                return Decimal(str(math.sqrt(float(total_variance / target_time))))
        raise OptionAnalyticsError("surface_term_bracket_missing")

    def implied_volatility_for_coordinate(
        self,
        *,
        expiry_at: str,
        coordinate_value: Decimal,
        coordinate: SurfaceCoordinate | None = None,
    ) -> Decimal:
        """Interpolate on the policy coordinate without mutating raw points."""

        selected_coordinate = coordinate or self.policy.coordinate
        if selected_coordinate is not self.policy.coordinate:
            raise OptionAnalyticsError("surface_query_coordinate_policy_mismatch")
        target = _finite_decimal(
            coordinate_value,
            "surface_query.coordinate_value",
        )
        observations = {item.content_hash: item for item in self.raw_observations}
        expiries = sorted({item.expiry_at for item in self.nodes})
        target_expiry = parse_timestamp(
            expiry_at,
            "surface_coordinate_query.expiry_at",
        )
        expiry_times = [
            parse_timestamp(item, "surface_coordinate_query.expiry_grid")
            for item in expiries
        ]

        def curve_value(expiry: str) -> Decimal:
            points = sorted(
                (
                    (
                        observations[node.observation_hash].coordinate(
                            selected_coordinate
                        ),
                        node.calibrated_implied_volatility,
                    )
                    for node in self.nodes
                    if node.expiry_at == expiry
                ),
                key=lambda item: item[0],
            )
            if len({value for value, _iv in points}) != len(points):
                raise OptionAnalyticsError("surface_coordinate_grid_duplicate")
            for value, implied in points:
                if value == target:
                    return implied
            if target < points[0][0] or target > points[-1][0]:
                if self.policy.extrapolation is SurfaceExtrapolation.REJECT:
                    raise OptionAnalyticsError(
                        "surface_coordinate_extrapolation_forbidden"
                    )
                return points[0][1] if target < points[0][0] else points[-1][1]
            for (left_value, left_iv), (right_value, right_iv) in zip(
                points,
                points[1:],
            ):
                if left_value < target < right_value:
                    weight = (target - left_value) / (right_value - left_value)
                    return left_iv + weight * (right_iv - left_iv)
            raise OptionAnalyticsError("surface_coordinate_bracket_missing")

        if expiry_at in expiries:
            return curve_value(expiry_at)
        if target_expiry < expiry_times[0] or target_expiry > expiry_times[-1]:
            if self.policy.extrapolation is SurfaceExtrapolation.REJECT:
                raise OptionAnalyticsError(
                    "surface_coordinate_term_extrapolation_forbidden"
                )
            return (
                curve_value(expiries[0])
                if target_expiry < expiry_times[0]
                else curve_value(expiries[-1])
            )
        for index in range(len(expiries) - 1):
            if expiry_times[index] < target_expiry < expiry_times[index + 1]:
                near_iv = curve_value(expiries[index])
                far_iv = curve_value(expiries[index + 1])
                near_time = next(
                    item.time_years
                    for item in self.nodes
                    if item.expiry_at == expiries[index]
                )
                far_time = next(
                    item.time_years
                    for item in self.nodes
                    if item.expiry_at == expiries[index + 1]
                )
                span = Decimal(
                    str((expiry_times[index + 1] - expiry_times[index]).total_seconds())
                )
                elapsed = Decimal(
                    str((target_expiry - expiry_times[index]).total_seconds())
                )
                weight = elapsed / span
                target_time = near_time + weight * (far_time - near_time)
                total_variance = near_iv**2 * near_time + weight * (
                    far_iv**2 * far_time - near_iv**2 * near_time
                )
                return Decimal(str(math.sqrt(float(total_variance / target_time))))
        raise OptionAnalyticsError("surface_coordinate_term_bracket_missing")


def _project_surface_nodes(
    observations: Sequence[SurfaceObservation],
    policy: SurfaceCalibrationPolicy,
) -> tuple[CalibratedSurfaceNode, ...]:
    nodes = [
        CalibratedSurfaceNode(
            contract_id=item.contract_id,
            expiry_at=item.expiry_at,
            time_years=item.time_years,
            strike=item.strike,
            forward=item.forward,
            discount_factor=item.discount_factor,
            raw_implied_volatility=item.raw_implied_volatility,
            calibrated_implied_volatility=item.raw_implied_volatility,
            raw_call_equivalent_price=_call_equivalent(item),
            calibrated_call_equivalent_price=_call_equivalent(item),
            liquidity_weight=item.liquidity_weight,
            observation_hash=item.content_hash,
            repair_reasons=(),
        )
        for item in observations
    ]
    for _ in range(policy.maximum_iterations):
        before = tuple(
            (
                item.contract_id,
                item.calibrated_call_equivalent_price,
                item.calibrated_implied_volatility,
            )
            for item in nodes
        )
        by_expiry: dict[str, list[int]] = {}
        for index, node in enumerate(nodes):
            by_expiry.setdefault(node.expiry_at, []).append(index)
        for indexes in by_expiry.values():
            ordered = sorted(indexes, key=lambda index: nodes[index].strike)
            for index in ordered:
                node = nodes[index]
                lower = node.discount_factor * max(_ZERO, node.forward - node.strike)
                upper = node.discount_factor * node.forward
                clamped = min(
                    upper,
                    max(lower, node.calibrated_call_equivalent_price),
                )
                if clamped != node.calibrated_call_equivalent_price:
                    nodes[index] = replace(
                        node,
                        calibrated_call_equivalent_price=clamped,
                        repair_reasons=(
                            *node.repair_reasons,
                            SurfaceDiagnosticKind.VERTICAL_BOUND.value,
                        ),
                    )
            for left_index, right_index in zip(ordered, ordered[1:]):
                left = nodes[left_index]
                right = nodes[right_index]
                if (
                    right.calibrated_call_equivalent_price
                    > left.calibrated_call_equivalent_price
                ):
                    nodes[right_index] = replace(
                        right,
                        calibrated_call_equivalent_price=(
                            left.calibrated_call_equivalent_price
                        ),
                        repair_reasons=(
                            *right.repair_reasons,
                            SurfaceDiagnosticKind.STRIKE_MONOTONICITY.value,
                        ),
                    )
            for left_index, middle_index, right_index in zip(
                ordered,
                ordered[1:],
                ordered[2:],
            ):
                left = nodes[left_index]
                middle = nodes[middle_index]
                right = nodes[right_index]
                weight = (middle.strike - left.strike) / (right.strike - left.strike)
                chord = left.calibrated_call_equivalent_price + weight * (
                    right.calibrated_call_equivalent_price
                    - left.calibrated_call_equivalent_price
                )
                if middle.calibrated_call_equivalent_price > chord:
                    nodes[middle_index] = replace(
                        middle,
                        calibrated_call_equivalent_price=chord,
                        repair_reasons=(
                            *middle.repair_reasons,
                            SurfaceDiagnosticKind.BUTTERFLY_CONVEXITY.value,
                        ),
                    )
        for index, node in enumerate(nodes):
            calibrated_iv = _implied_black_call_volatility(
                call_price=node.calibrated_call_equivalent_price,
                forward=node.forward,
                strike=node.strike,
                discount_factor=node.discount_factor,
                time_years=node.time_years,
            )
            nodes[index] = replace(
                node,
                calibrated_implied_volatility=calibrated_iv,
            )
        by_strike: dict[Decimal, list[int]] = {}
        for index, node in enumerate(nodes):
            by_strike.setdefault(node.strike, []).append(index)
        for indexes in by_strike.values():
            ordered = sorted(indexes, key=lambda index: nodes[index].time_years)
            prior_variance: Decimal | None = None
            for index in ordered:
                node = nodes[index]
                variance = node.calibrated_total_variance
                if prior_variance is not None and variance < prior_variance:
                    repaired_iv = Decimal(
                        str(math.sqrt(float(prior_variance / node.time_years)))
                    )
                    repaired_price = _black_forward_price(
                        OptionType.CALL,
                        forward=node.forward,
                        strike=node.strike,
                        discount_factor=node.discount_factor,
                        time_years=node.time_years,
                        volatility=repaired_iv,
                    )
                    nodes[index] = replace(
                        node,
                        calibrated_implied_volatility=repaired_iv,
                        calibrated_call_equivalent_price=repaired_price,
                        repair_reasons=(
                            *node.repair_reasons,
                            SurfaceDiagnosticKind.CALENDAR_TOTAL_VARIANCE.value,
                        ),
                    )
                    variance = prior_variance
                prior_variance = variance
        after = tuple(
            (
                item.contract_id,
                item.calibrated_call_equivalent_price,
                item.calibrated_implied_volatility,
            )
            for item in nodes
        )
        if after == before:
            break
    return tuple(
        sorted(
            (
                replace(item, repair_reasons=tuple(sorted(set(item.repair_reasons))))
                for item in nodes
            ),
            key=lambda item: (item.expiry_at, item.strike, item.contract_id),
        )
    )


def calibrate_volatility_surface(
    *,
    surface_id: str,
    calibrated_at: str,
    underlying_id: str,
    observations: Sequence[SurfaceObservation],
    rejected_points: Sequence[RejectedSurfacePoint] = (),
    policy: SurfaceCalibrationPolicy,
    calibrator_version: str = "deterministic_static_arbitrage_projection_v1",
) -> CalibratedVolatilitySurface:
    require_stable_id(
        calibrator_version,
        "surface_calibration.calibrator_version",
    )
    ordered = tuple(
        sorted(
            observations,
            key=lambda item: (item.expiry_at, item.strike, item.contract_id),
        )
    )
    if len(ordered) < 3:
        raise OptionAnalyticsError("surface_calibration_points_insufficient")
    keys = [(item.expiry_at, item.strike) for item in ordered]
    if len(keys) != len(set(keys)):
        raise OptionAnalyticsError("surface_calibration_grid_duplicate")
    nodes = _project_surface_nodes(ordered, policy)
    repeated_nodes = _project_surface_nodes(ordered, policy)
    node_payload = [item.identity_payload() for item in nodes]
    repeated_payload = [item.identity_payload() for item in repeated_nodes]
    if node_payload != repeated_payload:
        raise OptionAnalyticsError("surface_calibration_not_deterministic")
    tolerance = policy.arbitrage_tolerance
    pre = _surface_diagnostics(
        nodes,
        raw=True,
        phase="PRE_REPAIR",
        tolerance=tolerance,
    )
    post = _surface_diagnostics(
        nodes,
        raw=False,
        phase="POST_REPAIR",
        tolerance=tolerance,
    )
    residuals = [
        abs(item.calibrated_call_equivalent_price - item.raw_call_equivalent_price)
        for item in nodes
    ]
    weights = [item.liquidity_weight for item in nodes]
    weighted_square = sum(
        (weight * residual * residual for weight, residual in zip(weights, residuals)),
        _ZERO,
    )
    total_weight = sum(weights, _ZERO)
    weighted_rmse = Decimal(str(math.sqrt(float(weighted_square / total_weight))))
    stability_hash = _hash(
        "option_surface_stability",
        {
            "raw_observation_hashes": [item.content_hash for item in ordered],
            "rejected_point_hashes": [
                item.content_hash
                for item in sorted(
                    rejected_points,
                    key=lambda item: item.contract_id,
                )
            ],
            "node_payload": node_payload,
            "policy_hash": policy.content_hash,
            "calibrator_version": calibrator_version,
        },
    )
    return CalibratedVolatilitySurface(
        surface_id=surface_id,
        calibrated_at=calibrated_at,
        underlying_id=underlying_id,
        raw_observations=ordered,
        rejected_points=tuple(rejected_points),
        nodes=nodes,
        pre_repair_diagnostics=pre,
        post_repair_diagnostics=post,
        policy=policy,
        weighted_rmse=weighted_rmse,
        maximum_price_residual=max(residuals),
        repair_count=sum(bool(item.repair_reasons) for item in nodes),
        stability_hash=stability_hash,
        calibrator_version=calibrator_version,
    )


@dataclass(frozen=True, slots=True)
class OptionModelInput:
    """Canonical primitive input shared by every registered pricing model."""

    input_id: str
    contract_id: str
    option_type: OptionType
    exercise_style: ExerciseStyle
    strike: Decimal
    time_years: Decimal
    spot: Decimal
    forward: Decimal
    volatility: Decimal
    risk_free_rate: Decimal
    dividend_yield: Decimal
    borrow_rate: Decimal
    payoff_kind: str
    underlying_kind: str
    valuation_at: str
    expiry_at: str
    day_count: DayCountConvention
    discrete_dividends: tuple[DiscreteDividend, ...] = ()
    monitoring_steps: int = 0
    source_hashes: tuple[str, ...] = ()
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.day_count, DayCountConvention):
            raise OptionAnalyticsError("option_model_input_day_count_invalid")
        require_stable_id(self.input_id, "option_model_input.input_id")
        require_stable_id(self.contract_id, "option_model_input.contract_id")
        for field_name in (
            "strike",
            "time_years",
            "spot",
            "forward",
            "volatility",
        ):
            object.__setattr__(
                self,
                field_name,
                _finite_decimal(
                    getattr(self, field_name),
                    f"option_model_input.{field_name}",
                    positive=True,
                ),
            )
        model_time = year_fraction(
            self.valuation_at,
            self.expiry_at,
            self.day_count,
        )
        if abs(model_time - self.time_years) > Decimal("0.000000000001"):
            raise OptionAnalyticsError("option_model_input_time_years_inconsistent")
        for field_name in (
            "risk_free_rate",
            "dividend_yield",
            "borrow_rate",
        ):
            object.__setattr__(
                self,
                field_name,
                _finite_decimal(
                    getattr(self, field_name),
                    f"option_model_input.{field_name}",
                ),
            )
        if self.payoff_kind not in {"VANILLA", "ASIAN_ARITHMETIC"}:
            raise OptionAnalyticsError("option_model_input_payoff_kind_unknown")
        if self.underlying_kind not in {"SPOT", "FUTURE"}:
            raise OptionAnalyticsError("option_model_input_underlying_kind_unknown")
        if self.payoff_kind == "ASIAN_ARITHMETIC":
            if self.monitoring_steps < 2:
                raise OptionAnalyticsError("asian_option_monitoring_steps_insufficient")
        elif self.monitoring_steps:
            raise OptionAnalyticsError("vanilla_option_monitoring_steps_forbidden")
        dividends = tuple(sorted(self.discrete_dividends, key=lambda item: item.ex_at))
        valuation = parse_timestamp(
            self.valuation_at,
            "option_model_input.valuation_at",
        )
        expiry = parse_timestamp(
            self.expiry_at,
            "option_model_input.expiry_at",
        )
        if any(
            not valuation
            < parse_timestamp(item.ex_at, "option_model_input.dividend_ex_at")
            <= expiry
            for item in dividends
        ):
            raise OptionAnalyticsError("option_model_input_dividend_outside_horizon")
        object.__setattr__(self, "discrete_dividends", dividends)
        if not self.source_hashes:
            raise OptionAnalyticsError("option_model_input_source_hashes_required")
        for value in self.source_hashes:
            require_hash(value, "option_model_input.source_hash")
        if len(set(self.source_hashes)) != len(self.source_hashes):
            raise OptionAnalyticsError("option_model_input_source_hash_duplicate")
        object.__setattr__(
            self,
            "content_hash",
            _hash("option_model_input", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
            "input_id": self.input_id,
            "contract_id": self.contract_id,
            "option_type": self.option_type.value,
            "exercise_style": self.exercise_style.value,
            "strike": decimal_text(self.strike),
            "time_years": decimal_text(self.time_years),
            "spot": decimal_text(self.spot),
            "forward": decimal_text(self.forward),
            "volatility": decimal_text(self.volatility),
            "risk_free_rate": decimal_text(self.risk_free_rate),
            "dividend_yield": decimal_text(self.dividend_yield),
            "borrow_rate": decimal_text(self.borrow_rate),
            "payoff_kind": self.payoff_kind,
            "underlying_kind": self.underlying_kind,
            "valuation_at": self.valuation_at,
            "expiry_at": self.expiry_at,
            "day_count": self.day_count.value,
            "discrete_dividends": [
                {
                    "ex_at": item.ex_at,
                    "amount": decimal_text(item.amount),
                    "source_hash": item.source_hash,
                }
                for item in self.discrete_dividends
            ],
            "monitoring_steps": self.monitoring_steps,
            "source_hashes": list(self.source_hashes),
        }


@dataclass(frozen=True, slots=True)
class AuthoritativeGreeks:
    delta: Decimal
    gamma: Decimal
    vega_per_vol_point: Decimal
    theta_per_calendar_day: Decimal
    rho_per_rate_point: Decimal
    vanna: Decimal
    volga: Decimal
    charm: Decimal

    def __post_init__(self) -> None:
        for field_name in (
            "delta",
            "gamma",
            "vega_per_vol_point",
            "theta_per_calendar_day",
            "rho_per_rate_point",
            "vanna",
            "volga",
            "charm",
        ):
            object.__setattr__(
                self,
                field_name,
                _finite_decimal(
                    getattr(self, field_name),
                    f"authoritative_greeks.{field_name}",
                ),
            )
        if not -1 <= self.delta <= 1:
            raise OptionAnalyticsError("authoritative_delta_out_of_range")

    def as_dict(self) -> dict[str, str]:
        return {
            field_name: decimal_text(getattr(self, field_name))
            for field_name in (
                "delta",
                "gamma",
                "vega_per_vol_point",
                "theta_per_calendar_day",
                "rho_per_rate_point",
                "vanna",
                "volga",
                "charm",
            )
        }


@dataclass(frozen=True, slots=True)
class OptionModelResult:
    model_kind: OptionModelKind
    model_version: str
    model_hash: str
    input_hash: str
    price: Decimal
    greeks: AuthoritativeGreeks
    numerical_method: str
    numerical_tolerance: Decimal
    assumptions: tuple[str, ...]
    iterations: int
    converged: bool
    convergence_error: Decimal
    exercise_boundary: tuple[tuple[int, Decimal], ...] = ()
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.model_version, "option_model_result.model_version")
        require_hash(self.model_hash, "option_model_result.model_hash")
        require_hash(self.input_hash, "option_model_result.input_hash")
        require_stable_id(
            self.numerical_method,
            "option_model_result.numerical_method",
        )
        object.__setattr__(
            self,
            "price",
            _finite_decimal(
                self.price,
                "option_model_result.price",
                non_negative=True,
            ),
        )
        error = _finite_decimal(
            self.convergence_error,
            "option_model_result.convergence_error",
            non_negative=True,
        )
        object.__setattr__(self, "convergence_error", error)
        tolerance = _finite_decimal(
            self.numerical_tolerance,
            "option_model_result.numerical_tolerance",
            positive=True,
        )
        object.__setattr__(self, "numerical_tolerance", tolerance)
        assumptions = tuple(sorted(set(self.assumptions)))
        if not assumptions:
            raise OptionAnalyticsError("option_model_result_assumptions_required")
        for assumption in assumptions:
            require_stable_id(
                assumption,
                "option_model_result.assumption",
            )
        object.__setattr__(self, "assumptions", assumptions)
        if not self.converged:
            raise OptionAnalyticsError("option_model_result_not_converged")
        if error > tolerance:
            raise OptionAnalyticsError(
                "option_model_result_convergence_tolerance_exceeded"
            )
        if isinstance(self.iterations, bool) or self.iterations < 1:
            raise OptionAnalyticsError("option_model_result_iterations_invalid")
        boundary = tuple(self.exercise_boundary)
        if boundary != tuple(sorted(boundary, key=lambda item: item[0])):
            raise OptionAnalyticsError("option_exercise_boundary_not_canonical")
        if any(step < 0 or price <= 0 for step, price in boundary):
            raise OptionAnalyticsError("option_exercise_boundary_invalid")
        object.__setattr__(self, "exercise_boundary", boundary)
        object.__setattr__(
            self,
            "content_hash",
            _hash("authoritative_option_model_result", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
            "model_kind": self.model_kind.value,
            "model_version": self.model_version,
            "model_hash": self.model_hash,
            "input_hash": self.input_hash,
            "price": decimal_text(self.price),
            "greeks": self.greeks.as_dict(),
            "numerical_method": self.numerical_method,
            "numerical_tolerance": decimal_text(self.numerical_tolerance),
            "assumptions": list(self.assumptions),
            "iterations": self.iterations,
            "converged": self.converged,
            "convergence_error": decimal_text(self.convergence_error),
            "exercise_boundary": [
                {"step": step, "underlying_price": decimal_text(price)}
                for step, price in self.exercise_boundary
            ],
        }


class RegisteredOptionModel(Protocol):
    @property
    def kind(self) -> OptionModelKind: ...

    @property
    def model_version(self) -> str: ...

    @property
    def content_hash(self) -> str: ...

    def supports(self, inputs: OptionModelInput) -> bool: ...

    def price(self, inputs: OptionModelInput) -> Decimal: ...

    def evaluate(self, inputs: OptionModelInput) -> OptionModelResult: ...


def _present_value_of_discrete_dividends(
    inputs: OptionModelInput,
) -> Decimal:
    if not inputs.discrete_dividends:
        return _ZERO
    return sum(
        (
            item.amount
            * Decimal(
                str(
                    math.exp(
                        -float(
                            inputs.risk_free_rate
                            * year_fraction(
                                inputs.valuation_at,
                                item.ex_at,
                                inputs.day_count,
                            )
                        )
                    )
                )
            )
            for item in inputs.discrete_dividends
        ),
        _ZERO,
    )


def _black_scholes_spot_price(inputs: OptionModelInput) -> Decimal:
    adjusted_spot = inputs.spot - _present_value_of_discrete_dividends(inputs)
    if adjusted_spot <= 0:
        raise OptionAnalyticsError("option_model_dividend_adjusted_spot_non_positive")
    time = float(inputs.time_years)
    sigma = float(inputs.volatility)
    root = math.sqrt(time)
    carry = inputs.risk_free_rate - inputs.dividend_yield + inputs.borrow_rate
    d1 = (
        math.log(float(adjusted_spot / inputs.strike))
        + (float(carry) + 0.5 * sigma * sigma) * time
    ) / (sigma * root)
    d2 = d1 - sigma * root
    discounted_spot = float(adjusted_spot) * math.exp(
        -float(inputs.dividend_yield - inputs.borrow_rate) * time
    )
    discounted_strike = float(inputs.strike) * math.exp(
        -float(inputs.risk_free_rate) * time
    )
    if inputs.option_type is OptionType.CALL:
        value = discounted_spot * _normal_cdf(d1) - discounted_strike * _normal_cdf(d2)
    else:
        value = discounted_strike * _normal_cdf(-d2) - discounted_spot * (
            _normal_cdf(-d1)
        )
    return max(_ZERO, Decimal(str(value)))


def _finite_difference_greeks(
    model: RegisteredOptionModel,
    inputs: OptionModelInput,
) -> AuthoritativeGreeks:
    spot_bump = max(Decimal("0.0001"), inputs.spot * Decimal("0.0001"))
    vol_bump = Decimal("0.0001")
    rate_bump = Decimal("0.0001")
    day_bump = min(
        Decimal("1") / _CALENDAR_DAYS,
        inputs.time_years / Decimal("10"),
    )

    def valuation_for_time(time_years: Decimal) -> str:
        if time_years == inputs.time_years:
            return inputs.valuation_at
        expiry = parse_timestamp(
            inputs.expiry_at,
            "option_greek.expiry_at",
        )
        seconds = (
            time_years * _day_count_denominator(inputs.day_count) * Decimal("86400")
        )
        return (expiry - timedelta(seconds=float(seconds))).isoformat()

    def priced(
        *,
        spot: Decimal = inputs.spot,
        volatility: Decimal = inputs.volatility,
        risk_free_rate: Decimal = inputs.risk_free_rate,
        time_years: Decimal = inputs.time_years,
    ) -> Decimal:
        return model.price(
            replace(
                inputs,
                spot=spot,
                volatility=volatility,
                risk_free_rate=risk_free_rate,
                time_years=time_years,
                valuation_at=valuation_for_time(time_years),
            )
        )

    base = model.price(inputs)
    up_spot = priced(spot=inputs.spot + spot_bump)
    down_spot_value = inputs.spot - spot_bump
    if down_spot_value <= 0:
        raise OptionAnalyticsError("option_greek_spot_bump_domain_failure")
    down_spot = priced(spot=down_spot_value)
    delta = (up_spot - down_spot) / (_TWO * spot_bump)
    gamma = (up_spot - _TWO * base + down_spot) / (spot_bump**2)
    up_vol = priced(volatility=inputs.volatility + vol_bump)
    down_vol_value = inputs.volatility - vol_bump
    if down_vol_value <= 0:
        raise OptionAnalyticsError("option_greek_volatility_bump_domain_failure")
    down_vol = priced(volatility=down_vol_value)
    vega = (up_vol - down_vol) / (_TWO * vol_bump) * _ONE_PERCENT
    volga = (up_vol - _TWO * base + down_vol) / (vol_bump**2) * (_ONE_PERCENT**2)
    up_rate = priced(risk_free_rate=inputs.risk_free_rate + rate_bump)
    down_rate = priced(risk_free_rate=inputs.risk_free_rate - rate_bump)
    rho = (up_rate - down_rate) / (_TWO * rate_bump) * _ONE_PERCENT
    shorter = priced(time_years=inputs.time_years - day_bump)
    theta = shorter - base
    up_spot_up_vol = model.price(
        replace(
            inputs,
            spot=inputs.spot + spot_bump,
            volatility=inputs.volatility + vol_bump,
        )
    )
    up_spot_down_vol = model.price(
        replace(
            inputs,
            spot=inputs.spot + spot_bump,
            volatility=down_vol_value,
        )
    )
    down_spot_up_vol = model.price(
        replace(
            inputs,
            spot=down_spot_value,
            volatility=inputs.volatility + vol_bump,
        )
    )
    down_spot_down_vol = model.price(
        replace(
            inputs,
            spot=down_spot_value,
            volatility=down_vol_value,
        )
    )
    vanna = (
        (up_spot_up_vol - up_spot_down_vol - down_spot_up_vol + down_spot_down_vol)
        / (_TWO * spot_bump * _TWO * vol_bump)
        * _ONE_PERCENT
    )
    shorter_up = model.price(
        replace(
            inputs,
            time_years=inputs.time_years - day_bump,
            valuation_at=valuation_for_time(inputs.time_years - day_bump),
            spot=inputs.spot + spot_bump,
        )
    )
    shorter_down = model.price(
        replace(
            inputs,
            time_years=inputs.time_years - day_bump,
            valuation_at=valuation_for_time(inputs.time_years - day_bump),
            spot=down_spot_value,
        )
    )
    shorter_delta = (shorter_up - shorter_down) / (_TWO * spot_bump)
    charm = shorter_delta - delta
    return AuthoritativeGreeks(
        delta=delta,
        gamma=gamma,
        vega_per_vol_point=vega,
        theta_per_calendar_day=theta,
        rho_per_rate_point=rho,
        vanna=vanna,
        volga=volga,
        charm=charm,
    )


@dataclass(frozen=True, slots=True)
class EuropeanBlackScholesModel:
    kind: OptionModelKind = OptionModelKind.EUROPEAN_BLACK_SCHOLES
    model_version: str = "european_black_scholes_authority_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "registered_option_model",
                {
                    "kind": self.kind.value,
                    "model_version": self.model_version,
                    "method": "closed_form_with_discrete_dividend_pv",
                },
            ),
        )

    def supports(self, inputs: OptionModelInput) -> bool:
        return (
            inputs.exercise_style is ExerciseStyle.EUROPEAN
            and inputs.payoff_kind == "VANILLA"
            and inputs.underlying_kind == "SPOT"
        )

    def price(self, inputs: OptionModelInput) -> Decimal:
        if not self.supports(inputs):
            raise OptionAnalyticsError("black_scholes_model_domain_unsupported")
        return _black_scholes_spot_price(inputs)

    def evaluate(self, inputs: OptionModelInput) -> OptionModelResult:
        price = self.price(inputs)
        greeks = _finite_difference_greeks(self, inputs)
        return OptionModelResult(
            model_kind=self.kind,
            model_version=self.model_version,
            model_hash=self.content_hash,
            input_hash=inputs.content_hash,
            price=price,
            greeks=greeks,
            numerical_method="closed_form_and_central_finite_differences",
            numerical_tolerance=Decimal("0.000000000001"),
            assumptions=(
                "continuous_dividend_yield",
                "discrete_dividend_present_value",
                "lognormal_spot_diffusion",
            ),
            iterations=1,
            converged=True,
            convergence_error=_ZERO,
        )


@dataclass(frozen=True, slots=True)
class FuturesBlack76Model:
    kind: OptionModelKind = OptionModelKind.FUTURES_BLACK_76
    model_version: str = "futures_black_76_authority_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "registered_option_model",
                {
                    "kind": self.kind.value,
                    "model_version": self.model_version,
                    "method": "black_76_closed_form",
                },
            ),
        )

    def supports(self, inputs: OptionModelInput) -> bool:
        return (
            inputs.exercise_style is ExerciseStyle.EUROPEAN
            and inputs.payoff_kind == "VANILLA"
            and inputs.underlying_kind == "FUTURE"
            and not inputs.discrete_dividends
        )

    def price(self, inputs: OptionModelInput) -> Decimal:
        if not self.supports(inputs):
            raise OptionAnalyticsError("black_76_model_domain_unsupported")
        return _black_forward_price(
            inputs.option_type,
            forward=inputs.forward,
            strike=inputs.strike,
            discount_factor=Decimal(
                str(math.exp(-float(inputs.risk_free_rate * inputs.time_years)))
            ),
            time_years=inputs.time_years,
            volatility=inputs.volatility,
        )

    def evaluate(self, inputs: OptionModelInput) -> OptionModelResult:
        return OptionModelResult(
            model_kind=self.kind,
            model_version=self.model_version,
            model_hash=self.content_hash,
            input_hash=inputs.content_hash,
            price=self.price(inputs),
            greeks=_finite_difference_greeks(self, inputs),
            numerical_method="black_76_closed_form_and_finite_differences",
            numerical_tolerance=Decimal("0.000000000001"),
            assumptions=(
                "futures_underlying",
                "lognormal_forward_diffusion",
                "no_discrete_dividend",
            ),
            iterations=1,
            converged=True,
            convergence_error=_ZERO,
        )


def _american_binomial_value(
    inputs: OptionModelInput,
    *,
    steps: int,
) -> tuple[Decimal, tuple[tuple[int, Decimal], ...]]:
    if steps < 2:
        raise OptionAnalyticsError("american_binomial_steps_insufficient")
    adjusted_spot = inputs.spot - _present_value_of_discrete_dividends(inputs)
    if adjusted_spot <= 0:
        raise OptionAnalyticsError("american_dividend_adjusted_spot_non_positive")
    dt = float(inputs.time_years) / steps
    up = math.exp(float(inputs.volatility) * math.sqrt(dt))
    down = 1.0 / up
    growth = math.exp(
        float(inputs.risk_free_rate - inputs.dividend_yield + inputs.borrow_rate) * dt
    )
    probability = (growth - down) / (up - down)
    if not 0 < probability < 1:
        raise OptionAnalyticsError("american_binomial_probability_invalid")
    discount = math.exp(-float(inputs.risk_free_rate) * dt)

    def payoff(spot: float) -> float:
        return (
            max(spot - float(inputs.strike), 0.0)
            if inputs.option_type is OptionType.CALL
            else max(float(inputs.strike) - spot, 0.0)
        )

    values = [
        payoff(float(adjusted_spot) * up ** (steps - index) * down**index)
        for index in range(steps + 1)
    ]
    boundaries: list[tuple[int, Decimal]] = []
    for step in range(steps - 1, -1, -1):
        next_values: list[float] = []
        exercised_spots: list[float] = []
        for index in range(step + 1):
            spot = float(adjusted_spot) * up ** (step - index) * down**index
            continuation = discount * (
                probability * values[index] + (1.0 - probability) * values[index + 1]
            )
            exercise = payoff(spot)
            if exercise > continuation and exercise > 0:
                exercised_spots.append(spot)
            next_values.append(max(exercise, continuation))
        if exercised_spots:
            boundary = (
                min(exercised_spots)
                if inputs.option_type is OptionType.CALL
                else max(exercised_spots)
            )
            boundaries.append((step, Decimal(str(boundary))))
        values = next_values
    return Decimal(str(values[0])), tuple(reversed(boundaries))


@dataclass(frozen=True, slots=True)
class AmericanCrrBinomialModel:
    steps: int = 200
    convergence_tolerance: Decimal = Decimal("0.50")
    kind: OptionModelKind = OptionModelKind.AMERICAN_CRR_BINOMIAL
    model_version: str = "american_crr_binomial_authority_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if isinstance(self.steps, bool) or not 50 <= self.steps <= 4000:
            raise OptionAnalyticsError("american_binomial_steps_invalid")
        object.__setattr__(
            self,
            "convergence_tolerance",
            _finite_decimal(
                self.convergence_tolerance,
                "american_binomial.convergence_tolerance",
                positive=True,
            ),
        )
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "registered_option_model",
                {
                    "kind": self.kind.value,
                    "model_version": self.model_version,
                    "steps": self.steps,
                    "convergence_tolerance": decimal_text(self.convergence_tolerance),
                },
            ),
        )

    def supports(self, inputs: OptionModelInput) -> bool:
        return (
            inputs.exercise_style is ExerciseStyle.AMERICAN
            and inputs.payoff_kind == "VANILLA"
            and inputs.underlying_kind == "SPOT"
        )

    def price(self, inputs: OptionModelInput) -> Decimal:
        if not self.supports(inputs):
            raise OptionAnalyticsError("american_binomial_model_domain_unsupported")
        return _american_binomial_value(inputs, steps=self.steps)[0]

    def evaluate(self, inputs: OptionModelInput) -> OptionModelResult:
        price, boundary = _american_binomial_value(inputs, steps=self.steps)
        coarser = _american_binomial_value(
            inputs,
            steps=max(2, self.steps // 2),
        )[0]
        convergence_error = abs(price - coarser)
        if convergence_error > self.convergence_tolerance:
            raise OptionAnalyticsError("american_binomial_convergence_failed")
        return OptionModelResult(
            model_kind=self.kind,
            model_version=self.model_version,
            model_hash=self.content_hash,
            input_hash=inputs.content_hash,
            price=price,
            greeks=_finite_difference_greeks(self, inputs),
            numerical_method="cox_ross_rubinstein_early_exercise_tree",
            numerical_tolerance=self.convergence_tolerance,
            assumptions=(
                "american_early_exercise",
                "crr_recombining_lattice",
                "discrete_dividend_present_value",
            ),
            iterations=self.steps,
            converged=True,
            convergence_error=convergence_error,
            exercise_boundary=boundary,
        )


@dataclass(frozen=True, slots=True)
class AmericanRichardsonBinomialModel:
    coarse_steps: int = 120
    convergence_tolerance: Decimal = Decimal("0.50")
    kind: OptionModelKind = OptionModelKind.AMERICAN_RICHARDSON_BINOMIAL
    model_version: str = "american_richardson_binomial_authority_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if isinstance(self.coarse_steps, bool) or not 50 <= self.coarse_steps <= 2000:
            raise OptionAnalyticsError("american_richardson_steps_invalid")
        object.__setattr__(
            self,
            "convergence_tolerance",
            _finite_decimal(
                self.convergence_tolerance,
                "american_richardson.convergence_tolerance",
                positive=True,
            ),
        )
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "registered_option_model",
                {
                    "kind": self.kind.value,
                    "model_version": self.model_version,
                    "coarse_steps": self.coarse_steps,
                    "fine_steps": self.coarse_steps * 2,
                    "convergence_tolerance": decimal_text(self.convergence_tolerance),
                },
            ),
        )

    def supports(self, inputs: OptionModelInput) -> bool:
        return (
            inputs.exercise_style is ExerciseStyle.AMERICAN
            and inputs.payoff_kind == "VANILLA"
            and inputs.underlying_kind == "SPOT"
        )

    def price(self, inputs: OptionModelInput) -> Decimal:
        if not self.supports(inputs):
            raise OptionAnalyticsError("american_richardson_model_domain_unsupported")
        coarse = _american_binomial_value(
            inputs,
            steps=self.coarse_steps,
        )[0]
        fine = _american_binomial_value(
            inputs,
            steps=self.coarse_steps * 2,
        )[0]
        intrinsic = max(
            _ZERO,
            inputs.spot - inputs.strike
            if inputs.option_type is OptionType.CALL
            else inputs.strike - inputs.spot,
        )
        return max(intrinsic, _TWO * fine - coarse)

    def evaluate(self, inputs: OptionModelInput) -> OptionModelResult:
        coarse = _american_binomial_value(
            inputs,
            steps=self.coarse_steps,
        )[0]
        fine, boundary = _american_binomial_value(
            inputs,
            steps=self.coarse_steps * 2,
        )
        convergence_error = abs(fine - coarse)
        if convergence_error > self.convergence_tolerance:
            raise OptionAnalyticsError("american_richardson_convergence_failed")
        price = self.price(inputs)
        return OptionModelResult(
            model_kind=self.kind,
            model_version=self.model_version,
            model_hash=self.content_hash,
            input_hash=inputs.content_hash,
            price=price,
            greeks=_finite_difference_greeks(self, inputs),
            numerical_method="richardson_extrapolated_crr_tree",
            numerical_tolerance=self.convergence_tolerance,
            assumptions=(
                "american_early_exercise",
                "discrete_dividend_present_value",
                "richardson_extrapolated_lattice",
            ),
            iterations=self.coarse_steps * 2,
            converged=True,
            convergence_error=convergence_error,
            exercise_boundary=boundary,
        )


@dataclass(frozen=True, slots=True)
class AsianArithmeticMonteCarloModel:
    paths: int = 4096
    seed: int = 1729
    convergence_tolerance: Decimal = Decimal("0.5")
    kind: OptionModelKind = OptionModelKind.ASIAN_ARITHMETIC_MONTE_CARLO
    model_version: str = "asian_arithmetic_antithetic_mc_authority_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.paths, bool)
            or self.paths < 256
            or self.paths % 2
            or self.paths > 1_000_000
        ):
            raise OptionAnalyticsError("asian_model_paths_invalid")
        if isinstance(self.seed, bool) or self.seed < 0:
            raise OptionAnalyticsError("asian_model_seed_invalid")
        object.__setattr__(
            self,
            "convergence_tolerance",
            _finite_decimal(
                self.convergence_tolerance,
                "asian_model.convergence_tolerance",
                positive=True,
            ),
        )
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "registered_option_model",
                {
                    "kind": self.kind.value,
                    "model_version": self.model_version,
                    "paths": self.paths,
                    "seed": self.seed,
                    "convergence_tolerance": decimal_text(self.convergence_tolerance),
                    "variance_reduction": "antithetic_common_random_numbers",
                },
            ),
        )

    def supports(self, inputs: OptionModelInput) -> bool:
        return (
            inputs.exercise_style is ExerciseStyle.EUROPEAN
            and inputs.payoff_kind == "ASIAN_ARITHMETIC"
            and inputs.underlying_kind == "SPOT"
            and not inputs.discrete_dividends
        )

    def _price(self, inputs: OptionModelInput, paths: int) -> Decimal:
        if not self.supports(inputs):
            raise OptionAnalyticsError("asian_model_domain_unsupported")
        generator = random.Random(self.seed)
        steps = inputs.monitoring_steps
        dt = float(inputs.time_years) / steps
        drift = (
            float(inputs.risk_free_rate - inputs.dividend_yield + inputs.borrow_rate)
            - 0.5 * float(inputs.volatility) ** 2
        ) * dt
        diffusion = float(inputs.volatility) * math.sqrt(dt)
        total = 0.0
        pairs = paths // 2
        for _ in range(pairs):
            normals = [generator.gauss(0.0, 1.0) for _step in range(steps)]
            pair_payoff = 0.0
            for sign in (1.0, -1.0):
                spot = float(inputs.spot)
                running = spot
                for normal in normals:
                    spot *= math.exp(drift + diffusion * normal * sign)
                    running += spot
                average = running / (steps + 1)
                payoff = (
                    max(average - float(inputs.strike), 0.0)
                    if inputs.option_type is OptionType.CALL
                    else max(float(inputs.strike) - average, 0.0)
                )
                pair_payoff += payoff
            total += pair_payoff
        discounted = (
            math.exp(-float(inputs.risk_free_rate * inputs.time_years)) * total / paths
        )
        return Decimal(str(discounted))

    def price(self, inputs: OptionModelInput) -> Decimal:
        first = self._price(inputs, self.paths)
        repeated = self._price(inputs, self.paths)
        if first != repeated:
            raise OptionAnalyticsError("asian_model_not_deterministic")
        return first

    def evaluate(self, inputs: OptionModelInput) -> OptionModelResult:
        price = self.price(inputs)
        coarse = self._price(inputs, self.paths // 2)
        error = abs(price - coarse)
        if error > self.convergence_tolerance:
            raise OptionAnalyticsError("asian_model_convergence_failed")
        return OptionModelResult(
            model_kind=self.kind,
            model_version=self.model_version,
            model_hash=self.content_hash,
            input_hash=inputs.content_hash,
            price=price,
            greeks=_finite_difference_greeks(self, inputs),
            numerical_method="seeded_antithetic_arithmetic_asian_monte_carlo",
            numerical_tolerance=self.convergence_tolerance,
            assumptions=(
                "arithmetic_discrete_monitoring",
                "common_random_numbers",
                "seeded_antithetic_paths",
            ),
            iterations=self.paths,
            converged=True,
            convergence_error=error,
        )


@dataclass(frozen=True, slots=True)
class OptionModelRegistry:
    models: tuple[RegisteredOptionModel, ...]
    registry_version: str = "authoritative_option_model_registry_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(
            self.registry_version,
            "option_model_registry.registry_version",
        )
        models = tuple(self.models)
        if not models:
            raise OptionAnalyticsError("option_model_registry_empty")
        kinds = [item.kind for item in models]
        if len(kinds) != len(set(kinds)):
            raise OptionAnalyticsError("option_model_registry_kind_duplicate")
        object.__setattr__(
            self,
            "models",
            tuple(sorted(models, key=lambda item: item.kind.value)),
        )
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "option_model_registry",
                {
                    "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
                    "registry_version": self.registry_version,
                    "models": [
                        {
                            "kind": item.kind.value,
                            "model_version": item.model_version,
                            "content_hash": item.content_hash,
                        }
                        for item in self.models
                    ],
                },
            ),
        )

    def resolve(
        self,
        kind: OptionModelKind,
        inputs: OptionModelInput,
    ) -> RegisteredOptionModel:
        matches = [item for item in self.models if item.kind is kind]
        if len(matches) != 1:
            raise OptionAnalyticsError("option_model_registry_resolution_failed")
        model = matches[0]
        if not model.supports(inputs):
            raise OptionAnalyticsError("option_model_registry_domain_mismatch")
        return model

    def evaluate(
        self,
        kind: OptionModelKind,
        inputs: OptionModelInput,
    ) -> OptionModelResult:
        model = self.resolve(kind, inputs)
        first = model.evaluate(inputs)
        second = model.evaluate(inputs)
        if first != second:
            raise OptionAnalyticsError("option_model_registry_not_deterministic")
        if (
            first.model_kind is not kind
            or first.model_version != model.model_version
            or first.model_hash != model.content_hash
            or first.input_hash != inputs.content_hash
        ):
            raise OptionAnalyticsError("option_model_registry_result_binding_mismatch")
        return first


def default_option_model_registry() -> OptionModelRegistry:
    return OptionModelRegistry(
        (
            EuropeanBlackScholesModel(),
            FuturesBlack76Model(),
            AmericanCrrBinomialModel(),
            AmericanRichardsonBinomialModel(),
            AsianArithmeticMonteCarloModel(),
        )
    )


@dataclass(frozen=True, slots=True)
class SupplierAnalyticsObservation:
    provider_id: str
    contract_id: str
    observed_at: str
    source_hash: str
    implied_volatility: Decimal | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    vega_per_vol_point: Decimal | None = None
    theta_per_calendar_day: Decimal | None = None
    rho_per_rate_point: Decimal | None = None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(
            self.provider_id,
            "supplier_option_analytics.provider_id",
        )
        require_stable_id(
            self.contract_id,
            "supplier_option_analytics.contract_id",
        )
        parse_timestamp(
            self.observed_at,
            "supplier_option_analytics.observed_at",
        )
        require_hash(
            self.source_hash,
            "supplier_option_analytics.source_hash",
        )
        for field_name in (
            "implied_volatility",
            "delta",
            "gamma",
            "vega_per_vol_point",
            "theta_per_calendar_day",
            "rho_per_rate_point",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_decimal(
                    getattr(self, field_name),
                    f"supplier_option_analytics.{field_name}",
                ),
            )
        if all(
            getattr(self, field_name) is None
            for field_name in (
                "implied_volatility",
                "delta",
                "gamma",
                "vega_per_vol_point",
                "theta_per_calendar_day",
                "rho_per_rate_point",
            )
        ):
            raise OptionAnalyticsError("supplier_option_analytics_values_required")
        object.__setattr__(
            self,
            "content_hash",
            _hash("supplier_option_analytics_observation", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
            "provider_id": self.provider_id,
            "contract_id": self.contract_id,
            "observed_at": self.observed_at,
            "source_hash": self.source_hash,
            **{
                field_name: (
                    None
                    if getattr(self, field_name) is None
                    else decimal_text(getattr(self, field_name))
                )
                for field_name in (
                    "implied_volatility",
                    "delta",
                    "gamma",
                    "vega_per_vol_point",
                    "theta_per_calendar_day",
                    "rho_per_rate_point",
                )
            },
        }


@dataclass(frozen=True, slots=True)
class AnalyticsComparisonPolicy:
    policy_id: str
    policy_version: str
    action: AnalyticsComparisonAction
    implied_volatility_tolerance: Decimal
    delta_tolerance: Decimal
    gamma_tolerance: Decimal
    vega_tolerance: Decimal
    theta_tolerance: Decimal
    rho_tolerance: Decimal
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.policy_id, "analytics_comparison.policy_id")
        require_stable_id(
            self.policy_version,
            "analytics_comparison.policy_version",
        )
        for field_name in (
            "implied_volatility_tolerance",
            "delta_tolerance",
            "gamma_tolerance",
            "vega_tolerance",
            "theta_tolerance",
            "rho_tolerance",
        ):
            object.__setattr__(
                self,
                field_name,
                _finite_decimal(
                    getattr(self, field_name),
                    f"analytics_comparison.{field_name}",
                    non_negative=True,
                ),
            )
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "option_analytics_comparison_policy",
                {
                    "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
                    "policy_id": self.policy_id,
                    "policy_version": self.policy_version,
                    "action": self.action.value,
                    **{
                        field_name: decimal_text(getattr(self, field_name))
                        for field_name in (
                            "implied_volatility_tolerance",
                            "delta_tolerance",
                            "gamma_tolerance",
                            "vega_tolerance",
                            "theta_tolerance",
                            "rho_tolerance",
                        )
                    },
                },
            ),
        )


def default_analytics_comparison_policy(
    *,
    action: AnalyticsComparisonAction = AnalyticsComparisonAction.REJECT,
) -> AnalyticsComparisonPolicy:
    return AnalyticsComparisonPolicy(
        policy_id="supplier_analytics_comparison",
        policy_version="v1",
        action=action,
        implied_volatility_tolerance=Decimal("0.02"),
        delta_tolerance=Decimal("0.05"),
        gamma_tolerance=Decimal("0.02"),
        vega_tolerance=Decimal("0.05"),
        theta_tolerance=Decimal("0.05"),
        rho_tolerance=Decimal("0.05"),
    )


@dataclass(frozen=True, slots=True)
class AnalyticsComparison:
    status: AnalyticsComparisonStatus
    supplier_observation_hash: str | None
    policy_hash: str
    differences: tuple[tuple[str, Decimal], ...]
    breached_fields: tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_hash(self.policy_hash, "analytics_comparison.policy_hash")
        if self.supplier_observation_hash is not None:
            require_hash(
                self.supplier_observation_hash,
                "analytics_comparison.supplier_observation_hash",
            )
        differences = tuple(sorted(self.differences))
        if len({name for name, _value in differences}) != len(differences):
            raise OptionAnalyticsError("analytics_comparison_field_duplicate")
        for name, value in differences:
            require_stable_id(name, "analytics_comparison.field")
            if value < 0:
                raise OptionAnalyticsError("analytics_comparison_difference_negative")
        breached = tuple(sorted(set(self.breached_fields)))
        object.__setattr__(self, "differences", differences)
        object.__setattr__(self, "breached_fields", breached)
        if self.status is AnalyticsComparisonStatus.NOT_PROVIDED:
            if self.supplier_observation_hash is not None or differences or breached:
                raise OptionAnalyticsError(
                    "analytics_comparison_not_provided_has_values"
                )
        elif self.supplier_observation_hash is None:
            raise OptionAnalyticsError("analytics_comparison_supplier_hash_required")
        elif self.status is AnalyticsComparisonStatus.MATCHED and breached:
            raise OptionAnalyticsError("analytics_comparison_matched_with_breach")
        elif self.status is AnalyticsComparisonStatus.DEGRADED and not breached:
            raise OptionAnalyticsError("analytics_comparison_degraded_without_breach")
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "option_analytics_supplier_comparison",
                {
                    "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
                    "status": self.status.value,
                    "supplier_observation_hash": self.supplier_observation_hash,
                    "policy_hash": self.policy_hash,
                    "differences": [
                        {"field": name, "absolute_difference": decimal_text(value)}
                        for name, value in differences
                    ],
                    "breached_fields": list(breached),
                },
            ),
        )


def _compare_supplier_analytics(
    supplier: SupplierAnalyticsObservation | None,
    *,
    contract_id: str,
    observed_at: str,
    implied_volatility: Decimal,
    greeks: AuthoritativeGreeks,
    policy: AnalyticsComparisonPolicy,
) -> AnalyticsComparison:
    if supplier is None:
        return AnalyticsComparison(
            status=AnalyticsComparisonStatus.NOT_PROVIDED,
            supplier_observation_hash=None,
            policy_hash=policy.content_hash,
            differences=(),
            breached_fields=(),
        )
    if supplier.contract_id != contract_id:
        raise OptionAnalyticsError("supplier_analytics_contract_mismatch")
    if parse_timestamp(supplier.observed_at, "supplier_analytics.observed_at") > (
        parse_timestamp(observed_at, "own_analytics.observed_at")
    ):
        raise OptionAnalyticsError("supplier_analytics_from_future")
    mappings: tuple[tuple[str, Decimal | None, Decimal, Decimal], ...] = (
        (
            "implied_volatility",
            supplier.implied_volatility,
            implied_volatility,
            policy.implied_volatility_tolerance,
        ),
        ("delta", supplier.delta, greeks.delta, policy.delta_tolerance),
        ("gamma", supplier.gamma, greeks.gamma, policy.gamma_tolerance),
        (
            "vega_per_vol_point",
            supplier.vega_per_vol_point,
            greeks.vega_per_vol_point,
            policy.vega_tolerance,
        ),
        (
            "theta_per_calendar_day",
            supplier.theta_per_calendar_day,
            greeks.theta_per_calendar_day,
            policy.theta_tolerance,
        ),
        (
            "rho_per_rate_point",
            supplier.rho_per_rate_point,
            greeks.rho_per_rate_point,
            policy.rho_tolerance,
        ),
    )
    differences = tuple(
        (name, abs(supplier_value - own_value))
        for name, supplier_value, own_value, _tolerance in mappings
        if supplier_value is not None
    )
    breached = tuple(
        name
        for name, supplier_value, own_value, tolerance in mappings
        if supplier_value is not None and abs(supplier_value - own_value) > tolerance
    )
    if breached and policy.action is AnalyticsComparisonAction.REJECT:
        raise OptionAnalyticsError(
            "supplier_option_analytics_tolerance_exceeded:" + ",".join(sorted(breached))
        )
    return AnalyticsComparison(
        status=(
            AnalyticsComparisonStatus.DEGRADED
            if breached
            else AnalyticsComparisonStatus.MATCHED
        ),
        supplier_observation_hash=supplier.content_hash,
        policy_hash=policy.content_hash,
        differences=differences,
        breached_fields=breached,
    )


@dataclass(frozen=True, slots=True)
class AuthoritativeOptionAnalyticsReceipt:
    """Factory-only receipt binding raw quote, own model, and public mark."""

    receipt_id: str
    analytics_mark: OptionAnalyticsMark
    own_model_result: OptionModelResult
    implied_volatility: Decimal
    market_implied_volatility: Decimal
    volatility_source: AnalyticsVolatilitySource
    quote_hash: str
    valuation_input_hash: str
    registry_hash: str
    factory_hash: str
    surface_hash: str | None
    supplier_comparison: AnalyticsComparison
    cross_model_price_residual: Decimal
    _factory_token: InitVar[object | None] = None
    content_hash: str = field(init=False)

    def __post_init__(self, _factory_token: object | None) -> None:
        if _factory_token is not _RECEIPT_FACTORY_TOKEN:
            raise OptionAnalyticsError(
                "option_analytics_receipt_requires_authoritative_factory"
            )
        require_stable_id(self.receipt_id, "option_analytics_receipt.receipt_id")
        if not isinstance(self.analytics_mark, OptionAnalyticsMark):
            raise OptionAnalyticsError("option_analytics_receipt_mark_required")
        if not isinstance(self.own_model_result, OptionModelResult):
            raise OptionAnalyticsError("option_analytics_receipt_model_result_required")
        for value, field_name in (
            (self.quote_hash, "option_analytics_receipt.quote_hash"),
            (
                self.valuation_input_hash,
                "option_analytics_receipt.valuation_input_hash",
            ),
            (self.registry_hash, "option_analytics_receipt.registry_hash"),
            (self.factory_hash, "option_analytics_receipt.factory_hash"),
        ):
            require_hash(value, field_name)
        if self.surface_hash is not None:
            require_hash(
                self.surface_hash,
                "option_analytics_receipt.surface_hash",
            )
        if not isinstance(
            self.volatility_source,
            AnalyticsVolatilitySource,
        ):
            raise OptionAnalyticsError(
                "option_analytics_receipt_volatility_source_invalid"
            )
        implied = _finite_decimal(
            self.implied_volatility,
            "option_analytics_receipt.implied_volatility",
            positive=True,
        )
        market_implied = _finite_decimal(
            self.market_implied_volatility,
            "option_analytics_receipt.market_implied_volatility",
            positive=True,
        )
        residual = _finite_decimal(
            self.cross_model_price_residual,
            "option_analytics_receipt.cross_model_price_residual",
            non_negative=True,
        )
        object.__setattr__(self, "implied_volatility", implied)
        object.__setattr__(
            self,
            "market_implied_volatility",
            market_implied,
        )
        object.__setattr__(self, "cross_model_price_residual", residual)
        mark = self.analytics_mark
        if mark.valuation_input_hash != self.valuation_input_hash:
            raise OptionAnalyticsError("option_analytics_receipt_binding_mismatch")
        if mark.source_quote_hash != self.quote_hash:
            raise OptionAnalyticsError("option_analytics_receipt_quote_mismatch")
        if mark.implied_volatility != implied:
            raise OptionAnalyticsError("option_analytics_receipt_iv_mismatch")
        if (
            self.volatility_source is AnalyticsVolatilitySource.MARKET_QUOTE_INVERSION
            and implied != market_implied
        ):
            raise OptionAnalyticsError("option_analytics_receipt_market_iv_mismatch")
        if (
            self.volatility_source is AnalyticsVolatilitySource.CALIBRATED_SURFACE
            and self.surface_hash is None
        ):
            raise OptionAnalyticsError(
                "option_analytics_receipt_surface_source_unbound"
            )
        object.__setattr__(
            self,
            "content_hash",
            _hash("authoritative_option_analytics_receipt", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
            "receipt_id": self.receipt_id,
            "analytics_mark_hash": self.analytics_mark.content_hash,
            "own_model_result_hash": self.own_model_result.content_hash,
            "implied_volatility": decimal_text(self.implied_volatility),
            "market_implied_volatility": decimal_text(self.market_implied_volatility),
            "volatility_source": self.volatility_source.value,
            "quote_hash": self.quote_hash,
            "valuation_input_hash": self.valuation_input_hash,
            "registry_hash": self.registry_hash,
            "factory_hash": self.factory_hash,
            "surface_hash": self.surface_hash,
            "supplier_comparison_hash": self.supplier_comparison.content_hash,
            "cross_model_price_residual": decimal_text(self.cross_model_price_residual),
        }

    def require_valid(self) -> None:
        if self.content_hash != _hash(
            "authoritative_option_analytics_receipt",
            self.identity_payload(),
        ):
            raise OptionAnalyticsError("option_analytics_receipt_hash_mismatch")


@dataclass(frozen=True, slots=True)
class AuthoritativeOptionAnalyticsFactory:
    """Only supported creator of public ``OptionAnalyticsMark`` evidence."""

    registry: OptionModelRegistry
    comparison_policy: AnalyticsComparisonPolicy
    margin_model_hash: str
    pricing_adapter: BlackScholesPricingAdapter = field(
        default_factory=BlackScholesPricingAdapter
    )
    model_kind: OptionModelKind = OptionModelKind.EUROPEAN_BLACK_SCHOLES
    factory_version: str = "authoritative_option_analytics_factory_v1"
    maximum_cross_model_price_residual: Decimal = Decimal("0.000001")
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_hash(
            self.margin_model_hash,
            "authoritative_option_factory.margin_model_hash",
        )
        require_stable_id(
            self.factory_version,
            "authoritative_option_factory.factory_version",
        )
        object.__setattr__(
            self,
            "maximum_cross_model_price_residual",
            _finite_decimal(
                self.maximum_cross_model_price_residual,
                "authoritative_option_factory.maximum_cross_model_price_residual",
                non_negative=True,
            ),
        )
        if self.model_kind is not OptionModelKind.EUROPEAN_BLACK_SCHOLES:
            raise OptionAnalyticsError("public_option_factory_model_kind_unsupported")
        object.__setattr__(
            self,
            "content_hash",
            _hash(
                "authoritative_option_analytics_factory",
                {
                    "schema_version": OPTION_ANALYTICS_SCHEMA_VERSION,
                    "factory_version": self.factory_version,
                    "registry_hash": self.registry.content_hash,
                    "comparison_policy_hash": self.comparison_policy.content_hash,
                    "margin_model_hash": self.margin_model_hash,
                    "pricing_adapter_hash": self.pricing_adapter.content_hash,
                    "model_kind": self.model_kind.value,
                    "maximum_cross_model_price_residual": decimal_text(
                        self.maximum_cross_model_price_residual
                    ),
                },
            ),
        )

    @staticmethod
    def _validate_quote(
        quote: OptionContractQuote,
        valuation_input: ValuationInputSnapshot,
        *,
        permit_illiquid: bool,
    ) -> None:
        contract = valuation_input.contract
        source = valuation_input.quote
        expected_right = (
            MarketStateOptionRight.CALL
            if contract.option_type is OptionType.CALL
            else MarketStateOptionRight.PUT
        )
        if (
            quote.contract_id != contract.contract_id
            or quote.underlying_instrument_id != contract.underlying_id
            or quote.right is not expected_right
            or quote.strike != contract.strike
            or quote.currency != contract.currency
            or parse_timestamp(quote.expiry_at, "option_quote.expiry_at")
            != parse_timestamp(
                contract.expiration_at,
                "option_contract.expiration_at",
            )
            or quote.bid != source.bid
            or quote.ask != source.ask
            or quote.last != source.last
            or quote.bid_size != source.bid_size
            or quote.ask_size != source.ask_size
            or quote.volume != Decimal(source.volume)
            or quote.open_interest != Decimal(source.open_interest)
        ):
            raise OptionAnalyticsError(
                "authoritative_option_factory_quote_binding_mismatch"
            )
        if source.state is not QuoteState.NORMAL and not (
            permit_illiquid and source.state is QuoteState.ILLIQUID
        ):
            raise OptionAnalyticsError(
                "authoritative_option_factory_source_quote_unusable"
            )
        if quote.condition not in {
            QuoteCondition.NORMAL,
            QuoteCondition.INDICATIVE,
        }:
            raise OptionAnalyticsError(
                "authoritative_option_factory_quote_condition_unusable"
            )
        if (
            quote.metadata.source_hash != source.content_hash
            or parse_timestamp(
                quote.metadata.observed_at,
                "option_quote.metadata.observed_at",
            )
            != parse_timestamp(
                source.availability.event_at,
                "option_quote.availability.event_at",
            )
            or parse_timestamp(
                quote.metadata.knowledge_at,
                "option_quote.metadata.knowledge_at",
            )
            != source.availability.available_at
            or quote.metadata.max_age_seconds != source.stale_after_seconds
        ):
            raise OptionAnalyticsError(
                "authoritative_option_factory_quote_evidence_mismatch"
            )

    @staticmethod
    def _model_input(
        valuation_input: ValuationInputSnapshot,
        volatility: Decimal,
    ) -> OptionModelInput:
        contract = valuation_input.contract
        if contract.exercise_style is not ExerciseStyle.EUROPEAN:
            raise OptionAnalyticsError("public_black_scholes_factory_requires_european")
        return OptionModelInput(
            input_id=f"{valuation_input.valuation_input_id}.authority",
            contract_id=contract.contract_id,
            option_type=contract.option_type,
            exercise_style=contract.exercise_style,
            strike=contract.strike,
            time_years=valuation_input.time_to_expiry_years,
            spot=valuation_input.spot_price,
            forward=valuation_input.forward_price,
            volatility=volatility,
            risk_free_rate=valuation_input.risk_free_rate,
            dividend_yield=valuation_input.dividend_yield,
            borrow_rate=_ZERO,
            payoff_kind="VANILLA",
            underlying_kind="SPOT",
            valuation_at=valuation_input.valuation_at,
            expiry_at=contract.expiration_at,
            day_count=DayCountConvention.ACT_365_25,
            source_hashes=(
                valuation_input.content_hash,
                valuation_input.quote.content_hash,
            ),
        )

    def derive(
        self,
        *,
        receipt_id: str,
        quote: OptionContractQuote,
        valuation_input: ValuationInputSnapshot,
        margin_per_contract: Decimal,
        collateral_per_contract: Decimal,
        supplier_observation: SupplierAnalyticsObservation | None = None,
        surface: CalibratedVolatilitySurface | None = None,
        permit_illiquid: bool = False,
    ) -> AuthoritativeOptionAnalyticsReceipt:
        if not isinstance(permit_illiquid, bool):
            raise OptionAnalyticsError(
                "authoritative_option_factory_permit_illiquid_invalid"
            )
        self._validate_quote(
            quote,
            valuation_input,
            permit_illiquid=permit_illiquid,
        )
        if surface is not None:
            if (
                surface.underlying_id != valuation_input.contract.underlying_id
                or valuation_input.contract.contract_id
                not in {item.contract_id for item in surface.nodes}
                or parse_timestamp(
                    surface.calibrated_at,
                    "authoritative_option_factory.surface_calibrated_at",
                )
                > parse_timestamp(
                    quote.metadata.knowledge_at,
                    "authoritative_option_factory.quote_knowledge_at",
                )
            ):
                raise OptionAnalyticsError(
                    "authoritative_option_factory_surface_binding_mismatch"
                )
        market_price = quote.midpoint
        implied_result = self.pricing_adapter.model.implied_volatility(
            valuation_input,
            market_price,
            permit_illiquid=permit_illiquid,
        )
        if (
            implied_result.contract_id != valuation_input.contract.contract_id
            or implied_result.valuation_input_hash != valuation_input.content_hash
            or implied_result.model_version != self.pricing_adapter.model.model_version
        ):
            raise OptionAnalyticsError(
                "authoritative_option_factory_iv_result_binding_mismatch"
            )
        if not implied_result.success or implied_result.volatility is None:
            raise OptionAnalyticsError(
                f"authoritative_option_factory_iv_failed:{implied_result.failure.value}"
            )
        market_implied = implied_result.volatility
        if surface is None:
            implied = market_implied
            volatility_source = AnalyticsVolatilitySource.MARKET_QUOTE_INVERSION
        else:
            implied = surface.implied_volatility(
                expiry_at=valuation_input.contract.expiration_at,
                strike=valuation_input.contract.strike,
            )
            volatility_source = AnalyticsVolatilitySource.CALIBRATED_SURFACE
        state = self.pricing_adapter.bind_state(valuation_input, implied)
        public_price = self.pricing_adapter.value(valuation_input.contract, state)
        legacy_greeks = self.pricing_adapter.greeks(
            valuation_input.contract,
            state,
        )
        model_input = self._model_input(valuation_input, implied)
        own_result = self.registry.evaluate(self.model_kind, model_input)
        cross_residual = abs(own_result.price - public_price)
        if cross_residual > self.maximum_cross_model_price_residual:
            raise OptionAnalyticsError(
                "authoritative_option_factory_cross_model_mismatch"
            )
        own_greeks = AuthoritativeGreeks(
            delta=legacy_greeks.delta,
            gamma=legacy_greeks.gamma,
            vega_per_vol_point=legacy_greeks.vega_per_vol_point,
            theta_per_calendar_day=legacy_greeks.theta_per_calendar_day,
            rho_per_rate_point=legacy_greeks.rho_per_rate_point,
            vanna=own_result.greeks.vanna,
            volga=own_result.greeks.volga,
            charm=own_result.greeks.charm,
        )
        comparison = _compare_supplier_analytics(
            supplier_observation,
            contract_id=valuation_input.contract.contract_id,
            observed_at=quote.metadata.knowledge_at,
            implied_volatility=implied,
            greeks=own_greeks,
            policy=self.comparison_policy,
        )
        margin = _finite_decimal(
            margin_per_contract,
            "authoritative_option_factory.margin_per_contract",
            non_negative=True,
        )
        collateral = _finite_decimal(
            collateral_per_contract,
            "authoritative_option_factory.collateral_per_contract",
            non_negative=True,
        )
        mark_metadata = (
            replace(quote.metadata, quality=MarketDataQuality.INDICATIVE)
            if comparison.status is AnalyticsComparisonStatus.DEGRADED
            else quote.metadata
        )
        mark = OptionAnalyticsMark(
            contract_id=quote.contract_id,
            underlying_instrument_id=quote.underlying_instrument_id,
            expiry_at=quote.expiry_at,
            currency=quote.currency,
            price_unit=quote.price_unit,
            market_price=market_price,
            model_price=public_price,
            implied_volatility=implied,
            delta=own_greeks.delta,
            gamma=own_greeks.gamma,
            vega=own_greeks.vega_per_vol_point,
            theta=own_greeks.theta_per_calendar_day,
            rho=own_greeks.rho_per_rate_point,
            margin_per_contract=margin,
            collateral_per_contract=collateral,
            model_hash=self.pricing_adapter.model.content_hash,
            model_specification_hash=(self.pricing_adapter.specification.content_hash),
            margin_model_hash=self.margin_model_hash,
            valuation_input_hash=valuation_input.content_hash,
            source_quote_hash=quote.content_hash,
            metadata=mark_metadata,
        )
        receipt = AuthoritativeOptionAnalyticsReceipt(
            receipt_id=receipt_id,
            analytics_mark=mark,
            own_model_result=own_result,
            implied_volatility=implied,
            market_implied_volatility=market_implied,
            volatility_source=volatility_source,
            quote_hash=quote.content_hash,
            valuation_input_hash=valuation_input.content_hash,
            registry_hash=self.registry.content_hash,
            factory_hash=self.content_hash,
            surface_hash=None if surface is None else surface.content_hash,
            supplier_comparison=comparison,
            cross_model_price_residual=cross_residual,
            _factory_token=_RECEIPT_FACTORY_TOKEN,
        )
        receipt.require_valid()
        return receipt


def validate_option_model_conformance(
    registry: OptionModelRegistry,
    *,
    european_spot_input: OptionModelInput,
    european_future_input: OptionModelInput,
    american_input: OptionModelInput,
    asian_input: OptionModelInput,
    american_tolerance: Decimal = Decimal("0.20"),
) -> dict[str, str]:
    """Cross-validate all required model families without circular assertions."""

    tolerance = _finite_decimal(
        american_tolerance,
        "option_model_conformance.american_tolerance",
        positive=True,
    )
    bs = registry.evaluate(
        OptionModelKind.EUROPEAN_BLACK_SCHOLES,
        european_spot_input,
    )
    black = registry.evaluate(
        OptionModelKind.FUTURES_BLACK_76,
        european_future_input,
    )
    crr = registry.evaluate(
        OptionModelKind.AMERICAN_CRR_BINOMIAL,
        american_input,
    )
    richardson = registry.evaluate(
        OptionModelKind.AMERICAN_RICHARDSON_BINOMIAL,
        american_input,
    )
    asian = registry.evaluate(
        OptionModelKind.ASIAN_ARITHMETIC_MONTE_CARLO,
        asian_input,
    )
    european_benchmark_input = replace(
        american_input,
        input_id=f"{american_input.input_id}.european-benchmark",
        exercise_style=ExerciseStyle.EUROPEAN,
    )
    european_benchmark = registry.evaluate(
        OptionModelKind.EUROPEAN_BLACK_SCHOLES,
        european_benchmark_input,
    )
    if (
        crr.price + tolerance < european_benchmark.price
        or richardson.price + tolerance < european_benchmark.price
    ):
        raise OptionAnalyticsError("american_price_below_european_benchmark")
    intrinsic = max(
        _ZERO,
        (
            american_input.spot - american_input.strike
            if american_input.option_type is OptionType.CALL
            else american_input.strike - american_input.spot
        ),
    )
    upper_bound = (
        american_input.spot
        if american_input.option_type is OptionType.CALL
        else american_input.strike
    )
    if any(
        item.price + tolerance < intrinsic or item.price > upper_bound + tolerance
        for item in (crr, richardson)
    ):
        raise OptionAnalyticsError("american_put_call_bound_failed")
    if abs(crr.price - richardson.price) > tolerance:
        raise OptionAnalyticsError("american_model_cross_validation_failed")
    for result in (crr, richardson):
        if any(
            (
                boundary_price < american_input.strike
                if american_input.option_type is OptionType.CALL
                else boundary_price > american_input.strike
            )
            for _step, boundary_price in result.exercise_boundary
        ):
            raise OptionAnalyticsError("american_early_exercise_boundary_inconsistent")
    alternate_asian_input = replace(
        asian_input,
        input_id=f"{asian_input.input_id}.path-grid-variant",
        monitoring_steps=asian_input.monitoring_steps + 1,
    )
    alternate_asian = registry.evaluate(
        OptionModelKind.ASIAN_ARITHMETIC_MONTE_CARLO,
        alternate_asian_input,
    )
    if alternate_asian.input_hash == asian.input_hash:
        raise OptionAnalyticsError("asian_path_grid_not_hash_bound")
    if min(bs.price, black.price, crr.price, richardson.price, asian.price) < 0:
        raise OptionAnalyticsError("option_model_conformance_negative_price")
    return {
        "registry_hash": registry.content_hash,
        "black_scholes_result_hash": bs.content_hash,
        "black_76_result_hash": black.content_hash,
        "american_crr_result_hash": crr.content_hash,
        "american_richardson_result_hash": richardson.content_hash,
        "american_european_benchmark_hash": european_benchmark.content_hash,
        "asian_result_hash": asian.content_hash,
        "asian_path_grid_variant_hash": alternate_asian.content_hash,
    }


__all__ = (
    "AnalyticsComparison",
    "AnalyticsComparisonAction",
    "AnalyticsComparisonPolicy",
    "AnalyticsComparisonStatus",
    "AnalyticsVolatilitySource",
    "AmericanCrrBinomialModel",
    "AmericanRichardsonBinomialModel",
    "AsianArithmeticMonteCarloModel",
    "AuthoritativeGreeks",
    "AuthoritativeOptionAnalyticsFactory",
    "AuthoritativeOptionAnalyticsReceipt",
    "CalibratedSurfaceNode",
    "CalibratedVolatilitySurface",
    "DayCountConvention",
    "DeterministicProviderNormalizationAdapter",
    "DiscreteDividend",
    "EuropeanBlackScholesModel",
    "ForwardInput",
    "ForwardMethod",
    "ForwardReceipt",
    "FuturesBlack76Model",
    "MissingValueConvention",
    "NormalizedOptionQuote",
    "OPTION_ANALYTICS_SCHEMA_VERSION",
    "OptionAnalyticsError",
    "OptionModelInput",
    "OptionModelKind",
    "OptionModelRegistry",
    "OptionModelResult",
    "OptionQuoteQualityCandidate",
    "OptionQuoteQualityContext",
    "OptionQuoteQualityPolicy",
    "OptionQuoteQualityRecord",
    "ProviderNormalizationAdapter",
    "ProviderOptionQuoteRow",
    "ProviderPriceConvention",
    "ProviderQuoteConvention",
    "QualityScreenedOptionChain",
    "QuoteQualityAction",
    "QuoteQualityDisposition",
    "RejectedSurfacePoint",
    "SupplierAnalyticsObservation",
    "SurfaceCalibrationPolicy",
    "SurfaceCoordinate",
    "SurfaceDiagnostic",
    "SurfaceDiagnosticKind",
    "SurfaceExtrapolation",
    "SurfaceObservation",
    "TimestampConvention",
    "calibrate_volatility_surface",
    "default_analytics_comparison_policy",
    "default_option_quote_quality_policy",
    "default_option_model_registry",
    "estimate_forward",
    "screen_option_quote_quality",
    "standard_provider_adapters",
    "validate_option_model_conformance",
    "year_fraction",
)
