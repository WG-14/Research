"""Strict provenance contract for externally prepared candle datasets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable

from ..hashing import sha256_prefixed


SOURCE_PROVENANCE_SCHEMA_VERSION = 2
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "artifact_type",
        "sources",
        "source_priority",
        "semantics",
        "lineage",
        "provenance_manifest_hash",
    }
)
_SOURCE_FIELDS = frozenset(
    {
        "provider_id",
        "dataset_id",
        "release_id",
        "source_kind",
        "request_parameters",
        "requested_at",
        "received_at",
        "response_version",
        "acquisition_code_version",
        "retry_count",
        "acquisition_status",
        "error_code",
        "coverage_start_ts",
        "coverage_end_ts",
        "content_hash",
    }
)
_SEMANTICS_FIELDS = frozenset(
    {
        "asset_class",
        "instrument_scope",
        "observation_calendar",
        "timezone",
        "price_adjustment",
        "corporate_actions",
        "universe",
    }
)
_LINEAGE_FIELDS = frozenset(
    {
        "layer",
        "artifact_id",
        "content_hash",
        "schema_version",
        "transformation_id",
    }
)
_REQUIRED_LAYERS = ("raw", "cleaned", "standardized")
_REQUIRED_SEMANTICS = {
    "asset_class": "spot",
    "instrument_scope": "single_instrument",
    "observation_calendar": "continuous_24x7",
    "timezone": "UTC",
    "price_adjustment": "not_applicable",
    "corporate_actions": "not_applicable",
    "universe": "not_applicable",
}
_SOURCE_KINDS = frozenset(
    {"external_api", "file_export", "object_snapshot", "vendor_archive"}
)
_ACQUISITION_STATUSES = frozenset({"complete", "partial", "failed"})
_SENSITIVE_PARAMETER_TOKENS = (
    "api_key",
    "authorization",
    "password",
    "secret",
    "signature",
    "token",
)


class SourceProvenanceError(ValueError):
    pass


@dataclass(frozen=True)
class SourceRecord:
    provider_id: str
    dataset_id: str
    release_id: str
    source_kind: str
    request_parameters: tuple[tuple[str, str], ...]
    requested_at: str
    received_at: str
    response_version: str
    acquisition_code_version: str
    retry_count: int
    acquisition_status: str
    error_code: str
    coverage_start_ts: int
    coverage_end_ts: int
    content_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "dataset_id": self.dataset_id,
            "release_id": self.release_id,
            "source_kind": self.source_kind,
            "request_parameters": dict(self.request_parameters),
            "requested_at": self.requested_at,
            "received_at": self.received_at,
            "response_version": self.response_version,
            "acquisition_code_version": self.acquisition_code_version,
            "retry_count": self.retry_count,
            "acquisition_status": self.acquisition_status,
            "error_code": self.error_code,
            "coverage_start_ts": self.coverage_start_ts,
            "coverage_end_ts": self.coverage_end_ts,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class LineageStage:
    layer: str
    artifact_id: str
    content_hash: str
    schema_version: int
    transformation_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "layer": self.layer,
            "artifact_id": self.artifact_id,
            "content_hash": self.content_hash,
            "schema_version": self.schema_version,
            "transformation_id": self.transformation_id,
        }


@dataclass(frozen=True)
class DatasetSourceProvenance:
    schema_version: int
    artifact_type: str
    sources: tuple[SourceRecord, ...]
    source_priority: tuple[str, ...]
    semantics: tuple[tuple[str, str], ...]
    lineage: tuple[LineageStage, ...]
    provenance_manifest_hash: str

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": self.artifact_type,
            "sources": [source.as_dict() for source in self.sources],
            "source_priority": list(self.source_priority),
            "semantics": dict(self.semantics),
            "lineage": [stage.as_dict() for stage in self.lineage],
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.identity_payload(),
            "provenance_manifest_hash": self.provenance_manifest_hash,
        }


def source_provenance_hash(payload: dict[str, Any]) -> str:
    material = {
        key: value
        for key, value in payload.items()
        if key != "provenance_manifest_hash"
    }
    return sha256_prefixed(
        {"hash_domain": "dataset_source_provenance_v2", "provenance": material},
        label="dataset_source_provenance_hash",
    )


def build_dataset_source_provenance(
    *,
    sources: Iterable[dict[str, object]],
    source_priority: Iterable[str],
    lineage: Iterable[dict[str, object]],
    semantics: dict[str, str] | None = None,
) -> DatasetSourceProvenance:
    payload: dict[str, Any] = {
        "schema_version": SOURCE_PROVENANCE_SCHEMA_VERSION,
        "artifact_type": "dataset_source_provenance",
        "sources": list(sources),
        "source_priority": list(source_priority),
        "semantics": dict(semantics or _REQUIRED_SEMANTICS),
        "lineage": list(lineage),
    }
    payload["provenance_manifest_hash"] = source_provenance_hash(payload)
    return parse_dataset_source_provenance(payload)


def load_dataset_source_provenance(path: str | Path) -> DatasetSourceProvenance:
    manifest_path = Path(path).expanduser()
    if not manifest_path.is_absolute():
        raise SourceProvenanceError("source_provenance_uri_must_be_absolute")
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceProvenanceError("source_provenance_unavailable") from exc
    return parse_dataset_source_provenance(value)


def parse_dataset_source_provenance(payload: Any) -> DatasetSourceProvenance:
    if not isinstance(payload, dict):
        raise SourceProvenanceError("source_provenance_must_be_object")
    _reject_unknown(payload, _TOP_LEVEL_FIELDS, "source_provenance")
    if payload.get("schema_version") != SOURCE_PROVENANCE_SCHEMA_VERSION:
        raise SourceProvenanceError("source_provenance_schema_version_unsupported")
    if payload.get("artifact_type") != "dataset_source_provenance":
        raise SourceProvenanceError("source_provenance_artifact_type_unsupported")
    expected_hash = _hash(payload.get("provenance_manifest_hash"))
    if source_provenance_hash(payload) != expected_hash:
        raise SourceProvenanceError("source_provenance_hash_mismatch")

    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise SourceProvenanceError("source_provenance_sources_required")
    sources = tuple(_parse_source(value) for value in raw_sources)
    provider_ids = tuple(source.provider_id for source in sources)
    if len(set(provider_ids)) != len(provider_ids):
        raise SourceProvenanceError("source_provenance_provider_id_duplicate")

    raw_priority = payload.get("source_priority")
    if not isinstance(raw_priority, list) or not raw_priority:
        raise SourceProvenanceError("source_provenance_priority_required")
    priority = tuple(_text(value, "source_priority") for value in raw_priority)
    if len(set(priority)) != len(priority) or set(priority) != set(provider_ids):
        raise SourceProvenanceError("source_provenance_priority_must_order_all_sources")

    raw_semantics = payload.get("semantics")
    if not isinstance(raw_semantics, dict):
        raise SourceProvenanceError("source_provenance_semantics_required")
    _reject_unknown(raw_semantics, _SEMANTICS_FIELDS, "source_provenance.semantics")
    semantics = {
        key: _text(raw_semantics.get(key), f"semantics.{key}")
        for key in _SEMANTICS_FIELDS
    }
    if semantics != _REQUIRED_SEMANTICS:
        raise SourceProvenanceError(
            "source_provenance_semantics_outside_supported_scope"
        )

    raw_lineage = payload.get("lineage")
    if not isinstance(raw_lineage, list):
        raise SourceProvenanceError("source_provenance_lineage_required")
    lineage = tuple(_parse_lineage(value) for value in raw_lineage)
    if tuple(stage.layer for stage in lineage) != _REQUIRED_LAYERS:
        raise SourceProvenanceError(
            "source_provenance_lineage_layers_must_be_raw_cleaned_standardized"
        )

    return DatasetSourceProvenance(
        schema_version=SOURCE_PROVENANCE_SCHEMA_VERSION,
        artifact_type="dataset_source_provenance",
        sources=sources,
        source_priority=priority,
        semantics=tuple(sorted(semantics.items())),
        lineage=lineage,
        provenance_manifest_hash=expected_hash,
    )


def validate_source_coverage(
    provenance: DatasetSourceProvenance, *, start_ts: int, end_ts: int
) -> None:
    for source in provenance.sources:
        if source.acquisition_status != "complete":
            raise SourceProvenanceError("source_provenance_source_not_complete")
        if (
            int(start_ts) < source.coverage_start_ts
            or int(end_ts) > source.coverage_end_ts
        ):
            raise SourceProvenanceError(
                "source_provenance_requested_range_outside_source_coverage"
            )


def _parse_source(value: Any) -> SourceRecord:
    if not isinstance(value, dict):
        raise SourceProvenanceError("source_provenance_source_must_be_object")
    _reject_unknown(value, _SOURCE_FIELDS, "source_provenance.source")
    start_ts = _strict_int(value.get("coverage_start_ts"), "source.coverage_start_ts")
    end_ts = _strict_int(value.get("coverage_end_ts"), "source.coverage_end_ts")
    if start_ts > end_ts:
        raise SourceProvenanceError("source_provenance_source_coverage_inverted")
    source_kind = _text(value.get("source_kind"), "source.source_kind")
    if source_kind not in _SOURCE_KINDS:
        raise SourceProvenanceError("source_provenance_source_kind_invalid")
    parameters = _request_parameters(value.get("request_parameters"))
    requested_at = _utc_datetime(value.get("requested_at"), "source.requested_at")
    received_at = _utc_datetime(value.get("received_at"), "source.received_at")
    if received_at < requested_at:
        raise SourceProvenanceError("source_provenance_received_before_requested")
    retry_count = _strict_int(value.get("retry_count"), "source.retry_count")
    if retry_count < 0:
        raise SourceProvenanceError("source_provenance_retry_count_negative")
    status = _text(value.get("acquisition_status"), "source.acquisition_status")
    if status not in _ACQUISITION_STATUSES:
        raise SourceProvenanceError("source_provenance_acquisition_status_invalid")
    raw_error = value.get("error_code")
    if not isinstance(raw_error, str):
        raise SourceProvenanceError("source_provenance_error_code_invalid")
    error_code = raw_error.strip()
    if status == "complete" and error_code:
        raise SourceProvenanceError("source_provenance_complete_source_has_error")
    if status != "complete" and not error_code:
        raise SourceProvenanceError(
            "source_provenance_incomplete_source_requires_error"
        )
    return SourceRecord(
        provider_id=_text(value.get("provider_id"), "source.provider_id"),
        dataset_id=_text(value.get("dataset_id"), "source.dataset_id"),
        release_id=_text(value.get("release_id"), "source.release_id"),
        source_kind=source_kind,
        request_parameters=parameters,
        requested_at=requested_at.isoformat().replace("+00:00", "Z"),
        received_at=received_at.isoformat().replace("+00:00", "Z"),
        response_version=_text(
            value.get("response_version"), "source.response_version"
        ),
        acquisition_code_version=_text(
            value.get("acquisition_code_version"),
            "source.acquisition_code_version",
        ),
        retry_count=retry_count,
        acquisition_status=status,
        error_code=error_code,
        coverage_start_ts=start_ts,
        coverage_end_ts=end_ts,
        content_hash=_hash(value.get("content_hash")),
    )


def _parse_lineage(value: Any) -> LineageStage:
    if not isinstance(value, dict):
        raise SourceProvenanceError("source_provenance_lineage_stage_must_be_object")
    _reject_unknown(value, _LINEAGE_FIELDS, "source_provenance.lineage")
    version = _strict_int(value.get("schema_version"), "lineage.schema_version")
    if version <= 0:
        raise SourceProvenanceError("source_provenance_lineage_schema_version_invalid")
    return LineageStage(
        layer=_text(value.get("layer"), "lineage.layer"),
        artifact_id=_text(value.get("artifact_id"), "lineage.artifact_id"),
        content_hash=_hash(value.get("content_hash")),
        schema_version=version,
        transformation_id=_text(
            value.get("transformation_id"), "lineage.transformation_id"
        ),
    )


def _reject_unknown(
    value: dict[str, Any], allowed: frozenset[str], context: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise SourceProvenanceError(f"{context}_unknown_field:{','.join(unknown)}")


def _hash(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
    ):
        raise SourceProvenanceError("source_provenance_hash_invalid")
    if any(char not in "0123456789abcdef" for char in value[7:]):
        raise SourceProvenanceError("source_provenance_hash_invalid")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceProvenanceError(f"source_provenance_{label}_invalid")
    return value.strip()


def _strict_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceProvenanceError(f"source_provenance_{label}_invalid")
    return int(value)


def _utc_datetime(value: Any, label: str) -> datetime:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceProvenanceError(f"source_provenance_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise SourceProvenanceError(f"source_provenance_{label}_must_be_utc")
    return parsed


def _request_parameters(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict) or not value:
        raise SourceProvenanceError("source_provenance_request_parameters_invalid")
    normalized: list[tuple[str, str]] = []
    for raw_key, raw_value in value.items():
        key = _text(raw_key, "source.request_parameter_key")
        lowered = key.lower()
        if any(token in lowered for token in _SENSITIVE_PARAMETER_TOKENS):
            raise SourceProvenanceError(
                "source_provenance_request_parameters_sensitive"
            )
        normalized.append((key, _text(raw_value, f"source.request_parameters.{key}")))
    keys = [key for key, _value in normalized]
    if len(keys) != len(set(keys)):
        raise SourceProvenanceError("source_provenance_request_parameters_duplicate")
    return tuple(sorted(normalized))
