"""Explicit, domain-separated hashes for dataset artifacts and snapshots.

These helpers deliberately use different hash domains.  Equal candle rows in an
artifact and a materialized snapshot therefore do not accidentally become
interchangeable evidence merely because their serialized row values match.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..hashing import sha256_prefixed


def canonical_candle_rows(rows: Iterable[tuple[Any, ...]]) -> list[dict[str, object]]:
    """Return the candle-row canonicalization shared by artifact and snapshot hashing."""
    return [
        {
            "ts": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5] or 0.0),
        }
        for row in rows
    ]


def artifact_content_hash(rows: Iterable[tuple[Any, ...]]) -> str:
    """Hash the complete data content declared by one immutable artifact."""
    return sha256_prefixed(
        {"hash_domain": "artifact_content_v1", "candle_rows": canonical_candle_rows(rows)},
        label="artifact_content_hash",
    )


def artifact_schema_hash(schema: Mapping[str, Any]) -> str:
    """Hash an artifact's declared physical schema, independently of its rows."""
    return sha256_prefixed(
        {"hash_domain": "artifact_schema_v1", "schema": dict(schema)},
        label="artifact_schema_hash",
    )


def artifact_manifest_hash(manifest: Mapping[str, Any]) -> str:
    """Hash a manifest payload excluding its self-referential hash field."""
    payload = {key: value for key, value in manifest.items() if key != "artifact_manifest_hash"}
    return sha256_prefixed(
        {"hash_domain": "artifact_manifest_v1", "manifest": payload},
        label="artifact_manifest_hash",
    )


def snapshot_data_hash(
    *,
    candle_rows: Iterable[tuple[Any, ...]],
    execution_evidence: Mapping[str, Any],
) -> str:
    """Hash materialized rows and execution evidence, excluding query and split role."""
    return sha256_prefixed(
        {
            "hash_domain": "snapshot_data_v1",
            "candle_rows": canonical_candle_rows(candle_rows),
            "execution_evidence": dict(execution_evidence),
        },
        label="snapshot_data_hash",
    )


def snapshot_query_hash(
    *,
    market: str,
    interval: str,
    start_ts: int,
    end_ts: int,
    filters: Mapping[str, Any] | None = None,
) -> str:
    """Hash the requested materialization slice, independently of row content."""
    return sha256_prefixed(
        {
            "hash_domain": "snapshot_query_v1",
            "market": str(market),
            "interval": str(interval),
            "requested_range": {"start_ts": int(start_ts), "end_ts": int(end_ts)},
            "filters": dict(filters or {}),
        },
        label="snapshot_query_hash",
    )


def snapshot_fingerprint_hash(
    *,
    artifact_identity: Mapping[str, Any],
    data_hash: str,
    query_hash: str,
    split_role: str,
    adapter_version: str | None,
) -> str:
    """Bind immutable-artifact identity, materialized data, query, and split role."""
    return sha256_prefixed(
        {
            "hash_domain": "snapshot_fingerprint_v1",
            "artifact_identity": dict(artifact_identity),
            "snapshot_data_hash": str(data_hash),
            "snapshot_query_hash": str(query_hash),
            "split_role": str(split_role),
            "adapter_version": str(adapter_version or "unknown"),
        },
        label="snapshot_fingerprint_hash",
    )
