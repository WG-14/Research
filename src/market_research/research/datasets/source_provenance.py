"""Strict provenance contract for externally prepared candle datasets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from market_research.paths import ResearchPathManager

from ..hashing import canonical_json_bytes, sha256_prefixed
from .source_catalog import (
    SourceCatalog,
    SourceCatalogError,
    parse_source_catalog,
)


SOURCE_PROVENANCE_SCHEMA_VERSION = 4
TRANSFORMATION_RECEIPT_SCHEMA_VERSION = 1
TRANSFORMATION_TRUST_STORE_SCHEMA_VERSION = 1
STANDARDIZED_CANONICALIZATION_ID = "market_research.artifact_content_v2"
_MAX_TRANSFORMATION_RECEIPT_BYTES = 1024 * 1024
_MAX_TRANSFORMATION_TRUST_STORE_BYTES = 256 * 1024
_MAX_TRANSFORMATION_PUBLIC_KEY_BYTES = 256
TRANSFORMATION_RECEIPT_SIGNATURE_DOMAIN = (
    b"market-research:dataset-transformation-receipt:v1\x00"
)
TRANSFORMATION_RECEIPT_SIGNATURE_ALGORITHM = "ed25519"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,254}$")
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "artifact_type",
        "source_catalog",
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
        "artifact_uri",
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
        "artifact_uri",
        "content_hash",
        "schema_version",
        "transformation_id",
        "transformation_receipt_uri",
        "transformation_receipt_content_hash",
        "code_artifact_uri",
        "config_artifact_uri",
        "canonicalization_id",
        "canonical_content_hash",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "artifact_type",
        "layer",
        "transformation_id",
        "input_artifacts",
        "output_artifact",
        "authority_id",
        "key_id",
        "signed_at",
        "code_artifact_id",
        "code_hash",
        "config_artifact_id",
        "config_hash",
        "output_schema_version",
        "output_canonicalization_id",
        "output_canonical_content_hash",
        "receipt_hash",
        "signature",
    }
)
_RECEIPT_ARTIFACT_FIELDS = frozenset({"artifact_id", "content_hash"})
_TRUST_STORE_FIELDS = frozenset(
    {
        "schema_version",
        "artifact_type",
        "authority_id",
        "issued_at",
        "expires_at",
        "keys",
    }
)
_TRUST_KEY_FIELDS = frozenset(
    {
        "key_id",
        "algorithm",
        "public_key_path",
        "public_key_content_hash",
        "valid_from",
        "valid_until",
        "revoked_at",
        "revocation_reason",
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
    artifact_uri: str
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
            "artifact_uri": self.artifact_uri,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class LineageStage:
    layer: str
    artifact_id: str
    artifact_uri: str
    content_hash: str
    schema_version: int
    transformation_id: str
    transformation_receipt_uri: str
    transformation_receipt_content_hash: str
    code_artifact_uri: str
    config_artifact_uri: str
    canonicalization_id: str | None
    canonical_content_hash: str | None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "layer": self.layer,
            "artifact_id": self.artifact_id,
            "artifact_uri": self.artifact_uri,
            "content_hash": self.content_hash,
            "schema_version": self.schema_version,
            "transformation_id": self.transformation_id,
            "transformation_receipt_uri": self.transformation_receipt_uri,
            "transformation_receipt_content_hash": (
                self.transformation_receipt_content_hash
            ),
            "code_artifact_uri": self.code_artifact_uri,
            "config_artifact_uri": self.config_artifact_uri,
        }
        if self.canonicalization_id is not None:
            payload["canonicalization_id"] = self.canonicalization_id
        if self.canonical_content_hash is not None:
            payload["canonical_content_hash"] = self.canonical_content_hash
        return payload


@dataclass(frozen=True)
class ReceiptArtifactBinding:
    artifact_id: str
    content_hash: str

    def as_dict(self) -> dict[str, str]:
        return {
            "artifact_id": self.artifact_id,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class TransformationReceipt:
    schema_version: int
    artifact_type: str
    layer: str
    transformation_id: str
    input_artifacts: tuple[ReceiptArtifactBinding, ...]
    output_artifact: ReceiptArtifactBinding
    authority_id: str
    key_id: str
    signed_at: str
    code_artifact_id: str
    code_hash: str
    config_artifact_id: str
    config_hash: str
    output_schema_version: int
    output_canonicalization_id: str | None
    output_canonical_content_hash: str | None
    receipt_hash: str
    signature: str

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": self.artifact_type,
            "layer": self.layer,
            "transformation_id": self.transformation_id,
            "input_artifacts": [item.as_dict() for item in self.input_artifacts],
            "output_artifact": self.output_artifact.as_dict(),
            "authority_id": self.authority_id,
            "key_id": self.key_id,
            "signed_at": self.signed_at,
            "code_artifact_id": self.code_artifact_id,
            "code_hash": self.code_hash,
            "config_artifact_id": self.config_artifact_id,
            "config_hash": self.config_hash,
            "output_schema_version": self.output_schema_version,
            "output_canonicalization_id": self.output_canonicalization_id,
            "output_canonical_content_hash": self.output_canonical_content_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.identity_payload(),
            "receipt_hash": self.receipt_hash,
            "signature": self.signature,
        }

    def signed_bytes(self) -> bytes:
        return TRANSFORMATION_RECEIPT_SIGNATURE_DOMAIN + canonical_json_bytes(
            {**self.identity_payload(), "receipt_hash": self.receipt_hash}
        )


@dataclass(frozen=True)
class TransformationTrustKey:
    key_id: str
    public_key_path: str
    public_key_content_hash: str
    valid_from: str
    valid_until: str
    revoked_at: str | None
    revocation_reason: str
    public_key: Ed25519PublicKey


@dataclass(frozen=True)
class TransformationTrustStore:
    """Administrator-pinned verification authority for transformation receipts.

    The store is runtime configuration, not dataset evidence.  Neither a
    provenance manifest nor a freeze caller can name a replacement key.  The
    loader below obtains the path and exact store hash only from
    ``ResearchSettings`` and verifies the separately located public-key bytes.
    """

    authority_id: str
    issued_at: str
    expires_at: str
    keys: tuple[TransformationTrustKey, ...]
    content_hash: str

    def verify_receipt(
        self,
        receipt: TransformationReceipt,
        *,
        now: datetime,
    ) -> None:
        current = _aware_utc(now, "transformation_trust_verification_time")
        issued_at = _utc_datetime(
            self.issued_at, "transformation_trust_store.issued_at"
        )
        expires_at = _utc_datetime(
            self.expires_at, "transformation_trust_store.expires_at"
        )
        if current < issued_at:
            raise SourceProvenanceError("transformation_trust_store_not_yet_valid")
        if current > expires_at:
            raise SourceProvenanceError("transformation_trust_store_expired")
        if receipt.authority_id != self.authority_id:
            raise SourceProvenanceError(
                "transformation_receipt_authority_not_trusted"
            )
        matches = tuple(key for key in self.keys if key.key_id == receipt.key_id)
        if len(matches) != 1:
            raise SourceProvenanceError("transformation_receipt_key_not_trusted")
        key = matches[0]
        signed_at = _utc_datetime(receipt.signed_at, "receipt.signed_at")
        if signed_at > current:
            raise SourceProvenanceError("transformation_receipt_signed_in_future")
        valid_from = _utc_datetime(
            key.valid_from, "transformation_trust_key.valid_from"
        )
        valid_until = _utc_datetime(
            key.valid_until, "transformation_trust_key.valid_until"
        )
        if signed_at < valid_from or signed_at > valid_until:
            raise SourceProvenanceError(
                "transformation_receipt_outside_key_validity"
            )
        if key.revoked_at is not None:
            revoked_at = _utc_datetime(
                key.revoked_at, "transformation_trust_key.revoked_at"
            )
            # Revocation is fail-closed for all evidence once effective.  A
            # receipt predating revocation does not silently retain authority.
            if current >= revoked_at:
                raise SourceProvenanceError("transformation_receipt_key_revoked")
        try:
            signature = base64.b64decode(
                receipt.signature.removeprefix("ed25519:"), validate=True
            )
            key.public_key.verify(signature, receipt.signed_bytes())
        except (InvalidSignature, ValueError, TypeError) as exc:
            raise SourceProvenanceError(
                "transformation_receipt_signature_verification_failed"
            ) from exc


@dataclass(frozen=True)
class DatasetSourceProvenance:
    schema_version: int
    artifact_type: str
    source_catalog: SourceCatalog
    sources: tuple[SourceRecord, ...]
    source_priority: tuple[str, ...]
    semantics: tuple[tuple[str, str], ...]
    lineage: tuple[LineageStage, ...]
    provenance_manifest_hash: str

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": self.artifact_type,
            "source_catalog": self.source_catalog.as_dict(),
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
        {"hash_domain": "dataset_source_provenance_v4", "provenance": material},
        label="dataset_source_provenance_hash",
    )


def transformation_receipt_hash(payload: dict[str, Any]) -> str:
    material = {
        key
        : value
        for key, value in payload.items()
        if key not in {"receipt_hash", "signature"}
    }
    return sha256_prefixed(
        {"hash_domain": "dataset_transformation_receipt_v1", "receipt": material},
        label="dataset_transformation_receipt_hash",
    )


def build_transformation_receipt(
    *,
    layer: str,
    transformation_id: str,
    input_artifacts: Iterable[dict[str, object]],
    output_artifact: dict[str, object],
    authority_id: str,
    key_id: str,
    signed_at: str,
    code_artifact_id: str,
    code_hash: str,
    config_artifact_id: str,
    config_hash: str,
    output_schema_version: int,
    output_canonicalization_id: str | None = None,
    output_canonical_content_hash: str | None = None,
    private_key: Ed25519PrivateKey,
) -> TransformationReceipt:
    if not isinstance(private_key, Ed25519PrivateKey):
        raise SourceProvenanceError(
            "transformation_receipt_ed25519_private_key_required"
        )
    normalized_authority_id = _identifier(
        authority_id, "transformation_receipt_authority_id"
    )
    normalized_key_id = _identifier(key_id, "transformation_receipt_key_id")
    normalized_signed_at = _utc_datetime(
        signed_at, "receipt.signed_at"
    ).isoformat().replace("+00:00", "Z")
    payload: dict[str, Any] = {
        "schema_version": TRANSFORMATION_RECEIPT_SCHEMA_VERSION,
        "artifact_type": "dataset_transformation_receipt",
        "layer": layer,
        "transformation_id": transformation_id,
        "input_artifacts": list(input_artifacts),
        "output_artifact": dict(output_artifact),
        "authority_id": normalized_authority_id,
        "key_id": normalized_key_id,
        "signed_at": normalized_signed_at,
        "code_artifact_id": code_artifact_id,
        "code_hash": code_hash,
        "config_artifact_id": config_artifact_id,
        "config_hash": config_hash,
        "output_schema_version": output_schema_version,
        "output_canonicalization_id": output_canonicalization_id,
        "output_canonical_content_hash": output_canonical_content_hash,
    }
    payload["receipt_hash"] = transformation_receipt_hash(payload)
    payload["signature"] = "ed25519:" + base64.b64encode(
        private_key.sign(
            TRANSFORMATION_RECEIPT_SIGNATURE_DOMAIN
            + canonical_json_bytes(payload)
        )
    ).decode("ascii")
    return parse_transformation_receipt(payload)


def transformation_receipt_bytes(receipt: TransformationReceipt) -> bytes:
    """Canonical external receipt serialization used by producers and fixtures."""
    return canonical_json_bytes(receipt.as_dict()) + b"\n"


def local_artifact_bytes_hash(path: str | Path) -> str:
    """Return the SHA-256 of exact file bytes (not a JSON semantic hash)."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def build_dataset_source_provenance(
    *,
    source_catalog: SourceCatalog,
    sources: Iterable[dict[str, object]],
    source_priority: Iterable[str],
    lineage: Iterable[dict[str, object]],
    semantics: dict[str, str] | None = None,
) -> DatasetSourceProvenance:
    if not isinstance(source_catalog, SourceCatalog):
        raise SourceProvenanceError("source_provenance_source_catalog_required")
    payload: dict[str, Any] = {
        "schema_version": SOURCE_PROVENANCE_SCHEMA_VERSION,
        "artifact_type": "dataset_source_provenance",
        "source_catalog": source_catalog.as_dict(),
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


def load_transformation_trust_store(
    *,
    manager: ResearchPathManager,
    now: datetime | None = None,
) -> TransformationTrustStore:
    """Load the sole administrator-configured transformation trust anchor.

    Dataset manifests and API arguments deliberately cannot supply this path or
    hash.  Both values must already be present in ``ResearchSettings``.  The
    configured digest pins the exact canonical trust-store bytes, while every
    store entry separately pins one repository/state-external public-key file.
    """

    if not isinstance(manager, ResearchPathManager):
        raise SourceProvenanceError(
            "dataset_transformation_trust_manager_required"
        )
    configured_path = manager.settings.dataset_transformation_trust_store_path
    configured_hash = manager.settings.dataset_transformation_trust_store_hash
    if configured_path is None or configured_hash is None:
        raise SourceProvenanceError(
            "dataset_transformation_trust_configuration_required"
        )
    expected_hash = _hash(configured_hash)
    raw = _load_admin_pinned_file(
        configured_path,
        expected_hash=expected_hash,
        manager=manager,
        label="transformation_trust_store",
        maximum_bytes=_MAX_TRANSFORMATION_TRUST_STORE_BYTES,
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceProvenanceError("transformation_trust_store_invalid") from exc
    if not isinstance(payload, dict):
        raise SourceProvenanceError("transformation_trust_store_invalid")
    _reject_unknown(payload, _TRUST_STORE_FIELDS, "transformation_trust_store")
    if payload.get("schema_version") != TRANSFORMATION_TRUST_STORE_SCHEMA_VERSION:
        raise SourceProvenanceError(
            "transformation_trust_store_schema_version_unsupported"
        )
    if payload.get("artifact_type") != "dataset_transformation_trust_store":
        raise SourceProvenanceError(
            "transformation_trust_store_artifact_type_unsupported"
        )
    if raw != canonical_json_bytes(payload) + b"\n":
        raise SourceProvenanceError("transformation_trust_store_not_canonical_json")
    authority_id = _identifier(
        payload.get("authority_id"), "transformation_trust_store_authority_id"
    )
    issued_at = _utc_datetime(
        payload.get("issued_at"), "transformation_trust_store.issued_at"
    )
    expires_at = _utc_datetime(
        payload.get("expires_at"), "transformation_trust_store.expires_at"
    )
    if expires_at <= issued_at:
        raise SourceProvenanceError("transformation_trust_store_validity_invalid")
    raw_keys = payload.get("keys")
    if not isinstance(raw_keys, list) or not raw_keys:
        raise SourceProvenanceError("transformation_trust_store_keys_required")
    keys = tuple(
        _parse_transformation_trust_key(item, manager=manager)
        for item in raw_keys
    )
    if len({key.key_id for key in keys}) != len(keys):
        raise SourceProvenanceError("transformation_trust_store_key_id_duplicate")
    if len({key.public_key_content_hash for key in keys}) != len(keys):
        raise SourceProvenanceError(
            "transformation_trust_store_public_key_rebound"
        )
    store = TransformationTrustStore(
        authority_id=authority_id,
        issued_at=issued_at.isoformat().replace("+00:00", "Z"),
        expires_at=expires_at.isoformat().replace("+00:00", "Z"),
        keys=keys,
        content_hash=expected_hash,
    )
    current = _aware_utc(
        now or datetime.now(timezone.utc),
        "transformation_trust_verification_time",
    )
    if current < issued_at:
        raise SourceProvenanceError("transformation_trust_store_not_yet_valid")
    if current > expires_at:
        raise SourceProvenanceError("transformation_trust_store_expired")
    return store


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

    try:
        source_catalog = parse_source_catalog(payload.get("source_catalog"))
    except SourceCatalogError as exc:
        raise SourceProvenanceError(str(exc)) from exc

    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise SourceProvenanceError("source_provenance_sources_required")
    sources = tuple(_parse_source(value) for value in raw_sources)
    provider_ids = tuple(source.provider_id for source in sources)
    if len(set(provider_ids)) != len(provider_ids):
        raise SourceProvenanceError("source_provenance_provider_id_duplicate")
    _validate_sources_against_catalog(sources, source_catalog)

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
        source_catalog=source_catalog,
        sources=sources,
        source_priority=priority,
        semantics=tuple(sorted(semantics.items())),
        lineage=lineage,
        provenance_manifest_hash=expected_hash,
    )


def parse_transformation_receipt(payload: Any) -> TransformationReceipt:
    if not isinstance(payload, dict):
        raise SourceProvenanceError("transformation_receipt_must_be_object")
    _reject_unknown(payload, _RECEIPT_FIELDS, "transformation_receipt")
    if payload.get("schema_version") != TRANSFORMATION_RECEIPT_SCHEMA_VERSION:
        raise SourceProvenanceError("transformation_receipt_schema_version_unsupported")
    if payload.get("artifact_type") != "dataset_transformation_receipt":
        raise SourceProvenanceError("transformation_receipt_artifact_type_unsupported")
    expected_hash = _hash(payload.get("receipt_hash"))
    if transformation_receipt_hash(payload) != expected_hash:
        raise SourceProvenanceError("transformation_receipt_hash_mismatch")
    layer = _text(payload.get("layer"), "receipt.layer")
    if layer not in _REQUIRED_LAYERS:
        raise SourceProvenanceError("transformation_receipt_layer_invalid")
    raw_inputs = payload.get("input_artifacts")
    if not isinstance(raw_inputs, list) or not raw_inputs:
        raise SourceProvenanceError("transformation_receipt_inputs_required")
    inputs = tuple(_parse_receipt_binding(item) for item in raw_inputs)
    if len({item.artifact_id for item in inputs}) != len(inputs):
        raise SourceProvenanceError("transformation_receipt_input_id_duplicate")
    signed_at = _utc_datetime(payload.get("signed_at"), "receipt.signed_at")
    signature = _ed25519_signature(payload.get("signature"))
    output_schema_version = _strict_int(
        payload.get("output_schema_version"), "receipt.output_schema_version"
    )
    if output_schema_version <= 0:
        raise SourceProvenanceError(
            "transformation_receipt_output_schema_version_invalid"
        )
    raw_canonicalization_id = payload.get("output_canonicalization_id")
    raw_canonical_content_hash = payload.get("output_canonical_content_hash")
    if (raw_canonicalization_id is None) != (raw_canonical_content_hash is None):
        raise SourceProvenanceError(
            "transformation_receipt_output_canonicalization_incomplete"
        )
    output_canonicalization_id = (
        None
        if raw_canonicalization_id is None
        else _text(
            raw_canonicalization_id,
            "receipt.output_canonicalization_id",
        )
    )
    output_canonical_content_hash = (
        None
        if raw_canonical_content_hash is None
        else _hash(raw_canonical_content_hash)
    )
    if layer == "standardized":
        if (
            output_canonicalization_id != STANDARDIZED_CANONICALIZATION_ID
            or output_canonical_content_hash is None
        ):
            raise SourceProvenanceError(
                "transformation_receipt_output_canonicalization_invalid"
            )
    elif (
        output_canonicalization_id is not None
        or output_canonical_content_hash is not None
    ):
        raise SourceProvenanceError(
            "transformation_receipt_output_canonicalization_forbidden"
        )
    return TransformationReceipt(
        schema_version=TRANSFORMATION_RECEIPT_SCHEMA_VERSION,
        artifact_type="dataset_transformation_receipt",
        layer=layer,
        transformation_id=_text(
            payload.get("transformation_id"), "receipt.transformation_id"
        ),
        input_artifacts=inputs,
        output_artifact=_parse_receipt_binding(payload.get("output_artifact")),
        authority_id=_identifier(
            payload.get("authority_id"), "transformation_receipt_authority_id"
        ),
        key_id=_identifier(
            payload.get("key_id"), "transformation_receipt_key_id"
        ),
        signed_at=signed_at.isoformat().replace("+00:00", "Z"),
        code_artifact_id=_text(
            payload.get("code_artifact_id"), "receipt.code_artifact_id"
        ),
        code_hash=_hash(payload.get("code_hash")),
        config_artifact_id=_text(
            payload.get("config_artifact_id"), "receipt.config_artifact_id"
        ),
        config_hash=_hash(payload.get("config_hash")),
        output_schema_version=output_schema_version,
        output_canonicalization_id=output_canonicalization_id,
        output_canonical_content_hash=output_canonical_content_hash,
        receipt_hash=expected_hash,
        signature=signature,
    )


def _parse_transformation_trust_key(
    payload: Any,
    *,
    manager: ResearchPathManager,
) -> TransformationTrustKey:
    if not isinstance(payload, dict):
        raise SourceProvenanceError("transformation_trust_store_key_invalid")
    _reject_unknown(payload, _TRUST_KEY_FIELDS, "transformation_trust_store.key")
    key_id = _identifier(
        payload.get("key_id"), "transformation_trust_store_key_id"
    )
    if payload.get("algorithm") != TRANSFORMATION_RECEIPT_SIGNATURE_ALGORITHM:
        raise SourceProvenanceError(
            "transformation_trust_store_key_algorithm_unsupported"
        )
    public_key_path = _normalized_absolute_path(
        payload.get("public_key_path"),
        "transformation_trust_store_public_key_path",
    )
    public_key_content_hash = _hash(payload.get("public_key_content_hash"))
    valid_from = _utc_datetime(
        payload.get("valid_from"), "transformation_trust_key.valid_from"
    )
    valid_until = _utc_datetime(
        payload.get("valid_until"), "transformation_trust_key.valid_until"
    )
    if valid_until <= valid_from:
        raise SourceProvenanceError("transformation_trust_key_validity_invalid")
    raw_revoked_at = payload.get("revoked_at")
    raw_reason = payload.get("revocation_reason")
    if raw_revoked_at is None:
        if raw_reason != "":
            raise SourceProvenanceError(
                "transformation_trust_key_revocation_invalid"
            )
        revoked_at: datetime | None = None
    else:
        revoked_at = _utc_datetime(
            raw_revoked_at, "transformation_trust_key.revoked_at"
        )
        if not isinstance(raw_reason, str) or not raw_reason.strip():
            raise SourceProvenanceError(
                "transformation_trust_key_revocation_invalid"
            )
        if revoked_at < valid_from:
            raise SourceProvenanceError(
                "transformation_trust_key_revocation_invalid"
            )
    public_key_raw = _load_admin_pinned_file(
        public_key_path,
        expected_hash=public_key_content_hash,
        manager=manager,
        label="transformation_trust_public_key",
        maximum_bytes=_MAX_TRANSFORMATION_PUBLIC_KEY_BYTES,
    )
    public_key = _parse_ed25519_public_key_file(public_key_raw)
    return TransformationTrustKey(
        key_id=key_id,
        public_key_path=str(public_key_path),
        public_key_content_hash=public_key_content_hash,
        valid_from=valid_from.isoformat().replace("+00:00", "Z"),
        valid_until=valid_until.isoformat().replace("+00:00", "Z"),
        revoked_at=(
            None
            if revoked_at is None
            else revoked_at.isoformat().replace("+00:00", "Z")
        ),
        revocation_reason=("" if revoked_at is None else str(raw_reason).strip()),
        public_key=public_key,
    )


def validate_source_artifact_chain(
    provenance: DatasetSourceProvenance,
    *,
    manager: ResearchPathManager,
    now: datetime | None = None,
) -> Path:
    """Verify every physical input, receipt, and raw→cleaned→standardized edge.

    Returns the already-validated standardized artifact path.  Callers that read
    it must invoke this function again after the read to detect replacement or
    mutation during consumption.
    """
    if not isinstance(manager, ResearchPathManager):
        raise SourceProvenanceError(
            "dataset_transformation_trust_manager_required"
        )
    repo = manager.project_root.expanduser().resolve()
    verification_time = _aware_utc(
        now or datetime.now(timezone.utc),
        "transformation_trust_verification_time",
    )
    trust_store = load_transformation_trust_store(
        manager=manager,
        now=verification_time,
    )
    ordered_sources = {source.provider_id: source for source in provenance.sources}
    source_bindings: list[ReceiptArtifactBinding] = []
    occupied_uris: set[str] = set()
    for provider_id in provenance.source_priority:
        source = ordered_sources[provider_id]
        path = verify_external_local_artifact(
            source.artifact_uri,
            source.content_hash,
            repository_root=repo,
            label="source_artifact",
        )
        normalized = str(path)
        if normalized in occupied_uris:
            raise SourceProvenanceError("source_provenance_artifact_uri_duplicate")
        occupied_uris.add(normalized)
        source_bindings.append(
            ReceiptArtifactBinding(
                source_record_artifact_id(
                    source,
                    source_catalog_hash=provenance.source_catalog.catalog_hash,
                ),
                source.content_hash,
            )
        )

    expected_inputs = tuple(source_bindings)
    previous_evidence_time = max(
        _utc_datetime(source.received_at, "source.received_at")
        for source in provenance.sources
    )
    for stage in provenance.lineage:
        artifact_path = verify_external_local_artifact(
            stage.artifact_uri,
            stage.content_hash,
            repository_root=repo,
            label=f"{stage.layer}_artifact",
        )
        receipt_path, receipt_raw = _read_verified_external_local_artifact(
            stage.transformation_receipt_uri,
            stage.transformation_receipt_content_hash,
            repository_root=repo,
            label=f"{stage.layer}_transformation_receipt",
            maximum_bytes=_MAX_TRANSFORMATION_RECEIPT_BYTES,
        )
        receipt = _parse_verified_receipt_bytes(
            receipt_raw, stage.transformation_receipt_content_hash
        )
        trust_store.verify_receipt(receipt, now=verification_time)
        receipt_signed_at = _utc_datetime(receipt.signed_at, "receipt.signed_at")
        code_path = verify_external_local_artifact(
            stage.code_artifact_uri,
            receipt.code_hash,
            repository_root=repo,
            label=f"{stage.layer}_transformation_code",
        )
        config_path = verify_external_local_artifact(
            stage.config_artifact_uri,
            receipt.config_hash,
            repository_root=repo,
            label=f"{stage.layer}_transformation_config",
        )
        for path in (artifact_path, receipt_path, code_path, config_path):
            normalized = str(path)
            if normalized in occupied_uris:
                raise SourceProvenanceError("source_provenance_artifact_uri_duplicate")
            occupied_uris.add(normalized)
        if receipt.layer != stage.layer:
            raise SourceProvenanceError("transformation_receipt_layer_mismatch")
        if receipt.transformation_id != stage.transformation_id:
            raise SourceProvenanceError(
                "transformation_receipt_transformation_id_mismatch"
            )
        if receipt.input_artifacts != expected_inputs:
            raise SourceProvenanceError("transformation_receipt_input_chain_mismatch")
        if receipt_signed_at < previous_evidence_time:
            raise SourceProvenanceError(
                "transformation_receipt_causal_time_inverted"
            )
        if receipt.code_artifact_id != f"{stage.transformation_id}:code":
            raise SourceProvenanceError(
                "transformation_receipt_code_artifact_id_mismatch"
            )
        if receipt.config_artifact_id != f"{stage.transformation_id}:config":
            raise SourceProvenanceError(
                "transformation_receipt_config_artifact_id_mismatch"
            )
        if receipt.output_schema_version != stage.schema_version:
            raise SourceProvenanceError(
                "transformation_receipt_output_schema_version_mismatch"
            )
        if (
            receipt.output_canonicalization_id != stage.canonicalization_id
            or receipt.output_canonical_content_hash
            != stage.canonical_content_hash
        ):
            raise SourceProvenanceError(
                "transformation_receipt_output_canonicalization_mismatch"
            )
        expected_output = ReceiptArtifactBinding(stage.artifact_id, stage.content_hash)
        if receipt.output_artifact != expected_output:
            raise SourceProvenanceError(
                "transformation_receipt_output_binding_mismatch"
            )
        expected_inputs = (expected_output,)
        previous_evidence_time = receipt_signed_at
    return Path(provenance.lineage[-1].artifact_uri)


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


def _validate_sources_against_catalog(
    sources: tuple[SourceRecord, ...], source_catalog: SourceCatalog
) -> None:
    for source in sources:
        try:
            catalog_entry = source_catalog.resolve(source.provider_id)
        except SourceCatalogError as exc:
            raise SourceProvenanceError(str(exc)) from exc
        if source.source_kind not in catalog_entry.source_kinds:
            raise SourceProvenanceError(
                "source_provenance_source_kind_not_approved_by_catalog"
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
        artifact_uri=_artifact_uri(value.get("artifact_uri"), "source.artifact_uri"),
        content_hash=_hash(value.get("content_hash")),
    )


def _parse_lineage(value: Any) -> LineageStage:
    if not isinstance(value, dict):
        raise SourceProvenanceError("source_provenance_lineage_stage_must_be_object")
    _reject_unknown(value, _LINEAGE_FIELDS, "source_provenance.lineage")
    version = _strict_int(value.get("schema_version"), "lineage.schema_version")
    if version <= 0:
        raise SourceProvenanceError("source_provenance_lineage_schema_version_invalid")
    layer = _text(value.get("layer"), "lineage.layer")
    canonicalization_id: str | None = None
    canonical_content_hash: str | None = None
    if layer == "standardized":
        canonicalization_id = _text(
            value.get("canonicalization_id"), "lineage.canonicalization_id"
        )
        if canonicalization_id != STANDARDIZED_CANONICALIZATION_ID:
            raise SourceProvenanceError(
                "source_provenance_standardized_canonicalization_unsupported"
            )
        canonical_content_hash = _hash(value.get("canonical_content_hash"))
    elif "canonicalization_id" in value or "canonical_content_hash" in value:
        raise SourceProvenanceError(
            "source_provenance_canonicalization_only_standardized"
        )
    return LineageStage(
        layer=layer,
        artifact_id=_text(value.get("artifact_id"), "lineage.artifact_id"),
        artifact_uri=_artifact_uri(value.get("artifact_uri"), "lineage.artifact_uri"),
        content_hash=_hash(value.get("content_hash")),
        schema_version=version,
        transformation_id=_text(
            value.get("transformation_id"), "lineage.transformation_id"
        ),
        transformation_receipt_uri=_artifact_uri(
            value.get("transformation_receipt_uri"),
            "lineage.transformation_receipt_uri",
        ),
        transformation_receipt_content_hash=_hash(
            value.get("transformation_receipt_content_hash")
        ),
        code_artifact_uri=_artifact_uri(
            value.get("code_artifact_uri"), "lineage.code_artifact_uri"
        ),
        config_artifact_uri=_artifact_uri(
            value.get("config_artifact_uri"), "lineage.config_artifact_uri"
        ),
        canonicalization_id=canonicalization_id,
        canonical_content_hash=canonical_content_hash,
    )


def _parse_receipt_binding(value: Any) -> ReceiptArtifactBinding:
    if not isinstance(value, dict):
        raise SourceProvenanceError("transformation_receipt_binding_must_be_object")
    _reject_unknown(value, _RECEIPT_ARTIFACT_FIELDS, "transformation_receipt.binding")
    return ReceiptArtifactBinding(
        artifact_id=_text(value.get("artifact_id"), "receipt.artifact_id"),
        content_hash=_hash(value.get("content_hash")),
    )


def source_record_artifact_id(
    source: SourceRecord,
    *,
    source_catalog_hash: str,
) -> str:
    """Return the signed logical identity for one acquired source artifact.

    The local locator is intentionally excluded so byte-identical evidence can
    be relocated.  The exact bytes are the separate receipt binding.  Every
    acquisition/coverage semantic and the approved catalog version are bound,
    so editing metadata and merely rehashing provenance cannot reuse a trusted
    receipt.
    """

    if not isinstance(source, SourceRecord):
        raise SourceProvenanceError("source_record_binding_source_required")
    metadata = source.as_dict()
    metadata.pop("artifact_uri", None)
    metadata.pop("content_hash", None)
    binding_hash = sha256_prefixed(
        {
            "schema_version": 1,
            "source_catalog_hash": _hash(source_catalog_hash),
            "source_record": metadata,
        },
        label="dataset_source_record_binding",
    )
    return f"source-record:{binding_hash}"


def _artifact_uri(value: Any, label: str) -> str:
    text = _text(value, label)
    path = Path(text)
    if not path.is_absolute() or os.path.normpath(text) != text:
        raise SourceProvenanceError(
            f"source_provenance_{label}_must_be_normalized_absolute_local_path"
        )
    return str(path)


def _normalized_absolute_path(value: Any, label: str) -> Path:
    text = _text(value, label)
    path = Path(text)
    if not path.is_absolute() or os.path.normpath(text) != text:
        raise SourceProvenanceError(f"{label}_absolute_normalized_path_required")
    return path


def verify_external_local_artifact(
    artifact_uri: str,
    expected_hash: str,
    *,
    repository_root: Path,
    label: str,
    require_admin_owned_mode: bool = False,
) -> Path:
    resolved = _resolve_external_local_artifact(
        artifact_uri,
        repository_root=repository_root,
        label=label,
        require_admin_owned_mode=require_admin_owned_mode,
    )

    descriptor = _open_nofollow_descriptor(resolved, label=label)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SourceProvenanceError(f"{label}_must_be_regular_non_symlink")
        if before.st_nlink != 1:
            raise SourceProvenanceError(f"{label}_hardlink_rejected")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if after.st_nlink != 1 or _file_identity(before) != _file_identity(after):
            raise SourceProvenanceError(f"{label}_changed_during_verification")
        actual_hash = f"sha256:{digest.hexdigest()}"
    finally:
        os.close(descriptor)
    _require_path_identity_unchanged(resolved, before, label=label)
    if actual_hash != expected_hash:
        raise SourceProvenanceError(f"{label}_content_hash_mismatch")
    return resolved


def _resolve_external_local_artifact(
    artifact_uri: str,
    *,
    repository_root: Path,
    label: str,
    require_admin_owned_mode: bool,
) -> Path:
    path = Path(artifact_uri)
    try:
        lexical = path.absolute()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SourceProvenanceError(f"{label}_unavailable") from exc
    if lexical != resolved:
        raise SourceProvenanceError(f"{label}_symlink_rejected")
    try:
        resolved.relative_to(repository_root)
    except ValueError:
        pass
    else:
        raise SourceProvenanceError(f"{label}_inside_repository")
    current = resolved
    while True:
        try:
            status = current.lstat()
        except OSError as exc:
            raise SourceProvenanceError(f"{label}_unavailable") from exc
        if current == resolved:
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                raise SourceProvenanceError(f"{label}_must_be_regular_non_symlink")
            if (
                require_admin_owned_mode
                and os.name == "posix"
                and stat.S_IMODE(status.st_mode) & 0o022
            ):
                raise SourceProvenanceError(f"{label}_permissions_too_open")
        elif stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise SourceProvenanceError(f"{label}_parent_symlink_rejected")
        parent = current.parent
        if parent == current:
            break
        current = parent

    return resolved


def copy_verified_external_local_artifact(
    artifact_uri: str,
    expected_hash: str,
    *,
    repository_root: Path,
    destination: Path,
    label: str,
) -> Path:
    """Copy exact verified bytes through a no-follow descriptor.

    SQLite is subsequently opened only from this private copy.  That prevents
    a path replacement between chain verification and SQLite's independent
    path open from influencing the published frozen rows.
    """

    source = _resolve_external_local_artifact(
        artifact_uri,
        repository_root=repository_root,
        label=label,
        require_admin_owned_mode=False,
    )
    descriptor = _open_nofollow_descriptor(source, label=label)
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SourceProvenanceError(f"{label}_must_be_regular_non_symlink")
        if before.st_nlink != 1:
            raise SourceProvenanceError(f"{label}_hardlink_rejected")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with destination.open("xb") as output:
            os.chmod(destination, 0o600)
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(descriptor)
        if after.st_nlink != 1 or _file_identity(before) != _file_identity(after):
            raise SourceProvenanceError(f"{label}_changed_during_snapshot")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
    _require_path_identity_unchanged(source, before, label=label)
    actual_hash = f"sha256:{digest.hexdigest()}"
    if actual_hash != expected_hash:
        destination.unlink(missing_ok=True)
        raise SourceProvenanceError(f"{label}_content_hash_mismatch")
    return destination


def _read_verified_external_local_artifact(
    artifact_uri: str,
    expected_hash: str,
    *,
    repository_root: Path,
    label: str,
    maximum_bytes: int,
) -> tuple[Path, bytes]:
    """Read one bounded evidence file through one no-follow descriptor."""

    source = _resolve_external_local_artifact(
        artifact_uri,
        repository_root=repository_root,
        label=label,
        require_admin_owned_mode=False,
    )
    descriptor = _open_nofollow_descriptor(source, label=label)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > maximum_bytes
        ):
            raise SourceProvenanceError(f"{label}_size_invalid")
        if before.st_nlink != 1:
            raise SourceProvenanceError(f"{label}_hardlink_rejected")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        digest = hashlib.sha256()
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(raw) > maximum_bytes
            or after.st_nlink != 1
            or _file_identity(before) != _file_identity(after)
        ):
            raise SourceProvenanceError(f"{label}_changed_during_verification")
    finally:
        os.close(descriptor)
    _require_path_identity_unchanged(source, before, label=label)
    if f"sha256:{digest.hexdigest()}" != expected_hash:
        raise SourceProvenanceError(f"{label}_content_hash_mismatch")
    return source, raw


