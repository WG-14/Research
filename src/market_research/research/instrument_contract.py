"""Immutable instrument identity and exact unit contracts for offline research.

The research engine historically used a display/vendor market code as its only
identity.  This module separates that compatibility code from an immutable
internal instrument identity and keeps derivative-specific data behind typed
extension contracts.  It deliberately contains no venue, account, or order
adapter logic.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_EVEN
from typing import Any, Mapping

from .hashing import sha256_prefixed


INSTRUMENT_CONTRACT_SCHEMA_VERSION = 1
_LEGACY_NAMESPACE = uuid.UUID("ad6bb9a1-3886-54b2-9b76-f059f53bd624")
_INSTRUMENT_ID = re.compile(r"^inst_[a-z0-9][a-z0-9_-]{7,63}$")
_VERSION_ID = re.compile(r"^instv_[a-z0-9][a-z0-9_-]{7,63}$")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_CURRENCY = re.compile(r"^[A-Z][A-Z0-9]{2,11}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_ASSET_TYPES = frozenset({"spot", "equity", "etf", "future", "option"})
_ROUNDING_POLICIES = frozenset({"reject", "down", "half_even"})


class InstrumentContractError(ValueError):
    """An instrument identity, unit, or extension contract is invalid."""


def decimal_value(value: object, field: str, *, positive: bool = False) -> Decimal:
    """Parse a finite base-10 value without accepting binary float ambiguity."""

    if isinstance(value, bool) or isinstance(value, float):
        raise InstrumentContractError(f"{field}_must_be_decimal_string_or_integer")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InstrumentContractError(f"{field}_invalid_decimal") from exc
    if not parsed.is_finite():
        raise InstrumentContractError(f"{field}_non_finite")
    if positive and parsed <= 0:
        raise InstrumentContractError(f"{field}_must_be_positive")
    return parsed


def decimal_text(value: Decimal) -> str:
    """Canonical, non-exponent decimal representation used in hashes."""

    normalized = value.normalize()
    if normalized == 0:
        return "0"
    return format(normalized, "f")


@dataclass(frozen=True, slots=True)
class VendorSymbolMapping:
    provider_id: str
    symbol: str
    effective_from: str
    effective_to: str | None = None

    def __post_init__(self) -> None:
        _require_stable_id(self.provider_id, "vendor_mapping.provider_id")
        _require_text(self.symbol, "vendor_mapping.symbol")
        start = _timestamp(self.effective_from, "vendor_mapping.effective_from")
        if self.effective_to is not None:
            end = _timestamp(self.effective_to, "vendor_mapping.effective_to")
            if end <= start:
                raise InstrumentContractError("vendor_mapping_effective_range_invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "symbol": self.symbol,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
        }


@dataclass(frozen=True, slots=True)
class InstrumentName:
    name: str
    effective_from: str
    effective_to: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.name, "instrument_name.name")
        start = _timestamp(self.effective_from, "instrument_name.effective_from")
        if self.effective_to is not None:
            end = _timestamp(self.effective_to, "instrument_name.effective_to")
            if end <= start:
                raise InstrumentContractError("instrument_name_effective_range_invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
        }


@dataclass(frozen=True, slots=True)
class FuturesExtension:
    contract_code: str
    underlying_instrument_id: str
    expiry_at: str
    contract_multiplier: Decimal
    margin_currency: str
    initial_margin_ratio: Decimal
    maintenance_margin_ratio: Decimal
    settlement_type: str
    continuous_series_policy_id: str
    roll_policy_id: str
    basis_unit: str
    session_calendar_id: str
    max_leverage_ratio: Decimal

    def __post_init__(self) -> None:
        _require_text(self.contract_code, "futures.contract_code")
        _require_instrument_id(
            self.underlying_instrument_id, "futures.underlying_instrument_id"
        )
        _timestamp(self.expiry_at, "futures.expiry_at")
        _require_positive(self.contract_multiplier, "futures.contract_multiplier")
        _require_currency(self.margin_currency, "futures.margin_currency")
        _require_ratio(self.initial_margin_ratio, "futures.initial_margin_ratio")
        _require_ratio(
            self.maintenance_margin_ratio, "futures.maintenance_margin_ratio"
        )
        if self.maintenance_margin_ratio > self.initial_margin_ratio:
            raise InstrumentContractError("futures_margin_ratio_order_invalid")
        if self.settlement_type not in {"cash", "physical"}:
            raise InstrumentContractError("futures.settlement_type_unknown")
        for field, value in (
            ("continuous_series_policy_id", self.continuous_series_policy_id),
            ("roll_policy_id", self.roll_policy_id),
            ("basis_unit", self.basis_unit),
            ("session_calendar_id", self.session_calendar_id),
        ):
            _require_stable_id(value, f"futures.{field}")
        _require_positive(self.max_leverage_ratio, "futures.max_leverage_ratio")

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_code": self.contract_code,
            "underlying_instrument_id": self.underlying_instrument_id,
            "expiry_at": self.expiry_at,
            "contract_multiplier": decimal_text(self.contract_multiplier),
            "margin_currency": self.margin_currency,
            "initial_margin_ratio": decimal_text(self.initial_margin_ratio),
            "maintenance_margin_ratio": decimal_text(self.maintenance_margin_ratio),
            "settlement_type": self.settlement_type,
            "continuous_series_policy_id": self.continuous_series_policy_id,
            "roll_policy_id": self.roll_policy_id,
            "basis_unit": self.basis_unit,
            "session_calendar_id": self.session_calendar_id,
            "max_leverage_ratio": decimal_text(self.max_leverage_ratio),
        }


@dataclass(frozen=True, slots=True)
class OptionExtension:
    option_type: str
    underlying_instrument_id: str
    strike_price: Decimal
    expiry_at: str
    contract_multiplier: Decimal
    premium_currency: str
    settlement_type: str
    greeks_policy_id: str
    implied_volatility_policy_id: str
    volatility_surface_id: str
    position_group_policy_id: str
    expiry_payoff_policy_id: str
    liquidity_policy_id: str

    def __post_init__(self) -> None:
        if self.option_type not in {"call", "put"}:
            raise InstrumentContractError("option.option_type_unknown")
        _require_instrument_id(
            self.underlying_instrument_id, "option.underlying_instrument_id"
        )
        _require_positive(self.strike_price, "option.strike_price")
        _timestamp(self.expiry_at, "option.expiry_at")
        _require_positive(self.contract_multiplier, "option.contract_multiplier")
        _require_currency(self.premium_currency, "option.premium_currency")
        if self.settlement_type not in {"cash", "physical"}:
            raise InstrumentContractError("option.settlement_type_unknown")
        for field, value in (
            ("greeks_policy_id", self.greeks_policy_id),
            ("implied_volatility_policy_id", self.implied_volatility_policy_id),
            ("volatility_surface_id", self.volatility_surface_id),
            ("position_group_policy_id", self.position_group_policy_id),
            ("expiry_payoff_policy_id", self.expiry_payoff_policy_id),
            ("liquidity_policy_id", self.liquidity_policy_id),
        ):
            _require_stable_id(value, f"option.{field}")

    def as_dict(self) -> dict[str, object]:
        return {
            "option_type": self.option_type,
            "underlying_instrument_id": self.underlying_instrument_id,
            "strike_price": decimal_text(self.strike_price),
            "expiry_at": self.expiry_at,
            "contract_multiplier": decimal_text(self.contract_multiplier),
            "premium_currency": self.premium_currency,
            "settlement_type": self.settlement_type,
            "greeks_policy_id": self.greeks_policy_id,
            "implied_volatility_policy_id": self.implied_volatility_policy_id,
            "volatility_surface_id": self.volatility_surface_id,
            "position_group_policy_id": self.position_group_policy_id,
            "expiry_payoff_policy_id": self.expiry_payoff_policy_id,
            "liquidity_policy_id": self.liquidity_policy_id,
        }


@dataclass(frozen=True, slots=True)
class InstrumentMaster:
    schema_version: int
    instrument_id: str
    instrument_version_id: str
    version: int
    asset_type: str
    exchange_mic: str
    trading_currency: str
    price_tick: Decimal
    quantity_step: Decimal
    trading_unit: Decimal
    listed_on: str
    delisted_on: str | None
    name_history: tuple[InstrumentName, ...]
    vendor_mappings: tuple[VendorSymbolMapping, ...]
    etf_underlying_index_id: str | None = None
    futures: FuturesExtension | None = None
    option: OptionExtension | None = None
    source: str = "manifest"

    def __post_init__(self) -> None:
        if self.schema_version != INSTRUMENT_CONTRACT_SCHEMA_VERSION:
            raise InstrumentContractError("instrument_schema_version_unsupported")
        _require_instrument_id(self.instrument_id, "instrument.instrument_id")
        if not _VERSION_ID.fullmatch(self.instrument_version_id):
            raise InstrumentContractError("instrument.instrument_version_id_invalid")
        if isinstance(self.version, bool) or self.version < 1:
            raise InstrumentContractError("instrument.version_invalid")
        if self.asset_type not in _ASSET_TYPES:
            raise InstrumentContractError("instrument.asset_type_unknown")
        if not re.fullmatch(r"[A-Z0-9]{4}", self.exchange_mic):
            raise InstrumentContractError("instrument.exchange_mic_invalid")
        _require_currency(self.trading_currency, "instrument.trading_currency")
        _require_positive(self.price_tick, "instrument.price_tick")
        _require_positive(self.quantity_step, "instrument.quantity_step")
        _require_positive(self.trading_unit, "instrument.trading_unit")
        listed = _date(self.listed_on, "instrument.listed_on")
        if (
            self.delisted_on is not None
            and _date(self.delisted_on, "instrument.delisted_on") < listed
        ):
            raise InstrumentContractError("instrument_listing_range_invalid")
        if not self.name_history:
            raise InstrumentContractError("instrument.name_history_required")
        if not self.vendor_mappings:
            raise InstrumentContractError("instrument.vendor_mappings_required")
        _require_non_overlapping_mappings(self.vendor_mappings)
        if self.asset_type == "etf" and not self.etf_underlying_index_id:
            raise InstrumentContractError("instrument.etf_underlying_index_id_required")
        if self.etf_underlying_index_id is not None:
            _require_stable_id(
                self.etf_underlying_index_id,
                "instrument.etf_underlying_index_id",
            )
        if (self.asset_type == "future") != (self.futures is not None):
            raise InstrumentContractError("instrument.futures_extension_mismatch")
        if (self.asset_type == "option") != (self.option is not None):
            raise InstrumentContractError("instrument.option_extension_mismatch")
        if self.futures is not None and self.option is not None:
            raise InstrumentContractError("instrument_derivative_extensions_conflict")
        if self.source not in {"manifest", "legacy_market_mapping"}:
            raise InstrumentContractError("instrument.source_unknown")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "instrument_id": self.instrument_id,
            "instrument_version_id": self.instrument_version_id,
            "version": self.version,
            "asset_type": self.asset_type,
            "exchange_mic": self.exchange_mic,
            "trading_currency": self.trading_currency,
            "price_tick": decimal_text(self.price_tick),
            "quantity_step": decimal_text(self.quantity_step),
            "trading_unit": decimal_text(self.trading_unit),
            "listed_on": self.listed_on,
            "delisted_on": self.delisted_on,
            "name_history": [item.as_dict() for item in self.name_history],
            "vendor_mappings": [item.as_dict() for item in self.vendor_mappings],
            "etf_underlying_index_id": self.etf_underlying_index_id,
            "futures": self.futures.as_dict() if self.futures else None,
            "option": self.option.as_dict() if self.option else None,
            "source": self.source,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="instrument_master_contract")

    def require_market_mapping(self, market: str) -> None:
        if not any(
            item.provider_id == "manifest_market" and item.symbol == market
            for item in self.vendor_mappings
        ):
            raise InstrumentContractError("instrument_manifest_market_mapping_missing")

    def validate_price(self, value: object) -> Decimal:
        return require_increment(value, self.price_tick, "price")

    def validate_quantity(self, value: object) -> Decimal:
        return require_increment(value, self.quantity_step, "quantity")

    def round_quantity(self, value: object, *, policy: str) -> Decimal:
        return round_to_increment(
            value, self.quantity_step, policy=policy, field="quantity"
        )


@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if not self.amount.is_finite():
            raise InstrumentContractError("money.amount_non_finite")
        _require_currency(self.currency, "money.currency")

    def as_dict(self) -> dict[str, str]:
        return {"amount": decimal_text(self.amount), "currency": self.currency}


@dataclass(frozen=True, slots=True)
class Ratio:
    """Dimensionless ratio where two percent is exactly ``0.02``."""

    value: Decimal

    def __post_init__(self) -> None:
        if not self.value.is_finite():
            raise InstrumentContractError("ratio_non_finite")

    def as_dict(self) -> dict[str, str]:
        return {"value": decimal_text(self.value), "unit": "ratio_1_equals_100_percent"}


@dataclass(frozen=True, slots=True)
class GenericPositionLeg:
    """Derivative-ready research position boundary, not an execution position."""

    instrument_id: str
    quantity: Decimal
    quantity_unit: str
    entry_price: Money
    contract_multiplier: Decimal
    side: str
    leg_id: str

    def __post_init__(self) -> None:
        _require_instrument_id(self.instrument_id, "position_leg.instrument_id")
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise InstrumentContractError("position_leg.quantity_invalid")
        _require_stable_id(self.quantity_unit, "position_leg.quantity_unit")
        _require_positive(self.contract_multiplier, "position_leg.contract_multiplier")
        if self.side not in {"long", "short"}:
            raise InstrumentContractError("position_leg.side_unknown")
        _require_stable_id(self.leg_id, "position_leg.leg_id")

    def as_dict(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "quantity": decimal_text(self.quantity),
            "quantity_unit": self.quantity_unit,
            "entry_price": self.entry_price.as_dict(),
            "contract_multiplier": decimal_text(self.contract_multiplier),
            "side": self.side,
            "leg_id": self.leg_id,
        }


def parse_instrument_master(value: object) -> InstrumentMaster:
    payload = _object(value, "instrument")
    _unknown(
        payload,
        {
            "schema_version",
            "instrument_id",
            "instrument_version_id",
            "version",
            "asset_type",
            "exchange_mic",
            "trading_currency",
            "price_tick",
            "quantity_step",
            "trading_unit",
            "listed_on",
            "delisted_on",
            "name_history",
            "vendor_mappings",
            "etf_underlying_index_id",
            "futures",
            "option",
            "source",
        },
        "instrument",
    )
    names = _array(payload.get("name_history"), "instrument.name_history")
    mappings = _array(payload.get("vendor_mappings"), "instrument.vendor_mappings")
    return InstrumentMaster(
        schema_version=_integer(
            payload.get("schema_version"), "instrument.schema_version"
        ),
        instrument_id=_text(payload.get("instrument_id"), "instrument.instrument_id"),
        instrument_version_id=_text(
            payload.get("instrument_version_id"), "instrument.instrument_version_id"
        ),
        version=_integer(payload.get("version"), "instrument.version"),
        asset_type=_text(payload.get("asset_type"), "instrument.asset_type"),
        exchange_mic=_text(payload.get("exchange_mic"), "instrument.exchange_mic"),
        trading_currency=_text(
            payload.get("trading_currency"), "instrument.trading_currency"
        ),
        price_tick=decimal_value(
            payload.get("price_tick"), "instrument.price_tick", positive=True
        ),
        quantity_step=decimal_value(
            payload.get("quantity_step"), "instrument.quantity_step", positive=True
        ),
        trading_unit=decimal_value(
            payload.get("trading_unit"), "instrument.trading_unit", positive=True
        ),
        listed_on=_text(payload.get("listed_on"), "instrument.listed_on"),
        delisted_on=_optional_text(
            payload.get("delisted_on"), "instrument.delisted_on"
        ),
        name_history=tuple(_parse_name(item) for item in names),
        vendor_mappings=tuple(_parse_mapping(item) for item in mappings),
        etf_underlying_index_id=_optional_text(
            payload.get("etf_underlying_index_id"),
            "instrument.etf_underlying_index_id",
        ),
        futures=_parse_futures(payload["futures"])
        if payload.get("futures") is not None
        else None,
        option=_parse_option(payload["option"])
        if payload.get("option") is not None
        else None,
        source=str(payload.get("source") or "manifest"),
    )


def derive_legacy_instrument_master(market: str) -> InstrumentMaster:
    """Compatibility identity for old research-only manifests.

    The UUID namespace makes the internal ID deterministic and explicitly marks
    that it was derived from the historical display code.  New manifests should
    carry an authoritative ``instrument`` object instead.
    """

    symbol = str(market).strip().upper()
    token = uuid.uuid5(_LEGACY_NAMESPACE, symbol).hex
    quote = symbol.split("-", 1)[0] if "-" in symbol else "UNK"
    return InstrumentMaster(
        schema_version=1,
        instrument_id=f"inst_{token[:24]}",
        instrument_version_id=f"instv_{token[:24]}_v1",
        version=1,
        asset_type="spot",
        exchange_mic="XOFF",
        trading_currency=quote if _CURRENCY.fullmatch(quote) else "UNK",
        price_tick=Decimal("0.00000001"),
        quantity_step=Decimal("0.00000001"),
        trading_unit=Decimal("1"),
        listed_on="1970-01-01",
        delisted_on=None,
        name_history=(InstrumentName(symbol, "1970-01-01T00:00:00+00:00"),),
        vendor_mappings=(
            VendorSymbolMapping("manifest_market", symbol, "1970-01-01T00:00:00+00:00"),
        ),
        source="legacy_market_mapping",
    )


def require_increment(value: object, increment: Decimal, field: str) -> Decimal:
    parsed = decimal_value(value, field)
    if parsed % increment != 0:
        raise InstrumentContractError(f"{field}_not_aligned_to_increment")
    return parsed


def round_to_increment(
    value: object, increment: Decimal, *, policy: str, field: str
) -> Decimal:
    if policy not in _ROUNDING_POLICIES:
        raise InstrumentContractError(f"{field}_rounding_policy_unknown")
    parsed = decimal_value(value, field)
    if parsed % increment == 0:
        return parsed
    if policy == "reject":
        raise InstrumentContractError(f"{field}_not_aligned_to_increment")
    units = parsed / increment
    rounding = ROUND_DOWN if policy == "down" else ROUND_HALF_EVEN
    return units.to_integral_value(rounding=rounding) * increment


def require_hash(value: str, field: str) -> None:
    if not _HASH.fullmatch(value):
        raise InstrumentContractError(f"{field}_invalid_hash")


def _parse_name(value: object) -> InstrumentName:
    payload = _object(value, "instrument.name_history[]")
    _unknown(
        payload, {"name", "effective_from", "effective_to"}, "instrument.name_history[]"
    )
    return InstrumentName(
        _text(payload.get("name"), "instrument.name_history[].name"),
        _text(
            payload.get("effective_from"), "instrument.name_history[].effective_from"
        ),
        _optional_text(
            payload.get("effective_to"), "instrument.name_history[].effective_to"
        ),
    )


def _parse_mapping(value: object) -> VendorSymbolMapping:
    payload = _object(value, "instrument.vendor_mappings[]")
    _unknown(
        payload,
        {"provider_id", "symbol", "effective_from", "effective_to"},
        "instrument.vendor_mappings[]",
    )
    return VendorSymbolMapping(
        _text(payload.get("provider_id"), "instrument.vendor_mappings[].provider_id"),
        _text(payload.get("symbol"), "instrument.vendor_mappings[].symbol"),
        _text(
            payload.get("effective_from"), "instrument.vendor_mappings[].effective_from"
        ),
        _optional_text(
            payload.get("effective_to"), "instrument.vendor_mappings[].effective_to"
        ),
    )


def _parse_futures(value: object) -> FuturesExtension:
    payload = _object(value, "instrument.futures")
    fields = {
        "contract_code",
        "underlying_instrument_id",
        "expiry_at",
        "contract_multiplier",
        "margin_currency",
        "initial_margin_ratio",
        "maintenance_margin_ratio",
        "settlement_type",
        "continuous_series_policy_id",
        "roll_policy_id",
        "basis_unit",
        "session_calendar_id",
        "max_leverage_ratio",
    }
    _unknown(payload, fields, "instrument.futures")
    return FuturesExtension(
        contract_code=_text(
            payload.get("contract_code"), "instrument.futures.contract_code"
        ),
        underlying_instrument_id=_text(
            payload.get("underlying_instrument_id"),
            "instrument.futures.underlying_instrument_id",
        ),
        expiry_at=_text(payload.get("expiry_at"), "instrument.futures.expiry_at"),
        contract_multiplier=decimal_value(
            payload.get("contract_multiplier"),
            "instrument.futures.contract_multiplier",
            positive=True,
        ),
        margin_currency=_text(
            payload.get("margin_currency"), "instrument.futures.margin_currency"
        ),
        initial_margin_ratio=decimal_value(
            payload.get("initial_margin_ratio"),
            "instrument.futures.initial_margin_ratio",
            positive=True,
        ),
        maintenance_margin_ratio=decimal_value(
            payload.get("maintenance_margin_ratio"),
            "instrument.futures.maintenance_margin_ratio",
            positive=True,
        ),
        settlement_type=_text(
            payload.get("settlement_type"), "instrument.futures.settlement_type"
        ),
        continuous_series_policy_id=_text(
            payload.get("continuous_series_policy_id"),
            "instrument.futures.continuous_series_policy_id",
        ),
        roll_policy_id=_text(
            payload.get("roll_policy_id"), "instrument.futures.roll_policy_id"
        ),
        basis_unit=_text(payload.get("basis_unit"), "instrument.futures.basis_unit"),
        session_calendar_id=_text(
            payload.get("session_calendar_id"),
            "instrument.futures.session_calendar_id",
        ),
        max_leverage_ratio=decimal_value(
            payload.get("max_leverage_ratio"),
            "instrument.futures.max_leverage_ratio",
            positive=True,
        ),
    )


def _parse_option(value: object) -> OptionExtension:
    payload = _object(value, "instrument.option")
    fields = {
        "option_type",
        "underlying_instrument_id",
        "strike_price",
        "expiry_at",
        "contract_multiplier",
        "premium_currency",
        "settlement_type",
        "greeks_policy_id",
        "implied_volatility_policy_id",
        "volatility_surface_id",
        "position_group_policy_id",
        "expiry_payoff_policy_id",
        "liquidity_policy_id",
    }
    _unknown(payload, fields, "instrument.option")
    return OptionExtension(
        option_type=_text(payload.get("option_type"), "instrument.option.option_type"),
        underlying_instrument_id=_text(
            payload.get("underlying_instrument_id"),
            "instrument.option.underlying_instrument_id",
        ),
        strike_price=decimal_value(
            payload.get("strike_price"), "instrument.option.strike_price", positive=True
        ),
        expiry_at=_text(payload.get("expiry_at"), "instrument.option.expiry_at"),
        contract_multiplier=decimal_value(
            payload.get("contract_multiplier"),
            "instrument.option.contract_multiplier",
            positive=True,
        ),
        premium_currency=_text(
            payload.get("premium_currency"), "instrument.option.premium_currency"
        ),
        settlement_type=_text(
            payload.get("settlement_type"), "instrument.option.settlement_type"
        ),
        greeks_policy_id=_text(
            payload.get("greeks_policy_id"), "instrument.option.greeks_policy_id"
        ),
        implied_volatility_policy_id=_text(
            payload.get("implied_volatility_policy_id"),
            "instrument.option.implied_volatility_policy_id",
        ),
        volatility_surface_id=_text(
            payload.get("volatility_surface_id"),
            "instrument.option.volatility_surface_id",
        ),
        position_group_policy_id=_text(
            payload.get("position_group_policy_id"),
            "instrument.option.position_group_policy_id",
        ),
        expiry_payoff_policy_id=_text(
            payload.get("expiry_payoff_policy_id"),
            "instrument.option.expiry_payoff_policy_id",
        ),
        liquidity_policy_id=_text(
            payload.get("liquidity_policy_id"),
            "instrument.option.liquidity_policy_id",
        ),
    )


def _require_non_overlapping_mappings(
    mappings: tuple[VendorSymbolMapping, ...],
) -> None:
    for index, left in enumerate(mappings):
        left_start = _timestamp(left.effective_from, "vendor_mapping.effective_from")
        left_end = (
            _timestamp(left.effective_to, "vendor_mapping.effective_to")
            if left.effective_to
            else datetime.max.replace(tzinfo=left_start.tzinfo)
        )
        for right in mappings[index + 1 :]:
            if left.provider_id != right.provider_id:
                continue
            right_start = _timestamp(
                right.effective_from, "vendor_mapping.effective_from"
            )
            right_end = (
                _timestamp(right.effective_to, "vendor_mapping.effective_to")
                if right.effective_to
                else datetime.max.replace(tzinfo=right_start.tzinfo)
            )
            if max(left_start, right_start) < min(left_end, right_end):
                raise InstrumentContractError("vendor_mapping_effective_ranges_overlap")


def _require_instrument_id(value: str, field: str) -> None:
    if not _INSTRUMENT_ID.fullmatch(value):
        raise InstrumentContractError(f"{field}_invalid")


def _require_stable_id(value: str, field: str) -> None:
    if not _STABLE_ID.fullmatch(value):
        raise InstrumentContractError(f"{field}_invalid")


def _require_currency(value: str, field: str) -> None:
    if not _CURRENCY.fullmatch(value):
        raise InstrumentContractError(f"{field}_invalid")


def _require_positive(value: Decimal, field: str) -> None:
    if not value.is_finite() or value <= 0:
        raise InstrumentContractError(f"{field}_must_be_positive")


def _require_ratio(value: Decimal, field: str) -> None:
    if not value.is_finite() or not Decimal("0") < value <= Decimal("1"):
        raise InstrumentContractError(f"{field}_must_be_ratio")


def _timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InstrumentContractError(f"{field}_invalid_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InstrumentContractError(f"{field}_timezone_required")
    return parsed


def _date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InstrumentContractError(f"{field}_invalid_date") from exc


def _require_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InstrumentContractError(f"{field}_required")


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise InstrumentContractError(f"{field}_must_be_object")
    return value


def _array(value: object, field: str) -> list[object]:
    if not isinstance(value, list) or not value:
        raise InstrumentContractError(f"{field}_must_be_non_empty_array")
    return list(value)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InstrumentContractError(f"{field}_required")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InstrumentContractError(f"{field}_must_be_integer")
    return value


def _unknown(payload: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise InstrumentContractError(f"{field}_unknown_fields:{','.join(unknown)}")
