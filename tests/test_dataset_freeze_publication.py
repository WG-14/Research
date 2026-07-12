from __future__ import annotations
import json
import multiprocessing
import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from .test_dataset_artifact_manifest_contract import _source
from market_research.research.dataset_freeze import DatasetFreezeError, freeze_sqlite_candles_dataset
from market_research.research.datasets.artifact_manifest import ArtifactManifestError, build_artifact_manifest, load_artifact_manifest


def _publish_process(
    source: str,
    out_dir: str,
    barrier,
    queue,
    *,
    end_ts: int = 2,
    force_collision: bool = False,
    wait_for_winner=None,
    winner_published=None,
    tamper_winner: bool = False,
    role: str = "publisher",
) -> None:
    """Independent-process publisher used to exercise the filesystem race path."""
    import market_research.research.dataset_freeze as freezer

    original_replace = freezer.os.replace
    original_hash = freezer.artifact_content_hash
    if force_collision:
        freezer.artifact_content_hash = lambda *args, **kwargs: "sha256:" + "f" * 64
    if wait_for_winner is not None:
        wait_for_winner.wait(10)
        if tamper_winner:
            for path in Path(out_dir).rglob("candles.sqlite"):
                with sqlite3.connect(path) as db:
                    db.execute("UPDATE candles SET close=999 WHERE ts=1")
    else:
        def synchronized_replace(source_path, destination_path):
            barrier.wait(10)
            return original_replace(source_path, destination_path)
        freezer.os.replace = synchronized_replace
    try:
        result = freezer.freeze_sqlite_candles_dataset(
            source_db=source, market="KRW-BTC", interval="1m", start_ts=1,
            end_ts=end_ts, out_dir=out_dir,
        )
        queue.put({"status": "ok", "role": role, "result": result})
        if winner_published is not None:
            winner_published.set()
    except Exception as exc:  # Process boundary serializes the explicit failure for the parent assertion.
        cause = exc.__cause__
        queue.put({
            "status": "error",
            "role": role,
            "exception_type": type(exc).__name__,
            "reason": str(exc),
            "cause_type": type(cause).__name__ if cause is not None else None,
            "cause_reason": str(cause) if cause is not None else None,
        })
    finally:
        freezer.os.replace = original_replace
        freezer.artifact_content_hash = original_hash


