"""Strict, self-contained immutable candle artifact sidecar contract."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hashing_contract import artifact_manifest_hash
from .locators import ContentAddressedLocal, LocatorValidationError, parse_immutable_locator

ARTIFACT_MANIFEST_SCHEMA_VERSION = 2
_TOP_LEVEL_FIELDS = frozenset({"schema_version", "artifact_type", "artifact_id", "format", "artifact",
                               "artifact_identity_hash", "scope", "canonicalization", "locator",
                               "artifact_manifest_hash"})
_ARTIFACT_FIELDS = frozenset({"uri", "content_hash", "schema_hash", "row_count"})
_SCOPE_FIELDS = frozenset({"market", "interval", "start_ts", "end_ts", "coverage_start_ts", "coverage_end_ts"})
_CANONICALIZATION_FIELDS = frozenset({"name", "version"})
_CANONICALIZATION = ("ohlcv_pair_interval_rows", 1)


class ArtifactManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ArtifactManifest:
    schema_version: int
    artifact_type: str
    artifact_id: str
    format: str
    locator: ContentAddressedLocal
    content_hash: str
    schema_hash: str
    row_count: int
    market: str
    interval: str
    start_ts: int
    end_ts: int
    coverage_start_ts: int
    coverage_end_ts: int
    canonicalization_name: str
    canonicalization_version: int
    artifact_identity_hash: str
    artifact_manifest_hash: str

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "artifact_type": self.artifact_type,
            "artifact_id": self.artifact_id, "format": self.format,
            "artifact": {"content_hash": self.content_hash, "schema_hash": self.schema_hash,
                         "row_count": self.row_count},
            "scope": {"market": self.market, "interval": self.interval,
                      "start_ts": self.start_ts, "end_ts": self.end_ts,
                      "coverage_start_ts": self.coverage_start_ts,
                      "coverage_end_ts": self.coverage_end_ts},
            "canonicalization": {"name": self.canonicalization_name,
                                   "version": self.canonicalization_version},
        }

    def as_dict(self) -> dict[str, Any]:
        payload = self.identity_payload()
        payload["artifact"]["uri"] = self.locator.path
        payload["artifact_identity_hash"] = self.artifact_identity_hash
        payload["locator"] = self.locator.as_dict()
        payload["artifact_manifest_hash"] = self.artifact_manifest_hash
        return payload


def build_artifact_manifest(*, artifact_id: str, path: str, content_hash: str, schema_hash: str,
                            row_count: int, market: str, interval: str, start_ts: int,
                            end_ts: int, coverage_start_ts: int, coverage_end_ts: int) -> ArtifactManifest:
    identity = {
        "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
        "artifact_type": "immutable_candle_dataset", "artifact_id": artifact_id, "format": "sqlite",
        "artifact": {"content_hash": content_hash, "schema_hash": schema_hash, "row_count": int(row_count)},
        "scope": {"market": market, "interval": interval, "start_ts": int(start_ts), "end_ts": int(end_ts),
                  "coverage_start_ts": int(coverage_start_ts), "coverage_end_ts": int(coverage_end_ts)},
        "canonicalization": {"name": _CANONICALIZATION[0], "version": _CANONICALIZATION[1]},
    }
    identity_hash = artifact_manifest_hash(identity)
    locator = ContentAddressedLocal(path=str(Path(path).resolve()), artifact_content_hash=content_hash)
    payload = dict(identity)
    payload["artifact"] = {**identity["artifact"], "uri": locator.path}
    payload["artifact_identity_hash"] = identity_hash
    payload["locator"] = locator.as_dict()
    digest = artifact_manifest_hash(payload)
    return ArtifactManifest(**_parse_values({**payload, "artifact_manifest_hash": digest}, locator=locator))


def parse_artifact_manifest(payload: dict[str, Any]) -> ArtifactManifest:
    if not isinstance(payload, dict):
        raise ArtifactManifestError("artifact_manifest_must_be_object")
    _reject_unknown(payload, _TOP_LEVEL_FIELDS, "artifact_manifest")
    if payload.get("schema_version") != ARTIFACT_MANIFEST_SCHEMA_VERSION:
        raise ArtifactManifestError("artifact_manifest_schema_version_unsupported")
    expected = _hash(payload.get("artifact_manifest_hash"))
    if artifact_manifest_hash({key: value for key, value in payload.items() if key != "artifact_manifest_hash"}) != expected:
        raise ArtifactManifestError("artifact_manifest_hash_mismatch")
    try:
        locator = parse_immutable_locator(payload.get("locator"))
    except LocatorValidationError as exc:
        raise ArtifactManifestError(str(exc)) from exc
    return ArtifactManifest(**_parse_values(payload, locator=locator))


def load_artifact_manifest(path: str | Path, expected_hash: str | None = None) -> ArtifactManifest:
    manifest_path = Path(path).expanduser()
    if not manifest_path.is_absolute():
        raise ArtifactManifestError("artifact_manifest_uri_must_be_absolute")
    try:
        with manifest_path.open(encoding="utf-8") as handle:
            parsed = parse_artifact_manifest(json.load(handle))
    except OSError as exc:
        raise ArtifactManifestError("artifact_manifest_unavailable") from exc
    if expected_hash is not None and parsed.artifact_manifest_hash != _hash(expected_hash):
        raise ArtifactManifestError("artifact_manifest_reference_hash_mismatch")
    # A local artifact sidecar is authoritative only for the committed bundle
    # which contains it.  An outside DB cannot be smuggled in via a valid hash.
    if manifest_path.name != "artifact.manifest.json" or parsed.locator.path != str((manifest_path.parent / "candles.sqlite").resolve()):
        raise ArtifactManifestError("artifact_manifest_locator_not_in_published_bundle")
    return parsed


def _parse_values(payload: dict[str, Any], *, locator: ContentAddressedLocal) -> dict[str, Any]:
    artifact = payload.get("artifact")
    scope = payload.get("scope")
    canonicalization = payload.get("canonicalization")
    if not all(isinstance(item, dict) for item in (artifact, scope, canonicalization)):
        raise ArtifactManifestError("artifact_manifest_sections_invalid")
    _reject_unknown(artifact, _ARTIFACT_FIELDS, "artifact_manifest.artifact")
    _reject_unknown(scope, _SCOPE_FIELDS, "artifact_manifest.scope")
    _reject_unknown(canonicalization, _CANONICALIZATION_FIELDS, "artifact_manifest.canonicalization")
    artifact_id = _text(payload.get("artifact_id"))
    artifact_type = _text(payload.get("artifact_type"))
    physical_format = _text(payload.get("format"))
    if artifact_type != "immutable_candle_dataset" or physical_format != "sqlite":
        raise ArtifactManifestError("artifact_manifest_type_unsupported")
    canonical_name = _text(canonicalization.get("name"))
    canonical_version = _strict_int(canonicalization.get("version"), "canonicalization.version")
    if (canonical_name, canonical_version) != _CANONICALIZATION:
        raise ArtifactManifestError("artifact_manifest_canonicalization_unsupported")
    row_count = _strict_int(artifact.get("row_count"), "artifact.row_count")
    if row_count < 0:
        raise ArtifactManifestError("artifact_manifest_row_count_negative")
    start_ts = _strict_int(scope.get("start_ts"), "scope.start_ts")
    end_ts = _strict_int(scope.get("end_ts"), "scope.end_ts")
    coverage_start_ts = _strict_int(scope.get("coverage_start_ts"), "scope.coverage_start_ts")
    coverage_end_ts = _strict_int(scope.get("coverage_end_ts"), "scope.coverage_end_ts")
    if start_ts > end_ts:
        raise ArtifactManifestError("artifact_manifest_scope_inverted")
    if coverage_start_ts != start_ts or coverage_end_ts < end_ts:
        raise ArtifactManifestError("artifact_manifest_coverage_scope_invalid")
    uri = _text(artifact.get("uri"))
    if uri != locator.path:
        raise ArtifactManifestError("artifact_manifest_uri_locator_mismatch")
    values = {
        "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION, "artifact_type": artifact_type,
        "artifact_id": artifact_id, "format": physical_format, "locator": locator,
        "content_hash": _hash(artifact.get("content_hash")), "schema_hash": _hash(artifact.get("schema_hash")),
        "row_count": row_count, "market": _text(scope.get("market")), "interval": _text(scope.get("interval")),
        "start_ts": start_ts, "end_ts": end_ts, "coverage_start_ts": coverage_start_ts,
        "coverage_end_ts": coverage_end_ts, "canonicalization_name": canonical_name,
        "canonicalization_version": canonical_version,
        "artifact_identity_hash": _hash(payload.get("artifact_identity_hash")),
        "artifact_manifest_hash": _hash(payload.get("artifact_manifest_hash")),
    }
    identity = ArtifactManifest(**values).identity_payload()
    if artifact_manifest_hash(identity) != values["artifact_identity_hash"]:
        raise ArtifactManifestError("artifact_manifest_identity_hash_mismatch")
    if locator.artifact_content_hash != values["content_hash"]:
        raise ArtifactManifestError("artifact_manifest_locator_binding_mismatch")
    return values


def _reject_unknown(value: dict[str, Any], allowed: frozenset[str], context: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ArtifactManifestError(f"{context}_unknown_field:{','.join(unknown)}")


def _hash(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        raise ArtifactManifestError("artifact_manifest_hash_invalid")
    if any(char not in "0123456789abcdef" for char in value[7:]):
        raise ArtifactManifestError("artifact_manifest_hash_invalid")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactManifestError("artifact_manifest_text_invalid")
    return value


def _strict_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArtifactManifestError(f"artifact_manifest_{label}_invalid")
    return int(value)
