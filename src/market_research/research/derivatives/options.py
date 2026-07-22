"""First-class, offline-only option research contracts and simulation tools.

The module intentionally does not reuse the spot candle ledger.  Every option
series, quote, valuation input, model result, lifecycle event, and multi-leg
fill remains an immutable research object with explicit point-in-time evidence.
No class in this module can submit an order or access an account or network.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Mapping, Sequence

from market_research.research.hashing import sha256_prefixed

from .common import (
    DERIVATIVE_RESEARCH_SCHEMA_VERSION,
    AvailabilityTimes,
    DerivativeResearchError,
    QualityDecision,
    QualityResult,
    RunType,
    decimal_text,
    exact_decimal,
    parse_timestamp,
    require_confirmatory_quality,
    require_hash,
    require_stable_id,
)


_CURRENCY = re.compile(r"^[A-Z][A-Z0-9]{2,11}$")
_SECONDS_PER_YEAR = Decimal("31557600")  # 365.25 days
_ZERO = Decimal("0")
_ONE = Decimal("1")


def _require_schema_version(value: int) -> None:
    if value != DERIVATIVE_RESEARCH_SCHEMA_VERSION:
        raise DerivativeResearchError("option_schema_version_unsupported")


def _computed_decimal(value: float) -> Decimal:
    if not math.isfinite(value):
        raise DerivativeResearchError("option_computation_non_finite")
    return Decimal(format(value, ".15g"))


def _optional_decimal(
    value: object | None,
    field_name: str,
    *,
    non_negative: bool = False,
) -> Decimal | None:
    if value is None:
        return None
    parsed = exact_decimal(value, field_name)
    if non_negative and parsed < 0:
        raise DerivativeResearchError(f"{field_name}_must_be_non_negative")
    return parsed


def _require_enum(value: object, enum_type: type[StrEnum], field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise DerivativeResearchError(f"{field_name}_enum_required")


def _time_years(start: str, end: str) -> Decimal:
    seconds = Decimal(
        str(
            (
                parse_timestamp(end, "option.time_end")
                - parse_timestamp(start, "option.time_start")
            ).total_seconds()
        )
    )
    return max(_ZERO, seconds / _SECONDS_PER_YEAR)


def _signed(side: "PositionSide") -> Decimal:
    return _ONE if side is PositionSide.LONG else -_ONE


class OptionType(StrEnum):
    CALL = "CALL"
    PUT = "PUT"


class ExerciseStyle(StrEnum):
    EUROPEAN = "EUROPEAN"
    AMERICAN = "AMERICAN"
    BERMUDAN = "BERMUDAN"


class SettlementType(StrEnum):
    CASH = "CASH"
    PHYSICAL = "PHYSICAL"


class QuoteState(StrEnum):
    NORMAL = "NORMAL"
    NO_QUOTE = "NO_QUOTE"
    ZERO_BID = "ZERO_BID"
    CROSSED = "CROSSED"
    STALE = "STALE"
    ILLIQUID = "ILLIQUID"


class TransactionSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class PositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class FillStatus(StrEnum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    UNFILLED = "UNFILLED"
    FAILED = "FAILED"
    UNWOUND = "UNWOUND"


class IVFailure(StrEnum):
    NONE = "NONE"
    NO_QUOTE = "NO_QUOTE"
    ZERO_BID = "ZERO_BID"
    CROSSED = "CROSSED"
    STALE = "STALE"
    ILLIQUID = "ILLIQUID"
    INVALID_INPUT = "INVALID_INPUT"
    OUTSIDE_ARBITRAGE_BOUNDS = "OUTSIDE_ARBITRAGE_BOUNDS"
    NO_SOLUTION = "NO_SOLUTION"


class MoneynessMethod(StrEnum):
    STRIKE_OVER_SPOT = "STRIKE_OVER_SPOT"
    STRIKE_OVER_FORWARD = "STRIKE_OVER_FORWARD"
    LOG_FORWARD_MONEYNESS = "LOG_FORWARD_MONEYNESS"


class MultiLegExecutionPolicy(StrEnum):
    SIMULTANEOUS = "SIMULTANEOUS"
    SEQUENTIAL = "SEQUENTIAL"


class MultiLegState(StrEnum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNWOUND = "UNWOUND"


class LifecycleEventType(StrEnum):
    EXPIRY = "EXPIRY"
    EXERCISE = "EXERCISE"
    ASSIGNMENT = "ASSIGNMENT"


class ProspectiveStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    DEGRADED = "DEGRADED"
    INVALIDATED = "INVALIDATED"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class OptionContract:
    """One uniquely identified listed option series."""

    contract_id: str
    underlying_id: str
    option_type: OptionType
    strike: Decimal
    expiration_at: str
    exercise_style: ExerciseStyle
    settlement_type: SettlementType
    multiplier: Decimal
    currency: str
    exchange: str
    listing_at: str
    last_trade_at: str
    settlement_at: str
    price_tick: Decimal
    quantity_step: Decimal = Decimal("1")
    bermudan_exercise_at: tuple[str, ...] = ()
    adjusted_contract: bool = False
    deliverable_asset_id: str | None = None
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        require_stable_id(self.contract_id, "option_contract.contract_id")
        require_stable_id(self.underlying_id, "option_contract.underlying_id")
        _require_enum(self.option_type, OptionType, "option_contract.option_type")
        _require_enum(
            self.exercise_style, ExerciseStyle, "option_contract.exercise_style"
        )
        _require_enum(
            self.settlement_type, SettlementType, "option_contract.settlement_type"
        )
        strike = exact_decimal(self.strike, "option_contract.strike", positive=True)
        multiplier = exact_decimal(
            self.multiplier, "option_contract.multiplier", positive=True
        )
        price_tick = exact_decimal(
            self.price_tick, "option_contract.price_tick", positive=True
        )
        quantity_step = exact_decimal(
            self.quantity_step, "option_contract.quantity_step", positive=True
        )
        object.__setattr__(self, "strike", strike)
        object.__setattr__(self, "multiplier", multiplier)
        object.__setattr__(self, "price_tick", price_tick)
        object.__setattr__(self, "quantity_step", quantity_step)
        if not _CURRENCY.fullmatch(self.currency):
            raise DerivativeResearchError("option_contract_currency_invalid")
        require_stable_id(self.exchange, "option_contract.exchange")
        listing = parse_timestamp(self.listing_at, "option_contract.listing_at")
        last_trade = parse_timestamp(
            self.last_trade_at, "option_contract.last_trade_at"
        )
        expiration = parse_timestamp(
            self.expiration_at, "option_contract.expiration_at"
        )
        settlement = parse_timestamp(
            self.settlement_at, "option_contract.settlement_at"
        )
        if not listing < last_trade <= expiration <= settlement:
            raise DerivativeResearchError("option_contract_time_order_invalid")
        bermudan = tuple(
            parse_timestamp(item, "option_contract.bermudan_exercise_at")
            for item in self.bermudan_exercise_at
        )
        if self.exercise_style is ExerciseStyle.BERMUDAN:
            if not bermudan or any(
                not listing <= item <= expiration for item in bermudan
            ):
                raise DerivativeResearchError("bermudan_exercise_schedule_invalid")
            if tuple(sorted(bermudan)) != bermudan or len(set(bermudan)) != len(
                bermudan
            ):
                raise DerivativeResearchError("bermudan_exercise_schedule_invalid")
        elif self.bermudan_exercise_at:
            raise DerivativeResearchError("bermudan_schedule_for_non_bermudan_option")
        if self.settlement_type is SettlementType.PHYSICAL:
            if self.deliverable_asset_id is None:
                raise DerivativeResearchError(
                    "physical_option_deliverable_asset_required"
                )
            require_stable_id(
                self.deliverable_asset_id,
                "option_contract.deliverable_asset_id",
            )
        elif self.deliverable_asset_id is not None:
            raise DerivativeResearchError("cash_option_cannot_declare_deliverable")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_contract"),
        )

    @property
    def series_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.underlying_id,
            self.option_type.value,
            decimal_text(self.strike),
            self.expiration_at,
            self.exchange,
        )

    def is_listed_at(self, as_of: str) -> bool:
        instant = parse_timestamp(as_of, "option_contract.as_of")
        return (
            parse_timestamp(self.listing_at, "option_contract.listing_at")
            <= instant
            <= parse_timestamp(self.settlement_at, "option_contract.settlement_at")
        )

    def is_tradeable_at(self, as_of: str) -> bool:
        instant = parse_timestamp(as_of, "option_contract.as_of")
        return (
            parse_timestamp(self.listing_at, "option_contract.listing_at")
            <= instant
            <= parse_timestamp(self.last_trade_at, "option_contract.last_trade_at")
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "underlying_id": self.underlying_id,
            "option_type": self.option_type.value,
            "strike": decimal_text(self.strike),
            "expiration_at": self.expiration_at,
            "exercise_style": self.exercise_style.value,
            "settlement_type": self.settlement_type.value,
            "multiplier": decimal_text(self.multiplier),
            "currency": self.currency,
            "exchange": self.exchange,
            "listing_at": self.listing_at,
            "last_trade_at": self.last_trade_at,
            "settlement_at": self.settlement_at,
            "price_tick": decimal_text(self.price_tick),
            "quantity_step": decimal_text(self.quantity_step),
            "bermudan_exercise_at": list(self.bermudan_exercise_at),
            "adjusted_contract": self.adjusted_contract,
            "deliverable_asset_id": self.deliverable_asset_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class OptionQuote:
    """An immutable two-sided quote with explicit unusable states."""

    quote_id: str
    contract_id: str
    availability: AvailabilityTimes
    as_of: str
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    bid_size: Decimal = Decimal("0")
    ask_size: Decimal = Decimal("0")
    volume: int = 0
    open_interest: int = 0
    stale_after_seconds: int = 60
    max_spread_ratio: Decimal = Decimal("0.25")
    min_volume: int = 0
    min_open_interest: int = 0
    state: QuoteState = field(init=False)
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        require_stable_id(self.quote_id, "option_quote.quote_id")
        require_stable_id(self.contract_id, "option_quote.contract_id")
        if not isinstance(self.availability, AvailabilityTimes):
            raise DerivativeResearchError("option_quote_availability_required")
        as_of = parse_timestamp(self.as_of, "option_quote.as_of")
        if not self.availability.known_at(self.as_of):
            raise DerivativeResearchError("option_quote_not_known_at_as_of")
        bid = _optional_decimal(self.bid, "option_quote.bid", non_negative=True)
        ask = _optional_decimal(self.ask, "option_quote.ask", non_negative=True)
        last = _optional_decimal(self.last, "option_quote.last", non_negative=True)
        bid_size = exact_decimal(self.bid_size, "option_quote.bid_size")
        ask_size = exact_decimal(self.ask_size, "option_quote.ask_size")
        max_spread = exact_decimal(
            self.max_spread_ratio, "option_quote.max_spread_ratio", positive=True
        )
        if bid_size < 0 or ask_size < 0:
            raise DerivativeResearchError("option_quote_size_negative")
        if ask is not None and ask <= 0:
            raise DerivativeResearchError("option_quote_ask_must_be_positive")
        for name, value in (
            ("volume", self.volume),
            ("open_interest", self.open_interest),
            ("min_volume", self.min_volume),
            ("min_open_interest", self.min_open_interest),
            ("stale_after_seconds", self.stale_after_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise DerivativeResearchError(f"option_quote_{name}_invalid")
        object.__setattr__(self, "bid", bid)
        object.__setattr__(self, "ask", ask)
        object.__setattr__(self, "last", last)
        object.__setattr__(self, "bid_size", bid_size)
        object.__setattr__(self, "ask_size", ask_size)
        object.__setattr__(self, "max_spread_ratio", max_spread)
        age = (
            as_of - parse_timestamp(self.availability.event_at, "quote.event_at")
        ).total_seconds()
        if bid is None or ask is None:
            state = QuoteState.NO_QUOTE
        elif bid > ask:
            state = QuoteState.CROSSED
        elif bid == 0:
            state = QuoteState.ZERO_BID
        elif age > self.stale_after_seconds:
            state = QuoteState.STALE
        else:
            midpoint = (bid + ask) / Decimal("2")
            spread_ratio = (ask - bid) / midpoint
            insufficient = (
                spread_ratio > max_spread
                or self.volume < self.min_volume
                or self.open_interest < self.min_open_interest
                or bid_size == 0
                or ask_size == 0
            )
            state = QuoteState.ILLIQUID if insufficient else QuoteState.NORMAL
        object.__setattr__(self, "state", state)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_quote"),
        )

    @property
    def quote_age_seconds(self) -> Decimal:
        age = parse_timestamp(self.as_of, "option_quote.as_of") - parse_timestamp(
            self.availability.event_at, "option_quote.event_at"
        )
        return Decimal(str(age.total_seconds()))

    @property
    def midpoint(self) -> Decimal | None:
        if self.state not in {QuoteState.NORMAL, QuoteState.ILLIQUID}:
            return None
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread_width(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    def executable_price(
        self, side: TransactionSide, *, allow_illiquid: bool = False
    ) -> Decimal:
        _require_enum(side, TransactionSide, "option_quote.transaction_side")
        allowed = {QuoteState.NORMAL}
        if allow_illiquid:
            allowed.add(QuoteState.ILLIQUID)
        if self.state not in allowed:
            raise DerivativeResearchError(
                f"option_quote_not_executable:{self.state.value}"
            )
        price = self.ask if side is TransactionSide.BUY else self.bid
        if price is None or price <= 0:
            raise DerivativeResearchError("option_quote_executable_price_missing")
        return price

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "quote_id": self.quote_id,
            "contract_id": self.contract_id,
            "availability": self.availability.as_dict(),
            "as_of": self.as_of,
            "bid": decimal_text(self.bid) if self.bid is not None else None,
            "ask": decimal_text(self.ask) if self.ask is not None else None,
            "last": decimal_text(self.last) if self.last is not None else None,
            "bid_size": decimal_text(self.bid_size),
            "ask_size": decimal_text(self.ask_size),
            "volume": self.volume,
            "open_interest": self.open_interest,
            "stale_after_seconds": self.stale_after_seconds,
            "max_spread_ratio": decimal_text(self.max_spread_ratio),
            "min_volume": self.min_volume,
            "min_open_interest": self.min_open_interest,
            "state": self.state.value,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class OptionChainSnapshot:
    """The exact set of listed option series knowable at one instant."""

    chain_snapshot_id: str
    underlying_id: str
    knowledge_time: str
    underlying_price: Decimal
    contracts: tuple[OptionContract, ...]
    quotes: tuple[OptionQuote, ...]
    source_manifest_hashes: tuple[str, ...]
    quality_results: tuple[QualityResult, ...] = ()
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        require_stable_id(self.chain_snapshot_id, "option_chain.chain_snapshot_id")
        require_stable_id(self.underlying_id, "option_chain.underlying_id")
        knowledge_time = parse_timestamp(
            self.knowledge_time, "option_chain.knowledge_time"
        )
        underlying_price = exact_decimal(
            self.underlying_price, "option_chain.underlying_price", positive=True
        )
        object.__setattr__(self, "underlying_price", underlying_price)
        if not self.contracts:
            raise DerivativeResearchError("option_chain_contracts_required")
        contract_ids = [item.contract_id for item in self.contracts]
        series_keys = [item.series_key for item in self.contracts]
        if len(set(contract_ids)) != len(contract_ids):
            raise DerivativeResearchError("option_chain_contract_id_duplicate")
        if len(set(series_keys)) != len(series_keys):
            raise DerivativeResearchError("option_chain_series_duplicate")
        if any(item.underlying_id != self.underlying_id for item in self.contracts):
            raise DerivativeResearchError("option_chain_underlying_mismatch")
        not_listed = sorted(
            item.contract_id
            for item in self.contracts
            if not item.is_listed_at(self.knowledge_time)
        )
        if not_listed:
            raise DerivativeResearchError(
                "option_chain_contains_unlisted_series:" + ",".join(not_listed)
            )
        quote_ids = [item.contract_id for item in self.quotes]
        if len(quote_ids) != len(set(quote_ids)):
            raise DerivativeResearchError("option_chain_quote_duplicate")
        if set(quote_ids) != set(contract_ids):
            raise DerivativeResearchError("option_chain_quote_coverage_mismatch")
        if any(
            not item.availability.known_at(self.knowledge_time) for item in self.quotes
        ):
            raise DerivativeResearchError("option_chain_quote_future_knowledge")
        if any(
            parse_timestamp(item.as_of, "option_quote.as_of") > knowledge_time
            for item in self.quotes
        ):
            raise DerivativeResearchError("option_chain_quote_future_as_of")
        if not self.source_manifest_hashes:
            raise DerivativeResearchError("option_chain_source_manifest_required")
        for value in self.source_manifest_hashes:
            require_hash(value, "option_chain.source_manifest_hash")
        if len(set(self.source_manifest_hashes)) != len(self.source_manifest_hashes):
            raise DerivativeResearchError("option_chain_source_manifest_duplicate")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_chain_snapshot"),
        )

    def contract(self, contract_id: str) -> OptionContract:
        found = next(
            (item for item in self.contracts if item.contract_id == contract_id),
            None,
        )
        return found if found is not None else _raise_missing_contract(contract_id)

    def quote(self, contract_id: str) -> OptionQuote:
        found = next(
            (item for item in self.quotes if item.contract_id == contract_id),
            None,
        )
        return found if found is not None else _raise_missing_quote(contract_id)

    def select(
        self,
        *,
        option_type: OptionType | None = None,
        expiration_at: str | None = None,
        minimum_open_interest: int = 0,
        maximum_quote_age_seconds: int | None = None,
    ) -> tuple[OptionContract, ...]:
        selected: list[OptionContract] = []
        for contract in self.contracts:
            quote = self.quote(contract.contract_id)
            if option_type is not None and contract.option_type is not option_type:
                continue
            if expiration_at is not None and contract.expiration_at != expiration_at:
                continue
            if quote.open_interest < minimum_open_interest:
                continue
            if (
                maximum_quote_age_seconds is not None
                and quote.quote_age_seconds > maximum_quote_age_seconds
            ):
                continue
            if quote.state is not QuoteState.NORMAL:
                continue
            selected.append(contract)
        return tuple(
            sorted(selected, key=lambda item: (item.expiration_at, item.strike))
        )

    def admit(self, run_type: RunType) -> None:
        if run_type in {RunType.CONFIRMATORY, RunType.PROSPECTIVE}:
            require_confirmatory_quality(self.quality_results)

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "chain_snapshot_id": self.chain_snapshot_id,
            "underlying_id": self.underlying_id,
            "knowledge_time": self.knowledge_time,
            "underlying_price": decimal_text(self.underlying_price),
            "contracts": [item.as_dict() for item in self.contracts],
            "quotes": [item.as_dict() for item in self.quotes],
            "source_manifest_hashes": list(self.source_manifest_hashes),
            "quality_results": [item.as_dict() for item in self.quality_results],
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def _raise_missing_contract(contract_id: str) -> OptionContract:
    raise DerivativeResearchError(f"option_chain_contract_missing:{contract_id}")


def _raise_missing_quote(contract_id: str) -> OptionQuote:
    raise DerivativeResearchError(f"option_chain_quote_missing:{contract_id}")


def option_chain_as_of(
    *,
    chain_snapshot_id: str,
    underlying_id: str,
    as_of: str,
    underlying_price: Decimal,
    contracts: Sequence[OptionContract],
    quotes: Sequence[OptionQuote],
    source_manifest_hashes: tuple[str, ...],
    quality_results: tuple[QualityResult, ...] = (),
) -> OptionChainSnapshot:
    """Build a PIT-safe chain, excluding rather than peeking at future series."""

    included_contracts = tuple(item for item in contracts if item.is_listed_at(as_of))
    included_ids = {item.contract_id for item in included_contracts}
    included_quotes = tuple(
        item
        for item in quotes
        if item.contract_id in included_ids and item.availability.known_at(as_of)
    )
    return OptionChainSnapshot(
        chain_snapshot_id=chain_snapshot_id,
        underlying_id=underlying_id,
        knowledge_time=as_of,
        underlying_price=underlying_price,
        contracts=included_contracts,
        quotes=included_quotes,
        source_manifest_hashes=source_manifest_hashes,
        quality_results=quality_results,
    )


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _normal_pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / math.sqrt(2.0 * math.pi)


@dataclass(frozen=True, slots=True)
class BlackScholesIVSemanticResult:
    """Deterministic numerical result shared by runtime and persisted verification."""

    failure: IVFailure
    volatility: Decimal | None
    iterations: int
    lower_price_bound: Decimal
    upper_price_bound: Decimal
    model_price: Decimal | None
    residual: Decimal | None


def _black_scholes_price_float(
    *,
    option_type: OptionType,
    spot: float,
    strike: float,
    risk_free_rate: float,
    dividend_yield: float,
    time_years: float,
    volatility: float,
) -> float:
    if time_years <= 0:
        return (
            max(spot - strike, 0.0)
            if option_type is OptionType.CALL
            else max(strike - spot, 0.0)
        )
    root_time = math.sqrt(time_years)
    d1 = (
        math.log(spot / strike)
        + (risk_free_rate - dividend_yield + 0.5 * volatility * volatility) * time_years
    ) / (volatility * root_time)
    d2 = d1 - volatility * root_time
    if option_type is OptionType.CALL:
        return spot * math.exp(-dividend_yield * time_years) * _normal_cdf(
            d1
        ) - strike * math.exp(-risk_free_rate * time_years) * _normal_cdf(d2)
    return strike * math.exp(-risk_free_rate * time_years) * _normal_cdf(
        -d2
    ) - spot * math.exp(-dividend_yield * time_years) * _normal_cdf(-d1)


def _black_scholes_arbitrage_bounds(
    *,
    option_type: OptionType,
    spot: Decimal,
    strike: Decimal,
    risk_free_rate: Decimal,
    dividend_yield: Decimal,
    time_years: Decimal,
) -> tuple[Decimal, Decimal]:
    time = float(time_years)
    discounted_spot = float(spot) * math.exp(-float(dividend_yield) * time)
    discounted_strike = float(strike) * math.exp(-float(risk_free_rate) * time)
    if option_type is OptionType.CALL:
        lower = max(0.0, discounted_spot - discounted_strike)
        upper = discounted_spot
    else:
        lower = max(0.0, discounted_strike - discounted_spot)
        upper = discounted_strike
    return _computed_decimal(lower), _computed_decimal(upper)


def solve_black_scholes_implied_volatility(
    *,
    option_type: OptionType,
    spot: Decimal,
    strike: Decimal,
    risk_free_rate: Decimal,
    dividend_yield: Decimal,
    time_years: Decimal,
    market_price: Decimal,
    minimum_volatility: Decimal,
    maximum_volatility: Decimal,
    price_tolerance: Decimal,
    maximum_iterations: int,
) -> BlackScholesIVSemanticResult:
    """Run the canonical bisection solver from primitive immutable inputs."""

    time = float(time_years)
    lower, upper = _black_scholes_arbitrage_bounds(
        option_type=option_type,
        spot=spot,
        strike=strike,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
        time_years=time_years,
    )
    if market_price < lower - price_tolerance or market_price > upper + price_tolerance:
        return BlackScholesIVSemanticResult(
            failure=IVFailure.OUTSIDE_ARBITRAGE_BOUNDS,
            volatility=None,
            iterations=0,
            lower_price_bound=lower,
            upper_price_bound=upper,
            model_price=None,
            residual=None,
        )
    low = float(minimum_volatility)
    high = float(maximum_volatility)
    target = float(market_price)

    def model_price(volatility: float) -> float:
        return _black_scholes_price_float(
            option_type=option_type,
            spot=float(spot),
            strike=float(strike),
            risk_free_rate=float(risk_free_rate),
            dividend_yield=float(dividend_yield),
            time_years=time,
            volatility=volatility,
        )

    if model_price(high) + float(price_tolerance) < target:
        return BlackScholesIVSemanticResult(
            failure=IVFailure.NO_SOLUTION,
            volatility=None,
            iterations=0,
            lower_price_bound=lower,
            upper_price_bound=upper,
            model_price=None,
            residual=None,
        )
    iterations = 0
    while iterations < maximum_iterations:
        iterations += 1
        midpoint = (low + high) / 2.0
        computed = model_price(midpoint)
        difference = computed - target
        if abs(difference) <= float(price_tolerance):
            return BlackScholesIVSemanticResult(
                failure=IVFailure.NONE,
                volatility=_computed_decimal(midpoint),
                iterations=iterations,
                lower_price_bound=lower,
                upper_price_bound=upper,
                model_price=_computed_decimal(computed),
                residual=_computed_decimal(abs(difference)),
            )
        if difference > 0:
            high = midpoint
        else:
            low = midpoint
    return BlackScholesIVSemanticResult(
        failure=IVFailure.NO_SOLUTION,
        volatility=None,
        iterations=iterations,
        lower_price_bound=lower,
        upper_price_bound=upper,
        model_price=None,
        residual=None,
    )


def _arbitrage_bounds(
    contract: OptionContract,
    *,
    spot: Decimal,
    risk_free_rate: Decimal,
    dividend_yield: Decimal,
    valuation_at: str,
) -> tuple[Decimal, Decimal]:
    return _black_scholes_arbitrage_bounds(
        option_type=contract.option_type,
        spot=spot,
        strike=contract.strike,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
        time_years=_time_years(valuation_at, contract.expiration_at),
    )


def evaluate_option_chain_quality(
    snapshot: OptionChainSnapshot,
    *,
    risk_free_rate: Decimal = Decimal("0"),
    dividend_yield: Decimal = Decimal("0"),
    parity_tolerance: Decimal = Decimal("0.05"),
) -> tuple[QualityResult, ...]:
    """Run quote-state and static no-arbitrage checks over one PIT chain."""

    rate = exact_decimal(risk_free_rate, "option_quality.risk_free_rate")
    dividend = exact_decimal(dividend_yield, "option_quality.dividend_yield")
    tolerance = exact_decimal(
        parity_tolerance, "option_quality.parity_tolerance", positive=True
    )
    crossed = tuple(
        item.contract_id for item in snapshot.quotes if item.state is QuoteState.CROSSED
    )
    stale = tuple(
        item.contract_id for item in snapshot.quotes if item.state is QuoteState.STALE
    )
    restricted = tuple(
        item.contract_id
        for item in snapshot.quotes
        if item.state in {QuoteState.NO_QUOTE, QuoteState.ZERO_BID, QuoteState.ILLIQUID}
    )
    quote_decision = (
        QualityDecision.FAILED
        if crossed
        else QualityDecision.STALE
        if stale
        else QualityDecision.RESTRICTED
        if restricted
        else QualityDecision.PASS
    )
    results: list[QualityResult] = [
        QualityResult(
            check_id="option.quote_state",
            check_version="1",
            decision=quote_decision,
            affected_ids=tuple(sorted(set((*crossed, *stale, *restricted)))),
            diagnostics=tuple(
                item
                for item in (
                    "crossed_quote" if crossed else "",
                    "stale_quote" if stale else "",
                    "restricted_liquidity" if restricted else "",
                )
                if item
            ),
        )
    ]
    bound_violations: list[str] = []
    usable: dict[tuple[str, OptionType], list[tuple[OptionContract, Decimal]]] = {}
    for contract in snapshot.contracts:
        quote = snapshot.quote(contract.contract_id)
        midpoint = quote.midpoint
        if midpoint is None:
            continue
        lower, upper = _arbitrage_bounds(
            contract,
            spot=snapshot.underlying_price,
            risk_free_rate=rate,
            dividend_yield=dividend,
            valuation_at=snapshot.knowledge_time,
        )
        if midpoint < lower - tolerance or midpoint > upper + tolerance:
            bound_violations.append(contract.contract_id)
        usable.setdefault((contract.expiration_at, contract.option_type), []).append(
            (contract, midpoint)
        )
    results.append(
        QualityResult(
            check_id="option.theoretical_bounds",
            check_version="1",
            decision=QualityDecision.FAILED
            if bound_violations
            else QualityDecision.PASS,
            affected_ids=tuple(sorted(bound_violations)),
            diagnostics=("premium_outside_no_arbitrage_bounds",)
            if bound_violations
            else (),
        )
    )
    monotonic: list[str] = []
    convexity: list[str] = []
    for (_expiry, option_type), rows in usable.items():
        ordered = sorted(rows, key=lambda item: item[0].strike)
        prices = [price for _contract, price in ordered]
        for index in range(1, len(ordered)):
            violates = (
                prices[index] > prices[index - 1] + tolerance
                if option_type is OptionType.CALL
                else prices[index] + tolerance < prices[index - 1]
            )
            if violates:
                monotonic.extend(
                    [ordered[index - 1][0].contract_id, ordered[index][0].contract_id]
                )
        for index in range(1, len(ordered) - 1):
            k0, k1, k2 = (
                ordered[index - 1][0].strike,
                ordered[index][0].strike,
                ordered[index + 1][0].strike,
            )
            p0, p1, p2 = prices[index - 1], prices[index], prices[index + 1]
            left_slope = (p1 - p0) / (k1 - k0)
            right_slope = (p2 - p1) / (k2 - k1)
            if right_slope + tolerance < left_slope:
                convexity.append(ordered[index][0].contract_id)
    results.extend(
        (
            QualityResult(
                check_id="option.strike_monotonicity",
                check_version="1",
                decision=QualityDecision.FAILED if monotonic else QualityDecision.PASS,
                affected_ids=tuple(sorted(set(monotonic))),
                diagnostics=("strike_monotonicity_violation",) if monotonic else (),
            ),
            QualityResult(
                check_id="option.strike_convexity",
                check_version="1",
                decision=QualityDecision.FAILED if convexity else QualityDecision.PASS,
                affected_ids=tuple(sorted(set(convexity))),
                diagnostics=("strike_convexity_violation",) if convexity else (),
            ),
        )
    )
    parity_violations: list[str] = []
    pairs: dict[tuple[str, str], dict[OptionType, tuple[OptionContract, Decimal]]] = {}
    for contract in snapshot.contracts:
        midpoint = snapshot.quote(contract.contract_id).midpoint
        if midpoint is not None:
            pairs.setdefault(
                (contract.expiration_at, decimal_text(contract.strike)), {}
            )[contract.option_type] = (contract, midpoint)
    for (expiry, _strike), sides in pairs.items():
        if OptionType.CALL not in sides or OptionType.PUT not in sides:
            continue
        call, call_price = sides[OptionType.CALL]
        put, put_price = sides[OptionType.PUT]
        time = float(_time_years(snapshot.knowledge_time, expiry))
        expected = float(snapshot.underlying_price) * math.exp(
            -float(dividend) * time
        ) - float(call.strike) * math.exp(-float(rate) * time)
        if abs(float(call_price - put_price) - expected) > float(tolerance):
            parity_violations.extend((call.contract_id, put.contract_id))
    results.append(
        QualityResult(
            check_id="option.put_call_parity",
            check_version="1",
            decision=QualityDecision.FAILED
            if parity_violations
            else QualityDecision.PASS,
            affected_ids=tuple(sorted(set(parity_violations))),
            diagnostics=("put_call_parity_violation",) if parity_violations else (),
        )
    )
    return tuple(results)


@dataclass(frozen=True, slots=True)
class ValuationInputSnapshot:
    """PIT-aligned market and carry inputs for one option valuation."""

    valuation_input_id: str
    contract: OptionContract
    quote: OptionQuote
    valuation_at: str
    spot_price: Decimal
    risk_free_rate: Decimal
    dividend_yield: Decimal
    forward_price: Decimal
    spot_availability: AvailabilityTimes
    rate_availability: AvailabilityTimes
    dividend_availability: AvailabilityTimes
    forward_availability: AvailabilityTimes
    source_manifest_hashes: tuple[str, ...]
    maximum_alignment_seconds: int = 60
    maximum_forward_deviation_ratio: Decimal = Decimal("0.05")
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        require_stable_id(
            self.valuation_input_id, "option_valuation_input.valuation_input_id"
        )
        if not isinstance(self.contract, OptionContract):
            raise DerivativeResearchError("option_valuation_contract_required")
        if not isinstance(self.quote, OptionQuote):
            raise DerivativeResearchError("option_valuation_quote_required")
        if self.quote.contract_id != self.contract.contract_id:
            raise DerivativeResearchError("option_valuation_quote_contract_mismatch")
        valuation = parse_timestamp(
            self.valuation_at, "option_valuation_input.valuation_at"
        )
        if valuation != parse_timestamp(self.quote.as_of, "option_quote.as_of"):
            raise DerivativeResearchError("option_valuation_quote_time_mismatch")
        if valuation > parse_timestamp(
            self.contract.expiration_at, "option_contract.expiration_at"
        ):
            raise DerivativeResearchError("option_valuation_after_expiration")
        spot = exact_decimal(
            self.spot_price, "option_valuation_input.spot_price", positive=True
        )
        rate = exact_decimal(
            self.risk_free_rate, "option_valuation_input.risk_free_rate"
        )
        dividend = exact_decimal(
            self.dividend_yield, "option_valuation_input.dividend_yield"
        )
        forward = exact_decimal(
            self.forward_price, "option_valuation_input.forward_price", positive=True
        )
        deviation = exact_decimal(
            self.maximum_forward_deviation_ratio,
            "option_valuation_input.maximum_forward_deviation_ratio",
            positive=True,
        )
        object.__setattr__(self, "spot_price", spot)
        object.__setattr__(self, "risk_free_rate", rate)
        object.__setattr__(self, "dividend_yield", dividend)
        object.__setattr__(self, "forward_price", forward)
        object.__setattr__(self, "maximum_forward_deviation_ratio", deviation)
        if (
            isinstance(self.maximum_alignment_seconds, bool)
            or not isinstance(self.maximum_alignment_seconds, int)
            or self.maximum_alignment_seconds < 0
        ):
            raise DerivativeResearchError("option_valuation_alignment_limit_invalid")
        availabilities = (
            self.quote.availability,
            self.spot_availability,
            self.rate_availability,
            self.dividend_availability,
            self.forward_availability,
        )
        if any(not isinstance(item, AvailabilityTimes) for item in availabilities):
            raise DerivativeResearchError("option_valuation_availability_required")
        if any(not item.known_at(self.valuation_at) for item in availabilities):
            raise DerivativeResearchError("option_valuation_input_future_knowledge")
        processed = [item.available_at for item in availabilities]
        if (
            max(processed) - min(processed)
        ).total_seconds() > self.maximum_alignment_seconds:
            raise DerivativeResearchError("option_valuation_inputs_not_time_aligned")
        event_times = [
            parse_timestamp(item.event_at, "option_valuation_input.event_at")
            for item in availabilities
        ]
        if (
            max(event_times) - min(event_times)
        ).total_seconds() > self.maximum_alignment_seconds:
            raise DerivativeResearchError("option_valuation_events_not_time_aligned")
        time = float(_time_years(self.valuation_at, self.contract.expiration_at))
        implied_forward = float(spot) * math.exp(float(rate - dividend) * time)
        forward_deviation = abs(float(forward) - implied_forward) / implied_forward
        if forward_deviation > float(deviation):
            raise DerivativeResearchError("option_valuation_forward_inconsistent")
        if not self.source_manifest_hashes:
            raise DerivativeResearchError("option_valuation_sources_required")
        for source_hash in self.source_manifest_hashes:
            require_hash(source_hash, "option_valuation_input.source_manifest_hash")
        if len(set(self.source_manifest_hashes)) != len(self.source_manifest_hashes):
            raise DerivativeResearchError("option_valuation_source_hash_duplicate")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_valuation_input"),
        )

    @property
    def time_to_expiry_years(self) -> Decimal:
        return _time_years(self.valuation_at, self.contract.expiration_at)

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "valuation_input_id": self.valuation_input_id,
            "contract": self.contract.as_dict(),
            "quote": self.quote.as_dict(),
            "valuation_at": self.valuation_at,
            "spot_price": decimal_text(self.spot_price),
            "risk_free_rate": decimal_text(self.risk_free_rate),
            "dividend_yield": decimal_text(self.dividend_yield),
            "forward_price": decimal_text(self.forward_price),
            "spot_availability": self.spot_availability.as_dict(),
            "rate_availability": self.rate_availability.as_dict(),
            "dividend_availability": self.dividend_availability.as_dict(),
            "forward_availability": self.forward_availability.as_dict(),
            "source_manifest_hashes": list(self.source_manifest_hashes),
            "maximum_alignment_seconds": self.maximum_alignment_seconds,
            "maximum_forward_deviation_ratio": decimal_text(
                self.maximum_forward_deviation_ratio
            ),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class ImpliedVolatilityResult:
    contract_id: str
    valuation_input_hash: str
    model_version: str
    success: bool
    volatility: Decimal | None
    failure: IVFailure
    iterations: int
    market_price: Decimal | None
    lower_price_bound: Decimal | None
    upper_price_bound: Decimal | None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.contract_id, "option_iv.contract_id")
        require_hash(self.valuation_input_hash, "option_iv.valuation_input_hash")
        require_stable_id(self.model_version, "option_iv.model_version")
        _require_enum(self.failure, IVFailure, "option_iv.failure")
        volatility = _optional_decimal(
            self.volatility, "option_iv.volatility", non_negative=True
        )
        market_price = _optional_decimal(
            self.market_price, "option_iv.market_price", non_negative=True
        )
        lower = _optional_decimal(
            self.lower_price_bound, "option_iv.lower_price_bound", non_negative=True
        )
        upper = _optional_decimal(
            self.upper_price_bound, "option_iv.upper_price_bound", non_negative=True
        )
        object.__setattr__(self, "volatility", volatility)
        object.__setattr__(self, "market_price", market_price)
        object.__setattr__(self, "lower_price_bound", lower)
        object.__setattr__(self, "upper_price_bound", upper)
        if self.iterations < 0:
            raise DerivativeResearchError("option_iv_iterations_invalid")
        if self.success:
            if (
                volatility is None
                or volatility <= 0
                or self.failure is not IVFailure.NONE
            ):
                raise DerivativeResearchError("option_iv_success_contract_invalid")
        elif volatility is not None or self.failure is IVFailure.NONE:
            raise DerivativeResearchError("option_iv_failure_contract_invalid")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.as_payload(), label="option_implied_volatility"),
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "valuation_input_hash": self.valuation_input_hash,
            "model_version": self.model_version,
            "success": self.success,
            "volatility": decimal_text(self.volatility)
            if self.volatility is not None
            else None,
            "failure": self.failure.value,
            "iterations": self.iterations,
            "market_price": decimal_text(self.market_price)
            if self.market_price is not None
            else None,
            "lower_price_bound": decimal_text(self.lower_price_bound)
            if self.lower_price_bound is not None
            else None,
            "upper_price_bound": decimal_text(self.upper_price_bound)
            if self.upper_price_bound is not None
            else None,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.as_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class OptionGreeks:
    contract_id: str
    valuation_input_hash: str
    model_version: str
    volatility: Decimal
    price: Decimal
    delta: Decimal
    gamma: Decimal
    vega: Decimal
    theta_per_year: Decimal
    rho: Decimal
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.contract_id, "option_greeks.contract_id")
        require_hash(self.valuation_input_hash, "option_greeks.input_hash")
        require_stable_id(self.model_version, "option_greeks.model_version")
        for name in (
            "volatility",
            "price",
            "delta",
            "gamma",
            "vega",
            "theta_per_year",
            "rho",
        ):
            parsed = exact_decimal(getattr(self, name), f"option_greeks.{name}")
            object.__setattr__(self, name, parsed)
        if self.volatility <= 0 or self.price < 0 or self.gamma < 0 or self.vega < 0:
            raise DerivativeResearchError("option_greeks_value_invalid")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.as_payload(), label="option_greeks"),
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "valuation_input_hash": self.valuation_input_hash,
            "model_version": self.model_version,
            "volatility": decimal_text(self.volatility),
            "price": decimal_text(self.price),
            "delta": decimal_text(self.delta),
            "gamma": decimal_text(self.gamma),
            "vega": decimal_text(self.vega),
            "theta_per_year": decimal_text(self.theta_per_year),
            "rho": decimal_text(self.rho),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.as_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class BlackScholesModel:
    """Versioned European Black-Scholes reference implementation."""

    model_version: str = "black_scholes_european_v1"
    minimum_volatility: Decimal = Decimal("0.000001")
    maximum_volatility: Decimal = Decimal("5")
    price_tolerance: Decimal = Decimal("0.00000001")
    maximum_iterations: int = 200
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.model_version, "option_model.model_version")
        minimum = exact_decimal(
            self.minimum_volatility, "option_model.minimum_volatility", positive=True
        )
        maximum = exact_decimal(
            self.maximum_volatility, "option_model.maximum_volatility", positive=True
        )
        tolerance = exact_decimal(
            self.price_tolerance, "option_model.price_tolerance", positive=True
        )
        if minimum >= maximum:
            raise DerivativeResearchError("option_model_volatility_range_invalid")
        if self.maximum_iterations <= 0:
            raise DerivativeResearchError("option_model_iterations_invalid")
        object.__setattr__(self, "minimum_volatility", minimum)
        object.__setattr__(self, "maximum_volatility", maximum)
        object.__setattr__(self, "price_tolerance", tolerance)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_valuation_model"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "model_version": self.model_version,
            "minimum_volatility": decimal_text(self.minimum_volatility),
            "maximum_volatility": decimal_text(self.maximum_volatility),
            "price_tolerance": decimal_text(self.price_tolerance),
            "maximum_iterations": self.maximum_iterations,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def price(self, inputs: ValuationInputSnapshot, volatility: Decimal) -> Decimal:
        if inputs.contract.exercise_style is not ExerciseStyle.EUROPEAN:
            raise DerivativeResearchError("black_scholes_requires_european_option")
        sigma = exact_decimal(volatility, "option_model.volatility", positive=True)
        return _computed_decimal(self._price_float(inputs, float(sigma)))

    def _price_float(self, inputs: ValuationInputSnapshot, sigma: float) -> float:
        contract = inputs.contract
        return _black_scholes_price_float(
            option_type=contract.option_type,
            spot=float(inputs.spot_price),
            strike=float(contract.strike),
            risk_free_rate=float(inputs.risk_free_rate),
            dividend_yield=float(inputs.dividend_yield),
            time_years=float(inputs.time_to_expiry_years),
            volatility=sigma,
        )

    def implied_volatility(
        self,
        inputs: ValuationInputSnapshot,
        market_price: Decimal | None = None,
        *,
        permit_illiquid: bool = False,
    ) -> ImpliedVolatilityResult:
        quote = inputs.quote
        state_failures = {
            QuoteState.NO_QUOTE: IVFailure.NO_QUOTE,
            QuoteState.ZERO_BID: IVFailure.ZERO_BID,
            QuoteState.CROSSED: IVFailure.CROSSED,
            QuoteState.STALE: IVFailure.STALE,
            QuoteState.ILLIQUID: IVFailure.ILLIQUID,
        }
        if quote.state is not QuoteState.NORMAL and not (
            permit_illiquid and quote.state is QuoteState.ILLIQUID
        ):
            return self._iv_failure(inputs, state_failures[quote.state], None)
        try:
            selected = (
                exact_decimal(market_price, "option_iv.market_price", positive=True)
                if market_price is not None
                else quote.midpoint
            )
        except DerivativeResearchError:
            return self._iv_failure(inputs, IVFailure.INVALID_INPUT, None)
        if selected is None or selected <= 0:
            return self._iv_failure(inputs, IVFailure.INVALID_INPUT, selected)
        if inputs.contract.exercise_style is not ExerciseStyle.EUROPEAN:
            return self._iv_failure(inputs, IVFailure.INVALID_INPUT, selected)
        semantic = solve_black_scholes_implied_volatility(
            option_type=inputs.contract.option_type,
            spot=inputs.spot_price,
            strike=inputs.contract.strike,
            risk_free_rate=inputs.risk_free_rate,
            dividend_yield=inputs.dividend_yield,
            time_years=inputs.time_to_expiry_years,
            market_price=selected,
            minimum_volatility=self.minimum_volatility,
            maximum_volatility=self.maximum_volatility,
            price_tolerance=self.price_tolerance,
            maximum_iterations=self.maximum_iterations,
        )
        if semantic.failure is IVFailure.OUTSIDE_ARBITRAGE_BOUNDS:
            return self._iv_failure(
                inputs,
                IVFailure.OUTSIDE_ARBITRAGE_BOUNDS,
                selected,
                lower=semantic.lower_price_bound,
                upper=semantic.upper_price_bound,
            )
        if semantic.failure is IVFailure.NO_SOLUTION:
            return self._iv_failure(
                inputs,
                IVFailure.NO_SOLUTION,
                selected,
                lower=semantic.lower_price_bound,
                upper=semantic.upper_price_bound,
                iterations=semantic.iterations,
            )
        if semantic.volatility is None:
            raise DerivativeResearchError("option_iv_solver_success_missing_volatility")
        return ImpliedVolatilityResult(
            contract_id=inputs.contract.contract_id,
            valuation_input_hash=inputs.content_hash,
            model_version=self.model_version,
            success=True,
            volatility=semantic.volatility,
            failure=IVFailure.NONE,
            iterations=semantic.iterations,
            market_price=selected,
            lower_price_bound=semantic.lower_price_bound,
            upper_price_bound=semantic.upper_price_bound,
        )

    def _iv_failure(
        self,
        inputs: ValuationInputSnapshot,
        failure: IVFailure,
        market_price: Decimal | None,
        *,
        lower: Decimal | None = None,
        upper: Decimal | None = None,
        iterations: int = 0,
    ) -> ImpliedVolatilityResult:
        return ImpliedVolatilityResult(
            contract_id=inputs.contract.contract_id,
            valuation_input_hash=inputs.content_hash,
            model_version=self.model_version,
            success=False,
            volatility=None,
            failure=failure,
            iterations=iterations,
            market_price=market_price,
            lower_price_bound=lower,
            upper_price_bound=upper,
        )

    def greeks(
        self, inputs: ValuationInputSnapshot, volatility: Decimal
    ) -> OptionGreeks:
        if inputs.contract.exercise_style is not ExerciseStyle.EUROPEAN:
            raise DerivativeResearchError("black_scholes_requires_european_option")
        sigma = float(
            exact_decimal(volatility, "option_greeks.volatility", positive=True)
        )
        time = float(inputs.time_to_expiry_years)
        if time <= 0:
            raise DerivativeResearchError("option_greeks_expired_contract")
        spot = float(inputs.spot_price)
        strike = float(inputs.contract.strike)
        rate = float(inputs.risk_free_rate)
        dividend = float(inputs.dividend_yield)
        root_time = math.sqrt(time)
        d1 = (
            math.log(spot / strike) + (rate - dividend + sigma * sigma / 2.0) * time
        ) / (sigma * root_time)
        d2 = d1 - sigma * root_time
        discounted_spot = math.exp(-dividend * time)
        discounted_strike = math.exp(-rate * time)
        pdf = _normal_pdf(d1)
        gamma = discounted_spot * pdf / (spot * sigma * root_time)
        vega = spot * discounted_spot * pdf * root_time
        if inputs.contract.option_type is OptionType.CALL:
            delta = discounted_spot * _normal_cdf(d1)
            theta = (
                -spot * discounted_spot * pdf * sigma / (2.0 * root_time)
                - rate * strike * discounted_strike * _normal_cdf(d2)
                + dividend * spot * discounted_spot * _normal_cdf(d1)
            )
            rho = strike * time * discounted_strike * _normal_cdf(d2)
        else:
            delta = discounted_spot * (_normal_cdf(d1) - 1.0)
            theta = (
                -spot * discounted_spot * pdf * sigma / (2.0 * root_time)
                + rate * strike * discounted_strike * _normal_cdf(-d2)
                - dividend * spot * discounted_spot * _normal_cdf(-d1)
            )
            rho = -strike * time * discounted_strike * _normal_cdf(-d2)
        return OptionGreeks(
            contract_id=inputs.contract.contract_id,
            valuation_input_hash=inputs.content_hash,
            model_version=self.model_version,
            volatility=_computed_decimal(sigma),
            price=self.price(inputs, _computed_decimal(sigma)),
            delta=_computed_decimal(delta),
            gamma=_computed_decimal(gamma),
            vega=_computed_decimal(vega),
            theta_per_year=_computed_decimal(theta),
            rho=_computed_decimal(rho),
        )


@dataclass(frozen=True, slots=True)
class SurfacePoint:
    contract_id: str
    expiration_at: str
    strike: Decimal
    implied_volatility: Decimal
    valuation_input_hash: str
    iv_result_hash: str
    model_version: str

    def __post_init__(self) -> None:
        require_stable_id(self.contract_id, "option_surface.contract_id")
        parse_timestamp(self.expiration_at, "option_surface.expiration_at")
        object.__setattr__(
            self,
            "strike",
            exact_decimal(self.strike, "option_surface.strike", positive=True),
        )
        object.__setattr__(
            self,
            "implied_volatility",
            exact_decimal(
                self.implied_volatility,
                "option_surface.implied_volatility",
                positive=True,
            ),
        )
        require_hash(self.valuation_input_hash, "option_surface.valuation_input_hash")
        require_hash(self.iv_result_hash, "option_surface.iv_result_hash")
        require_stable_id(self.model_version, "option_surface.model_version")

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "expiration_at": self.expiration_at,
            "strike": decimal_text(self.strike),
            "implied_volatility": decimal_text(self.implied_volatility),
            "valuation_input_hash": self.valuation_input_hash,
            "iv_result_hash": self.iv_result_hash,
            "model_version": self.model_version,
        }


@dataclass(frozen=True, slots=True)
class VolatilitySurface:
    surface_id: str
    as_of: str
    underlying_id: str
    points: tuple[SurfacePoint, ...]
    interpolation_version: str
    source_chain_hash: str
    quality_results: tuple[QualityResult, ...] = ()
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        require_stable_id(self.surface_id, "option_surface.surface_id")
        require_stable_id(self.underlying_id, "option_surface.underlying_id")
        as_of = parse_timestamp(self.as_of, "option_surface.as_of")
        require_stable_id(
            self.interpolation_version, "option_surface.interpolation_version"
        )
        require_hash(self.source_chain_hash, "option_surface.source_chain_hash")
        if len(self.points) < 2:
            raise DerivativeResearchError("option_surface_points_insufficient")
        keys = [(item.expiration_at, item.strike) for item in self.points]
        if len(keys) != len(set(keys)):
            raise DerivativeResearchError("option_surface_point_duplicate")
        if any(
            parse_timestamp(item.expiration_at, "option_surface.expiration_at") <= as_of
            for item in self.points
        ):
            raise DerivativeResearchError("option_surface_expired_point")
        versions = {item.model_version for item in self.points}
        if len(versions) != 1:
            raise DerivativeResearchError("option_surface_model_version_mixed")
        object.__setattr__(
            self,
            "points",
            tuple(
                sorted(self.points, key=lambda item: (item.expiration_at, item.strike))
            ),
        )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_volatility_surface"),
        )

    @property
    def model_version(self) -> str:
        return self.points[0].model_version

    def _strike_interpolation(self, expiration_at: str, strike: Decimal) -> Decimal:
        rows = [item for item in self.points if item.expiration_at == expiration_at]
        if not rows:
            raise DerivativeResearchError("option_surface_expiration_missing")
        ordered = sorted(rows, key=lambda item: item.strike)
        target = exact_decimal(strike, "option_surface.target_strike", positive=True)
        exact = next((item for item in ordered if item.strike == target), None)
        if exact is not None:
            return exact.implied_volatility
        if target < ordered[0].strike or target > ordered[-1].strike:
            raise DerivativeResearchError(
                "option_surface_strike_extrapolation_forbidden"
            )
        for left, right in zip(ordered, ordered[1:], strict=False):
            if left.strike < target < right.strike:
                weight = (target - left.strike) / (right.strike - left.strike)
                return left.implied_volatility + weight * (
                    right.implied_volatility - left.implied_volatility
                )
        raise DerivativeResearchError("option_surface_strike_bracket_missing")

    def interpolate(self, *, expiration_at: str, strike: Decimal) -> Decimal:
        """Interpolate strike linearly and maturity in total variance."""

        target_time = parse_timestamp(
            expiration_at, "option_surface.target_expiration_at"
        )
        as_of = parse_timestamp(self.as_of, "option_surface.as_of")
        if target_time <= as_of:
            raise DerivativeResearchError("option_surface_target_expired")
        expiries = sorted({item.expiration_at for item in self.points})
        if expiration_at in expiries:
            return self._strike_interpolation(expiration_at, strike)
        parsed = [
            parse_timestamp(item, "option_surface.expiration_at") for item in expiries
        ]
        if target_time < parsed[0] or target_time > parsed[-1]:
            raise DerivativeResearchError("option_surface_term_extrapolation_forbidden")
        for index in range(len(parsed) - 1):
            if parsed[index] < target_time < parsed[index + 1]:
                near_name, far_name = expiries[index], expiries[index + 1]
                near_iv = self._strike_interpolation(near_name, strike)
                far_iv = self._strike_interpolation(far_name, strike)
                near_t = _time_years(self.as_of, near_name)
                far_t = _time_years(self.as_of, far_name)
                target_t = _time_years(self.as_of, expiration_at)
                near_variance = near_iv * near_iv * near_t
                far_variance = far_iv * far_iv * far_t
                weight = (target_t - near_t) / (far_t - near_t)
                variance = near_variance + weight * (far_variance - near_variance)
                if variance <= 0:
                    raise DerivativeResearchError(
                        "option_surface_interpolated_variance_invalid"
                    )
                return _computed_decimal(math.sqrt(float(variance / target_t)))
        raise DerivativeResearchError("option_surface_term_bracket_missing")

    def admit(self, run_type: RunType) -> None:
        if run_type in {RunType.CONFIRMATORY, RunType.PROSPECTIVE}:
            require_confirmatory_quality(self.quality_results)

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "surface_id": self.surface_id,
            "as_of": self.as_of,
            "underlying_id": self.underlying_id,
            "points": [item.as_dict() for item in self.points],
            "interpolation_version": self.interpolation_version,
            "source_chain_hash": self.source_chain_hash,
            "quality_results": [item.as_dict() for item in self.quality_results],
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def evaluate_volatility_surface_quality(
    surface: VolatilitySurface,
    *,
    calendar_tolerance: Decimal = Decimal("0.000001"),
) -> tuple[QualityResult, ...]:
    """Check non-decreasing total variance on directly comparable grid points."""

    tolerance = exact_decimal(
        calendar_tolerance,
        "option_surface_quality.calendar_tolerance",
        positive=True,
    )
    by_strike: dict[Decimal, list[SurfacePoint]] = {}
    for point in surface.points:
        by_strike.setdefault(point.strike, []).append(point)
    violations: list[str] = []
    for rows in by_strike.values():
        ordered = sorted(rows, key=lambda item: item.expiration_at)
        previous: Decimal | None = None
        previous_id: str | None = None
        for point in ordered:
            time = _time_years(surface.as_of, point.expiration_at)
            total_variance = point.implied_volatility**2 * time
            if previous is not None and total_variance + tolerance < previous:
                violations.extend((str(previous_id), point.contract_id))
            previous = total_variance
            previous_id = point.contract_id
    return (
        QualityResult(
            check_id="option.surface_calendar_arbitrage",
            check_version="1",
            decision=QualityDecision.FAILED if violations else QualityDecision.PASS,
            affected_ids=tuple(sorted(set(violations))),
            diagnostics=("decreasing_total_variance",) if violations else (),
        ),
    )


def option_moneyness(
    contract: OptionContract,
    *,
    spot_price: Decimal,
    forward_price: Decimal,
    method: MoneynessMethod,
) -> Decimal:
    _require_enum(method, MoneynessMethod, "option_feature.moneyness_method")
    spot = exact_decimal(spot_price, "option_feature.spot_price", positive=True)
    forward = exact_decimal(
        forward_price, "option_feature.forward_price", positive=True
    )
    if method is MoneynessMethod.STRIKE_OVER_SPOT:
        return contract.strike / spot
    if method is MoneynessMethod.STRIKE_OVER_FORWARD:
        return contract.strike / forward
    return _computed_decimal(math.log(float(contract.strike / forward)))


def volatility_skew(
    surface: VolatilitySurface,
    *,
    expiration_at: str,
    lower_strike: Decimal,
    upper_strike: Decimal,
) -> Decimal:
    lower = exact_decimal(lower_strike, "option_feature.lower_strike", positive=True)
    upper = exact_decimal(upper_strike, "option_feature.upper_strike", positive=True)
    if lower >= upper:
        raise DerivativeResearchError("option_feature_skew_strike_order_invalid")
    lower_iv = surface.interpolate(expiration_at=expiration_at, strike=lower)
    upper_iv = surface.interpolate(expiration_at=expiration_at, strike=upper)
    return (upper_iv - lower_iv) / (upper - lower)


def volatility_term_structure(
    surface: VolatilitySurface,
    *,
    strike: Decimal,
    near_expiration_at: str,
    far_expiration_at: str,
) -> Decimal:
    if parse_timestamp(
        near_expiration_at, "option_feature.near_expiration_at"
    ) >= parse_timestamp(far_expiration_at, "option_feature.far_expiration_at"):
        raise DerivativeResearchError("option_feature_term_order_invalid")
    near = surface.interpolate(expiration_at=near_expiration_at, strike=strike)
    far = surface.interpolate(expiration_at=far_expiration_at, strike=strike)
    years = _time_years(near_expiration_at, far_expiration_at)
    return (far - near) / years


def liquidity_features(quote: OptionQuote) -> dict[str, object]:
    midpoint = quote.midpoint
    spread_ratio = (
        quote.spread_width / midpoint
        if midpoint is not None and quote.spread_width is not None
        else None
    )
    return {
        "state": quote.state.value,
        "spread_width": quote.spread_width,
        "spread_ratio": spread_ratio,
        "bid_size": quote.bid_size,
        "ask_size": quote.ask_size,
        "volume": quote.volume,
        "open_interest": quote.open_interest,
        "quote_age_seconds": quote.quote_age_seconds,
    }


def put_call_parity_residual(
    *,
    call_price: Decimal,
    put_price: Decimal,
    spot_price: Decimal,
    strike: Decimal,
    risk_free_rate: Decimal,
    dividend_yield: Decimal,
    valuation_at: str,
    expiration_at: str,
) -> Decimal:
    call = exact_decimal(call_price, "option_feature.call_price", positive=True)
    put = exact_decimal(put_price, "option_feature.put_price", positive=True)
    spot = exact_decimal(spot_price, "option_feature.spot_price", positive=True)
    strike_value = exact_decimal(strike, "option_feature.strike", positive=True)
    rate = exact_decimal(risk_free_rate, "option_feature.risk_free_rate")
    dividend = exact_decimal(dividend_yield, "option_feature.dividend_yield")
    time = float(_time_years(valuation_at, expiration_at))
    fair_difference = float(spot) * math.exp(-float(dividend) * time) - float(
        strike_value
    ) * math.exp(-float(rate) * time)
    return call - put - _computed_decimal(fair_difference)


@dataclass(frozen=True, slots=True)
class OptionFeatureSnapshot:
    feature_snapshot_id: str
    contract_id: str
    feature_at: str
    moneyness_method: MoneynessMethod
    moneyness: Decimal
    skew: Decimal | None
    term_slope: Decimal | None
    parity_residual: Decimal | None
    liquidity_state: QuoteState
    spread_ratio: Decimal | None
    definition_hashes: tuple[str, ...]
    source_hashes: tuple[str, ...]
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        require_stable_id(
            self.feature_snapshot_id, "option_feature.feature_snapshot_id"
        )
        require_stable_id(self.contract_id, "option_feature.contract_id")
        parse_timestamp(self.feature_at, "option_feature.feature_at")
        _require_enum(
            self.moneyness_method,
            MoneynessMethod,
            "option_feature.moneyness_method",
        )
        _require_enum(
            self.liquidity_state, QuoteState, "option_feature.liquidity_state"
        )
        for name in (
            "moneyness",
            "skew",
            "term_slope",
            "parity_residual",
            "spread_ratio",
        ):
            value = getattr(self, name)
            parsed = _optional_decimal(value, f"option_feature.{name}")
            if name == "moneyness" and parsed is None:
                raise DerivativeResearchError("option_feature_moneyness_required")
            object.__setattr__(self, name, parsed)
        for group_name, group in (
            ("definition_hash", self.definition_hashes),
            ("source_hash", self.source_hashes),
        ):
            if not group:
                raise DerivativeResearchError(f"option_feature_{group_name}s_required")
            for value in group:
                require_hash(value, f"option_feature.{group_name}")
            if len(set(group)) != len(group):
                raise DerivativeResearchError(f"option_feature_{group_name}_duplicate")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_feature_snapshot"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "feature_snapshot_id": self.feature_snapshot_id,
            "contract_id": self.contract_id,
            "feature_at": self.feature_at,
            "moneyness_method": self.moneyness_method.value,
            "moneyness": decimal_text(self.moneyness),
            "skew": decimal_text(self.skew) if self.skew is not None else None,
            "term_slope": decimal_text(self.term_slope)
            if self.term_slope is not None
            else None,
            "parity_residual": decimal_text(self.parity_residual)
            if self.parity_residual is not None
            else None,
            "liquidity_state": self.liquidity_state.value,
            "spread_ratio": decimal_text(self.spread_ratio)
            if self.spread_ratio is not None
            else None,
            "definition_hashes": list(self.definition_hashes),
            "source_hashes": list(self.source_hashes),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class OptionFill:
    fill_id: str
    contract: OptionContract
    side: TransactionSide
    requested_quantity: Decimal
    filled_quantity: Decimal
    price: Decimal | None
    fee: Decimal
    slippage_ticks: int
    filled_at: str
    quote_hash: str
    status: FillStatus
    failure_code: str | None = None
    cash_flow: Decimal = field(init=False)
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        require_stable_id(self.fill_id, "option_fill.fill_id")
        if not isinstance(self.contract, OptionContract):
            raise DerivativeResearchError("option_fill_contract_required")
        _require_enum(self.side, TransactionSide, "option_fill.side")
        _require_enum(self.status, FillStatus, "option_fill.status")
        requested = exact_decimal(
            self.requested_quantity, "option_fill.requested_quantity", positive=True
        )
        filled = exact_decimal(self.filled_quantity, "option_fill.filled_quantity")
        fee = exact_decimal(self.fee, "option_fill.fee")
        price = _optional_decimal(self.price, "option_fill.price")
        if filled < 0 or filled > requested or fee < 0:
            raise DerivativeResearchError("option_fill_quantity_or_fee_invalid")
        if requested % self.contract.quantity_step != 0:
            raise DerivativeResearchError("option_fill_requested_quantity_step_invalid")
        if filled % self.contract.quantity_step != 0:
            raise DerivativeResearchError("option_fill_filled_quantity_step_invalid")
        if isinstance(self.slippage_ticks, bool) or self.slippage_ticks < 0:
            raise DerivativeResearchError("option_fill_slippage_ticks_invalid")
        parse_timestamp(self.filled_at, "option_fill.filled_at")
        require_hash(self.quote_hash, "option_fill.quote_hash")
        if self.status in {FillStatus.FILLED, FillStatus.PARTIAL, FillStatus.UNWOUND}:
            if price is None or price <= 0 or filled <= 0:
                raise DerivativeResearchError("option_fill_executed_fields_invalid")
        elif price is not None or filled != 0 or fee != 0:
            raise DerivativeResearchError("option_fill_unexecuted_fields_invalid")
        if self.status is FillStatus.FILLED and filled != requested:
            raise DerivativeResearchError("option_fill_full_quantity_mismatch")
        if self.status is FillStatus.PARTIAL and not (_ZERO < filled < requested):
            raise DerivativeResearchError("option_fill_partial_quantity_mismatch")
        if self.status in {FillStatus.FAILED, FillStatus.UNFILLED}:
            if self.failure_code is None:
                raise DerivativeResearchError("option_fill_failure_code_required")
            require_stable_id(self.failure_code, "option_fill.failure_code")
        elif self.failure_code is not None:
            raise DerivativeResearchError("option_fill_success_failure_code_forbidden")
        object.__setattr__(self, "requested_quantity", requested)
        object.__setattr__(self, "filled_quantity", filled)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "fee", fee)
        gross = _ZERO if price is None else price * filled * self.contract.multiplier
        cash_flow = -gross - fee if self.side is TransactionSide.BUY else gross - fee
        object.__setattr__(self, "cash_flow", cash_flow)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_fill"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "fill_id": self.fill_id,
            "contract_hash": self.contract.content_hash,
            "side": self.side.value,
            "requested_quantity": decimal_text(self.requested_quantity),
            "filled_quantity": decimal_text(self.filled_quantity),
            "price": decimal_text(self.price) if self.price is not None else None,
            "fee": decimal_text(self.fee),
            "slippage_ticks": self.slippage_ticks,
            "filled_at": self.filled_at,
            "quote_hash": self.quote_hash,
            "status": self.status.value,
            "failure_code": self.failure_code,
            "cash_flow": decimal_text(self.cash_flow),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def _failed_option_fill(
    *,
    fill_id: str,
    contract: OptionContract,
    quote: OptionQuote,
    side: TransactionSide,
    requested_quantity: Decimal,
    filled_at: str,
    status: FillStatus,
    failure_code: str,
) -> OptionFill:
    return OptionFill(
        fill_id=fill_id,
        contract=contract,
        side=side,
        requested_quantity=requested_quantity,
        filled_quantity=_ZERO,
        price=None,
        fee=_ZERO,
        slippage_ticks=0,
        filled_at=filled_at,
        quote_hash=quote.content_hash,
        status=status,
        failure_code=failure_code,
    )


def simulate_option_fill(
    *,
    fill_id: str,
    contract: OptionContract,
    quote: OptionQuote,
    side: TransactionSide,
    quantity: Decimal,
    filled_at: str,
    participation_rate: Decimal = Decimal("1"),
    fee_per_contract: Decimal = Decimal("0"),
    slippage_ticks: int = 0,
    allow_partial: bool = False,
    allow_illiquid: bool = False,
) -> OptionFill:
    """Deterministically cross the recorded option quote; never synthesize a fill."""

    _require_enum(side, TransactionSide, "option_fill.side")
    requested = exact_decimal(quantity, "option_fill.quantity", positive=True)
    participation = exact_decimal(
        participation_rate, "option_fill.participation_rate", positive=True
    )
    fee_rate = exact_decimal(fee_per_contract, "option_fill.fee_per_contract")
    if participation > 1 or fee_rate < 0:
        raise DerivativeResearchError("option_fill_participation_or_fee_invalid")
    fill_time = parse_timestamp(filled_at, "option_fill.filled_at")
    if quote.contract_id != contract.contract_id:
        raise DerivativeResearchError("option_fill_quote_contract_mismatch")
    if not quote.availability.known_at(filled_at):
        raise DerivativeResearchError("option_fill_quote_future_knowledge")
    if not contract.is_tradeable_at(filled_at):
        return _failed_option_fill(
            fill_id=fill_id,
            contract=contract,
            quote=quote,
            side=side,
            requested_quantity=requested,
            filled_at=filled_at,
            status=FillStatus.FAILED,
            failure_code="contract_not_tradeable",
        )
    if fill_time < parse_timestamp(quote.as_of, "option_quote.as_of"):
        raise DerivativeResearchError("option_fill_before_quote_as_of")
    quote_execution_age = (
        fill_time - parse_timestamp(quote.as_of, "option_quote.as_of")
    ).total_seconds()
    if quote_execution_age > quote.stale_after_seconds:
        return _failed_option_fill(
            fill_id=fill_id,
            contract=contract,
            quote=quote,
            side=side,
            requested_quantity=requested,
            filled_at=filled_at,
            status=FillStatus.FAILED,
            failure_code="quote_stale_at_fill",
        )
    try:
        base_price = quote.executable_price(side, allow_illiquid=allow_illiquid)
    except DerivativeResearchError:
        return _failed_option_fill(
            fill_id=fill_id,
            contract=contract,
            quote=quote,
            side=side,
            requested_quantity=requested,
            filled_at=filled_at,
            status=FillStatus.FAILED,
            failure_code=f"quote_{quote.state.value.lower()}",
        )
    available_size = quote.ask_size if side is TransactionSide.BUY else quote.bid_size
    capacity = available_size * participation
    capacity_steps = (capacity // contract.quantity_step) * contract.quantity_step
    filled = min(requested, capacity_steps)
    if filled <= 0 or (filled < requested and not allow_partial):
        return _failed_option_fill(
            fill_id=fill_id,
            contract=contract,
            quote=quote,
            side=side,
            requested_quantity=requested,
            filled_at=filled_at,
            status=FillStatus.UNFILLED,
            failure_code="insufficient_displayed_liquidity",
        )
    if (
        isinstance(slippage_ticks, bool)
        or not isinstance(slippage_ticks, int)
        or slippage_ticks < 0
    ):
        raise DerivativeResearchError("option_fill_slippage_ticks_invalid")
    adjustment = contract.price_tick * slippage_ticks
    execution_price = (
        base_price + adjustment
        if side is TransactionSide.BUY
        else base_price - adjustment
    )
    if execution_price <= 0:
        return _failed_option_fill(
            fill_id=fill_id,
            contract=contract,
            quote=quote,
            side=side,
            requested_quantity=requested,
            filled_at=filled_at,
            status=FillStatus.FAILED,
            failure_code="slippage_price_non_positive",
        )
    return OptionFill(
        fill_id=fill_id,
        contract=contract,
        side=side,
        requested_quantity=requested,
        filled_quantity=filled,
        price=execution_price,
        fee=fee_rate * filled,
        slippage_ticks=slippage_ticks,
        filled_at=filled_at,
        quote_hash=quote.content_hash,
        status=FillStatus.FILLED if filled == requested else FillStatus.PARTIAL,
    )


@dataclass(frozen=True, slots=True)
class OptionPosition:
    position_id: str
    contract: OptionContract
    side: PositionSide
    quantity: Decimal
    entry_price: Decimal
    entry_fee: Decimal
    opened_at: str
    source_fill_hash: str
    entry_cash_flow: Decimal = field(init=False)
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.position_id, "option_position.position_id")
        if not isinstance(self.contract, OptionContract):
            raise DerivativeResearchError("option_position_contract_required")
        _require_enum(self.side, PositionSide, "option_position.side")
        quantity = exact_decimal(
            self.quantity, "option_position.quantity", positive=True
        )
        price = exact_decimal(
            self.entry_price, "option_position.entry_price", positive=True
        )
        fee = exact_decimal(self.entry_fee, "option_position.entry_fee")
        if fee < 0 or quantity % self.contract.quantity_step != 0:
            raise DerivativeResearchError("option_position_quantity_or_fee_invalid")
        parse_timestamp(self.opened_at, "option_position.opened_at")
        require_hash(self.source_fill_hash, "option_position.source_fill_hash")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "entry_price", price)
        object.__setattr__(self, "entry_fee", fee)
        gross = price * quantity * self.contract.multiplier
        entry_cash = -gross - fee if self.side is PositionSide.LONG else gross - fee
        object.__setattr__(self, "entry_cash_flow", entry_cash)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_position"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "position_id": self.position_id,
            "contract_hash": self.contract.content_hash,
            "side": self.side.value,
            "quantity": decimal_text(self.quantity),
            "entry_price": decimal_text(self.entry_price),
            "entry_fee": decimal_text(self.entry_fee),
            "opened_at": self.opened_at,
            "source_fill_hash": self.source_fill_hash,
            "entry_cash_flow": decimal_text(self.entry_cash_flow),
        }


def position_from_fill(fill: OptionFill, *, position_id: str) -> OptionPosition:
    if fill.status not in {FillStatus.FILLED, FillStatus.PARTIAL, FillStatus.UNWOUND}:
        raise DerivativeResearchError("option_position_requires_executed_fill")
    if fill.price is None:
        raise DerivativeResearchError("option_position_fill_price_missing")
    return OptionPosition(
        position_id=position_id,
        contract=fill.contract,
        side=PositionSide.LONG
        if fill.side is TransactionSide.BUY
        else PositionSide.SHORT,
        quantity=fill.filled_quantity,
        entry_price=fill.price,
        entry_fee=fill.fee,
        opened_at=fill.filled_at,
        source_fill_hash=fill.content_hash,
    )


@dataclass(frozen=True, slots=True)
class OptionMark:
    position_id: str
    marked_at: str
    quote_hash: str
    theoretical_input_hash: str
    theoretical_price: Decimal
    liquidation_price: Decimal | None
    signed_theoretical_value: Decimal
    signed_liquidation_value: Decimal | None
    theoretical_pnl: Decimal
    liquidation_pnl: Decimal | None
    quote_state: QuoteState
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.position_id, "option_mark.position_id")
        parse_timestamp(self.marked_at, "option_mark.marked_at")
        require_hash(self.quote_hash, "option_mark.quote_hash")
        require_hash(self.theoretical_input_hash, "option_mark.theoretical_input_hash")
        _require_enum(self.quote_state, QuoteState, "option_mark.quote_state")
        for name in (
            "theoretical_price",
            "liquidation_price",
            "signed_theoretical_value",
            "signed_liquidation_value",
            "theoretical_pnl",
            "liquidation_pnl",
        ):
            value = getattr(self, name)
            parsed = _optional_decimal(value, f"option_mark.{name}")
            object.__setattr__(self, name, parsed)
        if self.theoretical_price is None or self.theoretical_price < 0:
            raise DerivativeResearchError("option_mark_theoretical_price_invalid")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_mark"),
        )

    def identity_payload(self) -> dict[str, object]:
        def text_or_none(value: Decimal | None) -> str | None:
            return decimal_text(value) if value is not None else None

        return {
            "position_id": self.position_id,
            "marked_at": self.marked_at,
            "quote_hash": self.quote_hash,
            "theoretical_input_hash": self.theoretical_input_hash,
            "theoretical_price": decimal_text(self.theoretical_price),
            "liquidation_price": text_or_none(self.liquidation_price),
            "signed_theoretical_value": decimal_text(self.signed_theoretical_value),
            "signed_liquidation_value": text_or_none(self.signed_liquidation_value),
            "theoretical_pnl": decimal_text(self.theoretical_pnl),
            "liquidation_pnl": text_or_none(self.liquidation_pnl),
            "quote_state": self.quote_state.value,
        }


def mark_option_position(
    position: OptionPosition,
    *,
    quote: OptionQuote,
    theoretical_price: Decimal,
    theoretical_input_hash: str,
    marked_at: str,
    allow_illiquid: bool = False,
) -> OptionMark:
    if quote.contract_id != position.contract.contract_id:
        raise DerivativeResearchError("option_mark_quote_contract_mismatch")
    if not quote.availability.known_at(marked_at):
        raise DerivativeResearchError("option_mark_quote_future_knowledge")
    theoretical = exact_decimal(theoretical_price, "option_mark.theoretical_price")
    if theoretical < 0:
        raise DerivativeResearchError("option_mark_theoretical_price_invalid")
    sign = _signed(position.side)
    scale = position.quantity * position.contract.multiplier
    signed_theoretical = sign * theoretical * scale
    theoretical_pnl = position.entry_cash_flow + signed_theoretical
    transaction_side = (
        TransactionSide.SELL
        if position.side is PositionSide.LONG
        else TransactionSide.BUY
    )
    try:
        liquidation = quote.executable_price(
            transaction_side, allow_illiquid=allow_illiquid
        )
    except DerivativeResearchError:
        liquidation = None
    signed_liquidation = sign * liquidation * scale if liquidation is not None else None
    liquidation_pnl = (
        position.entry_cash_flow + signed_liquidation
        if signed_liquidation is not None
        else None
    )
    return OptionMark(
        position_id=position.position_id,
        marked_at=marked_at,
        quote_hash=quote.content_hash,
        theoretical_input_hash=theoretical_input_hash,
        theoretical_price=theoretical,
        liquidation_price=liquidation,
        signed_theoretical_value=signed_theoretical,
        signed_liquidation_value=signed_liquidation,
        theoretical_pnl=theoretical_pnl,
        liquidation_pnl=liquidation_pnl,
        quote_state=quote.state,
    )


@dataclass(frozen=True, slots=True)
class OptionSettlementInput:
    """Immutable point-in-time observation used for exercise or expiry.

    A lifecycle event must not accept a caller-supplied scalar spot price.  The
    observation therefore carries all five availability clocks and the source
    manifest binding needed to prove that it belonged to the admitted dataset
    and was knowable when the event was evaluated.
    """

    settlement_input_id: str
    contract_id: str
    settlement_at: str
    availability: AvailabilityTimes
    spot_price: Decimal
    source_manifest_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(
            self.settlement_input_id, "option_settlement_input.settlement_input_id"
        )
        require_stable_id(self.contract_id, "option_settlement_input.contract_id")
        parse_timestamp(self.settlement_at, "option_settlement_input.settlement_at")
        if not isinstance(self.availability, AvailabilityTimes):
            raise DerivativeResearchError(
                "option_settlement_input_availability_required"
            )
        if self.settlement_at != self.availability.event_at:
            raise DerivativeResearchError("option_settlement_input_event_time_mismatch")
        spot = exact_decimal(
            self.spot_price, "option_settlement_input.spot_price", positive=True
        )
        object.__setattr__(self, "spot_price", spot)
        require_hash(
            self.source_manifest_hash, "option_settlement_input.source_manifest_hash"
        )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_settlement_input"),
        )

    def require_known_at(self, as_of: str) -> None:
        if not self.availability.known_at(as_of):
            raise DerivativeResearchError("option_settlement_input_future_knowledge")

    def identity_payload(self) -> dict[str, object]:
        return {
            "settlement_input_id": self.settlement_input_id,
            "contract_id": self.contract_id,
            "settlement_at": self.settlement_at,
            "availability": self.availability.as_dict(),
            "spot_price": decimal_text(self.spot_price),
            "source_manifest_hash": self.source_manifest_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class EarlyExerciseDecision:
    contract_id: str
    evaluated_at: str
    permitted: bool
    exercise: bool
    intrinsic_value: Decimal
    continuation_value: Decimal
    transaction_cost: Decimal
    reason: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.contract_id, "option_exercise.contract_id")
        parse_timestamp(self.evaluated_at, "option_exercise.evaluated_at")
        for name in ("intrinsic_value", "continuation_value", "transaction_cost"):
            value = exact_decimal(getattr(self, name), f"option_exercise.{name}")
            if value < 0:
                raise DerivativeResearchError("option_exercise_value_negative")
            object.__setattr__(self, name, value)
        require_stable_id(self.reason, "option_exercise.reason")
        if self.exercise and not self.permitted:
            raise DerivativeResearchError("option_exercise_not_permitted")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(), label="option_early_exercise_decision"
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "evaluated_at": self.evaluated_at,
            "permitted": self.permitted,
            "exercise": self.exercise,
            "intrinsic_value": decimal_text(self.intrinsic_value),
            "continuation_value": decimal_text(self.continuation_value),
            "transaction_cost": decimal_text(self.transaction_cost),
            "reason": self.reason,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def evaluate_early_exercise(
    contract: OptionContract,
    *,
    evaluated_at: str,
    spot_price: Decimal,
    continuation_value: Decimal,
    transaction_cost: Decimal = Decimal("0"),
) -> EarlyExerciseDecision:
    instant = parse_timestamp(evaluated_at, "option_exercise.evaluated_at")
    expiry = parse_timestamp(contract.expiration_at, "option_contract.expiration_at")
    spot = exact_decimal(spot_price, "option_exercise.spot_price", positive=True)
    continuation = exact_decimal(
        continuation_value, "option_exercise.continuation_value"
    )
    cost = exact_decimal(transaction_cost, "option_exercise.transaction_cost")
    if continuation < 0 or cost < 0 or instant >= expiry:
        if instant >= expiry:
            permitted = False
            reason = "expiry_settlement_required"
        else:
            raise DerivativeResearchError("option_exercise_input_negative")
    elif contract.exercise_style is ExerciseStyle.AMERICAN:
        permitted = True
        reason = "american_exercise_window"
    elif contract.exercise_style is ExerciseStyle.BERMUDAN:
        permitted = evaluated_at in contract.bermudan_exercise_at
        reason = "bermudan_exercise_date" if permitted else "outside_bermudan_window"
    else:
        permitted = False
        reason = "european_before_expiry"
    intrinsic = (
        max(_ZERO, spot - contract.strike)
        if contract.option_type is OptionType.CALL
        else max(_ZERO, contract.strike - spot)
    )
    exercise = permitted and intrinsic > continuation + cost
    if permitted and not exercise:
        reason = "continuation_value_preferred"
    elif exercise:
        reason = "intrinsic_exceeds_continuation"
    return EarlyExerciseDecision(
        contract_id=contract.contract_id,
        evaluated_at=evaluated_at,
        permitted=permitted,
        exercise=exercise,
        intrinsic_value=intrinsic,
        continuation_value=continuation,
        transaction_cost=cost,
        reason=reason,
    )


@dataclass(frozen=True, slots=True)
class OptionLifecycleEvent:
    event_id: str
    event_type: LifecycleEventType
    contract_id: str
    position_id: str
    occurred_at: str
    settlement_input: OptionSettlementInput
    exercise_fraction: Decimal
    exercised_quantity: Decimal
    intrinsic_value_per_unit: Decimal
    cash_delta: Decimal
    deliverable_quantity_delta: Decimal
    deliverable_asset_id: str | None
    source_position_hash: str
    early_exercise_decision: EarlyExerciseDecision | None = None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.event_id, "option_lifecycle.event_id")
        _require_enum(
            self.event_type, LifecycleEventType, "option_lifecycle.event_type"
        )
        require_stable_id(self.contract_id, "option_lifecycle.contract_id")
        require_stable_id(self.position_id, "option_lifecycle.position_id")
        parse_timestamp(self.occurred_at, "option_lifecycle.occurred_at")
        if not isinstance(self.settlement_input, OptionSettlementInput):
            raise DerivativeResearchError("option_lifecycle_settlement_input_required")
        if self.settlement_input.contract_id != self.contract_id:
            raise DerivativeResearchError(
                "option_lifecycle_settlement_contract_mismatch"
            )
        self.settlement_input.require_known_at(self.occurred_at)
        for name in (
            "exercise_fraction",
            "exercised_quantity",
            "intrinsic_value_per_unit",
            "cash_delta",
            "deliverable_quantity_delta",
        ):
            parsed = exact_decimal(getattr(self, name), f"option_lifecycle.{name}")
            object.__setattr__(self, name, parsed)
        if (
            self.exercise_fraction < 0
            or self.exercise_fraction > 1
            or self.exercised_quantity < 0
            or self.intrinsic_value_per_unit < 0
        ):
            raise DerivativeResearchError("option_lifecycle_value_invalid")
        if self.deliverable_asset_id is not None:
            require_stable_id(
                self.deliverable_asset_id, "option_lifecycle.deliverable_asset_id"
            )
        require_hash(self.source_position_hash, "option_lifecycle.position_hash")
        if self.early_exercise_decision is not None and not isinstance(
            self.early_exercise_decision, EarlyExerciseDecision
        ):
            raise DerivativeResearchError(
                "option_lifecycle_early_exercise_decision_invalid"
            )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_lifecycle_event"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "contract_id": self.contract_id,
            "position_id": self.position_id,
            "occurred_at": self.occurred_at,
            "settlement_input": self.settlement_input.as_dict(),
            "settlement_spot": decimal_text(self.settlement_input.spot_price),
            "exercise_fraction": decimal_text(self.exercise_fraction),
            "exercised_quantity": decimal_text(self.exercised_quantity),
            "intrinsic_value_per_unit": decimal_text(self.intrinsic_value_per_unit),
            "cash_delta": decimal_text(self.cash_delta),
            "deliverable_quantity_delta": decimal_text(self.deliverable_quantity_delta),
            "deliverable_asset_id": self.deliverable_asset_id,
            "source_position_hash": self.source_position_hash,
            "early_exercise_decision": (
                None
                if self.early_exercise_decision is None
                else self.early_exercise_decision.as_dict()
            ),
        }


def simulate_option_lifecycle(
    position: OptionPosition,
    *,
    event_id: str,
    event_at: str,
    settlement_input: OptionSettlementInput,
    exercise_fraction: Decimal = Decimal("1"),
    early_exercise_decision: EarlyExerciseDecision | None = None,
) -> OptionLifecycleEvent:
    contract = position.contract
    instant = parse_timestamp(event_at, "option_lifecycle.event_at")
    expiry = parse_timestamp(contract.expiration_at, "option_contract.expiration_at")
    if not isinstance(settlement_input, OptionSettlementInput):
        raise DerivativeResearchError("option_lifecycle_settlement_input_required")
    if settlement_input.contract_id != contract.contract_id:
        raise DerivativeResearchError("option_lifecycle_settlement_contract_mismatch")
    settlement_instant = parse_timestamp(
        settlement_input.settlement_at, "option_settlement_input.settlement_at"
    )
    if settlement_instant > instant:
        raise DerivativeResearchError("option_lifecycle_settlement_after_event")
    settlement_input.require_known_at(event_at)
    spot = settlement_input.spot_price
    fraction = exact_decimal(exercise_fraction, "option_lifecycle.exercise_fraction")
    if fraction < 0 or fraction > 1:
        raise DerivativeResearchError("option_lifecycle_exercise_fraction_invalid")
    if instant < expiry:
        if settlement_instant != instant:
            raise DerivativeResearchError(
                "option_lifecycle_early_settlement_time_mismatch"
            )
        if early_exercise_decision is None or not early_exercise_decision.exercise:
            raise DerivativeResearchError(
                "option_lifecycle_early_exercise_not_approved"
            )
        if early_exercise_decision.contract_id != contract.contract_id:
            raise DerivativeResearchError("option_lifecycle_exercise_contract_mismatch")
        if early_exercise_decision.evaluated_at != event_at:
            raise DerivativeResearchError("option_lifecycle_exercise_time_mismatch")
        expected_decision = evaluate_early_exercise(
            contract,
            evaluated_at=event_at,
            spot_price=spot,
            continuation_value=early_exercise_decision.continuation_value,
            transaction_cost=early_exercise_decision.transaction_cost,
        )
        if early_exercise_decision != expected_decision:
            raise DerivativeResearchError("option_lifecycle_exercise_decision_forged")
    elif early_exercise_decision is not None:
        raise DerivativeResearchError("option_lifecycle_expiry_decision_forbidden")
    else:
        scheduled_settlement = parse_timestamp(
            contract.settlement_at, "option_contract.settlement_at"
        )
        if not expiry <= settlement_instant <= scheduled_settlement:
            raise DerivativeResearchError(
                "option_lifecycle_expiry_settlement_time_invalid"
            )
    intrinsic = (
        max(_ZERO, spot - contract.strike)
        if contract.option_type is OptionType.CALL
        else max(_ZERO, contract.strike - spot)
    )
    exercised = position.quantity * fraction if intrinsic > 0 else _ZERO
    position_sign = _signed(position.side)
    event_type = (
        LifecycleEventType.EXERCISE
        if instant < expiry and position.side is PositionSide.LONG
        else LifecycleEventType.ASSIGNMENT
        if instant < expiry
        else LifecycleEventType.EXPIRY
    )
    cash_delta = _ZERO
    deliverable_delta = _ZERO
    deliverable_id: str | None = None
    if exercised > 0:
        scale = exercised * contract.multiplier
        if contract.settlement_type is SettlementType.CASH:
            cash_delta = position_sign * intrinsic * scale
        else:
            deliverable_id = contract.deliverable_asset_id
            if contract.option_type is OptionType.CALL:
                deliverable_delta = position_sign * scale
                cash_delta = -position_sign * contract.strike * scale
            else:
                deliverable_delta = -position_sign * scale
                cash_delta = position_sign * contract.strike * scale
    return OptionLifecycleEvent(
        event_id=event_id,
        event_type=event_type,
        contract_id=contract.contract_id,
        position_id=position.position_id,
        occurred_at=event_at,
        settlement_input=settlement_input,
        exercise_fraction=fraction,
        exercised_quantity=exercised,
        intrinsic_value_per_unit=intrinsic,
        cash_delta=cash_delta,
        deliverable_quantity_delta=deliverable_delta,
        deliverable_asset_id=deliverable_id,
        source_position_hash=position.content_hash,
        early_exercise_decision=early_exercise_decision,
    )


@dataclass(frozen=True, slots=True)
class OptionLeg:
    leg_id: str
    contract: OptionContract
    side: PositionSide
    quantity: Decimal

    def __post_init__(self) -> None:
        require_stable_id(self.leg_id, "option_leg.leg_id")
        if not isinstance(self.contract, OptionContract):
            raise DerivativeResearchError("option_leg_contract_required")
        _require_enum(self.side, PositionSide, "option_leg.side")
        quantity = exact_decimal(self.quantity, "option_leg.quantity", positive=True)
        if quantity % self.contract.quantity_step != 0:
            raise DerivativeResearchError("option_leg_quantity_step_invalid")
        object.__setattr__(self, "quantity", quantity)

    @property
    def transaction_side(self) -> TransactionSide:
        return (
            TransactionSide.BUY
            if self.side is PositionSide.LONG
            else TransactionSide.SELL
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "leg_id": self.leg_id,
            "contract_hash": self.contract.content_hash,
            "side": self.side.value,
            "quantity": decimal_text(self.quantity),
        }


@dataclass(frozen=True, slots=True)
class MultiLegOrder:
    group_id: str
    legs: tuple[OptionLeg, ...]
    policy: MultiLegExecutionPolicy
    requested_at: str
    maximum_leg_time_skew_seconds: int
    allow_partial: bool
    execution_policy_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.group_id, "option_multileg.group_id")
        _require_enum(self.policy, MultiLegExecutionPolicy, "option_multileg.policy")
        parse_timestamp(self.requested_at, "option_multileg.requested_at")
        if len(self.legs) < 2:
            raise DerivativeResearchError("option_multileg_requires_two_legs")
        leg_ids = [leg.leg_id for leg in self.legs]
        if len(leg_ids) != len(set(leg_ids)):
            raise DerivativeResearchError("option_multileg_leg_id_duplicate")
        if (
            isinstance(self.maximum_leg_time_skew_seconds, bool)
            or self.maximum_leg_time_skew_seconds < 0
        ):
            raise DerivativeResearchError("option_multileg_time_skew_invalid")
        require_hash(self.execution_policy_hash, "option_multileg.policy_hash")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_multileg_order"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "legs": [leg.as_dict() for leg in self.legs],
            "policy": self.policy.value,
            "requested_at": self.requested_at,
            "maximum_leg_time_skew_seconds": self.maximum_leg_time_skew_seconds,
            "allow_partial": self.allow_partial,
            "execution_policy_hash": self.execution_policy_hash,
        }


@dataclass(frozen=True, slots=True)
class MultiLegExecutionResult:
    group_id: str
    order_hash: str
    policy: MultiLegExecutionPolicy
    state: MultiLegState
    attempted_fills: tuple[OptionFill, ...]
    committed_fills: tuple[OptionFill, ...]
    legging_exposure_contract_ids: tuple[str, ...]
    net_cash_flow: Decimal
    opened_at: str
    finished_at: str
    failure_code: str | None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.group_id, "option_multileg_result.group_id")
        require_hash(self.order_hash, "option_multileg_result.order_hash")
        _require_enum(
            self.policy, MultiLegExecutionPolicy, "option_multileg_result.policy"
        )
        _require_enum(self.state, MultiLegState, "option_multileg_result.state")
        opened = parse_timestamp(self.opened_at, "option_multileg_result.opened_at")
        finished = parse_timestamp(
            self.finished_at, "option_multileg_result.finished_at"
        )
        if finished < opened:
            raise DerivativeResearchError("option_multileg_result_time_order_invalid")
        net = exact_decimal(self.net_cash_flow, "option_multileg_result.net_cash_flow")
        if net != sum((fill.cash_flow for fill in self.committed_fills), _ZERO):
            raise DerivativeResearchError("option_multileg_result_cash_flow_mismatch")
        object.__setattr__(self, "net_cash_flow", net)
        if len(self.legging_exposure_contract_ids) != len(
            set(self.legging_exposure_contract_ids)
        ):
            raise DerivativeResearchError("option_multileg_legging_exposure_duplicate")
        if self.state is MultiLegState.FAILED and self.failure_code is None:
            raise DerivativeResearchError("option_multileg_failure_code_required")
        if self.failure_code is not None:
            require_stable_id(self.failure_code, "option_multileg_result.failure_code")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_multileg_result"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "order_hash": self.order_hash,
            "policy": self.policy.value,
            "state": self.state.value,
            "attempted_fill_hashes": [
                item.content_hash for item in self.attempted_fills
            ],
            "committed_fill_hashes": [
                item.content_hash for item in self.committed_fills
            ],
            "legging_exposure_contract_ids": list(self.legging_exposure_contract_ids),
            "net_cash_flow": decimal_text(self.net_cash_flow),
            "opened_at": self.opened_at,
            "finished_at": self.finished_at,
            "failure_code": self.failure_code,
        }


def execute_multi_leg_order(
    order: MultiLegOrder,
    *,
    quotes: Mapping[str, OptionQuote],
    fill_times: Mapping[str, str],
    participation_rates: Mapping[str, Decimal] | None = None,
    fee_per_contract: Decimal = Decimal("0"),
    slippage_ticks: int = 0,
    allow_illiquid: bool = False,
) -> MultiLegExecutionResult:
    """Execute an atomic simultaneous group or an explicitly legged sequence."""

    participation = participation_rates or {}
    attempts: list[OptionFill] = []
    for leg in order.legs:
        quote = quotes.get(leg.contract.contract_id)
        fill_at = fill_times.get(leg.leg_id)
        if quote is None or fill_at is None:
            raise DerivativeResearchError("option_multileg_quote_or_fill_time_missing")
        attempts.append(
            simulate_option_fill(
                fill_id=f"{order.group_id}.{leg.leg_id}",
                contract=leg.contract,
                quote=quote,
                side=leg.transaction_side,
                quantity=leg.quantity,
                filled_at=fill_at,
                participation_rate=participation.get(leg.leg_id, _ONE),
                fee_per_contract=fee_per_contract,
                slippage_ticks=slippage_ticks,
                allow_partial=order.allow_partial,
                allow_illiquid=allow_illiquid,
            )
        )
    timestamps = [
        parse_timestamp(item.filled_at, "option_multileg.fill_at") for item in attempts
    ]
    time_skew = (max(timestamps) - min(timestamps)).total_seconds()
    fully_filled = all(item.status is FillStatus.FILLED for item in attempts)
    executed = tuple(
        item
        for item in attempts
        if item.status in {FillStatus.FILLED, FillStatus.PARTIAL}
    )
    legging: tuple[str, ...]
    if order.policy is MultiLegExecutionPolicy.SIMULTANEOUS:
        committed = (
            tuple(attempts)
            if fully_filled and time_skew <= order.maximum_leg_time_skew_seconds
            else ()
        )
        state = MultiLegState.FILLED if committed else MultiLegState.FAILED
        failure = (
            None
            if committed
            else (
                "simultaneous_time_skew"
                if time_skew > order.maximum_leg_time_skew_seconds
                else "simultaneous_atomic_fill_failed"
            )
        )
        legging = ()
    else:
        committed = executed
        state = (
            MultiLegState.FILLED
            if fully_filled
            else MultiLegState.PARTIAL
            if committed
            else MultiLegState.FAILED
        )
        failure = (
            "sequential_no_leg_filled"
            if not committed
            else "sequential_partial_fill_forbidden"
            if not fully_filled and not order.allow_partial
            else None
        )
        legging = (
            ()
            if fully_filled
            else tuple(item.contract.contract_id for item in committed)
        )
    return MultiLegExecutionResult(
        group_id=order.group_id,
        order_hash=order.content_hash,
        policy=order.policy,
        state=state,
        attempted_fills=tuple(attempts),
        committed_fills=committed,
        legging_exposure_contract_ids=legging,
        net_cash_flow=sum((item.cash_flow for item in committed), _ZERO),
        opened_at=min(timestamps).isoformat(),
        finished_at=max(timestamps).isoformat(),
        failure_code=failure,
    )


def unwind_multi_leg_execution(
    result: MultiLegExecutionResult,
    *,
    unwind_group_id: str,
    quotes: Mapping[str, OptionQuote],
    filled_at: str,
    fee_per_contract: Decimal = Decimal("0"),
) -> MultiLegExecutionResult:
    attempts: list[OptionFill] = []
    for index, original in enumerate(reversed(result.committed_fills)):
        attempts.append(
            simulate_option_fill(
                fill_id=f"{unwind_group_id}.leg{index}",
                contract=original.contract,
                quote=quotes[original.contract.contract_id],
                side=TransactionSide.SELL
                if original.side is TransactionSide.BUY
                else TransactionSide.BUY,
                quantity=original.filled_quantity,
                filled_at=filled_at,
                fee_per_contract=fee_per_contract,
                allow_partial=False,
            )
        )
    complete = bool(attempts) and all(
        item.status is FillStatus.FILLED for item in attempts
    )
    committed = tuple(attempts) if complete else ()
    return MultiLegExecutionResult(
        group_id=unwind_group_id,
        order_hash=result.order_hash,
        policy=MultiLegExecutionPolicy.SEQUENTIAL,
        state=MultiLegState.UNWOUND if complete else MultiLegState.FAILED,
        attempted_fills=tuple(attempts),
        committed_fills=committed,
        legging_exposure_contract_ids=()
        if complete
        else tuple(item.contract.contract_id for item in result.committed_fills),
        net_cash_flow=sum((item.cash_flow for item in committed), _ZERO),
        opened_at=filled_at,
        finished_at=filled_at,
        failure_code=None if complete else "multileg_unwind_failed",
    )


@dataclass(frozen=True, slots=True)
class NetOptionGreeks:
    delta: Decimal
    gamma: Decimal
    vega: Decimal
    theta_per_year: Decimal
    rho: Decimal
    expiry_mismatch: bool


def net_option_greeks(
    positions: Sequence[OptionPosition],
    greeks_by_contract: Mapping[str, OptionGreeks],
) -> NetOptionGreeks:
    if not positions:
        raise DerivativeResearchError("option_net_greeks_positions_required")

    def aggregate(name: str) -> Decimal:
        total = _ZERO
        for position in positions:
            greek = greeks_by_contract.get(position.contract.contract_id)
            if greek is None:
                raise DerivativeResearchError("option_net_greeks_contract_missing")
            total += (
                _signed(position.side)
                * position.quantity
                * position.contract.multiplier
                * getattr(greek, name)
            )
        return total

    return NetOptionGreeks(
        delta=aggregate("delta"),
        gamma=aggregate("gamma"),
        vega=aggregate("vega"),
        theta_per_year=aggregate("theta_per_year"),
        rho=aggregate("rho"),
        expiry_mismatch=len({position.contract.expiration_at for position in positions})
        > 1,
    )


@dataclass(frozen=True, slots=True)
class PayoffPoint:
    underlying_price: Decimal
    profit_loss: Decimal


@dataclass(frozen=True, slots=True)
class OptionPayoffAnalysis:
    points: tuple[PayoffPoint, ...]
    break_even_estimates: tuple[Decimal, ...]
    maximum_profit: Decimal | None
    maximum_loss: Decimal | None
    unbounded_profit: bool
    unbounded_loss: bool
    expiry_mismatch: bool


def option_expiry_payoff(
    positions: Sequence[OptionPosition], terminal_spot: Decimal
) -> Decimal:
    spot = exact_decimal(terminal_spot, "option_payoff.terminal_spot")
    if spot < 0:
        raise DerivativeResearchError("option_payoff_terminal_spot_negative")
    payoff = sum((position.entry_cash_flow for position in positions), _ZERO)
    for position in positions:
        intrinsic = (
            max(_ZERO, spot - position.contract.strike)
            if position.contract.option_type is OptionType.CALL
            else max(_ZERO, position.contract.strike - spot)
        )
        payoff += (
            _signed(position.side)
            * intrinsic
            * position.quantity
            * position.contract.multiplier
        )
    return payoff


def analyze_option_payoff(
    positions: Sequence[OptionPosition], *, scenario_spots: Sequence[Decimal]
) -> OptionPayoffAnalysis:
    if not positions or len(scenario_spots) < 2:
        raise DerivativeResearchError("option_payoff_positions_and_scenarios_required")
    spots = sorted(
        {exact_decimal(item, "option_payoff.scenario_spot") for item in scenario_spots}
    )
    if spots[0] < 0:
        raise DerivativeResearchError("option_payoff_terminal_spot_negative")
    points = tuple(
        PayoffPoint(item, option_expiry_payoff(positions, item)) for item in spots
    )
    break_evens: list[Decimal] = []
    for left, right in zip(points, points[1:], strict=False):
        if left.profit_loss == 0:
            break_evens.append(left.underlying_price)
        elif left.profit_loss * right.profit_loss < 0:
            span = right.underlying_price - left.underlying_price
            weight = -left.profit_loss / (right.profit_loss - left.profit_loss)
            break_evens.append(left.underlying_price + span * weight)
    high_tail_slope = sum(
        (
            _signed(position.side) * position.quantity * position.contract.multiplier
            for position in positions
            if position.contract.option_type is OptionType.CALL
        ),
        _ZERO,
    )
    unbounded_profit = high_tail_slope > 0
    unbounded_loss = high_tail_slope < 0
    profits = [item.profit_loss for item in points]
    return OptionPayoffAnalysis(
        points=points,
        break_even_estimates=tuple(break_evens),
        maximum_profit=None if unbounded_profit else max(profits),
        maximum_loss=None if unbounded_loss else min(profits),
        unbounded_profit=unbounded_profit,
        unbounded_loss=unbounded_loss,
        expiry_mismatch=len({item.contract.expiration_at for item in positions}) > 1,
    )


@dataclass(frozen=True, slots=True)
class OptionCapitalRequirement:
    stressed_capital: Decimal
    worst_scenario_spot: Decimal
    unbounded_tail: bool


def option_capital_requirement(
    positions: Sequence[OptionPosition],
    *,
    reference_spot: Decimal,
    upside_stress_multiple: Decimal = Decimal("3"),
) -> OptionCapitalRequirement:
    reference = exact_decimal(
        reference_spot, "option_capital.reference_spot", positive=True
    )
    multiple = exact_decimal(
        upside_stress_multiple,
        "option_capital.upside_stress_multiple",
        positive=True,
    )
    scenario_spots = sorted(
        {
            _ZERO,
            reference,
            reference * multiple,
            *(item.contract.strike for item in positions),
        }
    )
    losses = [(spot, option_expiry_payoff(positions, spot)) for spot in scenario_spots]
    worst_spot, worst_pnl = min(losses, key=lambda item: item[1])
    unbounded = (
        sum(
            (
                _signed(item.side) * item.quantity * item.contract.multiplier
                for item in positions
                if item.contract.option_type is OptionType.CALL
            ),
            _ZERO,
        )
        < 0
    )
    return OptionCapitalRequirement(
        stressed_capital=max(_ZERO, -worst_pnl),
        worst_scenario_spot=worst_spot,
        unbounded_tail=unbounded,
    )


@dataclass(frozen=True, slots=True)
class OptionStressScenario:
    scenario_id: str
    spot_shock_ratio: Decimal
    volatility_shock: Decimal
    rate_shock: Decimal
    dividend_yield_shock: Decimal
    liquidity_spread_multiplier: Decimal
    days_forward: int
    scenario_policy_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.scenario_id, "option_stress.scenario_id")
        for name in (
            "spot_shock_ratio",
            "volatility_shock",
            "rate_shock",
            "dividend_yield_shock",
            "liquidity_spread_multiplier",
        ):
            parsed = exact_decimal(getattr(self, name), f"option_stress.{name}")
            object.__setattr__(self, name, parsed)
        if self.spot_shock_ratio <= -1 or self.liquidity_spread_multiplier <= 0:
            raise DerivativeResearchError("option_stress_shock_invalid")
        if isinstance(self.days_forward, bool) or self.days_forward < 0:
            raise DerivativeResearchError("option_stress_days_forward_invalid")
        require_hash(self.scenario_policy_hash, "option_stress.policy_hash")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_stress_scenario"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "spot_shock_ratio": decimal_text(self.spot_shock_ratio),
            "volatility_shock": decimal_text(self.volatility_shock),
            "rate_shock": decimal_text(self.rate_shock),
            "dividend_yield_shock": decimal_text(self.dividend_yield_shock),
            "liquidity_spread_multiplier": decimal_text(
                self.liquidity_spread_multiplier
            ),
            "days_forward": self.days_forward,
            "scenario_policy_hash": self.scenario_policy_hash,
        }


@dataclass(frozen=True, slots=True)
class OptionStressLegResult:
    position_id: str
    base_signed_value: Decimal
    stressed_signed_value: Decimal
    stressed_liquidation_value: Decimal
    profit_loss_change: Decimal


@dataclass(frozen=True, slots=True)
class OptionStressResult:
    scenario_hash: str
    position_hashes: tuple[str, ...]
    input_hashes: tuple[str, ...]
    leg_results: tuple[OptionStressLegResult, ...]
    total_base_value: Decimal
    total_stressed_value: Decimal
    total_stressed_liquidation_value: Decimal
    total_profit_loss_change: Decimal
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_hash(self.scenario_hash, "option_stress_result.scenario_hash")
        for value in (*self.position_hashes, *self.input_hashes):
            require_hash(value, "option_stress_result.evidence_hash")
        if not self.leg_results:
            raise DerivativeResearchError("option_stress_result_legs_required")
        for name in (
            "total_base_value",
            "total_stressed_value",
            "total_stressed_liquidation_value",
            "total_profit_loss_change",
        ):
            object.__setattr__(
                self,
                name,
                exact_decimal(getattr(self, name), f"option_stress_result.{name}"),
            )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "scenario_hash": self.scenario_hash,
                    "position_hashes": list(self.position_hashes),
                    "input_hashes": list(self.input_hashes),
                    "leg_results": [
                        {
                            "position_id": item.position_id,
                            "base_signed_value": decimal_text(item.base_signed_value),
                            "stressed_signed_value": decimal_text(
                                item.stressed_signed_value
                            ),
                            "stressed_liquidation_value": decimal_text(
                                item.stressed_liquidation_value
                            ),
                            "profit_loss_change": decimal_text(item.profit_loss_change),
                        }
                        for item in self.leg_results
                    ],
                },
                label="option_stress_result",
            ),
        )


def stress_option_portfolio(
    positions: Sequence[OptionPosition],
    *,
    inputs_by_contract: Mapping[str, ValuationInputSnapshot],
    volatility_by_contract: Mapping[str, Decimal],
    scenario: OptionStressScenario,
    model: BlackScholesModel | None = None,
) -> OptionStressResult:
    if not positions:
        raise DerivativeResearchError("option_stress_positions_required")
    pricing_model = model or BlackScholesModel()
    legs: list[OptionStressLegResult] = []
    input_hashes: list[str] = []
    for position in positions:
        contract_id = position.contract.contract_id
        inputs = inputs_by_contract.get(contract_id)
        volatility = volatility_by_contract.get(contract_id)
        if inputs is None or volatility is None:
            raise DerivativeResearchError("option_stress_input_missing")
        if inputs.contract.content_hash != position.contract.content_hash:
            raise DerivativeResearchError("option_stress_contract_mismatch")
        base_vol = exact_decimal(
            volatility, "option_stress.base_volatility", positive=True
        )
        stressed_vol = base_vol + scenario.volatility_shock
        if stressed_vol <= 0:
            raise DerivativeResearchError("option_stress_volatility_non_positive")
        base_price = pricing_model.price(inputs, base_vol)
        time = max(
            0.0,
            float(inputs.time_to_expiry_years) - scenario.days_forward / 365.25,
        )
        spot = float(inputs.spot_price * (_ONE + scenario.spot_shock_ratio))
        strike = float(position.contract.strike)
        rate = float(inputs.risk_free_rate + scenario.rate_shock)
        dividend = float(inputs.dividend_yield + scenario.dividend_yield_shock)
        sigma = float(stressed_vol)
        if time == 0:
            stressed_price_float = (
                max(spot - strike, 0.0)
                if position.contract.option_type is OptionType.CALL
                else max(strike - spot, 0.0)
            )
        else:
            root_time = math.sqrt(time)
            d1 = (
                math.log(spot / strike) + (rate - dividend + sigma * sigma / 2.0) * time
            ) / (sigma * root_time)
            d2 = d1 - sigma * root_time
            if position.contract.option_type is OptionType.CALL:
                stressed_price_float = spot * math.exp(-dividend * time) * _normal_cdf(
                    d1
                ) - strike * math.exp(-rate * time) * _normal_cdf(d2)
            else:
                stressed_price_float = strike * math.exp(-rate * time) * _normal_cdf(
                    -d2
                ) - spot * math.exp(-dividend * time) * _normal_cdf(-d1)
        stressed_price = _computed_decimal(stressed_price_float)
        spread = inputs.quote.spread_width or _ZERO
        liquidity_penalty = spread * scenario.liquidity_spread_multiplier / Decimal("2")
        liquidation_price = (
            max(_ZERO, stressed_price - liquidity_penalty)
            if position.side is PositionSide.LONG
            else stressed_price + liquidity_penalty
        )
        scale = position.quantity * position.contract.multiplier
        sign = _signed(position.side)
        base_value = sign * base_price * scale
        stressed_value = sign * stressed_price * scale
        stressed_liquidation = sign * liquidation_price * scale
        legs.append(
            OptionStressLegResult(
                position_id=position.position_id,
                base_signed_value=base_value,
                stressed_signed_value=stressed_value,
                stressed_liquidation_value=stressed_liquidation,
                profit_loss_change=stressed_liquidation - base_value,
            )
        )
        input_hashes.append(inputs.content_hash)
    base_total = sum((item.base_signed_value for item in legs), _ZERO)
    stressed_total = sum((item.stressed_signed_value for item in legs), _ZERO)
    liquidation_total = sum((item.stressed_liquidation_value for item in legs), _ZERO)
    return OptionStressResult(
        scenario_hash=scenario.content_hash,
        position_hashes=tuple(item.content_hash for item in positions),
        input_hashes=tuple(input_hashes),
        leg_results=tuple(legs),
        total_base_value=base_total,
        total_stressed_value=stressed_total,
        total_stressed_liquidation_value=liquidation_total,
        total_profit_loss_change=liquidation_total - base_total,
    )


class OptionRobustnessDimension(StrEnum):
    """The complete option robustness axis from Research Semantics v2."""

    BID_ASK_COST = "S5-O01"
    MIDPOINT_COMPARISON = "S5-O02"
    STALE_QUOTE_FILTER = "S5-O03"
    LIQUIDITY_FILTER = "S5-O04"
    IV_MODEL = "S5-O05"
    RATE_DIVIDEND = "S5-O06"
    SURFACE_INTERPOLATION = "S5-O07"
    CHAIN_SELECTION = "S5-O08"
    EXPIRY_CONCENTRATION = "S5-O09"
    STRIKE_CONCENTRATION = "S5-O10"
    EXTREME_VOLATILITY = "S5-O11"
    VOLATILITY_SHOCK = "S5-O12"
    SKEW_SHIFT = "S5-O13"
    SPOT_IV_GAP = "S5-O14"
    EXERCISE_ASSIGNMENT = "S5-O15"
    EXPIRY_LIQUIDITY_LOSS = "S5-O16"
    ZERO_BID_LIQUIDATION = "S5-O17"
    MULTILEG_PARTIAL_FILL = "S5-O18"
    PAYOFF_TAIL_RISK = "S5-O19"
    SHORT_RARE_LOSS = "S5-O20"


@dataclass(frozen=True, slots=True)
class OptionRobustnessPolicy:
    """Frozen parameters used by every case in one option robustness suite."""

    policy_id: str
    spread_multipliers: tuple[Decimal, ...] = (
        Decimal("1"),
        Decimal("1.5"),
        Decimal("2"),
    )
    stale_cutoffs_seconds: tuple[int, ...] = (5, 60, 300)
    volume_thresholds: tuple[int, ...] = (0, 100, 1000)
    open_interest_thresholds: tuple[int, ...] = (0, 500, 5000)
    spread_ratio_thresholds: tuple[Decimal, ...] = (
        Decimal("0.05"),
        Decimal("0.25"),
        Decimal("0.50"),
    )
    quote_age_thresholds: tuple[int, ...] = (5, 60, 300)
    rate_shocks: tuple[Decimal, ...] = (Decimal("-0.01"), Decimal("0.01"))
    dividend_yield_shocks: tuple[Decimal, ...] = (
        Decimal("-0.01"),
        Decimal("0.01"),
    )
    extreme_volatilities: tuple[Decimal, ...] = (
        Decimal("0.05"),
        Decimal("1.50"),
    )
    volatility_shocks: tuple[Decimal, ...] = (
        Decimal("-0.10"),
        Decimal("0.50"),
    )
    skew_shift: Decimal = Decimal("0.50")
    spot_gap_ratio: Decimal = Decimal("-0.30")
    iv_gap: Decimal = Decimal("0.50")
    expiry_spread_multiplier: Decimal = Decimal("10")
    delta_target: Decimal = Decimal("0.50")
    moneyness_tolerance: Decimal = Decimal("0.10")
    rare_event_probability: Decimal = Decimal("0.01")
    zero_bid_recovery_ratio: Decimal = Decimal("0")
    partial_fill_haircut: Decimal = Decimal("0.25")
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.policy_id, "option_robustness_policy.policy_id")
        decimal_sequences = (
            "spread_multipliers",
            "spread_ratio_thresholds",
            "rate_shocks",
            "dividend_yield_shocks",
            "extreme_volatilities",
            "volatility_shocks",
        )
        for name in decimal_sequences:
            values = tuple(
                exact_decimal(item, f"option_robustness_policy.{name}")
                for item in getattr(self, name)
            )
            if not values or len(values) != len(set(values)):
                raise DerivativeResearchError(
                    f"option_robustness_policy_{name}_invalid"
                )
            object.__setattr__(self, name, values)
        integer_sequences = (
            "stale_cutoffs_seconds",
            "volume_thresholds",
            "open_interest_thresholds",
            "quote_age_thresholds",
        )
        for name in integer_sequences:
            values = getattr(self, name)
            if (
                not values
                or len(values) != len(set(values))
                or any(
                    isinstance(item, bool) or not isinstance(item, int) or item < 0
                    for item in values
                )
            ):
                raise DerivativeResearchError(
                    f"option_robustness_policy_{name}_invalid"
                )
        scalar_names = (
            "skew_shift",
            "spot_gap_ratio",
            "iv_gap",
            "expiry_spread_multiplier",
            "delta_target",
            "moneyness_tolerance",
            "rare_event_probability",
            "zero_bid_recovery_ratio",
            "partial_fill_haircut",
        )
        for name in scalar_names:
            object.__setattr__(
                self,
                name,
                exact_decimal(getattr(self, name), f"option_robustness_policy.{name}"),
            )
        if set(self.spread_multipliers) != {
            Decimal("1"),
            Decimal("1.5"),
            Decimal("2"),
        }:
            raise DerivativeResearchError(
                "option_robustness_policy_spread_grid_required"
            )
        if (
            any(item <= 0 for item in self.spread_multipliers)
            or any(item <= 0 for item in self.spread_ratio_thresholds)
            or any(item <= 0 for item in self.extreme_volatilities)
            or not any(item < 0 for item in self.volatility_shocks)
            or not any(item > 0 for item in self.volatility_shocks)
            or not any(item < 0 for item in self.rate_shocks)
            or not any(item > 0 for item in self.rate_shocks)
            or not any(item < 0 for item in self.dividend_yield_shocks)
            or not any(item > 0 for item in self.dividend_yield_shocks)
            or self.skew_shift == 0
            or self.spot_gap_ratio <= -1
            or self.iv_gap == 0
            or self.expiry_spread_multiplier <= 1
            or not _ZERO < self.delta_target < _ONE
            or not _ZERO < self.moneyness_tolerance < _ONE
            or not _ZERO < self.rare_event_probability < _ONE
            or not _ZERO <= self.zero_bid_recovery_ratio <= _ONE
            or not _ZERO <= self.partial_fill_haircut <= _ONE
        ):
            raise DerivativeResearchError("option_robustness_policy_shock_invalid")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_robustness_policy"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "spread_multipliers": [
                decimal_text(item) for item in self.spread_multipliers
            ],
            "stale_cutoffs_seconds": list(self.stale_cutoffs_seconds),
            "volume_thresholds": list(self.volume_thresholds),
            "open_interest_thresholds": list(self.open_interest_thresholds),
            "spread_ratio_thresholds": [
                decimal_text(item) for item in self.spread_ratio_thresholds
            ],
            "quote_age_thresholds": list(self.quote_age_thresholds),
            "rate_shocks": [decimal_text(item) for item in self.rate_shocks],
            "dividend_yield_shocks": [
                decimal_text(item) for item in self.dividend_yield_shocks
            ],
            "extreme_volatilities": [
                decimal_text(item) for item in self.extreme_volatilities
            ],
            "volatility_shocks": [
                decimal_text(item) for item in self.volatility_shocks
            ],
            "skew_shift": decimal_text(self.skew_shift),
            "spot_gap_ratio": decimal_text(self.spot_gap_ratio),
            "iv_gap": decimal_text(self.iv_gap),
            "expiry_spread_multiplier": decimal_text(self.expiry_spread_multiplier),
            "delta_target": decimal_text(self.delta_target),
            "moneyness_tolerance": decimal_text(self.moneyness_tolerance),
            "rare_event_probability": decimal_text(self.rare_event_probability),
            "zero_bid_recovery_ratio": decimal_text(self.zero_bid_recovery_ratio),
            "partial_fill_haircut": decimal_text(self.partial_fill_haircut),
        }


@dataclass(frozen=True, slots=True)
class OptionRobustnessCase:
    case_id: str
    dimension: OptionRobustnessDimension
    policy: OptionRobustnessPolicy
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.case_id, "option_robustness_case.case_id")
        _require_enum(
            self.dimension,
            OptionRobustnessDimension,
            "option_robustness_case.dimension",
        )
        if not isinstance(self.policy, OptionRobustnessPolicy):
            raise DerivativeResearchError("option_robustness_case_policy_required")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_robustness_case"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "dimension": self.dimension.value,
            "policy": self.policy.identity_payload(),
            "policy_hash": self.policy.content_hash,
        }


def standard_option_robustness_cases(
    policy: OptionRobustnessPolicy,
) -> tuple[OptionRobustnessCase, ...]:
    """Create exactly one deterministic executable case for every S5-O axis."""

    return tuple(
        OptionRobustnessCase(
            case_id=f"option_robustness.{dimension.value}",
            dimension=dimension,
            policy=policy,
        )
        for dimension in OptionRobustnessDimension
    )


def _position_payload(position: OptionPosition) -> dict[str, object]:
    return {**position.identity_payload(), "content_hash": position.content_hash}


def _mark_payload(mark: OptionMark) -> dict[str, object]:
    return {**mark.identity_payload(), "content_hash": mark.content_hash}


def _lifecycle_payload(event: OptionLifecycleEvent) -> dict[str, object]:
    return {**event.identity_payload(), "content_hash": event.content_hash}


def _order_payload(order: MultiLegOrder) -> dict[str, object]:
    return {**order.identity_payload(), "content_hash": order.content_hash}


def _multileg_payload(result: MultiLegExecutionResult) -> dict[str, object]:
    return {**result.identity_payload(), "content_hash": result.content_hash}


@dataclass(frozen=True, slots=True)
class OptionRobustnessInput:
    """Complete immutable evidence envelope consumed by the S5 executor."""

    robustness_input_id: str
    run_type: RunType
    chain_snapshot: OptionChainSnapshot
    positions: tuple[OptionPosition, ...]
    priced_position_ids: tuple[str, ...]
    valuation_inputs: tuple[ValuationInputSnapshot, ...]
    base_iv_results: tuple[ImpliedVolatilityResult, ...]
    comparison_iv_results: tuple[ImpliedVolatilityResult, ...]
    greeks: tuple[OptionGreeks, ...]
    base_surface: VolatilitySurface
    comparison_surface: VolatilitySurface
    fills: tuple[OptionFill, ...]
    marks: tuple[OptionMark, ...]
    lifecycle_events: tuple[OptionLifecycleEvent, ...]
    multileg_orders: tuple[MultiLegOrder, ...]
    multileg_results: tuple[MultiLegExecutionResult, ...]
    payoff_spots: tuple[Decimal, ...]
    definition_hashes: tuple[str, ...]
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version)
        require_stable_id(
            self.robustness_input_id,
            "option_robustness_input.robustness_input_id",
        )
        _require_enum(self.run_type, RunType, "option_robustness_input.run_type")
        if self.run_type is not RunType.ROBUSTNESS:
            raise DerivativeResearchError("option_robustness_input_run_type_required")
        if not isinstance(self.chain_snapshot, OptionChainSnapshot):
            raise DerivativeResearchError("option_robustness_input_chain_required")
        if not isinstance(self.base_surface, VolatilitySurface) or not isinstance(
            self.comparison_surface, VolatilitySurface
        ):
            raise DerivativeResearchError("option_robustness_input_surfaces_required")
        if not self.positions or not self.priced_position_ids:
            raise DerivativeResearchError("option_robustness_input_positions_required")
        sequence_keys: tuple[tuple[str, Sequence[object], object], ...] = (
            ("positions", self.positions, lambda item: item.position_id),
            (
                "valuation_inputs",
                self.valuation_inputs,
                lambda item: item.contract.contract_id,
            ),
            (
                "base_iv_results",
                self.base_iv_results,
                lambda item: item.contract_id,
            ),
            (
                "comparison_iv_results",
                self.comparison_iv_results,
                lambda item: item.contract_id,
            ),
            ("greeks", self.greeks, lambda item: item.contract_id),
            ("fills", self.fills, lambda item: item.content_hash),
            ("marks", self.marks, lambda item: item.position_id),
            (
                "lifecycle_events",
                self.lifecycle_events,
                lambda item: item.event_id,
            ),
            (
                "multileg_orders",
                self.multileg_orders,
                lambda item: item.group_id,
            ),
            (
                "multileg_results",
                self.multileg_results,
                lambda item: item.group_id,
            ),
        )
        for name, items, key_function in sequence_keys:
            keys = [key_function(item) for item in items]  # type: ignore[operator]
            if len(keys) != len(set(keys)):
                raise DerivativeResearchError(
                    f"option_robustness_input_{name}_duplicate"
                )
        if len(self.priced_position_ids) != len(set(self.priced_position_ids)):
            raise DerivativeResearchError(
                "option_robustness_input_priced_position_duplicate"
            )
        for position_id in self.priced_position_ids:
            require_stable_id(position_id, "option_robustness_input.priced_position_id")
        positions_by_id = {item.position_id: item for item in self.positions}
        priced_positions = tuple(
            positions_by_id.get(position_id) for position_id in self.priced_position_ids
        )
        if any(item is None for item in priced_positions):
            raise DerivativeResearchError(
                "option_robustness_input_priced_position_missing"
            )
        priced = tuple(item for item in priced_positions if item is not None)
        priced_contract_ids = {item.contract.contract_id for item in priced}
        if len(priced_contract_ids) != len(priced):
            raise DerivativeResearchError(
                "option_robustness_input_priced_contract_duplicate"
            )
        if any(
            item.contract.exercise_style is not ExerciseStyle.EUROPEAN
            for item in priced
        ):
            raise DerivativeResearchError(
                "option_robustness_input_priced_option_not_european"
            )
        if {item.side for item in priced} != {PositionSide.LONG, PositionSide.SHORT}:
            raise DerivativeResearchError(
                "option_robustness_input_long_short_priced_positions_required"
            )
        if len({item.contract.expiration_at for item in priced}) < 2:
            raise DerivativeResearchError(
                "option_robustness_input_expiry_dispersion_required"
            )
        if len({item.contract.strike for item in priced}) < 2:
            raise DerivativeResearchError(
                "option_robustness_input_strike_dispersion_required"
            )
        chain_contracts = {
            item.contract_id: item for item in self.chain_snapshot.contracts
        }
        chain_quotes = {item.contract_id: item for item in self.chain_snapshot.quotes}
        for position in self.positions:
            contract = chain_contracts.get(position.contract.contract_id)
            if (
                contract is None
                or contract.content_hash != position.contract.content_hash
            ):
                raise DerivativeResearchError(
                    "option_robustness_input_position_chain_mismatch"
                )
        fills_by_hash = {item.content_hash: item for item in self.fills}
        for position in self.positions:
            fill = fills_by_hash.get(position.source_fill_hash)
            if fill is None:
                raise DerivativeResearchError(
                    "option_robustness_input_position_fill_missing"
                )
            rebuilt = position_from_fill(fill, position_id=position.position_id)
            if rebuilt.content_hash != position.content_hash:
                raise DerivativeResearchError(
                    "option_robustness_input_position_fill_mismatch"
                )
            quote = chain_quotes.get(fill.contract.contract_id)
            if quote is None or quote.content_hash != fill.quote_hash:
                raise DerivativeResearchError(
                    "option_robustness_input_fill_quote_mismatch"
                )
        inputs_by_contract = {
            item.contract.contract_id: item for item in self.valuation_inputs
        }
        if set(inputs_by_contract) != priced_contract_ids:
            raise DerivativeResearchError(
                "option_robustness_input_valuation_coverage_mismatch"
            )
        expected_sources = set(self.chain_snapshot.source_manifest_hashes)
        for contract_id, inputs in inputs_by_contract.items():
            position = next(
                item for item in priced if item.contract.contract_id == contract_id
            )
            chain_quote = chain_quotes[contract_id]
            if (
                inputs.contract.content_hash != position.contract.content_hash
                or inputs.quote.content_hash != chain_quote.content_hash
                or inputs.valuation_at != self.chain_snapshot.knowledge_time
                or set(inputs.source_manifest_hashes) != expected_sources
            ):
                raise DerivativeResearchError(
                    "option_robustness_input_valuation_chain_mismatch"
                )
        base_iv = {item.contract_id: item for item in self.base_iv_results}
        comparison_iv = {item.contract_id: item for item in self.comparison_iv_results}
        greek_by_contract = {item.contract_id: item for item in self.greeks}
        if (
            set(base_iv) != priced_contract_ids
            or set(comparison_iv) != priced_contract_ids
            or set(greek_by_contract) != priced_contract_ids
        ):
            raise DerivativeResearchError(
                "option_robustness_input_model_coverage_mismatch"
            )
        base_versions = {item.model_version for item in self.base_iv_results}
        comparison_versions = {
            item.model_version for item in self.comparison_iv_results
        }
        if (
            len(base_versions) != 1
            or len(comparison_versions) != 1
            or base_versions == comparison_versions
        ):
            raise DerivativeResearchError(
                "option_robustness_input_distinct_iv_models_required"
            )
        for contract_id in priced_contract_ids:
            inputs = inputs_by_contract[contract_id]
            base = base_iv[contract_id]
            comparison = comparison_iv[contract_id]
            greek = greek_by_contract[contract_id]
            if (
                not base.success
                or not comparison.success
                or base.volatility is None
                or comparison.volatility is None
                or base.valuation_input_hash != inputs.content_hash
                or comparison.valuation_input_hash != inputs.content_hash
                or greek.valuation_input_hash != inputs.content_hash
                or greek.volatility != base.volatility
                or greek.model_version != base.model_version
            ):
                raise DerivativeResearchError(
                    "option_robustness_input_model_evidence_mismatch"
                )
        if (
            self.base_surface.source_chain_hash != self.chain_snapshot.content_hash
            or self.comparison_surface.source_chain_hash
            != self.chain_snapshot.content_hash
            or self.base_surface.underlying_id != self.chain_snapshot.underlying_id
            or self.comparison_surface.underlying_id
            != self.chain_snapshot.underlying_id
            or self.base_surface.as_of != self.chain_snapshot.knowledge_time
            or self.comparison_surface.as_of != self.chain_snapshot.knowledge_time
            or self.base_surface.interpolation_version
            == self.comparison_surface.interpolation_version
        ):
            raise DerivativeResearchError(
                "option_robustness_input_surface_chain_mismatch"
            )
        for surface, expected_iv in (
            (self.base_surface, base_iv),
            (self.comparison_surface, comparison_iv),
        ):
            points = {item.contract_id: item for item in surface.points}
            if not priced_contract_ids.issubset(points):
                raise DerivativeResearchError(
                    "option_robustness_input_surface_coverage_mismatch"
                )
            for contract_id in priced_contract_ids:
                point = points[contract_id]
                inputs = inputs_by_contract[contract_id]
                iv_result = expected_iv[contract_id]
                if (
                    point.expiration_at != inputs.contract.expiration_at
                    or point.strike != inputs.contract.strike
                    or point.valuation_input_hash != inputs.content_hash
                    or point.iv_result_hash != iv_result.content_hash
                    or point.model_version != iv_result.model_version
                    or point.implied_volatility != iv_result.volatility
                ):
                    raise DerivativeResearchError(
                        "option_robustness_input_surface_point_mismatch"
                    )
        marks_by_position = {item.position_id: item for item in self.marks}
        if set(marks_by_position) != set(self.priced_position_ids):
            raise DerivativeResearchError(
                "option_robustness_input_mark_coverage_mismatch"
            )
        for position in priced:
            mark = marks_by_position[position.position_id]
            inputs = inputs_by_contract[position.contract.contract_id]
            quote = chain_quotes[position.contract.contract_id]
            if (
                mark.quote_hash != quote.content_hash
                or mark.theoretical_input_hash != inputs.content_hash
                or mark.marked_at != self.chain_snapshot.knowledge_time
                or mark.signed_liquidation_value is None
                or mark.liquidation_pnl is None
            ):
                raise DerivativeResearchError("option_robustness_input_mark_mismatch")
        if not self.lifecycle_events:
            raise DerivativeResearchError(
                "option_robustness_input_lifecycle_evidence_required"
            )
        lifecycle_types = {item.event_type for item in self.lifecycle_events}
        if not {
            LifecycleEventType.EXERCISE,
            LifecycleEventType.ASSIGNMENT,
        }.issubset(lifecycle_types):
            raise DerivativeResearchError(
                "option_robustness_input_exercise_assignment_required"
            )
        for event in self.lifecycle_events:
            lifecycle_position = positions_by_id.get(event.position_id)
            if (
                lifecycle_position is None
                or event.contract_id != lifecycle_position.contract.contract_id
                or event.source_position_hash != lifecycle_position.content_hash
                or (
                    event.event_type is LifecycleEventType.EXERCISE
                    and lifecycle_position.side is not PositionSide.LONG
                )
                or (
                    event.event_type is LifecycleEventType.ASSIGNMENT
                    and lifecycle_position.side is not PositionSide.SHORT
                )
            ):
                raise DerivativeResearchError(
                    "option_robustness_input_lifecycle_mismatch"
                )
        orders_by_group = {item.group_id: item for item in self.multileg_orders}
        if not self.multileg_results or set(orders_by_group) != {
            item.group_id for item in self.multileg_results
        }:
            raise DerivativeResearchError(
                "option_robustness_input_multileg_coverage_mismatch"
            )
        if not any(
            item.state is MultiLegState.PARTIAL for item in self.multileg_results
        ):
            raise DerivativeResearchError(
                "option_robustness_input_partial_multileg_required"
            )
        for result in self.multileg_results:
            order = orders_by_group[result.group_id]
            if result.order_hash != order.content_hash:
                raise DerivativeResearchError(
                    "option_robustness_input_multileg_order_mismatch"
                )
            for fill in result.attempted_fills:
                quote = chain_quotes.get(fill.contract.contract_id)
                if quote is None or fill.quote_hash != quote.content_hash:
                    raise DerivativeResearchError(
                        "option_robustness_input_multileg_quote_mismatch"
                    )
        spots = tuple(
            sorted(
                {
                    exact_decimal(item, "option_robustness_input.payoff_spot")
                    for item in self.payoff_spots
                }
            )
        )
        if (
            len(spots) < 4
            or spots[0] != 0
            or spots[-1] < self.chain_snapshot.underlying_price * Decimal("3")
        ):
            raise DerivativeResearchError(
                "option_robustness_input_payoff_tail_grid_required"
            )
        object.__setattr__(self, "payoff_spots", spots)
        if len(self.definition_hashes) < 5 or len(self.definition_hashes) != len(
            set(self.definition_hashes)
        ):
            raise DerivativeResearchError(
                "option_robustness_input_definition_hashes_invalid"
            )
        for value in self.definition_hashes:
            require_hash(value, "option_robustness_input.definition_hash")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_robustness_input"),
        )

    @property
    def evidence_hashes(self) -> tuple[str, ...]:
        nested = (
            self.chain_snapshot.content_hash,
            *(item.content_hash for item in self.positions),
            *(item.content_hash for item in self.valuation_inputs),
            *(item.content_hash for item in self.base_iv_results),
            *(item.content_hash for item in self.comparison_iv_results),
            *(item.content_hash for item in self.greeks),
            self.base_surface.content_hash,
            self.comparison_surface.content_hash,
            *(item.content_hash for item in self.fills),
            *(item.content_hash for item in self.marks),
            *(item.content_hash for item in self.lifecycle_events),
            *(item.content_hash for item in self.multileg_orders),
            *(item.content_hash for item in self.multileg_results),
            *self.definition_hashes,
        )
        return tuple(dict.fromkeys(nested))

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "robustness_input_id": self.robustness_input_id,
            "run_type": self.run_type.value,
            "chain_snapshot": self.chain_snapshot.as_dict(),
            "positions": [_position_payload(item) for item in self.positions],
            "priced_position_ids": list(self.priced_position_ids),
            "valuation_inputs": [item.as_dict() for item in self.valuation_inputs],
            "base_iv_results": [item.as_dict() for item in self.base_iv_results],
            "comparison_iv_results": [
                item.as_dict() for item in self.comparison_iv_results
            ],
            "greeks": [item.as_dict() for item in self.greeks],
            "base_surface": self.base_surface.as_dict(),
            "comparison_surface": self.comparison_surface.as_dict(),
            "fills": [item.as_dict() for item in self.fills],
            "marks": [_mark_payload(item) for item in self.marks],
            "lifecycle_events": [
                _lifecycle_payload(item) for item in self.lifecycle_events
            ],
            "multileg_orders": [_order_payload(item) for item in self.multileg_orders],
            "multileg_results": [
                _multileg_payload(item) for item in self.multileg_results
            ],
            "payoff_spots": [decimal_text(item) for item in self.payoff_spots],
            "definition_hashes": list(self.definition_hashes),
        }


@dataclass(frozen=True, slots=True)
class OptionRobustnessMetric:
    metric_id: str
    value: Decimal
    unit: str
    contract_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_stable_id(self.metric_id, "option_robustness_metric.metric_id")
        require_stable_id(self.unit, "option_robustness_metric.unit")
        object.__setattr__(
            self,
            "value",
            exact_decimal(self.value, "option_robustness_metric.value"),
        )
        if len(self.contract_ids) != len(set(self.contract_ids)):
            raise DerivativeResearchError("option_robustness_metric_contract_duplicate")
        for contract_id in self.contract_ids:
            require_stable_id(contract_id, "option_robustness_metric.contract_id")

    def as_dict(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id,
            "value": decimal_text(self.value),
            "unit": self.unit,
            "contract_ids": list(self.contract_ids),
        }


@dataclass(frozen=True, slots=True)
class OptionRobustnessExecution:
    case_id: str
    dimension: OptionRobustnessDimension
    case_hash: str
    input_hash: str
    evidence_hashes: tuple[str, ...]
    derived_artifact_hashes: tuple[str, ...]
    metrics: tuple[OptionRobustnessMetric, ...]
    baseline_value: Decimal
    worst_value: Decimal
    adverse_change: Decimal
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.case_id, "option_robustness_execution.case_id")
        _require_enum(
            self.dimension,
            OptionRobustnessDimension,
            "option_robustness_execution.dimension",
        )
        require_hash(self.case_hash, "option_robustness_execution.case_hash")
        require_hash(self.input_hash, "option_robustness_execution.input_hash")
        for value in (*self.evidence_hashes, *self.derived_artifact_hashes):
            require_hash(value, "option_robustness_execution.evidence_hash")
        if not self.evidence_hashes or not self.metrics:
            raise DerivativeResearchError(
                "option_robustness_execution_evidence_and_metrics_required"
            )
        if len(self.evidence_hashes) != len(set(self.evidence_hashes)) or len(
            self.derived_artifact_hashes
        ) != len(set(self.derived_artifact_hashes)):
            raise DerivativeResearchError("option_robustness_execution_hash_duplicate")
        metric_ids = [item.metric_id for item in self.metrics]
        if len(metric_ids) != len(set(metric_ids)):
            raise DerivativeResearchError(
                "option_robustness_execution_metric_duplicate"
            )
        baseline = exact_decimal(
            self.baseline_value, "option_robustness_execution.baseline_value"
        )
        worst = exact_decimal(
            self.worst_value, "option_robustness_execution.worst_value"
        )
        adverse = exact_decimal(
            self.adverse_change, "option_robustness_execution.adverse_change"
        )
        if adverse != worst - baseline:
            raise DerivativeResearchError(
                "option_robustness_execution_adverse_change_mismatch"
            )
        object.__setattr__(self, "baseline_value", baseline)
        object.__setattr__(self, "worst_value", worst)
        object.__setattr__(self, "adverse_change", adverse)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(), label="option_robustness_execution"
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "dimension": self.dimension.value,
            "case_hash": self.case_hash,
            "input_hash": self.input_hash,
            "evidence_hashes": list(self.evidence_hashes),
            "derived_artifact_hashes": list(self.derived_artifact_hashes),
            "metrics": [item.as_dict() for item in self.metrics],
            "baseline_value": decimal_text(self.baseline_value),
            "worst_value": decimal_text(self.worst_value),
            "adverse_change": decimal_text(self.adverse_change),
        }


@dataclass(frozen=True, slots=True)
class OptionRobustnessSummary:
    suite_id: str
    input_hash: str
    case_hashes: tuple[str, ...]
    execution_hashes: tuple[str, ...]
    dimensions: tuple[OptionRobustnessDimension, ...]
    worst_dimension: OptionRobustnessDimension
    worst_adverse_change: Decimal
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.suite_id, "option_robustness_summary.suite_id")
        require_hash(self.input_hash, "option_robustness_summary.input_hash")
        for value in (*self.case_hashes, *self.execution_hashes):
            require_hash(value, "option_robustness_summary.artifact_hash")
        expected = tuple(OptionRobustnessDimension)
        if (
            tuple(sorted(self.dimensions, key=lambda item: item.value))
            != tuple(sorted(expected, key=lambda item: item.value))
            or len(self.case_hashes) != len(expected)
            or len(self.execution_hashes) != len(expected)
            or len(set(self.case_hashes)) != len(expected)
            or len(set(self.execution_hashes)) != len(expected)
        ):
            raise DerivativeResearchError(
                "option_robustness_summary_full_coverage_required"
            )
        _require_enum(
            self.worst_dimension,
            OptionRobustnessDimension,
            "option_robustness_summary.worst_dimension",
        )
        object.__setattr__(
            self,
            "worst_adverse_change",
            exact_decimal(
                self.worst_adverse_change,
                "option_robustness_summary.worst_adverse_change",
            ),
        )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_robustness_summary"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "suite_id": self.suite_id,
            "input_hash": self.input_hash,
            "case_hashes": list(self.case_hashes),
            "execution_hashes": list(self.execution_hashes),
            "dimensions": [item.value for item in self.dimensions],
            "worst_dimension": self.worst_dimension.value,
            "worst_adverse_change": decimal_text(self.worst_adverse_change),
        }


def _robustness_maps(
    inputs: OptionRobustnessInput,
) -> tuple[
    tuple[OptionPosition, ...],
    dict[str, ValuationInputSnapshot],
    dict[str, Decimal],
    dict[str, Decimal],
]:
    positions_by_id = {item.position_id: item for item in inputs.positions}
    priced = tuple(positions_by_id[item] for item in inputs.priced_position_ids)
    valuation = {item.contract.contract_id: item for item in inputs.valuation_inputs}
    base = {
        item.contract_id: item.volatility
        for item in inputs.base_iv_results
        if item.volatility is not None
    }
    comparison = {
        item.contract_id: item.volatility
        for item in inputs.comparison_iv_results
        if item.volatility is not None
    }
    return priced, valuation, base, comparison


def _run_robustness_stress(
    inputs: OptionRobustnessInput,
    case: OptionRobustnessCase,
    *,
    suffix: str,
    positions: Sequence[OptionPosition] | None = None,
    volatilities: Mapping[str, Decimal] | None = None,
    spot_shock_ratio: Decimal = _ZERO,
    volatility_shock: Decimal = _ZERO,
    rate_shock: Decimal = _ZERO,
    dividend_yield_shock: Decimal = _ZERO,
    spread_multiplier: Decimal = _ONE,
    days_forward: int = 0,
) -> OptionStressResult:
    priced, valuation, base_volatilities, _comparison = _robustness_maps(inputs)
    selected = tuple(positions) if positions is not None else priced
    scenario = OptionStressScenario(
        scenario_id=f"{case.case_id}.{suffix}",
        spot_shock_ratio=spot_shock_ratio,
        volatility_shock=volatility_shock,
        rate_shock=rate_shock,
        dividend_yield_shock=dividend_yield_shock,
        liquidity_spread_multiplier=spread_multiplier,
        days_forward=days_forward,
        scenario_policy_hash=case.policy.content_hash,
    )
    model_version = next(iter(inputs.base_iv_results)).model_version
    return stress_option_portfolio(
        selected,
        inputs_by_contract=valuation,
        volatility_by_contract=volatilities or base_volatilities,
        scenario=scenario,
        model=BlackScholesModel(model_version=model_version),
    )


def _metric(
    metric_id: str,
    value: Decimal | int,
    unit: str,
    contract_ids: Sequence[str] = (),
) -> OptionRobustnessMetric:
    return OptionRobustnessMetric(
        metric_id=metric_id,
        value=Decimal(value),
        unit=unit,
        contract_ids=tuple(sorted(set(contract_ids))),
    )


def _required_liquidation_value(mark: OptionMark) -> Decimal:
    value = mark.signed_liquidation_value
    if value is None:
        raise DerivativeResearchError("option_robustness_liquidation_value_missing")
    return value


def execute_option_robustness_case(
    inputs: OptionRobustnessInput,
    case: OptionRobustnessCase,
) -> OptionRobustnessExecution:
    """Execute one S5-O case without network, randomness, or mutable state."""

    if not isinstance(inputs, OptionRobustnessInput) or not isinstance(
        case, OptionRobustnessCase
    ):
        raise DerivativeResearchError("option_robustness_typed_inputs_required")
    priced, valuation, base_vols, comparison_vols = _robustness_maps(inputs)
    quotes = {item.contract_id: item for item in inputs.chain_snapshot.quotes}
    marks = {item.position_id: item for item in inputs.marks}
    metrics: list[OptionRobustnessMetric] = []
    derived: list[str] = []
    dimension = case.dimension
    baseline = _ZERO
    worst = _ZERO

    if dimension is OptionRobustnessDimension.BID_ASK_COST:
        values: list[Decimal] = []
        for multiplier in case.policy.spread_multipliers:
            result = _run_robustness_stress(
                inputs,
                case,
                suffix=f"spread.{decimal_text(multiplier)}",
                spread_multiplier=multiplier,
            )
            derived.append(result.content_hash)
            values.append(result.total_stressed_liquidation_value)
            metrics.append(
                _metric(
                    f"spread.{decimal_text(multiplier)}",
                    result.total_stressed_liquidation_value,
                    "currency",
                )
            )
        baseline = values[case.policy.spread_multipliers.index(_ONE)]
        worst = min(values)
    elif dimension is OptionRobustnessDimension.MIDPOINT_COMPARISON:
        midpoint_value = _ZERO
        conservative_value = _ZERO
        for position in priced:
            quote = quotes[position.contract.contract_id]
            midpoint = quote.midpoint
            mark = marks[position.position_id]
            if midpoint is None or mark.signed_liquidation_value is None:
                raise DerivativeResearchError(
                    "option_robustness_midpoint_or_liquidation_missing"
                )
            scale = position.quantity * position.contract.multiplier
            midpoint_value += _signed(position.side) * midpoint * scale
            conservative_value += mark.signed_liquidation_value
        metrics.extend(
            (
                _metric("midpoint.value", midpoint_value, "currency"),
                _metric("conservative.value", conservative_value, "currency"),
                _metric(
                    "conservative.cost",
                    conservative_value - midpoint_value,
                    "currency",
                ),
            )
        )
        baseline, worst = midpoint_value, conservative_value
    elif dimension is OptionRobustnessDimension.STALE_QUOTE_FILTER:
        values = []
        for cutoff in case.policy.stale_cutoffs_seconds:
            retained = [
                item
                for item in priced
                if quotes[item.contract.contract_id].quote_age_seconds <= cutoff
            ]
            value = sum(
                (
                    _required_liquidation_value(marks[item.position_id])
                    for item in retained
                ),
                _ZERO,
            )
            values.append(value)
            metrics.extend(
                (
                    _metric(f"stale.{cutoff}.count", len(retained), "count"),
                    _metric(f"stale.{cutoff}.value", value, "currency"),
                )
            )
        baseline, worst = values[-1], min(values)
    elif dimension is OptionRobustnessDimension.LIQUIDITY_FILTER:
        values = []

        def record_filter(
            label: str,
            threshold: Decimal | int | str,
            accepted: Sequence[OptionPosition],
        ) -> None:
            value = sum(
                (
                    _required_liquidation_value(marks[item.position_id])
                    for item in accepted
                ),
                _ZERO,
            )
            values.append(value)
            metrics.append(
                _metric(f"{label}.{threshold}.count", len(accepted), "count")
            )
            metrics.append(_metric(f"{label}.{threshold}.value", value, "currency"))

        for volume_threshold in case.policy.volume_thresholds:
            record_filter(
                "volume",
                volume_threshold,
                [
                    item
                    for item in priced
                    if quotes[item.contract.contract_id].volume >= volume_threshold
                ],
            )
        for open_interest_threshold in case.policy.open_interest_thresholds:
            record_filter(
                "open_interest",
                open_interest_threshold,
                [
                    item
                    for item in priced
                    if quotes[item.contract.contract_id].open_interest
                    >= open_interest_threshold
                ],
            )
        for spread_ratio_threshold in case.policy.spread_ratio_thresholds:
            accepted: list[OptionPosition] = []
            for item in priced:
                quote = quotes[item.contract.contract_id]
                midpoint = quote.midpoint
                if (
                    midpoint is not None
                    and quote.spread_width is not None
                    and quote.spread_width / midpoint <= spread_ratio_threshold
                ):
                    accepted.append(item)
            record_filter(
                "spread_ratio",
                decimal_text(spread_ratio_threshold),
                accepted,
            )
        for quote_age_threshold in case.policy.quote_age_thresholds:
            record_filter(
                "quote_age",
                quote_age_threshold,
                [
                    item
                    for item in priced
                    if quotes[item.contract.contract_id].quote_age_seconds
                    <= quote_age_threshold
                ],
            )
        baseline = sum(
            (_required_liquidation_value(marks[item.position_id]) for item in priced),
            _ZERO,
        )
        worst = min(values)
    elif dimension is OptionRobustnessDimension.IV_MODEL:
        base_result = _run_robustness_stress(
            inputs, case, suffix="iv.base", volatilities=base_vols
        )
        comparison_result = _run_robustness_stress(
            inputs,
            case,
            suffix="iv.comparison",
            volatilities=comparison_vols,
        )
        derived.extend((base_result.content_hash, comparison_result.content_hash))
        baseline = base_result.total_stressed_value
        worst = min(baseline, comparison_result.total_stressed_value)
        metrics.extend(
            (
                _metric("iv_model.base", baseline, "currency"),
                _metric(
                    "iv_model.comparison",
                    comparison_result.total_stressed_value,
                    "currency",
                ),
            )
        )
    elif dimension is OptionRobustnessDimension.RATE_DIVIDEND:
        base_result = _run_robustness_stress(inputs, case, suffix="carry.base")
        derived.append(base_result.content_hash)
        baseline = base_result.total_stressed_value
        values = [baseline]
        metrics.append(_metric("carry.base", baseline, "currency"))
        for shock in case.policy.rate_shocks:
            result = _run_robustness_stress(
                inputs,
                case,
                suffix=f"rate.{decimal_text(shock)}",
                rate_shock=shock,
            )
            derived.append(result.content_hash)
            values.append(result.total_stressed_value)
            metrics.append(
                _metric(
                    f"rate.{decimal_text(shock)}",
                    result.total_stressed_value,
                    "currency",
                )
            )
        for shock in case.policy.dividend_yield_shocks:
            result = _run_robustness_stress(
                inputs,
                case,
                suffix=f"dividend.{decimal_text(shock)}",
                dividend_yield_shock=shock,
            )
            derived.append(result.content_hash)
            values.append(result.total_stressed_value)
            metrics.append(
                _metric(
                    f"dividend.{decimal_text(shock)}",
                    result.total_stressed_value,
                    "currency",
                )
            )
        worst = min(values)
    elif dimension is OptionRobustnessDimension.SURFACE_INTERPOLATION:
        surface_vols = {
            item.contract.contract_id: inputs.base_surface.interpolate(
                expiration_at=item.contract.expiration_at,
                strike=item.contract.strike,
            )
            for item in priced
        }
        comparison_surface_vols = {
            item.contract.contract_id: inputs.comparison_surface.interpolate(
                expiration_at=item.contract.expiration_at,
                strike=item.contract.strike,
            )
            for item in priced
        }
        base_result = _run_robustness_stress(
            inputs, case, suffix="surface.base", volatilities=surface_vols
        )
        comparison_result = _run_robustness_stress(
            inputs,
            case,
            suffix="surface.comparison",
            volatilities=comparison_surface_vols,
        )
        derived.extend((base_result.content_hash, comparison_result.content_hash))
        baseline = base_result.total_stressed_value
        worst = min(baseline, comparison_result.total_stressed_value)
        metrics.extend(
            (
                _metric("surface.base", baseline, "currency"),
                _metric(
                    "surface.comparison",
                    comparison_result.total_stressed_value,
                    "currency",
                ),
            )
        )
    elif dimension is OptionRobustnessDimension.CHAIN_SELECTION:
        normal_contracts = [
            item
            for item in inputs.chain_snapshot.contracts
            if quotes[item.contract_id].state is QuoteState.NORMAL
        ]
        nearest_strike_distance = min(
            abs(item.strike - inputs.chain_snapshot.underlying_price)
            for item in normal_contracts
        )
        fixed = [
            item.contract_id
            for item in normal_contracts
            if abs(item.strike - inputs.chain_snapshot.underlying_price)
            == nearest_strike_distance
        ]
        greek_map = {item.contract_id: item for item in inputs.greeks}
        delta_distance = min(
            abs(abs(item.delta) - case.policy.delta_target) for item in inputs.greeks
        )
        delta_selected = [
            contract_id
            for contract_id, item in greek_map.items()
            if abs(abs(item.delta) - case.policy.delta_target) == delta_distance
        ]
        moneyness = [
            item.contract_id
            for item in normal_contracts
            if abs(item.strike / inputs.chain_snapshot.underlying_price - _ONE)
            <= case.policy.moneyness_tolerance
        ]
        nearest_expiry = min(item.expiration_at for item in normal_contracts)
        expiry = [
            item.contract_id
            for item in normal_contracts
            if item.expiration_at == nearest_expiry
        ]
        selections = (
            ("fixed_strike", fixed),
            ("delta", delta_selected),
            ("moneyness", moneyness),
            ("expiry", expiry),
        )
        for label, selected in selections:
            metrics.append(
                _metric(f"selection.{label}.count", len(selected), "count", selected)
            )
        baseline = Decimal(len(normal_contracts))
        worst = Decimal(min(len(item) for _label, item in selections))
    elif dimension in {
        OptionRobustnessDimension.EXPIRY_CONCENTRATION,
        OptionRobustnessDimension.STRIKE_CONCENTRATION,
    }:
        grouped: dict[str, Decimal] = {}
        grouped_ids: dict[str, list[str]] = {}
        for position in priced:
            mark = marks[position.position_id]
            if mark.liquidation_pnl is None:
                raise DerivativeResearchError(
                    "option_robustness_concentration_mark_missing"
                )
            key = (
                position.contract.expiration_at
                if dimension is OptionRobustnessDimension.EXPIRY_CONCENTRATION
                else decimal_text(position.contract.strike)
            )
            grouped[key] = grouped.get(key, _ZERO) + mark.liquidation_pnl
            grouped_ids.setdefault(key, []).append(position.contract.contract_id)
        prefix = (
            "expiry"
            if dimension is OptionRobustnessDimension.EXPIRY_CONCENTRATION
            else "strike"
        )
        for index, key in enumerate(sorted(grouped)):
            metrics.append(
                _metric(
                    f"{prefix}.{index}.pnl", grouped[key], "currency", grouped_ids[key]
                )
            )
        total_absolute = sum((abs(item) for item in grouped.values()), _ZERO)
        concentration = (
            max(abs(item) for item in grouped.values()) / total_absolute
            if total_absolute > 0
            else _ZERO
        )
        metrics.append(_metric(f"{prefix}.concentration", concentration, "ratio"))
        baseline = sum(grouped.values(), _ZERO)
        worst = min(grouped.values())
    elif dimension is OptionRobustnessDimension.EXTREME_VOLATILITY:
        values = []
        for volatility in case.policy.extreme_volatilities:
            volatilities = {item.contract.contract_id: volatility for item in priced}
            result = _run_robustness_stress(
                inputs,
                case,
                suffix=f"extreme_vol.{decimal_text(volatility)}",
                volatilities=volatilities,
            )
            derived.append(result.content_hash)
            values.append(result.total_stressed_value)
            metrics.append(
                _metric(
                    f"extreme_vol.{decimal_text(volatility)}",
                    result.total_stressed_value,
                    "currency",
                )
            )
        baseline = _run_robustness_stress(
            inputs, case, suffix="extreme_vol.base"
        ).total_stressed_value
        worst = min(values)
    elif dimension is OptionRobustnessDimension.VOLATILITY_SHOCK:
        base_result = _run_robustness_stress(inputs, case, suffix="vol.base")
        derived.append(base_result.content_hash)
        baseline = base_result.total_stressed_value
        values = [baseline]
        metrics.append(_metric("vol.base", baseline, "currency"))
        for shock in case.policy.volatility_shocks:
            result = _run_robustness_stress(
                inputs,
                case,
                suffix=f"vol.{decimal_text(shock)}",
                volatility_shock=shock,
            )
            derived.append(result.content_hash)
            values.append(result.total_stressed_value)
            metrics.append(
                _metric(
                    f"vol.{decimal_text(shock)}",
                    result.total_stressed_value,
                    "currency",
                )
            )
        worst = min(values)
    elif dimension is OptionRobustnessDimension.SKEW_SHIFT:
        base_result = _run_robustness_stress(inputs, case, suffix="skew.base")
        derived.append(base_result.content_hash)
        baseline = base_result.total_stressed_value
        stressed = _ZERO
        for position in priced:
            shift = case.policy.skew_shift * (
                position.contract.strike / inputs.chain_snapshot.underlying_price - _ONE
            )
            result = _run_robustness_stress(
                inputs,
                case,
                suffix=f"skew.{position.position_id}",
                positions=(position,),
                volatility_shock=shift,
            )
            derived.append(result.content_hash)
            stressed += result.total_stressed_value
        metrics.extend(
            (
                _metric("skew.base", baseline, "currency"),
                _metric("skew.shifted", stressed, "currency"),
            )
        )
        worst = min(baseline, stressed)
    elif dimension is OptionRobustnessDimension.SPOT_IV_GAP:
        base_result = _run_robustness_stress(inputs, case, suffix="gap.base")
        gap_result = _run_robustness_stress(
            inputs,
            case,
            suffix="gap.combined",
            spot_shock_ratio=case.policy.spot_gap_ratio,
            volatility_shock=case.policy.iv_gap,
            spread_multiplier=Decimal("2"),
        )
        derived.extend((base_result.content_hash, gap_result.content_hash))
        baseline = base_result.total_stressed_liquidation_value
        worst = min(baseline, gap_result.total_stressed_liquidation_value)
        metrics.extend(
            (
                _metric("gap.base", baseline, "currency"),
                _metric(
                    "gap.spot_iv",
                    gap_result.total_stressed_liquidation_value,
                    "currency",
                ),
            )
        )
    elif dimension is OptionRobustnessDimension.EXERCISE_ASSIGNMENT:
        exercise = [
            item
            for item in inputs.lifecycle_events
            if item.event_type is LifecycleEventType.EXERCISE
        ]
        assignment = [
            item
            for item in inputs.lifecycle_events
            if item.event_type is LifecycleEventType.ASSIGNMENT
        ]
        lifecycle_cash = sum(
            (item.cash_delta for item in (*exercise, *assignment)), _ZERO
        )
        baseline = _ZERO
        worst = min(_ZERO, lifecycle_cash)
        metrics.extend(
            (
                _metric("lifecycle.exercise.count", len(exercise), "count"),
                _metric("lifecycle.assignment.count", len(assignment), "count"),
                _metric("lifecycle.applied_cash", lifecycle_cash, "currency"),
                _metric("lifecycle.no_early_cash", _ZERO, "currency"),
            )
        )
    elif dimension is OptionRobustnessDimension.EXPIRY_LIQUIDITY_LOSS:
        base_result = _run_robustness_stress(
            inputs, case, suffix="expiry_liquidity.base"
        )
        derived.append(base_result.content_hash)
        baseline = base_result.total_stressed_liquidation_value
        stressed = _ZERO
        for position in priced:
            value_input = valuation[position.contract.contract_id]
            seconds = (
                parse_timestamp(
                    position.contract.expiration_at, "option_contract.expiration_at"
                )
                - parse_timestamp(
                    value_input.valuation_at, "option_valuation_input.valuation_at"
                )
            ).total_seconds()
            days_forward = max(0, math.ceil(seconds / 86400))
            result = _run_robustness_stress(
                inputs,
                case,
                suffix=f"expiry_liquidity.{position.position_id}",
                positions=(position,),
                spread_multiplier=case.policy.expiry_spread_multiplier,
                days_forward=days_forward,
            )
            derived.append(result.content_hash)
            stressed += result.total_stressed_liquidation_value
        metrics.extend(
            (
                _metric("expiry_liquidity.base", baseline, "currency"),
                _metric("expiry_liquidity.lost", stressed, "currency"),
            )
        )
        worst = min(baseline, stressed)
    elif dimension is OptionRobustnessDimension.ZERO_BID_LIQUIDATION:
        base_value = _ZERO
        zero_bid_value = _ZERO
        affected: list[str] = []
        for position in priced:
            mark = marks[position.position_id]
            if mark.signed_liquidation_value is None:
                raise DerivativeResearchError("option_robustness_zero_bid_mark_missing")
            base_value += mark.signed_liquidation_value
            if position.side is PositionSide.LONG:
                zero_bid_value += (
                    mark.signed_liquidation_value * case.policy.zero_bid_recovery_ratio
                )
                affected.append(position.contract.contract_id)
            else:
                zero_bid_value += mark.signed_liquidation_value
        baseline, worst = base_value, min(base_value, zero_bid_value)
        metrics.extend(
            (
                _metric("zero_bid.base", base_value, "currency"),
                _metric("zero_bid.liquidation", zero_bid_value, "currency", affected),
                _metric(
                    "zero_bid.loss", zero_bid_value - base_value, "currency", affected
                ),
            )
        )
    elif dimension is OptionRobustnessDimension.MULTILEG_PARTIAL_FILL:
        partial = [
            item
            for item in inputs.multileg_results
            if item.state is MultiLegState.PARTIAL
        ]
        baseline = _ZERO
        stressed = _ZERO
        total_requested = _ZERO
        total_filled = _ZERO
        exposed_contracts: list[str] = []
        for multileg_result in partial:
            baseline += multileg_result.net_cash_flow
            exposed_notional = sum(
                (abs(fill.cash_flow) for fill in multileg_result.committed_fills),
                _ZERO,
            )
            stressed += multileg_result.net_cash_flow - (
                exposed_notional * case.policy.partial_fill_haircut
            )
            total_requested += sum(
                (fill.requested_quantity for fill in multileg_result.attempted_fills),
                _ZERO,
            )
            total_filled += sum(
                (fill.filled_quantity for fill in multileg_result.attempted_fills),
                _ZERO,
            )
            exposed_contracts.extend(multileg_result.legging_exposure_contract_ids)
        fill_ratio = total_filled / total_requested if total_requested > 0 else _ZERO
        worst = min(baseline, stressed)
        metrics.extend(
            (
                _metric("multileg.partial.count", len(partial), "count"),
                _metric("multileg.fill_ratio", fill_ratio, "ratio"),
                _metric("multileg.base_cash", baseline, "currency"),
                _metric(
                    "multileg.haircut_cash", stressed, "currency", exposed_contracts
                ),
            )
        )
    elif dimension is OptionRobustnessDimension.PAYOFF_TAIL_RISK:
        analysis = analyze_option_payoff(
            inputs.positions, scenario_spots=inputs.payoff_spots
        )
        for index, point in enumerate(analysis.points):
            metrics.append(
                _metric(f"payoff.tail.{index}", point.profit_loss, "currency")
            )
        metrics.extend(
            (
                _metric(
                    "payoff.unbounded_loss", int(analysis.unbounded_loss), "boolean"
                ),
                _metric(
                    "payoff.unbounded_profit", int(analysis.unbounded_profit), "boolean"
                ),
            )
        )
        payoff_hash = sha256_prefixed(
            {
                "position_hashes": [item.content_hash for item in inputs.positions],
                "points": [
                    {
                        "spot": decimal_text(item.underlying_price),
                        "profit_loss": decimal_text(item.profit_loss),
                    }
                    for item in analysis.points
                ],
                "unbounded_loss": analysis.unbounded_loss,
                "unbounded_profit": analysis.unbounded_profit,
            },
            label="option_robustness_payoff_tail",
        )
        derived.append(payoff_hash)
        baseline = next(
            (
                item.profit_loss
                for item in analysis.points
                if item.underlying_price == inputs.chain_snapshot.underlying_price
            ),
            option_expiry_payoff(
                inputs.positions, inputs.chain_snapshot.underlying_price
            ),
        )
        worst = min(item.profit_loss for item in analysis.points)
    else:
        short_positions = [
            item for item in inputs.positions if item.side is PositionSide.SHORT
        ]
        if not short_positions:
            raise DerivativeResearchError("option_robustness_short_positions_required")
        total_loss = _ZERO
        baseline = sum((item.entry_cash_flow for item in short_positions), _ZERO)
        for position in short_positions:
            standalone_losses = []
            for spot in inputs.payoff_spots:
                intrinsic = (
                    max(_ZERO, spot - position.contract.strike)
                    if position.contract.option_type is OptionType.CALL
                    else max(_ZERO, position.contract.strike - spot)
                )
                pnl = position.entry_cash_flow - (
                    intrinsic * position.quantity * position.contract.multiplier
                )
                standalone_losses.append(max(_ZERO, -pnl))
            worst_loss = max(standalone_losses)
            total_loss += worst_loss
            metrics.append(
                _metric(
                    f"rare_loss.{position.position_id}",
                    worst_loss * case.policy.rare_event_probability,
                    "probability_weighted_currency",
                    (position.contract.contract_id,),
                )
            )
        weighted = total_loss * case.policy.rare_event_probability
        metrics.extend(
            (
                _metric("rare_loss.total", total_loss, "currency"),
                _metric(
                    "rare_loss.weighted", weighted, "probability_weighted_currency"
                ),
                _metric(
                    "rare_loss.probability", case.policy.rare_event_probability, "ratio"
                ),
            )
        )
        worst = -total_loss

    evidence_hashes = inputs.evidence_hashes
    return OptionRobustnessExecution(
        case_id=case.case_id,
        dimension=case.dimension,
        case_hash=case.content_hash,
        input_hash=inputs.content_hash,
        evidence_hashes=evidence_hashes,
        derived_artifact_hashes=tuple(dict.fromkeys(derived)),
        metrics=tuple(metrics),
        baseline_value=baseline,
        worst_value=worst,
        adverse_change=worst - baseline,
    )


def run_option_robustness_suite(
    *,
    suite_id: str,
    inputs: OptionRobustnessInput,
    cases: Sequence[OptionRobustnessCase],
) -> tuple[tuple[OptionRobustnessExecution, ...], OptionRobustnessSummary]:
    """Run and summarize an exact, complete S5-O01..O20 matrix."""

    require_stable_id(suite_id, "option_robustness_suite.suite_id")
    expected = set(OptionRobustnessDimension)
    observed = [item.dimension for item in cases]
    if (
        len(cases) != len(expected)
        or set(observed) != expected
        or len(observed) != len(set(observed))
    ):
        raise DerivativeResearchError(
            "option_robustness_suite_full_case_matrix_required"
        )
    policy_hashes = {item.policy.content_hash for item in cases}
    if len(policy_hashes) != 1:
        raise DerivativeResearchError("option_robustness_suite_policy_mismatch")
    ordered_cases = tuple(sorted(cases, key=lambda item: item.dimension.value))
    executions = tuple(
        execute_option_robustness_case(inputs, item) for item in ordered_cases
    )
    worst_execution = min(executions, key=lambda item: item.adverse_change)
    summary = OptionRobustnessSummary(
        suite_id=suite_id,
        input_hash=inputs.content_hash,
        case_hashes=tuple(item.content_hash for item in ordered_cases),
        execution_hashes=tuple(item.content_hash for item in executions),
        dimensions=tuple(item.dimension for item in executions),
        worst_dimension=worst_execution.dimension,
        worst_adverse_change=worst_execution.adverse_change,
    )
    return executions, summary


@dataclass(frozen=True, slots=True)
class OptionProspectiveProtocol:
    protocol_id: str
    frozen_at: str
    evaluation_start: str
    evaluation_end: str
    minimum_observations: int
    maximum_mean_absolute_error: Decimal
    maximum_invalid_quote_fraction: Decimal
    model_version: str
    dataset_snapshot_hash: str
    surface_definition_hash: str
    acceptance_policy_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.protocol_id, "option_prospective.protocol_id")
        frozen = parse_timestamp(self.frozen_at, "option_prospective.frozen_at")
        start = parse_timestamp(
            self.evaluation_start, "option_prospective.evaluation_start"
        )
        end = parse_timestamp(self.evaluation_end, "option_prospective.evaluation_end")
        if not frozen < start < end:
            raise DerivativeResearchError("option_prospective_time_order_invalid")
        if self.minimum_observations <= 0:
            raise DerivativeResearchError("option_prospective_minimum_invalid")
        error = exact_decimal(
            self.maximum_mean_absolute_error,
            "option_prospective.maximum_mean_absolute_error",
        )
        invalid = exact_decimal(
            self.maximum_invalid_quote_fraction,
            "option_prospective.maximum_invalid_quote_fraction",
        )
        if error < 0 or invalid < 0 or invalid > 1:
            raise DerivativeResearchError("option_prospective_threshold_invalid")
        object.__setattr__(self, "maximum_mean_absolute_error", error)
        object.__setattr__(self, "maximum_invalid_quote_fraction", invalid)
        require_stable_id(self.model_version, "option_prospective.model_version")
        for value in (
            self.dataset_snapshot_hash,
            self.surface_definition_hash,
            self.acceptance_policy_hash,
        ):
            require_hash(value, "option_prospective.evidence_hash")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(), label="option_prospective_protocol"
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "protocol_id": self.protocol_id,
            "frozen_at": self.frozen_at,
            "evaluation_start": self.evaluation_start,
            "evaluation_end": self.evaluation_end,
            "minimum_observations": self.minimum_observations,
            "maximum_mean_absolute_error": decimal_text(
                self.maximum_mean_absolute_error
            ),
            "maximum_invalid_quote_fraction": decimal_text(
                self.maximum_invalid_quote_fraction
            ),
            "model_version": self.model_version,
            "dataset_snapshot_hash": self.dataset_snapshot_hash,
            "surface_definition_hash": self.surface_definition_hash,
            "acceptance_policy_hash": self.acceptance_policy_hash,
        }


@dataclass(frozen=True, slots=True)
class OptionProspectiveObservation:
    observation_id: str
    contract_id: str
    prediction_made_at: str
    observed_at: str
    predicted_price: Decimal
    observed_price: Decimal | None
    quote_state: QuoteState
    valuation_input_hash: str
    model_result_hash: str
    quote_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.observation_id, "option_observation.observation_id")
        require_stable_id(self.contract_id, "option_observation.contract_id")
        predicted_at = parse_timestamp(
            self.prediction_made_at, "option_observation.prediction_made_at"
        )
        observed_at = parse_timestamp(
            self.observed_at, "option_observation.observed_at"
        )
        if predicted_at >= observed_at:
            raise DerivativeResearchError("option_observation_not_prospective")
        predicted = exact_decimal(
            self.predicted_price, "option_observation.predicted_price"
        )
        observed = _optional_decimal(
            self.observed_price, "option_observation.observed_price"
        )
        if predicted < 0 or (observed is not None and observed < 0):
            raise DerivativeResearchError("option_observation_price_negative")
        _require_enum(self.quote_state, QuoteState, "option_observation.quote_state")
        if (self.quote_state is QuoteState.NORMAL) != (observed is not None):
            raise DerivativeResearchError("option_observation_quote_price_mismatch")
        object.__setattr__(self, "predicted_price", predicted)
        object.__setattr__(self, "observed_price", observed)
        for value in (
            self.valuation_input_hash,
            self.model_result_hash,
            self.quote_hash,
        ):
            require_hash(value, "option_observation.evidence_hash")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_observation"),
        )

    @property
    def absolute_error(self) -> Decimal | None:
        return (
            abs(self.observed_price - self.predicted_price)
            if self.observed_price is not None
            else None
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "contract_id": self.contract_id,
            "prediction_made_at": self.prediction_made_at,
            "observed_at": self.observed_at,
            "predicted_price": decimal_text(self.predicted_price),
            "observed_price": decimal_text(self.observed_price)
            if self.observed_price is not None
            else None,
            "quote_state": self.quote_state.value,
            "valuation_input_hash": self.valuation_input_hash,
            "model_result_hash": self.model_result_hash,
            "quote_hash": self.quote_hash,
        }


@dataclass(frozen=True, slots=True)
class OptionProspectiveEvaluation:
    protocol_hash: str
    observation_hashes: tuple[str, ...]
    evaluated_at: str
    valid_observations: int
    invalid_observations: int
    mean_absolute_error: Decimal | None
    invalid_quote_fraction: Decimal
    status: ProspectiveStatus
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_hash(self.protocol_hash, "option_evaluation.protocol_hash")
        for value in self.observation_hashes:
            require_hash(value, "option_evaluation.observation_hash")
        parse_timestamp(self.evaluated_at, "option_evaluation.evaluated_at")
        _require_enum(self.status, ProspectiveStatus, "option_evaluation.status")
        error = _optional_decimal(
            self.mean_absolute_error, "option_evaluation.mean_absolute_error"
        )
        invalid = exact_decimal(
            self.invalid_quote_fraction, "option_evaluation.invalid_quote_fraction"
        )
        object.__setattr__(self, "mean_absolute_error", error)
        object.__setattr__(self, "invalid_quote_fraction", invalid)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "protocol_hash": self.protocol_hash,
                    "observation_hashes": list(self.observation_hashes),
                    "evaluated_at": self.evaluated_at,
                    "valid_observations": self.valid_observations,
                    "invalid_observations": self.invalid_observations,
                    "mean_absolute_error": decimal_text(error)
                    if error is not None
                    else None,
                    "invalid_quote_fraction": decimal_text(invalid),
                    "status": self.status.value,
                },
                label="option_prospective_evaluation",
            ),
        )


def evaluate_option_prospective(
    protocol: OptionProspectiveProtocol,
    observations: Sequence[OptionProspectiveObservation],
    *,
    evaluated_at: str,
) -> OptionProspectiveEvaluation:
    evaluated = parse_timestamp(evaluated_at, "option_evaluation.evaluated_at")
    if evaluated < parse_timestamp(
        protocol.evaluation_end, "option_prospective.evaluation_end"
    ):
        raise DerivativeResearchError("option_evaluation_before_window_end")
    ids = [item.observation_id for item in observations]
    if len(ids) != len(set(ids)):
        raise DerivativeResearchError("option_evaluation_observation_duplicate")
    start = parse_timestamp(protocol.evaluation_start, "option_prospective.start")
    end = parse_timestamp(protocol.evaluation_end, "option_prospective.end")
    if any(
        not start
        <= parse_timestamp(item.observed_at, "option_observation.observed_at")
        <= end
        for item in observations
    ):
        raise DerivativeResearchError("option_evaluation_observation_outside_window")
    errors = [
        item.absolute_error for item in observations if item.absolute_error is not None
    ]
    valid = len(errors)
    invalid = len(observations) - valid
    mean_error = sum(errors, _ZERO) / valid if valid else None
    invalid_fraction = (
        Decimal(invalid) / Decimal(len(observations)) if observations else _ONE
    )
    if len(observations) < protocol.minimum_observations or not errors:
        status = ProspectiveStatus.INCONCLUSIVE
    elif invalid_fraction > protocol.maximum_invalid_quote_fraction:
        status = ProspectiveStatus.INVALIDATED
    elif mean_error is not None and mean_error > protocol.maximum_mean_absolute_error:
        status = ProspectiveStatus.DEGRADED
    else:
        status = ProspectiveStatus.CONFIRMED
    return OptionProspectiveEvaluation(
        protocol_hash=protocol.content_hash,
        observation_hashes=tuple(item.content_hash for item in observations),
        evaluated_at=evaluated_at,
        valid_observations=valid,
        invalid_observations=invalid,
        mean_absolute_error=mean_error,
        invalid_quote_fraction=invalid_fraction,
        status=status,
    )
