"""Strict, versioned data and model cards for multi-asset research packages."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Mapping, Sequence, cast


DATA_CARD_SCHEMA_VERSION = 2
MODEL_CARD_SCHEMA_VERSION = 2

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_QUALITY_FLAG = re.compile(r"^[A-Z][A-Z0-9_:-]{0,127}$")


class ResearchCardError(ValueError):
    """A data or model card violates its immutable schema."""


class ValidationStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class DistributionStatus(StrEnum):
    """Legally meaningful distribution status for one dataset snapshot."""

    PUBLIC = "PUBLIC"
    REDISTRIBUTABLE = "REDISTRIBUTABLE"
    LICENSE_RESTRICTED = "LICENSE_RESTRICTED"
    INTERNAL_ONLY = "INTERNAL_ONLY"
    NON_REDISTRIBUTABLE = "NON_REDISTRIBUTABLE"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _content_hash(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}"


def _require_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _STABLE_ID.fullmatch(value):
        raise ResearchCardError(f"{field_name}_invalid")
    return value


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ResearchCardError(f"{field_name}_invalid")
    return value


def _require_hash(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ResearchCardError(f"{field_name}_invalid")
    return value


def _timestamp_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ResearchCardError(f"{field_name}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchCardError(f"{field_name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchCardError(f"{field_name}_timezone_required")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _strings(
    values: Sequence[str],
    field_name: str,
    *,
    allow_empty: bool = False,
    pattern: re.Pattern[str] | None = None,
) -> tuple[str, ...]:
    result = tuple(values)
    if not allow_empty and not result:
        raise ResearchCardError(f"{field_name}_required")
    if result != tuple(sorted(set(result))):
        raise ResearchCardError(f"{field_name}_must_be_sorted_unique")
    for value in result:
        if (
            not isinstance(value, str)
            or not value
            or value.strip() != value
            or (pattern is not None and not pattern.fullmatch(value))
        ):
            raise ResearchCardError(f"{field_name}_invalid")
    return result


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ResearchCardError(f"{field_name}_object_required")
    return cast(Mapping[str, object], value)


def _exact_fields(
    payload: Mapping[str, object],
    expected: frozenset[str],
    field_name: str,
) -> None:
    if set(payload) != expected:
        raise ResearchCardError(f"{field_name}_fields_invalid")


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ResearchCardError(f"{field_name}_array_required")
    return tuple(cast(list[str], value))


def _hashes(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    result = _strings(values, field_name)
    for value in result:
        _require_hash(value, field_name)
    return result


def _ids(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    result = _strings(values, field_name, pattern=_STABLE_ID)
    return result


@dataclass(frozen=True, slots=True)
class CardValidationResult:
    """One immutable validation result cited by a data or model card."""

    check_id: str
    status: ValidationStatus
    summary: str
    evidence_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.check_id, "card_validation.check_id")
        if not isinstance(self.status, ValidationStatus):
            raise ResearchCardError("card_validation.status_invalid")
        _require_text(self.summary, "card_validation.summary")
        _require_hash(self.evidence_hash, "card_validation.evidence_hash")
        object.__setattr__(self, "content_hash", _content_hash(self.identity_payload()))

    def identity_payload(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "status": self.status.value,
            "summary": self.summary,
            "evidence_hash": self.evidence_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> "CardValidationResult":
        payload = _mapping(value, "card_validation")
        _exact_fields(
            payload,
            frozenset(
                {"check_id", "status", "summary", "evidence_hash", "content_hash"}
            ),
            "card_validation",
        )
        try:
            status = ValidationStatus(cast(str, payload["status"]))
        except (TypeError, ValueError) as exc:
            raise ResearchCardError("card_validation.status_invalid") from exc
        result = cls(
            check_id=_require_id(payload["check_id"], "card_validation.check_id"),
            status=status,
            summary=_require_text(payload["summary"], "card_validation.summary"),
            evidence_hash=_require_hash(
                payload["evidence_hash"],
                "card_validation.evidence_hash",
            ),
        )
        if payload["content_hash"] != result.content_hash:
            raise ResearchCardError("card_validation.content_hash_mismatch")
        return result


def _validation_results(
    values: Sequence[CardValidationResult],
    field_name: str,
) -> tuple[CardValidationResult, ...]:
    result = tuple(values)
    if not result or any(not isinstance(item, CardValidationResult) for item in result):
        raise ResearchCardError(f"{field_name}_required")
    ids = tuple(item.check_id for item in result)
    if ids != tuple(sorted(set(ids))):
        raise ResearchCardError(f"{field_name}_must_be_sorted_unique")
    return result


def _validation_results_from_dict(
    value: object,
    field_name: str,
) -> tuple[CardValidationResult, ...]:
    if not isinstance(value, list):
        raise ResearchCardError(f"{field_name}_array_required")
    return tuple(CardValidationResult.from_dict(item) for item in value)


@dataclass(frozen=True, slots=True)
class DataFieldDefinition:
    """One canonical source field and its declared research semantics."""

    field_name: str
    data_type: str
    semantic_type: str
    nullable: bool
    description: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.field_name, "data_field.field_name")
        _require_text(self.data_type, "data_field.data_type")
        _require_text(self.semantic_type, "data_field.semantic_type")
        if not isinstance(self.nullable, bool):
            raise ResearchCardError("data_field.nullable_invalid")
        _require_text(self.description, "data_field.description")
        object.__setattr__(self, "content_hash", _content_hash(self.identity_payload()))

    def identity_payload(self) -> dict[str, object]:
        return {
            "field_name": self.field_name,
            "data_type": self.data_type,
            "semantic_type": self.semantic_type,
            "nullable": self.nullable,
            "description": self.description,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> "DataFieldDefinition":
        payload = _mapping(value, "data_field")
        _exact_fields(
            payload,
            frozenset(
                {
                    "field_name",
                    "data_type",
                    "semantic_type",
                    "nullable",
                    "description",
                    "content_hash",
                }
            ),
            "data_field",
        )
        nullable = payload["nullable"]
        if not isinstance(nullable, bool):
            raise ResearchCardError("data_field.nullable_invalid")
        result = cls(
            field_name=_require_id(payload["field_name"], "data_field.field_name"),
            data_type=_require_text(payload["data_type"], "data_field.data_type"),
            semantic_type=_require_text(
                payload["semantic_type"],
                "data_field.semantic_type",
            ),
            nullable=nullable,
            description=_require_text(
                payload["description"],
                "data_field.description",
            ),
        )
        if payload["content_hash"] != result.content_hash:
            raise ResearchCardError("data_field.content_hash_mismatch")
        return result


@dataclass(frozen=True, slots=True)
class DataFieldUnit:
    """Explicit unit bound to one field in a data-card schema."""

    field_name: str
    unit: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.field_name, "data_unit.field_name")
        _require_text(self.unit, "data_unit.unit")
        object.__setattr__(self, "content_hash", _content_hash(self.identity_payload()))

    def identity_payload(self) -> dict[str, object]:
        return {"field_name": self.field_name, "unit": self.unit}

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> "DataFieldUnit":
        payload = _mapping(value, "data_unit")
        _exact_fields(
            payload,
            frozenset({"field_name", "unit", "content_hash"}),
            "data_unit",
        )
        result = cls(
            field_name=_require_id(payload["field_name"], "data_unit.field_name"),
            unit=_require_text(payload["unit"], "data_unit.unit"),
        )
        if payload["content_hash"] != result.content_hash:
            raise ResearchCardError("data_unit.content_hash_mismatch")
        return result


@dataclass(frozen=True, slots=True)
class DataTemporalSemantics:
    """Point-in-time field bindings, timezone, and calendars."""

    valid_time_field: str
    valid_time_definition: str
    knowledge_time_field: str
    knowledge_time_definition: str
    availability_time_field: str
    availability_time_definition: str
    timezone: str
    calendar_ids: tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "valid_time_field",
            "knowledge_time_field",
            "availability_time_field",
        ):
            _require_id(getattr(self, field_name), f"data_temporal.{field_name}")
        for field_name in (
            "valid_time_definition",
            "knowledge_time_definition",
            "availability_time_definition",
            "timezone",
        ):
            _require_text(getattr(self, field_name), f"data_temporal.{field_name}")
        _ids(self.calendar_ids, "data_temporal.calendar_ids")
        object.__setattr__(self, "content_hash", _content_hash(self.identity_payload()))

    def identity_payload(self) -> dict[str, object]:
        return {
            "valid_time_field": self.valid_time_field,
            "valid_time_definition": self.valid_time_definition,
            "knowledge_time_field": self.knowledge_time_field,
            "knowledge_time_definition": self.knowledge_time_definition,
            "availability_time_field": self.availability_time_field,
            "availability_time_definition": self.availability_time_definition,
            "timezone": self.timezone,
            "calendar_ids": list(self.calendar_ids),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> "DataTemporalSemantics":
        payload = _mapping(value, "data_temporal")
        expected = frozenset(
            {
                "valid_time_field",
                "valid_time_definition",
                "knowledge_time_field",
                "knowledge_time_definition",
                "availability_time_field",
                "availability_time_definition",
                "timezone",
                "calendar_ids",
                "content_hash",
            }
        )
        _exact_fields(payload, expected, "data_temporal")
        result = cls(
            valid_time_field=_require_id(
                payload["valid_time_field"],
                "data_temporal.valid_time_field",
            ),
            valid_time_definition=_require_text(
                payload["valid_time_definition"],
                "data_temporal.valid_time_definition",
            ),
            knowledge_time_field=_require_id(
                payload["knowledge_time_field"],
                "data_temporal.knowledge_time_field",
            ),
            knowledge_time_definition=_require_text(
                payload["knowledge_time_definition"],
                "data_temporal.knowledge_time_definition",
            ),
            availability_time_field=_require_id(
                payload["availability_time_field"],
                "data_temporal.availability_time_field",
            ),
            availability_time_definition=_require_text(
                payload["availability_time_definition"],
                "data_temporal.availability_time_definition",
            ),
            timezone=_require_text(payload["timezone"], "data_temporal.timezone"),
            calendar_ids=_string_tuple(
                payload["calendar_ids"],
                "data_temporal.calendar_ids",
            ),
        )
        if payload["content_hash"] != result.content_hash:
            raise ResearchCardError("data_temporal.content_hash_mismatch")
        return result


@dataclass(frozen=True, slots=True)
class DataRowResolverMetadata:
    """Metadata needed to resolve a normalized row to immutable source evidence."""

    resolver_id: str
    resolver_version: str
    row_identity_fields: tuple[str, ...]
    source_artifact_hash_field: str
    source_row_hash_field: str
    resolution_policy: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "resolver_id",
            "resolver_version",
            "source_artifact_hash_field",
            "source_row_hash_field",
        ):
            _require_id(getattr(self, field_name), f"data_resolver.{field_name}")
        _ids(self.row_identity_fields, "data_resolver.row_identity_fields")
        _require_text(self.resolution_policy, "data_resolver.resolution_policy")
        object.__setattr__(self, "content_hash", _content_hash(self.identity_payload()))

    def identity_payload(self) -> dict[str, object]:
        return {
            "resolver_id": self.resolver_id,
            "resolver_version": self.resolver_version,
            "row_identity_fields": list(self.row_identity_fields),
            "source_artifact_hash_field": self.source_artifact_hash_field,
            "source_row_hash_field": self.source_row_hash_field,
            "resolution_policy": self.resolution_policy,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> "DataRowResolverMetadata":
        payload = _mapping(value, "data_resolver")
        expected = frozenset(
            {
                "resolver_id",
                "resolver_version",
                "row_identity_fields",
                "source_artifact_hash_field",
                "source_row_hash_field",
                "resolution_policy",
                "content_hash",
            }
        )
        _exact_fields(payload, expected, "data_resolver")
        result = cls(
            resolver_id=_require_id(
                payload["resolver_id"],
                "data_resolver.resolver_id",
            ),
            resolver_version=_require_id(
                payload["resolver_version"],
                "data_resolver.resolver_version",
            ),
            row_identity_fields=_string_tuple(
                payload["row_identity_fields"],
                "data_resolver.row_identity_fields",
            ),
            source_artifact_hash_field=_require_id(
                payload["source_artifact_hash_field"],
                "data_resolver.source_artifact_hash_field",
            ),
            source_row_hash_field=_require_id(
                payload["source_row_hash_field"],
                "data_resolver.source_row_hash_field",
            ),
            resolution_policy=_require_text(
                payload["resolution_policy"],
                "data_resolver.resolution_policy",
            ),
        )
        if payload["content_hash"] != result.content_hash:
            raise ResearchCardError("data_resolver.content_hash_mismatch")
        return result


@dataclass(frozen=True, slots=True)
class ModelParameter:
    """One canonical model parameter or deterministic configuration value."""

    parameter_name: str
    value: str
    data_type: str
    unit: str
    description: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.parameter_name, "model_parameter.parameter_name")
        for field_name in ("value", "data_type", "unit", "description"):
            _require_text(
                getattr(self, field_name),
                f"model_parameter.{field_name}",
            )
        object.__setattr__(self, "content_hash", _content_hash(self.identity_payload()))

    def identity_payload(self) -> dict[str, object]:
        return {
            "parameter_name": self.parameter_name,
            "value": self.value,
            "data_type": self.data_type,
            "unit": self.unit,
            "description": self.description,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> "ModelParameter":
        payload = _mapping(value, "model_parameter")
        expected = frozenset(
            {
                "parameter_name",
                "value",
                "data_type",
                "unit",
                "description",
                "content_hash",
            }
        )
        _exact_fields(payload, expected, "model_parameter")
        result = cls(
            parameter_name=_require_id(
                payload["parameter_name"],
                "model_parameter.parameter_name",
            ),
            value=_require_text(payload["value"], "model_parameter.value"),
            data_type=_require_text(
                payload["data_type"],
                "model_parameter.data_type",
            ),
            unit=_require_text(payload["unit"], "model_parameter.unit"),
            description=_require_text(
                payload["description"],
                "model_parameter.description",
            ),
        )
        if payload["content_hash"] != result.content_hash:
            raise ResearchCardError("model_parameter.content_hash_mismatch")
        return result


def _field_definitions(
    values: Sequence[DataFieldDefinition],
    field_name: str,
) -> tuple[DataFieldDefinition, ...]:
    result = tuple(values)
    if not result or any(not isinstance(item, DataFieldDefinition) for item in result):
        raise ResearchCardError(f"{field_name}_required")
    names = tuple(item.field_name for item in result)
    if names != tuple(sorted(set(names))):
        raise ResearchCardError(f"{field_name}_must_be_sorted_unique")
    return result


def _field_units(
    values: Sequence[DataFieldUnit],
    field_name: str,
) -> tuple[DataFieldUnit, ...]:
    result = tuple(values)
    if not result or any(not isinstance(item, DataFieldUnit) for item in result):
        raise ResearchCardError(f"{field_name}_required")
    names = tuple(item.field_name for item in result)
    if names != tuple(sorted(set(names))):
        raise ResearchCardError(f"{field_name}_must_be_sorted_unique")
    return result


def _model_parameters(
    values: Sequence[ModelParameter],
    field_name: str,
) -> tuple[ModelParameter, ...]:
    result = tuple(values)
    if not result or any(not isinstance(item, ModelParameter) for item in result):
        raise ResearchCardError(f"{field_name}_required")
    names = tuple(item.parameter_name for item in result)
    if names != tuple(sorted(set(names))):
        raise ResearchCardError(f"{field_name}_must_be_sorted_unique")
    return result


def _object_list(value: object, field_name: str) -> list[object]:
    if not isinstance(value, list):
        raise ResearchCardError(f"{field_name}_array_required")
    return cast(list[object], value)


@dataclass(frozen=True, slots=True)
class DataCard:
    """Complete data provenance, suitability, and limitation declaration."""

    card_id: str
    version: str
    dataset_id: str
    dataset_version: str
    source_name: str
    source_reference: str
    license_id: str
    license_terms_hash: str
    distribution_status: DistributionStatus
    use_constraints: tuple[str, ...]
    snapshot_method: str
    coverage_start_at: str
    coverage_end_at: str
    coverage_markets: tuple[str, ...]
    coverage_instruments: tuple[str, ...]
    field_schema: tuple[DataFieldDefinition, ...]
    units: tuple[DataFieldUnit, ...]
    temporal_semantics: DataTemporalSemantics
    normalization_transformations: tuple[str, ...]
    known_corrections: tuple[str, ...]
    missing_data_summary: str
    missing_data_policy: str
    survivorship_policy: str
    corporate_action_policy: str
    known_biases: tuple[str, ...]
    known_limitations: tuple[str, ...]
    intended_uses: tuple[str, ...]
    prohibited_uses: tuple[str, ...]
    revision_policy: str
    source_hashes: tuple[str, ...]
    row_resolver_metadata: DataRowResolverMetadata
    validation_results: tuple[CardValidationResult, ...]
    quality_flags: tuple[str, ...]
    content_hash: str = field(init=False)
    schema_version: int = DATA_CARD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DATA_CARD_SCHEMA_VERSION:
            raise ResearchCardError("data_card.schema_version_invalid")
        for field_name in ("card_id", "version", "dataset_id", "dataset_version"):
            _require_id(getattr(self, field_name), f"data_card.{field_name}")
        for field_name in (
            "source_name",
            "source_reference",
            "license_id",
            "snapshot_method",
            "missing_data_summary",
            "missing_data_policy",
            "survivorship_policy",
            "corporate_action_policy",
            "revision_policy",
        ):
            _require_text(getattr(self, field_name), f"data_card.{field_name}")
        _require_hash(self.license_terms_hash, "data_card.license_terms_hash")
        if not isinstance(self.distribution_status, DistributionStatus):
            raise ResearchCardError("data_card.distribution_status_invalid")
        _strings(self.use_constraints, "data_card.use_constraints")
        start = _timestamp_text(self.coverage_start_at, "data_card.coverage_start_at")
        end = _timestamp_text(self.coverage_end_at, "data_card.coverage_end_at")
        if end < start:
            raise ResearchCardError("data_card.coverage_time_order_invalid")
        object.__setattr__(self, "coverage_start_at", start)
        object.__setattr__(self, "coverage_end_at", end)
        _strings(self.coverage_markets, "data_card.coverage_markets")
        _strings(self.coverage_instruments, "data_card.coverage_instruments")
        fields = _field_definitions(self.field_schema, "data_card.field_schema")
        units = _field_units(self.units, "data_card.units")
        field_names = {item.field_name for item in fields}
        if {item.field_name for item in units} != field_names:
            raise ResearchCardError("data_card.units_field_coverage_mismatch")
        if not isinstance(self.temporal_semantics, DataTemporalSemantics):
            raise ResearchCardError("data_card.temporal_semantics_required")
        temporal_fields = {
            self.temporal_semantics.valid_time_field,
            self.temporal_semantics.knowledge_time_field,
            self.temporal_semantics.availability_time_field,
        }
        if not temporal_fields <= field_names:
            raise ResearchCardError(
                "data_card.temporal_semantics_field_coverage_mismatch"
            )
        _strings(
            self.normalization_transformations,
            "data_card.normalization_transformations",
            allow_empty=True,
        )
        _strings(
            self.known_corrections,
            "data_card.known_corrections",
            allow_empty=True,
        )
        _strings(self.known_biases, "data_card.known_biases")
        _strings(self.known_limitations, "data_card.known_limitations")
        intended = _strings(self.intended_uses, "data_card.intended_uses")
        prohibited = _strings(self.prohibited_uses, "data_card.prohibited_uses")
        if set(intended) & set(prohibited):
            raise ResearchCardError("data_card.use_scope_overlap")
        _hashes(self.source_hashes, "data_card.source_hashes")
        if not isinstance(self.row_resolver_metadata, DataRowResolverMetadata):
            raise ResearchCardError("data_card.row_resolver_metadata_required")
        resolver_fields = {
            *self.row_resolver_metadata.row_identity_fields,
            self.row_resolver_metadata.source_artifact_hash_field,
            self.row_resolver_metadata.source_row_hash_field,
        }
        if not resolver_fields <= field_names:
            raise ResearchCardError("data_card.row_resolver_field_coverage_mismatch")
        _validation_results(self.validation_results, "data_card.validation_results")
        _strings(
            self.quality_flags,
            "data_card.quality_flags",
            pattern=_QUALITY_FLAG,
        )
        object.__setattr__(self, "content_hash", _content_hash(self.identity_payload()))

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "card_type": "DATA_CARD",
            "card_id": self.card_id,
            "version": self.version,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "source_name": self.source_name,
            "source_reference": self.source_reference,
            "license_id": self.license_id,
            "license_terms_hash": self.license_terms_hash,
            "distribution_status": self.distribution_status.value,
            "use_constraints": list(self.use_constraints),
            "snapshot_method": self.snapshot_method,
            "coverage_start_at": self.coverage_start_at,
            "coverage_end_at": self.coverage_end_at,
            "coverage_markets": list(self.coverage_markets),
            "coverage_instruments": list(self.coverage_instruments),
            "field_schema": [item.as_dict() for item in self.field_schema],
            "units": [item.as_dict() for item in self.units],
            "temporal_semantics": self.temporal_semantics.as_dict(),
            "normalization_transformations": list(self.normalization_transformations),
            "known_corrections": list(self.known_corrections),
            "missing_data_summary": self.missing_data_summary,
            "missing_data_policy": self.missing_data_policy,
            "survivorship_policy": self.survivorship_policy,
            "corporate_action_policy": self.corporate_action_policy,
            "known_biases": list(self.known_biases),
            "known_limitations": list(self.known_limitations),
            "intended_uses": list(self.intended_uses),
            "prohibited_uses": list(self.prohibited_uses),
            "revision_policy": self.revision_policy,
            "source_hashes": list(self.source_hashes),
            "row_resolver_metadata": self.row_resolver_metadata.as_dict(),
            "validation_results": [item.as_dict() for item in self.validation_results],
            "quality_flags": list(self.quality_flags),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> "DataCard":
        payload = _mapping(value, "data_card")
        expected = frozenset(
            {
                "schema_version",
                "card_type",
                "card_id",
                "version",
                "dataset_id",
                "dataset_version",
                "source_name",
                "source_reference",
                "license_id",
                "license_terms_hash",
                "distribution_status",
                "use_constraints",
                "snapshot_method",
                "coverage_start_at",
                "coverage_end_at",
                "coverage_markets",
                "coverage_instruments",
                "field_schema",
                "units",
                "temporal_semantics",
                "normalization_transformations",
                "known_corrections",
                "missing_data_summary",
                "missing_data_policy",
                "survivorship_policy",
                "corporate_action_policy",
                "known_biases",
                "known_limitations",
                "intended_uses",
                "prohibited_uses",
                "revision_policy",
                "source_hashes",
                "row_resolver_metadata",
                "validation_results",
                "quality_flags",
                "content_hash",
            }
        )
        _exact_fields(payload, expected, "data_card")
        if payload["card_type"] != "DATA_CARD":
            raise ResearchCardError("data_card.card_type_invalid")
        if payload["schema_version"] != DATA_CARD_SCHEMA_VERSION:
            raise ResearchCardError("data_card.schema_version_invalid")
        try:
            distribution_status = DistributionStatus(
                cast(str, payload["distribution_status"])
            )
        except (TypeError, ValueError) as exc:
            raise ResearchCardError("data_card.distribution_status_invalid") from exc
        result = cls(
            card_id=_require_id(payload["card_id"], "data_card.card_id"),
            version=_require_id(payload["version"], "data_card.version"),
            dataset_id=_require_id(payload["dataset_id"], "data_card.dataset_id"),
            dataset_version=_require_id(
                payload["dataset_version"],
                "data_card.dataset_version",
            ),
            source_name=_require_text(
                payload["source_name"],
                "data_card.source_name",
            ),
            source_reference=_require_text(
                payload["source_reference"],
                "data_card.source_reference",
            ),
            license_id=_require_text(payload["license_id"], "data_card.license_id"),
            license_terms_hash=_require_hash(
                payload["license_terms_hash"],
                "data_card.license_terms_hash",
            ),
            distribution_status=distribution_status,
            use_constraints=_string_tuple(
                payload["use_constraints"],
                "data_card.use_constraints",
            ),
            snapshot_method=_require_text(
                payload["snapshot_method"],
                "data_card.snapshot_method",
            ),
            coverage_start_at=_require_text(
                payload["coverage_start_at"],
                "data_card.coverage_start_at",
            ),
            coverage_end_at=_require_text(
                payload["coverage_end_at"],
                "data_card.coverage_end_at",
            ),
            coverage_markets=_string_tuple(
                payload["coverage_markets"],
                "data_card.coverage_markets",
            ),
            coverage_instruments=_string_tuple(
                payload["coverage_instruments"],
                "data_card.coverage_instruments",
            ),
            field_schema=tuple(
                DataFieldDefinition.from_dict(item)
                for item in _object_list(
                    payload["field_schema"],
                    "data_card.field_schema",
                )
            ),
            units=tuple(
                DataFieldUnit.from_dict(item)
                for item in _object_list(payload["units"], "data_card.units")
            ),
            temporal_semantics=DataTemporalSemantics.from_dict(
                payload["temporal_semantics"]
            ),
            normalization_transformations=_string_tuple(
                payload["normalization_transformations"],
                "data_card.normalization_transformations",
            ),
            known_corrections=_string_tuple(
                payload["known_corrections"],
                "data_card.known_corrections",
            ),
            missing_data_summary=_require_text(
                payload["missing_data_summary"],
                "data_card.missing_data_summary",
            ),
            missing_data_policy=_require_text(
                payload["missing_data_policy"],
                "data_card.missing_data_policy",
            ),
            survivorship_policy=_require_text(
                payload["survivorship_policy"],
                "data_card.survivorship_policy",
            ),
            corporate_action_policy=_require_text(
                payload["corporate_action_policy"],
                "data_card.corporate_action_policy",
            ),
            known_biases=_string_tuple(
                payload["known_biases"],
                "data_card.known_biases",
            ),
            known_limitations=_string_tuple(
                payload["known_limitations"],
                "data_card.known_limitations",
            ),
            intended_uses=_string_tuple(
                payload["intended_uses"],
                "data_card.intended_uses",
            ),
            prohibited_uses=_string_tuple(
                payload["prohibited_uses"],
                "data_card.prohibited_uses",
            ),
            revision_policy=_require_text(
                payload["revision_policy"],
                "data_card.revision_policy",
            ),
            source_hashes=_string_tuple(
                payload["source_hashes"],
                "data_card.source_hashes",
            ),
            row_resolver_metadata=DataRowResolverMetadata.from_dict(
                payload["row_resolver_metadata"]
            ),
            validation_results=_validation_results_from_dict(
                payload["validation_results"],
                "data_card.validation_results",
            ),
            quality_flags=_string_tuple(
                payload["quality_flags"],
                "data_card.quality_flags",
            ),
        )
        if payload["content_hash"] != result.content_hash:
            raise ResearchCardError("data_card.content_hash_mismatch")
        return result


@dataclass(frozen=True, slots=True)
class ModelCard:
    """Complete model assumptions, applicability, and validation declaration."""

    card_id: str
    version: str
    model_id: str
    model_version: str
    model_name: str
    model_family: str
    implementation_hash: str
    code_hash: str
    configuration_hash: str
    input_schema_hash: str
    output_schema_hash: str
    input_hashes: tuple[str, ...]
    output_hashes: tuple[str, ...]
    assumptions: tuple[str, ...]
    applicability_scope: tuple[str, ...]
    unsupported_cases: tuple[str, ...]
    parameters: tuple[ModelParameter, ...]
    calibration_data_hashes: tuple[str, ...]
    calibration_process: str
    objective: str
    diagnostic_results: tuple[CardValidationResult, ...]
    convergence_criteria: str
    convergence_result: CardValidationResult
    failure_conditions: tuple[str, ...]
    failure_behavior: str
    validation_results: tuple[CardValidationResult, ...]
    benchmark_results: tuple[CardValidationResult, ...]
    sensitivity_results: tuple[CardValidationResult, ...]
    deterministic_configuration: tuple[ModelParameter, ...]
    known_limitations: tuple[str, ...]
    quality_flags: tuple[str, ...]
    content_hash: str = field(init=False)
    schema_version: int = MODEL_CARD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MODEL_CARD_SCHEMA_VERSION:
            raise ResearchCardError("model_card.schema_version_invalid")
        for field_name in ("card_id", "version", "model_id", "model_version"):
            _require_id(getattr(self, field_name), f"model_card.{field_name}")
        for field_name in (
            "model_name",
            "model_family",
            "calibration_process",
            "objective",
            "convergence_criteria",
            "failure_behavior",
        ):
            _require_text(getattr(self, field_name), f"model_card.{field_name}")
        for field_name in (
            "implementation_hash",
            "code_hash",
            "configuration_hash",
            "input_schema_hash",
            "output_schema_hash",
        ):
            _require_hash(getattr(self, field_name), f"model_card.{field_name}")
        _hashes(self.input_hashes, "model_card.input_hashes")
        _hashes(self.output_hashes, "model_card.output_hashes")
        _strings(self.assumptions, "model_card.assumptions")
        _strings(self.applicability_scope, "model_card.applicability_scope")
        _strings(self.unsupported_cases, "model_card.unsupported_cases")
        _model_parameters(self.parameters, "model_card.parameters")
        _hashes(self.calibration_data_hashes, "model_card.calibration_data_hashes")
        _validation_results(
            self.diagnostic_results,
            "model_card.diagnostic_results",
        )
        if not isinstance(self.convergence_result, CardValidationResult):
            raise ResearchCardError("model_card.convergence_result_required")
        _strings(self.failure_conditions, "model_card.failure_conditions")
        _validation_results(self.validation_results, "model_card.validation_results")
        _validation_results(self.benchmark_results, "model_card.benchmark_results")
        _validation_results(
            self.sensitivity_results,
            "model_card.sensitivity_results",
        )
        _model_parameters(
            self.deterministic_configuration,
            "model_card.deterministic_configuration",
        )
        _strings(self.known_limitations, "model_card.known_limitations")
        _strings(
            self.quality_flags,
            "model_card.quality_flags",
            pattern=_QUALITY_FLAG,
        )
        object.__setattr__(self, "content_hash", _content_hash(self.identity_payload()))

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "card_type": "MODEL_CARD",
            "card_id": self.card_id,
            "version": self.version,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "model_name": self.model_name,
            "model_family": self.model_family,
            "implementation_hash": self.implementation_hash,
            "code_hash": self.code_hash,
            "configuration_hash": self.configuration_hash,
            "input_schema_hash": self.input_schema_hash,
            "output_schema_hash": self.output_schema_hash,
            "input_hashes": list(self.input_hashes),
            "output_hashes": list(self.output_hashes),
            "assumptions": list(self.assumptions),
            "applicability_scope": list(self.applicability_scope),
            "unsupported_cases": list(self.unsupported_cases),
            "parameters": [item.as_dict() for item in self.parameters],
            "calibration_data_hashes": list(self.calibration_data_hashes),
            "calibration_process": self.calibration_process,
            "objective": self.objective,
            "diagnostic_results": [item.as_dict() for item in self.diagnostic_results],
            "convergence_criteria": self.convergence_criteria,
            "convergence_result": self.convergence_result.as_dict(),
            "failure_conditions": list(self.failure_conditions),
            "failure_behavior": self.failure_behavior,
            "validation_results": [item.as_dict() for item in self.validation_results],
            "benchmark_results": [item.as_dict() for item in self.benchmark_results],
            "sensitivity_results": [
                item.as_dict() for item in self.sensitivity_results
            ],
            "deterministic_configuration": [
                item.as_dict() for item in self.deterministic_configuration
            ],
            "known_limitations": list(self.known_limitations),
            "quality_flags": list(self.quality_flags),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> "ModelCard":
        payload = _mapping(value, "model_card")
        expected = frozenset(
            {
                "schema_version",
                "card_type",
                "card_id",
                "version",
                "model_id",
                "model_version",
                "model_name",
                "model_family",
                "implementation_hash",
                "code_hash",
                "configuration_hash",
                "input_schema_hash",
                "output_schema_hash",
                "input_hashes",
                "output_hashes",
                "assumptions",
                "applicability_scope",
                "unsupported_cases",
                "parameters",
                "calibration_data_hashes",
                "calibration_process",
                "objective",
                "diagnostic_results",
                "convergence_criteria",
                "convergence_result",
                "failure_conditions",
                "failure_behavior",
                "validation_results",
                "benchmark_results",
                "sensitivity_results",
                "deterministic_configuration",
                "known_limitations",
                "quality_flags",
                "content_hash",
            }
        )
        _exact_fields(payload, expected, "model_card")
        if payload["card_type"] != "MODEL_CARD":
            raise ResearchCardError("model_card.card_type_invalid")
        if payload["schema_version"] != MODEL_CARD_SCHEMA_VERSION:
            raise ResearchCardError("model_card.schema_version_invalid")
        result = cls(
            card_id=_require_id(payload["card_id"], "model_card.card_id"),
            version=_require_id(payload["version"], "model_card.version"),
            model_id=_require_id(payload["model_id"], "model_card.model_id"),
            model_version=_require_id(
                payload["model_version"],
                "model_card.model_version",
            ),
            model_name=_require_text(payload["model_name"], "model_card.model_name"),
            model_family=_require_text(
                payload["model_family"],
                "model_card.model_family",
            ),
            implementation_hash=_require_hash(
                payload["implementation_hash"],
                "model_card.implementation_hash",
            ),
            code_hash=_require_hash(
                payload["code_hash"],
                "model_card.code_hash",
            ),
            configuration_hash=_require_hash(
                payload["configuration_hash"],
                "model_card.configuration_hash",
            ),
            input_schema_hash=_require_hash(
                payload["input_schema_hash"],
                "model_card.input_schema_hash",
            ),
            output_schema_hash=_require_hash(
                payload["output_schema_hash"],
                "model_card.output_schema_hash",
            ),
            input_hashes=_string_tuple(
                payload["input_hashes"],
                "model_card.input_hashes",
            ),
            output_hashes=_string_tuple(
                payload["output_hashes"],
                "model_card.output_hashes",
            ),
            assumptions=_string_tuple(
                payload["assumptions"],
                "model_card.assumptions",
            ),
            applicability_scope=_string_tuple(
                payload["applicability_scope"],
                "model_card.applicability_scope",
            ),
            unsupported_cases=_string_tuple(
                payload["unsupported_cases"],
                "model_card.unsupported_cases",
            ),
            parameters=tuple(
                ModelParameter.from_dict(item)
                for item in _object_list(
                    payload["parameters"],
                    "model_card.parameters",
                )
            ),
            calibration_data_hashes=_string_tuple(
                payload["calibration_data_hashes"],
                "model_card.calibration_data_hashes",
            ),
            calibration_process=_require_text(
                payload["calibration_process"],
                "model_card.calibration_process",
            ),
            objective=_require_text(
                payload["objective"],
                "model_card.objective",
            ),
            diagnostic_results=_validation_results_from_dict(
                payload["diagnostic_results"],
                "model_card.diagnostic_results",
            ),
            convergence_criteria=_require_text(
                payload["convergence_criteria"],
                "model_card.convergence_criteria",
            ),
            convergence_result=CardValidationResult.from_dict(
                payload["convergence_result"]
            ),
            failure_conditions=_string_tuple(
                payload["failure_conditions"],
                "model_card.failure_conditions",
            ),
            failure_behavior=_require_text(
                payload["failure_behavior"],
                "model_card.failure_behavior",
            ),
            validation_results=_validation_results_from_dict(
                payload["validation_results"],
                "model_card.validation_results",
            ),
            benchmark_results=_validation_results_from_dict(
                payload["benchmark_results"],
                "model_card.benchmark_results",
            ),
            sensitivity_results=_validation_results_from_dict(
                payload["sensitivity_results"],
                "model_card.sensitivity_results",
            ),
            deterministic_configuration=tuple(
                ModelParameter.from_dict(item)
                for item in _object_list(
                    payload["deterministic_configuration"],
                    "model_card.deterministic_configuration",
                )
            ),
            known_limitations=_string_tuple(
                payload["known_limitations"],
                "model_card.known_limitations",
            ),
            quality_flags=_string_tuple(
                payload["quality_flags"],
                "model_card.quality_flags",
            ),
        )
        if payload["content_hash"] != result.content_hash:
            raise ResearchCardError("model_card.content_hash_mismatch")
        return result


__all__ = [
    "CardValidationResult",
    "DATA_CARD_SCHEMA_VERSION",
    "DataFieldDefinition",
    "DataFieldUnit",
    "DataRowResolverMetadata",
    "DataTemporalSemantics",
    "DataCard",
    "DistributionStatus",
    "MODEL_CARD_SCHEMA_VERSION",
    "ModelCard",
    "ModelParameter",
    "ResearchCardError",
    "ValidationStatus",
]