def test_freeze_is_idempotent_for_identical_input(tmp_path) -> None:
    source = _source(tmp_path)
    first = freeze_sqlite_candles_dataset(source_db=source, market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    second = freeze_sqlite_candles_dataset(source_db=source, market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    assert first["artifact_id"] == second["artifact_id"]
    assert second["reused_existing"] is True


@pytest.mark.parametrize("stage", ("during_db_write", "during_db_creation", "during_manifest_creation", "after_verification_before_rename", "during_final_publication"))
def test_interrupted_publication_never_exposes_bundle(tmp_path, stage: str) -> None:
    with pytest.raises(DatasetFreezeError, match="injected"):
        freeze_sqlite_candles_dataset(source_db=_source(tmp_path), market="KRW-BTC", interval="1m", start_ts=1, end_ts=2,
                                      out_dir=tmp_path / "out", failure_stage=stage)
    assert not list((tmp_path / "out").rglob("artifact.manifest.json"))


def test_manifest_only_bundle_is_not_resolved(tmp_path) -> None:
    source = _source(tmp_path)
    frozen = freeze_sqlite_candles_dataset(source_db=source, market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    Path(frozen["artifact_path"]).unlink()
    with pytest.raises(DatasetFreezeError, match="incomplete"):
        freeze_sqlite_candles_dataset(source_db=source, market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")


def test_db_only_bundle_is_not_resolved(tmp_path) -> None:
    source = _source(tmp_path)
    frozen = freeze_sqlite_candles_dataset(source_db=source, market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    Path(frozen["manifest_path"]).unlink()
    with pytest.raises(DatasetFreezeError, match="incomplete"):
        freeze_sqlite_candles_dataset(source_db=source, market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")


def test_orphan_staging_bundle_is_not_resolved(tmp_path) -> None:
    frozen = freeze_sqlite_candles_dataset(source_db=_source(tmp_path), market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    staging = Path(frozen["manifest_path"]).parent.parent / ".orphan.staging-race"
    staging.mkdir()
    shutil.copy2(frozen["artifact_path"], staging / "candles.sqlite")
    payload = json.loads(Path(frozen["manifest_path"]).read_text())
    payload["artifact"]["uri"] = str((staging / "candles.sqlite").resolve())
    payload["locator"]["path"] = str((staging / "candles.sqlite").resolve())
    from market_research.research.datasets.hashing_contract import artifact_manifest_hash
    payload["artifact_manifest_hash"] = artifact_manifest_hash({key: value for key, value in payload.items() if key != "artifact_manifest_hash"})
    (staging / "artifact.manifest.json").write_text(json.dumps(payload))
    with pytest.raises(ArtifactManifestError, match="published_bundle"):
        load_artifact_manifest(staging / "artifact.manifest.json")


def test_existing_schema_conflict_is_rejected(tmp_path) -> None:
    source = _source(tmp_path)
    frozen = freeze_sqlite_candles_dataset(source_db=source, market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    with sqlite3.connect(frozen["artifact_path"]) as db:
        db.execute("CREATE INDEX incompatible_schema ON candles(ts)")
    with pytest.raises(DatasetFreezeError, match="schema_conflict"):
        freeze_sqlite_candles_dataset(source_db=source, market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")


def test_existing_scope_conflict_is_rejected(tmp_path) -> None:
    source = _source(tmp_path)
    frozen = freeze_sqlite_candles_dataset(source_db=source, market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    manifest = load_artifact_manifest(frozen["manifest_path"])
    altered = build_artifact_manifest(
        artifact_id=manifest.artifact_id, path=frozen["artifact_path"], content_hash=manifest.content_hash,
        schema_hash=manifest.schema_hash, row_count=manifest.row_count, market=manifest.market,
        interval=manifest.interval, start_ts=1, end_ts=1, coverage_start_ts=1, coverage_end_ts=60_000,
    )
    Path(frozen["manifest_path"]).write_text(json.dumps(altered.as_dict()))
    with pytest.raises(DatasetFreezeError, match="scope_verification_failed"):
        freeze_sqlite_candles_dataset(source_db=source, market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")


def test_concurrent_identical_publication_reuses_verified_bundle(tmp_path) -> None:
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    source = _source(tmp_path)
    processes = [context.Process(target=_publish_process, args=(str(source), str(tmp_path / "out"), barrier, queue)) for _ in range(2)]
    for process in processes: process.start()
    results = [queue.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    assert all(result["status"] == "ok" for result in results)
    published = [result["result"] for result in results]
    assert {result["artifact_id"] for result in published}.__len__() == 1
    assert sum(result["reused_existing"] for result in published) == 1


def test_concurrent_conflicting_publication_fails(tmp_path) -> None:
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    source = _source(tmp_path)
    processes = [
        context.Process(target=_publish_process, args=(str(source), str(tmp_path / "out"), barrier, queue), kwargs={"end_ts": end, "force_collision": True})
        for end in (2, 1)
    ]
    for process in processes: process.start()
    results = [queue.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    assert any(
        result["status"] == "error"
        and result["reason"] in {"artifact_scope_verification_failed", "existing_artifact_invalid_or_tampered"}
        for result in results
    )


def test_existing_content_hash_conflict_preserves_public_error_contract(tmp_path) -> None:
    source = _source(tmp_path)
    frozen = freeze_sqlite_candles_dataset(
        source_db=source, market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out"
    )
    with sqlite3.connect(frozen["artifact_path"]) as db:
        db.execute("UPDATE candles SET close=999 WHERE ts=1")

    with pytest.raises(DatasetFreezeError) as raised:
        freeze_sqlite_candles_dataset(
            source_db=source, market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out"
        )

    assert str(raised.value) == "existing_artifact_invalid_or_tampered"
    assert isinstance(raised.value.__cause__, DatasetFreezeError)
    assert str(raised.value.__cause__) == "artifact_content_hash_verification_failed"


def test_concurrent_tampered_winner_is_not_reused(tmp_path) -> None:
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(1)
    queue = context.Queue()
    published = context.Event()
    source = _source(tmp_path)
    winner = context.Process(target=_publish_process, args=(str(source), str(tmp_path / "out"), barrier, queue), kwargs={"winner_published": published, "role": "winner"})
    loser = context.Process(target=_publish_process, args=(str(source), str(tmp_path / "out"), barrier, queue), kwargs={"wait_for_winner": published, "tamper_winner": True, "role": "reuser"})
    winner.start(); loser.start()
    results = [queue.get(timeout=20) for _ in range(2)]
    for process in (winner, loser):
        process.join(20)
        assert process.exitcode == 0
    successes = [result for result in results if result["status"] == "ok"]
    failures = [result for result in results if result["status"] == "error"]
    assert len(successes) == 1, results
    assert len(failures) == 1, results
    winner_result = successes[0]
    reuser_failure = failures[0]
    assert winner_result["role"] == "winner"
    assert winner_result["result"]["reused_existing"] is False
    assert reuser_failure["role"] == "reuser"
    assert reuser_failure["exception_type"] == "DatasetFreezeError"
    assert reuser_failure["reason"] == "existing_artifact_invalid_or_tampered"
    assert reuser_failure["cause_type"] == "DatasetFreezeError"
    assert reuser_failure["cause_reason"] == "artifact_content_hash_verification_failed"
    assert not any(
        result["status"] == "ok" and result["result"]["reused_existing"] is True
        for result in results
    )
