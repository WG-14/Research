"""First-class, point-in-time safe futures research contracts.

This module is intentionally independent from the spot backtest ledger.  A
continuous futures series may be used to form a signal, but every simulated
fill, settlement, margin movement, and roll is bound to an immutable listed
contract quote.  The module performs no network I/O and owns no account or
broker concepts; all inputs are externally prepared research evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from enum import StrEnum
from typing import Iterable, Sequence

from market_research.research.derivatives.common import (
    AvailabilityTimes,
    DerivativeResearchError,
    QualityResult,
    RunType,
    decimal_text,
    exact_decimal,
    parse_timestamp,
    require_confirmatory_quality,
    require_hash,
    require_stable_id,
)
from market_research.research.hashing import sha256_prefixed


FUTURES_RESEARCH_SCHEMA_VERSION = 1
_ZERO = Decimal("0")
_ONE = Decimal("1")
_DAYS_PER_YEAR = Decimal("365")


def _as_decimal(
    value: object,
    field_name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    result = exact_decimal(value, field_name, positive=positive)
    if nonnegative and result < 0:
        raise DerivativeResearchError(f"{field_name}_must_be_nonnegative")
    return result


def _hash_payload(label: str, payload: dict[str, object]) -> str:
    return sha256_prefixed(payload, label=label)


def _require_schema(schema_version: int) -> None:
    if schema_version != FUTURES_RESEARCH_SCHEMA_VERSION:
        raise DerivativeResearchError("futures_schema_unsupported")


def _require_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DerivativeResearchError(f"{field_name}_invalid_date") from exc


def _decimal_payload(value: Decimal) -> str:
    return decimal_text(value)


def _round_to_tick(price: Decimal, tick: Decimal, side: "OrderSide") -> Decimal:
    rounding = ROUND_CEILING if side is OrderSide.BUY else ROUND_FLOOR
    ticks = (price / tick).to_integral_value(rounding=rounding)
    return ticks * tick


class SettlementType(StrEnum):
    CASH_SETTLED = "CASH_SETTLED"
    PHYSICAL_SETTLED = "PHYSICAL_SETTLED"


class RollTrigger(StrEnum):
    DAYS_BEFORE_LAST_TRADE = "DAYS_BEFORE_LAST_TRADE"
    VOLUME_CROSSOVER = "VOLUME_CROSSOVER"
    OPEN_INTEREST_CROSSOVER = "OPEN_INTEREST_CROSSOVER"
    FIXED_CALENDAR = "FIXED_CALENDAR"
    COMPOSITE = "COMPOSITE"


class CompositeOperator(StrEnum):
    ANY = "ANY"
    ALL = "ALL"


class ContinuousAdjustment(StrEnum):
    SIMPLE_LINK = "SIMPLE_LINK"
    UNADJUSTED = "UNADJUSTED"
    DIFFERENCE = "DIFFERENCE"
    RATIO = "RATIO"


class AdjustmentDirection(StrEnum):
    NONE = "NONE"
    BACKWARD = "BACKWARD"
    FORWARD = "FORWARD"


class MarginCallAction(StrEnum):
    REDUCE_POSITION = "REDUCE_POSITION"
    VIRTUAL_MARGIN_CALL = "VIRTUAL_MARGIN_CALL"
    BLOCK_NEW_TRADES = "BLOCK_NEW_TRADES"
    FAIL_RESEARCH = "FAIL_RESEARCH"


class PhysicalDeliveryAction(StrEnum):
    FORCE_CLOSE = "FORCE_CLOSE"
    FAIL_RESEARCH = "FAIL_RESEARCH"


class SessionType(StrEnum):
    NIGHT = "NIGHT"
    DAY = "DAY"
    COMBINED = "COMBINED"


class MarketState(StrEnum):
    OPEN = "OPEN"
    LIMIT_UP = "LIMIT_UP"
    LIMIT_DOWN = "LIMIT_DOWN"
    HALTED = "HALTED"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class LifecycleEventType(StrEnum):
    LISTED = "LISTED"
    FIRST_TRADE = "FIRST_TRADE"
    FIRST_NOTICE = "FIRST_NOTICE"
    LAST_TRADE = "LAST_TRADE"
    FINAL_SETTLEMENT = "FINAL_SETTLEMENT"
    EXPIRATION = "EXPIRATION"


class FuturesStressKind(StrEnum):
    ROLL_POLICY = "ROLL_POLICY"
    CONTINUOUS_ADJUSTMENT = "CONTINUOUS_ADJUSTMENT"
    CONTRACT_VS_SIGNAL = "CONTRACT_VS_SIGNAL"
    ROLL_COST = "ROLL_COST"
    NEAR_EXPIRY_EXCLUSION = "NEAR_EXPIRY_EXCLUSION"
    CURVE_REGIME = "CURVE_REGIME"
    HIGH_VOL_LOW_LIQUIDITY = "HIGH_VOL_LOW_LIQUIDITY"
    NIGHT_SESSION = "NIGHT_SESSION"
    MARGIN_INCREASE = "MARGIN_INCREASE"
    PRICE_LIMIT_NO_EXIT = "PRICE_LIMIT_NO_EXIT"
    MULTIPLIER_TICK_REGIME = "MULTIPLIER_TICK_REGIME"
    SPREAD_LEGGING = "SPREAD_LEGGING"


@dataclass(frozen=True, slots=True)
class FuturesRoot:
    root_id: str
    symbol: str
    exchange_id: str
    underlying_id: str
    quote_currency: str
    calendar_id: str
    settlement_type: SettlementType
    root_version: str
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        for name, value in (
            ("root_id", self.root_id),
            ("symbol", self.symbol),
            ("exchange_id", self.exchange_id),
            ("underlying_id", self.underlying_id),
            ("quote_currency", self.quote_currency),
            ("calendar_id", self.calendar_id),
            ("root_version", self.root_version),
        ):
            require_stable_id(value, f"futures_root.{name}")
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_root", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "root_id": self.root_id,
            "symbol": self.symbol,
            "exchange_id": self.exchange_id,
            "underlying_id": self.underlying_id,
            "quote_currency": self.quote_currency,
            "calendar_id": self.calendar_id,
            "settlement_type": self.settlement_type.value,
            "root_version": self.root_version,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class FuturesContract:
    contract_id: str
    root_id: str
    listing_date: str
    first_trade_date: str
    last_trade_date: str
    first_notice_date: str | None
    final_settlement_date: str
    expiration_date: str
    contract_multiplier: Decimal
    tick_size: Decimal
    settlement_type: SettlementType
    spec_effective_at: str
    spec_version: str
    availability: AvailabilityTimes
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.contract_id, "futures_contract.contract_id")
        require_stable_id(self.root_id, "futures_contract.root_id")
        require_stable_id(self.spec_version, "futures_contract.spec_version")
        listing = _require_date(self.listing_date, "futures_contract.listing_date")
        first_trade = _require_date(
            self.first_trade_date, "futures_contract.first_trade_date"
        )
        last_trade = _require_date(
            self.last_trade_date, "futures_contract.last_trade_date"
        )
        final_settlement = _require_date(
            self.final_settlement_date,
            "futures_contract.final_settlement_date",
        )
        expiration = _require_date(
            self.expiration_date, "futures_contract.expiration_date"
        )
        first_notice = (
            None
            if self.first_notice_date is None
            else _require_date(
                self.first_notice_date, "futures_contract.first_notice_date"
            )
        )
        if not (
            listing <= first_trade <= last_trade <= final_settlement
            and last_trade <= expiration
        ):
            raise DerivativeResearchError("futures_contract_date_order_invalid")
        if first_notice is not None and not (
            first_trade <= first_notice <= final_settlement
        ):
            raise DerivativeResearchError("futures_contract_first_notice_invalid")
        if (
            self.settlement_type is SettlementType.PHYSICAL_SETTLED
            and first_notice is None
        ):
            raise DerivativeResearchError(
                "physical_futures_first_notice_date_required"
            )
        multiplier = _as_decimal(
            self.contract_multiplier,
            "futures_contract.contract_multiplier",
            positive=True,
        )
        tick = _as_decimal(
            self.tick_size, "futures_contract.tick_size", positive=True
        )
        object.__setattr__(self, "contract_multiplier", multiplier)
        object.__setattr__(self, "tick_size", tick)
        effective = parse_timestamp(
            self.spec_effective_at, "futures_contract.spec_effective_at"
        )
        if effective.date() > last_trade:
            raise DerivativeResearchError("futures_contract_spec_effective_too_late")
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_contract", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "root_id": self.root_id,
            "listing_date": self.listing_date,
            "first_trade_date": self.first_trade_date,
            "last_trade_date": self.last_trade_date,
            "first_notice_date": self.first_notice_date,
            "final_settlement_date": self.final_settlement_date,
            "expiration_date": self.expiration_date,
            "contract_multiplier": _decimal_payload(self.contract_multiplier),
            "tick_size": _decimal_payload(self.tick_size),
            "settlement_type": self.settlement_type.value,
            "spec_effective_at": self.spec_effective_at,
            "spec_version": self.spec_version,
            "availability": self.availability.as_dict(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def tradable_at(self, as_of: str) -> bool:
        instant = parse_timestamp(as_of, "futures_contract.as_of")
        return (
            self.availability.known_at(as_of)
            and _require_date(self.first_trade_date, "first_trade")
            <= instant.date()
            <= _require_date(self.last_trade_date, "last_trade")
        )


@dataclass(frozen=True, slots=True)
class RollPolicy:
    policy_id: str
    policy_version: str
    trigger: RollTrigger
    days_before_last_trade: int | None = None
    crossover_ratio: Decimal = _ONE
    consecutive_observations: int = 1
    fixed_roll_dates: tuple[str, ...] = ()
    composite_operator: CompositeOperator = CompositeOperator.ANY
    forbid_future_observations: bool = True
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.policy_id, "roll_policy.policy_id")
        require_stable_id(self.policy_version, "roll_policy.policy_version")
        ratio = _as_decimal(
            self.crossover_ratio, "roll_policy.crossover_ratio", positive=True
        )
        object.__setattr__(self, "crossover_ratio", ratio)
        if not self.forbid_future_observations:
            raise DerivativeResearchError("future_roll_observations_must_be_forbidden")
        if self.consecutive_observations <= 0:
            raise DerivativeResearchError("roll_policy_consecutive_invalid")
        if self.days_before_last_trade is not None and self.days_before_last_trade < 0:
            raise DerivativeResearchError("roll_policy_days_before_invalid")
        if self.trigger in {
            RollTrigger.DAYS_BEFORE_LAST_TRADE,
            RollTrigger.COMPOSITE,
        } and self.days_before_last_trade is None:
            raise DerivativeResearchError("roll_policy_days_before_required")
        if self.trigger is RollTrigger.FIXED_CALENDAR and not self.fixed_roll_dates:
            raise DerivativeResearchError("roll_policy_fixed_dates_required")
        parsed_dates = [
            _require_date(value, "roll_policy.fixed_roll_date")
            for value in self.fixed_roll_dates
        ]
        if parsed_dates != sorted(set(parsed_dates)):
            raise DerivativeResearchError("roll_policy_fixed_dates_not_unique_sorted")
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_roll_policy", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "trigger": self.trigger.value,
            "days_before_last_trade": self.days_before_last_trade,
            "crossover_ratio": _decimal_payload(self.crossover_ratio),
            "consecutive_observations": self.consecutive_observations,
            "fixed_roll_dates": list(self.fixed_roll_dates),
            "composite_operator": self.composite_operator.value,
            "forbid_future_observations": self.forbid_future_observations,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class ContinuousFuturesPolicy:
    series_id: str
    root_id: str
    policy_version: str
    roll_policy_hash: str
    adjustment: ContinuousAdjustment
    adjustment_direction: AdjustmentDirection
    signal_only: bool = True
    prospective_append_only: bool = True
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.series_id, "continuous_policy.series_id")
        require_stable_id(self.root_id, "continuous_policy.root_id")
        require_stable_id(self.policy_version, "continuous_policy.policy_version")
        require_hash(self.roll_policy_hash, "continuous_policy.roll_policy_hash")
        if not self.signal_only:
            raise DerivativeResearchError("continuous_futures_must_be_signal_only")
        if not self.prospective_append_only:
            raise DerivativeResearchError(
                "prospective_continuous_series_must_be_append_only"
            )
        if self.adjustment in {
            ContinuousAdjustment.SIMPLE_LINK,
            ContinuousAdjustment.UNADJUSTED,
        }:
            if self.adjustment_direction is not AdjustmentDirection.NONE:
                raise DerivativeResearchError(
                    "unadjusted_continuous_direction_must_be_none"
                )
        elif self.adjustment_direction is AdjustmentDirection.NONE:
            raise DerivativeResearchError(
                "adjusted_continuous_direction_required"
            )
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("continuous_futures_policy", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "series_id": self.series_id,
            "root_id": self.root_id,
            "policy_version": self.policy_version,
            "roll_policy_hash": self.roll_policy_hash,
            "adjustment": self.adjustment.value,
            "adjustment_direction": self.adjustment_direction.value,
            "signal_only": self.signal_only,
            "prospective_append_only": self.prospective_append_only,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class SettlementPolicy:
    policy_id: str
    policy_version: str
    settlement_price_field: str
    daily_mark_to_market: bool
    realize_variation_margin_daily: bool
    collateral_annual_rate: Decimal = _ZERO
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.policy_id, "settlement_policy.policy_id")
        require_stable_id(self.policy_version, "settlement_policy.policy_version")
        if self.settlement_price_field != "settlement_price":
            raise DerivativeResearchError("settlement_price_field_must_be_explicit")
        if not self.daily_mark_to_market or not self.realize_variation_margin_daily:
            raise DerivativeResearchError("futures_daily_settlement_required")
        rate = _as_decimal(
            self.collateral_annual_rate,
            "settlement_policy.collateral_annual_rate",
        )
        object.__setattr__(self, "collateral_annual_rate", rate)
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_settlement_policy", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "settlement_price_field": self.settlement_price_field,
            "daily_mark_to_market": self.daily_mark_to_market,
            "realize_variation_margin_daily": self.realize_variation_margin_daily,
            "collateral_annual_rate": _decimal_payload(
                self.collateral_annual_rate
            ),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class MarginSimulationPolicy:
    policy_id: str
    policy_version: str
    initial_margin_per_contract: Decimal
    maintenance_margin_per_contract: Decimal
    collateral_fraction: Decimal
    margin_call_action: MarginCallAction
    variation_margin_enabled: bool = True
    research_only: bool = True
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.policy_id, "margin_policy.policy_id")
        require_stable_id(self.policy_version, "margin_policy.policy_version")
        initial = _as_decimal(
            self.initial_margin_per_contract,
            "margin_policy.initial_margin_per_contract",
            positive=True,
        )
        maintenance = _as_decimal(
            self.maintenance_margin_per_contract,
            "margin_policy.maintenance_margin_per_contract",
            positive=True,
        )
        collateral = _as_decimal(
            self.collateral_fraction,
            "margin_policy.collateral_fraction",
            positive=True,
        )
        if maintenance > initial:
            raise DerivativeResearchError("maintenance_margin_exceeds_initial")
        if collateral > _ONE:
            raise DerivativeResearchError("collateral_fraction_exceeds_one")
        if not self.variation_margin_enabled or not self.research_only:
            raise DerivativeResearchError("margin_policy_must_be_research_simulation")
        object.__setattr__(self, "initial_margin_per_contract", initial)
        object.__setattr__(self, "maintenance_margin_per_contract", maintenance)
        object.__setattr__(self, "collateral_fraction", collateral)
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_margin_policy", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "initial_margin_per_contract": _decimal_payload(
                self.initial_margin_per_contract
            ),
            "maintenance_margin_per_contract": _decimal_payload(
                self.maintenance_margin_per_contract
            ),
            "collateral_fraction": _decimal_payload(self.collateral_fraction),
            "margin_call_action": self.margin_call_action.value,
            "variation_margin_enabled": self.variation_margin_enabled,
            "research_only": self.research_only,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class ExpiryPolicy:
    policy_id: str
    policy_version: str
    exit_days_before_first_notice: int
    exit_days_before_last_trade: int
    physical_delivery_action: PhysicalDeliveryAction
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.policy_id, "expiry_policy.policy_id")
        require_stable_id(self.policy_version, "expiry_policy.policy_version")
        if (
            self.exit_days_before_first_notice < 0
            or self.exit_days_before_last_trade < 0
        ):
            raise DerivativeResearchError("expiry_policy_exit_days_invalid")
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_expiry_policy", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "exit_days_before_first_notice": self.exit_days_before_first_notice,
            "exit_days_before_last_trade": self.exit_days_before_last_trade,
            "physical_delivery_action": self.physical_delivery_action.value,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class FuturesCostPolicy:
    policy_id: str
    policy_version: str
    commission_per_contract: Decimal
    execution_slippage_ticks: Decimal
    roll_slippage_ticks: Decimal
    spread_legging_ticks: Decimal
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.policy_id, "futures_cost.policy_id")
        require_stable_id(self.policy_version, "futures_cost.policy_version")
        for name in (
            "commission_per_contract",
            "execution_slippage_ticks",
            "roll_slippage_ticks",
            "spread_legging_ticks",
        ):
            value = _as_decimal(
                getattr(self, name), f"futures_cost.{name}", nonnegative=True
            )
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_cost_policy", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "commission_per_contract": _decimal_payload(
                self.commission_per_contract
            ),
            "execution_slippage_ticks": _decimal_payload(
                self.execution_slippage_ticks
            ),
            "roll_slippage_ticks": _decimal_payload(self.roll_slippage_ticks),
            "spread_legging_ticks": _decimal_payload(self.spread_legging_ticks),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class FuturesLifecycleEvent:
    event_id: str
    contract_id: str
    event_type: LifecycleEventType
    event_at: str
    availability: AvailabilityTimes
    source_hash: str
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.event_id, "futures_lifecycle.event_id")
        require_stable_id(self.contract_id, "futures_lifecycle.contract_id")
        require_hash(self.source_hash, "futures_lifecycle.source_hash")
        event = parse_timestamp(self.event_at, "futures_lifecycle.event_at")
        if event != parse_timestamp(
            self.availability.event_at, "futures_lifecycle.availability.event_at"
        ):
            raise DerivativeResearchError("lifecycle_event_time_mismatch")
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_lifecycle_event", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "contract_id": self.contract_id,
            "event_type": self.event_type.value,
            "event_at": self.event_at,
            "availability": self.availability.as_dict(),
            "source_hash": self.source_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class ContractQuote:
    quote_id: str
    contract_id: str
    root_id: str
    observed_at: str
    trading_date: str
    session: SessionType
    session_sequence: int
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    settlement_price: Decimal
    volume: Decimal
    open_interest: Decimal
    availability: AvailabilityTimes
    source_hash: str
    market_state: MarketState = MarketState.OPEN
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    limit_up_price: Decimal | None = None
    limit_down_price: Decimal | None = None
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.quote_id, "contract_quote.quote_id")
        require_stable_id(self.contract_id, "contract_quote.contract_id")
        require_stable_id(self.root_id, "contract_quote.root_id")
        require_hash(self.source_hash, "contract_quote.source_hash")
        observed = parse_timestamp(self.observed_at, "contract_quote.observed_at")
        _require_date(self.trading_date, "contract_quote.trading_date")
        if self.session_sequence < 0:
            raise DerivativeResearchError("contract_quote_session_sequence_invalid")
        if observed != parse_timestamp(
            self.availability.event_at, "contract_quote.availability.event_at"
        ):
            raise DerivativeResearchError("contract_quote_event_time_mismatch")
        for name in (
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "settlement_price",
        ):
            value = _as_decimal(
                getattr(self, name), f"contract_quote.{name}", positive=True
            )
            object.__setattr__(self, name, value)
        for name in ("volume", "open_interest"):
            value = _as_decimal(
                getattr(self, name), f"contract_quote.{name}", nonnegative=True
            )
            object.__setattr__(self, name, value)
        for name in (
            "bid_price",
            "ask_price",
            "limit_up_price",
            "limit_down_price",
        ):
            raw = getattr(self, name)
            if raw is not None:
                object.__setattr__(
                    self,
                    name,
                    _as_decimal(raw, f"contract_quote.{name}", positive=True),
                )
        if not self.low_price <= min(
            self.open_price, self.close_price, self.settlement_price
        ) <= max(self.open_price, self.close_price, self.settlement_price) <= self.high_price:
            raise DerivativeResearchError("contract_quote_ohlc_range_invalid")
        if (
            self.bid_price is not None
            and self.ask_price is not None
            and self.bid_price > self.ask_price
        ):
            raise DerivativeResearchError("contract_quote_crossed_market")
        if self.market_state is MarketState.LIMIT_UP and self.limit_up_price is None:
            raise DerivativeResearchError("limit_up_price_required")
        if (
            self.market_state is MarketState.LIMIT_DOWN
            and self.limit_down_price is None
        ):
            raise DerivativeResearchError("limit_down_price_required")
        if (
            self.limit_up_price is not None
            and self.limit_down_price is not None
            and self.limit_down_price >= self.limit_up_price
        ):
            raise DerivativeResearchError("contract_quote_price_limits_invalid")
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_contract_quote", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        optional_prices = {
            name: None if value is None else _decimal_payload(value)
            for name, value in (
                ("bid_price", self.bid_price),
                ("ask_price", self.ask_price),
                ("limit_up_price", self.limit_up_price),
                ("limit_down_price", self.limit_down_price),
            )
        }
        return {
            "schema_version": self.schema_version,
            "quote_id": self.quote_id,
            "contract_id": self.contract_id,
            "root_id": self.root_id,
            "observed_at": self.observed_at,
            "trading_date": self.trading_date,
            "session": self.session.value,
            "session_sequence": self.session_sequence,
            "open_price": _decimal_payload(self.open_price),
            "high_price": _decimal_payload(self.high_price),
            "low_price": _decimal_payload(self.low_price),
            "close_price": _decimal_payload(self.close_price),
            "settlement_price": _decimal_payload(self.settlement_price),
            "volume": _decimal_payload(self.volume),
            "open_interest": _decimal_payload(self.open_interest),
            "availability": self.availability.as_dict(),
            "source_hash": self.source_hash,
            "market_state": self.market_state.value,
            **optional_prices,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def known_at(self, as_of: str) -> bool:
        return (
            parse_timestamp(self.observed_at, "contract_quote.observed_at")
            <= parse_timestamp(as_of, "contract_quote.as_of")
            and self.availability.known_at(as_of)
        )

    def require_executable(self, side: OrderSide) -> None:
        if self.market_state is MarketState.HALTED:
            raise DerivativeResearchError("futures_market_halted")
        if self.market_state is MarketState.LIMIT_UP and side is OrderSide.BUY:
            raise DerivativeResearchError("futures_limit_up_buy_unavailable")
        if self.market_state is MarketState.LIMIT_DOWN and side is OrderSide.SELL:
            raise DerivativeResearchError("futures_limit_down_sell_unavailable")


@dataclass(frozen=True, slots=True)
class ContractChainSnapshot:
    snapshot_id: str
    root_id: str
    observed_at: str
    availability: AvailabilityTimes
    contracts: tuple[FuturesContract, ...]
    quotes: tuple[ContractQuote, ...]
    lifecycle_events: tuple[FuturesLifecycleEvent, ...]
    quality_results: tuple[QualityResult, ...]
    source_manifest_hashes: tuple[str, ...]
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.snapshot_id, "contract_chain.snapshot_id")
        require_stable_id(self.root_id, "contract_chain.root_id")
        observed = parse_timestamp(self.observed_at, "contract_chain.observed_at")
        if observed != parse_timestamp(
            self.availability.event_at, "contract_chain.availability.event_at"
        ):
            raise DerivativeResearchError("contract_chain_event_time_mismatch")
        if not self.contracts or not self.quotes:
            raise DerivativeResearchError("contract_chain_contracts_and_quotes_required")
        contract_ids = [item.contract_id for item in self.contracts]
        if len(contract_ids) != len(set(contract_ids)):
            raise DerivativeResearchError("contract_chain_contract_duplicate")
        if any(item.root_id != self.root_id for item in self.contracts):
            raise DerivativeResearchError("contract_chain_contract_root_mismatch")
        quote_keys = [
            (item.contract_id, item.observed_at, item.session.value)
            for item in self.quotes
        ]
        if len(quote_keys) != len(set(quote_keys)):
            raise DerivativeResearchError("contract_chain_quote_duplicate")
        for quote in self.quotes:
            if quote.contract_id not in contract_ids or quote.root_id != self.root_id:
                raise DerivativeResearchError("contract_chain_quote_orphan")
            if parse_timestamp(quote.observed_at, "contract_quote.observed_at") > observed:
                raise DerivativeResearchError("contract_chain_quote_from_future")
        for event in self.lifecycle_events:
            if event.contract_id not in contract_ids:
                raise DerivativeResearchError("contract_chain_lifecycle_orphan")
        if not self.source_manifest_hashes:
            raise DerivativeResearchError("contract_chain_source_manifest_required")
        if len(self.source_manifest_hashes) != len(set(self.source_manifest_hashes)):
            raise DerivativeResearchError("contract_chain_source_manifest_duplicate")
        for source_hash in self.source_manifest_hashes:
            require_hash(source_hash, "contract_chain.source_manifest_hash")
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_contract_chain", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "root_id": self.root_id,
            "observed_at": self.observed_at,
            "availability": self.availability.as_dict(),
            "contracts": [item.as_dict() for item in self.contracts],
            "quotes": [item.as_dict() for item in self.quotes],
            "lifecycle_events": [item.as_dict() for item in self.lifecycle_events],
            "quality_results": [item.as_dict() for item in self.quality_results],
            "source_manifest_hashes": list(self.source_manifest_hashes),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def admit(self, run_type: RunType) -> None:
        if run_type in {RunType.CONFIRMATORY, RunType.PROSPECTIVE}:
            require_confirmatory_quality(self.quality_results)

    def quote_for(self, contract_id: str, as_of: str) -> ContractQuote:
        candidates = [
            item
            for item in self.quotes
            if item.contract_id == contract_id and item.known_at(as_of)
        ]
        if not candidates:
            raise DerivativeResearchError(
                f"contract_quote_not_available_as_of:{contract_id}"
            )
        return max(
            candidates,
            key=lambda item: (
                parse_timestamp(item.observed_at, "contract_quote.observed_at"),
                item.session_sequence,
                item.content_hash,
            ),
        )

    def listed_contracts(self, as_of: str) -> tuple[FuturesContract, ...]:
        result = []
        for contract in self.contracts:
            if not contract.tradable_at(as_of):
                continue
            try:
                self.quote_for(contract.contract_id, as_of)
            except DerivativeResearchError:
                continue
            result.append(contract)
        return tuple(
            sorted(
                result,
                key=lambda item: (
                    _require_date(item.last_trade_date, "last_trade"),
                    item.contract_id,
                ),
            )
        )


def select_chain_as_of(
    snapshots: Sequence[ContractChainSnapshot], as_of: str
) -> ContractChainSnapshot:
    decision_time = parse_timestamp(as_of, "contract_chain.as_of")
    eligible = [
        item
        for item in snapshots
        if parse_timestamp(item.observed_at, "contract_chain.observed_at")
        <= decision_time
        and item.availability.known_at(as_of)
    ]
    if not eligible:
        raise DerivativeResearchError("contract_chain_not_available_as_of")
    root_ids = {item.root_id for item in eligible}
    if len(root_ids) != 1:
        raise DerivativeResearchError("contract_chain_mixed_roots")
    return max(
        eligible,
        key=lambda item: (
            parse_timestamp(item.observed_at, "contract_chain.observed_at"),
            item.availability.available_at,
            item.content_hash,
        ),
    )


@dataclass(frozen=True, slots=True)
class RollDecision:
    decision_id: str
    decision_at: str
    root_id: str
    from_contract_id: str
    to_contract_id: str
    should_roll: bool
    reason: str
    policy_hash: str
    chain_snapshot_hash: str
    input_quote_hashes: tuple[str, ...]
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        for name, value in (
            ("decision_id", self.decision_id),
            ("root_id", self.root_id),
            ("from_contract_id", self.from_contract_id),
            ("to_contract_id", self.to_contract_id),
            ("reason", self.reason),
        ):
            require_stable_id(value, f"roll_decision.{name}")
        parse_timestamp(self.decision_at, "roll_decision.decision_at")
        require_hash(self.policy_hash, "roll_decision.policy_hash")
        require_hash(self.chain_snapshot_hash, "roll_decision.chain_snapshot_hash")
        if not self.input_quote_hashes:
            raise DerivativeResearchError("roll_decision_input_quotes_required")
        for item in self.input_quote_hashes:
            require_hash(item, "roll_decision.input_quote_hash")
        if self.should_roll == (self.from_contract_id == self.to_contract_id):
            raise DerivativeResearchError("roll_decision_contract_transition_invalid")
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_roll_decision", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "decision_at": self.decision_at,
            "root_id": self.root_id,
            "from_contract_id": self.from_contract_id,
            "to_contract_id": self.to_contract_id,
            "should_roll": self.should_roll,
            "reason": self.reason,
            "policy_hash": self.policy_hash,
            "chain_snapshot_hash": self.chain_snapshot_hash,
            "input_quote_hashes": list(self.input_quote_hashes),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def _roll_condition(
    policy: RollPolicy,
    contract: FuturesContract,
    current_quote: ContractQuote,
    deferred_quote: ContractQuote,
    decision_date: date,
) -> tuple[bool, str]:
    last_trade = _require_date(contract.last_trade_date, "last_trade")
    calendar_condition = (
        policy.days_before_last_trade is not None
        and decision_date
        >= last_trade - timedelta(days=policy.days_before_last_trade)
    )
    volume_condition = (
        deferred_quote.volume >= current_quote.volume * policy.crossover_ratio
    )
    oi_condition = (
        deferred_quote.open_interest
        >= current_quote.open_interest * policy.crossover_ratio
    )
    fixed_condition = any(
        _require_date(item, "fixed_roll_date") == decision_date
        for item in policy.fixed_roll_dates
    )
    if policy.trigger is RollTrigger.DAYS_BEFORE_LAST_TRADE:
        return calendar_condition, "CALENDAR"
    if policy.trigger is RollTrigger.VOLUME_CROSSOVER:
        return volume_condition, "VOLUME_CROSSOVER"
    if policy.trigger is RollTrigger.OPEN_INTEREST_CROSSOVER:
        return oi_condition, "OPEN_INTEREST_CROSSOVER"
    if policy.trigger is RollTrigger.FIXED_CALENDAR:
        return fixed_condition, "FIXED_CALENDAR"
    conditions = (calendar_condition, volume_condition, oi_condition)
    decision = (
        all(conditions)
        if policy.composite_operator is CompositeOperator.ALL
        else any(conditions)
    )
    return decision, f"COMPOSITE_{policy.composite_operator.value}"


def decide_roll(
    snapshots: Sequence[ContractChainSnapshot],
    policy: RollPolicy,
    *,
    as_of: str,
    current_contract_id: str,
    decision_id: str,
) -> RollDecision:
    """Make a roll decision from inputs that were available by ``as_of`` only."""

    require_stable_id(decision_id, "roll_decision.decision_id")
    decision_time = parse_timestamp(as_of, "roll_decision.as_of")
    chain = select_chain_as_of(snapshots, as_of)
    contracts = {item.contract_id: item for item in chain.listed_contracts(as_of)}
    current = contracts.get(current_contract_id)
    if current is None:
        raise DerivativeResearchError("roll_current_contract_not_tradable_as_of")
    deferred = [
        item
        for item in contracts.values()
        if _require_date(item.last_trade_date, "last_trade")
        > _require_date(current.last_trade_date, "last_trade")
    ]
    if not deferred:
        current_quote = chain.quote_for(current_contract_id, as_of)
        return RollDecision(
            decision_id=decision_id,
            decision_at=as_of,
            root_id=chain.root_id,
            from_contract_id=current_contract_id,
            to_contract_id=current_contract_id,
            should_roll=False,
            reason="NO_DEFERRED_CONTRACT",
            policy_hash=policy.content_hash,
            chain_snapshot_hash=chain.content_hash,
            input_quote_hashes=(current_quote.content_hash,),
        )
    next_contract = min(
        deferred,
        key=lambda item: (
            _require_date(item.last_trade_date, "last_trade"), item.contract_id
        ),
    )

    eligible_snapshots = sorted(
        (
            item
            for item in snapshots
            if item.root_id == chain.root_id
            and parse_timestamp(item.observed_at, "contract_chain.observed_at")
            <= decision_time
            and item.availability.known_at(as_of)
        ),
        key=lambda item: parse_timestamp(
            item.observed_at, "contract_chain.observed_at"
        ),
        reverse=True,
    )
    observations: list[tuple[bool, str, ContractQuote, ContractQuote]] = []
    for snapshot in eligible_snapshots:
        try:
            current_quote = snapshot.quote_for(current_contract_id, as_of)
            deferred_quote = snapshot.quote_for(next_contract.contract_id, as_of)
        except DerivativeResearchError:
            continue
        condition, reason = _roll_condition(
            policy,
            current,
            current_quote,
            deferred_quote,
            parse_timestamp(snapshot.observed_at, "contract_chain.observed_at").date(),
        )
        observations.append((condition, reason, current_quote, deferred_quote))
        if len(observations) >= policy.consecutive_observations:
            break
    latest_current = chain.quote_for(current_contract_id, as_of)
    latest_deferred = chain.quote_for(next_contract.contract_id, as_of)
    should_roll = (
        len(observations) == policy.consecutive_observations
        and all(item[0] for item in observations)
    )
    reason = observations[0][1] if observations else "INSUFFICIENT_OBSERVATIONS"
    if not should_roll:
        reason = f"{reason}_NOT_TRIGGERED"
    evidence_hashes = tuple(
        dict.fromkeys(
            value
            for item in observations
            for value in (item[2].content_hash, item[3].content_hash)
        )
    ) or (latest_current.content_hash, latest_deferred.content_hash)
    return RollDecision(
        decision_id=decision_id,
        decision_at=as_of,
        root_id=chain.root_id,
        from_contract_id=current_contract_id,
        to_contract_id=(
            next_contract.contract_id if should_roll else current_contract_id
        ),
        should_roll=should_roll,
        reason=reason,
        policy_hash=policy.content_hash,
        chain_snapshot_hash=chain.content_hash,
        input_quote_hashes=evidence_hashes,
    )


@dataclass(frozen=True, slots=True)
class ContinuousFuturesPoint:
    point_id: str
    series_id: str
    root_id: str
    observed_at: str
    source_contract_id: str
    source_quote_hash: str
    source_price: Decimal
    continuous_price: Decimal
    additive_adjustment: Decimal
    multiplicative_adjustment: Decimal
    roll_gap: Decimal
    policy_hash: str
    roll_decision_hash: str
    chain_snapshot_hash: str
    previous_point_hash: str | None
    signal_only: bool = True
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        for name, value in (
            ("point_id", self.point_id),
            ("series_id", self.series_id),
            ("root_id", self.root_id),
            ("source_contract_id", self.source_contract_id),
        ):
            require_stable_id(value, f"continuous_point.{name}")
        parse_timestamp(self.observed_at, "continuous_point.observed_at")
        for name, value in (
            ("source_quote_hash", self.source_quote_hash),
            ("policy_hash", self.policy_hash),
            ("roll_decision_hash", self.roll_decision_hash),
            ("chain_snapshot_hash", self.chain_snapshot_hash),
        ):
            require_hash(value, f"continuous_point.{name}")
        if self.previous_point_hash is not None:
            require_hash(
                self.previous_point_hash, "continuous_point.previous_point_hash"
            )
        if not self.signal_only:
            raise DerivativeResearchError("continuous_point_must_be_signal_only")
        for name in (
            "source_price",
            "continuous_price",
            "multiplicative_adjustment",
        ):
            parsed_decimal = _as_decimal(
                getattr(self, name), f"continuous_point.{name}", positive=True
            )
            object.__setattr__(self, name, parsed_decimal)
        for name in ("additive_adjustment", "roll_gap"):
            object.__setattr__(
                self,
                name,
                _as_decimal(getattr(self, name), f"continuous_point.{name}"),
            )
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("continuous_futures_point", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "point_id": self.point_id,
            "series_id": self.series_id,
            "root_id": self.root_id,
            "observed_at": self.observed_at,
            "source_contract_id": self.source_contract_id,
            "source_quote_hash": self.source_quote_hash,
            "source_price": _decimal_payload(self.source_price),
            "continuous_price": _decimal_payload(self.continuous_price),
            "additive_adjustment": _decimal_payload(self.additive_adjustment),
            "multiplicative_adjustment": _decimal_payload(
                self.multiplicative_adjustment
            ),
            "roll_gap": _decimal_payload(self.roll_gap),
            "policy_hash": self.policy_hash,
            "roll_decision_hash": self.roll_decision_hash,
            "chain_snapshot_hash": self.chain_snapshot_hash,
            "previous_point_hash": self.previous_point_hash,
            "signal_only": self.signal_only,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def build_continuous_point(
    snapshots: Sequence[ContractChainSnapshot],
    roll_policy: RollPolicy,
    continuous_policy: ContinuousFuturesPolicy,
    *,
    as_of: str,
    current_contract_id: str,
    point_id: str,
    decision_id: str,
    previous_point: ContinuousFuturesPoint | None = None,
    prospective: bool = False,
) -> tuple[ContinuousFuturesPoint, RollDecision]:
    if continuous_policy.roll_policy_hash != roll_policy.content_hash:
        raise DerivativeResearchError("continuous_roll_policy_hash_mismatch")
    if (
        prospective
        and continuous_policy.adjustment_direction is AdjustmentDirection.BACKWARD
    ):
        raise DerivativeResearchError(
            "prospective_backward_adjustment_would_rewrite_history"
        )
    if previous_point is not None:
        if previous_point.series_id != continuous_policy.series_id:
            raise DerivativeResearchError("continuous_previous_series_mismatch")
        if parse_timestamp(previous_point.observed_at, "previous.observed_at") >= (
            parse_timestamp(as_of, "continuous.as_of")
        ):
            raise DerivativeResearchError("continuous_points_not_append_only")
        if previous_point.source_contract_id != current_contract_id:
            raise DerivativeResearchError(
                "continuous_current_contract_not_previous_mapping"
            )
    chain = select_chain_as_of(snapshots, as_of)
    if chain.root_id != continuous_policy.root_id:
        raise DerivativeResearchError("continuous_policy_root_mismatch")
    decision = decide_roll(
        snapshots,
        roll_policy,
        as_of=as_of,
        current_contract_id=current_contract_id,
        decision_id=decision_id,
    )
    selected_id = decision.to_contract_id
    selected_quote = chain.quote_for(selected_id, as_of)
    source_price = selected_quote.close_price
    additive = previous_point.additive_adjustment if previous_point else _ZERO
    multiplicative = (
        previous_point.multiplicative_adjustment if previous_point else _ONE
    )
    roll_gap = _ZERO
    if decision.should_roll:
        old_quote = chain.quote_for(decision.from_contract_id, as_of)
        roll_gap = selected_quote.close_price - old_quote.close_price
        if continuous_policy.adjustment is ContinuousAdjustment.DIFFERENCE:
            additive += old_quote.close_price - selected_quote.close_price
        elif continuous_policy.adjustment is ContinuousAdjustment.RATIO:
            multiplicative *= old_quote.close_price / selected_quote.close_price
    if continuous_policy.adjustment is ContinuousAdjustment.DIFFERENCE:
        continuous_price = source_price + additive
    elif continuous_policy.adjustment is ContinuousAdjustment.RATIO:
        continuous_price = source_price * multiplicative
    else:
        continuous_price = source_price
    return (
        ContinuousFuturesPoint(
            point_id=point_id,
            series_id=continuous_policy.series_id,
            root_id=continuous_policy.root_id,
            observed_at=as_of,
            source_contract_id=selected_id,
            source_quote_hash=selected_quote.content_hash,
            source_price=source_price,
            continuous_price=continuous_price,
            additive_adjustment=additive,
            multiplicative_adjustment=multiplicative,
            roll_gap=roll_gap,
            policy_hash=continuous_policy.content_hash,
            roll_decision_hash=decision.content_hash,
            chain_snapshot_hash=chain.content_hash,
            previous_point_hash=(
                None if previous_point is None else previous_point.content_hash
            ),
        ),
        decision,
    )


@dataclass(frozen=True, slots=True)
class BasisFeature:
    feature_id: str
    observed_at: str
    contract_id: str
    spot_price: Decimal
    futures_price: Decimal
    basis: Decimal
    basis_ratio: Decimal
    annualized_basis: Decimal
    days_to_expiration: int
    spot_availability: AvailabilityTimes
    futures_quote_hash: str
    feature_version: str
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.feature_id, "basis_feature.feature_id")
        require_stable_id(self.contract_id, "basis_feature.contract_id")
        require_stable_id(self.feature_version, "basis_feature.feature_version")
        require_hash(self.futures_quote_hash, "basis_feature.futures_quote_hash")
        parse_timestamp(self.observed_at, "basis_feature.observed_at")
        if self.days_to_expiration <= 0:
            raise DerivativeResearchError("basis_feature_expiration_days_invalid")
        for name in ("spot_price", "futures_price"):
            object.__setattr__(
                self,
                name,
                _as_decimal(getattr(self, name), f"basis_feature.{name}", positive=True),
            )
        for name in ("basis", "basis_ratio", "annualized_basis"):
            object.__setattr__(
                self,
                name,
                _as_decimal(getattr(self, name), f"basis_feature.{name}"),
            )
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_basis_feature", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "feature_id": self.feature_id,
            "observed_at": self.observed_at,
            "contract_id": self.contract_id,
            "spot_price": _decimal_payload(self.spot_price),
            "futures_price": _decimal_payload(self.futures_price),
            "basis": _decimal_payload(self.basis),
            "basis_ratio": _decimal_payload(self.basis_ratio),
            "annualized_basis": _decimal_payload(self.annualized_basis),
            "days_to_expiration": self.days_to_expiration,
            "spot_availability": self.spot_availability.as_dict(),
            "futures_quote_hash": self.futures_quote_hash,
            "feature_version": self.feature_version,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def compute_basis_feature(
    *,
    feature_id: str,
    feature_version: str,
    as_of: str,
    spot_price: Decimal,
    spot_availability: AvailabilityTimes,
    futures_quote: ContractQuote,
    contract: FuturesContract,
    max_event_skew_seconds: int = 0,
) -> BasisFeature:
    if max_event_skew_seconds < 0:
        raise DerivativeResearchError("basis_event_skew_invalid")
    if not spot_availability.known_at(as_of) or not futures_quote.known_at(as_of):
        raise DerivativeResearchError("basis_inputs_not_available_as_of")
    spot_event = parse_timestamp(spot_availability.event_at, "basis.spot_event")
    futures_event = parse_timestamp(futures_quote.observed_at, "basis.future_event")
    if abs((spot_event - futures_event).total_seconds()) > max_event_skew_seconds:
        raise DerivativeResearchError("basis_inputs_not_time_aligned")
    spot = _as_decimal(spot_price, "basis.spot_price", positive=True)
    days = (
        _require_date(contract.expiration_date, "expiration_date")
        - parse_timestamp(as_of, "basis.as_of").date()
    ).days
    if days <= 0:
        raise DerivativeResearchError("basis_contract_already_expired")
    basis = futures_quote.close_price - spot
    ratio = basis / spot
    annualized = ratio * _DAYS_PER_YEAR / Decimal(days)
    return BasisFeature(
        feature_id=feature_id,
        observed_at=as_of,
        contract_id=contract.contract_id,
        spot_price=spot,
        futures_price=futures_quote.close_price,
        basis=basis,
        basis_ratio=ratio,
        annualized_basis=annualized,
        days_to_expiration=days,
        spot_availability=spot_availability,
        futures_quote_hash=futures_quote.content_hash,
        feature_version=feature_version,
    )


@dataclass(frozen=True, slots=True)
class CurveFeature:
    feature_id: str
    observed_at: str
    near_contract_id: str
    deferred_contract_id: str
    near_price: Decimal
    deferred_price: Decimal
    calendar_spread: Decimal
    annualized_slope: Decimal
    curvature: Decimal | None
    input_quote_hashes: tuple[str, ...]
    feature_version: str
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        for name, value in (
            ("feature_id", self.feature_id),
            ("near_contract_id", self.near_contract_id),
            ("deferred_contract_id", self.deferred_contract_id),
            ("feature_version", self.feature_version),
        ):
            require_stable_id(value, f"curve_feature.{name}")
        parse_timestamp(self.observed_at, "curve_feature.observed_at")
        if self.near_contract_id == self.deferred_contract_id:
            raise DerivativeResearchError("curve_feature_distinct_contracts_required")
        if len(self.input_quote_hashes) not in {2, 3}:
            raise DerivativeResearchError("curve_feature_quote_count_invalid")
        for value in self.input_quote_hashes:
            require_hash(value, "curve_feature.input_quote_hash")
        for name in ("near_price", "deferred_price"):
            object.__setattr__(
                self,
                name,
                _as_decimal(getattr(self, name), f"curve_feature.{name}", positive=True),
            )
        for name in ("calendar_spread", "annualized_slope"):
            object.__setattr__(
                self,
                name,
                _as_decimal(getattr(self, name), f"curve_feature.{name}"),
            )
        if self.curvature is not None:
            object.__setattr__(
                self,
                "curvature",
                _as_decimal(self.curvature, "curve_feature.curvature"),
            )
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_curve_feature", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "feature_id": self.feature_id,
            "observed_at": self.observed_at,
            "near_contract_id": self.near_contract_id,
            "deferred_contract_id": self.deferred_contract_id,
            "near_price": _decimal_payload(self.near_price),
            "deferred_price": _decimal_payload(self.deferred_price),
            "calendar_spread": _decimal_payload(self.calendar_spread),
            "annualized_slope": _decimal_payload(self.annualized_slope),
            "curvature": (
                None if self.curvature is None else _decimal_payload(self.curvature)
            ),
            "input_quote_hashes": list(self.input_quote_hashes),
            "feature_version": self.feature_version,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def compute_curve_feature(
    *,
    feature_id: str,
    feature_version: str,
    as_of: str,
    near_quote: ContractQuote,
    deferred_quote: ContractQuote,
    near_contract: FuturesContract,
    deferred_contract: FuturesContract,
    third_quote: ContractQuote | None = None,
) -> CurveFeature:
    quotes = (near_quote, deferred_quote) + (
        () if third_quote is None else (third_quote,)
    )
    if any(not quote.known_at(as_of) for quote in quotes):
        raise DerivativeResearchError("curve_inputs_not_available_as_of")
    near_expiry = _require_date(near_contract.expiration_date, "near_expiration")
    deferred_expiry = _require_date(
        deferred_contract.expiration_date, "deferred_expiration"
    )
    tenor_days = (deferred_expiry - near_expiry).days
    if tenor_days <= 0:
        raise DerivativeResearchError("curve_contract_order_invalid")
    spread = deferred_quote.close_price - near_quote.close_price
    slope = spread / near_quote.close_price * _DAYS_PER_YEAR / Decimal(tenor_days)
    curvature = None
    if third_quote is not None:
        curvature = (
            third_quote.close_price
            - Decimal("2") * deferred_quote.close_price
            + near_quote.close_price
        )
    return CurveFeature(
        feature_id=feature_id,
        observed_at=as_of,
        near_contract_id=near_contract.contract_id,
        deferred_contract_id=deferred_contract.contract_id,
        near_price=near_quote.close_price,
        deferred_price=deferred_quote.close_price,
        calendar_spread=spread,
        annualized_slope=slope,
        curvature=curvature,
        input_quote_hashes=tuple(item.content_hash for item in quotes),
        feature_version=feature_version,
    )


@dataclass(frozen=True, slots=True)
class RollAttributionFeature:
    feature_id: str
    observed_at: str
    previous_contract_id: str
    current_contract_id: str
    continuous_return: Decimal
    contract_price_return: Decimal
    roll_return: Decimal
    settlement_return: Decimal | None
    execution_return: Decimal | None
    previous_point_hash: str
    current_point_hash: str
    input_quote_hashes: tuple[str, ...]
    feature_version: str
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        for name, value in (
            ("feature_id", self.feature_id),
            ("previous_contract_id", self.previous_contract_id),
            ("current_contract_id", self.current_contract_id),
            ("feature_version", self.feature_version),
        ):
            require_stable_id(value, f"roll_attribution.{name}")
        parse_timestamp(self.observed_at, "roll_attribution.observed_at")
        for value in (
            self.previous_point_hash,
            self.current_point_hash,
            *self.input_quote_hashes,
        ):
            require_hash(value, "roll_attribution.evidence_hash")
        for name in ("continuous_return", "contract_price_return", "roll_return"):
            object.__setattr__(
                self,
                name,
                _as_decimal(getattr(self, name), f"roll_attribution.{name}"),
            )
        for name in ("settlement_return", "execution_return"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    _as_decimal(value, f"roll_attribution.{name}"),
                )
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_roll_attribution", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "feature_id": self.feature_id,
            "observed_at": self.observed_at,
            "previous_contract_id": self.previous_contract_id,
            "current_contract_id": self.current_contract_id,
            "continuous_return": _decimal_payload(self.continuous_return),
            "contract_price_return": _decimal_payload(
                self.contract_price_return
            ),
            "roll_return": _decimal_payload(self.roll_return),
            "settlement_return": (
                None
                if self.settlement_return is None
                else _decimal_payload(self.settlement_return)
            ),
            "execution_return": (
                None
                if self.execution_return is None
                else _decimal_payload(self.execution_return)
            ),
            "previous_point_hash": self.previous_point_hash,
            "current_point_hash": self.current_point_hash,
            "input_quote_hashes": list(self.input_quote_hashes),
            "feature_version": self.feature_version,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def attribute_roll_return(
    *,
    feature_id: str,
    feature_version: str,
    previous_point: ContinuousFuturesPoint,
    current_point: ContinuousFuturesPoint,
    old_contract_quote_at_roll: ContractQuote,
    settlement_return: Decimal | None = None,
    execution_return: Decimal | None = None,
) -> RollAttributionFeature:
    if current_point.previous_point_hash != previous_point.content_hash:
        raise DerivativeResearchError("roll_attribution_point_chain_broken")
    if not old_contract_quote_at_roll.known_at(current_point.observed_at):
        raise DerivativeResearchError("roll_attribution_quote_not_available")
    continuous_return = (
        current_point.continuous_price / previous_point.continuous_price - _ONE
    )
    price_return = (
        old_contract_quote_at_roll.close_price / previous_point.source_price - _ONE
    )
    roll_return = continuous_return - price_return
    return RollAttributionFeature(
        feature_id=feature_id,
        observed_at=current_point.observed_at,
        previous_contract_id=previous_point.source_contract_id,
        current_contract_id=current_point.source_contract_id,
        continuous_return=continuous_return,
        contract_price_return=price_return,
        roll_return=roll_return,
        settlement_return=settlement_return,
        execution_return=execution_return,
        previous_point_hash=previous_point.content_hash,
        current_point_hash=current_point.content_hash,
        input_quote_hashes=(old_contract_quote_at_roll.content_hash,),
        feature_version=feature_version,
    )


@dataclass(frozen=True, slots=True)
class FuturesOrderIntent:
    intent_id: str
    contract_id: str
    side: OrderSide
    quantity: int
    decision_at: str
    signal_series_id: str | None = None
    signal_point_hash: str | None = None
    limit_price: Decimal | None = None
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.intent_id, "futures_intent.intent_id")
        require_stable_id(self.contract_id, "futures_intent.contract_id")
        parse_timestamp(self.decision_at, "futures_intent.decision_at")
        if self.quantity <= 0:
            raise DerivativeResearchError("futures_intent_quantity_invalid")
        if (self.signal_series_id is None) != (self.signal_point_hash is None):
            raise DerivativeResearchError("futures_intent_signal_binding_incomplete")
        if self.signal_series_id is not None:
            require_stable_id(
                self.signal_series_id, "futures_intent.signal_series_id"
            )
            require_hash(
                self.signal_point_hash or "", "futures_intent.signal_point_hash"
            )
        if self.limit_price is not None:
            object.__setattr__(
                self,
                "limit_price",
                _as_decimal(
                    self.limit_price, "futures_intent.limit_price", positive=True
                ),
            )
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_order_intent", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "intent_id": self.intent_id,
            "contract_id": self.contract_id,
            "side": self.side.value,
            "quantity": self.quantity,
            "decision_at": self.decision_at,
            "signal_series_id": self.signal_series_id,
            "signal_point_hash": self.signal_point_hash,
            "limit_price": (
                None if self.limit_price is None else _decimal_payload(self.limit_price)
            ),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class FuturesPosition:
    contract_id: str
    quantity: int
    average_entry_price: Decimal
    last_settlement_price: Decimal
    contract_spec_hash: str
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.contract_id, "futures_position.contract_id")
        if self.quantity == 0:
            raise DerivativeResearchError("zero_quantity_position_not_preserved")
        for name in ("average_entry_price", "last_settlement_price"):
            object.__setattr__(
                self,
                name,
                _as_decimal(
                    getattr(self, name), f"futures_position.{name}", positive=True
                ),
            )
        require_hash(self.contract_spec_hash, "futures_position.contract_spec_hash")
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_position", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "quantity": self.quantity,
            "average_entry_price": _decimal_payload(self.average_entry_price),
            "last_settlement_price": _decimal_payload(
                self.last_settlement_price
            ),
            "contract_spec_hash": self.contract_spec_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class FuturesFill:
    fill_id: str
    intent_hash: str
    contract_id: str
    quote_hash: str
    filled_at: str
    trading_date: str
    session: SessionType
    side: OrderSide
    quantity: int
    reference_price: Decimal
    fill_price: Decimal
    multiplier: Decimal
    commission: Decimal
    slippage_cost: Decimal
    realized_trade_pnl: Decimal
    is_roll_leg: bool
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.fill_id, "futures_fill.fill_id")
        require_stable_id(self.contract_id, "futures_fill.contract_id")
        require_hash(self.intent_hash, "futures_fill.intent_hash")
        require_hash(self.quote_hash, "futures_fill.quote_hash")
        parse_timestamp(self.filled_at, "futures_fill.filled_at")
        _require_date(self.trading_date, "futures_fill.trading_date")
        if self.quantity <= 0:
            raise DerivativeResearchError("futures_fill_quantity_invalid")
        for name in ("reference_price", "fill_price", "multiplier"):
            object.__setattr__(
                self,
                name,
                _as_decimal(getattr(self, name), f"futures_fill.{name}", positive=True),
            )
        for name in ("commission", "slippage_cost"):
            object.__setattr__(
                self,
                name,
                _as_decimal(
                    getattr(self, name), f"futures_fill.{name}", nonnegative=True
                ),
            )
        object.__setattr__(
            self,
            "realized_trade_pnl",
            _as_decimal(self.realized_trade_pnl, "futures_fill.realized_trade_pnl"),
        )
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_fill", self.identity_payload()),
        )

    @property
    def total_cost(self) -> Decimal:
        return self.commission + self.slippage_cost

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "fill_id": self.fill_id,
            "intent_hash": self.intent_hash,
            "contract_id": self.contract_id,
            "quote_hash": self.quote_hash,
            "filled_at": self.filled_at,
            "trading_date": self.trading_date,
            "session": self.session.value,
            "side": self.side.value,
            "quantity": self.quantity,
            "reference_price": _decimal_payload(self.reference_price),
            "fill_price": _decimal_payload(self.fill_price),
            "multiplier": _decimal_payload(self.multiplier),
            "commission": _decimal_payload(self.commission),
            "slippage_cost": _decimal_payload(self.slippage_cost),
            "realized_trade_pnl": _decimal_payload(self.realized_trade_pnl),
            "is_roll_leg": self.is_roll_leg,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class FuturesLedger:
    ledger_id: str
    initial_cash: Decimal
    cash_balance: Decimal
    positions: tuple[FuturesPosition, ...]
    cumulative_variation_margin: Decimal
    cumulative_fees: Decimal
    margin_call_count: int
    blocked_new_trades: bool
    failed: bool
    last_event_at: str | None
    last_trading_date: str | None
    last_session_sequence: int | None
    event_hashes: tuple[str, ...]
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.ledger_id, "futures_ledger.ledger_id")
        initial = _as_decimal(
            self.initial_cash, "futures_ledger.initial_cash", positive=True
        )
        cash = _as_decimal(self.cash_balance, "futures_ledger.cash_balance")
        variation = _as_decimal(
            self.cumulative_variation_margin,
            "futures_ledger.cumulative_variation_margin",
        )
        fees = _as_decimal(
            self.cumulative_fees,
            "futures_ledger.cumulative_fees",
            nonnegative=True,
        )
        object.__setattr__(self, "initial_cash", initial)
        object.__setattr__(self, "cash_balance", cash)
        object.__setattr__(self, "cumulative_variation_margin", variation)
        object.__setattr__(self, "cumulative_fees", fees)
        if self.margin_call_count < 0:
            raise DerivativeResearchError("futures_ledger_margin_call_count_invalid")
        position_ids = [item.contract_id for item in self.positions]
        if position_ids != sorted(set(position_ids)):
            raise DerivativeResearchError("futures_ledger_positions_not_unique_sorted")
        for value in self.event_hashes:
            require_hash(value, "futures_ledger.event_hash")
        timeline_fields = (
            self.last_event_at,
            self.last_trading_date,
            self.last_session_sequence,
        )
        if any(item is None for item in timeline_fields) and any(
            item is not None for item in timeline_fields
        ):
            raise DerivativeResearchError("futures_ledger_timeline_incomplete")
        if self.last_event_at is not None:
            parse_timestamp(self.last_event_at, "futures_ledger.last_event_at")
            _require_date(
                self.last_trading_date or "", "futures_ledger.last_trading_date"
            )
            if (self.last_session_sequence or 0) < 0:
                raise DerivativeResearchError(
                    "futures_ledger_session_sequence_invalid"
                )
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_ledger", self.identity_payload()),
        )

    @classmethod
    def open(cls, ledger_id: str, initial_cash: Decimal) -> "FuturesLedger":
        return cls(
            ledger_id=ledger_id,
            initial_cash=initial_cash,
            cash_balance=initial_cash,
            positions=(),
            cumulative_variation_margin=_ZERO,
            cumulative_fees=_ZERO,
            margin_call_count=0,
            blocked_new_trades=False,
            failed=False,
            last_event_at=None,
            last_trading_date=None,
            last_session_sequence=None,
            event_hashes=(),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "ledger_id": self.ledger_id,
            "initial_cash": _decimal_payload(self.initial_cash),
            "cash_balance": _decimal_payload(self.cash_balance),
            "positions": [item.as_dict() for item in self.positions],
            "cumulative_variation_margin": _decimal_payload(
                self.cumulative_variation_margin
            ),
            "cumulative_fees": _decimal_payload(self.cumulative_fees),
            "margin_call_count": self.margin_call_count,
            "blocked_new_trades": self.blocked_new_trades,
            "failed": self.failed,
            "last_event_at": self.last_event_at,
            "last_trading_date": self.last_trading_date,
            "last_session_sequence": self.last_session_sequence,
            "event_hashes": list(self.event_hashes),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def position_for(self, contract_id: str) -> FuturesPosition | None:
        return next(
            (item for item in self.positions if item.contract_id == contract_id),
            None,
        )


@dataclass(frozen=True, slots=True)
class SettlementEvent:
    event_id: str
    contract_id: str
    quote_hash: str
    settled_at: str
    previous_settlement_price: Decimal
    settlement_price: Decimal
    quantity: int
    multiplier: Decimal
    variation_margin: Decimal
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.event_id, "settlement_event.event_id")
        require_stable_id(self.contract_id, "settlement_event.contract_id")
        require_hash(self.quote_hash, "settlement_event.quote_hash")
        parse_timestamp(self.settled_at, "settlement_event.settled_at")
        if self.quantity == 0:
            raise DerivativeResearchError("settlement_event_quantity_invalid")
        for name in (
            "previous_settlement_price",
            "settlement_price",
            "multiplier",
        ):
            object.__setattr__(
                self,
                name,
                _as_decimal(
                    getattr(self, name), f"settlement_event.{name}", positive=True
                ),
            )
        object.__setattr__(
            self,
            "variation_margin",
            _as_decimal(self.variation_margin, "settlement_event.variation_margin"),
        )
        expected = (
            (self.settlement_price - self.previous_settlement_price)
            * self.multiplier
            * self.quantity
        )
        if self.variation_margin != expected:
            raise DerivativeResearchError("settlement_event_variation_margin_mismatch")
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_settlement_event", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "contract_id": self.contract_id,
            "quote_hash": self.quote_hash,
            "settled_at": self.settled_at,
            "previous_settlement_price": _decimal_payload(
                self.previous_settlement_price
            ),
            "settlement_price": _decimal_payload(self.settlement_price),
            "quantity": self.quantity,
            "multiplier": _decimal_payload(self.multiplier),
            "variation_margin": _decimal_payload(self.variation_margin),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class MarginCallEvent:
    event_id: str
    observed_at: str
    equity: Decimal
    maintenance_requirement: Decimal
    action: MarginCallAction
    positions_before: tuple[str, ...]
    positions_after: tuple[str, ...]
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.event_id, "margin_call.event_id")
        parse_timestamp(self.observed_at, "margin_call.observed_at")
        object.__setattr__(
            self,
            "equity",
            _as_decimal(self.equity, "margin_call.equity"),
        )
        object.__setattr__(
            self,
            "maintenance_requirement",
            _as_decimal(
                self.maintenance_requirement,
                "margin_call.maintenance_requirement",
                nonnegative=True,
            ),
        )
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_margin_call", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "observed_at": self.observed_at,
            "equity": _decimal_payload(self.equity),
            "maintenance_requirement": _decimal_payload(
                self.maintenance_requirement
            ),
            "action": self.action.value,
            "positions_before": list(self.positions_before),
            "positions_after": list(self.positions_after),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class RollExecution:
    execution_id: str
    decision_hash: str
    executed_at: str
    from_contract_id: str
    to_contract_id: str
    close_fill_hash: str
    open_fill_hash: str
    close_cost: Decimal
    open_cost: Decimal
    price_gap: Decimal
    roll_yield: Decimal
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.execution_id, "roll_execution.execution_id")
        require_stable_id(
            self.from_contract_id, "roll_execution.from_contract_id"
        )
        require_stable_id(self.to_contract_id, "roll_execution.to_contract_id")
        parse_timestamp(self.executed_at, "roll_execution.executed_at")
        for value in (
            self.decision_hash,
            self.close_fill_hash,
            self.open_fill_hash,
        ):
            require_hash(value, "roll_execution.evidence_hash")
        if self.from_contract_id == self.to_contract_id:
            raise DerivativeResearchError("roll_execution_contracts_must_differ")
        for name in ("close_cost", "open_cost"):
            object.__setattr__(
                self,
                name,
                _as_decimal(
                    getattr(self, name), f"roll_execution.{name}", nonnegative=True
                ),
            )
        for name in ("price_gap", "roll_yield"):
            object.__setattr__(
                self,
                name,
                _as_decimal(getattr(self, name), f"roll_execution.{name}"),
            )
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_roll_execution", self.identity_payload()),
        )

    @property
    def total_roll_cost(self) -> Decimal:
        return self.close_cost + self.open_cost

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "execution_id": self.execution_id,
            "decision_hash": self.decision_hash,
            "executed_at": self.executed_at,
            "from_contract_id": self.from_contract_id,
            "to_contract_id": self.to_contract_id,
            "close_fill_hash": self.close_fill_hash,
            "open_fill_hash": self.open_fill_hash,
            "close_cost": _decimal_payload(self.close_cost),
            "open_cost": _decimal_payload(self.open_cost),
            "price_gap": _decimal_payload(self.price_gap),
            "roll_yield": _decimal_payload(self.roll_yield),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class SimulationStep:
    step_id: str
    ledger: FuturesLedger
    fills: tuple[FuturesFill, ...] = ()
    settlement_events: tuple[SettlementEvent, ...] = ()
    margin_call: MarginCallEvent | None = None
    roll_execution: RollExecution | None = None
    diagnostics: tuple[str, ...] = ()
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.step_id, "simulation_step.step_id")
        if not (
            self.fills
            or self.settlement_events
            or self.margin_call is not None
            or self.roll_execution is not None
            or self.diagnostics
        ):
            raise DerivativeResearchError("simulation_step_evidence_required")
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_simulation_step", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "step_id": self.step_id,
            "ledger": self.ledger.as_dict(),
            "fills": [item.as_dict() for item in self.fills],
            "settlement_events": [
                item.as_dict() for item in self.settlement_events
            ],
            "margin_call": (
                None if self.margin_call is None else self.margin_call.as_dict()
            ),
            "roll_execution": (
                None
                if self.roll_execution is None
                else self.roll_execution.as_dict()
            ),
            "diagnostics": list(self.diagnostics),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class SpreadLeg:
    contract_id: str
    ratio: int
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.contract_id, "spread_leg.contract_id")
        if self.ratio == 0:
            raise DerivativeResearchError("spread_leg_ratio_cannot_be_zero")
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_spread_leg", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "ratio": self.ratio,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class FuturesSpreadOrder:
    order_id: str
    spread_id: str
    legs: tuple[SpreadLeg, ...]
    units: int
    decision_at: str
    simultaneous_fill: bool
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.order_id, "spread_order.order_id")
        require_stable_id(self.spread_id, "spread_order.spread_id")
        parse_timestamp(self.decision_at, "spread_order.decision_at")
        if self.units <= 0 or len(self.legs) < 2:
            raise DerivativeResearchError("spread_order_legs_or_units_invalid")
        ids = [item.contract_id for item in self.legs]
        if len(ids) != len(set(ids)):
            raise DerivativeResearchError("spread_order_contract_duplicate")
        if not any(item.ratio > 0 for item in self.legs) or not any(
            item.ratio < 0 for item in self.legs
        ):
            raise DerivativeResearchError("spread_order_long_and_short_legs_required")
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_spread_order", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "order_id": self.order_id,
            "spread_id": self.spread_id,
            "legs": [item.as_dict() for item in self.legs],
            "units": self.units,
            "decision_at": self.decision_at,
            "simultaneous_fill": self.simultaneous_fill,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class SpreadExecution:
    execution_id: str
    spread_order_hash: str
    fill_hashes: tuple[str, ...]
    simultaneous_fill: bool
    legging_cost: Decimal
    basis_risk_flag: bool
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.execution_id, "spread_execution.execution_id")
        require_hash(self.spread_order_hash, "spread_execution.order_hash")
        if len(self.fill_hashes) < 2:
            raise DerivativeResearchError("spread_execution_fills_required")
        for value in self.fill_hashes:
            require_hash(value, "spread_execution.fill_hash")
        object.__setattr__(
            self,
            "legging_cost",
            _as_decimal(
                self.legging_cost, "spread_execution.legging_cost", nonnegative=True
            ),
        )
        if self.simultaneous_fill == self.basis_risk_flag:
            raise DerivativeResearchError("spread_execution_basis_risk_mismatch")
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_spread_execution", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "execution_id": self.execution_id,
            "spread_order_hash": self.spread_order_hash,
            "fill_hashes": list(self.fill_hashes),
            "simultaneous_fill": self.simultaneous_fill,
            "legging_cost": _decimal_payload(self.legging_cost),
            "basis_risk_flag": self.basis_risk_flag,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class FuturesSimulator:
    simulator_id: str
    simulator_version: str
    contracts: tuple[FuturesContract, ...]
    settlement_policy: SettlementPolicy
    margin_policy: MarginSimulationPolicy
    expiry_policy: ExpiryPolicy
    cost_policy: FuturesCostPolicy
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.simulator_id, "futures_simulator.simulator_id")
        require_stable_id(
            self.simulator_version, "futures_simulator.simulator_version"
        )
        if not self.contracts:
            raise DerivativeResearchError("futures_simulator_contracts_required")
        ids = [item.contract_id for item in self.contracts]
        if len(ids) != len(set(ids)):
            raise DerivativeResearchError("futures_simulator_contract_duplicate")
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_simulator", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "simulator_id": self.simulator_id,
            "simulator_version": self.simulator_version,
            "contract_hashes": [item.content_hash for item in self.contracts],
            "settlement_policy_hash": self.settlement_policy.content_hash,
            "margin_policy_hash": self.margin_policy.content_hash,
            "expiry_policy_hash": self.expiry_policy.content_hash,
            "cost_policy_hash": self.cost_policy.content_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def contract_for(self, contract_id: str) -> FuturesContract:
        contract = next(
            (item for item in self.contracts if item.contract_id == contract_id),
            None,
        )
        if contract is None:
            raise DerivativeResearchError("futures_contract_not_in_simulator")
        return contract

    def execute_continuous(
        self, ledger: FuturesLedger, point: ContinuousFuturesPoint
    ) -> SimulationStep:
        del ledger, point
        raise DerivativeResearchError("continuous_futures_not_executable")

    def _check_timeline(self, ledger: FuturesLedger, quote: ContractQuote) -> None:
        if ledger.last_event_at is None:
            return
        event_at = parse_timestamp(quote.observed_at, "quote.observed_at")
        last_at = parse_timestamp(ledger.last_event_at, "ledger.last_event_at")
        if event_at < last_at:
            raise DerivativeResearchError("futures_event_time_reversed")
        quote_date = _require_date(quote.trading_date, "quote.trading_date")
        ledger_date = _require_date(
            ledger.last_trading_date or "", "ledger.last_trading_date"
        )
        if quote_date < ledger_date:
            raise DerivativeResearchError("futures_trading_date_reversed")
        if (
            quote_date == ledger_date
            and ledger.last_session_sequence is not None
            and quote.session_sequence < ledger.last_session_sequence
        ):
            raise DerivativeResearchError("futures_session_order_reversed")

    def _margin_requirement(self, positions: Iterable[FuturesPosition], *, maintenance: bool) -> Decimal:
        per_contract = (
            self.margin_policy.maintenance_margin_per_contract
            if maintenance
            else self.margin_policy.initial_margin_per_contract
        )
        return sum((abs(item.quantity) for item in positions), 0) * per_contract

    def _with_event(
        self,
        ledger: FuturesLedger,
        *,
        cash_balance: Decimal | None = None,
        positions: tuple[FuturesPosition, ...] | None = None,
        variation_margin: Decimal = _ZERO,
        fee: Decimal = _ZERO,
        event_hash: str,
        quote: ContractQuote,
        blocked_new_trades: bool | None = None,
        failed: bool | None = None,
        margin_call_increment: int = 0,
    ) -> FuturesLedger:
        return FuturesLedger(
            ledger_id=ledger.ledger_id,
            initial_cash=ledger.initial_cash,
            cash_balance=(
                ledger.cash_balance if cash_balance is None else cash_balance
            ),
            positions=ledger.positions if positions is None else positions,
            cumulative_variation_margin=(
                ledger.cumulative_variation_margin + variation_margin
            ),
            cumulative_fees=ledger.cumulative_fees + fee,
            margin_call_count=ledger.margin_call_count + margin_call_increment,
            blocked_new_trades=(
                ledger.blocked_new_trades
                if blocked_new_trades is None
                else blocked_new_trades
            ),
            failed=ledger.failed if failed is None else failed,
            last_event_at=quote.observed_at,
            last_trading_date=quote.trading_date,
            last_session_sequence=quote.session_sequence,
            event_hashes=(*ledger.event_hashes, event_hash),
        )

    def _execute(
        self,
        ledger: FuturesLedger,
        intent: FuturesOrderIntent,
        quote: ContractQuote,
        *,
        fill_id: str,
        is_roll_leg: bool,
        extra_slippage_ticks: Decimal = _ZERO,
    ) -> tuple[FuturesLedger, FuturesFill]:
        if ledger.failed:
            raise DerivativeResearchError("futures_ledger_already_failed")
        contract = self.contract_for(intent.contract_id)
        if quote.contract_id != intent.contract_id:
            raise DerivativeResearchError("futures_intent_quote_contract_mismatch")
        if not quote.known_at(intent.decision_at):
            raise DerivativeResearchError("futures_quote_not_available_at_decision")
        if not contract.tradable_at(intent.decision_at):
            raise DerivativeResearchError("futures_contract_not_tradable_at_decision")
        self._check_timeline(ledger, quote)
        quote.require_executable(intent.side)
        existing = ledger.position_for(contract.contract_id)
        signed_delta = intent.quantity if intent.side is OrderSide.BUY else -intent.quantity
        old_quantity = 0 if existing is None else existing.quantity
        new_quantity = old_quantity + signed_delta
        increasing = abs(new_quantity) > abs(old_quantity)
        if ledger.blocked_new_trades and increasing:
            raise DerivativeResearchError("futures_new_trades_blocked_by_margin")
        decision_date = parse_timestamp(intent.decision_at, "intent.decision_at").date()
        last_trade = _require_date(contract.last_trade_date, "last_trade")
        last_exit = last_trade - timedelta(
            days=self.expiry_policy.exit_days_before_last_trade
        )
        notice_exit = None
        if contract.first_notice_date is not None:
            notice_exit = _require_date(contract.first_notice_date, "first_notice") - timedelta(
                days=self.expiry_policy.exit_days_before_first_notice
            )
        cutoff = min(last_exit, notice_exit) if notice_exit is not None else last_exit
        if increasing and decision_date >= cutoff:
            raise DerivativeResearchError("futures_open_after_expiry_exit_cutoff")
        reference = quote.close_price
        if intent.limit_price is not None:
            if intent.side is OrderSide.BUY and reference > intent.limit_price:
                raise DerivativeResearchError("futures_buy_limit_not_filled")
            if intent.side is OrderSide.SELL and reference < intent.limit_price:
                raise DerivativeResearchError("futures_sell_limit_not_filled")
            reference = intent.limit_price
        slippage_ticks = self.cost_policy.execution_slippage_ticks + extra_slippage_ticks
        if is_roll_leg:
            slippage_ticks += self.cost_policy.roll_slippage_ticks
        adverse = slippage_ticks * contract.tick_size
        unrounded = reference + adverse if intent.side is OrderSide.BUY else reference - adverse
        fill_price = _round_to_tick(unrounded, contract.tick_size, intent.side)
        if quote.limit_up_price is not None and fill_price > quote.limit_up_price:
            raise DerivativeResearchError("futures_fill_above_price_limit")
        if quote.limit_down_price is not None and fill_price < quote.limit_down_price:
            raise DerivativeResearchError("futures_fill_below_price_limit")
        closed_quantity = 0
        if old_quantity and old_quantity * signed_delta < 0:
            closed_quantity = min(abs(old_quantity), abs(signed_delta))
        realized_pnl = _ZERO
        if existing is not None and closed_quantity:
            closed_signed = closed_quantity if old_quantity > 0 else -closed_quantity
            realized_pnl = (
                (fill_price - existing.last_settlement_price)
                * closed_signed
                * contract.contract_multiplier
            )
        commission = self.cost_policy.commission_per_contract * intent.quantity
        slippage_cost = (
            abs(fill_price - reference)
            * contract.contract_multiplier
            * intent.quantity
        )
        fill = FuturesFill(
            fill_id=fill_id,
            intent_hash=intent.content_hash,
            contract_id=contract.contract_id,
            quote_hash=quote.content_hash,
            filled_at=quote.observed_at,
            trading_date=quote.trading_date,
            session=quote.session,
            side=intent.side,
            quantity=intent.quantity,
            reference_price=reference,
            fill_price=fill_price,
            multiplier=contract.contract_multiplier,
            commission=commission,
            slippage_cost=slippage_cost,
            realized_trade_pnl=realized_pnl,
            is_roll_leg=is_roll_leg,
        )
        positions_by_id = {item.contract_id: item for item in ledger.positions}
        if new_quantity == 0:
            positions_by_id.pop(contract.contract_id, None)
        else:
            if old_quantity != 0 and existing is None:
                raise DerivativeResearchError("futures_position_state_inconsistent")
            if old_quantity == 0 or old_quantity * new_quantity <= 0:
                average = fill_price
                last_settlement = fill_price
            elif increasing:
                assert existing is not None
                average = (
                    (existing.average_entry_price * abs(old_quantity))
                    + (fill_price * abs(signed_delta))
                ) / abs(new_quantity)
                # Existing units have already accrued variation margin from
                # their prior settlement basis.  Newly opened units start at
                # the fill price.  A quantity-weighted basis prevents the next
                # daily settlement from applying pre-entry P&L to the new
                # contracts.
                last_settlement = (
                    (existing.last_settlement_price * abs(old_quantity))
                    + (fill_price * abs(signed_delta))
                ) / abs(new_quantity)
            else:
                assert existing is not None
                average = existing.average_entry_price
                last_settlement = existing.last_settlement_price
            positions_by_id[contract.contract_id] = FuturesPosition(
                contract_id=contract.contract_id,
                quantity=new_quantity,
                average_entry_price=average,
                last_settlement_price=last_settlement,
                contract_spec_hash=contract.content_hash,
            )
        positions = tuple(sorted(positions_by_id.values(), key=lambda item: item.contract_id))
        cash = ledger.cash_balance + realized_pnl - fill.total_cost
        initial_requirement = self._margin_requirement(positions, maintenance=False)
        if increasing and cash * self.margin_policy.collateral_fraction < initial_requirement:
            raise DerivativeResearchError("futures_initial_margin_insufficient")
        next_ledger = self._with_event(
            ledger,
            cash_balance=cash,
            positions=positions,
            fee=fill.total_cost,
            event_hash=fill.content_hash,
            quote=quote,
        )
        return next_ledger, fill

    def execute(
        self,
        ledger: FuturesLedger,
        intent: FuturesOrderIntent,
        quote: ContractQuote,
        *,
        fill_id: str,
        step_id: str,
    ) -> SimulationStep:
        if isinstance(quote, ContinuousFuturesPoint):
            raise DerivativeResearchError("continuous_futures_not_executable")
        next_ledger, fill = self._execute(
            ledger,
            intent,
            quote,
            fill_id=fill_id,
            is_roll_leg=False,
        )
        return SimulationStep(step_id=step_id, ledger=next_ledger, fills=(fill,))

    def settle_daily(
        self,
        ledger: FuturesLedger,
        quote: ContractQuote,
        *,
        event_id: str,
        step_id: str,
        as_of: str | None = None,
    ) -> SimulationStep:
        if ledger.failed:
            raise DerivativeResearchError("futures_ledger_already_failed")
        position = ledger.position_for(quote.contract_id)
        if position is None:
            raise DerivativeResearchError("futures_settlement_position_missing")
        contract = self.contract_for(quote.contract_id)
        knowledge_time = as_of or quote.availability.processed_at
        if not quote.known_at(knowledge_time):
            raise DerivativeResearchError("futures_settlement_quote_not_available")
        self._check_timeline(ledger, quote)
        variation = (
            (quote.settlement_price - position.last_settlement_price)
            * position.quantity
            * contract.contract_multiplier
        )
        event = SettlementEvent(
            event_id=event_id,
            contract_id=contract.contract_id,
            quote_hash=quote.content_hash,
            settled_at=quote.observed_at,
            previous_settlement_price=position.last_settlement_price,
            settlement_price=quote.settlement_price,
            quantity=position.quantity,
            multiplier=contract.contract_multiplier,
            variation_margin=variation,
        )
        positions = tuple(
            replace(item, last_settlement_price=quote.settlement_price)
            if item.contract_id == contract.contract_id
            else item
            for item in ledger.positions
        )
        cash = ledger.cash_balance + variation
        next_ledger = self._with_event(
            ledger,
            cash_balance=cash,
            positions=positions,
            variation_margin=variation,
            event_hash=event.content_hash,
            quote=quote,
        )
        maintenance = self._margin_requirement(positions, maintenance=True)
        margin_call = None
        if cash * self.margin_policy.collateral_fraction < maintenance:
            before = tuple(item.contract_id for item in positions)
            after = before
            blocked = next_ledger.blocked_new_trades
            failed = next_ledger.failed
            if self.margin_policy.margin_call_action is MarginCallAction.REDUCE_POSITION:
                # A margin policy may require reduction, but positions cannot
                # disappear without an executable quote, fill, commission and
                # slippage record.  Block increases and leave liquidation to a
                # subsequent explicit reducing order.
                blocked = True
            elif self.margin_policy.margin_call_action in {
                MarginCallAction.VIRTUAL_MARGIN_CALL,
                MarginCallAction.BLOCK_NEW_TRADES,
            }:
                blocked = True
            else:
                failed = True
            margin_call = MarginCallEvent(
                event_id=f"{event_id}.margin",
                observed_at=quote.observed_at,
                equity=cash,
                maintenance_requirement=maintenance,
                action=self.margin_policy.margin_call_action,
                positions_before=before,
                positions_after=after,
            )
            next_ledger = FuturesLedger(
                ledger_id=next_ledger.ledger_id,
                initial_cash=next_ledger.initial_cash,
                cash_balance=next_ledger.cash_balance,
                positions=positions,
                cumulative_variation_margin=next_ledger.cumulative_variation_margin,
                cumulative_fees=next_ledger.cumulative_fees,
                margin_call_count=next_ledger.margin_call_count + 1,
                blocked_new_trades=blocked,
                failed=failed,
                last_event_at=next_ledger.last_event_at,
                last_trading_date=next_ledger.last_trading_date,
                last_session_sequence=next_ledger.last_session_sequence,
                event_hashes=(*next_ledger.event_hashes, margin_call.content_hash),
            )
        return SimulationStep(
            step_id=step_id,
            ledger=next_ledger,
            settlement_events=(event,),
            margin_call=margin_call,
        )

    def roll(
        self,
        ledger: FuturesLedger,
        decision: RollDecision,
        old_quote: ContractQuote,
        new_quote: ContractQuote,
        *,
        execution_id: str,
        step_id: str,
    ) -> SimulationStep:
        if not decision.should_roll:
            raise DerivativeResearchError("roll_execution_requires_triggered_decision")
        if (
            old_quote.contract_id != decision.from_contract_id
            or new_quote.contract_id != decision.to_contract_id
        ):
            raise DerivativeResearchError("roll_execution_quote_transition_mismatch")
        if old_quote.content_hash not in decision.input_quote_hashes or (
            new_quote.content_hash not in decision.input_quote_hashes
        ):
            raise DerivativeResearchError("roll_execution_quote_not_in_pit_decision")
        if not old_quote.known_at(decision.decision_at) or not new_quote.known_at(
            decision.decision_at
        ):
            raise DerivativeResearchError("roll_execution_quote_not_available_as_of")
        position = ledger.position_for(decision.from_contract_id)
        if position is None:
            raise DerivativeResearchError("roll_execution_source_position_missing")
        close_side = OrderSide.SELL if position.quantity > 0 else OrderSide.BUY
        open_side = OrderSide.BUY if position.quantity > 0 else OrderSide.SELL
        close_intent = FuturesOrderIntent(
            intent_id=f"{execution_id}.close",
            contract_id=decision.from_contract_id,
            side=close_side,
            quantity=abs(position.quantity),
            decision_at=decision.decision_at,
        )
        after_close, close_fill = self._execute(
            ledger,
            close_intent,
            old_quote,
            fill_id=f"{execution_id}.close.fill",
            is_roll_leg=True,
        )
        open_intent = FuturesOrderIntent(
            intent_id=f"{execution_id}.open",
            contract_id=decision.to_contract_id,
            side=open_side,
            quantity=abs(position.quantity),
            decision_at=decision.decision_at,
        )
        after_open, open_fill = self._execute(
            after_close,
            open_intent,
            new_quote,
            fill_id=f"{execution_id}.open.fill",
            is_roll_leg=True,
        )
        direction = _ONE if position.quantity > 0 else -_ONE
        price_gap = new_quote.close_price - old_quote.close_price
        old_contract = self.contract_for(decision.from_contract_id)
        roll_yield = -price_gap * abs(position.quantity) * old_contract.contract_multiplier * direction
        execution = RollExecution(
            execution_id=execution_id,
            decision_hash=decision.content_hash,
            executed_at=decision.decision_at,
            from_contract_id=decision.from_contract_id,
            to_contract_id=decision.to_contract_id,
            close_fill_hash=close_fill.content_hash,
            open_fill_hash=open_fill.content_hash,
            close_cost=close_fill.total_cost,
            open_cost=open_fill.total_cost,
            price_gap=price_gap,
            roll_yield=roll_yield,
        )
        final_ledger = FuturesLedger(
            ledger_id=after_open.ledger_id,
            initial_cash=after_open.initial_cash,
            cash_balance=after_open.cash_balance,
            positions=after_open.positions,
            cumulative_variation_margin=after_open.cumulative_variation_margin,
            cumulative_fees=after_open.cumulative_fees,
            margin_call_count=after_open.margin_call_count,
            blocked_new_trades=after_open.blocked_new_trades,
            failed=after_open.failed,
            last_event_at=after_open.last_event_at,
            last_trading_date=after_open.last_trading_date,
            last_session_sequence=after_open.last_session_sequence,
            event_hashes=(*after_open.event_hashes, execution.content_hash),
        )
        return SimulationStep(
            step_id=step_id,
            ledger=final_ledger,
            fills=(close_fill, open_fill),
            roll_execution=execution,
        )

    def execute_spread(
        self,
        ledger: FuturesLedger,
        order: FuturesSpreadOrder,
        quotes: Sequence[ContractQuote],
        *,
        execution_id: str,
        step_id: str,
    ) -> tuple[SimulationStep, SpreadExecution]:
        quotes_by_id = {item.contract_id: item for item in quotes}
        if set(quotes_by_id) != {item.contract_id for item in order.legs}:
            raise DerivativeResearchError("spread_quotes_do_not_match_legs")
        # Validate every leg before mutating the functional ledger; simultaneous
        # fills therefore have all-or-none market-state semantics.
        for leg in order.legs:
            quote = quotes_by_id[leg.contract_id]
            side = OrderSide.BUY if leg.ratio > 0 else OrderSide.SELL
            if not quote.known_at(order.decision_at):
                raise DerivativeResearchError("spread_quote_not_available_as_of")
            quote.require_executable(side)
        current = ledger
        fills: list[FuturesFill] = []
        legging_cost = _ZERO
        extra = (
            _ZERO
            if order.simultaneous_fill
            else self.cost_policy.spread_legging_ticks
        )
        for index, leg in enumerate(order.legs):
            side = OrderSide.BUY if leg.ratio > 0 else OrderSide.SELL
            intent = FuturesOrderIntent(
                intent_id=f"{order.order_id}.leg{index}",
                contract_id=leg.contract_id,
                side=side,
                quantity=abs(leg.ratio) * order.units,
                decision_at=order.decision_at,
            )
            current, fill = self._execute(
                current,
                intent,
                quotes_by_id[leg.contract_id],
                fill_id=f"{execution_id}.leg{index}.fill",
                is_roll_leg=False,
                extra_slippage_ticks=extra,
            )
            fills.append(fill)
            if not order.simultaneous_fill:
                contract = self.contract_for(leg.contract_id)
                legging_cost += (
                    extra
                    * contract.tick_size
                    * contract.contract_multiplier
                    * abs(leg.ratio)
                    * order.units
                )
        execution = SpreadExecution(
            execution_id=execution_id,
            spread_order_hash=order.content_hash,
            fill_hashes=tuple(item.content_hash for item in fills),
            simultaneous_fill=order.simultaneous_fill,
            legging_cost=legging_cost,
            basis_risk_flag=not order.simultaneous_fill,
        )
        final_ledger = FuturesLedger(
            ledger_id=current.ledger_id,
            initial_cash=current.initial_cash,
            cash_balance=current.cash_balance,
            positions=current.positions,
            cumulative_variation_margin=current.cumulative_variation_margin,
            cumulative_fees=current.cumulative_fees,
            margin_call_count=current.margin_call_count,
            blocked_new_trades=current.blocked_new_trades,
            failed=current.failed,
            last_event_at=current.last_event_at,
            last_trading_date=current.last_trading_date,
            last_session_sequence=current.last_session_sequence,
            event_hashes=(*current.event_hashes, execution.content_hash),
        )
        return (
            SimulationStep(
                step_id=step_id,
                ledger=final_ledger,
                fills=tuple(fills),
                diagnostics=("SPREAD_BASIS_RISK",)
                if execution.basis_risk_flag
                else ("SPREAD_SIMULTANEOUS",),
            ),
            execution,
        )

    def handle_expiration(
        self,
        ledger: FuturesLedger,
        quote: ContractQuote,
        *,
        event_id: str,
        step_id: str,
    ) -> SimulationStep:
        contract = self.contract_for(quote.contract_id)
        position = ledger.position_for(contract.contract_id)
        if position is None:
            raise DerivativeResearchError("expiry_position_missing")
        event_date = parse_timestamp(quote.observed_at, "quote.observed_at").date()
        if event_date < _require_date(contract.final_settlement_date, "final_settlement"):
            raise DerivativeResearchError("final_settlement_not_reached")
        if contract.settlement_type is SettlementType.PHYSICAL_SETTLED:
            if self.expiry_policy.physical_delivery_action is PhysicalDeliveryAction.FAIL_RESEARCH:
                failed_ledger = self._with_event(
                    ledger,
                    event_hash=sha256_prefixed(
                        {
                            "event_id": event_id,
                            "contract_id": contract.contract_id,
                            "action": "PHYSICAL_DELIVERY_RESEARCH_FAILED",
                            "quote_hash": quote.content_hash,
                        },
                        label="physical_delivery_research_failure",
                    ),
                    quote=quote,
                    failed=True,
                )
                return SimulationStep(
                    step_id=step_id,
                    ledger=failed_ledger,
                    diagnostics=("PHYSICAL_DELIVERY_NOT_SIMULATED",),
                )
            raise DerivativeResearchError("physical_future_not_closed_before_notice")
        settled = self.settle_daily(
            ledger,
            quote,
            event_id=event_id,
            step_id=f"{step_id}.settle",
            as_of=quote.availability.processed_at,
        )
        positions = tuple(
            item
            for item in settled.ledger.positions
            if item.contract_id != contract.contract_id
        )
        expiry_hash = sha256_prefixed(
            {
                "event_id": event_id,
                "contract_id": contract.contract_id,
                "settlement_price": _decimal_payload(quote.settlement_price),
                "settlement_event_hashes": [
                    item.content_hash for item in settled.settlement_events
                ],
            },
            label="cash_futures_expiration",
        )
        final_ledger = FuturesLedger(
            ledger_id=settled.ledger.ledger_id,
            initial_cash=settled.ledger.initial_cash,
            cash_balance=settled.ledger.cash_balance,
            positions=positions,
            cumulative_variation_margin=settled.ledger.cumulative_variation_margin,
            cumulative_fees=settled.ledger.cumulative_fees,
            margin_call_count=settled.ledger.margin_call_count,
            blocked_new_trades=settled.ledger.blocked_new_trades,
            failed=settled.ledger.failed,
            last_event_at=settled.ledger.last_event_at,
            last_trading_date=settled.ledger.last_trading_date,
            last_session_sequence=settled.ledger.last_session_sequence,
            event_hashes=(*settled.ledger.event_hashes, expiry_hash),
        )
        return SimulationStep(
            step_id=step_id,
            ledger=final_ledger,
            settlement_events=settled.settlement_events,
            margin_call=settled.margin_call,
            diagnostics=("CASH_SETTLED_AT_FINAL_SETTLEMENT_PRICE",),
        )


@dataclass(frozen=True, slots=True)
class FuturesStressCase:
    case_id: str
    case_version: str
    kind: FuturesStressKind
    scalar: Decimal
    baseline_policy_hashes: tuple[str, ...]
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.case_id, "futures_stress.case_id")
        require_stable_id(self.case_version, "futures_stress.case_version")
        scalar = _as_decimal(self.scalar, "futures_stress.scalar", positive=True)
        object.__setattr__(self, "scalar", scalar)
        if not self.baseline_policy_hashes:
            raise DerivativeResearchError("futures_stress_policy_hash_required")
        for value in self.baseline_policy_hashes:
            require_hash(value, "futures_stress.policy_hash")
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_stress_case", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "case_version": self.case_version,
            "kind": self.kind.value,
            "scalar": _decimal_payload(self.scalar),
            "baseline_policy_hashes": list(self.baseline_policy_hashes),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class FuturesStressResult:
    result_id: str
    case_hash: str
    baseline_equity: Decimal
    stressed_equity: Decimal
    blocked_exit_count: int
    margin_call_count: int
    diagnostics: tuple[str, ...]
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.result_id, "futures_stress_result.result_id")
        require_hash(self.case_hash, "futures_stress_result.case_hash")
        for name in ("baseline_equity", "stressed_equity"):
            object.__setattr__(
                self,
                name,
                _as_decimal(getattr(self, name), f"futures_stress_result.{name}"),
            )
        if self.blocked_exit_count < 0 or self.margin_call_count < 0:
            raise DerivativeResearchError("futures_stress_result_count_invalid")
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_stress_result", self.identity_payload()),
        )

    @property
    def equity_delta(self) -> Decimal:
        return self.stressed_equity - self.baseline_equity

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "result_id": self.result_id,
            "case_hash": self.case_hash,
            "baseline_equity": _decimal_payload(self.baseline_equity),
            "stressed_equity": _decimal_payload(self.stressed_equity),
            "equity_delta": _decimal_payload(self.equity_delta),
            "blocked_exit_count": self.blocked_exit_count,
            "margin_call_count": self.margin_call_count,
            "diagnostics": list(self.diagnostics),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class FuturesStressInputs:
    """Immutable, point-in-time inputs for one futures stress execution.

    ``marks`` are settlement marks, not executable continuous-series values.
    Optional policy and execution evidence is carried only when a stress kind
    needs it.  The executor below rejects both missing evidence and unrelated
    policy hashes instead of silently substituting assumptions.
    """

    input_id: str
    input_version: str
    simulator: FuturesSimulator
    ledger: FuturesLedger
    marks: tuple[ContractQuote, ...]
    as_of: str
    selected_contract_id: str | None = None
    alternate_contract_id: str | None = None
    roll_policy: RollPolicy | None = None
    continuous_policy: ContinuousFuturesPolicy | None = None
    continuous_point: ContinuousFuturesPoint | None = None
    roll_executions: tuple[RollExecution, ...] = ()
    spread_executions: tuple[SpreadExecution, ...] = ()
    lifecycle_events: tuple[FuturesLifecycleEvent, ...] = ()
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.input_id, "futures_stress_inputs.input_id")
        require_stable_id(
            self.input_version, "futures_stress_inputs.input_version"
        )
        parse_timestamp(self.as_of, "futures_stress_inputs.as_of")
        if not self.marks:
            raise DerivativeResearchError("futures_stress_marks_required")
        mark_ids = [item.contract_id for item in self.marks]
        if mark_ids != sorted(set(mark_ids)):
            raise DerivativeResearchError(
                "futures_stress_marks_not_unique_sorted"
            )
        contracts_by_id = {
            item.contract_id: item for item in self.simulator.contracts
        }
        for mark in self.marks:
            contract = contracts_by_id.get(mark.contract_id)
            if contract is None:
                raise DerivativeResearchError(
                    "futures_stress_mark_contract_not_in_simulator"
                )
            if mark.root_id != contract.root_id:
                raise DerivativeResearchError(
                    "futures_stress_mark_contract_root_mismatch"
                )
            if not mark.known_at(self.as_of):
                raise DerivativeResearchError(
                    "futures_stress_mark_not_available_as_of"
                )
        marks_by_id = {item.contract_id: item for item in self.marks}
        for position in self.ledger.positions:
            contract = contracts_by_id.get(position.contract_id)
            if contract is None:
                raise DerivativeResearchError(
                    "futures_stress_position_contract_not_in_simulator"
                )
            if position.contract_spec_hash != contract.content_hash:
                raise DerivativeResearchError(
                    "futures_stress_position_contract_spec_mismatch"
                )
            if position.contract_id not in marks_by_id:
                raise DerivativeResearchError(
                    "futures_stress_position_mark_missing"
                )
        for field_name, contract_id in (
            ("selected", self.selected_contract_id),
            ("alternate", self.alternate_contract_id),
        ):
            if contract_id is None:
                continue
            require_stable_id(
                contract_id, f"futures_stress_inputs.{field_name}_contract_id"
            )
            if contract_id not in marks_by_id:
                raise DerivativeResearchError(
                    f"futures_stress_{field_name}_mark_missing"
                )
        if (
            self.selected_contract_id is not None
            and self.ledger.position_for(self.selected_contract_id) is None
        ):
            raise DerivativeResearchError(
                "futures_stress_selected_position_missing"
            )
        if (
            self.selected_contract_id is not None
            and self.alternate_contract_id == self.selected_contract_id
        ):
            raise DerivativeResearchError(
                "futures_stress_alternate_contract_must_differ"
            )
        if self.continuous_point is not None:
            if self.continuous_policy is None:
                raise DerivativeResearchError(
                    "futures_stress_continuous_policy_required"
                )
            point = self.continuous_point
            if point.policy_hash != self.continuous_policy.content_hash:
                raise DerivativeResearchError(
                    "futures_stress_continuous_policy_mismatch"
                )
            if point.source_quote_hash not in {
                item.content_hash for item in self.marks
            }:
                raise DerivativeResearchError(
                    "futures_stress_continuous_source_mark_missing"
                )
            if point.source_contract_id not in marks_by_id:
                raise DerivativeResearchError(
                    "futures_stress_continuous_contract_missing"
                )
            if parse_timestamp(
                point.observed_at, "futures_stress_inputs.continuous_observed_at"
            ) > parse_timestamp(self.as_of, "futures_stress_inputs.as_of"):
                raise DerivativeResearchError(
                    "futures_stress_continuous_point_from_future"
                )
        if (
            self.continuous_policy is not None
            and self.roll_policy is not None
            and self.continuous_policy.roll_policy_hash
            != self.roll_policy.content_hash
        ):
            raise DerivativeResearchError(
                "futures_stress_continuous_roll_policy_mismatch"
            )
        roll_ids = [item.execution_id for item in self.roll_executions]
        if roll_ids != sorted(set(roll_ids)):
            raise DerivativeResearchError(
                "futures_stress_roll_executions_not_unique_sorted"
            )
        spread_ids = [item.execution_id for item in self.spread_executions]
        if spread_ids != sorted(set(spread_ids)):
            raise DerivativeResearchError(
                "futures_stress_spread_executions_not_unique_sorted"
            )
        lifecycle_keys = [
            (item.contract_id, item.event_at, item.event_id)
            for item in self.lifecycle_events
        ]
        if lifecycle_keys != sorted(set(lifecycle_keys)):
            raise DerivativeResearchError(
                "futures_stress_lifecycle_events_not_unique_sorted"
            )
        for event in self.lifecycle_events:
            if event.contract_id not in contracts_by_id:
                raise DerivativeResearchError(
                    "futures_stress_lifecycle_contract_not_in_simulator"
                )
            if not event.availability.known_at(self.as_of):
                raise DerivativeResearchError(
                    "futures_stress_lifecycle_event_not_available_as_of"
                )
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_stress_inputs", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "input_id": self.input_id,
            "input_version": self.input_version,
            "simulator_hash": self.simulator.content_hash,
            "ledger_hash": self.ledger.content_hash,
            "mark_hashes": [item.content_hash for item in self.marks],
            "as_of": self.as_of,
            "selected_contract_id": self.selected_contract_id,
            "alternate_contract_id": self.alternate_contract_id,
            "roll_policy_hash": (
                None if self.roll_policy is None else self.roll_policy.content_hash
            ),
            "continuous_policy_hash": (
                None
                if self.continuous_policy is None
                else self.continuous_policy.content_hash
            ),
            "continuous_point_hash": (
                None
                if self.continuous_point is None
                else self.continuous_point.content_hash
            ),
            "roll_execution_hashes": [
                item.content_hash for item in self.roll_executions
            ],
            "spread_execution_hashes": [
                item.content_hash for item in self.spread_executions
            ],
            "lifecycle_event_hashes": [
                item.content_hash for item in self.lifecycle_events
            ],
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class FuturesStressExecution:
    """Hash-bound result of applying one stress case to one immutable input."""

    execution_id: str
    execution_version: str
    case_hash: str
    input_hash: str
    scenario_hash: str
    simulator_hash: str
    ledger_hash: str
    evidence_hashes: tuple[str, ...]
    result: FuturesStressResult
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.execution_id, "futures_stress_execution.execution_id")
        require_stable_id(
            self.execution_version, "futures_stress_execution.execution_version"
        )
        for name in (
            "case_hash",
            "input_hash",
            "scenario_hash",
            "simulator_hash",
            "ledger_hash",
        ):
            require_hash(
                getattr(self, name), f"futures_stress_execution.{name}"
            )
        if not self.evidence_hashes:
            raise DerivativeResearchError(
                "futures_stress_execution_evidence_required"
            )
        if len(self.evidence_hashes) != len(set(self.evidence_hashes)):
            raise DerivativeResearchError(
                "futures_stress_execution_evidence_duplicate"
            )
        for value in self.evidence_hashes:
            require_hash(value, "futures_stress_execution.evidence_hash")
        if self.result.case_hash != self.case_hash:
            raise DerivativeResearchError(
                "futures_stress_execution_result_case_mismatch"
            )
        required = {
            self.case_hash,
            self.input_hash,
            self.scenario_hash,
            self.simulator_hash,
            self.ledger_hash,
            self.result.content_hash,
        }
        if not required.issubset(set(self.evidence_hashes)):
            raise DerivativeResearchError(
                "futures_stress_execution_evidence_binding_incomplete"
            )
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_stress_execution", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "execution_id": self.execution_id,
            "execution_version": self.execution_version,
            "case_hash": self.case_hash,
            "input_hash": self.input_hash,
            "scenario_hash": self.scenario_hash,
            "simulator_hash": self.simulator_hash,
            "ledger_hash": self.ledger_hash,
            "evidence_hashes": list(self.evidence_hashes),
            "result": self.result.as_dict(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def _stress_marked_equity(inputs: FuturesStressInputs) -> Decimal:
    marks_by_id = {item.contract_id: item for item in inputs.marks}
    equity = inputs.ledger.cash_balance
    for position in inputs.ledger.positions:
        mark = marks_by_id[position.contract_id]
        contract = inputs.simulator.contract_for(position.contract_id)
        equity += (
            (mark.settlement_price - position.last_settlement_price)
            * position.quantity
            * contract.contract_multiplier
        )
    return equity


def _stress_selected(
    inputs: FuturesStressInputs,
) -> tuple[FuturesPosition, FuturesContract, ContractQuote]:
    contract_id = inputs.selected_contract_id
    if contract_id is None:
        raise DerivativeResearchError(
            "futures_stress_selected_contract_required"
        )
    position = inputs.ledger.position_for(contract_id)
    if position is None:
        raise DerivativeResearchError("futures_stress_selected_position_missing")
    contract = inputs.simulator.contract_for(contract_id)
    mark = next(item for item in inputs.marks if item.contract_id == contract_id)
    return position, contract, mark


def _stress_alternate(
    inputs: FuturesStressInputs,
) -> tuple[FuturesContract, ContractQuote]:
    contract_id = inputs.alternate_contract_id
    if contract_id is None:
        raise DerivativeResearchError(
            "futures_stress_alternate_contract_required"
        )
    contract = inputs.simulator.contract_for(contract_id)
    mark = next(item for item in inputs.marks if item.contract_id == contract_id)
    return contract, mark


def _stress_required_policy_hashes(
    kind: FuturesStressKind, inputs: FuturesStressInputs
) -> frozenset[str]:
    simulator = inputs.simulator
    required = {simulator.content_hash}
    if kind is FuturesStressKind.ROLL_POLICY:
        if inputs.roll_policy is None:
            raise DerivativeResearchError("futures_stress_roll_policy_required")
        required.add(inputs.roll_policy.content_hash)
    elif kind is FuturesStressKind.CONTINUOUS_ADJUSTMENT:
        if inputs.continuous_policy is None:
            raise DerivativeResearchError(
                "futures_stress_continuous_policy_required"
            )
        required.add(inputs.continuous_policy.content_hash)
    elif kind in {
        FuturesStressKind.ROLL_COST,
        FuturesStressKind.HIGH_VOL_LOW_LIQUIDITY,
        FuturesStressKind.NIGHT_SESSION,
        FuturesStressKind.SPREAD_LEGGING,
    }:
        required.add(simulator.cost_policy.content_hash)
    elif kind is FuturesStressKind.NEAR_EXPIRY_EXCLUSION:
        required.update(
            {
                simulator.expiry_policy.content_hash,
                simulator.cost_policy.content_hash,
            }
        )
    elif kind is FuturesStressKind.MARGIN_INCREASE:
        required.add(simulator.margin_policy.content_hash)
    return frozenset(required)


def _validate_stress_policy_binding(
    case: FuturesStressCase, inputs: FuturesStressInputs
) -> None:
    supplied = set(case.baseline_policy_hashes)
    if len(supplied) != len(case.baseline_policy_hashes):
        raise DerivativeResearchError(
            "futures_stress_baseline_policy_hash_duplicate"
        )
    simulator = inputs.simulator
    known = {
        simulator.content_hash,
        simulator.settlement_policy.content_hash,
        simulator.margin_policy.content_hash,
        simulator.expiry_policy.content_hash,
        simulator.cost_policy.content_hash,
    }
    if inputs.roll_policy is not None:
        known.add(inputs.roll_policy.content_hash)
    if inputs.continuous_policy is not None:
        known.add(inputs.continuous_policy.content_hash)
    if not supplied.issubset(known):
        raise DerivativeResearchError(
            "futures_stress_unknown_baseline_policy_hash"
        )
    required = _stress_required_policy_hashes(case.kind, inputs)
    if not required.issubset(supplied):
        raise DerivativeResearchError(
            "futures_stress_required_policy_hash_missing"
        )


def _stress_exit_cost(
    simulator: FuturesSimulator,
    contract: FuturesContract,
    quantity: int,
) -> Decimal:
    per_contract = simulator.cost_policy.commission_per_contract + (
        simulator.cost_policy.execution_slippage_ticks * contract.tick_size
    ) * contract.contract_multiplier
    return per_contract * abs(quantity)


def run_futures_stress_case(
    case: FuturesStressCase,
    inputs: FuturesStressInputs,
    *,
    execution_id: str,
    execution_version: str = "v1",
) -> FuturesStressExecution:
    """Execute one deterministic futures stress scenario.

    ``scalar`` is a severity multiplier and must be at least one for an
    executable stress.  A scalar of one is the baseline for multiplicative
    stresses; price-limit scenarios still apply the observed exchange limit at
    one because the discrete no-exit event itself is the stress.
    """

    require_stable_id(execution_id, "futures_stress_execution.execution_id")
    require_stable_id(
        execution_version, "futures_stress_execution.execution_version"
    )
    if case.scalar < _ONE:
        raise DerivativeResearchError(
            "futures_stress_scalar_below_baseline"
        )
    _validate_stress_policy_binding(case, inputs)
    baseline_equity = _stress_marked_equity(inputs)
    stressed_equity = baseline_equity
    blocked_exit_count = 0
    margin_call_count = inputs.ledger.margin_call_count
    diagnostics: list[str] = [
        "FUTURES_STRESS_EXECUTED",
        case.kind.value,
    ]
    scenario: dict[str, object] = {
        "schema_version": FUTURES_RESEARCH_SCHEMA_VERSION,
        "case_hash": case.content_hash,
        "input_hash": inputs.content_hash,
        "kind": case.kind.value,
        "scalar": _decimal_payload(case.scalar),
        "baseline_equity": _decimal_payload(baseline_equity),
    }
    severity = case.scalar - _ONE

    if case.kind is FuturesStressKind.ROLL_POLICY:
        position, contract, mark = _stress_selected(inputs)
        alternate_contract, alternate_mark = _stress_alternate(inputs)
        roll_policy = inputs.roll_policy
        assert roll_policy is not None
        if contract.root_id != alternate_contract.root_id:
            raise DerivativeResearchError(
                "futures_stress_roll_contract_root_mismatch"
            )
        if not alternate_contract.tradable_at(inputs.as_of):
            raise DerivativeResearchError(
                "futures_stress_roll_target_not_tradable"
            )
        roll_notional_gap = abs(
            alternate_mark.close_price * alternate_contract.contract_multiplier
            - mark.close_price * contract.contract_multiplier
        ) * abs(position.quantity)
        estimated_round_trip_cost = (
            _stress_exit_cost(inputs.simulator, contract, position.quantity)
            + _stress_exit_cost(
                inputs.simulator, alternate_contract, position.quantity
            )
        )
        adverse = severity * (roll_notional_gap + estimated_round_trip_cost)
        stressed_equity -= adverse
        policy_days = max(roll_policy.days_before_last_trade or 1, 1)
        stressed_days = int(
            (Decimal(policy_days) * case.scalar).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        scenario.update(
            {
                "roll_policy_hash": roll_policy.content_hash,
                "from_contract_hash": contract.content_hash,
                "to_contract_hash": alternate_contract.content_hash,
                "from_mark_hash": mark.content_hash,
                "to_mark_hash": alternate_mark.content_hash,
                "baseline_roll_days": policy_days,
                "stressed_roll_days": stressed_days,
                "roll_notional_gap": _decimal_payload(roll_notional_gap),
                "estimated_round_trip_cost": _decimal_payload(
                    estimated_round_trip_cost
                ),
                "adverse_delta": _decimal_payload(-adverse),
            }
        )
        diagnostics.append("ROLL_POLICY_TIMING_AND_GAP_STRESSED")
    elif case.kind is FuturesStressKind.CONTINUOUS_ADJUSTMENT:
        position, contract, mark = _stress_selected(inputs)
        policy = inputs.continuous_policy
        point = inputs.continuous_point
        if policy is None or point is None:
            raise DerivativeResearchError(
                "futures_stress_continuous_evidence_required"
            )
        if point.source_contract_id != contract.contract_id:
            raise DerivativeResearchError(
                "futures_stress_continuous_selected_contract_mismatch"
            )
        if point.source_quote_hash != mark.content_hash:
            raise DerivativeResearchError(
                "futures_stress_continuous_selected_mark_mismatch"
            )
        adjustment_exposure = (
            abs(point.continuous_price - point.source_price)
            * abs(position.quantity)
            * contract.contract_multiplier
        )
        adverse = adjustment_exposure * severity
        stressed_equity -= adverse
        scenario.update(
            {
                "continuous_policy_hash": policy.content_hash,
                "continuous_point_hash": point.content_hash,
                "continuous_signal_only": point.signal_only,
                "adjustment_exposure": _decimal_payload(adjustment_exposure),
                "adverse_delta": _decimal_payload(-adverse),
            }
        )
        diagnostics.append("CONTINUOUS_SIGNAL_NOT_EXECUTED")
    elif case.kind is FuturesStressKind.CONTRACT_VS_SIGNAL:
        position, contract, mark = _stress_selected(inputs)
        point = inputs.continuous_point
        if point is None:
            raise DerivativeResearchError(
                "futures_stress_signal_point_required"
            )
        if point.source_quote_hash != mark.content_hash:
            raise DerivativeResearchError(
                "futures_stress_signal_mark_mismatch"
            )
        divergence = (
            abs(point.continuous_price - mark.close_price)
            * abs(position.quantity)
            * contract.contract_multiplier
        )
        adverse = divergence * severity
        stressed_equity -= adverse
        scenario.update(
            {
                "signal_point_hash": point.content_hash,
                "executable_mark_hash": mark.content_hash,
                "signal_execution_divergence": _decimal_payload(divergence),
                "adverse_delta": _decimal_payload(-adverse),
            }
        )
        diagnostics.append("CONTRACT_SIGNAL_DIVERGENCE_STRESSED")
    elif case.kind is FuturesStressKind.ROLL_COST:
        if not inputs.roll_executions:
            raise DerivativeResearchError(
                "futures_stress_roll_execution_evidence_required"
            )
        base_cost = sum(
            (item.total_roll_cost for item in inputs.roll_executions), _ZERO
        )
        if base_cost <= 0:
            raise DerivativeResearchError(
                "futures_stress_roll_cost_must_be_positive"
            )
        adverse = base_cost * severity
        stressed_equity -= adverse
        scenario.update(
            {
                "roll_execution_hashes": [
                    item.content_hash for item in inputs.roll_executions
                ],
                "baseline_roll_cost": _decimal_payload(base_cost),
                "stressed_roll_cost": _decimal_payload(base_cost * case.scalar),
                "adverse_delta": _decimal_payload(-adverse),
            }
        )
        diagnostics.append("ROLL_COST_MULTIPLIER_APPLIED")
    elif case.kind is FuturesStressKind.NEAR_EXPIRY_EXCLUSION:
        position, contract, mark = _stress_selected(inputs)
        expiry_policy = inputs.simulator.expiry_policy
        last_trade = _require_date(contract.last_trade_date, "last_trade")
        last_trade_days = int(
            (
                Decimal(expiry_policy.exit_days_before_last_trade) * case.scalar
            ).to_integral_value(rounding=ROUND_CEILING)
        )
        stressed_cutoff = last_trade - timedelta(days=last_trade_days)
        first_notice_cutoff: date | None = None
        if contract.first_notice_date is not None:
            first_notice = _require_date(
                contract.first_notice_date, "first_notice"
            )
            notice_days = int(
                (
                    Decimal(expiry_policy.exit_days_before_first_notice)
                    * case.scalar
                ).to_integral_value(rounding=ROUND_CEILING)
            )
            first_notice_cutoff = first_notice - timedelta(days=notice_days)
            stressed_cutoff = min(stressed_cutoff, first_notice_cutoff)
        as_of_date = parse_timestamp(
            inputs.as_of, "futures_stress_inputs.as_of"
        ).date()
        excluded = as_of_date >= stressed_cutoff
        exit_cost = _ZERO
        if excluded:
            close_side = (
                OrderSide.SELL if position.quantity > 0 else OrderSide.BUY
            )
            try:
                mark.require_executable(close_side)
            except DerivativeResearchError:
                blocked_exit_count = 1
                diagnostics.append("NEAR_EXPIRY_EXIT_BLOCKED")
            else:
                exit_cost = _stress_exit_cost(
                    inputs.simulator, contract, position.quantity
                )
                stressed_equity -= exit_cost
                diagnostics.append("NEAR_EXPIRY_FORCED_EXIT_COST_APPLIED")
        else:
            diagnostics.append("OUTSIDE_STRESSED_EXPIRY_WINDOW")
        scenario.update(
            {
                "contract_hash": contract.content_hash,
                "mark_hash": mark.content_hash,
                "expiry_policy_hash": expiry_policy.content_hash,
                "last_trade_date": contract.last_trade_date,
                "first_notice_date": contract.first_notice_date,
                "stressed_exit_cutoff": stressed_cutoff.isoformat(),
                "first_notice_cutoff": (
                    None
                    if first_notice_cutoff is None
                    else first_notice_cutoff.isoformat()
                ),
                "excluded": excluded,
                "exit_cost": _decimal_payload(exit_cost),
                "blocked_exit_count": blocked_exit_count,
            }
        )
    elif case.kind is FuturesStressKind.CURVE_REGIME:
        position, contract, mark = _stress_selected(inputs)
        alternate_contract, alternate_mark = _stress_alternate(inputs)
        if contract.root_id != alternate_contract.root_id:
            raise DerivativeResearchError(
                "futures_stress_curve_contract_root_mismatch"
            )
        curve_gap = abs(
            alternate_mark.close_price * alternate_contract.contract_multiplier
            - mark.close_price * contract.contract_multiplier
        ) * abs(position.quantity)
        adverse = curve_gap * severity
        stressed_equity -= adverse
        scenario.update(
            {
                "near_mark_hash": mark.content_hash,
                "deferred_mark_hash": alternate_mark.content_hash,
                "curve_notional_gap": _decimal_payload(curve_gap),
                "adverse_delta": _decimal_payload(-adverse),
            }
        )
        diagnostics.append("CURVE_REGIME_GAP_STRESSED")
    elif case.kind is FuturesStressKind.HIGH_VOL_LOW_LIQUIDITY:
        position, contract, mark = _stress_selected(inputs)
        if mark.bid_price is None or mark.ask_price is None:
            raise DerivativeResearchError(
                "futures_stress_bid_ask_required"
            )
        if mark.volume <= 0:
            raise DerivativeResearchError(
                "futures_stress_positive_volume_required"
            )
        half_spread = (mark.ask_price - mark.bid_price) / Decimal("2")
        base_slippage = (
            half_spread
            + inputs.simulator.cost_policy.execution_slippage_ticks
            * contract.tick_size
        ) * contract.contract_multiplier * abs(position.quantity)
        participation = Decimal(abs(position.quantity)) / mark.volume
        adverse = base_slippage * severity * (_ONE + participation)
        stressed_equity -= adverse
        scenario.update(
            {
                "mark_hash": mark.content_hash,
                "half_spread": _decimal_payload(half_spread),
                "volume": _decimal_payload(mark.volume),
                "participation": _decimal_payload(participation),
                "baseline_liquidation_slippage": _decimal_payload(
                    base_slippage
                ),
                "adverse_delta": _decimal_payload(-adverse),
            }
        )
        diagnostics.append("VOLATILITY_LIQUIDITY_SLIPPAGE_STRESSED")
    elif case.kind is FuturesStressKind.NIGHT_SESSION:
        position, contract, mark = _stress_selected(inputs)
        if mark.session is not SessionType.NIGHT:
            raise DerivativeResearchError(
                "futures_stress_night_session_mark_required"
            )
        baseline_ticks = max(
            inputs.simulator.cost_policy.execution_slippage_ticks, _ONE
        )
        adverse = (
            baseline_ticks
            * severity
            * contract.tick_size
            * contract.contract_multiplier
            * abs(position.quantity)
        )
        stressed_equity -= adverse
        scenario.update(
            {
                "night_mark_hash": mark.content_hash,
                "baseline_slippage_ticks": _decimal_payload(baseline_ticks),
                "stressed_slippage_ticks": _decimal_payload(
                    baseline_ticks * case.scalar
                ),
                "adverse_delta": _decimal_payload(-adverse),
            }
        )
        diagnostics.append("NIGHT_SESSION_SLIPPAGE_STRESSED")
    elif case.kind is FuturesStressKind.MARGIN_INCREASE:
        if not inputs.ledger.positions:
            raise DerivativeResearchError(
                "futures_stress_margin_positions_required"
            )
        simulator = inputs.simulator
        baseline_initial = simulator._margin_requirement(
            inputs.ledger.positions, maintenance=False
        )
        baseline_maintenance = simulator._margin_requirement(
            inputs.ledger.positions, maintenance=True
        )
        stressed_initial = baseline_initial * case.scalar
        stressed_maintenance = baseline_maintenance * case.scalar
        collateral = baseline_equity * simulator.margin_policy.collateral_fraction
        triggered = collateral < stressed_maintenance
        if triggered:
            margin_call_count += 1
            diagnostics.append(
                "MARGIN_CALL_ACTION_"
                + simulator.margin_policy.margin_call_action.value
            )
        else:
            diagnostics.append("STRESSED_MARGIN_SUFFICIENT")
        scenario.update(
            {
                "margin_policy_hash": simulator.margin_policy.content_hash,
                "baseline_initial_requirement": _decimal_payload(
                    baseline_initial
                ),
                "baseline_maintenance_requirement": _decimal_payload(
                    baseline_maintenance
                ),
                "stressed_initial_requirement": _decimal_payload(
                    stressed_initial
                ),
                "stressed_maintenance_requirement": _decimal_payload(
                    stressed_maintenance
                ),
                "eligible_collateral": _decimal_payload(collateral),
                "margin_call_triggered": triggered,
                "margin_call_action": (
                    simulator.margin_policy.margin_call_action.value
                ),
            }
        )
    elif case.kind is FuturesStressKind.PRICE_LIMIT_NO_EXIT:
        position, contract, mark = _stress_selected(inputs)
        if position.quantity > 0:
            limit_price = mark.limit_down_price
            expected_state = MarketState.LIMIT_DOWN
        else:
            limit_price = mark.limit_up_price
            expected_state = MarketState.LIMIT_UP
        if limit_price is None:
            raise DerivativeResearchError(
                "futures_stress_adverse_price_limit_required"
            )
        adverse_move = (
            (limit_price - mark.settlement_price)
            * position.quantity
            * contract.contract_multiplier
        )
        if adverse_move >= 0:
            raise DerivativeResearchError(
                "futures_stress_price_limit_not_adverse"
            )
        stressed_equity += adverse_move * case.scalar
        blocked_exit_count = 1
        scenario.update(
            {
                "mark_hash": mark.content_hash,
                "synthetic_market_state": expected_state.value,
                "observed_market_state": mark.market_state.value,
                "adverse_limit_price": _decimal_payload(limit_price),
                "adverse_limit_move": _decimal_payload(adverse_move),
                "persistence_multiplier": _decimal_payload(case.scalar),
                "blocked_exit_count": blocked_exit_count,
            }
        )
        diagnostics.append("PRICE_LIMIT_EXIT_BLOCKED")
    elif case.kind is FuturesStressKind.MULTIPLIER_TICK_REGIME:
        position, contract, mark = _stress_selected(inputs)
        baseline_unsettled = (
            (mark.settlement_price - position.last_settlement_price)
            * position.quantity
            * contract.contract_multiplier
        )
        baseline_friction = (
            inputs.simulator.cost_policy.execution_slippage_ticks
            * contract.tick_size
            * contract.contract_multiplier
            * abs(position.quantity)
        )
        stressed_multiplier = contract.contract_multiplier * case.scalar
        stressed_tick = contract.tick_size * case.scalar
        stressed_friction = (
            inputs.simulator.cost_policy.execution_slippage_ticks
            * stressed_tick
            * stressed_multiplier
            * abs(position.quantity)
        )
        adverse = abs(baseline_unsettled) * severity + (
            stressed_friction - baseline_friction
        )
        stressed_equity -= adverse
        scenario.update(
            {
                "contract_hash": contract.content_hash,
                "mark_hash": mark.content_hash,
                "baseline_multiplier": _decimal_payload(
                    contract.contract_multiplier
                ),
                "stressed_multiplier": _decimal_payload(stressed_multiplier),
                "baseline_tick": _decimal_payload(contract.tick_size),
                "stressed_tick": _decimal_payload(stressed_tick),
                "baseline_unsettled_pnl": _decimal_payload(
                    baseline_unsettled
                ),
                "baseline_liquidation_friction": _decimal_payload(
                    baseline_friction
                ),
                "stressed_liquidation_friction": _decimal_payload(
                    stressed_friction
                ),
                "adverse_delta": _decimal_payload(-adverse),
            }
        )
        diagnostics.append("MULTIPLIER_AND_TICK_REGIME_STRESSED")
    elif case.kind is FuturesStressKind.SPREAD_LEGGING:
        if not inputs.spread_executions:
            raise DerivativeResearchError(
                "futures_stress_spread_execution_evidence_required"
            )
        if any(
            item.simultaneous_fill or not item.basis_risk_flag
            for item in inputs.spread_executions
        ):
            raise DerivativeResearchError(
                "futures_stress_non_simultaneous_spread_required"
            )
        base_cost = sum(
            (item.legging_cost for item in inputs.spread_executions), _ZERO
        )
        if base_cost <= 0:
            raise DerivativeResearchError(
                "futures_stress_spread_legging_cost_must_be_positive"
            )
        adverse = base_cost * severity
        stressed_equity -= adverse
        scenario.update(
            {
                "spread_execution_hashes": [
                    item.content_hash for item in inputs.spread_executions
                ],
                "baseline_legging_cost": _decimal_payload(base_cost),
                "stressed_legging_cost": _decimal_payload(
                    base_cost * case.scalar
                ),
                "adverse_delta": _decimal_payload(-adverse),
            }
        )
        diagnostics.append("SPREAD_LEGGING_COST_STRESSED")
    else:  # pragma: no cover - StrEnum construction is closed, kept fail-closed.
        raise DerivativeResearchError("futures_stress_kind_unsupported")

    scenario["stressed_equity"] = _decimal_payload(stressed_equity)
    scenario["blocked_exit_count"] = blocked_exit_count
    scenario["margin_call_count"] = margin_call_count
    scenario_hash = _hash_payload("futures_stress_scenario", scenario)
    result = FuturesStressResult(
        result_id=f"{execution_id}.result",
        case_hash=case.content_hash,
        baseline_equity=baseline_equity,
        stressed_equity=stressed_equity,
        blocked_exit_count=blocked_exit_count,
        margin_call_count=margin_call_count,
        diagnostics=tuple(diagnostics),
    )
    evidence = [
        case.content_hash,
        inputs.content_hash,
        scenario_hash,
        inputs.simulator.content_hash,
        inputs.ledger.content_hash,
        result.content_hash,
        *(item.content_hash for item in inputs.marks),
        *(item.content_hash for item in inputs.roll_executions),
        *(item.content_hash for item in inputs.spread_executions),
        *(item.content_hash for item in inputs.lifecycle_events),
    ]
    if inputs.roll_policy is not None:
        evidence.append(inputs.roll_policy.content_hash)
    if inputs.continuous_policy is not None:
        evidence.append(inputs.continuous_policy.content_hash)
    if inputs.continuous_point is not None:
        evidence.append(inputs.continuous_point.content_hash)
    return FuturesStressExecution(
        execution_id=execution_id,
        execution_version=execution_version,
        case_hash=case.content_hash,
        input_hash=inputs.content_hash,
        scenario_hash=scenario_hash,
        simulator_hash=inputs.simulator.content_hash,
        ledger_hash=inputs.ledger.content_hash,
        evidence_hashes=tuple(dict.fromkeys(evidence)),
        result=result,
    )


@dataclass(frozen=True, slots=True)
class FuturesRiskSummary:
    summary_id: str
    summary_version: str
    ledger_hash: str
    gross_notional: Decimal
    leverage: Decimal
    initial_margin_requirement: Decimal
    maintenance_margin_requirement: Decimal
    margin_utilization: Decimal
    cumulative_variation_margin: Decimal
    cumulative_fees: Decimal
    total_roll_cost: Decimal
    total_roll_yield: Decimal
    margin_call_count: int
    blocked_exit_count: int
    capital_usage_days: Decimal
    stress_result_hashes: tuple[str, ...]
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.summary_id, "futures_risk.summary_id")
        require_stable_id(self.summary_version, "futures_risk.summary_version")
        require_hash(self.ledger_hash, "futures_risk.ledger_hash")
        for value in self.stress_result_hashes:
            require_hash(value, "futures_risk.stress_result_hash")
        for name in (
            "gross_notional",
            "initial_margin_requirement",
            "maintenance_margin_requirement",
            "margin_utilization",
            "cumulative_fees",
            "total_roll_cost",
            "capital_usage_days",
        ):
            object.__setattr__(
                self,
                name,
                _as_decimal(
                    getattr(self, name), f"futures_risk.{name}", nonnegative=True
                ),
            )
        for name in ("leverage", "cumulative_variation_margin", "total_roll_yield"):
            object.__setattr__(
                self,
                name,
                _as_decimal(getattr(self, name), f"futures_risk.{name}"),
            )
        if self.margin_call_count < 0 or self.blocked_exit_count < 0:
            raise DerivativeResearchError("futures_risk_count_invalid")
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_risk_summary", self.identity_payload()),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "summary_id": self.summary_id,
            "summary_version": self.summary_version,
            "ledger_hash": self.ledger_hash,
            "gross_notional": _decimal_payload(self.gross_notional),
            "leverage": _decimal_payload(self.leverage),
            "initial_margin_requirement": _decimal_payload(
                self.initial_margin_requirement
            ),
            "maintenance_margin_requirement": _decimal_payload(
                self.maintenance_margin_requirement
            ),
            "margin_utilization": _decimal_payload(self.margin_utilization),
            "cumulative_variation_margin": _decimal_payload(
                self.cumulative_variation_margin
            ),
            "cumulative_fees": _decimal_payload(self.cumulative_fees),
            "total_roll_cost": _decimal_payload(self.total_roll_cost),
            "total_roll_yield": _decimal_payload(self.total_roll_yield),
            "margin_call_count": self.margin_call_count,
            "blocked_exit_count": self.blocked_exit_count,
            "capital_usage_days": _decimal_payload(self.capital_usage_days),
            "stress_result_hashes": list(self.stress_result_hashes),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def summarize_futures_risk(
    *,
    summary_id: str,
    summary_version: str,
    simulator: FuturesSimulator,
    ledger: FuturesLedger,
    marks: Sequence[ContractQuote],
    roll_executions: Sequence[RollExecution] = (),
    stress_results: Sequence[FuturesStressResult] = (),
    blocked_exit_count: int = 0,
    capital_usage_days: Decimal = _ZERO,
) -> FuturesRiskSummary:
    marks_by_id = {item.contract_id: item for item in marks}
    gross_notional = _ZERO
    for position in ledger.positions:
        quote = marks_by_id.get(position.contract_id)
        if quote is None:
            raise DerivativeResearchError("futures_risk_mark_missing")
        contract = simulator.contract_for(position.contract_id)
        gross_notional += (
            abs(position.quantity)
            * quote.close_price
            * contract.contract_multiplier
        )
    initial = simulator._margin_requirement(ledger.positions, maintenance=False)
    maintenance = simulator._margin_requirement(ledger.positions, maintenance=True)
    collateral = ledger.cash_balance * simulator.margin_policy.collateral_fraction
    leverage = _ZERO if collateral == 0 else gross_notional / collateral
    utilization = _ZERO if collateral <= 0 else initial / collateral
    return FuturesRiskSummary(
        summary_id=summary_id,
        summary_version=summary_version,
        ledger_hash=ledger.content_hash,
        gross_notional=gross_notional,
        leverage=leverage,
        initial_margin_requirement=initial,
        maintenance_margin_requirement=maintenance,
        margin_utilization=utilization,
        cumulative_variation_margin=ledger.cumulative_variation_margin,
        cumulative_fees=ledger.cumulative_fees,
        total_roll_cost=sum(
            (item.total_roll_cost for item in roll_executions), _ZERO
        ),
        total_roll_yield=sum((item.roll_yield for item in roll_executions), _ZERO),
        margin_call_count=ledger.margin_call_count,
        blocked_exit_count=blocked_exit_count,
        capital_usage_days=capital_usage_days,
        stress_result_hashes=tuple(item.content_hash for item in stress_results),
    )


@dataclass(frozen=True, slots=True)
class FuturesRobustnessSummary:
    summary_id: str
    summary_version: str
    risk_summary_hash: str
    cases: tuple[FuturesStressCase, ...]
    results: tuple[FuturesStressResult, ...]
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.summary_id, "futures_robustness.summary_id")
        require_stable_id(self.summary_version, "futures_robustness.summary_version")
        require_hash(self.risk_summary_hash, "futures_robustness.risk_summary_hash")
        case_hashes = {item.content_hash for item in self.cases}
        if len(case_hashes) != len(self.cases):
            raise DerivativeResearchError("futures_robustness_case_duplicate")
        if {item.case_hash for item in self.results} != case_hashes:
            raise DerivativeResearchError("futures_robustness_result_binding_invalid")
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("futures_robustness_summary", self.identity_payload()),
        )

    @property
    def covered_kinds(self) -> frozenset[FuturesStressKind]:
        return frozenset(item.kind for item in self.cases)

    @property
    def missing_kinds(self) -> frozenset[FuturesStressKind]:
        return frozenset(FuturesStressKind) - self.covered_kinds

    def require_complete_s5_coverage(self) -> None:
        if self.missing_kinds:
            raise DerivativeResearchError(
                "futures_robustness_coverage_missing:"
                + ",".join(sorted(item.value for item in self.missing_kinds))
            )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "summary_id": self.summary_id,
            "summary_version": self.summary_version,
            "risk_summary_hash": self.risk_summary_hash,
            "cases": [item.as_dict() for item in self.cases],
            "results": [item.as_dict() for item in self.results],
            "covered_kinds": sorted(item.value for item in self.covered_kinds),
            "missing_kinds": sorted(item.value for item in self.missing_kinds),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class ProspectiveFuturesEvidence:
    observation_id: str
    evidence_version: str
    observed_at: str
    frozen_experiment_hash: str
    chain_snapshot_hash: str
    listed_contract_ids: tuple[str, ...]
    selected_contract_id: str
    selected_quote_hash: str
    settlement_price: Decimal
    session: SessionType
    roll_decision_hash: str
    continuous_point_hash: str
    roll_execution_hash: str | None
    roll_fill_hashes: tuple[str, ...]
    roll_cost: Decimal
    margin_policy_hash: str
    settlement_policy_hash: str
    curve_feature_hash: str
    curve_slope: Decimal
    historical_curve_mean: Decimal
    historical_curve_std: Decimal
    curve_drift_zscore: Decimal
    previous_observation_hash: str | None
    content_hash: str = field(init=False)
    schema_version: int = FUTURES_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.observation_id, "prospective_futures.observation_id")
        require_stable_id(self.evidence_version, "prospective_futures.evidence_version")
        require_stable_id(
            self.selected_contract_id, "prospective_futures.selected_contract_id"
        )
        parse_timestamp(self.observed_at, "prospective_futures.observed_at")
        if not self.listed_contract_ids or self.selected_contract_id not in (
            self.listed_contract_ids
        ):
            raise DerivativeResearchError("prospective_selected_contract_not_listed")
        for value in (
            self.frozen_experiment_hash,
            self.chain_snapshot_hash,
            self.selected_quote_hash,
            self.roll_decision_hash,
            self.continuous_point_hash,
            self.margin_policy_hash,
            self.settlement_policy_hash,
            self.curve_feature_hash,
            *self.roll_fill_hashes,
        ):
            require_hash(value, "prospective_futures.evidence_hash")
        if self.roll_execution_hash is not None:
            require_hash(
                self.roll_execution_hash,
                "prospective_futures.roll_execution_hash",
            )
            if len(self.roll_fill_hashes) != 2:
                raise DerivativeResearchError(
                    "prospective_roll_requires_close_and_open_fills"
                )
        elif self.roll_fill_hashes or self.roll_cost != _ZERO:
            raise DerivativeResearchError("prospective_roll_evidence_inconsistent")
        if self.previous_observation_hash is not None:
            require_hash(
                self.previous_observation_hash,
                "prospective_futures.previous_observation_hash",
            )
        for name in ("settlement_price", "historical_curve_std"):
            object.__setattr__(
                self,
                name,
                _as_decimal(
                    getattr(self, name),
                    f"prospective_futures.{name}",
                    positive=True,
                ),
            )
        for name in (
            "roll_cost",
            "curve_slope",
            "historical_curve_mean",
            "curve_drift_zscore",
        ):
            object.__setattr__(
                self,
                name,
                _as_decimal(getattr(self, name), f"prospective_futures.{name}"),
            )
        expected_z = (
            self.curve_slope - self.historical_curve_mean
        ) / self.historical_curve_std
        if self.curve_drift_zscore != expected_z:
            raise DerivativeResearchError("prospective_curve_drift_mismatch")
        object.__setattr__(
            self,
            "content_hash",
            _hash_payload("prospective_futures_evidence", self.identity_payload()),
        )

    @classmethod
    def capture(
        cls,
        *,
        observation_id: str,
        evidence_version: str,
        frozen_experiment_hash: str,
        as_of: str,
        snapshots: Sequence[ContractChainSnapshot],
        continuous_point: ContinuousFuturesPoint,
        roll_decision: RollDecision,
        margin_policy: MarginSimulationPolicy,
        settlement_policy: SettlementPolicy,
        curve_feature: CurveFeature,
        historical_curve_mean: Decimal,
        historical_curve_std: Decimal,
        roll_execution: RollExecution | None = None,
        previous_observation: "ProspectiveFuturesEvidence | None" = None,
    ) -> "ProspectiveFuturesEvidence":
        chain = select_chain_as_of(snapshots, as_of)
        listed = tuple(item.contract_id for item in chain.listed_contracts(as_of))
        if continuous_point.observed_at != as_of:
            raise DerivativeResearchError("prospective_continuous_time_mismatch")
        if continuous_point.chain_snapshot_hash != chain.content_hash:
            raise DerivativeResearchError("prospective_chain_binding_mismatch")
        if continuous_point.roll_decision_hash != roll_decision.content_hash:
            raise DerivativeResearchError("prospective_roll_decision_mismatch")
        selected_quote = chain.quote_for(continuous_point.source_contract_id, as_of)
        if selected_quote.content_hash != continuous_point.source_quote_hash:
            raise DerivativeResearchError("prospective_selected_quote_mismatch")
        if previous_observation is not None and parse_timestamp(
            previous_observation.observed_at, "previous_observation.observed_at"
        ) >= parse_timestamp(as_of, "prospective.as_of"):
            raise DerivativeResearchError("prospective_observations_not_append_only")
        mean = _as_decimal(
            historical_curve_mean, "prospective.historical_curve_mean"
        )
        std = _as_decimal(
            historical_curve_std,
            "prospective.historical_curve_std",
            positive=True,
        )
        roll_fills: tuple[str, ...] = ()
        roll_cost = _ZERO
        roll_hash = None
        if roll_execution is not None:
            if roll_execution.decision_hash != roll_decision.content_hash:
                raise DerivativeResearchError("prospective_roll_execution_mismatch")
            roll_hash = roll_execution.content_hash
            roll_fills = (
                roll_execution.close_fill_hash,
                roll_execution.open_fill_hash,
            )
            roll_cost = roll_execution.total_roll_cost
        return cls(
            observation_id=observation_id,
            evidence_version=evidence_version,
            observed_at=as_of,
            frozen_experiment_hash=frozen_experiment_hash,
            chain_snapshot_hash=chain.content_hash,
            listed_contract_ids=listed,
            selected_contract_id=continuous_point.source_contract_id,
            selected_quote_hash=selected_quote.content_hash,
            settlement_price=selected_quote.settlement_price,
            session=selected_quote.session,
            roll_decision_hash=roll_decision.content_hash,
            continuous_point_hash=continuous_point.content_hash,
            roll_execution_hash=roll_hash,
            roll_fill_hashes=roll_fills,
            roll_cost=roll_cost,
            margin_policy_hash=margin_policy.content_hash,
            settlement_policy_hash=settlement_policy.content_hash,
            curve_feature_hash=curve_feature.content_hash,
            curve_slope=curve_feature.annualized_slope,
            historical_curve_mean=mean,
            historical_curve_std=std,
            curve_drift_zscore=(curve_feature.annualized_slope - mean) / std,
            previous_observation_hash=(
                None
                if previous_observation is None
                else previous_observation.content_hash
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "observation_id": self.observation_id,
            "evidence_version": self.evidence_version,
            "observed_at": self.observed_at,
            "frozen_experiment_hash": self.frozen_experiment_hash,
            "chain_snapshot_hash": self.chain_snapshot_hash,
            "listed_contract_ids": list(self.listed_contract_ids),
            "selected_contract_id": self.selected_contract_id,
            "selected_quote_hash": self.selected_quote_hash,
            "settlement_price": _decimal_payload(self.settlement_price),
            "session": self.session.value,
            "roll_decision_hash": self.roll_decision_hash,
            "continuous_point_hash": self.continuous_point_hash,
            "roll_execution_hash": self.roll_execution_hash,
            "roll_fill_hashes": list(self.roll_fill_hashes),
            "roll_cost": _decimal_payload(self.roll_cost),
            "margin_policy_hash": self.margin_policy_hash,
            "settlement_policy_hash": self.settlement_policy_hash,
            "curve_feature_hash": self.curve_feature_hash,
            "curve_slope": _decimal_payload(self.curve_slope),
            "historical_curve_mean": _decimal_payload(
                self.historical_curve_mean
            ),
            "historical_curve_std": _decimal_payload(self.historical_curve_std),
            "curve_drift_zscore": _decimal_payload(self.curve_drift_zscore),
            "previous_observation_hash": self.previous_observation_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


__all__ = [
    "AdjustmentDirection",
    "BasisFeature",
    "CompositeOperator",
    "ContractChainSnapshot",
    "ContractQuote",
    "ContinuousAdjustment",
    "ContinuousFuturesPoint",
    "ContinuousFuturesPolicy",
    "ExpiryPolicy",
    "FUTURES_RESEARCH_SCHEMA_VERSION",
    "FuturesContract",
    "FuturesCostPolicy",
    "FuturesFill",
    "FuturesLedger",
    "FuturesLifecycleEvent",
    "FuturesOrderIntent",
    "FuturesPosition",
    "FuturesRiskSummary",
    "FuturesRobustnessSummary",
    "FuturesRoot",
    "FuturesSimulator",
    "FuturesSpreadOrder",
    "FuturesStressCase",
    "FuturesStressExecution",
    "FuturesStressInputs",
    "FuturesStressKind",
    "FuturesStressResult",
    "LifecycleEventType",
    "MarginCallAction",
    "MarginCallEvent",
    "MarginSimulationPolicy",
    "MarketState",
    "OrderSide",
    "PhysicalDeliveryAction",
    "ProspectiveFuturesEvidence",
    "RollAttributionFeature",
    "RollDecision",
    "RollExecution",
    "RollPolicy",
    "RollTrigger",
    "SessionType",
    "SettlementEvent",
    "SettlementPolicy",
    "SettlementType",
    "SimulationStep",
    "SpreadExecution",
    "SpreadLeg",
    "attribute_roll_return",
    "build_continuous_point",
    "compute_basis_feature",
    "compute_curve_feature",
    "decide_roll",
    "run_futures_stress_case",
    "select_chain_as_of",
    "summarize_futures_risk",
]
