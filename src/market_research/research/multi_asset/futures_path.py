"""Point-in-time futures research contracts layered over the futures engine.

The product-specific :mod:`market_research.research.derivatives.futures` module
remains authoritative for contract, quote, continuous-series, execution,
settlement, and fee semantics.  This module adds the missing research-level
history, curve, exposure-preserving roll, and reconciliation contracts without
making continuous series tradable or changing the existing simulator.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_UP,
)
from enum import StrEnum
from typing import Protocol, TypeVar, runtime_checkable

from market_research.research.derivatives.common import (
    AvailabilityTimes,
    decimal_text,
    parse_timestamp,
)
from market_research.research.derivatives.futures import (
    ContractChainSnapshot,
    ContractQuote,
    FuturesCostPolicy,
    FuturesFill,
    MarginSimulationPolicy,
    OrderSide,
    RollExecution,
    SettlementEvent,
    SettlementType,
    compute_basis_feature,
    compute_curve_feature,
    select_chain_as_of,
)
from market_research.research.hashing import sha256_prefixed


FUTURES_PATH_SCHEMA_VERSION = 2
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_CURRENCY = re.compile(r"^[A-Z][A-Z0-9]{2,11}$")
_ZERO = Decimal("0")
_ONE = Decimal("1")
_DAYS_PER_YEAR = Decimal("365")


class FuturesPathError(ValueError):
    """A futures research path is ambiguous, unavailable, or unreconciled."""


def _require_id(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise FuturesPathError(f"{field_name}_invalid_stable_id")


def _require_hash(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise FuturesPathError(f"{field_name}_invalid_hash")


def _require_currency(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _CURRENCY.fullmatch(value):
        raise FuturesPathError(f"{field_name}_invalid_currency")


def _timestamp_text(value: str, field_name: str) -> str:
    try:
        parsed = parse_timestamp(value, field_name)
    except ValueError as exc:
        raise FuturesPathError(f"{field_name}_invalid_timestamp") from exc
    return parsed.isoformat().replace("+00:00", "Z")


def _date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise FuturesPathError(f"{field_name}_invalid_date") from exc


def _decimal(
    value: object,
    field_name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if isinstance(value, (bool, float)):
        raise FuturesPathError(f"{field_name}_must_be_exact_decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FuturesPathError(f"{field_name}_invalid_decimal") from exc
    if not result.is_finite():
        raise FuturesPathError(f"{field_name}_non_finite")
    if positive and result <= 0:
        raise FuturesPathError(f"{field_name}_must_be_positive")
    if nonnegative and result < 0:
        raise FuturesPathError(f"{field_name}_must_be_nonnegative")
    return result


def _content_hash(label: str, payload: dict[str, object]) -> str:
    return sha256_prefixed(payload, label=label)


@runtime_checkable
class FuturesContractProtocol(Protocol):
    """Read-only surface implemented by the existing ``FuturesContract``."""

    @property
    def contract_id(self) -> str: ...

    @property
    def root_id(self) -> str: ...

    @property
    def last_trade_date(self) -> str: ...

    @property
    def first_notice_date(self) -> str | None: ...

    @property
    def expiration_date(self) -> str: ...

    @property
    def contract_multiplier(self) -> Decimal: ...

    @property
    def tick_size(self) -> Decimal: ...

    @property
    def settlement_type(self) -> SettlementType: ...

    @property
    def spec_effective_at(self) -> str: ...

    @property
    def spec_version(self) -> str: ...

    @property
    def availability(self) -> AvailabilityTimes: ...

    @property
    def content_hash(self) -> str: ...

    def tradable_at(self, as_of: str) -> bool: ...


@runtime_checkable
class ContinuousPointProtocol(Protocol):
    """Structural adapter for existing continuous-futures observations."""

    @property
    def point_id(self) -> str: ...

    @property
    def series_id(self) -> str: ...

    @property
    def root_id(self) -> str: ...

    @property
    def observed_at(self) -> str: ...

    @property
    def source_contract_id(self) -> str: ...

    @property
    def source_quote_hash(self) -> str: ...

    @property
    def source_price(self) -> Decimal: ...

    @property
    def continuous_price(self) -> Decimal: ...

    @property
    def previous_point_hash(self) -> str | None: ...

    @property
    def signal_only(self) -> bool: ...

    @property
    def content_hash(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ReferenceMetadata:
    """Effective-time, knowledge-time, and immutable source binding."""

    effective_from: str
    effective_to: str | None
    knowledge_at: str
    source_id: str
    source_version: str
    source_hash: str

    def __post_init__(self) -> None:
        start = _timestamp_text(self.effective_from, "reference.effective_from")
        knowledge = _timestamp_text(self.knowledge_at, "reference.knowledge_at")
        object.__setattr__(self, "effective_from", start)
        object.__setattr__(self, "knowledge_at", knowledge)
        if self.effective_to is not None:
            end = _timestamp_text(self.effective_to, "reference.effective_to")
            if parse_timestamp(end, "reference.effective_to") <= parse_timestamp(
                start, "reference.effective_from"
            ):
                raise FuturesPathError("reference_effective_interval_invalid")
            object.__setattr__(self, "effective_to", end)
        _require_id(self.source_id, "reference.source_id")
        _require_id(self.source_version, "reference.source_version")
        _require_hash(self.source_hash, "reference.source_hash")

    def effective_at(self, valid_at: str) -> bool:
        instant = parse_timestamp(valid_at, "reference.valid_at")
        start = parse_timestamp(self.effective_from, "reference.effective_from")
        end = (
            None
            if self.effective_to is None
            else parse_timestamp(self.effective_to, "reference.effective_to")
        )
        return start <= instant and (end is None or instant < end)

    def known_at(self, as_of: str) -> bool:
        return parse_timestamp(
            self.knowledge_at, "reference.knowledge_at"
        ) <= parse_timestamp(as_of, "reference.as_of")

    def overlaps(self, other: ReferenceMetadata) -> bool:
        start = parse_timestamp(self.effective_from, "reference.effective_from")
        other_start = parse_timestamp(
            other.effective_from, "reference.other_effective_from"
        )
        end = (
            None
            if self.effective_to is None
            else parse_timestamp(self.effective_to, "reference.effective_to")
        )
        other_end = (
            None
            if other.effective_to is None
            else parse_timestamp(other.effective_to, "reference.other_effective_to")
        )
        return (end is None or other_start < end) and (
            other_end is None or start < other_end
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": FUTURES_PATH_SCHEMA_VERSION,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "knowledge_at": self.knowledge_at,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "source_hash": self.source_hash,
        }


@dataclass(frozen=True, slots=True)
class ContractSpecificationVersion:
    record_id: str
    contract_id: str
    root_id: str
    quote_currency: str
    contract_multiplier: Decimal
    tick_size: Decimal
    settlement_type: SettlementType
    last_trade_date: str
    first_notice_date: str | None
    expiration_date: str
    metadata: ReferenceMetadata
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for name, value in (
            ("record_id", self.record_id),
            ("contract_id", self.contract_id),
            ("root_id", self.root_id),
        ):
            _require_id(value, f"contract_specification.{name}")
        _require_currency(self.quote_currency, "contract_specification.quote_currency")
        object.__setattr__(
            self,
            "contract_multiplier",
            _decimal(
                self.contract_multiplier,
                "contract_specification.contract_multiplier",
                positive=True,
            ),
        )
        object.__setattr__(
            self,
            "tick_size",
            _decimal(
                self.tick_size,
                "contract_specification.tick_size",
                positive=True,
            ),
        )
        last_trade = _date(
            self.last_trade_date, "contract_specification.last_trade_date"
        )
        expiration = _date(
            self.expiration_date, "contract_specification.expiration_date"
        )
        if expiration < last_trade:
            raise FuturesPathError("contract_specification_expiration_before_trade")
        if self.first_notice_date is not None:
            notice = _date(
                self.first_notice_date,
                "contract_specification.first_notice_date",
            )
            if notice > expiration:
                raise FuturesPathError("contract_specification_notice_after_expiration")
        if (
            self.settlement_type is SettlementType.PHYSICAL_SETTLED
            and self.first_notice_date is None
        ):
            raise FuturesPathError("physical_contract_first_notice_required")
        object.__setattr__(
            self,
            "content_hash",
            _content_hash("futures_contract_specification_version", self.as_dict()),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": FUTURES_PATH_SCHEMA_VERSION,
            "record_id": self.record_id,
            "contract_id": self.contract_id,
            "root_id": self.root_id,
            "quote_currency": self.quote_currency,
            "contract_multiplier": decimal_text(self.contract_multiplier),
            "tick_size": decimal_text(self.tick_size),
            "settlement_type": self.settlement_type.value,
            "last_trade_date": self.last_trade_date,
            "first_notice_date": self.first_notice_date,
            "expiration_date": self.expiration_date,
            "metadata": self.metadata.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class MarginRequirementVersion:
    record_id: str
    contract_id: str
    currency: str
    initial_margin_per_contract: Decimal
    maintenance_margin_per_contract: Decimal
    collateral_fraction: Decimal
    variation_margin_enabled: bool
    metadata: ReferenceMetadata
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.record_id, "margin_requirement.record_id")
        _require_id(self.contract_id, "margin_requirement.contract_id")
        _require_currency(self.currency, "margin_requirement.currency")
        initial = _decimal(
            self.initial_margin_per_contract,
            "margin_requirement.initial_margin_per_contract",
            positive=True,
        )
        maintenance = _decimal(
            self.maintenance_margin_per_contract,
            "margin_requirement.maintenance_margin_per_contract",
            positive=True,
        )
        collateral = _decimal(
            self.collateral_fraction,
            "margin_requirement.collateral_fraction",
            positive=True,
        )
        if maintenance > initial:
            raise FuturesPathError("maintenance_margin_exceeds_initial")
        if collateral > _ONE:
            raise FuturesPathError("collateral_fraction_exceeds_one")
        if not self.variation_margin_enabled:
            raise FuturesPathError("daily_variation_margin_required")
        object.__setattr__(self, "initial_margin_per_contract", initial)
        object.__setattr__(self, "maintenance_margin_per_contract", maintenance)
        object.__setattr__(self, "collateral_fraction", collateral)
        object.__setattr__(
            self,
            "content_hash",
            _content_hash("futures_margin_requirement_version", self.as_dict()),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": FUTURES_PATH_SCHEMA_VERSION,
            "record_id": self.record_id,
            "contract_id": self.contract_id,
            "currency": self.currency,
            "initial_margin_per_contract": decimal_text(
                self.initial_margin_per_contract
            ),
            "maintenance_margin_per_contract": decimal_text(
                self.maintenance_margin_per_contract
            ),
            "collateral_fraction": decimal_text(self.collateral_fraction),
            "variation_margin_enabled": self.variation_margin_enabled,
            "metadata": self.metadata.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class DeliverableTermsVersion:
    record_id: str
    contract_id: str
    grades: tuple[str, ...]
    delivery_locations: tuple[str, ...]
    grade_differentials: tuple[tuple[str, Decimal], ...]
    metadata: ReferenceMetadata
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.record_id, "deliverable_terms.record_id")
        _require_id(self.contract_id, "deliverable_terms.contract_id")
        grades = tuple(sorted(self.grades))
        locations = tuple(sorted(self.delivery_locations))
        if not grades or not locations:
            raise FuturesPathError("deliverable_grades_and_locations_required")
        if len(grades) != len(set(grades)) or len(locations) != len(set(locations)):
            raise FuturesPathError("deliverable_terms_duplicate")
        for value in (*grades, *locations):
            _require_id(value, "deliverable_terms.value")
        differentials = tuple(
            sorted(
                (
                    grade,
                    _decimal(value, "deliverable_terms.grade_differential"),
                )
                for grade, value in self.grade_differentials
            )
        )
        differential_grades = [grade for grade, _value in differentials]
        if len(differential_grades) != len(set(differential_grades)):
            raise FuturesPathError("deliverable_grade_differential_duplicate")
        if set(differential_grades) - set(grades):
            raise FuturesPathError("deliverable_grade_differential_orphan")
        object.__setattr__(self, "grades", grades)
        object.__setattr__(self, "delivery_locations", locations)
        object.__setattr__(self, "grade_differentials", differentials)
        object.__setattr__(
            self,
            "content_hash",
            _content_hash("futures_deliverable_terms_version", self.as_dict()),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": FUTURES_PATH_SCHEMA_VERSION,
            "record_id": self.record_id,
            "contract_id": self.contract_id,
            "grades": list(self.grades),
            "delivery_locations": list(self.delivery_locations),
            "grade_differentials": [
                {"grade": grade, "differential": decimal_text(value)}
                for grade, value in self.grade_differentials
            ],
            "metadata": self.metadata.as_dict(),
        }


class _ReferenceVersion(Protocol):
    @property
    def record_id(self) -> str: ...

    @property
    def contract_id(self) -> str: ...

    @property
    def metadata(self) -> ReferenceMetadata: ...

    @property
    def content_hash(self) -> str: ...


_ReferenceT = TypeVar("_ReferenceT", bound=_ReferenceVersion)


def _ordered_versions(records: Sequence[_ReferenceT]) -> tuple[_ReferenceT, ...]:
    return tuple(
        sorted(
            records,
            key=lambda item: (
                parse_timestamp(
                    item.metadata.effective_from,
                    "reference.effective_from",
                ),
                parse_timestamp(item.metadata.knowledge_at, "reference.knowledge_at"),
                item.content_hash,
            ),
        )
    )


def _validate_history(
    records: Sequence[_ReferenceVersion],
    *,
    contract_id: str,
    required: bool,
    field_name: str,
) -> None:
    if required and not records:
        raise FuturesPathError(f"{field_name}_history_required")
    record_ids = [item.record_id for item in records]
    if len(record_ids) != len(set(record_ids)):
        raise FuturesPathError(f"{field_name}_record_id_duplicate")
    if any(item.contract_id != contract_id for item in records):
        raise FuturesPathError(f"{field_name}_contract_mismatch")
    for index, left in enumerate(records):
        for right in records[index + 1 :]:
            if (
                left.metadata.knowledge_at == right.metadata.knowledge_at
                and left.metadata.overlaps(right.metadata)
            ):
                raise FuturesPathError(f"{field_name}_ambiguous_knowledge_version")


def _select_version(
    records: Sequence[_ReferenceT],
    *,
    valid_at: str,
    known_at: str,
    field_name: str,
) -> _ReferenceT:
    candidates = [
        item
        for item in records
        if item.metadata.effective_at(valid_at) and item.metadata.known_at(known_at)
    ]
    if not candidates:
        raise FuturesPathError(f"{field_name}_not_available_point_in_time")
    return max(
        candidates,
        key=lambda item: (
            parse_timestamp(item.metadata.knowledge_at, "reference.knowledge_at"),
            item.content_hash,
        ),
    )


@dataclass(frozen=True, slots=True)
class FuturesReferenceSnapshot:
    contract_id: str
    valid_at: str
    known_at: str
    specification: ContractSpecificationVersion
    margin: MarginRequirementVersion
    deliverable_terms: DeliverableTermsVersion | None
    history_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.contract_id, "futures_reference_snapshot.contract_id")
        object.__setattr__(
            self,
            "valid_at",
            _timestamp_text(self.valid_at, "futures_reference_snapshot.valid_at"),
        )
        object.__setattr__(
            self,
            "known_at",
            _timestamp_text(self.known_at, "futures_reference_snapshot.known_at"),
        )
        _require_hash(self.history_hash, "futures_reference_snapshot.history_hash")
        if self.specification.contract_id != self.contract_id:
            raise FuturesPathError("snapshot_specification_contract_mismatch")
        if self.margin.contract_id != self.contract_id:
            raise FuturesPathError("snapshot_margin_contract_mismatch")
        if self.specification.settlement_type is SettlementType.PHYSICAL_SETTLED:
            if self.deliverable_terms is None:
                raise FuturesPathError("physical_snapshot_deliverable_terms_required")
        elif self.deliverable_terms is not None:
            raise FuturesPathError("cash_snapshot_deliverable_terms_forbidden")
        object.__setattr__(
            self,
            "content_hash",
            _content_hash("futures_reference_snapshot", self.as_dict()),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": FUTURES_PATH_SCHEMA_VERSION,
            "contract_id": self.contract_id,
            "valid_at": self.valid_at,
            "known_at": self.known_at,
            "specification_hash": self.specification.content_hash,
            "margin_hash": self.margin.content_hash,
            "deliverable_terms_hash": (
                None
                if self.deliverable_terms is None
                else self.deliverable_terms.content_hash
            ),
            "history_hash": self.history_hash,
        }


@dataclass(frozen=True, slots=True)
class FuturesReferenceHistory:
    history_id: str
    contract_id: str
    specifications: tuple[ContractSpecificationVersion, ...]
    margins: tuple[MarginRequirementVersion, ...]
    deliverable_terms: tuple[DeliverableTermsVersion, ...] = ()
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.history_id, "futures_reference_history.history_id")
        _require_id(self.contract_id, "futures_reference_history.contract_id")
        specifications = _ordered_versions(self.specifications)
        margins = _ordered_versions(self.margins)
        deliverables = _ordered_versions(self.deliverable_terms)
        _validate_history(
            specifications,
            contract_id=self.contract_id,
            required=True,
            field_name="contract_specification",
        )
        _validate_history(
            margins,
            contract_id=self.contract_id,
            required=True,
            field_name="margin_requirement",
        )
        _validate_history(
            deliverables,
            contract_id=self.contract_id,
            required=False,
            field_name="deliverable_terms",
        )
        object.__setattr__(self, "specifications", specifications)
        object.__setattr__(self, "margins", margins)
        object.__setattr__(self, "deliverable_terms", deliverables)
        object.__setattr__(
            self,
            "content_hash",
            _content_hash("futures_reference_history", self.as_dict()),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": FUTURES_PATH_SCHEMA_VERSION,
            "history_id": self.history_id,
            "contract_id": self.contract_id,
            "specification_hashes": [item.content_hash for item in self.specifications],
            "margin_hashes": [item.content_hash for item in self.margins],
            "deliverable_terms_hashes": [
                item.content_hash for item in self.deliverable_terms
            ],
        }

    def as_of(self, *, valid_at: str, known_at: str) -> FuturesReferenceSnapshot:
        specification = _select_version(
            self.specifications,
            valid_at=valid_at,
            known_at=known_at,
            field_name="contract_specification",
        )
        margin = _select_version(
            self.margins,
            valid_at=valid_at,
            known_at=known_at,
            field_name="margin_requirement",
        )
        deliverable: DeliverableTermsVersion | None = None
        eligible_deliverables = [
            item
            for item in self.deliverable_terms
            if item.metadata.effective_at(valid_at) and item.metadata.known_at(known_at)
        ]
        if eligible_deliverables:
            deliverable = max(
                eligible_deliverables,
                key=lambda item: (
                    parse_timestamp(
                        item.metadata.knowledge_at,
                        "deliverable.knowledge_at",
                    ),
                    item.content_hash,
                ),
            )
        return FuturesReferenceSnapshot(
            contract_id=self.contract_id,
            valid_at=valid_at,
            known_at=known_at,
            specification=specification,
            margin=margin,
            deliverable_terms=deliverable,
            history_hash=self.content_hash,
        )


def adapt_existing_futures_contract(
    contract: FuturesContractProtocol,
    *,
    quote_currency: str,
    effective_to: str | None = None,
) -> ContractSpecificationVersion:
    """Bind an existing futures contract to a bitemporal specification row."""

    if not isinstance(contract, FuturesContractProtocol):
        raise FuturesPathError("existing_futures_contract_protocol_required")
    return ContractSpecificationVersion(
        record_id=f"{contract.contract_id}.spec.{contract.spec_version}",
        contract_id=contract.contract_id,
        root_id=contract.root_id,
        quote_currency=quote_currency,
        contract_multiplier=contract.contract_multiplier,
        tick_size=contract.tick_size,
        settlement_type=contract.settlement_type,
        last_trade_date=contract.last_trade_date,
        first_notice_date=contract.first_notice_date,
        expiration_date=contract.expiration_date,
        metadata=ReferenceMetadata(
            effective_from=contract.spec_effective_at,
            effective_to=effective_to,
            knowledge_at=contract.availability.processed_at,
            source_id="derivatives.futures.FuturesContract",
            source_version=contract.spec_version,
            source_hash=contract.content_hash,
        ),
    )


def adapt_existing_margin_policy(
    policy: MarginSimulationPolicy,
    *,
    contract_id: str,
    currency: str,
    effective_from: str,
    effective_to: str | None,
    knowledge_at: str,
) -> MarginRequirementVersion:
    """Bind the existing research-only margin policy to one contract period."""

    return MarginRequirementVersion(
        record_id=f"{contract_id}.margin.{policy.policy_version}",
        contract_id=contract_id,
        currency=currency,
        initial_margin_per_contract=policy.initial_margin_per_contract,
        maintenance_margin_per_contract=policy.maintenance_margin_per_contract,
        collateral_fraction=policy.collateral_fraction,
        variation_margin_enabled=policy.variation_margin_enabled,
        metadata=ReferenceMetadata(
            effective_from=effective_from,
            effective_to=effective_to,
            knowledge_at=knowledge_at,
            source_id="derivatives.futures.MarginSimulationPolicy",
            source_version=policy.policy_version,
            source_hash=policy.content_hash,
        ),
    )


class ExpiryBucket(StrEnum):
    DAYS_0_30 = "DAYS_0_30"
    DAYS_31_90 = "DAYS_31_90"
    DAYS_91_180 = "DAYS_91_180"
    DAYS_181_PLUS = "DAYS_181_PLUS"


def _expiry_bucket(days: int) -> ExpiryBucket:
    if days <= 30:
        return ExpiryBucket.DAYS_0_30
    if days <= 90:
        return ExpiryBucket.DAYS_31_90
    if days <= 180:
        return ExpiryBucket.DAYS_91_180
    return ExpiryBucket.DAYS_181_PLUS


@dataclass(frozen=True, slots=True)
class FuturesCurvePoint:
    contract_id: str
    expiration_date: str
    days_to_expiration: int
    price: Decimal
    basis: Decimal
    basis_ratio: Decimal
    contract_multiplier: Decimal
    quote_hash: str
    contract_hash: str

    def __post_init__(self) -> None:
        _require_id(self.contract_id, "futures_curve_point.contract_id")
        _date(self.expiration_date, "futures_curve_point.expiration_date")
        if self.days_to_expiration <= 0:
            raise FuturesPathError("futures_curve_point_already_expired")
        for name in ("price", "contract_multiplier"):
            object.__setattr__(
                self,
                name,
                _decimal(
                    getattr(self, name),
                    f"futures_curve_point.{name}",
                    positive=True,
                ),
            )
        for name in ("basis", "basis_ratio"):
            object.__setattr__(
                self,
                name,
                _decimal(getattr(self, name), f"futures_curve_point.{name}"),
            )
        _require_hash(self.quote_hash, "futures_curve_point.quote_hash")
        _require_hash(self.contract_hash, "futures_curve_point.contract_hash")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": FUTURES_PATH_SCHEMA_VERSION,
            "contract_id": self.contract_id,
            "expiration_date": self.expiration_date,
            "days_to_expiration": self.days_to_expiration,
            "expiry_bucket": _expiry_bucket(self.days_to_expiration).value,
            "price": decimal_text(self.price),
            "basis": decimal_text(self.basis),
            "basis_ratio": decimal_text(self.basis_ratio),
            "contract_multiplier": decimal_text(self.contract_multiplier),
            "quote_hash": self.quote_hash,
            "contract_hash": self.contract_hash,
        }


@dataclass(frozen=True, slots=True)
class ExpiryBucketFeature:
    bucket: ExpiryBucket
    contract_ids: tuple[str, ...]
    minimum_days: int
    maximum_days: int
    mean_price: Decimal
    mean_basis: Decimal

    def __post_init__(self) -> None:
        if not self.contract_ids or len(self.contract_ids) != len(
            set(self.contract_ids)
        ):
            raise FuturesPathError("expiry_bucket_contracts_invalid")
        if self.minimum_days <= 0 or self.maximum_days < self.minimum_days:
            raise FuturesPathError("expiry_bucket_day_range_invalid")
        object.__setattr__(
            self,
            "mean_price",
            _decimal(self.mean_price, "expiry_bucket.mean_price", positive=True),
        )
        object.__setattr__(
            self,
            "mean_basis",
            _decimal(self.mean_basis, "expiry_bucket.mean_basis"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": FUTURES_PATH_SCHEMA_VERSION,
            "bucket": self.bucket.value,
            "contract_ids": list(self.contract_ids),
            "minimum_days": self.minimum_days,
            "maximum_days": self.maximum_days,
            "mean_price": decimal_text(self.mean_price),
            "mean_basis": decimal_text(self.mean_basis),
        }


@dataclass(frozen=True, slots=True)
class FuturesCurveSnapshot:
    snapshot_id: str
    observed_at: str
    root_id: str
    spot_price: Decimal
    points: tuple[FuturesCurvePoint, ...]
    basis: Decimal
    basis_ratio: Decimal
    implied_annualized_carry: Decimal
    front_back_slope: Decimal
    curvature: Decimal | None
    annualized_roll_yield: Decimal
    expiry_buckets: tuple[ExpiryBucketFeature, ...]
    chain_snapshot_hash: str
    basis_feature_hash: str
    curve_feature_hash: str
    spot_source_hash: str
    feature_version: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for name, value in (
            ("snapshot_id", self.snapshot_id),
            ("root_id", self.root_id),
            ("feature_version", self.feature_version),
        ):
            _require_id(value, f"futures_curve_snapshot.{name}")
        object.__setattr__(
            self,
            "observed_at",
            _timestamp_text(self.observed_at, "futures_curve_snapshot.observed_at"),
        )
        if len(self.points) < 2:
            raise FuturesPathError("futures_curve_requires_two_contracts")
        expirations = [item.days_to_expiration for item in self.points]
        if expirations != sorted(expirations) or len(expirations) != len(
            set(expirations)
        ):
            raise FuturesPathError("futures_curve_expirations_not_strictly_ordered")
        object.__setattr__(
            self,
            "spot_price",
            _decimal(
                self.spot_price, "futures_curve_snapshot.spot_price", positive=True
            ),
        )
        for name in (
            "basis",
            "basis_ratio",
            "implied_annualized_carry",
            "front_back_slope",
            "annualized_roll_yield",
        ):
            object.__setattr__(
                self,
                name,
                _decimal(getattr(self, name), f"futures_curve_snapshot.{name}"),
            )
        if self.curvature is not None:
            object.__setattr__(
                self,
                "curvature",
                _decimal(self.curvature, "futures_curve_snapshot.curvature"),
            )
        for name, value in (
            ("chain_snapshot_hash", self.chain_snapshot_hash),
            ("basis_feature_hash", self.basis_feature_hash),
            ("curve_feature_hash", self.curve_feature_hash),
            ("spot_source_hash", self.spot_source_hash),
        ):
            _require_hash(value, f"futures_curve_snapshot.{name}")
        if self.annualized_roll_yield != -self.front_back_slope:
            raise FuturesPathError("futures_curve_roll_yield_slope_mismatch")
        object.__setattr__(
            self,
            "content_hash",
            _content_hash("futures_curve_snapshot", self.as_dict()),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": FUTURES_PATH_SCHEMA_VERSION,
            "snapshot_id": self.snapshot_id,
            "observed_at": self.observed_at,
            "root_id": self.root_id,
            "spot_price": decimal_text(self.spot_price),
            "points": [item.as_dict() for item in self.points],
            "basis": decimal_text(self.basis),
            "basis_ratio": decimal_text(self.basis_ratio),
            "implied_annualized_carry": decimal_text(self.implied_annualized_carry),
            "front_back_slope": decimal_text(self.front_back_slope),
            "curvature": (
                None if self.curvature is None else decimal_text(self.curvature)
            ),
            "annualized_roll_yield": decimal_text(self.annualized_roll_yield),
            "expiry_buckets": [item.as_dict() for item in self.expiry_buckets],
            "chain_snapshot_hash": self.chain_snapshot_hash,
            "basis_feature_hash": self.basis_feature_hash,
            "curve_feature_hash": self.curve_feature_hash,
            "spot_source_hash": self.spot_source_hash,
            "feature_version": self.feature_version,
        }


def build_futures_curve_snapshot(
    chain: ContractChainSnapshot,
    *,
    snapshot_id: str,
    feature_version: str,
    as_of: str,
    spot_price: Decimal,
    spot_availability: AvailabilityTimes,
    spot_source_hash: str,
    max_event_skew_seconds: int = 0,
) -> FuturesCurveSnapshot:
    """Build curve features using the existing PIT chain and feature semantics."""

    selected = select_chain_as_of((chain,), as_of)
    contracts = tuple(
        sorted(
            selected.listed_contracts(as_of),
            key=lambda item: (
                _date(item.expiration_date, "curve.expiration_date"),
                item.contract_id,
            ),
        )
    )
    if len(contracts) < 2:
        raise FuturesPathError("futures_curve_requires_two_tradable_contracts")
    quotes = tuple(selected.quote_for(item.contract_id, as_of) for item in contracts)
    basis_feature = compute_basis_feature(
        feature_id=f"{snapshot_id}.basis",
        feature_version=feature_version,
        as_of=as_of,
        spot_price=spot_price,
        spot_availability=spot_availability,
        futures_quote=quotes[0],
        contract=contracts[0],
        max_event_skew_seconds=max_event_skew_seconds,
    )
    curve_feature = compute_curve_feature(
        feature_id=f"{snapshot_id}.curve",
        feature_version=feature_version,
        as_of=as_of,
        near_quote=quotes[0],
        deferred_quote=quotes[1],
        near_contract=contracts[0],
        deferred_contract=contracts[1],
        third_quote=quotes[2] if len(quotes) > 2 else None,
    )
    instant_date = parse_timestamp(as_of, "curve.as_of").date()
    spot = _decimal(spot_price, "curve.spot_price", positive=True)
    points = tuple(
        FuturesCurvePoint(
            contract_id=contract.contract_id,
            expiration_date=contract.expiration_date,
            days_to_expiration=(
                _date(contract.expiration_date, "curve.expiration_date") - instant_date
            ).days,
            price=quote.close_price,
            basis=quote.close_price - spot,
            basis_ratio=(quote.close_price - spot) / spot,
            contract_multiplier=contract.contract_multiplier,
            quote_hash=quote.content_hash,
            contract_hash=contract.content_hash,
        )
        for contract, quote in zip(contracts, quotes, strict=True)
    )
    buckets = []
    for bucket in ExpiryBucket:
        members = tuple(
            point
            for point in points
            if _expiry_bucket(point.days_to_expiration) is bucket
        )
        if not members:
            continue
        count = Decimal(len(members))
        buckets.append(
            ExpiryBucketFeature(
                bucket=bucket,
                contract_ids=tuple(item.contract_id for item in members),
                minimum_days=min(item.days_to_expiration for item in members),
                maximum_days=max(item.days_to_expiration for item in members),
                mean_price=sum((item.price for item in members), _ZERO) / count,
                mean_basis=sum((item.basis for item in members), _ZERO) / count,
            )
        )
    _require_hash(spot_source_hash, "curve.spot_source_hash")
    return FuturesCurveSnapshot(
        snapshot_id=snapshot_id,
        observed_at=as_of,
        root_id=selected.root_id,
        spot_price=spot,
        points=points,
        basis=basis_feature.basis,
        basis_ratio=basis_feature.basis_ratio,
        implied_annualized_carry=basis_feature.annualized_basis,
        front_back_slope=curve_feature.annualized_slope,
        curvature=curve_feature.curvature,
        annualized_roll_yield=-curve_feature.annualized_slope,
        expiry_buckets=tuple(buckets),
        chain_snapshot_hash=selected.content_hash,
        basis_feature_hash=basis_feature.content_hash,
        curve_feature_hash=curve_feature.content_hash,
        spot_source_hash=spot_source_hash,
        feature_version=feature_version,
    )


@dataclass(frozen=True, slots=True)
class ContinuousSignalMapping:
    point_id: str
    observed_at: str
    source_contract_id: str
    source_quote_hash: str
    source_price: Decimal
    continuous_price: Decimal
    point_hash: str
    previous_point_hash: str | None

    def __post_init__(self) -> None:
        _require_id(self.point_id, "continuous_mapping.point_id")
        _require_id(self.source_contract_id, "continuous_mapping.source_contract_id")
        object.__setattr__(
            self,
            "observed_at",
            _timestamp_text(self.observed_at, "continuous_mapping.observed_at"),
        )
        _require_hash(self.source_quote_hash, "continuous_mapping.source_quote_hash")
        _require_hash(self.point_hash, "continuous_mapping.point_hash")
        if self.previous_point_hash is not None:
            _require_hash(
                self.previous_point_hash,
                "continuous_mapping.previous_point_hash",
            )
        for name in ("source_price", "continuous_price"):
            object.__setattr__(
                self,
                name,
                _decimal(
                    getattr(self, name),
                    f"continuous_mapping.{name}",
                    positive=True,
                ),
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": FUTURES_PATH_SCHEMA_VERSION,
            "point_id": self.point_id,
            "observed_at": self.observed_at,
            "source_contract_id": self.source_contract_id,
            "source_quote_hash": self.source_quote_hash,
            "source_price": decimal_text(self.source_price),
            "continuous_price": decimal_text(self.continuous_price),
            "point_hash": self.point_hash,
            "previous_point_hash": self.previous_point_hash,
        }


@dataclass(frozen=True, slots=True)
class ContinuousSignalTrace:
    trace_id: str
    series_id: str
    root_id: str
    mappings: tuple[ContinuousSignalMapping, ...]
    signal_only: bool = True
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for name, value in (
            ("trace_id", self.trace_id),
            ("series_id", self.series_id),
            ("root_id", self.root_id),
        ):
            _require_id(value, f"continuous_trace.{name}")
        if not self.signal_only:
            raise FuturesPathError("continuous_trace_must_be_signal_only")
        if not self.mappings:
            raise FuturesPathError("continuous_trace_mappings_required")
        point_ids = [item.point_id for item in self.mappings]
        if len(point_ids) != len(set(point_ids)):
            raise FuturesPathError("continuous_trace_point_duplicate")
        for index, mapping in enumerate(self.mappings):
            if index == 0:
                if mapping.previous_point_hash is not None:
                    raise FuturesPathError("continuous_trace_must_start_at_origin")
                continue
            previous = self.mappings[index - 1]
            if mapping.previous_point_hash != previous.point_hash:
                raise FuturesPathError("continuous_trace_hash_chain_broken")
            if parse_timestamp(
                mapping.observed_at, "continuous_mapping.observed_at"
            ) <= parse_timestamp(
                previous.observed_at, "continuous_mapping.previous_observed_at"
            ):
                raise FuturesPathError("continuous_trace_not_append_only")
        object.__setattr__(
            self,
            "content_hash",
            _content_hash("continuous_signal_trace", self.as_dict()),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": FUTURES_PATH_SCHEMA_VERSION,
            "trace_id": self.trace_id,
            "series_id": self.series_id,
            "root_id": self.root_id,
            "mappings": [item.as_dict() for item in self.mappings],
            "signal_only": self.signal_only,
        }

    def source_contract_for(self, point_id: str) -> str:
        mapping = next(
            (item for item in self.mappings if item.point_id == point_id),
            None,
        )
        if mapping is None:
            raise FuturesPathError("continuous_trace_point_not_found")
        return mapping.source_contract_id

    def require_executable_contract(self, identifier: str) -> str:
        """Reject the derived series and return only an actual mapped contract."""

        if identifier == self.series_id or identifier in {
            item.point_id for item in self.mappings
        }:
            raise FuturesPathError("continuous_signal_identifier_not_executable")
        if identifier not in {item.source_contract_id for item in self.mappings}:
            raise FuturesPathError("contract_not_bound_to_continuous_trace")
        return identifier


def trace_continuous_signal(
    points: Sequence[ContinuousPointProtocol],
    *,
    trace_id: str,
) -> ContinuousSignalTrace:
    """Preserve every existing continuous point's actual source-contract edge."""

    if not points:
        raise FuturesPathError("continuous_trace_points_required")
    if any(not isinstance(item, ContinuousPointProtocol) for item in points):
        raise FuturesPathError("continuous_point_protocol_required")
    if any(not item.signal_only for item in points):
        raise FuturesPathError("continuous_point_must_remain_signal_only")
    series_ids = {item.series_id for item in points}
    root_ids = {item.root_id for item in points}
    if len(series_ids) != 1 or len(root_ids) != 1:
        raise FuturesPathError("continuous_trace_series_or_root_mismatch")
    return ContinuousSignalTrace(
        trace_id=trace_id,
        series_id=points[0].series_id,
        root_id=points[0].root_id,
        mappings=tuple(
            ContinuousSignalMapping(
                point_id=item.point_id,
                observed_at=item.observed_at,
                source_contract_id=item.source_contract_id,
                source_quote_hash=item.source_quote_hash,
                source_price=item.source_price,
                continuous_price=item.continuous_price,
                point_hash=item.content_hash,
                previous_point_hash=item.previous_point_hash,
            )
            for item in points
        ),
    )


