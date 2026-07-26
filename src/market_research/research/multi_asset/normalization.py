"""Provider-neutral normalization authority for immutable research inputs.

The adapters in this module decode already-prepared rows.  They never collect,
probe, retry, or backfill market data.  Every normalized row is emitted beside
its immutable raw row in :class:`AppendOnlyBitemporalStore`, with exact
calendar, unit, policy, code, and source-row bindings.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo

from ..hashing import sha256_prefixed
from ..market_calendar_contract import MarketCalendarAuthority
from .data import (
    AppendOnlyBitemporalStore,
    BitemporalRecord,
    DataLayer,
    DataLineage,
    DerivedLayerMetadata,
    NormalizedLayerMetadata,
    ObservationClocks,
    RawLayerMetadata,
    derived_input_snapshot_hash,
)


NORMALIZATION_SCHEMA_VERSION = 1
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_ZERO = Decimal("0")
_ONE = Decimal("1")


class ProviderNormalizationError(ValueError):
    """An immutable provider row cannot be assigned one economic meaning."""


class QuoteConvention(StrEnum):
    DIRECT = "DIRECT"
    RECIPROCAL = "RECIPROCAL"


class MissingValueSemantics(StrEnum):
    REJECT_ROW = "REJECT_ROW"
    EXPLICIT_MISSING = "EXPLICIT_MISSING"


class CorporateActionAdjustment(StrEnum):
    RAW = "RAW"
    SPLIT_ADJUSTED = "SPLIT_ADJUSTED"
    TOTAL_RETURN_ADJUSTED = "TOTAL_RETURN_ADJUSTED"


def _require_id(value: str, field: str) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ProviderNormalizationError(f"{field}_invalid")


def _require_hash(value: str, field: str) -> None:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ProviderNormalizationError(f"{field}_invalid_hash")


def _require_currency(value: str, field: str) -> None:
    if not isinstance(value, str) or not _CURRENCY.fullmatch(value):
        raise ProviderNormalizationError(f"{field}_invalid_currency")


def _timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ProviderNormalizationError(f"{field}_invalid_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderNormalizationError(f"{field}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _decimal(value: object, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ProviderNormalizationError(f"{field}_decimal_required")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProviderNormalizationError(f"{field}_decimal_required") from exc
    if not parsed.is_finite() or (positive and parsed <= _ZERO):
        raise ProviderNormalizationError(f"{field}_invalid_decimal")
    return parsed


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _field(
    fields: Mapping[str, object],
    name: str,
    *,
    missing_tokens: frozenset[str],
) -> object:
    if name not in fields:
        raise ProviderNormalizationError(f"provider_row_field_missing:{name}")
    value = fields[name]
    if value is None or (isinstance(value, str) and value in missing_tokens):
        raise ProviderNormalizationError(f"provider_row_value_missing:{name}")
    return value


@dataclass(frozen=True, slots=True)
class UnitDefinition:
    """One exact linear unit conversion into a registry canonical unit."""

    unit_id: str
    dimension: str
    canonical_unit_id: str
    scale_to_canonical: Decimal
    version: str
    source_hash: str

    def __post_init__(self) -> None:
        for field in ("unit_id", "dimension", "canonical_unit_id", "version"):
            _require_id(getattr(self, field), f"unit.{field}")
        if (
            not isinstance(self.scale_to_canonical, Decimal)
            or not self.scale_to_canonical.is_finite()
            or self.scale_to_canonical <= _ZERO
        ):
            raise ProviderNormalizationError("unit.scale_to_canonical_invalid")
        _require_hash(self.source_hash, "unit.source_hash")

    def as_dict(self) -> dict[str, str]:
        return {
            "unit_id": self.unit_id,
            "dimension": self.dimension,
            "canonical_unit_id": self.canonical_unit_id,
            "scale_to_canonical": _decimal_text(self.scale_to_canonical),
            "version": self.version,
            "source_hash": self.source_hash,
        }


@dataclass(frozen=True, slots=True)
class UnitRegistry:
    """Versioned unit authority; incompatible dimensions fail closed."""

    registry_id: str
    version: str
    definitions: tuple[UnitDefinition, ...]

    def __post_init__(self) -> None:
        _require_id(self.registry_id, "unit_registry.registry_id")
        _require_id(self.version, "unit_registry.version")
        if not self.definitions:
            raise ProviderNormalizationError("unit_registry.definitions_required")
        identifiers = [item.unit_id for item in self.definitions]
        if identifiers != sorted(identifiers) or len(identifiers) != len(
            set(identifiers)
        ):
            raise ProviderNormalizationError(
                "unit_registry.definitions_not_unique_canonical"
            )
        known = set(identifiers)
        for item in self.definitions:
            if item.canonical_unit_id not in known:
                raise ProviderNormalizationError(
                    f"unit_registry.canonical_unit_missing:{item.unit_id}"
                )
            canonical = next(
                candidate
                for candidate in self.definitions
                if candidate.unit_id == item.canonical_unit_id
            )
            if (
                canonical.dimension != item.dimension
                or canonical.canonical_unit_id != canonical.unit_id
                or canonical.scale_to_canonical != _ONE
            ):
                raise ProviderNormalizationError(
                    f"unit_registry.canonical_unit_invalid:{item.unit_id}"
                )

    def definition(self, unit_id: str) -> UnitDefinition:
        matches = [item for item in self.definitions if item.unit_id == unit_id]
        if len(matches) != 1:
            raise ProviderNormalizationError(f"unit_unknown:{unit_id}")
        return matches[0]

    def convert(self, value: Decimal, *, source: str, target: str) -> Decimal:
        source_unit = self.definition(source)
        target_unit = self.definition(target)
        if source_unit.dimension != target_unit.dimension:
            raise ProviderNormalizationError(
                f"unit_dimension_mismatch:{source}:{target}"
            )
        return value * source_unit.scale_to_canonical / target_unit.scale_to_canonical

    def as_dict(self) -> dict[str, object]:
        return {
            "registry_id": self.registry_id,
            "version": self.version,
            "definitions": [item.as_dict() for item in self.definitions],
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="multi_asset_unit_registry")


@dataclass(frozen=True, slots=True)
class CalendarRegistry:
    """Select one immutable calendar version by valid and knowledge time."""

    registry_id: str
    version: str
    calendars: tuple[MarketCalendarAuthority, ...]

    def __post_init__(self) -> None:
        _require_id(self.registry_id, "calendar_registry.registry_id")
        _require_id(self.version, "calendar_registry.version")
        if not self.calendars:
            raise ProviderNormalizationError("calendar_registry.calendars_required")
        identities = [
            (item.calendar_id, item.calendar_version_id) for item in self.calendars
        ]
        if identities != sorted(identities) or len(identities) != len(
            set(identities)
        ):
            raise ProviderNormalizationError(
                "calendar_registry.calendars_not_unique_canonical"
            )

    def resolve(
        self,
        calendar_id: str,
        *,
        trading_date: str,
        knowledge_at: str,
    ) -> MarketCalendarAuthority:
        _require_id(calendar_id, "calendar_registry.calendar_id")
        query_date = date.fromisoformat(trading_date)
        cutoff = _timestamp(knowledge_at, "calendar_registry.knowledge_at")
        candidates: list[MarketCalendarAuthority] = []
        for item in self.calendars:
            if item.calendar_id != calendar_id:
                continue
            valid_from = date.fromisoformat(item.valid_from)
            valid_to = (
                date.fromisoformat(item.valid_to)
                if item.valid_to is not None
                else None
            )
            if query_date < valid_from or (
                valid_to is not None and query_date > valid_to
            ):
                continue
            if _timestamp(item.observed_at, "calendar.observed_at") <= cutoff:
                candidates.append(item)
        if not candidates:
            raise ProviderNormalizationError(
                f"calendar_not_known_or_valid:{calendar_id}:{trading_date}"
            )
        candidates.sort(
            key=lambda item: (
                _timestamp(item.observed_at, "calendar.observed_at"),
                item.version,
            )
        )
        if len(candidates) > 1 and (
            candidates[-1].observed_at,
            candidates[-1].version,
        ) == (
            candidates[-2].observed_at,
            candidates[-2].version,
        ):
            raise ProviderNormalizationError(
                f"calendar_version_ambiguous:{calendar_id}"
            )
        return candidates[-1]

    def as_dict(self) -> dict[str, object]:
        return {
            "registry_id": self.registry_id,
            "version": self.version,
            "calendars": [
                {
                    "calendar_id": item.calendar_id,
                    "calendar_version_id": item.calendar_version_id,
                    "contract_hash": item.contract_hash(),
                }
                for item in self.calendars
            ],
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="multi_asset_calendar_registry")


@dataclass(frozen=True, slots=True)
class ProviderRow:
    """Raw row and immutable artifact identity supplied by an offline importer."""

    row_id: str
    provider_id: str
    provider_version: str
    schema_id: str
    source_object_id: str
    collection_batch_id: str
    source_artifact_hash: str
    source_schema_hash: str
    ingested_at: str
    fields: Mapping[str, object]
    revision: int = 1
    supersedes_hash: str | None = None

    def __post_init__(self) -> None:
        for field in (
            "row_id",
            "provider_id",
            "provider_version",
            "schema_id",
            "source_object_id",
            "collection_batch_id",
        ):
            _require_id(getattr(self, field), f"provider_row.{field}")
        _require_hash(self.source_artifact_hash, "provider_row.source_artifact_hash")
        _require_hash(self.source_schema_hash, "provider_row.source_schema_hash")
        _timestamp(self.ingested_at, "provider_row.ingested_at")
        if isinstance(self.revision, bool) or self.revision < 1:
            raise ProviderNormalizationError("provider_row.revision_invalid")
        if (self.revision == 1) != (self.supersedes_hash is None):
            raise ProviderNormalizationError(
                "provider_row.supersedes_binding_invalid"
            )
        if self.supersedes_hash is not None:
            _require_hash(self.supersedes_hash, "provider_row.supersedes_hash")
        if not self.fields:
            raise ProviderNormalizationError("provider_row.fields_required")
        for key, value in self.fields.items():
            _require_id(key, "provider_row.field_name")
            if isinstance(value, float):
                raise ProviderNormalizationError(
                    f"provider_row_float_forbidden:{key}"
                )

    def as_dict(self) -> dict[str, object]:
        return {
            "row_id": self.row_id,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "schema_id": self.schema_id,
            "source_object_id": self.source_object_id,
            "collection_batch_id": self.collection_batch_id,
            "source_artifact_hash": self.source_artifact_hash,
            "source_schema_hash": self.source_schema_hash,
            "ingested_at": self.ingested_at,
            "fields": dict(sorted(self.fields.items())),
            "revision": self.revision,
            "supersedes_hash": self.supersedes_hash,
        }

    def row_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="multi_asset_provider_row")


@dataclass(frozen=True, slots=True)
class ProviderNormalizationPolicy:
    policy_id: str
    version: str
    provider_id: str
    instrument_id: str
    provider_symbol: str
    calendar_id: str
    exchange_session_id: str
    source_timezone: str
    source_price_unit: str
    target_price_unit: str
    source_quantity_unit: str
    target_quantity_unit: str
    currency: str
    contract_multiplier: Decimal
    price_scale: Decimal
    quantity_scale: Decimal
    quote_convention: QuoteConvention
    corporate_action_adjustment: CorporateActionAdjustment
    missing_value_semantics: MissingValueSemantics
    missing_tokens: tuple[str, ...] = ("", "N/A", "NA", "NULL")
    provider_priority: int = 1

    def __post_init__(self) -> None:
        for field in (
            "policy_id",
            "version",
            "provider_id",
            "instrument_id",
            "provider_symbol",
            "calendar_id",
            "exchange_session_id",
            "source_price_unit",
            "target_price_unit",
            "source_quantity_unit",
            "target_quantity_unit",
        ):
            _require_id(getattr(self, field), f"normalization_policy.{field}")
        try:
            ZoneInfo(self.source_timezone)
        except (ValueError, KeyError) as exc:
            raise ProviderNormalizationError(
                "normalization_policy.source_timezone_unknown"
            ) from exc
        _require_currency(self.currency, "normalization_policy.currency")
        for field in ("contract_multiplier", "price_scale", "quantity_scale"):
            value = getattr(self, field)
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value <= _ZERO
            ):
                raise ProviderNormalizationError(
                    f"normalization_policy.{field}_invalid"
                )
        if not isinstance(self.quote_convention, QuoteConvention):
            raise ProviderNormalizationError(
                "normalization_policy.quote_convention_invalid"
            )
        if not isinstance(
            self.corporate_action_adjustment, CorporateActionAdjustment
        ):
            raise ProviderNormalizationError(
                "normalization_policy.corporate_action_adjustment_invalid"
            )
        if not isinstance(self.missing_value_semantics, MissingValueSemantics):
            raise ProviderNormalizationError(
                "normalization_policy.missing_value_semantics_invalid"
            )
        if (
            tuple(sorted(set(self.missing_tokens))) != self.missing_tokens
            or isinstance(self.provider_priority, bool)
            or self.provider_priority < 1
        ):
            raise ProviderNormalizationError(
                "normalization_policy_missing_or_priority_invalid"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "provider_id": self.provider_id,
            "instrument_id": self.instrument_id,
            "provider_symbol": self.provider_symbol,
            "calendar_id": self.calendar_id,
            "exchange_session_id": self.exchange_session_id,
            "source_timezone": self.source_timezone,
            "source_price_unit": self.source_price_unit,
            "target_price_unit": self.target_price_unit,
            "source_quantity_unit": self.source_quantity_unit,
            "target_quantity_unit": self.target_quantity_unit,
            "currency": self.currency,
            "contract_multiplier": _decimal_text(self.contract_multiplier),
            "price_scale": _decimal_text(self.price_scale),
            "quantity_scale": _decimal_text(self.quantity_scale),
            "quote_convention": self.quote_convention.value,
            "corporate_action_adjustment": (
                self.corporate_action_adjustment.value
            ),
            "missing_value_semantics": self.missing_value_semantics.value,
            "missing_tokens": list(self.missing_tokens),
            "provider_priority": self.provider_priority,
        }

    def policy_hash(self) -> str:
        return sha256_prefixed(
            self.as_dict(), label="multi_asset_provider_normalization_policy"
        )


@dataclass(frozen=True, slots=True)
class DecodedProviderObservation:
    event_at: str
    publication_at: str
    availability_at: str
    trading_date: str
    session_id: str
    provider_symbol: str
    price: Decimal | None
    quantity: Decimal | None
    quality_flags: tuple[str, ...] = ()
    exclusion_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        event = _timestamp(self.event_at, "decoded.event_at")
        publication = _timestamp(self.publication_at, "decoded.publication_at")
        availability = _timestamp(self.availability_at, "decoded.availability_at")
        if publication < event or availability < publication:
            raise ProviderNormalizationError("decoded_clock_order_invalid")
        try:
            date.fromisoformat(self.trading_date)
        except ValueError as exc:
            raise ProviderNormalizationError(
                "decoded.trading_date_invalid"
            ) from exc
        _require_id(self.session_id, "decoded.session_id")
        _require_id(self.provider_symbol, "decoded.provider_symbol")
        if (self.price is None) != (self.quantity is None):
            raise ProviderNormalizationError(
                "decoded_missing_price_quantity_mismatch"
            )
        if self.price is not None and (
            not self.price.is_finite() or self.price <= _ZERO
        ):
            raise ProviderNormalizationError("decoded.price_invalid")
        if self.quantity is not None and (
            not self.quantity.is_finite() or self.quantity < _ZERO
        ):
            raise ProviderNormalizationError("decoded.quantity_invalid")
        for values, field in (
            (self.quality_flags, "decoded.quality_flag"),
            (self.exclusion_reasons, "decoded.exclusion_reason"),
        ):
            if tuple(sorted(set(values))) != values:
                raise ProviderNormalizationError(f"{field}_not_unique_canonical")
            for value in values:
                _require_id(value, field)


class ProviderNormalizationAdapter(Protocol):
    """Provider convention decoder used by the common normalization service."""

    @property
    def adapter_id(self) -> str: ...

    @property
    def adapter_version(self) -> str: ...

    def decode(
        self,
        row: ProviderRow,
        policy: ProviderNormalizationPolicy,
    ) -> DecodedProviderObservation: ...


@dataclass(frozen=True, slots=True)
class IsoLocalDirectAdapter:
    """Convention A: local ISO event time and direct decimal price."""

    adapter_id: str = "provider.iso-local-direct"
    adapter_version: str = "1"

    def decode(
        self,
        row: ProviderRow,
        policy: ProviderNormalizationPolicy,
    ) -> DecodedProviderObservation:
        if row.provider_id != policy.provider_id:
            raise ProviderNormalizationError("provider_policy_mismatch")
        missing = frozenset(policy.missing_tokens)
        symbol = str(_field(row.fields, "symbol", missing_tokens=missing))
        if symbol != policy.provider_symbol:
            raise ProviderNormalizationError("provider_symbol_mismatch")
        local_text = str(
            _field(row.fields, "local_timestamp", missing_tokens=missing)
        )
        try:
            local = datetime.fromisoformat(local_text)
        except ValueError as exc:
            raise ProviderNormalizationError(
                "provider_local_timestamp_invalid"
            ) from exc
        if local.tzinfo is not None:
            raise ProviderNormalizationError(
                "provider_local_timestamp_must_be_naive"
            )
        zone = ZoneInfo(policy.source_timezone)
        aware = local.replace(tzinfo=zone, fold=0)
        round_trip = aware.astimezone(timezone.utc).astimezone(zone).replace(
            tzinfo=None
        )
        alternate = local.replace(tzinfo=zone, fold=1)
        if round_trip != local or aware.utcoffset() != alternate.utcoffset():
            raise ProviderNormalizationError(
                "provider_local_timestamp_dst_ambiguous_or_nonexistent"
            )
        publication = _timestamp(
            str(_field(row.fields, "published_at", missing_tokens=missing)),
            "provider.published_at",
        )
        availability = _timestamp(
            str(_field(row.fields, "available_at", missing_tokens=missing)),
            "provider.available_at",
        )
        return DecodedProviderObservation(
            event_at=_timestamp_text(aware.astimezone(timezone.utc)),
            publication_at=_timestamp_text(publication),
            availability_at=_timestamp_text(availability),
            trading_date=local.date().isoformat(),
            session_id=str(
                _field(row.fields, "session_id", missing_tokens=missing)
            ),
            provider_symbol=symbol,
            price=_decimal(
                _field(row.fields, "price", missing_tokens=missing),
                "provider.price",
                positive=True,
            ),
            quantity=_decimal(
                _field(row.fields, "quantity", missing_tokens=missing),
                "provider.quantity",
            ),
        )


@dataclass(frozen=True, slots=True)
class EpochScaledReciprocalAdapter:
    """Convention B: UTC epoch milliseconds, scaled reciprocal quote."""

    adapter_id: str = "provider.epoch-scaled-reciprocal"
    adapter_version: str = "1"

    def decode(
        self,
        row: ProviderRow,
        policy: ProviderNormalizationPolicy,
    ) -> DecodedProviderObservation:
        if row.provider_id != policy.provider_id:
            raise ProviderNormalizationError("provider_policy_mismatch")
        missing = frozenset(policy.missing_tokens)
        symbol = str(_field(row.fields, "ticker", missing_tokens=missing))
        if symbol != policy.provider_symbol:
            raise ProviderNormalizationError("provider_symbol_mismatch")
        event = self._milliseconds(row.fields, "event_ms", missing)
        publication = self._milliseconds(row.fields, "publication_ms", missing)
        availability = self._milliseconds(row.fields, "availability_ms", missing)
        reciprocal = _decimal(
            _field(
                row.fields,
                "reciprocal_price_scaled",
                missing_tokens=missing,
            ),
            "provider.reciprocal_price_scaled",
            positive=True,
        )
        quantity = _decimal(
            _field(row.fields, "size_lots", missing_tokens=missing),
            "provider.size_lots",
        )
        local = event.astimezone(ZoneInfo(policy.source_timezone))
        return DecodedProviderObservation(
            event_at=_timestamp_text(event),
            publication_at=_timestamp_text(publication),
            availability_at=_timestamp_text(availability),
            trading_date=local.date().isoformat(),
            session_id=str(
                _field(row.fields, "session_code", missing_tokens=missing)
            ),
            provider_symbol=symbol,
            price=reciprocal,
            quantity=quantity,
        )

    @staticmethod
    def _milliseconds(
        fields: Mapping[str, object],
        name: str,
        missing: frozenset[str],
    ) -> datetime:
        raw = _field(fields, name, missing_tokens=missing)
        if isinstance(raw, bool):
            raise ProviderNormalizationError(f"provider.{name}_invalid")
        try:
            milliseconds = int(str(raw))
        except ValueError as exc:
            raise ProviderNormalizationError(f"provider.{name}_invalid") from exc
        seconds, remainder = divmod(milliseconds, 1000)
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
            seconds=seconds,
            milliseconds=remainder,
        )


@dataclass(frozen=True, slots=True)
class NormalizationReceipt:
    adapter_id: str
    adapter_version: str
    policy_hash: str
    calendar_hash: str
    unit_registry_hash: str
    raw_record_hash: str
    normalized_record_hash: str
    input_row_hash: str
    output_hash: str
    quality_flags: tuple[str, ...]
    exclusion_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in ("adapter_id", "adapter_version"):
            _require_id(getattr(self, field), f"normalization_receipt.{field}")
        for field in (
            "policy_hash",
            "calendar_hash",
            "unit_registry_hash",
            "raw_record_hash",
            "normalized_record_hash",
            "input_row_hash",
            "output_hash",
        ):
            _require_hash(getattr(self, field), f"normalization_receipt.{field}")

    def as_dict(self) -> dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "policy_hash": self.policy_hash,
            "calendar_hash": self.calendar_hash,
            "unit_registry_hash": self.unit_registry_hash,
            "raw_record_hash": self.raw_record_hash,
            "normalized_record_hash": self.normalized_record_hash,
            "input_row_hash": self.input_row_hash,
            "output_hash": self.output_hash,
            "quality_flags": list(self.quality_flags),
            "exclusion_reasons": list(self.exclusion_reasons),
        }

    def receipt_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="normalization_receipt")


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    store: AppendOnlyBitemporalStore
    raw_record: BitemporalRecord
    normalized_record: BitemporalRecord
    receipt: NormalizationReceipt


@dataclass(frozen=True, slots=True)
class ProviderNormalizationService:
    """Single authority that materializes raw and normalized record layers."""

    calendar_registry: CalendarRegistry
    unit_registry: UnitRegistry
    adapters: tuple[ProviderNormalizationAdapter, ...]

    def __post_init__(self) -> None:
        if not self.adapters:
            raise ProviderNormalizationError("normalization.adapters_required")
        identities = [
            (item.adapter_id, item.adapter_version) for item in self.adapters
        ]
        if identities != sorted(identities) or len(identities) != len(
            set(identities)
        ):
            raise ProviderNormalizationError(
                "normalization.adapters_not_unique_canonical"
            )

    def normalize(
        self,
        row: ProviderRow,
        *,
        policy: ProviderNormalizationPolicy,
        adapter_id: str,
        store: AppendOnlyBitemporalStore | None = None,
    ) -> NormalizationResult:
        matches = [item for item in self.adapters if item.adapter_id == adapter_id]
        if len(matches) != 1:
            raise ProviderNormalizationError(
                f"normalization.adapter_not_unique:{adapter_id}"
            )
        adapter = matches[0]
        decoded = adapter.decode(row, policy)
        if decoded.session_id != policy.exchange_session_id:
            raise ProviderNormalizationError("provider_session_mismatch")
        calendar = self.calendar_registry.resolve(
            policy.calendar_id,
            trading_date=decoded.trading_date,
            knowledge_at=decoded.availability_at,
        )
        if not calendar.is_open_at(
            timestamp=decoded.event_at,
            known_at=decoded.availability_at,
        ):
            raise ProviderNormalizationError("provider_event_outside_session")
        if decoded.price is None or decoded.quantity is None:
            if (
                policy.missing_value_semantics
                is MissingValueSemantics.REJECT_ROW
            ):
                raise ProviderNormalizationError("provider_row_missing_rejected")
            raise ProviderNormalizationError(
                "explicit_missing_rows_are_evidence_only"
            )

        scaled_price = decoded.price * policy.price_scale
        if policy.quote_convention is QuoteConvention.RECIPROCAL:
            if scaled_price == _ZERO:
                raise ProviderNormalizationError("reciprocal_quote_zero")
            scaled_price = _ONE / scaled_price
        scaled_quantity = decoded.quantity * policy.quantity_scale
        price = self.unit_registry.convert(
            scaled_price,
            source=policy.source_price_unit,
            target=policy.target_price_unit,
        )
        quantity = self.unit_registry.convert(
            scaled_quantity,
            source=policy.source_quantity_unit,
            target=policy.target_quantity_unit,
        )
        if price <= _ZERO or quantity < _ZERO:
            raise ProviderNormalizationError("normalized_value_out_of_domain")

        existing = store or AppendOnlyBitemporalStore()
        raw_payload = dict(sorted(row.fields.items()))
        raw_record = BitemporalRecord(
            record_id=f"raw.{row.provider_id}.{row.row_id}",
            version=row.revision,
            layer=DataLayer.RAW,
            instrument_id=policy.instrument_id,
            data_kind="market_observation",
            clocks=ObservationClocks(
                event_at=decoded.event_at,
                knowledge_at=decoded.publication_at,
                revision_at=decoded.publication_at,
                received_at=decoded.availability_at,
                ingested_at=row.ingested_at,
            ),
            payload=raw_payload,
            lineage=DataLineage(
                source_id=row.provider_id,
                source_version=row.provider_version,
                source_artifact_hash=row.source_artifact_hash,
                source_schema_hash=row.source_schema_hash,
            ),
            layer_metadata=RawLayerMetadata(
                provider_record_id=row.row_id,
                provider_symbol=decoded.provider_symbol,
                source_object_id=row.source_object_id,
                collection_batch_id=row.collection_batch_id,
                original_schema_id=row.schema_id,
                provider_version=row.provider_version,
                payload_checksum=sha256_prefixed(
                    raw_payload, label="multi_asset_data_payload"
                ),
                quality_flags=decoded.quality_flags,
            ),
            supersedes_hash=row.supersedes_hash,
            correction_reason=(
                "provider_revision" if row.supersedes_hash is not None else None
            ),
        )
        with_raw = existing.append(raw_record)
        parameter_hash = sha256_prefixed(
            {
                "policy_hash": policy.policy_hash(),
                "adapter_id": adapter.adapter_id,
                "adapter_version": adapter.adapter_version,
                "calendar_hash": calendar.contract_hash(),
                "unit_registry_hash": self.unit_registry.contract_hash(),
            },
            label="multi_asset_normalization_parameters",
        )
        normalized_payload = {
            "event_at": decoded.event_at,
            "publication_at": decoded.publication_at,
            "availability_at": decoded.availability_at,
            "trading_date": decoded.trading_date,
            "session_id": decoded.session_id,
            "instrument_id": policy.instrument_id,
            "provider_symbol": decoded.provider_symbol,
            "price": _decimal_text(price),
            "quantity": _decimal_text(quantity),
            "currency": policy.currency,
            "price_unit": policy.target_price_unit,
            "quantity_unit": policy.target_quantity_unit,
            "contract_multiplier": _decimal_text(policy.contract_multiplier),
            "corporate_action_adjustment": (
                policy.corporate_action_adjustment.value
            ),
            "quote_convention": policy.quote_convention.value,
            "missing_value_semantics": policy.missing_value_semantics.value,
            "calendar_id": calendar.calendar_id,
            "calendar_version_id": calendar.calendar_version_id,
            "calendar_hash": calendar.contract_hash(),
            "unit_registry_hash": self.unit_registry.contract_hash(),
            "source_row_hash": row.row_hash(),
        }
        prior_normalized = existing.correction_history(
            f"normalized.{row.provider_id}.{row.row_id}",
            layer=DataLayer.NORMALIZED,
        )
        normalized_supersedes = (
            prior_normalized[-1].record_hash() if prior_normalized else None
        )
        if (row.revision == 1) != (normalized_supersedes is None):
            raise ProviderNormalizationError(
                "normalized_revision_history_incomplete"
            )
        normalized_record = BitemporalRecord(
            record_id=f"normalized.{row.provider_id}.{row.row_id}",
            version=row.revision,
            layer=DataLayer.NORMALIZED,
            instrument_id=policy.instrument_id,
            data_kind="market_observation",
            clocks=ObservationClocks(
                event_at=decoded.event_at,
                knowledge_at=row.ingested_at,
                revision_at=row.ingested_at,
                received_at=row.ingested_at,
                ingested_at=row.ingested_at,
            ),
            payload=normalized_payload,
            lineage=DataLineage(
                source_id=row.provider_id,
                source_version=row.provider_version,
                source_artifact_hash=row.source_artifact_hash,
                source_schema_hash=row.source_schema_hash,
                upstream_record_hashes=(raw_record.record_hash(),),
                transformation_id=adapter.adapter_id,
                transformation_version=adapter.adapter_version,
                parameters_hash=parameter_hash,
            ),
            layer_metadata=NormalizedLayerMetadata(
                internal_instrument_id=policy.instrument_id,
                source_timezone=policy.source_timezone,
                target_timezone="UTC",
                source_price_unit=policy.source_price_unit,
                target_price_unit=policy.target_price_unit,
                source_quantity_unit=policy.source_quantity_unit,
                target_quantity_unit=policy.target_quantity_unit,
                currency=policy.currency,
                exchange_session_id=policy.exchange_session_id,
                provider_priority=policy.provider_priority,
                duplicate_resolution="UNIQUE",
                unit_conversion_id=self.unit_registry.registry_id,
                missing_value=False,
                outlier=False,
                revised=row.revision > 1,
                quality_flags=decoded.quality_flags,
            ),
            supersedes_hash=normalized_supersedes,
            correction_reason=(
                "provider_revision"
                if normalized_supersedes is not None
                else None
            ),
        )
        complete = with_raw.append(normalized_record)
        output_hash = sha256_prefixed(
            normalized_payload, label="multi_asset_normalized_output"
        )
        receipt = NormalizationReceipt(
            adapter_id=adapter.adapter_id,
            adapter_version=adapter.adapter_version,
            policy_hash=policy.policy_hash(),
            calendar_hash=calendar.contract_hash(),
            unit_registry_hash=self.unit_registry.contract_hash(),
            raw_record_hash=raw_record.record_hash(),
            normalized_record_hash=normalized_record.record_hash(),
            input_row_hash=row.row_hash(),
            output_hash=output_hash,
            quality_flags=decoded.quality_flags,
            exclusion_reasons=decoded.exclusion_reasons,
        )
        return NormalizationResult(
            store=complete,
            raw_record=raw_record,
            normalized_record=normalized_record,
            receipt=receipt,
        )

    def normalize_many(
        self,
        rows: Sequence[ProviderRow],
        *,
        policies: Mapping[str, ProviderNormalizationPolicy],
        adapter_ids: Mapping[str, str],
    ) -> tuple[NormalizationResult, ...]:
        """Normalize an ordered immutable batch with duplicate/order checks."""

        if not rows:
            raise ProviderNormalizationError("normalization.rows_required")
        identities = [(item.provider_id, item.row_id, item.revision) for item in rows]
        if len(identities) != len(set(identities)):
            raise ProviderNormalizationError("provider_row_duplicate")
        results: list[NormalizationResult] = []
        store = AppendOnlyBitemporalStore()
        previous_ingested: datetime | None = None
        for row in rows:
            ingested = _timestamp(row.ingested_at, "provider_row.ingested_at")
            if previous_ingested is not None and ingested < previous_ingested:
                raise ProviderNormalizationError(
                    "provider_rows_out_of_order_arrival"
                )
            try:
                policy = policies[row.provider_id]
                adapter_id = adapter_ids[row.provider_id]
            except KeyError as exc:
                raise ProviderNormalizationError(
                    f"provider_configuration_missing:{row.provider_id}"
                ) from exc
            result = self.normalize(
                row,
                policy=policy,
                adapter_id=adapter_id,
                store=store,
            )
            results.append(result)
            store = result.store
            previous_ingested = ingested
        return tuple(results)


def derive_normalized_value(
    store: AppendOnlyBitemporalStore,
    *,
    record_id: str,
    instrument_id: str,
    data_kind: str,
    generated_at: str,
    model_id: str,
    model_version: str,
    code_version: str,
    code_hash: str,
    policy_hash: str,
    upstream_record_hashes: tuple[str, ...],
    payload: Mapping[str, object],
    quality_flags: tuple[str, ...] = (),
) -> tuple[AppendOnlyBitemporalStore, BitemporalRecord]:
    """Create one derived row with complete transformation/code bindings."""

    _require_hash(policy_hash, "derived.policy_hash")
    upstream = tuple(sorted(upstream_record_hashes))
    if not upstream:
        raise ProviderNormalizationError("derived.upstream_required")
    upstream_records = tuple(store.record_for_hash(item) for item in upstream)
    if any(item.layer is DataLayer.RAW for item in upstream_records):
        raise ProviderNormalizationError("derived.raw_upstream_forbidden")
    latest_availability = max(
        _timestamp(item.clocks.ingested_at, "derived.upstream_ingested_at")
        for item in upstream_records
    )
    generated = _timestamp(generated_at, "derived.generated_at")
    if generated < latest_availability:
        raise ProviderNormalizationError("derived_before_upstream_available")
    parameters_hash = sha256_prefixed(
        {"policy_hash": policy_hash},
        label="multi_asset_derived_parameters",
    )
    record = BitemporalRecord(
        record_id=record_id,
        version=1,
        layer=DataLayer.DERIVED,
        instrument_id=instrument_id,
        data_kind=data_kind,
        clocks=ObservationClocks(
            event_at=max(item.clocks.event_at for item in upstream_records),
            knowledge_at=generated_at,
            revision_at=generated_at,
            received_at=generated_at,
            ingested_at=generated_at,
        ),
        payload=payload,
        lineage=DataLineage(
            source_id="normalized.research.inputs",
            source_version=model_version,
            source_artifact_hash=store.content_hash(),
            source_schema_hash=sha256_prefixed(
                {"schema_version": NORMALIZATION_SCHEMA_VERSION},
                label="multi_asset_normalization_schema",
            ),
            upstream_record_hashes=upstream,
            transformation_id=model_id,
            transformation_version=model_version,
            parameters_hash=parameters_hash,
        ),
        layer_metadata=DerivedLayerMetadata(
            model_id=model_id,
            model_version=model_version,
            input_snapshot_hash=derived_input_snapshot_hash(upstream),
            generated_at=generated_at,
            code_version=code_version,
            code_hash=code_hash,
            quality_flags=quality_flags,
        ),
    )
    return store.append(record), record
