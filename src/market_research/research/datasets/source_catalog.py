"""Immutable catalog for externally prepared research data sources.

The Research distribution does not collect market data.  This contract records
which external preparation source produced an immutable input and the reviewed
time, revision, license, quality, ownership, and credential-boundary policies
that apply to it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Any, Iterable, Mapping

from ..hashing import sha256_prefixed


SOURCE_CATALOG_SCHEMA_VERSION = 1
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_SOURCE_KINDS = frozenset(
    {"external_api", "file_export", "object_snapshot", "vendor_archive"}
)
_QUALITY_LEVELS = frozenset({"PROVISIONAL", "REVIEWED", "VERIFIED"})
_PIT_POLICIES = frozenset(
    {
        "event_available_received_processed_times",
        "event_and_available_times",
    }
)
_REVISION_POLICIES = frozenset(
    {
        "append_new_release_preserve_prior",
        "append_correction_version_preserve_prior",
    }
)
_PREPARATION_BOUNDARY = "externally_prepared_offline_immutable_input_only"
_CREDENTIAL_BOUNDARY = "credentials_external_to_research_distribution"
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "catalog_id",
        "version",
        "approved_at",
        "approved_by",
        "entries",
        "catalog_hash",
    }
)
_ENTRY_FIELDS = frozenset(
    {
        "provider_id",
        "display_name",
        "data_kinds",
        "frequencies",
        "source_kinds",
        "point_in_time_policy",
        "revision_policy",
        "license_id",
        "research_use_terms",
        "redistribution_allowed",
        "quality_level",
        "preparation_boundary",
        "credential_boundary",
        "owner",
        "expected_delivery_lag_seconds",
        "maximum_staleness_seconds",
    }
)


class SourceCatalogError(ValueError):
    """A provider catalog is incomplete, unsafe, or hash-inconsistent."""


@dataclass(frozen=True, slots=True)
class SourceCatalogEntry:
    provider_id: str
    display_name: str
    data_kinds: tuple[str, ...]
    frequencies: tuple[str, ...]
    source_kinds: tuple[str, ...]
    point_in_time_policy: str
    revision_policy: str
    license_id: str
    research_use_terms: str
    redistribution_allowed: bool
    quality_level: str
    preparation_boundary: str
    credential_boundary: str
    owner: str
    expected_delivery_lag_seconds: float
    maximum_staleness_seconds: float

    def __post_init__(self) -> None:
        _require_id(self.provider_id, "provider_id")
        for text_value, label in (
            (self.display_name, "display_name"),
            (self.license_id, "license_id"),
            (self.research_use_terms, "research_use_terms"),
            (self.owner, "owner"),
        ):
            _require_text(text_value, label)
        _require_sorted_unique(self.data_kinds, "data_kinds")
        _require_sorted_unique(self.frequencies, "frequencies")
        _require_sorted_unique(self.source_kinds, "source_kinds")
        if not set(self.source_kinds).issubset(_SOURCE_KINDS):
            raise SourceCatalogError("source_catalog_source_kind_invalid")
        if self.point_in_time_policy not in _PIT_POLICIES:
            raise SourceCatalogError("source_catalog_point_in_time_policy_invalid")
        if self.revision_policy not in _REVISION_POLICIES:
            raise SourceCatalogError("source_catalog_revision_policy_invalid")
        if not isinstance(self.redistribution_allowed, bool):
            raise SourceCatalogError("source_catalog_redistribution_flag_invalid")
        if self.quality_level not in _QUALITY_LEVELS:
            raise SourceCatalogError("source_catalog_quality_level_invalid")
        if self.preparation_boundary != _PREPARATION_BOUNDARY:
            raise SourceCatalogError("source_catalog_preparation_boundary_invalid")
        if self.credential_boundary != _CREDENTIAL_BOUNDARY:
            raise SourceCatalogError("source_catalog_credential_boundary_invalid")
        for number, label, strictly_positive in (
            (
                self.expected_delivery_lag_seconds,
                "expected_delivery_lag_seconds",
                False,
            ),
            (
                self.maximum_staleness_seconds,
                "maximum_staleness_seconds",
                True,
            ),
        ):
            if (
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not isfinite(float(number))
                or float(number) < 0
                or (strictly_positive and float(number) == 0)
            ):
                raise SourceCatalogError(f"source_catalog_{label}_invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "data_kinds": list(self.data_kinds),
            "frequencies": list(self.frequencies),
            "source_kinds": list(self.source_kinds),
            "point_in_time_policy": self.point_in_time_policy,
            "revision_policy": self.revision_policy,
            "license_id": self.license_id,
            "research_use_terms": self.research_use_terms,
            "redistribution_allowed": self.redistribution_allowed,
            "quality_level": self.quality_level,
            "preparation_boundary": self.preparation_boundary,
            "credential_boundary": self.credential_boundary,
            "owner": self.owner,
            "expected_delivery_lag_seconds": self.expected_delivery_lag_seconds,
            "maximum_staleness_seconds": self.maximum_staleness_seconds,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="source_catalog_entry")


@dataclass(frozen=True, slots=True)
class SourceCatalog:
    schema_version: int
    catalog_id: str
    version: str
    approved_at: str
    approved_by: str
    entries: tuple[SourceCatalogEntry, ...]
    catalog_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != SOURCE_CATALOG_SCHEMA_VERSION:
            raise SourceCatalogError("source_catalog_schema_version_unsupported")
        _require_id(self.catalog_id, "catalog_id")
        _require_id(self.version, "version")
        _require_timestamp(self.approved_at, "approved_at")
        _require_text(self.approved_by, "approved_by")
        if not self.entries:
            raise SourceCatalogError("source_catalog_entries_required")
        provider_ids = tuple(item.provider_id for item in self.entries)
        if provider_ids != tuple(sorted(provider_ids)):
            raise SourceCatalogError("source_catalog_entries_not_sorted")
        if len(provider_ids) != len(set(provider_ids)):
            raise SourceCatalogError("source_catalog_provider_duplicate")
        _require_hash(self.catalog_hash, "catalog_hash")
        if self.catalog_hash != source_catalog_hash(self.identity_payload()):
            raise SourceCatalogError("source_catalog_hash_mismatch")

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "catalog_id": self.catalog_id,
            "version": self.version,
            "approved_at": self.approved_at,
            "approved_by": self.approved_by,
            "entries": [item.as_dict() for item in self.entries],
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "catalog_hash": self.catalog_hash}

    def resolve(self, provider_id: str) -> SourceCatalogEntry:
        matches = [item for item in self.entries if item.provider_id == provider_id]
        if len(matches) != 1:
            raise SourceCatalogError("source_catalog_provider_not_registered")
        return matches[0]


def source_catalog_hash(payload: Mapping[str, object]) -> str:
    material = {key: value for key, value in payload.items() if key != "catalog_hash"}
    return sha256_prefixed(material, label="source_catalog")


def build_source_catalog(
    *,
    catalog_id: str,
    version: str,
    approved_at: str,
    approved_by: str,
    entries: Iterable[Mapping[str, object]],
) -> SourceCatalog:
    payload: dict[str, Any] = {
        "schema_version": SOURCE_CATALOG_SCHEMA_VERSION,
        "catalog_id": catalog_id,
        "version": version,
        "approved_at": approved_at,
        "approved_by": approved_by,
        "entries": sorted(
            (dict(item) for item in entries),
            key=lambda item: str(item.get("provider_id")),
        ),
    }
    payload["catalog_hash"] = source_catalog_hash(payload)
    return parse_source_catalog(payload)


def parse_source_catalog(value: object) -> SourceCatalog:
    if not isinstance(value, dict):
        raise SourceCatalogError("source_catalog_must_be_object")
    _reject_unknown(value, _TOP_LEVEL_FIELDS, "source_catalog")
    raw_entries = value.get("entries")
    if not isinstance(raw_entries, list):
        raise SourceCatalogError("source_catalog_entries_required")
    entries = tuple(_parse_entry(item) for item in raw_entries)
    return SourceCatalog(
        schema_version=_strict_int(value.get("schema_version"), "schema_version"),
        catalog_id=_text(value.get("catalog_id"), "catalog_id"),
        version=_text(value.get("version"), "version"),
        approved_at=_text(value.get("approved_at"), "approved_at"),
        approved_by=_text(value.get("approved_by"), "approved_by"),
        entries=entries,
        catalog_hash=_text(value.get("catalog_hash"), "catalog_hash"),
    )


def _parse_entry(value: object) -> SourceCatalogEntry:
    if not isinstance(value, dict):
        raise SourceCatalogError("source_catalog_entry_must_be_object")
    _reject_unknown(value, _ENTRY_FIELDS, "source_catalog_entry")
    redistribution = value.get("redistribution_allowed")
    if not isinstance(redistribution, bool):
        raise SourceCatalogError("source_catalog_redistribution_flag_invalid")
    return SourceCatalogEntry(
        provider_id=_text(value.get("provider_id"), "provider_id"),
        display_name=_text(value.get("display_name"), "display_name"),
        data_kinds=_text_tuple(value.get("data_kinds"), "data_kinds"),
        frequencies=_text_tuple(value.get("frequencies"), "frequencies"),
        source_kinds=_text_tuple(value.get("source_kinds"), "source_kinds"),
        point_in_time_policy=_text(
            value.get("point_in_time_policy"), "point_in_time_policy"
        ),
        revision_policy=_text(value.get("revision_policy"), "revision_policy"),
        license_id=_text(value.get("license_id"), "license_id"),
        research_use_terms=_text(value.get("research_use_terms"), "research_use_terms"),
        redistribution_allowed=redistribution,
        quality_level=_text(value.get("quality_level"), "quality_level"),
        preparation_boundary=_text(
            value.get("preparation_boundary"), "preparation_boundary"
        ),
        credential_boundary=_text(
            value.get("credential_boundary"), "credential_boundary"
        ),
        owner=_text(value.get("owner"), "owner"),
        expected_delivery_lag_seconds=_number(
            value.get("expected_delivery_lag_seconds"),
            "expected_delivery_lag_seconds",
        ),
        maximum_staleness_seconds=_number(
            value.get("maximum_staleness_seconds"), "maximum_staleness_seconds"
        ),
    )


def _reject_unknown(value: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise SourceCatalogError(f"{label}_unknown_field:{','.join(unknown)}")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise SourceCatalogError(f"source_catalog_{label}_invalid")
    return value


def _text_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SourceCatalogError(f"source_catalog_{label}_invalid")
    return tuple(_text(item, label) for item in value)


def _require_text(value: object, label: str) -> None:
    _text(value, label)


def _require_id(value: object, label: str) -> None:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise SourceCatalogError(f"source_catalog_{label}_invalid")


def _require_sorted_unique(values: tuple[str, ...], label: str) -> None:
    if not values or values != tuple(sorted(values)) or len(values) != len(set(values)):
        raise SourceCatalogError(f"source_catalog_{label}_invalid")
    for value in values:
        _require_text(value, label)


def _require_hash(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in value[7:])
    ):
        raise SourceCatalogError(f"source_catalog_{label}_invalid")


def _strict_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceCatalogError(f"source_catalog_{label}_invalid")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SourceCatalogError(f"source_catalog_{label}_invalid")
    return float(value)


def _require_timestamp(value: object, label: str) -> None:
    _require_text(value, label)
    assert isinstance(value, str)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceCatalogError(f"source_catalog_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SourceCatalogError(f"source_catalog_{label}_timezone_required")


__all__ = [
    "SOURCE_CATALOG_SCHEMA_VERSION",
    "SourceCatalog",
    "SourceCatalogEntry",
    "SourceCatalogError",
    "build_source_catalog",
    "parse_source_catalog",
    "source_catalog_hash",
]