@dataclass(frozen=True, slots=True)
class RollPlanningPolicy:
    policy_id: str
    policy_version: str
    split_fractions: tuple[Decimal, ...] = ()
    fixed_maturity_days: int | None = None
    fixed_maturity_tolerance_days: int = 0
    minimum_days_to_notice: int = 5
    minimum_days_to_expiration: int = 5
    minimum_days_to_last_trade: int = 1
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.policy_id, "roll_planning_policy.policy_id")
        _require_id(self.policy_version, "roll_planning_policy.policy_version")
        fractions = tuple(
            _decimal(
                item,
                "roll_planning_policy.split_fraction",
                positive=True,
            )
            for item in self.split_fractions
        )
        if fractions and sum(fractions, _ZERO) != _ONE:
            raise FuturesPathError("split_roll_fractions_must_sum_to_one")
        if any(item > _ONE for item in fractions):
            raise FuturesPathError("split_roll_fraction_exceeds_one")
        if self.fixed_maturity_days is not None and self.fixed_maturity_days <= 0:
            raise FuturesPathError("fixed_maturity_days_must_be_positive")
        for name in (
            "fixed_maturity_tolerance_days",
            "minimum_days_to_notice",
            "minimum_days_to_expiration",
            "minimum_days_to_last_trade",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise FuturesPathError(f"roll_planning_policy_{name}_invalid")
        object.__setattr__(self, "split_fractions", fractions)
        object.__setattr__(
            self,
            "content_hash",
            _content_hash("futures_roll_planning_policy", self.as_dict()),
        )

    @property
    def is_split_roll(self) -> bool:
        return bool(self.split_fractions)

    def fraction_for(self, tranche_index: int) -> Decimal:
        if isinstance(tranche_index, bool) or tranche_index < 0:
            raise FuturesPathError("split_roll_tranche_index_invalid")
        if not self.split_fractions:
            if tranche_index != 0:
                raise FuturesPathError("full_roll_has_one_tranche")
            return _ONE
        if tranche_index >= len(self.split_fractions):
            raise FuturesPathError("split_roll_tranche_index_out_of_range")
        return self.split_fractions[tranche_index]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": FUTURES_PATH_SCHEMA_VERSION,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "split_fractions": [decimal_text(item) for item in self.split_fractions],
            "fixed_maturity_days": self.fixed_maturity_days,
            "fixed_maturity_tolerance_days": self.fixed_maturity_tolerance_days,
            "minimum_days_to_notice": self.minimum_days_to_notice,
            "minimum_days_to_expiration": self.minimum_days_to_expiration,
            "minimum_days_to_last_trade": self.minimum_days_to_last_trade,
        }


