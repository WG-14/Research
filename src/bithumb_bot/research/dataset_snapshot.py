from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bithumb_bot.public_api_minute_candles import interval_to_minute_unit

from .experiment_manifest import DateRange, ExperimentManifest, ManifestValidationError
from .hashing import sha256_prefixed


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def as_tuple(self) -> tuple[int, float, float, float, float, float]:
        return (self.ts, self.open, self.high, self.low, self.close, self.volume)


@dataclass(frozen=True)
class DatasetSnapshot:
    snapshot_id: str
    source: str
    market: str
    interval: str
    split_name: str
    date_range: DateRange
    candles: tuple[Candle, ...]

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "source": self.source,
            "market": self.market,
            "interval": self.interval,
            "split_name": self.split_name,
            "date_range": self.date_range.as_dict(),
            "candles": [candle.as_tuple() for candle in self.candles],
        }

    def content_hash(self) -> str:
        return sha256_prefixed(self.fingerprint_payload())


@dataclass(frozen=True)
class DatasetQualityReport:
    payload: dict[str, Any]

    @property
    def content_hash(self) -> str:
        return str(self.payload["content_hash"])

    @property
    def quality_gate_status(self) -> str:
        return str(self.payload["quality_gate_status"])

    @property
    def quality_gate_reasons(self) -> tuple[str, ...]:
        return tuple(str(reason) for reason in self.payload.get("quality_gate_reasons", ()))


def load_dataset_split(
    *,
    db_path: str | Path,
    manifest: ExperimentManifest,
    split_name: str,
) -> DatasetSnapshot:
    date_range = _split_range(manifest, split_name)
    return load_dataset_range(db_path=db_path, manifest=manifest, split_name=split_name, date_range=date_range)


