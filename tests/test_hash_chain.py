import builtins
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from threading import Event

import pytest

import market_research.research.hash_chain as hash_chain_module
from market_research.paths import ResearchPathManager
from market_research.research.artifact_store import ResearchArtifactContext
from market_research.research.hash_chain import (
    append_hash_chained_jsonl,
    append_hash_chained_jsonl_idempotent,
    mutate_hash_chained_jsonl_atomic,
    read_hash_chained_jsonl_snapshot,
    validate_hash_chained_jsonl,
    verify_hash_chained_jsonl_event,
)
from market_research.settings import ResearchSettings


def _store(tmp_path: Path) -> tuple[ResearchArtifactContext, Path]:
    settings = ResearchSettings(
        data_root=tmp_path / "data",
        artifact_root=tmp_path / "artifacts",
        report_root=tmp_path / "reports",
        cache_root=tmp_path / "cache",
        db_path=tmp_path / "input.sqlite",
        max_workers=1,
        random_seed=0,
    )
    manager = ResearchPathManager.from_settings(settings, project_root=Path.cwd())
    path = manager.report_path("research", "exp", "candidate_events.jsonl")
    return ResearchArtifactContext(manager=manager, experiment_id="exp"), path


def test_hash_chain_detects_candidate_event_mutation_and_reordering(tmp_path: Path) -> None:
    store, path = _store(tmp_path)
    first = append_hash_chained_jsonl(
        store=store, path=path, payload={"status": "STARTED"}, label="candidate_event"
    )
    second = append_hash_chained_jsonl(
        store=store, path=path, payload={"status": "COMPLETED"}, label="candidate_event"
    )

    assert second["prior_hash"] == first["row_hash"]
    assert validate_hash_chained_jsonl(path=path, label="candidate_event")["status"] == "PASS"

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["status"] = "COMPLETED"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    result = validate_hash_chained_jsonl(path=path, label="candidate_event")
    assert result["status"] == "FAIL"
    assert "row_hash_mismatch:0" in result["reasons"]


