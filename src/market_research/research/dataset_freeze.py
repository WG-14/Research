from __future__ import annotations

import json
import os
import errno
import shutil
import sqlite3
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..storage_io import write_json_atomic
from .datasets.artifact_manifest import ArtifactManifestError, build_artifact_manifest, load_artifact_manifest
from .datasets.hashing_contract import artifact_content_hash, artifact_schema_hash, dataset_artifact_key_hash
from .datasets.source_provenance import (
    DatasetSourceProvenance,
    load_dataset_source_provenance,
    validate_source_coverage,
)
from market_research.research.intervals import interval_to_milliseconds


class DatasetFreezeError(ValueError):
    pass


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _reject_repo_path(path: Path, *, label: str) -> None:
    try:
        path.expanduser().resolve().relative_to(_repo_root())
    except ValueError:
        return
    raise DatasetFreezeError(f"{label}_inside_repository")


def canonical_candle_rows_hash(rows: list[tuple[Any, ...]], *, market: str, interval: str) -> str:
    """Compatibility spelling with explicit artifact identity inputs."""
    return artifact_content_hash(rows, market=market, interval=interval)


def sqlite_candles_schema_hash(db_path: str | Path) -> str:
    conn = sqlite3.connect(f"file:{Path(db_path).expanduser().resolve()}?mode=ro", uri=True)
    try:
        table_info = [tuple(row) for row in conn.execute("PRAGMA table_info(candles)").fetchall()]
        index_list = [tuple(row) for row in conn.execute("PRAGMA index_list(candles)").fetchall()]
        return artifact_schema_hash({"table": "candles", "table_info": table_info, "index_list": index_list})
    finally:
        conn.close()