def _load_admin_pinned_file(
    path: str | Path,
    *,
    expected_hash: str,
    manager: ResearchPathManager,
    label: str,
    maximum_bytes: int,
) -> bytes:
    """Read one root-owned operated-runtime trust file without path races."""

    if os.environ.get("RESEARCH_RUNTIME_PROFILE", "").strip().lower() != "operated":
        raise SourceProvenanceError(
            "dataset_transformation_trust_operated_profile_required"
        )
    if os.name != "posix":
        raise SourceProvenanceError(f"{label}_permissions_unverifiable")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or os.path.normpath(str(candidate)) != str(
        candidate
    ):
        raise SourceProvenanceError(f"{label}_absolute_normalized_path_required")
    lexical = candidate.absolute()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise SourceProvenanceError(f"{label}_unavailable") from exc
    if lexical != resolved:
        raise SourceProvenanceError(f"{label}_symlink_rejected")
    forbidden_roots = (
        manager.project_root,
        manager.data_root,
        manager.artifact_root,
        manager.report_root,
        manager.cache_root,
    )
    if any(ResearchPathManager.is_within(resolved, root) for root in forbidden_roots):
        raise SourceProvenanceError(f"{label}_must_be_external_to_runtime_state")
    cursor = resolved
    while True:
        try:
            status = cursor.lstat()
        except OSError as exc:
            raise SourceProvenanceError(f"{label}_unavailable") from exc
        if stat.S_ISLNK(status.st_mode):
            raise SourceProvenanceError(f"{label}_symlink_rejected")
        if cursor == resolved:
            if not stat.S_ISREG(status.st_mode):
                raise SourceProvenanceError(f"{label}_must_be_regular_file")
            if status.st_nlink != 1:
                raise SourceProvenanceError(f"{label}_hardlink_rejected")
            if stat.S_IMODE(status.st_mode) != 0o644:
                raise SourceProvenanceError(f"{label}_permissions_too_open")
            if status.st_uid != 0:
                raise SourceProvenanceError(f"{label}_administrator_owner_required")
        elif (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid != 0
            or stat.S_IMODE(status.st_mode) & 0o022
        ):
            raise SourceProvenanceError(f"{label}_parent_permissions_too_open")
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    descriptor = _open_nofollow_descriptor(resolved, label=label)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size <= 0
            or before.st_size > maximum_bytes
        ):
            raise SourceProvenanceError(f"{label}_invalid")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(raw) > maximum_bytes or _file_identity(before) != _file_identity(after):
            raise SourceProvenanceError(f"{label}_changed_during_verification")
    finally:
        os.close(descriptor)
    _require_path_identity_unchanged(resolved, before, label=label)
    actual_hash = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    if actual_hash != expected_hash:
        raise SourceProvenanceError(f"{label}_content_hash_mismatch")
    return raw


