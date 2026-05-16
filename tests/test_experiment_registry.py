from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

from bithumb_bot.paths import PathManager
from bithumb_bot.research.experiment_registry import (
    EXPERIMENT_REGISTRY_EVIDENCE_HASH_PHASE,
    append_attempt_aborted,
    append_attempt_completion,
    append_research_attempt_rejected,
    compute_research_attempt_counters,
    compute_row_hash,
    experiment_registry_path,
    load_experiment_registry_rows,
    reserve_research_attempt,
    research_identity_from_manifest,
    validate_experiment_registry_binding,
)
from bithumb_bot.research.hashing import sha256_prefixed


def _manager(tmp_path: Path, monkeypatch) -> PathManager:
    monkeypatch.setenv("MODE", "paper")
    for key in ("ENV_ROOT", "RUN_ROOT", "DATA_ROOT", "LOG_ROOT", "BACKUP_ROOT", "ARCHIVE_ROOT"):
        monkeypatch.setenv(key, str(tmp_path / f"{key.lower()}_root"))
    return PathManager.from_env(Path.cwd())


def _payload(**overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "run_id": "exp_001",
        "experiment_family_id": "family_a",
        "hypothesis_id": "hypothesis_a",
        "hypothesis_status": "pre_registered",
        "experiment_id": "exp_001",
        "manifest_hash": "sha256:manifest",
        "manifest_metadata_hash": "sha256:metadata",
        "dataset_snapshot_id": "snap_001",
        "dataset_content_hash": "sha256:dataset",
        "dataset_quality_hash": "sha256:quality",
        "train_split_hash": "sha256:train",
        "validation_split_hash": "sha256:validation",
        "final_holdout_split_hash": "sha256:holdout",
        "final_holdout_fingerprint": "sha256:holdout-fingerprint",
        "final_holdout_identity_hash": "sha256:holdout-identity",
        "final_holdout_content_hash": "sha256:holdout-content",
        "final_holdout_reuse_key_hash": "sha256:holdout-identity",
        "parameter_space_hash": "sha256:space",
        "parameter_grid_size": 3,
        "candidate_count": None,
        "declared_attempt_index": None,
        "declared_holdout_reuse_count": None,
        "statistical_evidence_hash": None,
        "return_panel_hash": None,
        "promotion_artifact_hash": None,
        "promoted_candidate_id": None,
        "repository_version": "test",
        "command_args_hash": "sha256:args",
    }
    payload.update(overrides)
    return payload


def _report_from_reservation(reservation: dict[str, object], *, complete: dict[str, object] | None = None) -> dict[str, object]:
    row = reservation["row"]
    assert isinstance(row, dict)
    report = {
        **row,
        "experiment_registry_path": reservation["path"],
        "experiment_registry_prior_hash": reservation["prior_hash"],
        "experiment_registry_row_hash": reservation["row_hash"],
        "experiment_registry_completion_row_hash": complete.get("row_hash") if complete else None,
        "statistical_validation_contract": {
            "gates": {
                "max_holdout_reuse_count": 0,
                "max_attempt_index_without_new_hypothesis": 1,
            }
        },
    }
    return report


