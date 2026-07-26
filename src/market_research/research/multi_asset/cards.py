"""Strict, versioned data and model cards for multi-asset research packages."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Mapping, Sequence, cast


DATA_CARD_SCHEMA_VERSION = 1
MODEL_CARD_SCHEMA_VERSION = 1

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_QUALITY_FLAG = re.compile(r"^[A-Z][A-Z0-9_:-]{0,127}$")


class ResearchCardError(ValueError):
    """A data or model card violates its immutable schema."""


class ValidationStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


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
    pattern: re.Pattern[str] | None = None,
) -> tuple[str, ...]:
    result = tuple(values)
    if not result:
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
    use_constraints: tuple[str, ...]
    coverage_start_at: str
    coverage_end_at: str
    coverage_markets: tuple[str, ...]
    coverage_instruments: tuple[str, ...]
    missing_data_summary: str
    missing_data_policy: str
    known_biases: tuple[str, ...]
    revision_policy: str
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
            "missing_data_summary",
            "missing_data_policy",
            "revision_policy",
        ):
            _require_text(getattr(self, field_name), f"data_card.{field_name}")
        _require_hash(self.license_terms_hash, "data_card.license_terms_hash")
        _strings(self.use_constraints, "data_card.use_constraints")
        start = _timestamp_text(self.coverage_start_at, "data_card.coverage_start_at")
        end = _timestamp_text(self.coverage_end_at, "data_card.coverage_end_at")
        if end < start:
            raise ResearchCardError("data_card.coverage_time_order_invalid")
        object.__setattr__(self, "coverage_start_at", start)
        object.__setattr__(self, "coverage_end_at", end)
        _strings(self.coverage_markets, "data_card.coverage_markets")
        _strings(self.coverage_instruments, "data_card.coverage_instruments")
        _strings(self.known_biases, "data_card.known_biases")
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
            "use_constraints": list(self.use_constraints),
            "coverage_start_at": self.coverage_start_at,
            "coverage_end_at": self.coverage_end_at,
            "coverage_markets": list(self.coverage_markets),
            "coverage_instruments": list(self.coverage_instruments),
            "missing_data_summary": self.missing_data_summary,
            "missing_data_policy": self.missing_data_policy,
            "known_biases": list(self.known_biases),
            "revision_policy": self.revision_policy,
            "validation_results": [
                item.as_dict() for item in self.validation_results
            ],
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
                "use_constraints",
                "coverage_start_at",
                "coverage_end_at",
                "coverage_markets",
                "coverage_instruments",
                "missing_data_summary",
                "missing_data_policy",
                "known_biases",
                "revision_policy",
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
            use_constraints=_string_tuple(
                payload["use_constraints"],
                "data_card.use_constraints",
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
            missing_data_summary=_require_text(
                payload["missing_data_summary"],
                "data_card.missing_data_summary",
            ),
            missing_data_policy=_require_text(
                payload["missing_data_policy"],
                "data_card.missing_data_policy",
            ),
            known_biases=_string_tuple(
                payload["known_biases"],
                "data_card.known_biases",
            ),
            revision_policy=_require_text(
                payload["revision_policy"],
                "data_card.revision_policy",
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
    implementation_hash: str
    input_schema_hash: str
    output_schema_hash: str
    assumptions: tuple[str, ...]
    applicability_scope: tuple[str, ...]
    failure_conditions: tuple[str, ...]
    validation_results: tuple[CardValidationResult, ...]
    known_limitations: tuple[str, ...]
    quality_flags: tuple[str, ...]
    content_hash: str = field(init=False)
    schema_version: int = MODEL_CARD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MODEL_CARD_SCHEMA_VERSION:
            raise ResearchCardError("model_card.schema_version_invalid")
        for field_name in ("card_id", "version", "model_id", "model_version"):
            _require_id(getattr(self, field_name), f"model_card.{field_name}")
        _require_text(self.model_name, "model_card.model_name")
        for field_name in (
            "implementation_hash",
            "input_schema_hash",
            "output_schema_hash",
        ):
            _require_hash(getattr(self, field_name), f"model_card.{field_name}")
        _strings(self.assumptions, "model_card.assumptions")
        _strings(self.applicability_scope, "model_card.applicability_scope")
        _strings(self.failure_conditions, "model_card.failure_conditions")
        _validation_results(self.validation_results, "model_card.validation_results")
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
            "implementation_hash": self.implementation_hash,
            "input_schema_hash": self.input_schema_hash,
            "output_schema_hash": self.output_schema_hash,
            "assumptions": list(self.assumptions),
            "applicability_scope": list(self.applicability_scope),
            "failure_conditions": list(self.failure_conditions),
            "validation_results": [
                item.as_dict() for item in self.validation_results
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
                "implementation_hash",
                "input_schema_hash",
                "output_schema_hash",
                "assumptions",
                "applicability_scope",
                "failure_conditions",
                "validation_results",
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
            implementation_hash=_require_hash(
                payload["implementation_hash"],
                "model_card.implementation_hash",
            ),
            input_schema_hash=_require_hash(
                payload["input_schema_hash"],
                "model_card.input_schema_hash",
            ),
            output_schema_hash=_require_hash(
                payload["output_schema_hash"],
                "model_card.output_schema_hash",
            ),
            assumptions=_string_tuple(
                payload["assumptions"],
                "model_card.assumptions",
            ),
            applicability_scope=_string_tuple(
                payload["applicability_scope"],
                "model_card.applicability_scope",
            ),
            failure_conditions=_string_tuple(
                payload["failure_conditions"],
                "model_card.failure_conditions",
            ),
            validation_results=_validation_results_from_dict(
                payload["validation_results"],
                "model_card.validation_results",
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
    "DataCard",
    "MODEL_CARD_SCHEMA_VERSION",
    "ModelCard",
    "ResearchCardError",
    "ValidationStatus",
]
