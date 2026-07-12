from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..storage_io import write_json_atomic
from .datasets.hashing_contract import (
    artifact_content_hash,
    artifact_manifest_hash,
    artifact_schema_hash,
)


class DatasetFreezeError(ValueError):
    pass


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _reject_repo_path(path: Path, *, label: str) -> None:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(_repo_root())
    except ValueError:
        return
    raise DatasetFreezeError(f"{label}_inside_repository")


def canonical_candle_rows_hash(rows: list[tuple[Any, ...]]) -> str:
    """Compatibility spelling for the artifact-content hash contract."""
    return artifact_content_hash(rows)


def sqlite_candles_schema_hash(db_path: str | Path) -> str:
    conn = sqlite3.connect(f"file:{Path(db_path).expanduser().resolve()}?mode=ro", uri=True)
    try:
        table_info = [tuple(row) for row in conn.execute("PRAGMA table_info(candles)").fetchall()]
        return artifact_schema_hash({"table": "candles", "table_info": table_info})
    finally:
        conn.close()


def freeze_sqlite_candles_dataset(
    *,
    source_db: str | Path,
    market: str,
    interval: str,
    start_ts: int,
    end_ts: int,
    out_dir: str | Path,
) -> dict[str, Any]:
    source = Path(source_db).expanduser().resolve()
    out_root = Path(out_dir).expanduser()
    if not out_root.is_absolute():
        raise DatasetFreezeError("research_freeze_dataset_rejects_repo_relative_output")
    _reject_repo_path(out_root, label="research_freeze_dataset_output")
    if "/paper/" in str(source).replace("\\", "/"):
        raise DatasetFreezeError("research_freeze_dataset_rejects_paper_locator_for_validation")

    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT ts, open, high, low, close, volume
            FROM candles
            WHERE pair=? AND interval=? AND ts>=? AND ts<=?
            ORDER BY ts ASC
            """,
            (market, interval, int(start_ts), int(end_ts)),
        ).fetchall()
    finally:
        conn.close()
    artifact_content = artifact_content_hash(rows)
    source_schema_hash = sqlite_candles_schema_hash(source)
    digest = artifact_content.split(":", 1)[1]
    artifact_dir = out_root / "candles" / market / interval
    artifact_path = artifact_dir / f"{digest}.sqlite"
    manifest_path = artifact_dir / f"{digest}.manifest.json"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    out_conn = sqlite3.connect(artifact_path)
    try:
        out_conn.execute(
            """
            CREATE TABLE candles (
                pair TEXT NOT NULL,
                interval TEXT NOT NULL,
                ts INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                PRIMARY KEY(pair, interval, ts)
            )
            """
        )
        out_conn.executemany(
            "INSERT INTO candles(pair, interval, ts, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(market, interval, *tuple(row)) for row in rows],
        )
        out_conn.commit()
    finally:
        out_conn.close()
    artifact_schema_hash = sqlite_candles_schema_hash(artifact_path)
    if artifact_content_hash(_read_candle_rows(artifact_path, market=market, interval=interval, start_ts=start_ts, end_ts=end_ts)) != artifact_content:
        raise DatasetFreezeError("research_freeze_dataset_hash_verification_failed")
    manifest_fragment = {
        "source": "frozen_sqlite_candles",
        "source_uri": str(artifact_path),
        # Compatibility input only.  PR-2 replaces this fragment with the
        # first-class artifact-manifest reference.
        "source_content_hash": artifact_content,
        "source_schema_hash": artifact_schema_hash,
        "locator": {"type": "immutable_sqlite", "path": str(artifact_path)},
    }
    payload = {
        "artifact_type": "immutable_candle_dataset_freeze",
        "format": "sqlite",
        "source_uri": str(source),
        "artifact_content_hash": artifact_content,
        "artifact_schema_hash": artifact_schema_hash,
        "source_content_hash": artifact_content,
        "source_schema_hash": artifact_schema_hash,
        "source_input_schema_hash": source_schema_hash,
        "locator": manifest_fragment["locator"],
        "manifest_fragment": manifest_fragment,
        "canonical_row_hash": artifact_content,
        "row_count": len(rows),
        "market": market,
        "interval": interval,
        "start": int(start_ts),
        "end": int(end_ts),
        "artifact_path": str(artifact_path),
        "manifest_path": str(manifest_path),
    }
    payload["artifact_manifest_hash"] = artifact_manifest_hash(payload)
    write_json_atomic(manifest_path, payload)
    return payload


def _read_candle_rows(db_path: str | Path, *, market: str, interval: str, start_ts: int, end_ts: int) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(f"file:{Path(db_path).expanduser().resolve()}?mode=ro", uri=True)
    try:
        return conn.execute(
            """
            SELECT ts, open, high, low, close, volume
            FROM candles
            WHERE pair=? AND interval=? AND ts>=? AND ts<=?
            ORDER BY ts ASC
            """,
            (market, interval, int(start_ts), int(end_ts)),
        ).fetchall()
    finally:
        conn.close()


def cmd_research_freeze_dataset(
    *,
    db_path: str,
    market: str,
    interval: str,
    start: str,
    end: str,
    out_path: str,
) -> int:
    from .experiment_manifest import DateRange

    date_range = DateRange(start=start, end=end)
    payload = freeze_sqlite_candles_dataset(
        source_db=db_path,
        market=market,
        interval=interval,
        start_ts=date_range.start_ts_ms(),
        end_ts=date_range.end_ts_ms(),
        out_dir=out_path,
    )
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return 0
