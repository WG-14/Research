"""Versioned first-class immutable candle artifact manifest."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hashing_contract import artifact_manifest_hash
from .locators import ContentAddressedLocal, LocatorValidationError, parse_immutable_locator

ARTIFACT_MANIFEST_SCHEMA_VERSION = 1


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
    canonicalization_name: str
    canonicalization_version: int
    artifact_manifest_hash: str

    def stable_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "artifact_type": self.artifact_type,
            "artifact_id": self.artifact_id, "format": self.format,
            "artifact": {"content_hash": self.content_hash, "schema_hash": self.schema_hash,
                         "row_count": self.row_count},
            "scope": {"market": self.market, "interval": self.interval,
                      "start_ts": self.start_ts, "end_ts": self.end_ts},
            "canonicalization": {"name": self.canonicalization_name,
                                   "version": self.canonicalization_version},
        }

    def as_dict(self) -> dict[str, Any]:
        payload = self.stable_payload()
        payload["artifact"]["uri"] = self.locator.path
        payload["locator"] = self.locator.as_dict()
        payload["artifact_manifest_hash"] = self.artifact_manifest_hash
        return payload


def build_artifact_manifest(*, artifact_id: str, path: str, content_hash: str, schema_hash: str,
                            row_count: int, market: str, interval: str, start_ts: int,
                            end_ts: int) -> ArtifactManifest:
    # The manifest hash uses stable logical evidence, intentionally excluding
    # the operator-specific absolute path and the locator's self-binding hash.
    stable = {"schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
              "artifact_type": "immutable_candle_dataset", "artifact_id": artifact_id,
              "format": "sqlite", "artifact": {"content_hash": content_hash,
              "schema_hash": schema_hash, "row_count": int(row_count)},
              "scope": {"market": market, "interval": interval, "start_ts": int(start_ts), "end_ts": int(end_ts)},
              "canonicalization": {"name": "ohlcv_pair_interval_rows", "version": 1}}
    digest = artifact_manifest_hash(stable)
    locator = ContentAddressedLocal(path=str(Path(path).resolve()), artifact_manifest_hash=digest,
                                    artifact_content_hash=content_hash)
    return ArtifactManifest(schema_version=ARTIFACT_MANIFEST_SCHEMA_VERSION,
        artifact_type="immutable_candle_dataset", artifact_id=artifact_id, format="sqlite",
        locator=locator, content_hash=content_hash, schema_hash=schema_hash, row_count=int(row_count),
        market=market, interval=interval, start_ts=int(start_ts), end_ts=int(end_ts),
        canonicalization_name="ohlcv_pair_interval_rows", canonicalization_version=1,
        artifact_manifest_hash=digest)


def parse_artifact_manifest(payload: dict[str, Any]) -> ArtifactManifest:
    if not isinstance(payload, dict):
        raise ArtifactManifestError("artifact_manifest_must_be_object")
    if payload.get("schema_version") != ARTIFACT_MANIFEST_SCHEMA_VERSION:
        raise ArtifactManifestError("artifact_manifest_schema_version_unsupported")
    expected = payload.get("artifact_manifest_hash")
    if not isinstance(expected, str) or artifact_manifest_hash(_stable_from_payload(payload)) != expected:
        raise ArtifactManifestError("artifact_manifest_hash_mismatch")
    try:
        locator = parse_immutable_locator(payload.get("locator"))
    except LocatorValidationError as exc:
        raise ArtifactManifestError(str(exc)) from exc
    artifact = payload.get("artifact")
    scope = payload.get("scope")
    canonicalization = payload.get("canonicalization")
    if not all(isinstance(x, dict) for x in (artifact, scope, canonicalization)):
        raise ArtifactManifestError("artifact_manifest_sections_invalid")
    required = ("artifact_id", "artifact_type", "format")
    if any(not isinstance(payload.get(k), str) or not payload[k] for k in required):
        raise ArtifactManifestError("artifact_manifest_identity_invalid")
    if payload["artifact_type"] != "immutable_candle_dataset" or payload["format"] != "sqlite":
        raise ArtifactManifestError("artifact_manifest_type_unsupported")
    try:
        result = ArtifactManifest(schema_version=1, artifact_type=payload["artifact_type"], artifact_id=payload["artifact_id"],
            format=payload["format"], locator=locator, content_hash=_hash(artifact.get("content_hash")),
            schema_hash=_hash(artifact.get("schema_hash")), row_count=int(artifact["row_count"]),
            market=_text(scope.get("market")), interval=_text(scope.get("interval")), start_ts=int(scope["start_ts"]),
            end_ts=int(scope["end_ts"]), canonicalization_name=_text(canonicalization.get("name")),
            canonicalization_version=int(canonicalization["version"]), artifact_manifest_hash=expected)
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactManifestError("artifact_manifest_values_invalid") from exc
    if result.locator.artifact_manifest_hash != expected or result.locator.artifact_content_hash != result.content_hash:
        raise ArtifactManifestError("artifact_manifest_locator_binding_mismatch")
    return result


def load_artifact_manifest(path: str | Path, expected_hash: str | None = None) -> ArtifactManifest:
    manifest_path = Path(path).expanduser()
    if not manifest_path.is_absolute():
        raise ArtifactManifestError("artifact_manifest_uri_must_be_absolute")
    try:
        with manifest_path.open(encoding="utf-8") as handle:
            parsed = parse_artifact_manifest(json.load(handle))
    except OSError as exc:
        raise ArtifactManifestError("artifact_manifest_unavailable") from exc
    if expected_hash is not None and parsed.artifact_manifest_hash != expected_hash:
        raise ArtifactManifestError("artifact_manifest_reference_hash_mismatch")
    return parsed


def _stable_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    artifact = payload.get("artifact") or {}
    scope = payload.get("scope") or {}
    canonicalization = payload.get("canonicalization") or {}
    return {"schema_version": payload.get("schema_version"), "artifact_type": payload.get("artifact_type"),
            "artifact_id": payload.get("artifact_id"), "format": payload.get("format"),
            "artifact": {k: artifact.get(k) for k in ("content_hash", "schema_hash", "row_count")},
            "scope": {k: scope.get(k) for k in ("market", "interval", "start_ts", "end_ts")},
            "canonicalization": {k: canonicalization.get(k) for k in ("name", "version")}}


def _hash(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
        raise ValueError("hash")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("text")
    return value