_ContractT = TypeVar("_ContractT", bound=FuturesContractProtocol)


def _days_until(value: str, as_of: str, field_name: str) -> int:
    return (_date(value, field_name) - parse_timestamp(as_of, "roll.as_of").date()).days


def _target_is_safe(
    contract: FuturesContractProtocol,
    *,
    as_of: str,
    policy: RollPlanningPolicy,
) -> bool:
    if not contract.tradable_at(as_of):
        return False
    if (
        _days_until(contract.expiration_date, as_of, "roll.expiration_date")
        < policy.minimum_days_to_expiration
    ):
        return False
    if (
        _days_until(contract.last_trade_date, as_of, "roll.last_trade_date")
        < policy.minimum_days_to_last_trade
    ):
        return False
    if contract.first_notice_date is not None and (
        _days_until(contract.first_notice_date, as_of, "roll.first_notice_date")
        < policy.minimum_days_to_notice
    ):
        return False
    days = _days_until(contract.expiration_date, as_of, "roll.expiration_date")
    if policy.fixed_maturity_days is not None and (
        abs(days - policy.fixed_maturity_days) > policy.fixed_maturity_tolerance_days
    ):
        return False
    return True


def select_roll_target(
    current_contract: _ContractT,
    candidates: Sequence[_ContractT],
    *,
    as_of: str,
    policy: RollPlanningPolicy,
) -> _ContractT:
    """Select a known, tradable target without notice/expiry look-ahead risk."""

    eligible = [
        item
        for item in candidates
        if item.contract_id != current_contract.contract_id
        and item.root_id == current_contract.root_id
        and _target_is_safe(item, as_of=as_of, policy=policy)
    ]
    if not eligible:
        raise FuturesPathError("no_safe_roll_target")
    target_days = policy.fixed_maturity_days
    return min(
        eligible,
        key=lambda item: (
            0
            if target_days is None
            else abs(
                _days_until(item.expiration_date, as_of, "roll.expiration_date")
                - target_days
            ),
            _date(item.expiration_date, "roll.expiration_date"),
            item.contract_id,
        ),
    )