def _parse_ed25519_public_key_file(raw: bytes) -> Ed25519PublicKey:
    if not raw.startswith(b"ed25519:") or not raw.endswith(b"\n"):
        raise SourceProvenanceError("transformation_trust_public_key_invalid")
    encoded = raw[len(b"ed25519:") : -1]
    if b"\n" in encoded or b"\r" in encoded:
        raise SourceProvenanceError("transformation_trust_public_key_invalid")
    try:
        key_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise SourceProvenanceError(
            "transformation_trust_public_key_invalid"
        ) from exc
    if (
        len(key_bytes) != 32
        or base64.b64encode(key_bytes) != encoded
    ):
        raise SourceProvenanceError("transformation_trust_public_key_invalid")
    try:
        return Ed25519PublicKey.from_public_bytes(key_bytes)
    except ValueError as exc:
        raise SourceProvenanceError(
            "transformation_trust_public_key_invalid"
        ) from exc


def _open_nofollow_descriptor(path: Path, *, label: str) -> int:
    """Open an absolute file without following any path-component symlink.

    The component-wise ``openat`` walk pins every parent directory while the
    next component is opened.  This closes the resolve-then-open race left by
    checking a pathname and subsequently calling ``open(path)``.
    """

    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    if os.name != "posix" or not hasattr(os, "O_DIRECTORY"):
        try:
            return os.open(path, file_flags)
        except OSError as exc:
            raise SourceProvenanceError(f"{label}_unavailable") from exc
    parts = path.parts
    if not path.is_absolute() or len(parts) < 2:
        raise SourceProvenanceError(f"{label}_unavailable")
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directory_descriptor: int | None = None
    try:
        directory_descriptor = os.open(parts[0], directory_flags)
        for component in parts[1:-1]:
            next_descriptor = os.open(
                component,
                directory_flags,
                dir_fd=directory_descriptor,
            )
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        return os.open(
            parts[-1],
            file_flags,
            dir_fd=directory_descriptor,
        )
    except OSError as exc:
        raise SourceProvenanceError(f"{label}_unavailable") from exc
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)