def freeze_sqlite_candles_dataset(*, source_db: str | Path, market: str, interval: str,
                                  start_ts: int, end_ts: int, out_dir: str | Path,
                                  source_provenance: DatasetSourceProvenance,
                                  failure_stage: str | None = None) -> dict[str, Any]:
    """Publish a verified directory bundle using atomicity-only durability.

    The policy intentionally promises atomic visibility, not power-loss
    durability.  Both files are fsynced before the same-filesystem directory
    rename; parent-directory fsync is not part of this contract.
    ``failure_stage`` is a deterministic test hook and is not exposed by CLI.
    """
    source = Path(source_db).expanduser().resolve()
    out_root = Path(out_dir).expanduser()
    if not out_root.is_absolute():
        raise DatasetFreezeError("research_freeze_dataset_rejects_repo_relative_output")
    _reject_repo_path(out_root, label="research_freeze_dataset_output")
    validate_source_coverage(source_provenance, start_ts=int(start_ts), end_ts=int(end_ts))
    rows = _read_candle_rows(source, market=market, interval=interval, start_ts=start_ts, end_ts=end_ts)
    content_hash = artifact_content_hash(rows, market=market, interval=interval)
    artifact_key_hash = dataset_artifact_key_hash(
        content_hash=content_hash,
        source_provenance_hash=source_provenance.provenance_manifest_hash,
    )
    digest = artifact_key_hash.split(":", 1)[1]
    artifact_dir = out_root / "candles" / market / interval / digest
    artifact_path = artifact_dir / "candles.sqlite"
    manifest_path = artifact_dir / "artifact.manifest.json"
    # Artifacts are a directory bundle. A final directory is only visible after
    # both files have been verified and atomically renamed into place.
    expected_schema = _canonical_candles_schema_hash()
    if artifact_dir.exists():
        return _reuse_existing(artifact_dir=artifact_dir, artifact_path=artifact_path, manifest_path=manifest_path,
            market=market, interval=interval, start_ts=start_ts, end_ts=end_ts, expected_content=content_hash,
            expected_schema=expected_schema, expected_provenance_hash=source_provenance.provenance_manifest_hash)
    artifact_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{digest}.staging-", dir=artifact_dir.parent))
    staging_db = staging / "candles.sqlite"
    staging_manifest = staging / "artifact.manifest.json"
    try:
        _write_sqlite(staging_db, rows=rows, market=market, interval=interval,
                      failure_stage=failure_stage)
        _fail_if_requested(failure_stage, "during_db_creation")
        schema_hash = sqlite_candles_schema_hash(staging_db)
        artifact_id = f"immutable-candle:{artifact_key_hash}"
        final_manifest = build_artifact_manifest(artifact_id=artifact_id, path=str(artifact_path), content_hash=content_hash,
            schema_hash=schema_hash, row_count=len(rows), market=market, interval=interval,
            start_ts=int(start_ts), end_ts=int(end_ts), coverage_start_ts=int(start_ts),
            coverage_end_ts=_coverage_end_ts(end_ts=int(end_ts), interval=interval),
            source_provenance=source_provenance)
        write_json_atomic(staging_manifest, final_manifest.as_dict())
        _fail_if_requested(failure_stage, "during_manifest_creation")
        _verify_bundle(staging_db, staging_manifest, market=market, interval=interval, start_ts=start_ts,
            end_ts=end_ts, expected_content=content_hash,
            expected_provenance_hash=source_provenance.provenance_manifest_hash, committed=False)
        # ensure file contents are flushed before publishing the directory
        _fsync_file(staging_db); _fsync_file(staging_manifest)
        _fail_if_requested(failure_stage, "after_verification_before_rename")
        try:
            _fail_if_requested(failure_stage, "during_final_publication")
            os.replace(staging, artifact_dir)
        except OSError as exc:
            if not _is_destination_conflict(exc):
                raise
            shutil.rmtree(staging, ignore_errors=True)
            return _reuse_existing(artifact_dir=artifact_dir, artifact_path=artifact_path, manifest_path=manifest_path,
                market=market, interval=interval, start_ts=start_ts, end_ts=end_ts, expected_content=content_hash,
                expected_schema=expected_schema,
                expected_provenance_hash=source_provenance.provenance_manifest_hash)
        manifest = _verify_bundle(artifact_path, manifest_path, market=market, interval=interval,
            start_ts=start_ts, end_ts=end_ts, expected_content=content_hash,
            expected_provenance_hash=source_provenance.provenance_manifest_hash, committed=True)
        return _result(manifest, artifact_path, manifest_path, reused_existing=False)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _write_sqlite(path: Path, *, rows: list[tuple[Any, ...]], market: str, interval: str,
                  failure_stage: str | None = None) -> None:
    # `path` is always a same-filesystem staging path, never a published path.
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE candles (pair TEXT NOT NULL, interval TEXT NOT NULL, ts INTEGER NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL, PRIMARY KEY(pair, interval, ts))")
        for index, row in enumerate(rows):
            conn.execute("INSERT INTO candles(pair, interval, ts, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (market, interval, *tuple(row)))
            if index == 0:
                _fail_if_requested(failure_stage, "during_db_write")
        conn.commit()
    finally:
        conn.close()


def _reuse_existing(*, artifact_dir: Path, artifact_path: Path, manifest_path: Path, market: str, interval: str,
                    start_ts: int, end_ts: int, expected_content: str, expected_schema: str,
                    expected_provenance_hash: str) -> dict[str, Any]:
    if not artifact_dir.is_dir():
        raise DatasetFreezeError("existing_artifact_path_conflict")
    try:
        manifest = _verify_bundle(artifact_path, manifest_path, market=market, interval=interval, start_ts=start_ts,
            end_ts=end_ts, expected_content=expected_content, expected_schema=expected_schema,
            expected_provenance_hash=expected_provenance_hash, committed=True)
    except DatasetFreezeError as exc:
        if str(exc) in {"artifact_bundle_incomplete", "existing_artifact_schema_conflict", "artifact_scope_verification_failed"}:
            raise
        raise DatasetFreezeError("existing_artifact_invalid_or_tampered") from exc
    except (ArtifactManifestError, OSError, sqlite3.Error, ValueError) as exc:
        raise DatasetFreezeError("existing_artifact_invalid_or_tampered") from exc
    return _result(manifest, artifact_path, manifest_path, reused_existing=True)


def _verify_bundle(db_path: Path, manifest_path: Path, *, market: str, interval: str, start_ts: int, end_ts: int,
                   expected_content: str, expected_schema: str | None = None,
                   expected_provenance_hash: str, committed: bool):
    if not db_path.is_file() or not manifest_path.is_file():
        raise DatasetFreezeError("artifact_bundle_incomplete")
    if committed:
        manifest = load_artifact_manifest(manifest_path)
    else:
        # The staged sidecar already names its eventual committed DB path, so
        # only parse it here.  Binding is enforced after the atomic rename.
        from .datasets.artifact_manifest import parse_artifact_manifest
        manifest = parse_artifact_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
    rows = _read_all_artifact_rows(db_path)
    actual = artifact_content_hash(rows)
    if actual != expected_content or actual != manifest.content_hash:
        raise DatasetFreezeError("artifact_content_hash_verification_failed")
    actual_schema = sqlite_candles_schema_hash(db_path)
    if actual_schema != manifest.schema_hash:
        raise DatasetFreezeError(
            "existing_artifact_schema_conflict" if committed else "artifact_schema_hash_verification_failed"
        )
    if expected_schema is not None and actual_schema != expected_schema:
        raise DatasetFreezeError("existing_artifact_schema_conflict")
    if len(rows) != manifest.row_count:
        raise DatasetFreezeError("artifact_row_count_verification_failed")
    if manifest.source_provenance.provenance_manifest_hash != expected_provenance_hash:
        raise DatasetFreezeError("artifact_source_provenance_verification_failed")
    actual_pairs = {(str(row[0]), str(row[1])) for row in rows}
    if actual_pairs != {(market, interval)} or (manifest.market, manifest.interval) != (market, interval):
        raise DatasetFreezeError("artifact_market_interval_verification_failed")
    actual_scope = (min((int(row[2]) for row in rows), default=int(start_ts)), max((int(row[2]) for row in rows), default=int(end_ts)))
    actual_coverage = (actual_scope[0], _coverage_end_ts(end_ts=actual_scope[1], interval=interval))
    if (actual_scope != (int(start_ts), int(end_ts))
            or (manifest.start_ts, manifest.end_ts) != actual_scope
            or (manifest.coverage_start_ts, manifest.coverage_end_ts) != actual_coverage):
        raise DatasetFreezeError("artifact_scope_verification_failed")
    return manifest


@lru_cache(maxsize=1)
def _canonical_candles_schema_hash() -> str:
    """Schema expected from this freezer's fixed immutable candle contract."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE candles (pair TEXT NOT NULL, interval TEXT NOT NULL, ts INTEGER NOT NULL, "
            "open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, "
            "volume REAL NOT NULL, PRIMARY KEY(pair, interval, ts))"
        )
        table_info = [tuple(row) for row in conn.execute("PRAGMA table_info(candles)").fetchall()]
        index_list = [tuple(row) for row in conn.execute("PRAGMA index_list(candles)").fetchall()]
        return artifact_schema_hash({"table": "candles", "table_info": table_info, "index_list": index_list})
    finally:
        conn.close()


def _fail_if_requested(requested: str | None, stage: str) -> None:
    if requested == stage:
        raise DatasetFreezeError(f"freeze_failure_injected:{stage}")


def _coverage_end_ts(*, end_ts: int, interval: str) -> int:
    return int(end_ts) + interval_to_milliseconds(interval) - 1


def _is_destination_conflict(exc: OSError) -> bool:
    """Same-filesystem publisher conflict across POSIX and Windows errno forms."""
    return isinstance(exc, FileExistsError) or exc.errno in {errno.EEXIST, errno.ENOTEMPTY}


def _result(manifest, artifact_path: Path, manifest_path: Path, *, reused_existing: bool) -> dict[str, Any]:
    return {"artifact_id": manifest.artifact_id, "artifact_path": str(artifact_path), "manifest_path": str(manifest_path),
            "artifact_manifest_uri": str(manifest_path), "artifact_manifest_hash": manifest.artifact_manifest_hash,
            "artifact_content_hash": manifest.content_hash, "artifact_schema_hash": manifest.schema_hash,
            "row_count": manifest.row_count, "market": manifest.market, "interval": manifest.interval,
            "start_ts": manifest.start_ts, "end_ts": manifest.end_ts,
            "coverage_start_ts": manifest.coverage_start_ts, "coverage_end_ts": manifest.coverage_end_ts,
            "source_provenance": manifest.source_provenance.as_dict(),
            "source_provenance_hash": manifest.source_provenance.provenance_manifest_hash,
            "locator": manifest.locator.as_dict(),
            "reused_existing": reused_existing}


def _read_candle_rows(db_path: str | Path, *, market: str, interval: str, start_ts: int, end_ts: int) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(f"file:{Path(db_path).expanduser().resolve()}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT ts, open, high, low, close, volume FROM candles WHERE pair=? AND interval=? AND ts>=? AND ts<=? ORDER BY ts ASC", (market, interval, int(start_ts), int(end_ts))).fetchall()
    finally:
        conn.close()


def _read_all_artifact_rows(db_path: str | Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(f"file:{Path(db_path).expanduser().resolve()}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT pair, interval, ts, open, high, low, close, volume FROM candles ORDER BY pair, interval, ts").fetchall()
    finally:
        conn.close()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def cmd_research_freeze_dataset(*, db_path: str, market: str, interval: str, start: str, end: str,
                                out_path: str, provenance_manifest_path: str) -> int:
    from .experiment_manifest import DateRange
    date_range = DateRange(start=start, end=end)
    source_provenance = load_dataset_source_provenance(provenance_manifest_path)
    print(json.dumps(freeze_sqlite_candles_dataset(source_db=db_path, market=market, interval=interval,
        start_ts=date_range.start_ts_ms(), end_ts=date_range.end_ts_ms(), out_dir=out_path,
        source_provenance=source_provenance), sort_keys=True, ensure_ascii=False))
    return 0