@dataclass(frozen=True, slots=True)
class RollLegCost:
    expected_fill_price: Decimal
    commission: Decimal
    slippage_cost: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "expected_fill_price",
            _decimal(
                self.expected_fill_price,
                "roll_leg_cost.expected_fill_price",
                positive=True,
            ),
        )
        for name in ("commission", "slippage_cost"):
            object.__setattr__(
                self,
                name,
                _decimal(
                    getattr(self, name),
                    f"roll_leg_cost.{name}",
                    nonnegative=True,
                ),
            )

    @property
    def total(self) -> Decimal:
        return self.commission + self.slippage_cost

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": FUTURES_PATH_SCHEMA_VERSION,
            "expected_fill_price": decimal_text(self.expected_fill_price),
            "commission": decimal_text(self.commission),
            "slippage_cost": decimal_text(self.slippage_cost),
            "total": decimal_text(self.total),
        }


class RollCostModelProtocol(Protocol):
    @property
    def content_hash(self) -> str: ...

    def estimate_roll_leg(
        self,
        *,
        contract: FuturesContractProtocol,
        side: OrderSide,
        quantity: int,
        reference_price: Decimal,
    ) -> RollLegCost: ...


@dataclass(frozen=True, slots=True)
class ExistingFuturesCostPolicyAdapter:
    """Use the existing simulator's commission and tick-slippage formula."""

    policy: FuturesCostPolicy

    @property
    def content_hash(self) -> str:
        return self.policy.content_hash

    def estimate_roll_leg(
        self,
        *,
        contract: FuturesContractProtocol,
        side: OrderSide,
        quantity: int,
        reference_price: Decimal,
    ) -> RollLegCost:
        if quantity <= 0:
            raise FuturesPathError("roll_leg_quantity_must_be_positive")
        reference = _decimal(reference_price, "roll_leg.reference_price", positive=True)
        ticks = self.policy.execution_slippage_ticks + self.policy.roll_slippage_ticks
        adverse = ticks * contract.tick_size
        unrounded = (
            reference + adverse if side is OrderSide.BUY else reference - adverse
        )
        rounding = ROUND_CEILING if side is OrderSide.BUY else ROUND_FLOOR
        fill_price = (unrounded / contract.tick_size).to_integral_value(
            rounding=rounding
        ) * contract.tick_size
        if fill_price <= 0:
            raise FuturesPathError("roll_leg_expected_fill_not_positive")
        return RollLegCost(
            expected_fill_price=fill_price,
            commission=self.policy.commission_per_contract * quantity,
            slippage_cost=(
                abs(fill_price - reference) * contract.contract_multiplier * quantity
            ),
        )


