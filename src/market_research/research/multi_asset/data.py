"""Append-only, bitemporal data contracts for offline multi-asset research.

Records are supplied from externally prepared immutable artifacts.  The store
has no network, retry, probe, or backfill capability; appending returns a new
value and preserves every correction version for point-in-time replay.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias

from ..hashing import sha256_prefixed


MULTI_ASSET_DATA_SCHEMA_VERSION = 2
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_MODES = frozenset({"EXTERNALLY_PREPARED_IMMUTABLE", "MANUAL_REVIEWED_IMPORT"})


class MultiAssetDataError(ValueError):
    """Bitemporal data or its lineage is incomplete or inconsistent."""


class DataLayer(StrEnum):
    RAW = "RAW"
    NORMALIZED = "NORMALIZED"
    DERIVED = "DERIVED"


def _timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise MultiAssetDataError(f"{field}_invalid_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MultiAssetDataError(f"{field}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: str, field: str) -> str:
    return _timestamp(value, field).isoformat()


def _require_id(value: str, field: str) -> None:
    if not isinstance(value, str) or not _STABLE_ID.fullmatch(value):
        raise MultiAssetDataError(f"{field}_invalid_stable_id")


def _require_hash(value: str, field: str) -> None:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise MultiAssetDataError(f"{field}_invalid_hash")


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise MultiAssetDataError("payload_decimal_non_finite")
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


JsonScalar: TypeAlias = str | int | float | bool | None
FrozenJson: TypeAlias = (
    JsonScalar | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]
)


def _freeze_json(value: object, field: str = "payload") -> FrozenJson:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise MultiAssetDataError(f"{field}_non_finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, FrozenJson] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise MultiAssetDataError(f"{field}_key_must_be_text")
            frozen[key] = _freeze_json(item, f"{field}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, f"{field}[]") for item in value)
    raise MultiAssetDataError(f"{field}_not_canonical_json")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ObservationClocks:
    """Distinct economic, public, revision, receipt, and ingestion clocks."""

    event_at: str
    knowledge_at: str
    revision_at: str
    received_at: str
    ingested_at: str

    def __post_init__(self) -> None:
        values = {
            "event_at": _timestamp_text(self.event_at, "clocks.event_at"),
            "knowledge_at": _timestamp_text(self.knowledge_at, "clocks.knowledge_at"),
            "revision_at": _timestamp_text(self.revision_at, "clocks.revision_at"),
            "received_at": _timestamp_text(self.received_at, "clocks.received_at"),
            "ingested_at": _timestamp_text(self.ingested_at, "clocks.ingested_at"),
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)
        received = _timestamp(values["received_at"], "clocks.received_at")
        if received < _timestamp(values["knowledge_at"], "clocks.knowledge_at"):
            raise MultiAssetDataError("clocks_received_before_knowledge")
        if received < _timestamp(values["revision_at"], "clocks.revision_at"):
            raise MultiAssetDataError("clocks_received_before_revision")
        if _timestamp(values["ingested_at"], "clocks.ingested_at") < received:
            raise MultiAssetDataError("clocks_ingested_before_received")

    def available_at(self, as_of: str) -> bool:
        cutoff = _timestamp(as_of, "clocks.as_of")
        return all(
            _timestamp(value, f"clocks.{name}") <= cutoff
            for name, value in (
                ("knowledge_at", self.knowledge_at),
                ("revision_at", self.revision_at),
                ("received_at", self.received_at),
                ("ingested_at", self.ingested_at),
            )
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "event_at": self.event_at,
            "knowledge_at": self.knowledge_at,
            "revision_at": self.revision_at,
            "received_at": self.received_at,
            "ingested_at": self.ingested_at,
        }


@dataclass(frozen=True, slots=True)
class DataLineage:
    """Hash bindings for an external source and all upstream records."""

    source_id: str
    source_version: str
    source_artifact_hash: str
    source_schema_hash: str
    upstream_record_hashes: tuple[str, ...] = ()
    transformation_id: str | None = None
    transformation_version: str | None = None
    parameters_hash: str | None = None
    source_mode: str = "EXTERNALLY_PREPARED_IMMUTABLE"

    def __post_init__(self) -> None:
        _require_id(self.source_id, "lineage.source_id")
        _require_id(self.source_version, "lineage.source_version")
        _require_hash(self.source_artifact_hash, "lineage.source_artifact_hash")
        _require_hash(self.source_schema_hash, "lineage.source_schema_hash")
        upstream = tuple(sorted(self.upstream_record_hashes))
        if len(set(upstream)) != len(upstream):
            raise MultiAssetDataError("lineage_upstream_hash_duplicate")
        for item in upstream:
            _require_hash(item, "lineage.upstream_record_hash")
        object.__setattr__(self, "upstream_record_hashes", upstream)
        transform_fields = (
            self.transformation_id,
            self.transformation_version,
            self.parameters_hash,
        )
        if any(item is not None for item in transform_fields) and any(
            item is None for item in transform_fields
        ):
            raise MultiAssetDataError("lineage_transformation_binding_incomplete")
        if self.transformation_id is not None:
            _require_id(self.transformation_id, "lineage.transformation_id")
            if self.transformation_version is None or self.parameters_hash is None:
                raise MultiAssetDataError("lineage_transformation_binding_incomplete")
            _require_id(
                self.transformation_version,
                "lineage.transformation_version",
            )
            _require_hash(self.parameters_hash, "lineage.parameters_hash")
        if self.source_mode not in _SOURCE_MODES:
            raise MultiAssetDataError("network_source_collection_forbidden")

    def as_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "source_version": self.source_version,
            "source_artifact_hash": self.source_artifact_hash,
            "source_schema_hash": self.source_schema_hash,
            "upstream_record_hashes": list(self.upstream_record_hashes),
            "transformation_id": self.transformation_id,
            "transformation_version": self.transformation_version,
            "parameters_hash": self.parameters_hash,
            "source_mode": self.source_mode,
        }

    def lineage_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="multi_asset_data_lineage")


@dataclass(frozen=True, slots=True)
class BitemporalRecord:
    """One immutable version of a logical observation."""

    record_id: str
    version: int
    layer: DataLayer
    instrument_id: str
    data_kind: str
    clocks: ObservationClocks
    payload: Mapping[str, object]
    lineage: DataLineage
    supersedes_hash: str | None = None
    correction_reason: str | None = None
    schema_version: int = MULTI_ASSET_DATA_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MULTI_ASSET_DATA_SCHEMA_VERSION:
            raise MultiAssetDataError("data_record_schema_unsupported")
        _require_id(self.record_id, "record.record_id")
        if isinstance(self.version, bool) or self.version < 1:
            raise MultiAssetDataError("record.version_invalid")
        if not isinstance(self.layer, DataLayer):
            raise MultiAssetDataError("record.layer_invalid")
        _require_id(self.instrument_id, "record.instrument_id")
        _require_id(self.data_kind, "record.data_kind")
        frozen_payload = _freeze_json(self.payload)
        if not isinstance(frozen_payload, Mapping):
            raise MultiAssetDataError("record.payload_must_be_object")
        object.__setattr__(self, "payload", frozen_payload)
        if self.version == 1:
            if self.supersedes_hash is not None or self.correction_reason is not None:
                raise MultiAssetDataError("record_initial_version_correction_invalid")
        else:
            if self.supersedes_hash is None:
                raise MultiAssetDataError("record_correction_supersedes_hash_required")
            _require_hash(self.supersedes_hash, "record.supersedes_hash")
            if self.correction_reason is None or not self.correction_reason.strip():
                raise MultiAssetDataError("record_correction_reason_required")
        if self.layer is DataLayer.RAW and self.lineage.upstream_record_hashes:
            raise MultiAssetDataError("raw_record_upstream_forbidden")
        if self.layer is not DataLayer.RAW:
            if not self.lineage.upstream_record_hashes:
                raise MultiAssetDataError("processed_record_upstream_required")
            if self.lineage.transformation_id is None:
                raise MultiAssetDataError("processed_record_transform_required")

    @property
    def logical_key(self) -> tuple[DataLayer, str]:
        return (self.layer, self.record_id)

    def payload_hash(self) -> str:
        return sha256_prefixed(
            _thaw_json(self.payload), label="multi_asset_data_payload"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "record_id": self.record_id,
            "version": self.version,
            "layer": self.layer.value,
            "instrument_id": self.instrument_id,
            "data_kind": self.data_kind,
            "clocks": self.clocks.as_dict(),
            "payload": _thaw_json(self.payload),
            "payload_hash": self.payload_hash(),
            "lineage": self.lineage.as_dict(),
            "lineage_hash": self.lineage.lineage_hash(),
            "supersedes_hash": self.supersedes_hash,
            "correction_reason": self.correction_reason,
        }

    def record_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="multi_asset_data_record")


@dataclass(frozen=True, slots=True)
class AppendOnlyBitemporalStore:
    """Functional append-only store with no mutation or physical I/O authority."""

    records: tuple[BitemporalRecord, ...] = ()
    schema_version: int = MULTI_ASSET_DATA_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MULTI_ASSET_DATA_SCHEMA_VERSION:
            raise MultiAssetDataError("data_store_schema_unsupported")
        records = tuple(self.records)
        object.__setattr__(self, "records", records)
        accepted: list[BitemporalRecord] = []
        for record in records:
            self._validate_next(record, accepted)
            accepted.append(record)

    @staticmethod
    def _validate_next(
        record: BitemporalRecord, accepted: list[BitemporalRecord]
    ) -> None:
        hashes = {item.record_hash(): item for item in accepted}
        record_hash = record.record_hash()
        if record_hash in hashes:
            raise MultiAssetDataError("record_hash_duplicate")
        prior_versions = [
            item for item in accepted if item.logical_key == record.logical_key
        ]
        if not prior_versions:
            if record.version != 1:
                raise MultiAssetDataError("record_first_version_must_equal_one")
        else:
            prior = prior_versions[-1]
            if record.version != prior.version + 1:
                raise MultiAssetDataError("record_correction_version_not_sequential")
            if record.supersedes_hash != prior.record_hash():
                raise MultiAssetDataError("record_correction_supersedes_mismatch")
            if record.instrument_id != prior.instrument_id:
                raise MultiAssetDataError("record_correction_instrument_changed")
            if record.data_kind != prior.data_kind:
                raise MultiAssetDataError("record_correction_data_kind_changed")
            if record.clocks.event_at != prior.clocks.event_at:
                raise MultiAssetDataError("record_correction_event_time_changed")
            for name in (
                "knowledge_at",
                "revision_at",
                "received_at",
                "ingested_at",
            ):
                if _timestamp(getattr(record.clocks, name), f"clocks.{name}") < (
                    _timestamp(getattr(prior.clocks, name), f"clocks.{name}")
                ):
                    raise MultiAssetDataError(
                        f"record_correction_{name}_moved_backward"
                    )

        upstream_records: list[BitemporalRecord] = []
        for upstream_hash in record.lineage.upstream_record_hashes:
            upstream = hashes.get(upstream_hash)
            if upstream is None:
                raise MultiAssetDataError(
                    f"lineage_upstream_record_missing:{upstream_hash}"
                )
            upstream_records.append(upstream)
        upstream_layers = {item.layer for item in upstream_records}
        if record.layer is DataLayer.NORMALIZED and (
            not upstream_layers or upstream_layers != {DataLayer.RAW}
        ):
            raise MultiAssetDataError("normalized_lineage_must_reference_raw")
        if record.layer is DataLayer.DERIVED and (
            not upstream_layers
            or not upstream_layers.issubset({DataLayer.NORMALIZED, DataLayer.DERIVED})
        ):
            raise MultiAssetDataError(
                "derived_lineage_must_reference_processed_records"
            )

    def append(self, record: BitemporalRecord) -> AppendOnlyBitemporalStore:
        """Return a new store; the original append stream remains unchanged."""

        return AppendOnlyBitemporalStore(
            records=self.records + (record,), schema_version=self.schema_version
        )

    def query_as_of(
        self,
        *,
        event_as_of: str,
        knowledge_as_of: str,
        revision_as_of: str | None = None,
        received_as_of: str | None = None,
        ingested_as_of: str | None = None,
        layer: DataLayer | None = None,
        instrument_id: str | None = None,
        data_kind: str | None = None,
    ) -> tuple[BitemporalRecord, ...]:
        """Return latest versions that were both economic and locally available.

        Unspecified revision/receipt/ingestion cutoffs default to the knowledge
        cutoff.  This conservative default prevents a later correction or a row
        ingested after a simulated decision from leaking into the replay.
        """

        cutoffs = {
            "event_at": _timestamp(event_as_of, "query.event_as_of"),
            "knowledge_at": _timestamp(knowledge_as_of, "query.knowledge_as_of"),
            "revision_at": _timestamp(
                revision_as_of or knowledge_as_of, "query.revision_as_of"
            ),
            "received_at": _timestamp(
                received_as_of or knowledge_as_of, "query.received_as_of"
            ),
            "ingested_at": _timestamp(
                ingested_as_of or knowledge_as_of, "query.ingested_as_of"
            ),
        }
        eligible: list[BitemporalRecord] = []
        for record in self.records:
            if layer is not None and record.layer is not layer:
                continue
            if instrument_id is not None and record.instrument_id != instrument_id:
                continue
            if data_kind is not None and record.data_kind != data_kind:
                continue
            if any(
                _timestamp(getattr(record.clocks, name), f"clocks.{name}") > cutoff
                for name, cutoff in cutoffs.items()
            ):
                continue
            eligible.append(record)
        latest: dict[tuple[DataLayer, str], BitemporalRecord] = {}
        for record in eligible:
            prior = latest.get(record.logical_key)
            if prior is None or record.version > prior.version:
                latest[record.logical_key] = record
        return tuple(
            sorted(
                latest.values(),
                key=lambda item: (item.layer.value, item.record_id),
            )
        )

    def as_of(
        self,
        as_of: str,
        *,
        event_as_of: str | None = None,
        layer: DataLayer | None = None,
    ) -> tuple[BitemporalRecord, ...]:
        return self.query_as_of(
            event_as_of=event_as_of or as_of,
            knowledge_as_of=as_of,
            layer=layer,
        )

    point_in_time_query = query_as_of

    def correction_history(
        self, record_id: str, *, layer: DataLayer | None = None
    ) -> tuple[BitemporalRecord, ...]:
        _require_id(record_id, "history.record_id")
        return tuple(
            sorted(
                (
                    item
                    for item in self.records
                    if item.record_id == record_id
                    and (layer is None or item.layer is layer)
                ),
                key=lambda item: (item.layer.value, item.version),
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "append_semantics": "APPEND_ONLY",
            "records": [
                {
                    "record_hash": item.record_hash(),
                    "record": item.as_dict(),
                }
                for item in self.records
            ],
        }

    def content_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="multi_asset_bitemporal_store")

    def verify_content_hash(self, expected_hash: str) -> None:
        _require_hash(expected_hash, "store.expected_hash")
        if self.content_hash() != expected_hash:
            raise MultiAssetDataError("data_store_content_hash_mismatch")


DataRecord = BitemporalRecord
BitemporalDataStore = AppendOnlyBitemporalStore
PointInTimeDataStore = AppendOnlyBitemporalStore