def load_dataset_range(
    *,
    db_path: str | Path,
    manifest: ExperimentManifest,
    split_name: str,
    date_range: DateRange,
) -> DatasetSnapshot:
    conn = sqlite3.connect(f"file:{Path(db_path).expanduser().resolve()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT ts, open, high, low, close, volume
            FROM candles
            WHERE pair=? AND interval=? AND ts >= ? AND ts <= ?
            ORDER BY ts ASC
            """,
            (
                manifest.market,
                manifest.interval,
                date_range.start_ts_ms(),
                date_range.end_ts_ms(),
            ),
        ).fetchall()
    finally:
        conn.close()
    candles = tuple(
        Candle(
            ts=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5] or 0.0),
        )
        for row in rows
    )
    return DatasetSnapshot(
        snapshot_id=manifest.dataset.snapshot_id,
        source=manifest.dataset.source,
        market=manifest.market,
        interval=manifest.interval,
        split_name=split_name,
        date_range=date_range,
        candles=candles,
    )


def build_dataset_quality_report(
    *,
    db_path: str | Path,
    snapshot: DatasetSnapshot,
) -> DatasetQualityReport:
    interval_ms = _interval_ms(snapshot.interval)
    expected_ts = tuple(range(snapshot.date_range.start_ts_ms(), snapshot.date_range.end_ts_ms() + 1, interval_ms))
    expected_set = set(expected_ts)
    candles = snapshot.candles
    actual_ts = [candle.ts for candle in candles]
    actual_ts_set = set(actual_ts)
    missing_ts = [ts for ts in expected_ts if ts not in actual_ts_set]
    missing_ranges = _compact_missing_ranges(missing_ts, interval_ms)
    duplicate_key_count = _duplicate_key_count(db_path=db_path, snapshot=snapshot)
    non_monotonic = sum(1 for prev, curr in zip(actual_ts, actual_ts[1:]) if curr <= prev)
    interval_mismatch = sum(
        1
        for prev, curr in zip(actual_ts, actual_ts[1:])
        if curr > prev and (curr - prev) != interval_ms
    )
    ohlc_violations = 0
    non_positive_prices = 0
    negative_volume = 0
    for candle in candles:
        if not (
            candle.low <= candle.open <= candle.high
            and candle.low <= candle.close <= candle.high
            and candle.low <= candle.high
        ):
            ohlc_violations += 1
        if candle.open <= 0.0 or candle.high <= 0.0 or candle.low <= 0.0 or candle.close <= 0.0:
            non_positive_prices += 1
        if candle.volume < 0.0:
            negative_volume += 1

    reasons: list[str] = []
    if missing_ts:
        reasons.append("missing_candles")
    if duplicate_key_count:
        reasons.append("duplicate_candle_keys")
    if non_monotonic:
        reasons.append("non_monotonic_timestamps")
    if interval_mismatch:
        reasons.append("interval_mismatch")
    if ohlc_violations:
        reasons.append("ohlc_invariant_violation")
    if non_positive_prices:
        reasons.append("non_positive_price")
    if negative_volume:
        reasons.append("negative_volume")
    if actual_ts and (min(actual_ts) < snapshot.date_range.start_ts_ms() or max(actual_ts) > snapshot.date_range.end_ts_ms()):
        reasons.append("timestamp_outside_split_range")
    unexpected_count = sum(1 for ts in actual_ts if ts not in expected_set)
    if unexpected_count:
        reasons.append("unexpected_candle_bucket")

    expected_count = len(expected_ts)
    actual_count = len(candles)
    coverage_pct = (actual_count / expected_count * 100.0) if expected_count else 0.0
    payload: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "dataset_quality_report",
        "source": snapshot.source,
        "market": snapshot.market,
        "interval": snapshot.interval,
        "snapshot_id": snapshot.snapshot_id,
        "split_name": snapshot.split_name,
        "start_ts": snapshot.date_range.start_ts_ms(),
        "end_ts": snapshot.date_range.end_ts_ms(),
        "expected_candle_count": expected_count,
        "actual_candle_count": actual_count,
        "coverage_pct": round(coverage_pct, 8),
        "missing_bucket_count": len(missing_ts),
        "missing_bucket_ranges": missing_ranges,
        "missing_bucket_sample": missing_ts[:20],
        "duplicate_key_count": duplicate_key_count,
        "non_monotonic_ts_count": non_monotonic,
        "interval_mismatch_count": interval_mismatch,
        "unexpected_bucket_count": unexpected_count,
        "ohlc_violation_count": ohlc_violations,
        "non_positive_price_count": non_positive_prices,
        "negative_volume_count": negative_volume,
        "first_ts": actual_ts[0] if actual_ts else None,
        "last_ts": actual_ts[-1] if actual_ts else None,
        "db_schema_fingerprint": _db_schema_fingerprint(db_path),
        "dataset_content_hash": snapshot.content_hash(),
        "quality_gate_status": "PASS" if not reasons else "FAIL",
        "quality_gate_reasons": reasons,
        "limitations": {
            "orderbook_depth_available": False,
            "top_of_book_available": False,
            "intra_candle_path_available": False,
            "execution_reference_price": "candle_close",
            "intra_candle_policy": "close_price_only_no_intracandle_path",
        },
    }
    payload["content_hash"] = sha256_prefixed(payload)
    return DatasetQualityReport(payload=payload)


def combined_dataset_fingerprint(snapshots: tuple[DatasetSnapshot, ...]) -> str:
    return sha256_prefixed([snapshot.fingerprint_payload() for snapshot in snapshots])


def combined_dataset_quality_hash(reports: tuple[DatasetQualityReport, ...]) -> str:
    return sha256_prefixed([report.payload for report in reports])


def _split_range(manifest: ExperimentManifest, split_name: str) -> DateRange:
    if split_name == "train":
        return manifest.dataset.split.train
    if split_name == "validation":
        return manifest.dataset.split.validation
    if split_name == "final_holdout" and manifest.dataset.split.final_holdout is not None:
        return manifest.dataset.split.final_holdout
    raise ValueError(f"unknown or unavailable dataset split: {split_name}")


def _interval_ms(interval: str) -> int:
    try:
        return interval_to_minute_unit(interval) * 60_000
    except ValueError as exc:
        raise ManifestValidationError(f"unsupported dataset interval for quality report: {interval}") from exc


def _compact_missing_ranges(missing_ts: list[int], interval_ms: int, *, max_ranges: int = 20) -> list[dict[str, int]]:
    if not missing_ts:
        return []
    ranges: list[dict[str, int]] = []
    start = missing_ts[0]
    prev = missing_ts[0]
    count = 1
    for ts in missing_ts[1:]:
        if ts == prev + interval_ms:
            prev = ts
            count += 1
            continue
        ranges.append({"start_ts": start, "end_ts": prev, "bucket_count": count})
        start = prev = ts
        count = 1
    ranges.append({"start_ts": start, "end_ts": prev, "bucket_count": count})
    return ranges[:max_ranges]


def _duplicate_key_count(*, db_path: str | Path, snapshot: DatasetSnapshot) -> int:
    conn = sqlite3.connect(f"file:{Path(db_path).expanduser().resolve()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT COUNT(*) - COUNT(DISTINCT ts)
            FROM candles
            WHERE pair=? AND interval=? AND ts >= ? AND ts <= ?
            """,
            (
                snapshot.market,
                snapshot.interval,
                snapshot.date_range.start_ts_ms(),
                snapshot.date_range.end_ts_ms(),
            ),
        ).fetchone()
    finally:
        conn.close()
    return int(rows[0] or 0) if rows else 0


def _db_schema_fingerprint(db_path: str | Path) -> str:
    conn = sqlite3.connect(f"file:{Path(db_path).expanduser().resolve()}?mode=ro", uri=True)
    try:
        table_info = [tuple(row) for row in conn.execute("PRAGMA table_info(candles)").fetchall()]
        index_list = [tuple(row) for row in conn.execute("PRAGMA index_list(candles)").fetchall()]
        index_info = {
            str(index[1]): [tuple(row) for row in conn.execute(f"PRAGMA index_info({str(index[1])})").fetchall()]
            for index in index_list
        }
    finally:
        conn.close()
    return sha256_prefixed(
        {
            "table": "candles",
            "table_info": table_info,
            "index_list": index_list,
            "index_info": index_info,
        }
    )