class RollLegRole(StrEnum):
    CLOSE_OLD = "CLOSE_OLD"
    OPEN_NEW = "OPEN_NEW"


@dataclass(frozen=True, slots=True)
class PlannedRollLeg:
    role: RollLegRole
    contract_id: str
    side: OrderSide
    quantity: int
    reference_price: Decimal
    multiplier: Decimal
    exposure_delta: Decimal
    cost: RollLegCost

    def __post_init__(self) -> None:
        _require_id(self.contract_id, "planned_roll_leg.contract_id")
        if self.quantity <= 0:
            raise FuturesPathError("planned_roll_leg_quantity_invalid")
        for name in ("reference_price", "multiplier"):
            object.__setattr__(
                self,
                name,
                _decimal(
                    getattr(self, name),
                    f"planned_roll_leg.{name}",
                    positive=True,
                ),
            )
        object.__setattr__(
            self,
            "exposure_delta",
            _decimal(self.exposure_delta, "planned_roll_leg.exposure_delta"),
        )
        expected_sign = _ONE if self.side is OrderSide.BUY else -_ONE
        expected_delta = (
            expected_sign * self.quantity * self.reference_price * self.multiplier
        )
        if self.exposure_delta != expected_delta:
            raise FuturesPathError("planned_roll_leg_exposure_delta_mismatch")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": FUTURES_PATH_SCHEMA_VERSION,
            "role": self.role.value,
            "contract_id": self.contract_id,
            "side": self.side.value,
            "quantity": self.quantity,
            "reference_price": decimal_text(self.reference_price),
            "multiplier": decimal_text(self.multiplier),
            "exposure_delta": decimal_text(self.exposure_delta),
            "cost": self.cost.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ExposurePreservingRollPlan:
    plan_id: str
    planned_at: str
    old_contract_id: str
    new_contract_id: str
    old_contract_hash: str
    new_contract_hash: str
    old_quote_hash: str
    new_quote_hash: str
    old_price: Decimal
    new_price: Decimal
    old_multiplier: Decimal
    new_multiplier: Decimal
    original_old_quantity: int
    current_old_quantity: int
    existing_new_quantity: int
    remaining_old_quantity: int
    resulting_new_quantity: int
    target_exposure: Decimal
    achieved_exposure: Decimal
    rounding_residual: Decimal
    tranche_index: int
    tranche_fraction: Decimal
    legs: tuple[PlannedRollLeg, PlannedRollLeg]
    policy_hash: str
    cost_model_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.plan_id, "roll_plan.plan_id")
        _require_id(self.old_contract_id, "roll_plan.old_contract_id")
        _require_id(self.new_contract_id, "roll_plan.new_contract_id")
        if self.old_contract_id == self.new_contract_id:
            raise FuturesPathError("roll_plan_contracts_must_differ")
        object.__setattr__(
            self,
            "planned_at",
            _timestamp_text(self.planned_at, "roll_plan.planned_at"),
        )
        for name in (
            "old_contract_hash",
            "new_contract_hash",
            "old_quote_hash",
            "new_quote_hash",
            "policy_hash",
            "cost_model_hash",
        ):
            _require_hash(getattr(self, name), f"roll_plan.{name}")
        for name in (
            "old_price",
            "new_price",
            "old_multiplier",
            "new_multiplier",
        ):
            object.__setattr__(
                self,
                name,
                _decimal(getattr(self, name), f"roll_plan.{name}", positive=True),
            )
        for name in ("target_exposure", "achieved_exposure", "rounding_residual"):
            object.__setattr__(
                self,
                name,
                _decimal(getattr(self, name), f"roll_plan.{name}"),
            )
        fraction = _decimal(
            self.tranche_fraction,
            "roll_plan.tranche_fraction",
            positive=True,
        )
        if fraction > _ONE:
            raise FuturesPathError("roll_plan_tranche_fraction_exceeds_one")
        object.__setattr__(self, "tranche_fraction", fraction)
        for name in (
            "original_old_quantity",
            "current_old_quantity",
            "existing_new_quantity",
            "remaining_old_quantity",
            "resulting_new_quantity",
            "tranche_index",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise FuturesPathError(f"roll_plan_{name}_invalid")
        if self.tranche_index < 0:
            raise FuturesPathError("roll_plan_tranche_index_invalid")
        if self.current_old_quantity == 0 or self.original_old_quantity == 0:
            raise FuturesPathError("roll_plan_old_quantity_required")
        if (self.original_old_quantity > 0) != (self.current_old_quantity > 0) or abs(
            self.original_old_quantity
        ) < abs(self.current_old_quantity):
            raise FuturesPathError("roll_plan_original_old_quantity_inconsistent")
        if self.target_exposure == 0 or (self.target_exposure > 0) != (
            self.current_old_quantity > 0
        ):
            raise FuturesPathError("roll_plan_target_direction_mismatch")
        if len(self.legs) != 2:
            raise FuturesPathError("roll_plan_requires_two_legs")
        close_leg, open_leg = self.legs
        if close_leg.role is not RollLegRole.CLOSE_OLD:
            raise FuturesPathError("roll_plan_first_leg_must_close_old")
        if open_leg.role is not RollLegRole.OPEN_NEW:
            raise FuturesPathError("roll_plan_second_leg_must_open_new")
        if close_leg.contract_id != self.old_contract_id:
            raise FuturesPathError("roll_plan_close_contract_mismatch")
        if open_leg.contract_id != self.new_contract_id:
            raise FuturesPathError("roll_plan_open_contract_mismatch")
        if (
            close_leg.reference_price != self.old_price
            or open_leg.reference_price != self.new_price
        ):
            raise FuturesPathError("roll_plan_leg_reference_price_mismatch")
        if (
            close_leg.multiplier != self.old_multiplier
            or open_leg.multiplier != self.new_multiplier
        ):
            raise FuturesPathError("roll_plan_leg_multiplier_mismatch")
        old_direction = 1 if self.current_old_quantity > 0 else -1
        expected_close_side = OrderSide.SELL if old_direction > 0 else OrderSide.BUY
        if close_leg.side is not expected_close_side:
            raise FuturesPathError("roll_plan_close_side_mismatch")
        if self.remaining_old_quantity != (
            self.current_old_quantity - old_direction * close_leg.quantity
        ):
            raise FuturesPathError("roll_plan_remaining_old_quantity_mismatch")
        new_trade_direction = 1 if open_leg.side is OrderSide.BUY else -1
        target_direction = 1 if self.target_exposure > 0 else -1
        if new_trade_direction != target_direction:
            raise FuturesPathError("roll_plan_open_side_mismatch")
        if self.resulting_new_quantity != (
            self.existing_new_quantity + new_trade_direction * open_leg.quantity
        ):
            raise FuturesPathError("roll_plan_resulting_new_quantity_mismatch")
        expected_achieved_exposure = (
            Decimal(self.remaining_old_quantity) * self.old_price * self.old_multiplier
            + Decimal(self.resulting_new_quantity)
            * self.new_price
            * self.new_multiplier
        )
        if self.achieved_exposure != expected_achieved_exposure:
            raise FuturesPathError("roll_plan_achieved_exposure_mismatch")
        if self.rounding_residual != self.target_exposure - self.achieved_exposure:
            raise FuturesPathError("roll_plan_rounding_residual_mismatch")
        object.__setattr__(
            self,
            "content_hash",
            _content_hash("exposure_preserving_futures_roll_plan", self.as_dict()),
        )

    @property
    def total_cost(self) -> Decimal:
        return sum((item.cost.total for item in self.legs), _ZERO)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": FUTURES_PATH_SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "planned_at": self.planned_at,
            "old_contract_id": self.old_contract_id,
            "new_contract_id": self.new_contract_id,
            "old_contract_hash": self.old_contract_hash,
            "new_contract_hash": self.new_contract_hash,
            "old_quote_hash": self.old_quote_hash,
            "new_quote_hash": self.new_quote_hash,
            "old_price": decimal_text(self.old_price),
            "new_price": decimal_text(self.new_price),
            "old_multiplier": decimal_text(self.old_multiplier),
            "new_multiplier": decimal_text(self.new_multiplier),
            "original_old_quantity": self.original_old_quantity,
            "current_old_quantity": self.current_old_quantity,
            "existing_new_quantity": self.existing_new_quantity,
            "remaining_old_quantity": self.remaining_old_quantity,
            "resulting_new_quantity": self.resulting_new_quantity,
            "target_exposure": decimal_text(self.target_exposure),
            "achieved_exposure": decimal_text(self.achieved_exposure),
            "rounding_residual": decimal_text(self.rounding_residual),
            "tranche_index": self.tranche_index,
            "tranche_fraction": decimal_text(self.tranche_fraction),
            "legs": [item.as_dict() for item in self.legs],
            "policy_hash": self.policy_hash,
            "cost_model_hash": self.cost_model_hash,
            "total_cost": decimal_text(self.total_cost),
        }