def _require_path_identity_unchanged(
    path: Path,
    expected: os.stat_result,
    *,
    label: str,
) -> None:
    try:
        current = path.lstat()
    except OSError as exc:
        raise SourceProvenanceError(f"{label}_changed_during_verification") from exc
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
    ):
        raise SourceProvenanceError(f"{label}_changed_during_verification")


def _file_identity(status: os.stat_result) -> tuple[int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
    )


def _parse_verified_receipt_bytes(
    raw: bytes, expected_content_hash: str
) -> TransformationReceipt:
    try:
        if not raw or len(raw) > _MAX_TRANSFORMATION_RECEIPT_BYTES:
            raise SourceProvenanceError("transformation_receipt_too_large")
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceProvenanceError("transformation_receipt_unavailable") from exc
    actual_content_hash = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    if actual_content_hash != expected_content_hash:
        raise SourceProvenanceError(
            "transformation_receipt_content_hash_mismatch"
        )
    receipt = parse_transformation_receipt(payload)
    if raw != transformation_receipt_bytes(receipt):
        raise SourceProvenanceError("transformation_receipt_not_canonical_json")
    return receipt


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


def _ed25519_signature(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("ed25519:"):
        raise SourceProvenanceError("transformation_receipt_signature_invalid")
    encoded = value.removeprefix("ed25519:")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise SourceProvenanceError(
            "transformation_receipt_signature_invalid"
        ) from exc
    if len(decoded) != 64 or base64.b64encode(decoded).decode("ascii") != encoded:
        raise SourceProvenanceError("transformation_receipt_signature_invalid")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise SourceProvenanceError(f"{label}_invalid")
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
    return parsed.astimezone(timezone.utc)


def _aware_utc(value: datetime, label: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise SourceProvenanceError(f"{label}_utc_required")
    return value.astimezone(timezone.utc)


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
