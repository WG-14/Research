from __future__ import annotations

import copy
import json
import shutil
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

import market_research.application.holdout_authority as holdout_boundary
from market_research.application.contracts import (
    ActorContext,
    ResearchValidationRequest,
)
from market_research.application.sandbox_job import (
    SandboxJobContractError,
    _validated_request,
)
from market_research.paths import ResearchPathManager
from market_research.research.experiment_registry import (
    FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION,
    FinalHoldoutAccessPurpose,
    abort_final_holdout_reservation,
    activate_final_holdout_reservation,
    append_attempt_completion,
    consume_final_holdout_read_capability,
    final_holdout_authority_scope_hash,
    load_experiment_registry_rows,
    publish_pre_holdout_gate_artifact,
    reserve_final_holdout_authority,
    reserve_independent_reproduction_holdout_authority,
    reserve_research_attempt,
    reserve_research_attempt_checked,
    validate_final_holdout_authority_registry,
    validate_final_holdout_reservation_transport,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.principal_assertion import (
    IndependentVerificationAssertionScope,
)
from market_research.research.split_usage_policy import split_exposure_rows
from market_research.settings import ResearchSettings
from market_research.storage_io import (
    ATOMIC_PUBLICATION_MODE_ENV,
    write_json_atomic_create_or_verify,
)
from tests.independent_verification_fixture import (
    _fixture_selection_artifact,
    provision_test_principal_assertion,
    seed_reproduction_receipts,
)


@dataclass(frozen=True)
class _Range:
    start: str = "2026-04-01"
    end: str = "2026-04-30"

    def as_dict(self) -> dict[str, str]:
        return {"start": self.start, "end": self.end}


@dataclass(frozen=True)
class _Split:
    final_holdout: _Range | None = field(default_factory=_Range)


@dataclass(frozen=True)
class _Dataset:
    split: _Split
    source: str = "frozen_sqlite_candles"
    snapshot_id: str = "shared-authority-snapshot"
    source_content_hash: str = "sha256:" + "1" * 64
    source_schema_hash: str = "sha256:" + "2" * 64
    artifact_ref: object | None = None
    top_of_book: object | None = None
    depth: object | None = None


@dataclass(frozen=True)
class _StatisticalValidation:
    primary_metric: str = "return"

    def as_dict(self) -> dict[str, Any]:
        # The durable scope fence must remain single-use even when the broader
        # research-freedom contract permits a larger reuse count.
        return {
            "primary_metric": self.primary_metric,
            "gates": {
                "max_attempt_index_without_new_hypothesis": 99,
                "max_holdout_reuse_count": 99,
            },
        }


@dataclass(frozen=True)
class _Manifest:
    dataset: _Dataset = field(default_factory=lambda: _Dataset(split=_Split()))
    experiment_id: str = "shared-final-holdout-authority"
    hypothesis: str = "one immutable final holdout exposure"
    strategy_name: str = "noop_baseline"
    research_classification: str = "research_only"
    market: str = "KRW-BTC"
    interval: str = "1m"
    statistical_validation: _StatisticalValidation = field(
        default_factory=_StatisticalValidation
    )
    hypothesis_spec: object | None = None
    raw: dict[str, Any] = field(default_factory=lambda: {"objective_metric": "return"})

    def manifest_hash(self) -> str:
        holdout = self.dataset.split.final_holdout
        return sha256_prefixed(
            {
                "experiment_id": self.experiment_id,
                "dataset_snapshot_id": self.dataset.snapshot_id,
                "final_holdout": holdout.as_dict() if holdout is not None else None,
            }
        )


def _manager(*, root: Path, registry_path: Path) -> ResearchPathManager:
    settings = ResearchSettings(
        data_root=root / "data",
        artifact_root=root / "artifacts",
        report_root=root / "reports",
        cache_root=root / "cache",
        db_path=None,
        max_workers=1,
        random_seed=0,
        final_holdout_registry_path=registry_path,
    )
    return ResearchPathManager.from_settings(
        settings,
        project_root=Path(__file__).resolve().parents[1],
    )


def _reserve(
    *,
    manager: ResearchPathManager,
    manifest: _Manifest,
    request_id: str,
) -> dict[str, Any]:
    result = reserve_final_holdout_authority(
        manager=manager,
        manifest=manifest,
        request_id=request_id,
        request_hash=sha256_prefixed({"request_id": request_id}),
    )
    return dict(result["transport"])


def _activate(
    *, manager: ResearchPathManager, manifest: _Manifest, reservation: dict[str, Any]
) -> dict[str, Any]:
    selection_hash = sha256_prefixed({"selection": "candidate-a"})
    gate = _publish_gate(
        manager=manager,
        manifest=manifest,
        selection_artifact_hash=selection_hash,
        selected_candidate_id="candidate-a",
        label="primary",
    )
    activation = activate_final_holdout_reservation(
        manager=manager,
        reservation=reservation,
        manifest_hash=manifest.manifest_hash(),
        selection_artifact_hash=selection_hash,
        selected_candidate_id="candidate-a",
        selection_attempt_index=1,
        selection_holdout_reuse_count=0,
        pre_holdout_gate_hash=str(gate["content_hash"]),
    )
    _consume_activation(manager=manager, manifest=manifest, activation=activation)
    return activation


def _publish_gate(
    *,
    manager: ResearchPathManager,
    manifest: _Manifest,
    selection_artifact_hash: str,
    selected_candidate_id: str,
    label: str,
) -> dict[str, Any]:
    return publish_pre_holdout_gate_artifact(
        manager=manager,
        experiment_id=manifest.experiment_id,
        material={
            "schema_version": 1,
            "artifact_type": "pre_holdout_validation_gate",
            "final_holdout_authority_scope_hash": (
                final_holdout_authority_scope_hash(manifest)
            ),
            "manifest_hash": manifest.manifest_hash(),
            "selection_report_hash": sha256_prefixed({"selection_report": label}),
            "selection_artifact_hash": selection_artifact_hash,
            "selected_candidate_id": selected_candidate_id,
            "validation_experiment_bundle_hash": None,
            "native_validation_computation_receipt_hash": None,
            "gate_result": "PASS",
            "gate_reasons": [],
        },
    )


def _consume_activation(
    *,
    manager: ResearchPathManager,
    manifest: _Manifest,
    activation: dict[str, Any],
) -> None:
    holdout = manifest.dataset.split.final_holdout
    assert holdout is not None
    consume_final_holdout_read_capability(
        manager=manager,
        capability=activation["holdout_read_capability"],
        manifest_hash=manifest.manifest_hash(),
        authority_scope_hash=final_holdout_authority_scope_hash(manifest),
        split_name="final_holdout",
        requested_range=holdout.as_dict(),
    )


def _completed_primary_with_trusted_reproduction(
    *, tmp_path: Path
) -> tuple[
    _Manifest,
    ResearchPathManager,
    dict[str, Any],
    dict[str, Any],
    Path,
    object,
]:
    """Publish the exact evidence required to authorize one trusted replay."""

    manifest = _Manifest()
    manager = _manager(
        root=tmp_path / "runtime",
        registry_path=tmp_path / "shared" / "final-holdout.jsonl",
    )
    manifest_hash = manifest.manifest_hash()
    selection_artifact = _fixture_selection_artifact(manifest_hash=manifest_hash)
    reservation = reserve_final_holdout_authority(
        manager=manager,
        manifest=manifest,
        request_id="primary-confirmation",
        request_hash=sha256_prefixed({"request": "primary-confirmation"}),
    )
    primary_gate = _publish_gate(
        manager=manager,
        manifest=manifest,
        selection_artifact_hash=str(selection_artifact["content_hash"]),
        selected_candidate_id="candidate-a",
        label="completed-primary",
    )
    primary_activation = activate_final_holdout_reservation(
        manager=manager,
        reservation=dict(reservation["transport"]),
        manifest_hash=manifest_hash,
        selection_artifact_hash=str(selection_artifact["content_hash"]),
        selected_candidate_id="candidate-a",
        selection_attempt_index=1,
        selection_holdout_reuse_count=0,
        pre_holdout_gate_hash=str(primary_gate["content_hash"]),
    )
    _consume_activation(
        manager=manager,
        manifest=manifest,
        activation=primary_activation,
    )
    final_hashes = {
        "dataset_artifact_evidence_hash": sha256_prefixed(
            {"primary": "dataset-artifact"}
        ),
        "final_holdout_query_hash": sha256_prefixed({"primary": "query"}),
        "final_holdout_data_hash": sha256_prefixed({"primary": "data"}),
        "final_holdout_fingerprint_hash": sha256_prefixed({"primary": "fingerprint"}),
        "final_holdout_quality_hash": sha256_prefixed({"primary": "quality"}),
        "final_holdout_reuse_key_hash": sha256_prefixed({"primary": "reuse-key"}),
        "final_holdout_result_hash": sha256_prefixed({"primary": "result"}),
    }
    completion_updates = {
        **final_hashes,
        "final_holdout_reuse_key_schema_version": (
            FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION
        ),
        "final_holdout_result_hash_schema_version": 1,
        "selection_artifact_hash": selection_artifact["content_hash"],
        "selected_candidate_id": "candidate-a",
        "selection_attempt_index": 1,
        "selection_holdout_reuse_count": 0,
        "candidate_count": 1,
        "confirmation_gate_result": "PASS",
    }
    completion = append_attempt_completion(
        manager=manager,
        reservation=reservation,
        updates=completion_updates,
    )
    confirmation_material = {
        "schema_version": 2,
        "artifact_type": "final_holdout_confirmation",
        "manifest_hash": manifest_hash,
        "selection_artifact_hash": selection_artifact["content_hash"],
        "selected_candidate_id": "candidate-a",
        **final_hashes,
        "final_holdout_reuse_key_schema_version": (
            FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION
        ),
        "final_holdout_result_hash_schema_version": 1,
        "generated_at": completion["row"]["created_at"],
        "experiment_registry_path": reservation["path"],
        "experiment_registry_prior_hash": reservation["prior_hash"],
        "experiment_registry_row_hash": reservation["row_hash"],
        "experiment_registry_completion_row_hash": completion["row_hash"],
        "authorization_row_hash": reservation["row_hash"],
        "completion_row_hash": completion["row_hash"],
    }
    confirmation = {
        **confirmation_material,
        "content_hash": sha256_prefixed(
            confirmation_material,
            label="final_holdout_confirmation",
        ),
    }
    confirmation_path = manager.report_path(
        "research", manifest.experiment_id, "final_holdout_confirmation.json"
    )
    write_json_atomic_create_or_verify(confirmation_path, confirmation)
    baseline, baseline_path, _ = seed_reproduction_receipts(
        manager=manager,
        experiment_id=manifest.experiment_id,
        manifest_hash=manifest_hash,
        source_report_hash=sha256_prefixed({"terminal": "source-report"}),
    )
    trusted_manager, assertion = provision_test_principal_assertion(
        manager=manager,
        scope=IndependentVerificationAssertionScope(
            verification_id="trusted-independent-reproduction",
            verification_version="1",
            experiment_id=manifest.experiment_id,
            research_version=manifest_hash,
            source_report_hash=str(baseline["source_report_hash"]),
            baseline_receipt_hash=str(baseline["receipt_content_hash"]),
        ),
        subject="independent-verifier-a",
        nonce="one-time-independent-reproduction-nonce",
    )
    return (
        manifest,
        trusted_manager,
        reservation,
        completion,
        baseline_path,
        assertion,
    )


def _independent_completion_updates(
    primary_completion: dict[str, Any],
) -> dict[str, Any]:
    row = primary_completion["row"]
    return {
        field: row[field]
        for field in (
            "dataset_artifact_evidence_hash",
            "final_holdout_query_hash",
            "final_holdout_data_hash",
            "final_holdout_fingerprint_hash",
            "final_holdout_quality_hash",
            "final_holdout_reuse_key_hash",
            "final_holdout_reuse_key_schema_version",
            "selection_artifact_hash",
            "selected_candidate_id",
            "selection_attempt_index",
            "selection_holdout_reuse_count",
            "candidate_count",
            "confirmation_gate_result",
            "final_holdout_result_hash_schema_version",
            "final_holdout_result_hash",
        )
    }


def _reserve_independent(
    *,
    manifest: _Manifest,
    manager: ResearchPathManager,
    primary_completion: dict[str, Any],
    baseline_path: Path,
    assertion: object,
    request_id: str = "independent-reproduction-1",
) -> dict[str, Any]:
    return reserve_independent_reproduction_holdout_authority(
        manager=manager,
        manifest=manifest,
        request_id=request_id,
        request_hash=sha256_prefixed({"request_id": request_id}),
        primary_completion_row_hash=str(primary_completion["row_hash"]),
        baseline_receipt_path=baseline_path,
        principal_assertion=assertion,
    )


def _activate_independent(
    *,
    manager: ResearchPathManager,
    manifest: _Manifest,
    reservation: dict[str, Any],
    primary_completion: dict[str, Any],
) -> dict[str, Any]:
    row = primary_completion["row"]
    gate = _publish_gate(
        manager=manager,
        manifest=manifest,
        selection_artifact_hash=str(row["selection_artifact_hash"]),
        selected_candidate_id=str(row["selected_candidate_id"]),
        label="independent-reproduction",
    )
    activation = activate_final_holdout_reservation(
        manager=manager,
        reservation=dict(reservation["transport"]),
        manifest_hash=manifest.manifest_hash(),
        selection_artifact_hash=str(row["selection_artifact_hash"]),
        selected_candidate_id=str(row["selected_candidate_id"]),
        selection_attempt_index=int(row["selection_attempt_index"]),
        selection_holdout_reuse_count=int(row["selection_holdout_reuse_count"]),
        pre_holdout_gate_hash=str(gate["content_hash"]),
    )
    _consume_activation(manager=manager, manifest=manifest, activation=activation)
    return activation


def test_primary_and_signed_independent_reproduction_each_receive_one_exposure(
    tmp_path: Path,
) -> None:
    (
        manifest,
        manager,
        _primary_reservation,
        primary_completion,
        baseline_path,
        assertion,
    ) = _completed_primary_with_trusted_reproduction(tmp_path=tmp_path)

    independent = _reserve_independent(
        manifest=manifest,
        manager=manager,
        primary_completion=primary_completion,
        baseline_path=baseline_path,
        assertion=assertion,
    )
    replay = _reserve_independent(
        manifest=manifest,
        manager=manager,
        primary_completion=primary_completion,
        baseline_path=baseline_path,
        assertion=assertion,
    )
    row = independent["row"]
    assert replay["idempotent_replay"] is True
    assert replay["transport"] == independent["transport"]
    assert (
        row["final_holdout_access_purpose"]
        == FinalHoldoutAccessPurpose.INDEPENDENT_REPRODUCTION.value
    )
    assert row["primary_completion_row_hash"] == primary_completion["row_hash"]
    assert (
        row["primary_final_holdout_result_hash"]
        == primary_completion["row"]["final_holdout_result_hash"]
    )
    assert row["independent_principal_assertion_hash"] == assertion.content_hash
    assert (
        row["independent_principal_assertion_scope_hash"]
        == assertion.scope.content_hash()
    )
    assert row["independent_principal_assertion_subject"] == assertion.subject
    assert row["independent_principal_assertion_nonce"] == assertion.nonce

    _activate_independent(
        manager=manager,
        manifest=manifest,
        reservation=independent,
        primary_completion=primary_completion,
    )
    completion = append_attempt_completion(
        manager=manager,
        reservation=independent,
        updates=_independent_completion_updates(primary_completion),
    )
    assert (
        completion["row"]["final_holdout_result_hash"]
        == primary_completion["row"]["final_holdout_result_hash"]
    )
    assert validate_final_holdout_authority_registry(
        manager=manager,
        require_terminal=True,
    ) == {
        "status": "PASS",
        "row_count": 10,
        "reservation_count": 2,
        "reasons": [],
    }

    manager, second_assertion = provision_test_principal_assertion(
        manager=manager,
        scope=assertion.scope,
        subject="independent-verifier-b",
        nonce="second-valid-but-over-budget-nonce",
    )
    with pytest.raises(
        ValueError, match="independent_reproduction_primary_budget_exhausted"
    ):
        _reserve_independent(
            manifest=manifest,
            manager=manager,
            primary_completion=primary_completion,
            baseline_path=baseline_path,
            assertion=second_assertion,
            request_id="independent-reproduction-2",
        )


def test_independent_reproduction_must_match_primary_candidate_and_result(
    tmp_path: Path,
) -> None:
    (
        manifest,
        manager,
        _primary_reservation,
        primary_completion,
        baseline_path,
        assertion,
    ) = _completed_primary_with_trusted_reproduction(tmp_path=tmp_path)
    independent = _reserve_independent(
        manifest=manifest,
        manager=manager,
        primary_completion=primary_completion,
        baseline_path=baseline_path,
        assertion=assertion,
    )
    primary_row = primary_completion["row"]
    substituted_gate = _publish_gate(
        manager=manager,
        manifest=manifest,
        selection_artifact_hash=str(primary_row["selection_artifact_hash"]),
        selected_candidate_id="candidate-substitution",
        label="candidate-substitution",
    )

    with pytest.raises(
        ValueError,
        match="independent_activation_primary_mismatch:selected_candidate_id",
    ):
        activate_final_holdout_reservation(
            manager=manager,
            reservation=dict(independent["transport"]),
            manifest_hash=manifest.manifest_hash(),
            selection_artifact_hash=str(primary_row["selection_artifact_hash"]),
            selected_candidate_id="candidate-substitution",
            selection_attempt_index=int(primary_row["selection_attempt_index"]),
            selection_holdout_reuse_count=int(
                primary_row["selection_holdout_reuse_count"]
            ),
            pre_holdout_gate_hash=str(substituted_gate["content_hash"]),
        )

    _activate_independent(
        manager=manager,
        manifest=manifest,
        reservation=independent,
        primary_completion=primary_completion,
    )
    tampered_updates = _independent_completion_updates(primary_completion)
    tampered_updates["final_holdout_result_hash"] = sha256_prefixed(
        {"substituted": "result"}
    )
    with pytest.raises(ValueError, match="independent_completion_primary_mismatch"):
        append_attempt_completion(
            manager=manager,
            reservation=independent,
            updates=tampered_updates,
        )
    append_attempt_completion(
        manager=manager,
        reservation=independent,
        updates=_independent_completion_updates(primary_completion),
    )


def test_generic_registry_apis_cannot_self_declare_independent_purpose(
    tmp_path: Path,
) -> None:
    manager = _manager(
        root=tmp_path / "runtime",
        registry_path=tmp_path / "shared" / "final-holdout.jsonl",
    )
    payload = {
        "experiment_family_id": "attacker-controlled-family",
        "hypothesis_id": "attacker-controlled-hypothesis",
        "final_holdout_access_purpose": (
            FinalHoldoutAccessPurpose.INDEPENDENT_REPRODUCTION.value
        ),
    }
    with pytest.raises(
        ValueError, match="independent_reproduction_requires_trusted_reservation_api"
    ):
        reserve_research_attempt(manager=manager, base_payload=payload)
    with pytest.raises(
        ValueError, match="independent_reproduction_requires_trusted_reservation_api"
    ):
        reserve_research_attempt_checked(manager=manager, base_payload=payload)
    assert not manager.final_holdout_registry_path().exists()


def test_independent_reservation_rejects_wrong_primary_and_tampered_assertion_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        manifest,
        manager,
        _primary_reservation,
        primary_completion,
        baseline_path,
        assertion,
    ) = _completed_primary_with_trusted_reproduction(tmp_path=tmp_path)
    with pytest.raises(
        ValueError, match="independent_reproduction_primary_confirmation_invalid"
    ):
        reserve_independent_reproduction_holdout_authority(
            manager=manager,
            manifest=manifest,
            request_id="wrong-primary",
            request_hash=sha256_prefixed({"request": "wrong-primary"}),
            primary_completion_row_hash=sha256_prefixed({"not": "the-primary"}),
            baseline_receipt_path=baseline_path,
            principal_assertion=assertion,
        )

    monkeypatch.setattr(
        holdout_boundary,
        "load_manifest_with_registry",
        lambda *_args, **_kwargs: manifest,
    )
    assertion_path = tmp_path / "external-assertion.json"
    assertion_payload = assertion.as_dict()
    assertion_payload["subject"] = "forged-subject"
    assertion_path.write_text(
        json.dumps(assertion_payload, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="principal_assertion_content_hash_mismatch"):
        holdout_boundary.reserve_trusted_independent_reproduction_holdout(
            paths=manager,
            strategy_registry=object(),
            manifest_path="/external/manifest.json",
            request_id="forged-assertion",
            request_hash=sha256_prefixed({"request": "forged-assertion"}),
            primary_completion_row_hash=str(primary_completion["row_hash"]),
            baseline_receipt_path=str(baseline_path),
            principal_assertion_path=str(assertion_path),
        )


def test_concurrent_signed_independent_reservations_admit_exactly_one(
    tmp_path: Path,
) -> None:
    (
        manifest,
        manager,
        _primary_reservation,
        primary_completion,
        baseline_path,
        first_assertion,
    ) = _completed_primary_with_trusted_reproduction(tmp_path=tmp_path)
    manager, second_assertion = provision_test_principal_assertion(
        manager=manager,
        scope=first_assertion.scope,
        subject="independent-verifier-b",
        nonce="concurrent-independent-nonce-b",
    )
    assertions = (first_assertion, second_assertion)
    barrier = Barrier(2)

    def attempt(index: int) -> tuple[str, object]:
        barrier.wait(timeout=5)
        try:
            return (
                "accepted",
                _reserve_independent(
                    manifest=manifest,
                    manager=manager,
                    primary_completion=primary_completion,
                    baseline_path=baseline_path,
                    assertion=assertions[index],
                    request_id=f"concurrent-independent-{index}",
                ),
            )
        except ValueError as exc:
            return ("rejected", str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    accepted = [value for status, value in results if status == "accepted"]
    rejected = [value for status, value in results if status == "rejected"]
    assert len(accepted) == len(rejected) == 1
    assert "independent_reproduction_primary_budget_exhausted" in str(rejected[0])
    accepted_reservation = accepted[0]
    assert isinstance(accepted_reservation, dict)
    abort_final_holdout_reservation(
        manager=manager,
        reservation=dict(accepted_reservation["transport"]),
        reason="independent_reproduction_test_cleanup_before_exposure",
    )
    with pytest.raises(
        ValueError, match="independent_reproduction_primary_budget_exhausted"
    ):
        _reserve_independent(
            manifest=manifest,
            manager=manager,
            primary_completion=primary_completion,
            baseline_path=baseline_path,
            assertion=(
                second_assertion
                if accepted_reservation["row"]["independent_principal_assertion_hash"]
                == first_assertion.content_hash
                else first_assertion
            ),
            request_id="post-abort-retry-forbidden",
        )


def test_shared_authority_survives_sandbox_reset_and_blocks_second_job(
    tmp_path: Path,
) -> None:
    manifest = _Manifest()
    registry_path = tmp_path / "shared" / "final-holdout.jsonl"
    parent = _manager(root=tmp_path / "parent", registry_path=registry_path)
    first_child_root = tmp_path / "job-a"
    first_child = _manager(root=first_child_root, registry_path=registry_path)
    second_child = _manager(root=tmp_path / "job-b", registry_path=registry_path)

    reservation = _reserve(
        manager=parent,
        manifest=manifest,
        request_id="operated-job-a",
    )
    authoritative = validate_final_holdout_reservation_transport(
        manager=first_child,
        reservation=reservation,
    )
    activation = _activate(
        manager=first_child,
        manifest=manifest,
        reservation=reservation,
    )
    assert activation["row"]["reservation_row_hash"] == authoritative["row_hash"]

    first_child_root.mkdir(parents=True, exist_ok=True)
    (first_child_root / "sandbox-marker").write_text("ephemeral", encoding="utf-8")
    shutil.rmtree(first_child_root)
    abort_final_holdout_reservation(
        manager=parent,
        reservation=reservation,
        reason="sandbox_removed_after_exposure",
    )

    with pytest.raises(
        ValueError, match="final_holdout_authority_scope_already_exposed"
    ):
        _reserve(
            manager=second_child,
            manifest=manifest,
            request_id="operated-job-b",
        )

    rows = load_experiment_registry_rows(registry_path)
    assert [row["event_type"] for row in rows[:4]] == [
        "research_attempt_reserved",
        "research_attempt_activated",
        "research_attempt_holdout_read_started",
        "research_attempt_aborted",
    ]
    assert rows[3]["holdout_accessed"] is True


def test_clean_pre_holdout_abort_releases_scope_and_advances_fence(
    tmp_path: Path,
) -> None:
    manifest = _Manifest()
    registry_path = tmp_path / "shared" / "final-holdout.jsonl"
    first_manager = _manager(root=tmp_path / "job-a", registry_path=registry_path)
    second_manager = _manager(root=tmp_path / "job-b", registry_path=registry_path)
    first = _reserve(
        manager=first_manager,
        manifest=manifest,
        request_id="missing-validation-experiments",
    )

    aborted = abort_final_holdout_reservation(
        manager=first_manager,
        reservation=first,
        reason="pre_holdout_validation_experiment_gate_failed",
    )
    assert aborted is not None
    assert aborted["row"]["holdout_accessed"] is False
    assert aborted["row"]["holdout_access_status"] == "PRE_EXPOSURE_ABORTED"

    second = _reserve(
        manager=second_manager,
        manifest=manifest,
        request_id="corrected-validation-experiments",
    )
    assert second["fence_generation"] == first["fence_generation"] + 1
    with pytest.raises(ValueError, match="already_terminal"):
        validate_final_holdout_reservation_transport(
            manager=second_manager,
            reservation=first,
        )
    abort_final_holdout_reservation(
        manager=second_manager,
        reservation=second,
        reason="test_cleanup_before_exposure",
    )


def test_concurrent_jobs_receive_exactly_one_shared_scope_reservation(
    tmp_path: Path,
) -> None:
    manifest = _Manifest()
    registry_path = tmp_path / "shared" / "final-holdout.jsonl"
    managers = (
        _manager(root=tmp_path / "job-a", registry_path=registry_path),
        _manager(root=tmp_path / "job-b", registry_path=registry_path),
    )
    barrier = Barrier(2)

    def attempt(index: int) -> tuple[str, object]:
        barrier.wait(timeout=5)
        try:
            return (
                "accepted",
                _reserve(
                    manager=managers[index],
                    manifest=manifest,
                    request_id=f"concurrent-job-{index}",
                ),
            )
        except ValueError as exc:
            return ("rejected", str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))

    accepted = [value for status, value in results if status == "accepted"]
    rejected = [value for status, value in results if status == "rejected"]
    assert len(accepted) == len(rejected) == 1
    assert "final_holdout_authority_scope_already_exposed" in str(rejected[0])
    abort_final_holdout_reservation(
        manager=managers[0],
        reservation=accepted[0],
        reason="concurrency_test_cleanup_before_exposure",
    )


def test_tampered_transport_and_reused_activation_fence_fail_closed(
    tmp_path: Path,
) -> None:
    manifest = _Manifest()
    registry_path = tmp_path / "shared" / "final-holdout.jsonl"
    manager = _manager(root=tmp_path / "job-a", registry_path=registry_path)
    reservation = _reserve(
        manager=manager,
        manifest=manifest,
        request_id="tamper-target",
    )

    tampered = copy.deepcopy(reservation)
    tampered["fence_generation"] = int(tampered["fence_generation"]) + 1
    material = {key: value for key, value in tampered.items() if key != "content_hash"}
    tampered["content_hash"] = sha256_prefixed(
        material,
        label="final_holdout_reservation_transport",
    )
    with pytest.raises(ValueError, match="stale_or_tampered"):
        validate_final_holdout_reservation_transport(
            manager=manager,
            reservation=tampered,
        )

    _activate(manager=manager, manifest=manifest, reservation=reservation)
    with pytest.raises(ValueError, match="fence_already_used"):
        _activate(manager=manager, manifest=manifest, reservation=reservation)
    abort_final_holdout_reservation(
        manager=manager,
        reservation=reservation,
        reason="tamper_test_cleanup_after_exposure",
    )
    assert (
        validate_final_holdout_authority_registry(
            manager=manager,
            require_terminal=True,
        )["status"]
        == "PASS"
    )

    lines = registry_path.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace("tamper-target", "tamper-bypass", 1)
    registry_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    validation = validate_final_holdout_authority_registry(
        manager=manager,
        require_terminal=True,
    )
    assert validation["status"] == "FAIL"
    assert "experiment_registry_row_hash_mismatch" in validation["reasons"]
    with pytest.raises(ValueError, match="chain_invalid"):
        _reserve(
            manager=manager,
            manifest=manifest,
            request_id="tamper-bypass-job",
        )


def test_holdout_read_capability_is_registry_bound_and_consumed_once(
    tmp_path: Path,
) -> None:
    manifest = _Manifest()
    manager = _manager(
        root=tmp_path / "primary",
        registry_path=tmp_path / "authority" / "final-holdout.jsonl",
    )
    reservation = _reserve(
        manager=manager,
        manifest=manifest,
        request_id="one-use-read-capability",
    )
    selection_hash = sha256_prefixed({"selection": "candidate-a"})
    gate = _publish_gate(
        manager=manager,
        manifest=manifest,
        selection_artifact_hash=selection_hash,
        selected_candidate_id="candidate-a",
        label="one-use-read-capability",
    )
    activation = activate_final_holdout_reservation(
        manager=manager,
        reservation=reservation,
        manifest_hash=manifest.manifest_hash(),
        selection_artifact_hash=selection_hash,
        selected_candidate_id="candidate-a",
        selection_attempt_index=1,
        selection_holdout_reuse_count=0,
        pre_holdout_gate_hash=str(gate["content_hash"]),
    )
    holdout = manifest.dataset.split.final_holdout
    assert holdout is not None
    capability = activation["holdout_read_capability"]
    alternate = _manager(
        root=tmp_path / "alternate",
        registry_path=tmp_path / "other-authority" / "final-holdout.jsonl",
    )
    with pytest.raises(ValueError, match="holdout_read_capability_binding_mismatch"):
        consume_final_holdout_read_capability(
            manager=alternate,
            capability=capability,
            manifest_hash=manifest.manifest_hash(),
            authority_scope_hash=final_holdout_authority_scope_hash(manifest),
            split_name="alias_for_final_holdout",
            requested_range=holdout.as_dict(),
        )
    consumed = consume_final_holdout_read_capability(
        manager=manager,
        capability=capability,
        manifest_hash=manifest.manifest_hash(),
        authority_scope_hash=final_holdout_authority_scope_hash(manifest),
        split_name="alias_for_final_holdout",
        requested_range=holdout.as_dict(),
    )
    assert consumed["event_type"] == "research_attempt_holdout_read_started"
    reservation_row = next(
        row
        for row in load_experiment_registry_rows(
            Path(str(reservation["registry_path"]))
        )
        if row.get("event_type") == "research_attempt_reserved"
    )
    projected = split_exposure_rows(
        manager,
        hypothesis_id=str(reservation_row["hypothesis_id"]),
        hypothesis_version=str(reservation_row["hypothesis_version"]),
    )
    assert len(projected) == 1
    assert projected[0]["split_name"] == "final_holdout"
    assert projected[0]["purpose"] == "final_confirmation"
    assert projected[0]["authority_event_row_hash"] == consumed["row_hash"]
    assert projected[0]["prior_phase_exposure_count"] == 0
    with pytest.raises(ValueError, match="holdout_read_capability_already_consumed"):
        consume_final_holdout_read_capability(
            manager=manager,
            capability=capability,
            manifest_hash=manifest.manifest_hash(),
            authority_scope_hash=final_holdout_authority_scope_hash(manifest),
            split_name="final_holdout",
            requested_range=holdout.as_dict(),
        )
    abort_final_holdout_reservation(
        manager=manager,
        reservation=reservation,
        reason="one_use_capability_test_complete",
    )
    with pytest.raises(
        ValueError, match="holdout_read_capability_reservation_terminal"
    ):
        consume_final_holdout_read_capability(
            manager=manager,
            capability=capability,
            manifest_hash=manifest.manifest_hash(),
            authority_scope_hash=final_holdout_authority_scope_hash(manifest),
            split_name="final_holdout",
            requested_range=holdout.as_dict(),
        )


def test_application_boundary_is_idempotent_and_sandbox_binds_shared_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _Manifest()
    registry_path = tmp_path / "shared" / "final-holdout.jsonl"
    manager = _manager(root=tmp_path / "parent", registry_path=registry_path)
    monkeypatch.setattr(
        holdout_boundary,
        "load_manifest_with_registry",
        lambda *_args, **_kwargs: manifest,
    )
    actor = ActorContext(actor_id="worker-1", source="worker")
    request_hash = sha256_prefixed({"api_request": "one"})

    reservation = holdout_boundary.reserve_operated_final_holdout(
        paths=manager,
        strategy_registry=object(),
        manifest_path="/externally-verified/manifest.json",
        request_id="api-job-1",
        request_hash=request_hash,
        actor=actor,
    )
    replay = holdout_boundary.reserve_operated_final_holdout(
        paths=manager,
        strategy_registry=object(),
        manifest_path="/externally-verified/manifest.json",
        request_id="api-job-1",
        request_hash=request_hash,
        actor=actor,
    )
    assert isinstance(reservation, dict)
    assert replay == reservation
    assert reservation["content_hash"].startswith("sha256:")
    application_request = ResearchValidationRequest(
        manifest_path="/externally-verified/manifest.json",
        final_holdout_reservation=reservation,
    )
    assert application_request.final_holdout_reservation is not None
    with pytest.raises(Exception, match="frozen"):
        application_request.final_holdout_reservation.fence_generation = 99

    sandbox_root = tmp_path / "job-sandbox"
    request = {
        "schema_version": 1,
        "job_id": "api-job-1",
        "capability_id": "research-validate",
        "request_hash": request_hash,
        "manifest_hash": manifest.manifest_hash(),
        "manifest_content_hash": sha256_prefixed({"manifest": "content"}),
        "manifest_path": "/externally-verified/manifest.json",
        "runtime_project_root": str(Path(__file__).resolve().parents[1]),
        "sandbox_root": str(sandbox_root),
        "final_holdout_reservation": reservation,
        "settings": {
            "data_root": str(tmp_path / "external-data"),
            "artifact_root": str(sandbox_root / "artifacts"),
            "report_root": str(sandbox_root / "reports"),
            "cache_root": str(sandbox_root / "cache"),
            "db_path": None,
            "max_workers": 1,
            "random_seed": 0,
            "experiment_identity_registry_path": str(
                sandbox_root / "control" / "experiment-identity.jsonl"
            ),
            "final_holdout_registry_path": str(registry_path),
        },
        "actor": actor.model_dump(mode="json"),
    }
    assert _validated_request(request)["final_holdout_reservation"] == reservation

    inside_sandbox = copy.deepcopy(request)
    inside_sandbox["settings"]["final_holdout_registry_path"] = str(
        sandbox_root / "resettable-authority.jsonl"
    )
    with pytest.raises(SandboxJobContractError, match="must_be_shared"):
        _validated_request(inside_sandbox)

    mismatched = copy.deepcopy(request)
    mismatched["final_holdout_reservation"]["registry_path"] = str(
        tmp_path / "different-authority.jsonl"
    )
    with pytest.raises(SandboxJobContractError, match="registry_mismatch"):
        _validated_request(mismatched)

    with pytest.raises(ValueError, match="scope_already_exposed"):
        holdout_boundary.reserve_operated_final_holdout(
            paths=manager,
            strategy_registry=object(),
            manifest_path="/externally-verified/manifest.json",
            request_id="api-job-2",
            request_hash=sha256_prefixed({"api_request": "two"}),
            actor=actor,
        )
    holdout_boundary.abort_operated_final_holdout(
        paths=manager,
        reservation=reservation,
        reason="api_test_cleanup_before_exposure",
    )


def test_application_boundary_skips_reservation_without_final_holdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _Manifest(dataset=_Dataset(split=_Split(final_holdout=None)))
    manager = _manager(
        root=tmp_path / "parent",
        registry_path=tmp_path / "shared" / "final-holdout.jsonl",
    )
    monkeypatch.setattr(
        holdout_boundary,
        "load_manifest_with_registry",
        lambda *_args, **_kwargs: manifest,
    )

    assert (
        holdout_boundary.reserve_operated_final_holdout(
            paths=manager,
            strategy_registry=object(),
            manifest_path="/externally-verified/manifest.json",
            request_id="no-holdout-job",
            request_hash=sha256_prefixed({"api_request": "no-holdout"}),
            actor=ActorContext(actor_id="worker-1", source="worker"),
        )
        is None
    )
    assert not manager.final_holdout_registry_path().exists()


def test_manifest_rebind_cannot_reexpose_the_same_dataset_holdout(
    tmp_path: Path,
) -> None:
    first_manifest = _Manifest(experiment_id="first-hypothesis")
    rebound_manifest = _Manifest(experiment_id="rebound-hypothesis")
    registry_path = tmp_path / "shared" / "final-holdout.jsonl"
    manager = _manager(root=tmp_path / "runtime", registry_path=registry_path)
    reservation = _reserve(
        manager=manager,
        manifest=first_manifest,
        request_id="first-job",
    )
    _activate(manager=manager, manifest=first_manifest, reservation=reservation)
    abort_final_holdout_reservation(
        manager=manager,
        reservation=reservation,
        reason="exposure_finished_with_failure",
    )

    with pytest.raises(ValueError, match="scope_already_exposed"):
        _reserve(
            manager=manager,
            manifest=rebound_manifest,
            request_id="rebound-job",
        )


def test_idempotent_request_cannot_be_rebound_to_another_actor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _Manifest()
    manager = _manager(
        root=tmp_path / "runtime",
        registry_path=tmp_path / "shared" / "final-holdout.jsonl",
    )
    monkeypatch.setattr(
        holdout_boundary,
        "load_manifest_with_registry",
        lambda *_args, **_kwargs: manifest,
    )
    request_hash = sha256_prefixed({"request": "immutable"})
    first = holdout_boundary.reserve_operated_final_holdout(
        paths=manager,
        strategy_registry=object(),
        manifest_path="/external/manifest.json",
        request_id="same-job",
        request_hash=request_hash,
        actor=ActorContext(actor_id="worker-a", source="worker"),
    )
    assert first is not None
    with pytest.raises(ValueError, match="idempotency_conflict"):
        holdout_boundary.reserve_operated_final_holdout(
            paths=manager,
            strategy_registry=object(),
            manifest_path="/external/manifest.json",
            request_id="same-job",
            request_hash=request_hash,
            actor=ActorContext(actor_id="worker-b", source="worker"),
        )
    holdout_boundary.abort_operated_final_holdout(
        paths=manager,
        reservation=first,
        reason="rebind_test_cleanup",
    )


@pytest.mark.parametrize("mode", (0o666, 0o600))
def test_shared_authority_rejects_wrong_or_world_writable_ledger_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
) -> None:
    monkeypatch.setenv(ATOMIC_PUBLICATION_MODE_ENV, "0640")
    manager = _manager(
        root=tmp_path / "runtime",
        registry_path=tmp_path / "shared" / "final-holdout.jsonl",
    )
    reservation = _reserve(
        manager=manager,
        manifest=_Manifest(),
        request_id="mode-test",
    )
    registry = manager.final_holdout_registry_path()
    assert stat.S_IMODE(registry.parent.stat().st_mode) == 0o2770
    assert stat.S_IMODE(registry.stat().st_mode) == 0o660
    assert (
        stat.S_IMODE(registry.with_suffix(registry.suffix + ".lock").stat().st_mode)
        == 0o660
    )
    registry.chmod(mode)

    validation = validate_final_holdout_authority_registry(manager=manager)
    assert validation["status"] == "FAIL"
    assert validation["reasons"] == ["final_holdout_authority_unreadable:ValueError"]
    with pytest.raises(ValueError, match="authority_file_access_invalid"):
        validate_final_holdout_reservation_transport(
            manager=manager,
            reservation=reservation,
        )