def test_first_and_second_holdout_use_compute_reuse_count(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    first = reserve_research_attempt(manager=manager, base_payload=_payload())
    second = reserve_research_attempt(manager=manager, base_payload=_payload(experiment_id="exp_002", run_id="exp_002"))

    assert first["computed_holdout_reuse_count"] == 0
    assert second["computed_holdout_reuse_count"] == 1
    assert second["computed_attempt_index"] == 2


def test_same_holdout_date_range_counts_reuse_even_if_split_content_hash_changes(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    first = reserve_research_attempt(manager=manager, base_payload=_payload(final_holdout_content_hash="sha256:content-a"))
    second = reserve_research_attempt(
        manager=manager,
        base_payload=_payload(
            experiment_id="exp_002",
            run_id="exp_002",
            final_holdout_split_hash="sha256:different-split",
            final_holdout_content_hash="sha256:content-b",
        ),
    )

    assert first["computed_holdout_reuse_count"] == 0
    assert second["computed_holdout_reuse_count"] == 1


def test_same_holdout_date_range_counts_reuse_even_if_dataset_snapshot_id_changes(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    reserve_research_attempt(manager=manager, base_payload=_payload(dataset_snapshot_id="snap_a"))
    second = reserve_research_attempt(
        manager=manager,
        base_payload=_payload(
            experiment_id="exp_002",
            run_id="exp_002",
            dataset_snapshot_id="snap_b",
            final_holdout_content_hash="sha256:content-b",
        ),
    )

    assert second["computed_holdout_reuse_count"] == 1


def test_different_holdout_date_range_does_not_count_as_reuse(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    reserve_research_attempt(manager=manager, base_payload=_payload())
    second = reserve_research_attempt(
        manager=manager,
        base_payload=_payload(
            experiment_id="exp_002",
            run_id="exp_002",
            final_holdout_fingerprint="sha256:other-identity",
            final_holdout_identity_hash="sha256:other-identity",
            final_holdout_reuse_key_hash="sha256:other-identity",
        ),
    )

    assert second["computed_holdout_reuse_count"] == 0


def test_budget_and_declared_counter_mismatches_are_stable_reasons(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    reserve_research_attempt(manager=manager, base_payload=_payload())
    second = reserve_research_attempt(
        manager=manager,
        base_payload=_payload(
            experiment_id="exp_002",
            run_id="exp_002",
            declared_attempt_index=1,
            declared_holdout_reuse_count=0,
        ),
    )
    report = _report_from_reservation(second)

    reasons = validate_experiment_registry_binding(report=report)

    assert "experiment_registry_budget_exceeded" in reasons
    assert "declared_attempt_index_mismatch" in reasons
    assert "declared_holdout_reuse_count_mismatch" in reasons


def test_registry_row_hash_tampering_is_detected(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    reservation = reserve_research_attempt(manager=manager, base_payload=_payload())
    path = experiment_registry_path(manager=manager)
    rows = load_experiment_registry_rows(path)
    rows[0]["manifest_hash"] = "sha256:tampered"
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")

    reasons = validate_experiment_registry_binding(report=_report_from_reservation(reservation))

    assert "experiment_registry_row_hash_mismatch" in reasons


def test_registry_prior_hash_mismatch_is_detected(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    first = reserve_research_attempt(manager=manager, base_payload=_payload())
    second = reserve_research_attempt(manager=manager, base_payload=_payload(experiment_id="exp_002", run_id="exp_002"))
    path = experiment_registry_path(manager=manager)
    rows = load_experiment_registry_rows(path)
    rows[1]["prior_registry_hash"] = sha256_prefixed({"tampered": True})
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")

    reasons = validate_experiment_registry_binding(report=_report_from_reservation(second))

    assert first["computed_attempt_index"] == 1
    assert "experiment_registry_prior_hash_mismatch" in reasons


def test_final_holdout_fingerprint_mismatch_is_detected(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    reservation = reserve_research_attempt(manager=manager, base_payload=_payload())
    report = _report_from_reservation(reservation)
    report["final_holdout_fingerprint"] = "sha256:different"

    reasons = validate_experiment_registry_binding(report=report)

    assert "experiment_registry_final_holdout_fingerprint_mismatch" in reasons


def test_final_holdout_identity_and_content_mismatches_are_detected(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    reservation = reserve_research_attempt(manager=manager, base_payload=_payload())
    report = _report_from_reservation(reservation)
    report["final_holdout_identity_hash"] = "sha256:different-identity"
    report["final_holdout_content_hash"] = "sha256:different-content"
    report["final_holdout_reuse_key_hash"] = "sha256:different-identity"

    reasons = validate_experiment_registry_binding(report=report)

    assert "experiment_registry_final_holdout_identity_mismatch" in reasons
    assert "experiment_registry_final_holdout_content_mismatch" in reasons
    assert "experiment_registry_final_holdout_reuse_key_mismatch" in reasons


def test_incomplete_reservation_is_counted_and_blocks_complete_required_validation(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    first = reserve_research_attempt(manager=manager, base_payload=_payload())
    second = reserve_research_attempt(manager=manager, base_payload=_payload(experiment_id="exp_002", run_id="exp_002"))

    assert second["computed_attempt_index"] == 2
    assert "experiment_registry_incomplete_attempt" in validate_experiment_registry_binding(
        report=_report_from_reservation(first),
        require_complete=True,
    )


def test_completion_row_satisfies_complete_required_validation(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    reservation = reserve_research_attempt(manager=manager, base_payload=_payload())
    complete = append_attempt_completion(
        manager=manager,
        reservation=reservation,
        updates={
            "candidate_count": 3,
            "return_panel_hash": "sha256:return",
            "statistical_evidence_hash": "sha256:evidence",
            "statistical_evidence_hash_phase": EXPERIMENT_REGISTRY_EVIDENCE_HASH_PHASE,
        },
    )
    evidence = {
        **_report_from_reservation(reservation, complete=complete),
        "experiment_registry_bound_evidence_hash": "sha256:evidence",
        "experiment_registry_evidence_hash_phase": EXPERIMENT_REGISTRY_EVIDENCE_HASH_PHASE,
    }

    assert validate_experiment_registry_binding(
        report=_report_from_reservation(reservation, complete=complete),
        evidence=evidence,
        require_complete=True,
    ) == []


def test_completion_row_statistical_evidence_hash_mismatch_is_detected(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    reservation = reserve_research_attempt(manager=manager, base_payload=_payload())
    complete = append_attempt_completion(
        manager=manager,
        reservation=reservation,
        updates={
            "candidate_count": 3,
            "return_panel_hash": "sha256:return",
            "statistical_evidence_hash": "sha256:pre-final",
            "statistical_evidence_hash_phase": EXPERIMENT_REGISTRY_EVIDENCE_HASH_PHASE,
        },
    )
    evidence = {
        **_report_from_reservation(reservation, complete=complete),
        "experiment_registry_bound_evidence_hash": "sha256:other",
        "experiment_registry_evidence_hash_phase": EXPERIMENT_REGISTRY_EVIDENCE_HASH_PHASE,
    }

    reasons = validate_experiment_registry_binding(
        report=_report_from_reservation(reservation, complete=complete),
        evidence=evidence,
        require_complete=True,
    )

    assert "experiment_registry_statistical_evidence_hash_mismatch" in reasons


def test_completion_row_requires_explicit_evidence_hash_phase(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    reservation = reserve_research_attempt(manager=manager, base_payload=_payload())
    complete = append_attempt_completion(
        manager=manager,
        reservation=reservation,
        updates={"candidate_count": 3, "return_panel_hash": "sha256:return", "statistical_evidence_hash": "sha256:evidence"},
    )
    evidence = {
        **_report_from_reservation(reservation, complete=complete),
        "experiment_registry_bound_evidence_hash": "sha256:evidence",
    }

    reasons = validate_experiment_registry_binding(
        report=_report_from_reservation(reservation, complete=complete),
        evidence=evidence,
        require_complete=True,
    )

    assert "experiment_registry_evidence_hash_phase_mismatch" in reasons


def test_reproduce_fails_when_completion_row_points_to_pre_final_evidence_only_without_bound_hash(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    reservation = reserve_research_attempt(manager=manager, base_payload=_payload())
    complete = append_attempt_completion(
        manager=manager,
        reservation=reservation,
        updates={
            "candidate_count": 3,
            "return_panel_hash": "sha256:return",
            "statistical_evidence_hash": "sha256:pre-final",
            "statistical_evidence_hash_phase": EXPERIMENT_REGISTRY_EVIDENCE_HASH_PHASE,
        },
    )

    reasons = validate_experiment_registry_binding(
        report=_report_from_reservation(reservation, complete=complete),
        evidence=_report_from_reservation(reservation, complete=complete),
        require_complete=True,
    )

    assert "experiment_registry_bound_evidence_hash_missing" in reasons


def test_aborted_attempt_is_not_promotion_permitted(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    reservation = reserve_research_attempt(manager=manager, base_payload=_payload())
    append_attempt_aborted(manager=manager, reservation_row_hash=str(reservation["row_hash"]), reason="operator_interrupt")

    reasons = validate_experiment_registry_binding(
        report=_report_from_reservation(reservation),
        require_complete=True,
    )

    assert "experiment_registry_incomplete_attempt" in reasons


def test_declared_counter_mismatch_does_not_create_counted_reservation(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    reserve_research_attempt(manager=manager, base_payload=_payload())
    payload = _payload(experiment_id="exp_002", run_id="exp_002")
    counters = compute_research_attempt_counters(manager=manager, base_payload=payload)
    append_research_attempt_rejected(
        manager=manager,
        base_payload={**payload, "declared_attempt_index": 1, "declared_holdout_reuse_count": 0},
        reasons=["declared_attempt_index_mismatch", "declared_holdout_reuse_count_mismatch"],
        computed_attempt_index=counters["computed_attempt_index"],
        computed_holdout_reuse_count=counters["computed_holdout_reuse_count"],
    )

    next_counters = compute_research_attempt_counters(manager=manager, base_payload=payload)
    rows = load_experiment_registry_rows(experiment_registry_path(manager=manager))

    assert next_counters == counters
    assert rows[-1]["event_type"] == "research_attempt_rejected"
    assert rows[-1]["counted_attempt"] is False


def test_research_identity_from_manifest_records_fallback_source() -> None:
    manifest = SimpleNamespace(
        experiment_id="exp_001",
        hypothesis="sma_filter_hypothesis",
        raw={},
    )

    identity = research_identity_from_manifest(manifest)

    assert identity["experiment_family_id"] == "exp_001"
    assert identity["hypothesis_id"] == "sma_filter_hypothesis"
    assert identity["hypothesis_identity_source"] == "manifest.hypothesis"
    assert identity["experiment_family_identity_source"] == "experiment_id"


def test_concurrent_reservations_do_not_duplicate_attempt_index(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    results: list[dict[str, object]] = []
    lock = threading.Lock()

    def reserve(index: int) -> None:
        result = reserve_research_attempt(
            manager=manager,
            base_payload=_payload(experiment_id=f"exp_{index}", run_id=f"exp_{index}"),
        )
        with lock:
            results.append(result)

    threads = [threading.Thread(target=reserve, args=(index,)) for index in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    indexes = sorted(int(result["computed_attempt_index"]) for result in results)
    assert indexes == [1, 2]