def test_idempotent_hash_chain_returns_existing_row_and_rejects_conflict(
    tmp_path: Path,
) -> None:
    store, path = _store(tmp_path)
    payload = {"event_id": "event-1", "status": "COMPLETED"}

    first = append_hash_chained_jsonl_idempotent(
        store=store,
        path=path,
        payload=payload,
        label="candidate_event",
    )
    repeated = append_hash_chained_jsonl_idempotent(
        store=store,
        path=path,
        payload=payload,
        label="candidate_event",
    )

    assert repeated == first
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    with pytest.raises(ValueError, match="hash_chain_event_id_conflict"):
        append_hash_chained_jsonl_idempotent(
            store=store,
            path=path,
            payload={"event_id": "event-1", "status": "FAILED"},
            label="candidate_event",
        )
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_idempotent_hash_chain_serializes_concurrent_duplicate_delivery(
    tmp_path: Path,
) -> None:
    store, path = _store(tmp_path)
    payload = {"event_id": "event-concurrent", "status": "COMPLETED"}

    def append_once(_index: int) -> dict[str, object]:
        return append_hash_chained_jsonl_idempotent(
            store=store,
            path=path,
            payload=payload,
            label="candidate_event",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        rows = list(executor.map(append_once, range(16)))

    assert len({str(row["row_hash"]) for row in rows}) == 1
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    assert validate_hash_chained_jsonl(path=path, label="candidate_event")[
        "status"
    ] == "PASS"


def test_hash_chain_rejects_unterminated_final_record_before_adoption_or_append(
    tmp_path: Path,
) -> None:
    store, path = _store(tmp_path)
    payload = {"event_id": "event-unterminated", "status": "COMPLETED"}
    append_hash_chained_jsonl_idempotent(
        store=store,
        path=path,
        payload=payload,
        label="candidate_event",
    )
    path.write_bytes(path.read_bytes().removesuffix(b"\n"))
    interrupted = path.read_bytes()

    with pytest.raises(ValueError, match="hash_chain_unterminated_final_line"):
        validate_hash_chained_jsonl(path=path, label="candidate_event")
    with pytest.raises(ValueError, match="hash_chain_unterminated_final_line"):
        append_hash_chained_jsonl_idempotent(
            store=store,
            path=path,
            payload=payload,
            label="candidate_event",
        )
    with pytest.raises(ValueError, match="hash_chain_unterminated_final_line"):
        append_hash_chained_jsonl_idempotent(
            store=store,
            path=path,
            payload={"event_id": "event-next", "status": "COMPLETED"},
            label="candidate_event",
        )
    assert path.read_bytes() == interrupted


def test_event_verification_uses_one_exact_validated_stream_snapshot(
    tmp_path: Path,
) -> None:
    store, path = _store(tmp_path)
    payload = {"event_id": "event-verified", "status": "COMPLETED"}
    expected = append_hash_chained_jsonl_idempotent(
        store=store,
        path=path,
        payload=payload,
        label="candidate_event",
    )

    verified = verify_hash_chained_jsonl_event(
        path=path,
        label="candidate_event",
        event_id="event-verified",
        expected_payload=payload,
    )
    assert verified == expected

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["status"] = "TAMPERED"
    path.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash_chain_invalid:row_hash_mismatch:0"):
        verify_hash_chained_jsonl_event(
            path=path,
            label="candidate_event",
            event_id="event-verified",
            expected_payload=payload,
        )


def test_validation_snapshot_waits_for_atomic_publish_and_returns_one_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store_context, path = _store(tmp_path)
    publish_entered = Event()
    release_publish = Event()
    snapshot_started = Event()
    original_publish = hash_chain_module.write_jsonl_atomic

    def blocked_publish(target, rows):
        publish_entered.set()
        assert release_publish.wait(timeout=5)
        original_publish(target, rows)

    monkeypatch.setattr(
        "market_research.research.hash_chain.write_jsonl_atomic",
        blocked_publish,
    )

    def publish() -> None:
        mutate_hash_chained_jsonl_atomic(
            path=path,
            label="candidate_event",
            mutation=lambda _snapshot, stage: stage({"status": "COMPLETED"}),
        )

    def read_snapshot():
        snapshot_started.set()
        return read_hash_chained_jsonl_snapshot(
            path=path,
            label="candidate_event",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        publish_future = executor.submit(publish)
        assert publish_entered.wait(timeout=5)
        snapshot_future = executor.submit(read_snapshot)
        assert snapshot_started.wait(timeout=5)
        try:
            with pytest.raises(FutureTimeoutError):
                snapshot_future.result(timeout=0.05)
        finally:
            release_publish.set()
        publish_future.result(timeout=5)
        snapshot = snapshot_future.result(timeout=5)

    assert snapshot.status == "PASS"
    assert snapshot.reasons == ()
    assert snapshot.row_count == 1
    assert len(snapshot.rows) == 1
    assert snapshot.rows[0]["status"] == "COMPLETED"
    assert snapshot.stream_hash == snapshot.rows[0]["row_hash"]
    assert snapshot.as_validation() == validate_hash_chained_jsonl(
        path=path,
        label="candidate_event",
    )


def test_atomic_mutation_stages_hash_bound_rows_as_one_publish(tmp_path: Path) -> None:
    _store_context, path = _store(tmp_path)

    def mutation(snapshot, stage):
        assert snapshot.rows == ()
        review = stage({"event_type": "review", "value": 1})
        transition = stage(
            {"event_type": "transition", "review_hash": review["row_hash"]}
        )
        assert not path.exists()
        return transition["row_hash"]

    result = mutate_hash_chained_jsonl_atomic(
        path=path,
        label="candidate_event",
        mutation=mutation,
    )

    assert result.value == result.appended_rows[1]["row_hash"]
    assert (
        result.appended_rows[1]["review_hash"]
        == result.appended_rows[0]["row_hash"]
    )
    assert validate_hash_chained_jsonl(path=path, label="candidate_event")[
        "status"
    ] == "PASS"


def test_atomic_mutation_publish_failure_preserves_prior_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, path = _store(tmp_path)
    append_hash_chained_jsonl(
        store=store,
        path=path,
        payload={"status": "STARTED"},
        label="candidate_event",
    )
    prior = path.read_bytes()

    def fail_publish(*_args, **_kwargs):
        raise OSError("injected")

    monkeypatch.setattr(
        "market_research.research.hash_chain.write_jsonl_atomic",
        fail_publish,
    )
    with pytest.raises(OSError, match="injected"):
        mutate_hash_chained_jsonl_atomic(
            path=path,
            label="candidate_event",
            mutation=lambda _snapshot, stage: stage({"status": "COMPLETED"}),
        )

    assert path.read_bytes() == prior
    assert validate_hash_chained_jsonl(path=path, label="candidate_event")[
        "status"
    ] == "PASS"


def test_hash_chain_lock_unavailable_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, path = _store(tmp_path)
    real_import = builtins.__import__

    def import_without_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("injected")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_fcntl)
    with pytest.raises(RuntimeError, match="hash_chain_process_lock_unavailable"):
        append_hash_chained_jsonl(
            store=store,
            path=path,
            payload={"status": "STARTED"},
            label="candidate_event",
        )
