"""Explicit, domain-separated hashes for dataset artifacts and snapshots.

These helpers deliberately use different hash domains.  Equal candle rows in an
artifact and a materialized snapshot therefore do not accidentally become
interchangeable evidence merely because their serialized row values match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..hashing import sha256_prefixed


def canonical_candle_rows(rows: Iterable[tuple[Any, ...]]) -> list[dict[str, object]]:
    """Canonical snapshot rows (the materialized snapshot has no pair column)."""
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


@dataclass(frozen=True)
class ArtifactCandleRow:
    pair: str
    interval: str
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def canonical_payload(self) -> dict[str, object]:
        return {"pair": self.pair, "interval": self.interval, "ts": self.ts,
                "open": self.open, "high": self.high, "low": self.low,
                "close": self.close, "volume": self.volume}


def canonical_artifact_rows(*, market: str | None, interval: str | None,
                            rows: Iterable[tuple[Any, ...]]) -> list[dict[str, object]]:
    """Canonical full artifact rows with market and interval in the hash domain.

    Callers may pass full 8-column SQLite rows, or explicitly supply market and
    interval for 6-column OHLCV projections.  Ambiguous six-column input is
    rejected rather than being silently assigned a hash meaning.
    """
    output: list[dict[str, object]] = []
    for row in rows:
        if len(row) == 8:
            pair, row_interval, ts, open_, high, low, close, volume = row
        elif len(row) == 6 and market is not None and interval is not None:
            pair, row_interval = market, interval
            ts, open_, high, low, close, volume = row
        else:
            raise ValueError("artifact_content_hash_requires_pair_and_interval")
        output.append(ArtifactCandleRow(str(pair), str(row_interval), int(ts), float(open_),
            float(high), float(low), float(close), float(volume or 0.0)).canonical_payload())
    return output


def artifact_content_hash(rows: Iterable[tuple[Any, ...]], *, market: str | None = None,
                          interval: str | None = None) -> str:
    """Hash complete artifact rows using fixed pair/interval/OHLCV schema."""
    return sha256_prefixed(
        {"hash_domain": "artifact_content_v2", "artifact_row_schema":
         ["pair", "interval", "ts", "open", "high", "low", "close", "volume"],
         "candle_rows": canonical_artifact_rows(market=market, interval=interval, rows=rows)},
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
    dataset_options: Mapping[str, Any] | None = None,
    top_of_book: Mapping[str, Any] | None = None,
    depth: Mapping[str, Any] | None = None,
    execution: Mapping[str, Any] | None = None,
) -> str:
    """Hash the requested materialization slice, independently of row content."""
    return sha256_prefixed(
        {
            "hash_domain": "snapshot_query_v1",
            "market": str(market),
            "interval": str(interval),
            "requested_range": {"start_ts": int(start_ts), "end_ts": int(end_ts)},
            "dataset_options": dict(dataset_options or {}),
            "top_of_book": dict(top_of_book or {}),
            "depth": dict(depth or {}),
            "execution": dict(execution or {}),
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