def plan_exposure_preserving_roll(
    *,
    plan_id: str,
    as_of: str,
    old_contract: FuturesContractProtocol,
    new_contract: FuturesContractProtocol,
    old_quote: ContractQuote,
    new_quote: ContractQuote,
    current_old_quantity: int,
    target_exposure: Decimal,
    policy: RollPlanningPolicy,
    cost_model: RollCostModelProtocol,
    tranche_index: int = 0,
    original_old_quantity: int | None = None,
    existing_new_quantity: int = 0,
) -> ExposurePreservingRollPlan:
    """Plan two real roll legs while preserving signed economic exposure."""

    if not isinstance(old_contract, FuturesContractProtocol) or not isinstance(
        new_contract, FuturesContractProtocol
    ):
        raise FuturesPathError("futures_contract_protocol_required")
    if old_contract.contract_id == new_contract.contract_id:
        raise FuturesPathError("roll_requires_distinct_actual_contracts")
    if old_contract.root_id != new_contract.root_id:
        raise FuturesPathError("roll_contract_root_mismatch")
    if (
        old_quote.contract_id != old_contract.contract_id
        or new_quote.contract_id != new_contract.contract_id
    ):
        raise FuturesPathError("roll_quote_contract_mismatch")
    if not old_quote.known_at(as_of) or not new_quote.known_at(as_of):
        raise FuturesPathError("roll_quote_not_known_point_in_time")
    if not old_contract.tradable_at(as_of):
        raise FuturesPathError("roll_source_not_tradable")
    if not _target_is_safe(new_contract, as_of=as_of, policy=policy):
        raise FuturesPathError("roll_target_violates_notice_expiry_policy")
    if current_old_quantity == 0:
        raise FuturesPathError("roll_current_old_quantity_required")
    target = _decimal(target_exposure, "roll.target_exposure")
    if target == 0 or (target > 0) != (current_old_quantity > 0):
        raise FuturesPathError("roll_target_direction_mismatch")
    original = (
        current_old_quantity if original_old_quantity is None else original_old_quantity
    )
    if (
        original == 0
        or (original > 0) != (current_old_quantity > 0)
        or abs(original) < abs(current_old_quantity)
    ):
        raise FuturesPathError("roll_original_quantity_inconsistent")
    if existing_new_quantity and (existing_new_quantity > 0) != (target > 0):
        raise FuturesPathError("roll_existing_new_direction_mismatch")
    fraction = policy.fraction_for(tranche_index)
    final_tranche = (
        not policy.split_fractions or tranche_index == len(policy.split_fractions) - 1
    )
    scheduled = int(
        (Decimal(abs(original)) * fraction).to_integral_value(rounding=ROUND_HALF_UP)
    )
    close_quantity = (
        abs(current_old_quantity)
        if final_tranche
        else min(abs(current_old_quantity), scheduled)
    )
    if close_quantity <= 0:
        raise FuturesPathError("split_roll_tranche_rounds_to_zero")
    direction = 1 if current_old_quantity > 0 else -1
    remaining_old = current_old_quantity - direction * close_quantity
    old_unit_exposure = old_quote.close_price * old_contract.contract_multiplier
    new_unit_exposure = new_quote.close_price * new_contract.contract_multiplier
    residual_target_for_new = target - Decimal(remaining_old) * old_unit_exposure
    desired_new_quantity = int(
        (residual_target_for_new / new_unit_exposure).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    if desired_new_quantity == 0 or (desired_new_quantity > 0) != (target > 0):
        raise FuturesPathError("roll_new_contract_count_invalid")
    new_trade_quantity = desired_new_quantity - existing_new_quantity
    if new_trade_quantity == 0:
        raise FuturesPathError("roll_requires_two_nonzero_legs")
    if (new_trade_quantity > 0) != (target > 0):
        raise FuturesPathError("roll_new_leg_would_reduce_existing_target")
    close_side = OrderSide.SELL if current_old_quantity > 0 else OrderSide.BUY
    open_side = OrderSide.BUY if new_trade_quantity > 0 else OrderSide.SELL
    close_cost = cost_model.estimate_roll_leg(
        contract=old_contract,
        side=close_side,
        quantity=close_quantity,
        reference_price=old_quote.close_price,
    )
    open_cost = cost_model.estimate_roll_leg(
        contract=new_contract,
        side=open_side,
        quantity=abs(new_trade_quantity),
        reference_price=new_quote.close_price,
    )
    close_delta = Decimal(-direction * close_quantity) * old_unit_exposure
    open_delta = Decimal(new_trade_quantity) * new_unit_exposure
    achieved = (
        Decimal(remaining_old) * old_unit_exposure
        + Decimal(desired_new_quantity) * new_unit_exposure
    )
    _require_hash(cost_model.content_hash, "roll.cost_model_hash")
    return ExposurePreservingRollPlan(
        plan_id=plan_id,
        planned_at=as_of,
        old_contract_id=old_contract.contract_id,
        new_contract_id=new_contract.contract_id,
        old_contract_hash=old_contract.content_hash,
        new_contract_hash=new_contract.content_hash,
        old_quote_hash=old_quote.content_hash,
        new_quote_hash=new_quote.content_hash,
        old_price=old_quote.close_price,
        new_price=new_quote.close_price,
        old_multiplier=old_contract.contract_multiplier,
        new_multiplier=new_contract.contract_multiplier,
        original_old_quantity=original,
        current_old_quantity=current_old_quantity,
        existing_new_quantity=existing_new_quantity,
        remaining_old_quantity=remaining_old,
        resulting_new_quantity=desired_new_quantity,
        target_exposure=target,
        achieved_exposure=achieved,
        rounding_residual=target - achieved,
        tranche_index=tranche_index,
        tranche_fraction=fraction,
        legs=(
            PlannedRollLeg(
                role=RollLegRole.CLOSE_OLD,
                contract_id=old_contract.contract_id,
                side=close_side,
                quantity=close_quantity,
                reference_price=old_quote.close_price,
                multiplier=old_contract.contract_multiplier,
                exposure_delta=close_delta,
                cost=close_cost,
            ),
            PlannedRollLeg(
                role=RollLegRole.OPEN_NEW,
                contract_id=new_contract.contract_id,
                side=open_side,
                quantity=abs(new_trade_quantity),
                reference_price=new_quote.close_price,
                multiplier=new_contract.contract_multiplier,
                exposure_delta=open_delta,
                cost=open_cost,
            ),
        ),
        policy_hash=policy.content_hash,
        cost_model_hash=cost_model.content_hash,
    )


@dataclass(frozen=True, slots=True)
class FuturesPnlReconciliationEvidence:
    evidence_id: str
    observed_at: str
    opening_cash: Decimal
    closing_cash: Decimal
    settlement_pnl: Decimal
    roll_trade_pnl: Decimal
    roll_cost: Decimal
    roll_yield_attribution: Decimal
    expected_cash_delta: Decimal
    actual_cash_delta: Decimal
    residual: Decimal
    tolerance: Decimal
    settlement_event_hashes: tuple[str, ...]
    roll_execution_hash: str
    roll_fill_hashes: tuple[str, str]
    roll_plan_hash: str
    reconciled: bool = field(init=False)
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.evidence_id, "futures_pnl_evidence.evidence_id")
        object.__setattr__(
            self,
            "observed_at",
            _timestamp_text(self.observed_at, "futures_pnl_evidence.observed_at"),
        )
        for name in (
            "opening_cash",
            "closing_cash",
            "settlement_pnl",
            "roll_trade_pnl",
            "roll_cost",
            "roll_yield_attribution",
            "expected_cash_delta",
            "actual_cash_delta",
            "residual",
            "tolerance",
        ):
            object.__setattr__(
                self,
                name,
                _decimal(
                    getattr(self, name),
                    f"futures_pnl_evidence.{name}",
                    nonnegative=name in {"roll_cost", "tolerance"},
                ),
            )
        if not self.settlement_event_hashes:
            raise FuturesPathError("settlement_reconciliation_event_required")
        for value in (
            *self.settlement_event_hashes,
            self.roll_execution_hash,
            *self.roll_fill_hashes,
            self.roll_plan_hash,
        ):
            _require_hash(value, "futures_pnl_evidence.evidence_hash")
        if self.expected_cash_delta != (
            self.settlement_pnl + self.roll_trade_pnl - self.roll_cost
        ):
            raise FuturesPathError("futures_pnl_expected_delta_mismatch")
        if self.actual_cash_delta != self.closing_cash - self.opening_cash:
            raise FuturesPathError("futures_pnl_actual_delta_mismatch")
        if self.residual != self.actual_cash_delta - self.expected_cash_delta:
            raise FuturesPathError("futures_pnl_residual_mismatch")
        object.__setattr__(self, "reconciled", abs(self.residual) <= self.tolerance)
        object.__setattr__(
            self,
            "content_hash",
            _content_hash("futures_pnl_reconciliation_evidence", self.as_dict()),
        )

    def require_reconciled(self) -> None:
        if not self.reconciled:
            raise FuturesPathError("futures_pnl_not_reconciled")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": FUTURES_PATH_SCHEMA_VERSION,
            "evidence_id": self.evidence_id,
            "observed_at": self.observed_at,
            "opening_cash": decimal_text(self.opening_cash),
            "closing_cash": decimal_text(self.closing_cash),
            "settlement_pnl": decimal_text(self.settlement_pnl),
            "roll_trade_pnl": decimal_text(self.roll_trade_pnl),
            "roll_cost": decimal_text(self.roll_cost),
            "roll_yield_attribution": decimal_text(self.roll_yield_attribution),
            "expected_cash_delta": decimal_text(self.expected_cash_delta),
            "actual_cash_delta": decimal_text(self.actual_cash_delta),
            "residual": decimal_text(self.residual),
            "tolerance": decimal_text(self.tolerance),
            "settlement_event_hashes": list(self.settlement_event_hashes),
            "roll_execution_hash": self.roll_execution_hash,
            "roll_fill_hashes": list(self.roll_fill_hashes),
            "roll_plan_hash": self.roll_plan_hash,
            "reconciled": self.reconciled,
        }