def test_authority_path_rejects_symlink_replacement_before_read(
    tmp_path: Path,
) -> None:
    manager = _manager(
        root=tmp_path / "runtime",
        registry_path=tmp_path / "shared" / "final-holdout.jsonl",
    )
    reservation = _reserve(
        manager=manager,
        manifest=_Manifest(),
        request_id="symlink-test",
    )
    registry = manager.final_holdout_registry_path()
    target = registry.with_name("redirected.jsonl")
    registry.rename(target)
    registry.symlink_to(target)

    with pytest.raises(ValueError, match="symbolic links"):
        validate_final_holdout_reservation_transport(
            manager=manager,
            reservation=reservation,
        )


@pytest.mark.parametrize("replacement", ("symlink", "directory", "world-writable"))
def test_authority_lock_and_ledger_replacements_fail_before_transport_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    monkeypatch.setenv(ATOMIC_PUBLICATION_MODE_ENV, "0640")
    manager = _manager(
        root=tmp_path / "runtime",
        registry_path=tmp_path / "shared" / "final-holdout.jsonl",
    )
    reservation = _reserve(
        manager=manager,
        manifest=_Manifest(),
        request_id=f"replacement-{replacement}",
    )
    registry = manager.final_holdout_registry_path()
    lock = registry.with_suffix(registry.suffix + ".lock")
    if replacement == "symlink":
        target = lock.with_name("redirected.lock")
        lock.rename(target)
        lock.symlink_to(target)
    elif replacement == "directory":
        registry.unlink()
        registry.mkdir(mode=0o770)
    else:
        lock.chmod(0o666)

    with pytest.raises((ValueError, OSError)):
        validate_final_holdout_reservation_transport(
            manager=manager,
            reservation=reservation,
        )
