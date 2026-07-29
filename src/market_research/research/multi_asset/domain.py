"""Immutable multi-asset product-master contracts for offline research.

The existing spot, futures, and options models remain the authorities for their
product-specific simulation semantics.  This module only supplies neutral
identity, validity, source, and relationship contracts that can bind those
models without importing or changing them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Callable, Iterable, TypeVar

from ..hashing import sha256_prefixed
from ..instrument_kinds import InstrumentKind


MULTI_ASSET_DOMAIN_SCHEMA_VERSION = 3
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_CURRENCY = re.compile(r"^[A-Z][A-Z0-9]{2,11}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class ProductMasterError(ValueError):
    """The product master is incomplete, ambiguous, or inconsistent."""


class SettlementType(StrEnum):
    CASH = "CASH"
    PHYSICAL = "PHYSICAL"


class InstrumentTradingStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DELISTED = "DELISTED"
    EXPIRED = "EXPIRED"
    NON_TRADABLE_REFERENCE = "NON_TRADABLE_REFERENCE"


class LifecycleEventType(StrEnum):
    LISTING = "LISTING"
    DELISTING = "DELISTING"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"
    SPLIT = "SPLIT"
    CASH_DISTRIBUTION = "CASH_DISTRIBUTION"
    MERGER = "MERGER"
    EXPIRY = "EXPIRY"
    EXERCISE = "EXERCISE"
    ASSIGNMENT = "ASSIGNMENT"
    SETTLEMENT = "SETTLEMENT"
    DELIVERY = "DELIVERY"
    CONTRACT_ADJUSTMENT = "CONTRACT_ADJUSTMENT"
    SPINOFF = "SPINOFF"
    TENDER_OFFER = "TENDER_OFFER"
    LIQUIDATION = "LIQUIDATION"


class InstrumentRelationshipType(StrEnum):
    """Typed edges whose endpoint kinds are checked by the registry."""

    FUTURE_UNDERLYING = "FUTURE_UNDERLYING"
    OPTION_UNDERLYING = "OPTION_UNDERLYING"
    OPTION_DELIVERABLE = "OPTION_DELIVERABLE"
    FUTURE_OPTION_DELIVERABLE = "FUTURE_OPTION_DELIVERABLE"
    OPTION_DELIVERS_FUTURE = "FUTURE_OPTION_DELIVERABLE"
    TRACKS = "TRACKS"
    CONVERTS_TO = "CONVERTS_TO"
    HEDGE_PROXY = "HEDGE_PROXY"


class IssuerIdentifierNamespace(StrEnum):
    """Supported issuer namespaces; provider-specific aliases remain explicit."""

    LEI = "LEI"
    NATIONAL_REGISTRY = "NATIONAL_REGISTRY"
    TAX = "TAX"
    PROVIDER = "PROVIDER"


def _timestamp(value: str, field: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ProductMasterError(f"{field}_invalid_timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ProductMasterError(f"{field}_timezone_required")
    return result.astimezone(timezone.utc)


def _timestamp_text(value: str, field: str) -> str:
    return _timestamp(value, field).isoformat()


def _require_id(value: str, field: str) -> None:
    if not isinstance(value, str) or not _STABLE_ID.fullmatch(value):
        raise ProductMasterError(f"{field}_invalid_stable_id")


def _require_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ProductMasterError(f"{field}_required")


def _require_currency(value: str, field: str) -> None:
    if not isinstance(value, str) or not _CURRENCY.fullmatch(value):
        raise ProductMasterError(f"{field}_invalid_currency")


def _require_hash(value: str, field: str) -> None:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ProductMasterError(f"{field}_invalid_hash")


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _positive_decimal(value: Decimal, field: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ProductMasterError(f"{field}_must_be_positive_decimal")


@dataclass(frozen=True, slots=True)
class EffectivePeriod:
    """Half-open UTC validity interval ``[valid_from, valid_to)``."""

    valid_from: str
    valid_to: str | None = None

    def __post_init__(self) -> None:
        start = _timestamp_text(self.valid_from, "validity.valid_from")
        object.__setattr__(self, "valid_from", start)
        if self.valid_to is not None:
            end = _timestamp_text(self.valid_to, "validity.valid_to")
            if _timestamp(end, "validity.valid_to") <= _timestamp(
                start, "validity.valid_from"
            ):
                raise ProductMasterError("validity_range_invalid")
            object.__setattr__(self, "valid_to", end)

    @property
    def start(self) -> datetime:
        return _timestamp(self.valid_from, "validity.valid_from")

    @property
    def end(self) -> datetime | None:
        if self.valid_to is None:
            return None
        return _timestamp(self.valid_to, "validity.valid_to")

    def contains(self, at: str) -> bool:
        instant = _timestamp(at, "validity.at")
        return self.start <= instant and (self.end is None or instant < self.end)

    def overlaps(self, other: EffectivePeriod) -> bool:
        return (self.end is None or other.start < self.end) and (
            other.end is None or self.start < other.end
        )

    def covers(self, other: EffectivePeriod) -> bool:
        starts_before = self.start <= other.start
        ends_after = self.end is None or (
            other.end is not None and self.end >= other.end
        )
        return starts_before and ends_after

    def as_dict(self) -> dict[str, str | None]:
        return {"valid_from": self.valid_from, "valid_to": self.valid_to}


@dataclass(frozen=True, slots=True)
class SourceReference:
    """Binding to an externally prepared immutable source artifact."""

    source_id: str
    source_version: str
    content_hash: str
    observed_at: str
    source_uri: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.source_id, "source.source_id")
        _require_id(self.source_version, "source.source_version")
        _require_hash(self.content_hash, "source.content_hash")
        object.__setattr__(
            self,
            "observed_at",
            _timestamp_text(self.observed_at, "source.observed_at"),
        )
        if self.source_uri is not None:
            _require_text(self.source_uri, "source.source_uri")

    def known_at(self, knowledge_at: str) -> bool:
        """Return whether this immutable source was available by the cutoff."""

        return _timestamp(self.observed_at, "source.observed_at") <= _timestamp(
            knowledge_at,
            "source.knowledge_at",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "source_version": self.source_version,
            "content_hash": self.content_hash,
            "observed_at": self.observed_at,
            "source_uri": self.source_uri,
        }


@dataclass(frozen=True, slots=True)
class EconomicUnderlying:
    underlying_id: str
    name: str
    asset_class: str
    unit: str
    validity: EffectivePeriod
    source: SourceReference
    currency: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.underlying_id, "underlying.underlying_id")
        _require_text(self.name, "underlying.name")
        _require_id(self.asset_class, "underlying.asset_class")
        _require_id(self.unit, "underlying.unit")
        if self.currency is not None:
            _require_currency(self.currency, "underlying.currency")

    def as_dict(self) -> dict[str, object]:
        return {
            "underlying_id": self.underlying_id,
            "name": self.name,
            "asset_class": self.asset_class,
            "unit": self.unit,
            "currency": self.currency,
            "validity": self.validity.as_dict(),
            "source": self.source.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class Issuer:
    issuer_id: str
    legal_name: str
    jurisdiction: str
    validity: EffectivePeriod
    source: SourceReference
    legal_entity_identifier: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.issuer_id, "issuer.issuer_id")
        _require_text(self.legal_name, "issuer.legal_name")
        _require_id(self.jurisdiction, "issuer.jurisdiction")
        if self.legal_entity_identifier is not None:
            _require_id(
                self.legal_entity_identifier,
                "issuer.legal_entity_identifier",
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "issuer_id": self.issuer_id,
            "legal_name": self.legal_name,
            "jurisdiction": self.jurisdiction,
            "legal_entity_identifier": self.legal_entity_identifier,
            "validity": self.validity.as_dict(),
            "source": self.source.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class IssuerIdentifierRevision:
    """Append-only correction and validity history for one issuer identifier."""

    identifier_id: str
    revision: int
    issuer_id: str
    namespace: IssuerIdentifierNamespace
    value: str
    validity: EffectivePeriod
    knowledge_at: str
    source: SourceReference
    jurisdiction: str | None = None
    provider_id: str | None = None
    supersedes_hash: str | None = None
    correction_reason: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.identifier_id, "issuer_identifier.identifier_id")
        _require_id(self.issuer_id, "issuer_identifier.issuer_id")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ProductMasterError("issuer_identifier.revision_invalid")
        if not isinstance(self.namespace, IssuerIdentifierNamespace):
            raise ProductMasterError("issuer_identifier.namespace_invalid")
        _require_text(self.value, "issuer_identifier.value")
        object.__setattr__(
            self,
            "knowledge_at",
            _timestamp_text(
                self.knowledge_at,
                "issuer_identifier.knowledge_at",
            ),
        )
        if not self.source.known_at(self.knowledge_at):
            raise ProductMasterError(
                "issuer_identifier_knowledge_before_source_observed"
            )
        if self.jurisdiction is not None:
            _require_id(self.jurisdiction, "issuer_identifier.jurisdiction")
        if self.provider_id is not None:
            _require_id(self.provider_id, "issuer_identifier.provider_id")
        if self.namespace is IssuerIdentifierNamespace.PROVIDER:
            if self.provider_id is None:
                raise ProductMasterError("issuer_identifier_provider_id_required")
        elif self.provider_id is not None:
            raise ProductMasterError("issuer_identifier_provider_id_not_applicable")
        if self.revision == 1:
            if self.supersedes_hash is not None or self.correction_reason is not None:
                raise ProductMasterError(
                    "issuer_identifier_initial_revision_binding_forbidden"
                )
        else:
            if self.supersedes_hash is None or self.correction_reason is None:
                raise ProductMasterError("issuer_identifier_revision_binding_required")
            _require_hash(
                self.supersedes_hash,
                "issuer_identifier.supersedes_hash",
            )
            _require_text(
                self.correction_reason,
                "issuer_identifier.correction_reason",
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "identifier_id": self.identifier_id,
            "revision": self.revision,
            "issuer_id": self.issuer_id,
            "namespace": self.namespace.value,
            "value": self.value,
            "jurisdiction": self.jurisdiction,
            "provider_id": self.provider_id,
            "validity": self.validity.as_dict(),
            "knowledge_at": self.knowledge_at,
            "source": self.source.as_dict(),
            "supersedes_hash": self.supersedes_hash,
            "correction_reason": self.correction_reason,
        }

    def revision_hash(self) -> str:
        return sha256_prefixed(
            self.as_dict(),
            label="multi_asset_issuer_identifier_revision",
        )


@dataclass(frozen=True, slots=True)
class Instrument:
    instrument_id: str
    kind: InstrumentKind
    name: str
    economic_underlying_id: str
    currency: str
    unit: str
    validity: EffectivePeriod
    source: SourceReference
    issuer_id: str | None = None
    trading_status: InstrumentTradingStatus = InstrumentTradingStatus.ACTIVE
    primary_listing_id: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.instrument_id, "instrument.instrument_id")
        if not isinstance(self.kind, InstrumentKind):
            raise ProductMasterError("instrument.kind_invalid")
        _require_text(self.name, "instrument.name")
        _require_id(
            self.economic_underlying_id,
            "instrument.economic_underlying_id",
        )
        _require_currency(self.currency, "instrument.currency")
        _require_id(self.unit, "instrument.unit")
        if self.issuer_id is not None:
            _require_id(self.issuer_id, "instrument.issuer_id")
        if not isinstance(self.trading_status, InstrumentTradingStatus):
            raise ProductMasterError("instrument.trading_status_invalid")
        if self.primary_listing_id is not None:
            _require_id(
                self.primary_listing_id,
                "instrument.primary_listing_id",
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "kind": self.kind.value,
            "name": self.name,
            "economic_underlying_id": self.economic_underlying_id,
            "issuer_id": self.issuer_id,
            "trading_status": self.trading_status.value,
            "primary_listing_id": self.primary_listing_id,
            "currency": self.currency,
            "unit": self.unit,
            "validity": self.validity.as_dict(),
            "source": self.source.as_dict(),
        }


# Some product-master vocabularies call this entity Security.  Keeping an alias
# makes that spelling available without creating a second identity authority.
Security = Instrument


@dataclass(frozen=True, slots=True)
class Listing:
    listing_id: str
    instrument_id: str
    venue_mic: str
    symbol: str
    trading_currency: str
    price_unit: str
    quantity_unit: str
    calendar_id: str
    validity: EffectivePeriod
    source: SourceReference
    market_segment: str = "PRIMARY"
    session_id: str = "REGULAR"
    lot_size: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        _require_id(self.listing_id, "listing.listing_id")
        _require_id(self.instrument_id, "listing.instrument_id")
        if not re.fullmatch(r"[A-Z0-9]{4}", self.venue_mic):
            raise ProductMasterError("listing.venue_mic_invalid")
        _require_text(self.symbol, "listing.symbol")
        _require_currency(self.trading_currency, "listing.trading_currency")
        _require_id(self.price_unit, "listing.price_unit")
        _require_id(self.quantity_unit, "listing.quantity_unit")
        _require_id(self.calendar_id, "listing.calendar_id")
        _require_id(self.market_segment, "listing.market_segment")
        _require_id(self.session_id, "listing.session_id")
        _positive_decimal(self.lot_size, "listing.lot_size")

    def as_dict(self) -> dict[str, object]:
        return {
            "listing_id": self.listing_id,
            "instrument_id": self.instrument_id,
            "venue_mic": self.venue_mic,
            "symbol": self.symbol,
            "trading_currency": self.trading_currency,
            "price_unit": self.price_unit,
            "quantity_unit": self.quantity_unit,
            "calendar_id": self.calendar_id,
            "market_segment": self.market_segment,
            "session_id": self.session_id,
            "lot_size": _decimal_text(self.lot_size),
            "validity": self.validity.as_dict(),
            "source": self.source.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ContractSpecification:
    contract_specification_id: str
    instrument_id: str
    contract_multiplier: Decimal
    contract_unit: str
    settlement_type: SettlementType
    settlement_currency: str
    expiry_at: str
    validity: EffectivePeriod
    source: SourceReference
    last_trade_at: str | None = None
    exercise_style: str | None = None
    minimum_tick: Decimal | None = None
    tick_value: Decimal | None = None
    trading_currency: str | None = None
    calendar_id: str | None = None
    lifecycle_rule_id: str | None = None

    def __post_init__(self) -> None:
        _require_id(
            self.contract_specification_id,
            "contract_specification.contract_specification_id",
        )
        _require_id(self.instrument_id, "contract_specification.instrument_id")
        _positive_decimal(
            self.contract_multiplier,
            "contract_specification.contract_multiplier",
        )
        _require_id(self.contract_unit, "contract_specification.contract_unit")
        if not isinstance(self.settlement_type, SettlementType):
            raise ProductMasterError("contract_specification.settlement_type_invalid")
        _require_currency(
            self.settlement_currency,
            "contract_specification.settlement_currency",
        )
        expiry = _timestamp_text(self.expiry_at, "contract_specification.expiry_at")
        object.__setattr__(self, "expiry_at", expiry)
        if self.last_trade_at is not None:
            last_trade = _timestamp_text(
                self.last_trade_at,
                "contract_specification.last_trade_at",
            )
            if _timestamp(last_trade, "contract_specification.last_trade_at") > (
                _timestamp(expiry, "contract_specification.expiry_at")
            ):
                raise ProductMasterError(
                    "contract_specification_last_trade_after_expiry"
                )
            object.__setattr__(self, "last_trade_at", last_trade)
        if self.exercise_style is not None and self.exercise_style not in {
            "AMERICAN",
            "BERMUDAN",
            "EUROPEAN",
        }:
            raise ProductMasterError("contract_specification.exercise_style_invalid")
        if (self.minimum_tick is None) != (self.tick_value is None):
            raise ProductMasterError("contract_specification_tick_binding_incomplete")
        if self.minimum_tick is not None:
            _positive_decimal(
                self.minimum_tick,
                "contract_specification.minimum_tick",
            )
            if self.tick_value is None:
                raise ProductMasterError(
                    "contract_specification_tick_binding_incomplete"
                )
            _positive_decimal(
                self.tick_value,
                "contract_specification.tick_value",
            )
        if self.trading_currency is not None:
            _require_currency(
                self.trading_currency,
                "contract_specification.trading_currency",
            )
        if self.calendar_id is not None:
            _require_id(
                self.calendar_id,
                "contract_specification.calendar_id",
            )
        if self.lifecycle_rule_id is not None:
            _require_id(
                self.lifecycle_rule_id,
                "contract_specification.lifecycle_rule_id",
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_specification_id": self.contract_specification_id,
            "instrument_id": self.instrument_id,
            "contract_multiplier": _decimal_text(self.contract_multiplier),
            "contract_unit": self.contract_unit,
            "settlement_type": self.settlement_type.value,
            "settlement_currency": self.settlement_currency,
            "expiry_at": self.expiry_at,
            "last_trade_at": self.last_trade_at,
            "exercise_style": self.exercise_style,
            "minimum_tick": (
                _decimal_text(self.minimum_tick)
                if self.minimum_tick is not None
                else None
            ),
            "tick_value": (
                _decimal_text(self.tick_value) if self.tick_value is not None else None
            ),
            "trading_currency": self.trading_currency,
            "calendar_id": self.calendar_id,
            "lifecycle_rule_id": self.lifecycle_rule_id,
            "validity": self.validity.as_dict(),
            "source": self.source.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class SymbolAlias:
    alias_id: str
    instrument_id: str
    provider_id: str
    symbol: str
    validity: EffectivePeriod
    source: SourceReference
    listing_id: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.alias_id, "symbol_alias.alias_id")
        _require_id(self.instrument_id, "symbol_alias.instrument_id")
        _require_id(self.provider_id, "symbol_alias.provider_id")
        _require_text(self.symbol, "symbol_alias.symbol")
        if self.listing_id is not None:
            _require_id(self.listing_id, "symbol_alias.listing_id")

    def as_dict(self) -> dict[str, object]:
        return {
            "alias_id": self.alias_id,
            "instrument_id": self.instrument_id,
            "listing_id": self.listing_id,
            "provider_id": self.provider_id,
            "symbol": self.symbol,
            "validity": self.validity.as_dict(),
            "source": self.source.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    event_id: str
    instrument_id: str
    event_type: LifecycleEventType
    effective_at: str
    knowledge_at: str
    validity: EffectivePeriod
    source: SourceReference
    replacement_instrument_id: str | None = None
    contract_specification_id: str | None = None
    announced_at: str | None = None
    record_at: str | None = None
    payment_at: str | None = None
    quantity_ratio: Decimal | None = None
    cash_amount: Decimal | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.event_id, "lifecycle_event.event_id")
        _require_id(self.instrument_id, "lifecycle_event.instrument_id")
        if not isinstance(self.event_type, LifecycleEventType):
            raise ProductMasterError("lifecycle_event.event_type_invalid")
        effective = _timestamp_text(self.effective_at, "lifecycle_event.effective_at")
        knowledge = _timestamp_text(self.knowledge_at, "lifecycle_event.knowledge_at")
        object.__setattr__(self, "effective_at", effective)
        object.__setattr__(self, "knowledge_at", knowledge)
        if not self.validity.contains(effective):
            raise ProductMasterError("lifecycle_event_effective_outside_validity")
        if _timestamp(
            knowledge,
            "lifecycle_event.knowledge_at",
        ) < _timestamp(self.source.observed_at, "source.observed_at"):
            raise ProductMasterError("lifecycle_event_knowledge_before_source_observed")
        if self.replacement_instrument_id is not None:
            _require_id(
                self.replacement_instrument_id,
                "lifecycle_event.replacement_instrument_id",
            )
            if self.replacement_instrument_id == self.instrument_id:
                raise ProductMasterError("lifecycle_event_self_replacement")
        if self.contract_specification_id is not None:
            _require_id(
                self.contract_specification_id,
                "lifecycle_event.contract_specification_id",
            )
        for field_name in ("announced_at", "record_at", "payment_at"):
            value = getattr(self, field_name)
            if value is None:
                continue
            canonical = _timestamp_text(
                value,
                f"lifecycle_event.{field_name}",
            )
            if field_name == "announced_at" and _timestamp(
                canonical,
                "lifecycle_event.announced_at",
            ) > _timestamp(effective, "lifecycle_event.effective_at"):
                raise ProductMasterError("lifecycle_event_announced_after_effective")
            if field_name == "announced_at" and _timestamp(
                knowledge,
                "lifecycle_event.knowledge_at",
            ) < _timestamp(
                canonical,
                "lifecycle_event.announced_at",
            ):
                raise ProductMasterError(
                    "lifecycle_event_knowledge_before_announcement"
                )
            object.__setattr__(self, field_name, canonical)
        if (
            self.record_at is not None
            and self.payment_at is not None
            and _timestamp(
                self.record_at,
                "lifecycle_event.record_at",
            )
            > _timestamp(
                self.payment_at,
                "lifecycle_event.payment_at",
            )
        ):
            raise ProductMasterError("lifecycle_event_record_after_payment")
        if self.quantity_ratio is not None:
            _positive_decimal(
                self.quantity_ratio,
                "lifecycle_event.quantity_ratio",
            )
        if (self.cash_amount is None) != (self.currency is None):
            raise ProductMasterError("lifecycle_event_cash_currency_binding_incomplete")
        if self.cash_amount is not None:
            if (
                not isinstance(self.cash_amount, Decimal)
                or not self.cash_amount.is_finite()
                or self.cash_amount < 0
            ):
                raise ProductMasterError(
                    "lifecycle_event.cash_amount_must_be_nonnegative_decimal"
                )
            if self.currency is None:
                raise ProductMasterError(
                    "lifecycle_event_cash_currency_binding_incomplete"
                )
            _require_currency(self.currency, "lifecycle_event.currency")
        if (
            self.event_type is LifecycleEventType.CASH_DISTRIBUTION
            and self.cash_amount is None
        ):
            raise ProductMasterError("cash_distribution_amount_required")
        if (
            self.event_type
            in {
                LifecycleEventType.SPLIT,
                LifecycleEventType.MERGER,
                LifecycleEventType.SPINOFF,
            }
            and self.quantity_ratio is None
        ):
            raise ProductMasterError("lifecycle_event_quantity_ratio_required")

    def known_at(self, as_of: str) -> bool:
        return _timestamp(self.knowledge_at, "lifecycle_event.knowledge_at") <= (
            _timestamp(as_of, "lifecycle_event.as_of")
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "instrument_id": self.instrument_id,
            "event_type": self.event_type.value,
            "effective_at": self.effective_at,
            "knowledge_at": self.knowledge_at,
            "replacement_instrument_id": self.replacement_instrument_id,
            "contract_specification_id": self.contract_specification_id,
            "announced_at": self.announced_at,
            "record_at": self.record_at,
            "payment_at": self.payment_at,
            "quantity_ratio": (
                _decimal_text(self.quantity_ratio)
                if self.quantity_ratio is not None
                else None
            ),
            "cash_amount": (
                _decimal_text(self.cash_amount)
                if self.cash_amount is not None
                else None
            ),
            "currency": self.currency,
            "validity": self.validity.as_dict(),
            "source": self.source.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class InstrumentRelationship:
    relationship_id: str
    source_instrument_id: str
    target_instrument_id: str
    relationship_type: InstrumentRelationshipType
    validity: EffectivePeriod
    source: SourceReference
    quantity_ratio: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        _require_id(self.relationship_id, "relationship.relationship_id")
        _require_id(
            self.source_instrument_id,
            "relationship.source_instrument_id",
        )
        _require_id(
            self.target_instrument_id,
            "relationship.target_instrument_id",
        )
        if self.source_instrument_id == self.target_instrument_id:
            raise ProductMasterError("relationship_self_reference")
        if not isinstance(self.relationship_type, InstrumentRelationshipType):
            raise ProductMasterError("relationship.relationship_type_invalid")
        _positive_decimal(self.quantity_ratio, "relationship.quantity_ratio")

    def as_dict(self) -> dict[str, object]:
        return {
            "relationship_id": self.relationship_id,
            "source_instrument_id": self.source_instrument_id,
            "target_instrument_id": self.target_instrument_id,
            "relationship_type": self.relationship_type.value,
            "quantity_ratio": _decimal_text(self.quantity_ratio),
            "validity": self.validity.as_dict(),
            "source": self.source.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class DeliverableComponent:
    """One security or cash component in a versioned complex deliverable."""

    component_id: str
    target_instrument_id: str | None
    quantity: Decimal
    cash_amount: Decimal = Decimal("0")
    cash_currency: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.component_id, "deliverable_component.component_id")
        if self.target_instrument_id is not None:
            _require_id(
                self.target_instrument_id,
                "deliverable_component.target_instrument_id",
            )
        if (
            not isinstance(self.quantity, Decimal)
            or not self.quantity.is_finite()
            or self.quantity < 0
        ):
            raise ProductMasterError("deliverable_component.quantity_invalid")
        if (
            not isinstance(self.cash_amount, Decimal)
            or not self.cash_amount.is_finite()
            or self.cash_amount < 0
        ):
            raise ProductMasterError("deliverable_component.cash_amount_invalid")
        if (self.cash_amount > 0) != (self.cash_currency is not None):
            raise ProductMasterError(
                "deliverable_component_cash_currency_binding_invalid"
            )
        if self.cash_currency is not None:
            _require_currency(
                self.cash_currency,
                "deliverable_component.cash_currency",
            )
        if self.target_instrument_id is None and self.cash_amount == 0:
            raise ProductMasterError("deliverable_component_economic_value_required")
        if self.target_instrument_id is not None and self.quantity == 0:
            raise ProductMasterError(
                "deliverable_component_instrument_quantity_required"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "component_id": self.component_id,
            "target_instrument_id": self.target_instrument_id,
            "quantity": _decimal_text(self.quantity),
            "cash_amount": _decimal_text(self.cash_amount),
            "cash_currency": self.cash_currency,
        }


@dataclass(frozen=True, slots=True)
class CompositeDeliverable:
    """Revisioned option/future deliverable retaining all economic legs."""

    deliverable_id: str
    source_instrument_id: str
    components: tuple[DeliverableComponent, ...]
    validity: EffectivePeriod
    knowledge_at: str
    source: SourceReference

    def __post_init__(self) -> None:
        _require_id(self.deliverable_id, "composite_deliverable.deliverable_id")
        _require_id(
            self.source_instrument_id,
            "composite_deliverable.source_instrument_id",
        )
        components = tuple(self.components)
        if not components:
            raise ProductMasterError("composite_deliverable.components_required")
        identifiers = [item.component_id for item in components]
        if identifiers != sorted(identifiers) or len(identifiers) != len(
            set(identifiers)
        ):
            raise ProductMasterError(
                "composite_deliverable_components_not_unique_canonical"
            )
        object.__setattr__(self, "components", components)
        object.__setattr__(
            self,
            "knowledge_at",
            _timestamp_text(
                self.knowledge_at,
                "composite_deliverable.knowledge_at",
            ),
        )
        if not self.source.known_at(self.knowledge_at):
            raise ProductMasterError("composite_deliverable_knowledge_before_source")

    def as_dict(self) -> dict[str, object]:
        return {
            "deliverable_id": self.deliverable_id,
            "source_instrument_id": self.source_instrument_id,
            "components": [item.as_dict() for item in self.components],
            "validity": self.validity.as_dict(),
            "knowledge_at": self.knowledge_at,
            "source": self.source.as_dict(),
        }

    def deliverable_hash(self) -> str:
        return sha256_prefixed(
            self.as_dict(),
            label="multi_asset_composite_deliverable",
        )


_T = TypeVar("_T")


def _grouped(items: Iterable[_T], key: Callable[[_T], str]) -> dict[str, list[_T]]:
    result: dict[str, list[_T]] = {}
    for item in items:
        result.setdefault(key(item), []).append(item)
    return result


def _validate_non_overlapping_versions(
    items: Iterable[_T],
    *,
    key: Callable[[_T], str],
    period: Callable[[_T], EffectivePeriod],
    label: str,
) -> None:
    for stable_id, versions in _grouped(items, key).items():
        ordered = sorted(versions, key=lambda item: period(item).start)
        for left, right in zip(ordered, ordered[1:]):
            if period(left).overlaps(period(right)):
                raise ProductMasterError(f"{label}_validity_overlap:{stable_id}")


def _covering(
    period: EffectivePeriod,
    items: Iterable[_T],
    item_period: Callable[[_T], EffectivePeriod],
) -> bool:
    return any(item_period(item).covers(period) for item in items)


@dataclass(frozen=True, slots=True)
class InstrumentRegistry:
    """Immutable product-master snapshot with fail-closed referential checks."""

    economic_underlyings: tuple[EconomicUnderlying, ...] = ()
    issuers: tuple[Issuer, ...] = ()
    issuer_identifier_revisions: tuple[IssuerIdentifierRevision, ...] = ()
    instruments: tuple[Instrument, ...] = ()
    listings: tuple[Listing, ...] = ()
    contract_specifications: tuple[ContractSpecification, ...] = ()
    symbol_aliases: tuple[SymbolAlias, ...] = ()
    lifecycle_events: tuple[LifecycleEvent, ...] = ()
    relationships: tuple[InstrumentRelationship, ...] = ()
    composite_deliverables: tuple[CompositeDeliverable, ...] = ()
    schema_version: int = MULTI_ASSET_DOMAIN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MULTI_ASSET_DOMAIN_SCHEMA_VERSION:
            raise ProductMasterError("product_master_schema_unsupported")
        for field in (
            "economic_underlyings",
            "issuers",
            "issuer_identifier_revisions",
            "instruments",
            "listings",
            "contract_specifications",
            "symbol_aliases",
            "lifecycle_events",
            "relationships",
            "composite_deliverables",
        ):
            object.__setattr__(self, field, tuple(getattr(self, field)))
        self._validate_version_ranges()
        self._validate_references()
        self._validate_relationship_types()
        self._validate_derivative_completeness()
        self._validate_issuer_identifiers()
        self._validate_symbol_aliases()

    def _validate_version_ranges(self) -> None:
        definitions: tuple[
            tuple[Iterable[object], Callable[[object], str], str], ...
        ] = (
            (
                self.economic_underlyings,
                lambda item: item.underlying_id,  # type: ignore[attr-defined]
                "underlying",
            ),
            (
                self.issuers,
                lambda item: item.issuer_id,  # type: ignore[attr-defined]
                "issuer",
            ),
            (
                self.instruments,
                lambda item: item.instrument_id,  # type: ignore[attr-defined]
                "instrument",
            ),
            (
                self.listings,
                lambda item: item.listing_id,  # type: ignore[attr-defined]
                "listing",
            ),
            (
                self.contract_specifications,
                lambda item: item.contract_specification_id,  # type: ignore[attr-defined]
                "contract_specification",
            ),
            (
                self.symbol_aliases,
                lambda item: item.alias_id,  # type: ignore[attr-defined]
                "symbol_alias",
            ),
            (
                self.relationships,
                lambda item: item.relationship_id,  # type: ignore[attr-defined]
                "relationship",
            ),
            (
                self.composite_deliverables,
                lambda item: item.deliverable_id,  # type: ignore[attr-defined]
                "composite_deliverable",
            ),
        )
        for items, key, label in definitions:
            _validate_non_overlapping_versions(
                items,
                key=key,
                period=lambda item: item.validity,  # type: ignore[attr-defined]
                label=label,
            )
        event_ids: set[str] = set()
        for event in self.lifecycle_events:
            if event.event_id in event_ids:
                raise ProductMasterError(
                    f"lifecycle_event_id_duplicate:{event.event_id}"
                )
            event_ids.add(event.event_id)

    def _validate_references(self) -> None:
        underlyings = _grouped(
            self.economic_underlyings, lambda item: item.underlying_id
        )
        issuers = _grouped(self.issuers, lambda item: item.issuer_id)
        instruments = _grouped(self.instruments, lambda item: item.instrument_id)
        listings = _grouped(self.listings, lambda item: item.listing_id)
        specifications = _grouped(
            self.contract_specifications,
            lambda item: item.contract_specification_id,
        )

        for identifier in self.issuer_identifier_revisions:
            if not _covering(
                identifier.validity,
                issuers.get(identifier.issuer_id, ()),
                lambda item: item.validity,
            ):
                raise ProductMasterError(
                    "issuer_identifier_issuer_reference_invalid:"
                    f"{identifier.identifier_id}"
                )

        for instrument in self.instruments:
            candidates = underlyings.get(instrument.economic_underlying_id, ())
            if not _covering(
                instrument.validity, candidates, lambda item: item.validity
            ):
                raise ProductMasterError(
                    f"instrument_underlying_reference_invalid:{instrument.instrument_id}"
                )
            if instrument.issuer_id is not None and not _covering(
                instrument.validity,
                issuers.get(instrument.issuer_id, ()),
                lambda item: item.validity,
            ):
                raise ProductMasterError(
                    f"instrument_issuer_reference_invalid:{instrument.instrument_id}"
                )
        for listing in self.listings:
            if not _covering(
                listing.validity,
                instruments.get(listing.instrument_id, ()),
                lambda item: item.validity,
            ):
                raise ProductMasterError(
                    f"listing_instrument_reference_invalid:{listing.listing_id}"
                )
            listing_instrument = next(
                item
                for item in instruments[listing.instrument_id]
                if item.validity.covers(listing.validity)
            )
            if listing.trading_currency != listing_instrument.currency:
                raise ProductMasterError(
                    f"listing_trading_currency_mismatch:{listing.listing_id}"
                )
        for instrument in self.instruments:
            if instrument.primary_listing_id is None:
                continue
            primary_candidates = listings.get(
                instrument.primary_listing_id,
                (),
            )
            if (
                len(primary_candidates) != 1
                or primary_candidates[0].instrument_id != instrument.instrument_id
                or not primary_candidates[0].validity.covers(instrument.validity)
            ):
                raise ProductMasterError(
                    "instrument_primary_listing_reference_invalid:"
                    f"{instrument.instrument_id}"
                )

        for specification in self.contract_specifications:
            if not _covering(
                specification.validity,
                instruments.get(specification.instrument_id, ()),
                lambda item: item.validity,
            ):
                raise ProductMasterError(
                    "contract_specification_instrument_reference_invalid:"
                    f"{specification.contract_specification_id}"
                )
            specification_instrument = next(
                item
                for item in instruments[specification.instrument_id]
                if item.validity.covers(specification.validity)
            )
            if (
                specification.trading_currency is not None
                and specification.trading_currency != specification_instrument.currency
            ):
                raise ProductMasterError(
                    "contract_specification_trading_currency_mismatch:"
                    f"{specification.contract_specification_id}"
                )
            if (
                specification.minimum_tick is not None
                and specification.tick_value
                != specification.minimum_tick * specification.contract_multiplier
            ):
                raise ProductMasterError(
                    "contract_specification_tick_value_mismatch:"
                    f"{specification.contract_specification_id}"
                )

        for alias in self.symbol_aliases:
            if not _covering(
                alias.validity,
                instruments.get(alias.instrument_id, ()),
                lambda item: item.validity,
            ):
                raise ProductMasterError(
                    f"symbol_alias_instrument_reference_invalid:{alias.alias_id}"
                )
            if alias.listing_id is not None:
                listing_versions = listings.get(alias.listing_id, ())
                if not _covering(
                    alias.validity,
                    listing_versions,
                    lambda item: item.validity,
                ) or any(
                    item.instrument_id != alias.instrument_id
                    for item in listing_versions
                    if item.validity.overlaps(alias.validity)
                ):
                    raise ProductMasterError(
                        f"symbol_alias_listing_reference_invalid:{alias.alias_id}"
                    )

        for event in self.lifecycle_events:
            active = self._instrument_effective_as_of(
                event.instrument_id,
                event.effective_at,
            )
            if active is None:
                raise ProductMasterError(
                    f"lifecycle_event_instrument_reference_invalid:{event.event_id}"
                )
            if (
                event.replacement_instrument_id is not None
                and event.replacement_instrument_id not in instruments
            ):
                raise ProductMasterError(
                    f"lifecycle_event_replacement_reference_invalid:{event.event_id}"
                )
            if (
                event.contract_specification_id is not None
                and event.contract_specification_id not in specifications
            ):
                raise ProductMasterError(
                    f"lifecycle_event_contract_reference_invalid:{event.event_id}"
                )

        for relationship in self.relationships:
            for endpoint, instrument_id in (
                ("source", relationship.source_instrument_id),
                ("target", relationship.target_instrument_id),
            ):
                if not _covering(
                    relationship.validity,
                    instruments.get(instrument_id, ()),
                    lambda item: item.validity,
                ):
                    raise ProductMasterError(
                        f"relationship_{endpoint}_reference_invalid:"
                        f"{relationship.relationship_id}"
                    )

        for deliverable in self.composite_deliverables:
            if not _covering(
                deliverable.validity,
                instruments.get(deliverable.source_instrument_id, ()),
                lambda item: item.validity,
            ):
                raise ProductMasterError(
                    "composite_deliverable_source_reference_invalid:"
                    f"{deliverable.deliverable_id}"
                )
            for component in deliverable.components:
                if component.target_instrument_id is None:
                    continue
                if not _covering(
                    deliverable.validity,
                    instruments.get(component.target_instrument_id, ()),
                    lambda item: item.validity,
                ):
                    raise ProductMasterError(
                        "composite_deliverable_component_reference_invalid:"
                        f"{deliverable.deliverable_id}:{component.component_id}"
                    )

    def _validate_relationship_types(self) -> None:
        for relationship in self.relationships:
            source = self._instrument_effective_as_of(
                relationship.source_instrument_id,
                relationship.validity.valid_from,
            )
            target = self._instrument_effective_as_of(
                relationship.target_instrument_id,
                relationship.validity.valid_from,
            )
            if source is None or target is None:
                raise ProductMasterError(
                    f"relationship_endpoint_not_active:{relationship.relationship_id}"
                )
            relation = relationship.relationship_type
            allowed = True
            if relation is InstrumentRelationshipType.FUTURE_UNDERLYING:
                allowed = source.kind is InstrumentKind.FUTURE and target.kind in {
                    InstrumentKind.SPOT,
                    InstrumentKind.EQUITY,
                    InstrumentKind.ETF,
                    InstrumentKind.INDEX,
                    InstrumentKind.RATE,
                    InstrumentKind.FX,
                    InstrumentKind.COMMODITY,
                }
            elif relation is InstrumentRelationshipType.OPTION_UNDERLYING:
                allowed = source.kind is InstrumentKind.OPTION and target.kind in {
                    InstrumentKind.SPOT,
                    InstrumentKind.EQUITY,
                    InstrumentKind.ETF,
                    InstrumentKind.INDEX,
                    InstrumentKind.FUTURE,
                    InstrumentKind.RATE,
                    InstrumentKind.FX,
                    InstrumentKind.COMMODITY,
                }
            elif relation is InstrumentRelationshipType.OPTION_DELIVERABLE:
                allowed = source.kind is InstrumentKind.OPTION and target.kind in {
                    InstrumentKind.SPOT,
                    InstrumentKind.EQUITY,
                    InstrumentKind.ETF,
                    InstrumentKind.FUTURE,
                    InstrumentKind.FX,
                    InstrumentKind.COMMODITY,
                }
            elif relation is (InstrumentRelationshipType.FUTURE_OPTION_DELIVERABLE):
                allowed = (
                    source.kind is InstrumentKind.OPTION
                    and target.kind is InstrumentKind.FUTURE
                )
            elif relation is InstrumentRelationshipType.TRACKS:
                allowed = source.kind is InstrumentKind.ETF and target.kind in {
                    InstrumentKind.INDEX,
                    InstrumentKind.COMMODITY,
                }
            if not allowed:
                raise ProductMasterError(
                    f"relationship_endpoint_kind_invalid:{relationship.relationship_id}"
                )

    def _validate_derivative_completeness(self) -> None:
        specs_by_instrument = _grouped(
            self.contract_specifications, lambda item: item.instrument_id
        )
        relations_by_source = _grouped(
            self.relationships, lambda item: item.source_instrument_id
        )
        for instrument in self.instruments:
            specs = [
                item
                for item in specs_by_instrument.get(instrument.instrument_id, ())
                if item.validity.overlaps(instrument.validity)
            ]
            if instrument.kind in {InstrumentKind.FUTURE, InstrumentKind.OPTION}:
                if len(specs) != 1:
                    raise ProductMasterError(
                        f"derivative_contract_specification_required:"
                        f"{instrument.instrument_id}"
                    )
                specification = specs[0]
                required_economic_terms = (
                    specification.minimum_tick,
                    specification.tick_value,
                    specification.trading_currency,
                    specification.calendar_id,
                    specification.lifecycle_rule_id,
                )
                if any(item is None for item in required_economic_terms):
                    raise ProductMasterError(
                        "derivative_contract_specification_economic_terms_required:"
                        f"{instrument.instrument_id}"
                    )
                if (
                    instrument.kind is InstrumentKind.OPTION
                    and specification.exercise_style is None
                ):
                    raise ProductMasterError(
                        "option_contract_specification_exercise_style_required:"
                        f"{instrument.instrument_id}"
                    )
                if (
                    instrument.kind is InstrumentKind.FUTURE
                    and specification.exercise_style is not None
                ):
                    raise ProductMasterError(
                        "future_contract_specification_exercise_style_forbidden:"
                        f"{instrument.instrument_id}"
                    )
                required_underlying_type = (
                    InstrumentRelationshipType.FUTURE_UNDERLYING
                    if instrument.kind is InstrumentKind.FUTURE
                    else InstrumentRelationshipType.OPTION_UNDERLYING
                )
                underlying_relations = [
                    item
                    for item in relations_by_source.get(
                        instrument.instrument_id,
                        (),
                    )
                    if item.relationship_type is required_underlying_type
                    and item.validity.overlaps(specification.validity)
                ]
                if len(underlying_relations) != 1:
                    raise ProductMasterError(
                        "derivative_underlying_relationship_required:"
                        f"{instrument.instrument_id}"
                    )
            elif specs:
                raise ProductMasterError(
                    f"non_derivative_contract_specification_forbidden:"
                    f"{instrument.instrument_id}"
                )
            if instrument.kind is not InstrumentKind.OPTION or not specs:
                continue
            specification = specs[0]
            deliverables = [
                item
                for item in relations_by_source.get(instrument.instrument_id, ())
                if item.relationship_type
                in {
                    InstrumentRelationshipType.OPTION_DELIVERABLE,
                    InstrumentRelationshipType.FUTURE_OPTION_DELIVERABLE,
                }
                and item.validity.overlaps(specification.validity)
            ]
            composite_deliverables = [
                item
                for item in self.composite_deliverables
                if item.source_instrument_id == instrument.instrument_id
                and item.validity.overlaps(specification.validity)
            ]
            if specification.settlement_type is SettlementType.CASH and (
                deliverables or composite_deliverables
            ):
                raise ProductMasterError(
                    f"cash_option_deliverable_forbidden:{instrument.instrument_id}"
                )
            if (
                specification.settlement_type is SettlementType.PHYSICAL
                and len(deliverables) + len(composite_deliverables) != 1
            ):
                raise ProductMasterError(
                    f"physical_option_deliverable_required:{instrument.instrument_id}"
                )

    def _validate_issuer_identifiers(self) -> None:
        histories = _grouped(
            self.issuer_identifier_revisions,
            lambda item: item.identifier_id,
        )
        for identifier_id, revisions in histories.items():
            ordered = sorted(revisions, key=lambda item: item.revision)
            if [item.revision for item in ordered] != list(range(1, len(ordered) + 1)):
                raise ProductMasterError(
                    f"issuer_identifier_revision_not_contiguous:{identifier_id}"
                )
            previous: IssuerIdentifierRevision | None = None
            for revision in ordered:
                if previous is not None:
                    if revision.issuer_id != previous.issuer_id:
                        raise ProductMasterError(
                            "issuer_identifier_correction_changed_issuer:"
                            f"{identifier_id}"
                        )
                    if _timestamp(
                        revision.knowledge_at,
                        "issuer_identifier.knowledge_at",
                    ) <= _timestamp(
                        previous.knowledge_at,
                        "issuer_identifier.previous_knowledge_at",
                    ):
                        raise ProductMasterError(
                            "issuer_identifier_knowledge_not_increasing:"
                            f"{identifier_id}"
                        )
                    if revision.supersedes_hash != previous.revision_hash():
                        raise ProductMasterError(
                            f"issuer_identifier_revision_chain_broken:{identifier_id}"
                        )
                previous = revision

        all_revisions = tuple(self.issuer_identifier_revisions)
        for index, left in enumerate(all_revisions):
            for right in all_revisions[index + 1 :]:
                if left.identifier_id == right.identifier_id:
                    continue
                if (
                    left.namespace is not right.namespace
                    or left.value != right.value
                    or left.jurisdiction != right.jurisdiction
                    or left.provider_id != right.provider_id
                    or not left.validity.overlaps(right.validity)
                ):
                    continue
                raise ProductMasterError(
                    "issuer_identifier_namespace_collision:"
                    f"{left.namespace.value}:{left.value}"
                )

    def _validate_symbol_aliases(self) -> None:
        aliases = _grouped(
            self.symbol_aliases,
            lambda item: f"{item.provider_id}\x00{item.symbol}",
        )
        listings = {item.listing_id: item for item in self.listings}
        for provider_symbol, versions in aliases.items():
            ordered = sorted(versions, key=lambda item: item.validity.start)
            for index, left in enumerate(ordered):
                for right in ordered[index + 1 :]:
                    if not left.validity.overlaps(right.validity):
                        continue
                    left_listing = (
                        listings.get(left.listing_id)
                        if left.listing_id is not None
                        else None
                    )
                    right_listing = (
                        listings.get(right.listing_id)
                        if right.listing_id is not None
                        else None
                    )
                    different_venues = (
                        left_listing is not None
                        and right_listing is not None
                        and left_listing.venue_mic != right_listing.venue_mic
                    )
                    if different_venues:
                        continue
                    provider, symbol = provider_symbol.split("\x00", 1)
                    raise ProductMasterError(
                        f"symbol_alias_ambiguous:{provider}:{symbol}"
                    )

    @staticmethod
    def _knowledge_cutoff(as_of: str, knowledge_at: str | None, field: str) -> str:
        effective = _timestamp_text(as_of, f"{field}.as_of")
        return _timestamp_text(
            effective if knowledge_at is None else knowledge_at,
            f"{field}.knowledge_at",
        )

    def _instrument_effective_as_of(
        self,
        instrument_id: str,
        as_of: str,
    ) -> Instrument | None:
        _require_id(instrument_id, "instrument_lookup.instrument_id")
        matches = [
            item
            for item in self.instruments
            if item.instrument_id == instrument_id and item.validity.contains(as_of)
        ]
        if len(matches) > 1:
            raise ProductMasterError(
                f"instrument_identity_ambiguous_as_of:{instrument_id}"
            )
        return matches[0] if matches else None

    def instrument_as_of(
        self,
        instrument_id: str,
        as_of: str,
        *,
        knowledge_at: str | None = None,
    ) -> Instrument | None:
        """Resolve one instrument by effective and source-knowledge time.

        Omitting ``knowledge_at`` safely aligns it with ``as_of`` for existing
        callers.  Supplying both clocks supports as-known historical replay
        without exposing a retroactive record before its source was observed.
        """

        cutoff = self._knowledge_cutoff(
            as_of,
            knowledge_at,
            "instrument_lookup",
        )
        instrument = self._instrument_effective_as_of(instrument_id, as_of)
        if instrument is None or not instrument.source.known_at(cutoff):
            return None
        return instrument

    def issuer_as_of(
        self,
        issuer_id: str,
        as_of: str,
        *,
        knowledge_at: str | None = None,
    ) -> Issuer | None:
        """Resolve the effective issuer without exposing future corrections."""

        _require_id(issuer_id, "issuer_lookup.issuer_id")
        cutoff = self._knowledge_cutoff(as_of, knowledge_at, "issuer_lookup")
        matches = [
            item
            for item in self.issuers
            if item.issuer_id == issuer_id
            and item.validity.contains(as_of)
            and item.source.known_at(cutoff)
        ]
        if len(matches) > 1:
            raise ProductMasterError(f"issuer_ambiguous_as_of:{issuer_id}")
        return matches[0] if matches else None

    def listing_as_of(
        self,
        listing_id: str,
        as_of: str,
        *,
        knowledge_at: str | None = None,
    ) -> Listing | None:
        """Resolve one venue listing on both product-master clocks."""

        _require_id(listing_id, "listing_lookup.listing_id")
        cutoff = self._knowledge_cutoff(as_of, knowledge_at, "listing_lookup")
        matches = [
            item
            for item in self.listings
            if item.listing_id == listing_id
            and item.validity.contains(as_of)
            and item.source.known_at(cutoff)
        ]
        if len(matches) > 1:
            raise ProductMasterError(f"listing_ambiguous_as_of:{listing_id}")
        return matches[0] if matches else None

    def resolve_issuer_identifier(
        self,
        *,
        namespace: IssuerIdentifierNamespace,
        value: str,
        as_of: str,
        knowledge_at: str | None = None,
        jurisdiction: str | None = None,
        provider_id: str | None = None,
    ) -> Issuer:
        """Resolve the last identifier revision known by the query cutoff."""

        if not isinstance(namespace, IssuerIdentifierNamespace):
            raise ProductMasterError("issuer_identifier_lookup.namespace_invalid")
        _require_text(value, "issuer_identifier_lookup.value")
        if jurisdiction is not None:
            _require_id(
                jurisdiction,
                "issuer_identifier_lookup.jurisdiction",
            )
        if provider_id is not None:
            _require_id(provider_id, "issuer_identifier_lookup.provider_id")
        cutoff = self._knowledge_cutoff(
            as_of,
            knowledge_at,
            "issuer_identifier_lookup",
        )
        latest: dict[str, IssuerIdentifierRevision] = {}
        for revision in self.issuer_identifier_revisions:
            if (
                not revision.validity.contains(as_of)
                or _timestamp(
                    revision.knowledge_at,
                    "issuer_identifier.knowledge_at",
                )
                > _timestamp(cutoff, "issuer_identifier_lookup.knowledge_at")
                or not revision.source.known_at(cutoff)
            ):
                continue
            prior = latest.get(revision.identifier_id)
            if prior is None or revision.revision > prior.revision:
                latest[revision.identifier_id] = revision
        issuer_ids = {
            item.issuer_id
            for item in latest.values()
            if item.namespace is namespace
            and item.value == value
            and item.jurisdiction == jurisdiction
            and item.provider_id == provider_id
        }
        if len(issuer_ids) != 1:
            raise ProductMasterError(
                f"issuer_identifier_not_unique_as_of:{namespace.value}:{value}"
            )
        issuer_id = next(iter(issuer_ids))
        issuer = self.issuer_as_of(
            issuer_id,
            as_of,
            knowledge_at=cutoff,
        )
        if issuer is None:
            raise ProductMasterError("issuer_identifier_issuer_not_active_or_known")
        return issuer

    def tradable_instrument_as_of(
        self,
        instrument_id: str,
        as_of: str,
        *,
        knowledge_at: str | None = None,
    ) -> Instrument | None:
        """Resolve only an instrument that was tradable as known at the cutoff."""

        cutoff = self._knowledge_cutoff(
            as_of,
            knowledge_at,
            "tradable_instrument_lookup",
        )
        instrument = self.instrument_as_of(
            instrument_id,
            as_of,
            knowledge_at=cutoff,
        )
        if (
            instrument is None
            or instrument.trading_status is not InstrumentTradingStatus.ACTIVE
        ):
            return None
        active_listings = tuple(
            listing
            for listing in self.listings
            if listing.instrument_id == instrument_id
            and listing.validity.contains(as_of)
            and listing.source.known_at(cutoff)
            and (
                instrument.primary_listing_id is None
                or listing.listing_id == instrument.primary_listing_id
            )
        )
        if not active_listings:
            return None
        terminal_events = {
            LifecycleEventType.DELISTING,
            LifecycleEventType.EXPIRY,
        }
        if any(
            event.instrument_id == instrument_id
            and event.event_type in terminal_events
            and event.known_at(cutoff)
            and event.source.known_at(cutoff)
            and _timestamp(
                event.effective_at,
                "lifecycle_event.effective_at",
            )
            <= _timestamp(as_of, "tradable_instrument_lookup.as_of")
            for event in self.lifecycle_events
        ):
            return None
        return instrument

    def resolve_symbol(
        self,
        *,
        provider_id: str,
        symbol: str,
        as_of: str,
        knowledge_at: str | None = None,
        venue_mic: str | None = None,
        listing_id: str | None = None,
    ) -> Instrument:
        cutoff = self._knowledge_cutoff(as_of, knowledge_at, "symbol_lookup")
        if venue_mic is not None and not re.fullmatch(r"[A-Z0-9]{4}", venue_mic):
            raise ProductMasterError("symbol_lookup.venue_mic_invalid")
        if listing_id is not None:
            _require_id(listing_id, "symbol_lookup.listing_id")
        listings = {
            item.listing_id: item
            for item in self.listings
            if item.validity.contains(as_of) and item.source.known_at(cutoff)
        }
        matches = [
            item
            for item in self.symbol_aliases
            if item.provider_id == provider_id
            and item.symbol == symbol
            and item.validity.contains(as_of)
            and item.source.known_at(cutoff)
            and (listing_id is None or item.listing_id == listing_id)
            and (
                venue_mic is None
                or (
                    item.listing_id is not None
                    and item.listing_id in listings
                    and listings[item.listing_id].venue_mic == venue_mic
                )
            )
        ]
        if len(matches) != 1:
            raise ProductMasterError(
                f"symbol_alias_not_unique_as_of:{provider_id}:{symbol}"
            )
        instrument = self.instrument_as_of(
            matches[0].instrument_id,
            as_of,
            knowledge_at=cutoff,
        )
        if instrument is None:
            raise ProductMasterError("symbol_alias_instrument_not_active_or_known")
        return instrument

    def composite_deliverable_as_of(
        self,
        source_instrument_id: str,
        as_of: str,
        *,
        knowledge_at: str | None = None,
    ) -> CompositeDeliverable | None:
        cutoff = self._knowledge_cutoff(
            as_of,
            knowledge_at,
            "composite_deliverable_lookup",
        )
        matches = [
            item
            for item in self.composite_deliverables
            if item.source_instrument_id == source_instrument_id
            and item.validity.contains(as_of)
            and _timestamp(
                item.knowledge_at,
                "composite_deliverable.knowledge_at",
            )
            <= _timestamp(cutoff, "composite_deliverable_lookup.knowledge_at")
            and item.source.known_at(cutoff)
        ]
        if len(matches) > 1:
            raise ProductMasterError(
                f"composite_deliverable_ambiguous:{source_instrument_id}"
            )
        return matches[0] if matches else None

    def relationship_targets(
        self,
        *,
        source_instrument_id: str,
        relationship_type: InstrumentRelationshipType,
        as_of: str,
        knowledge_at: str | None = None,
    ) -> tuple[Instrument, ...]:
        cutoff = self._knowledge_cutoff(as_of, knowledge_at, "relationship_lookup")
        source = self.instrument_as_of(
            source_instrument_id,
            as_of,
            knowledge_at=cutoff,
        )
        if source is None:
            raise ProductMasterError("relationship_source_not_active_or_known")
        edges = [
            item
            for item in self.relationships
            if item.source_instrument_id == source_instrument_id
            and item.relationship_type is relationship_type
            and item.validity.contains(as_of)
            and item.source.known_at(cutoff)
        ]
        targets: list[Instrument] = []
        for edge in edges:
            target = self.instrument_as_of(
                edge.target_instrument_id,
                as_of,
                knowledge_at=cutoff,
            )
            if target is None:
                raise ProductMasterError("relationship_target_not_active_or_known")
            targets.append(target)
        return tuple(sorted(targets, key=lambda item: item.instrument_id))

    def contract_specification_as_of(
        self,
        instrument_id: str,
        as_of: str,
        *,
        knowledge_at: str | None = None,
    ) -> ContractSpecification | None:
        """Resolve a contract specification on both product-master clocks."""

        _require_id(instrument_id, "contract_lookup.instrument_id")
        cutoff = self._knowledge_cutoff(as_of, knowledge_at, "contract_lookup")
        matches = [
            item
            for item in self.contract_specifications
            if item.instrument_id == instrument_id
            and item.validity.contains(as_of)
            and item.source.known_at(cutoff)
        ]
        if len(matches) > 1:
            raise ProductMasterError(
                f"contract_specification_ambiguous_as_of:{instrument_id}"
            )
        return matches[0] if matches else None

    def lifecycle_events_known_at(self, as_of: str) -> tuple[LifecycleEvent, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.lifecycle_events
                    if item.known_at(as_of) and item.source.known_at(as_of)
                ),
                key=lambda item: (item.effective_at, item.event_id),
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "economic_underlyings": [
                item.as_dict()
                for item in sorted(
                    self.economic_underlyings,
                    key=lambda item: (item.underlying_id, item.validity.valid_from),
                )
            ],
            "issuers": [
                item.as_dict()
                for item in sorted(
                    self.issuers,
                    key=lambda item: (item.issuer_id, item.validity.valid_from),
                )
            ],
            "issuer_identifier_revisions": [
                item.as_dict()
                for item in sorted(
                    self.issuer_identifier_revisions,
                    key=lambda item: (item.identifier_id, item.revision),
                )
            ],
            "instruments": [
                item.as_dict()
                for item in sorted(
                    self.instruments,
                    key=lambda item: (item.instrument_id, item.validity.valid_from),
                )
            ],
            "listings": [
                item.as_dict()
                for item in sorted(
                    self.listings,
                    key=lambda item: (item.listing_id, item.validity.valid_from),
                )
            ],
            "contract_specifications": [
                item.as_dict()
                for item in sorted(
                    self.contract_specifications,
                    key=lambda item: (
                        item.contract_specification_id,
                        item.validity.valid_from,
                    ),
                )
            ],
            "symbol_aliases": [
                item.as_dict()
                for item in sorted(
                    self.symbol_aliases,
                    key=lambda item: (item.alias_id, item.validity.valid_from),
                )
            ],
            "lifecycle_events": [
                item.as_dict()
                for item in sorted(
                    self.lifecycle_events, key=lambda item: item.event_id
                )
            ],
            "relationships": [
                item.as_dict()
                for item in sorted(
                    self.relationships,
                    key=lambda item: (
                        item.relationship_id,
                        item.validity.valid_from,
                    ),
                )
            ],
            "composite_deliverables": [
                item.as_dict()
                for item in sorted(
                    self.composite_deliverables,
                    key=lambda item: (
                        item.deliverable_id,
                        item.validity.valid_from,
                    ),
                )
            ],
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="multi_asset_product_master")


@dataclass(frozen=True, slots=True)
class ProductMasterRevision:
    """One immutable product-master snapshot in an append-only revision chain.

    ``recorded_at`` is the transaction-time clock for the snapshot itself.
    Effective-time selection remains the responsibility of the contained
    :class:`InstrumentRegistry`, whose entities retain their own validity
    periods and source-observation clocks.
    """

    revision_id: str
    recorded_at: str
    registry: InstrumentRegistry
    supersedes_hash: str | None = None
    revision_reason: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.revision_id, "product_master_revision.revision_id")
        object.__setattr__(
            self,
            "recorded_at",
            _timestamp_text(
                self.recorded_at,
                "product_master_revision.recorded_at",
            ),
        )
        if not isinstance(self.registry, InstrumentRegistry):
            raise ProductMasterError("product_master_revision.registry_invalid")
        if self.supersedes_hash is not None:
            _require_hash(
                self.supersedes_hash,
                "product_master_revision.supersedes_hash",
            )
            if self.revision_reason is None:
                raise ProductMasterError(
                    "product_master_revision.revision_reason_required"
                )
        elif self.revision_reason is not None:
            raise ProductMasterError("product_master_revision.initial_reason_forbidden")
        if self.revision_reason is not None:
            _require_text(
                self.revision_reason,
                "product_master_revision.revision_reason",
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "revision_id": self.revision_id,
            "recorded_at": self.recorded_at,
            "registry_hash": self.registry.contract_hash(),
            "supersedes_hash": self.supersedes_hash,
            "revision_reason": self.revision_reason,
        }

    def revision_hash(self) -> str:
        return sha256_prefixed(
            self.as_dict(),
            label="multi_asset_product_master_revision",
        )


@dataclass(frozen=True, slots=True)
class ProductMasterHistory:
    """Bitemporal product-master authority retaining pre-correction values."""

    revisions: tuple[ProductMasterRevision, ...]

    def __post_init__(self) -> None:
        revisions = tuple(self.revisions)
        if not revisions:
            raise ProductMasterError("product_master_history_empty")
        object.__setattr__(self, "revisions", revisions)
        revision_ids: set[str] = set()
        revision_hashes: set[str] = set()
        previous: ProductMasterRevision | None = None
        for revision in revisions:
            if revision.revision_id in revision_ids:
                raise ProductMasterError(
                    f"product_master_revision_id_duplicate:{revision.revision_id}"
                )
            revision_hash = revision.revision_hash()
            if revision_hash in revision_hashes:
                raise ProductMasterError(
                    f"product_master_revision_hash_duplicate:{revision_hash}"
                )
            if previous is None:
                if revision.supersedes_hash is not None:
                    raise ProductMasterError(
                        "product_master_initial_revision_supersedes_forbidden"
                    )
            else:
                if _timestamp(
                    revision.recorded_at,
                    "product_master_revision.recorded_at",
                ) <= _timestamp(
                    previous.recorded_at,
                    "product_master_revision.previous_recorded_at",
                ):
                    raise ProductMasterError(
                        "product_master_revision_time_not_increasing"
                    )
                if revision.supersedes_hash != previous.revision_hash():
                    raise ProductMasterError("product_master_revision_chain_broken")
            revision_ids.add(revision.revision_id)
            revision_hashes.add(revision_hash)
            previous = revision

    def revision_as_known(
        self,
        knowledge_at: str,
    ) -> ProductMasterRevision | None:
        cutoff = _timestamp(
            knowledge_at,
            "product_master_history.knowledge_at",
        )
        known = [
            revision
            for revision in self.revisions
            if _timestamp(
                revision.recorded_at,
                "product_master_revision.recorded_at",
            )
            <= cutoff
        ]
        return known[-1] if known else None

    def registry_as_known(self, knowledge_at: str) -> InstrumentRegistry | None:
        revision = self.revision_as_known(knowledge_at)
        return None if revision is None else revision.registry

    def contract_hash_as_known(self, knowledge_at: str) -> str:
        """Hash only revisions available by the transaction-time cutoff."""

        cutoff = _timestamp(
            knowledge_at,
            "product_master_history.knowledge_at",
        )
        known = tuple(
            revision
            for revision in self.revisions
            if _timestamp(
                revision.recorded_at,
                "product_master_revision.recorded_at",
            )
            <= cutoff
        )
        if not known:
            raise ProductMasterError("product_master_snapshot_not_known")
        return sha256_prefixed(
            {
                "revisions": [revision.as_dict() for revision in known],
                "revision_hashes": [revision.revision_hash() for revision in known],
            },
            label="multi_asset_product_master_history_as_known",
        )

    def instrument_as_of(
        self,
        instrument_id: str,
        as_of: str,
        *,
        knowledge_at: str | None = None,
    ) -> Instrument | None:
        cutoff = as_of if knowledge_at is None else knowledge_at
        registry = self.registry_as_known(cutoff)
        if registry is None:
            return None
        return registry.instrument_as_of(
            instrument_id,
            as_of,
            knowledge_at=cutoff,
        )

    def tradable_instrument_as_of(
        self,
        instrument_id: str,
        as_of: str,
        *,
        knowledge_at: str | None = None,
    ) -> Instrument | None:
        cutoff = as_of if knowledge_at is None else knowledge_at
        registry = self.registry_as_known(cutoff)
        if registry is None:
            return None
        return registry.tradable_instrument_as_of(
            instrument_id,
            as_of,
            knowledge_at=cutoff,
        )

    def resolve_symbol(
        self,
        *,
        provider_id: str,
        symbol: str,
        as_of: str,
        knowledge_at: str | None = None,
        venue_mic: str | None = None,
        listing_id: str | None = None,
    ) -> Instrument:
        cutoff = as_of if knowledge_at is None else knowledge_at
        registry = self.registry_as_known(cutoff)
        if registry is None:
            raise ProductMasterError("product_master_snapshot_not_known")
        return registry.resolve_symbol(
            provider_id=provider_id,
            symbol=symbol,
            as_of=as_of,
            knowledge_at=cutoff,
            venue_mic=venue_mic,
            listing_id=listing_id,
        )

    def resolve_issuer_identifier(
        self,
        *,
        namespace: IssuerIdentifierNamespace,
        value: str,
        as_of: str,
        knowledge_at: str | None = None,
        jurisdiction: str | None = None,
        provider_id: str | None = None,
    ) -> Issuer:
        cutoff = as_of if knowledge_at is None else knowledge_at
        registry = self.registry_as_known(cutoff)
        if registry is None:
            raise ProductMasterError("product_master_snapshot_not_known")
        return registry.resolve_issuer_identifier(
            namespace=namespace,
            value=value,
            as_of=as_of,
            knowledge_at=cutoff,
            jurisdiction=jurisdiction,
            provider_id=provider_id,
        )

    def relationship_targets(
        self,
        *,
        source_instrument_id: str,
        relationship_type: InstrumentRelationshipType,
        as_of: str,
        knowledge_at: str | None = None,
    ) -> tuple[Instrument, ...]:
        cutoff = as_of if knowledge_at is None else knowledge_at
        registry = self.registry_as_known(cutoff)
        if registry is None:
            raise ProductMasterError("product_master_snapshot_not_known")
        return registry.relationship_targets(
            source_instrument_id=source_instrument_id,
            relationship_type=relationship_type,
            as_of=as_of,
            knowledge_at=cutoff,
        )

    def contract_specification_as_of(
        self,
        instrument_id: str,
        as_of: str,
        *,
        knowledge_at: str | None = None,
    ) -> ContractSpecification | None:
        cutoff = as_of if knowledge_at is None else knowledge_at
        registry = self.registry_as_known(cutoff)
        if registry is None:
            return None
        return registry.contract_specification_as_of(
            instrument_id,
            as_of,
            knowledge_at=cutoff,
        )

    def lifecycle_events_known_at(
        self,
        as_of: str,
    ) -> tuple[LifecycleEvent, ...]:
        registry = self.registry_as_known(as_of)
        if registry is None:
            return ()
        return registry.lifecycle_events_known_at(as_of)

    def as_dict(self) -> dict[str, object]:
        return {
            "revisions": [revision.as_dict() for revision in self.revisions],
            "revision_hashes": [
                revision.revision_hash() for revision in self.revisions
            ],
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(
            self.as_dict(),
            label="multi_asset_product_master_history",
        )


ProductMasterRegistry = InstrumentRegistry