def reconcile_existing_futures_pnl(
    *,
    evidence_id: str,
    observed_at: str,
    opening_cash: Decimal,
    closing_cash: Decimal,
    settlement_events: Sequence[SettlementEvent],
    roll_execution: RollExecution,
    roll_fills: Sequence[FuturesFill],
    roll_plan: ExposurePreservingRollPlan,
    tolerance: Decimal = _ZERO,
) -> FuturesPnlReconciliationEvidence:
    """Reconcile actual settlement and roll events without conflating roll yield."""

    if not settlement_events:
        raise FuturesPathError("settlement_reconciliation_event_required")
    observed = parse_timestamp(observed_at, "reconciliation.observed_at")
    planned = parse_timestamp(roll_plan.planned_at, "roll_plan.planned_at")
    executed = parse_timestamp(
        roll_execution.executed_at,
        "roll_execution.executed_at",
    )
    if not planned <= executed <= observed:
        raise FuturesPathError("roll_reconciliation_timeline_invalid")
    if roll_execution.decision_hash != roll_plan.content_hash:
        raise FuturesPathError("roll_execution_plan_hash_unbound")
    if len(roll_fills) != 2:
        raise FuturesPathError("roll_reconciliation_requires_two_fills")
    by_hash = {item.content_hash: item for item in roll_fills}
    if len(by_hash) != 2:
        raise FuturesPathError("roll_reconciliation_fill_duplicate")
    try:
        close_fill = by_hash[roll_execution.close_fill_hash]
        open_fill = by_hash[roll_execution.open_fill_hash]
    except KeyError as exc:
        raise FuturesPathError("roll_execution_fill_hash_unbound") from exc
    close_leg, open_leg = roll_plan.legs
    for fill, leg in ((close_fill, close_leg), (open_fill, open_leg)):
        if not fill.is_roll_leg:
            raise FuturesPathError("roll_reconciliation_non_roll_fill")
        if (
            fill.contract_id != leg.contract_id
            or fill.side is not leg.side
            or fill.quantity != leg.quantity
        ):
            raise FuturesPathError("roll_plan_fill_mismatch")
        if fill.intent_hash != roll_plan.content_hash:
            raise FuturesPathError("roll_plan_fill_intent_unbound")
        if parse_timestamp(fill.filled_at, "futures_fill.filled_at") != executed:
            raise FuturesPathError("roll_plan_fill_time_mismatch")
        expected_quote_hash = (
            roll_plan.old_quote_hash
            if leg.role is RollLegRole.CLOSE_OLD
            else roll_plan.new_quote_hash
        )
        if fill.quote_hash != expected_quote_hash:
            raise FuturesPathError("roll_plan_fill_quote_mismatch")
        if fill.fill_price != leg.cost.expected_fill_price:
            raise FuturesPathError("roll_plan_fill_price_mismatch")
        if fill.reference_price != leg.reference_price:
            raise FuturesPathError("roll_plan_fill_reference_price_mismatch")
        if fill.multiplier != leg.multiplier:
            raise FuturesPathError("roll_plan_fill_multiplier_mismatch")
        if (
            fill.commission != leg.cost.commission
            or fill.slippage_cost != leg.cost.slippage_cost
        ):
            raise FuturesPathError("roll_plan_fill_cost_mismatch")
    if (
        roll_execution.from_contract_id != roll_plan.old_contract_id
        or roll_execution.to_contract_id != roll_plan.new_contract_id
    ):
        raise FuturesPathError("roll_execution_plan_transition_mismatch")
    if (
        roll_execution.close_cost != close_fill.total_cost
        or roll_execution.open_cost != open_fill.total_cost
        or roll_execution.total_roll_cost != roll_plan.total_cost
    ):
        raise FuturesPathError("roll_execution_cost_reconciliation_mismatch")
    if roll_execution.price_gap != roll_plan.new_price - roll_plan.old_price:
        raise FuturesPathError("roll_execution_price_gap_mismatch")
    ordered_settlements = tuple(
        sorted(
            settlement_events,
            key=lambda item: (item.settled_at, item.event_id),
        )
    )
    if tuple(settlement_events) != ordered_settlements:
        raise FuturesPathError("settlement_reconciliation_events_not_ordered")
    if len({item.content_hash for item in settlement_events}) != len(settlement_events):
        raise FuturesPathError("settlement_reconciliation_event_duplicate")
    for event in settlement_events:
        if event.contract_id != roll_plan.old_contract_id:
            raise FuturesPathError("settlement_reconciliation_contract_unbound")
        if event.quote_hash != roll_plan.old_quote_hash:
            raise FuturesPathError("settlement_reconciliation_quote_unbound")
        if event.quantity != roll_plan.current_old_quantity:
            raise FuturesPathError("settlement_reconciliation_quantity_unbound")
        if event.multiplier != roll_plan.old_multiplier:
            raise FuturesPathError("settlement_reconciliation_multiplier_unbound")
        if parse_timestamp(event.settled_at, "settlement_event.settled_at") > executed:
            raise FuturesPathError("settlement_reconciliation_after_roll")
        if event.settlement_price != roll_plan.old_price:
            raise FuturesPathError("settlement_reconciliation_price_unbound")
    settlement_pnl = sum((item.variation_margin for item in settlement_events), _ZERO)
    roll_trade_pnl = sum((item.realized_trade_pnl for item in roll_fills), _ZERO)
    roll_cost = sum((item.total_cost for item in roll_fills), _ZERO)
    opening = _decimal(opening_cash, "reconciliation.opening_cash")
    closing = _decimal(closing_cash, "reconciliation.closing_cash")
    expected = settlement_pnl + roll_trade_pnl - roll_cost
    actual = closing - opening
    return FuturesPnlReconciliationEvidence(
        evidence_id=evidence_id,
        observed_at=observed_at,
        opening_cash=opening,
        closing_cash=closing,
        settlement_pnl=settlement_pnl,
        roll_trade_pnl=roll_trade_pnl,
        roll_cost=roll_cost,
        roll_yield_attribution=roll_execution.roll_yield,
        expected_cash_delta=expected,
        actual_cash_delta=actual,
        residual=actual - expected,
        tolerance=tolerance,
        settlement_event_hashes=tuple(item.content_hash for item in settlement_events),
        roll_execution_hash=roll_execution.content_hash,
        roll_fill_hashes=(close_fill.content_hash, open_fill.content_hash),
        roll_plan_hash=roll_plan.content_hash,
    )


__all__ = (
    "ContinuousPointProtocol",
    "ContinuousSignalMapping",
    "ContinuousSignalTrace",
    "ContractSpecificationVersion",
    "DeliverableTermsVersion",
    "ExistingFuturesCostPolicyAdapter",
    "ExpiryBucket",
    "ExpiryBucketFeature",
    "ExposurePreservingRollPlan",
    "FUTURES_PATH_SCHEMA_VERSION",
    "FuturesContractProtocol",
    "FuturesCurvePoint",
    "FuturesCurveSnapshot",
    "FuturesPathError",
    "FuturesPnlReconciliationEvidence",
    "FuturesReferenceHistory",
    "FuturesReferenceSnapshot",
    "MarginRequirementVersion",
    "PlannedRollLeg",
    "ReferenceMetadata",
    "RollCostModelProtocol",
    "RollLegCost",
    "RollLegRole",
    "RollPlanningPolicy",
    "adapt_existing_futures_contract",
    "adapt_existing_margin_policy",
    "build_futures_curve_snapshot",
    "plan_exposure_preserving_roll",
    "reconcile_existing_futures_pnl",
    "select_roll_target",
    "trace_continuous_signal",
)
