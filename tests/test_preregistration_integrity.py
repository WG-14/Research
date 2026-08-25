from __future__ import annotations

import json
from pathlib import Path

import pytest

from market_research.research.governance import (
    governance_registry_path,
    load_governance_rows,
)
from market_research.research.knowledge_registry import freeze_validation_admission
from market_research.research.split_usage_policy import (
    SplitUsagePolicyError,
    record_split_exposure,
    require_successor_after_confirmatory_exposure,
    split_exposure_rows,
    validate_split_usage,
)
from market_research.research.study_lifecycle import (
    StudyLifecycleError,
    admit_study_validation,
    preregister_study,
    record_study_stage,
    study_preregistration_registry_path,
)
from tests.data_governance_fixture import seed_confirmatory_data_governance
from tests.test_study_lifecycle import _design, _hash, _manager, _manifest


def _disable_unrelated_pit_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "market_research.research.knowledge_registry.require_point_in_time_scope",
        lambda *_args, **_kwargs: None,
    )


def _record_prevalidation_history(*, manager: object, manifest: object) -> None:
    for state, recorded_at, exploration_hash in (
        ("IDEA", "2025-12-05T00:00:00+00:00", None),
        ("STRUCTURED", "2025-12-06T00:00:00+00:00", None),
        ("EXPLORATORY", "2025-12-07T00:00:00+00:00", _hash("d")),
    ):
        record_study_stage(
            manager=manager,  # type: ignore[arg-type]
            hypothesis=manifest.hypothesis_spec,  # type: ignore[attr-defined]
            to_state=state,
            actor_id="researcher-a",
            recorded_at=recorded_at,
            reason=f"Record independent {state} work.",
            exploration_evidence_hash=exploration_hash,
        )


def test_admission_cannot_synthesize_missing_preregistration_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest(tmp_path)
    _disable_unrelated_pit_checks(monkeypatch)
    seed_confirmatory_data_governance(manager=manager, manifest=manifest)
    admission = freeze_validation_admission(
        manager=manager,
        manifest=manifest,
        admitted_at="2026-01-01T00:00:00+00:00",
    )

    with pytest.raises(
        StudyLifecycleError,
        match="preexisting_preregistration_missing",
    ):
        admit_study_validation(
            manager=manager,
            manifest=manifest,
            validation_admission=admission,
            run_id="RUN-no-prereg",
        )

    assert load_governance_rows(governance_registry_path(manager)) == []


def test_stage_history_rejects_same_timestamp_without_poisoning_stream(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest(tmp_path)
    record_study_stage(
        manager=manager,
        hypothesis=manifest.hypothesis_spec,
        to_state="IDEA",
        actor_id="researcher-a",
        recorded_at="2025-12-05T00:00:00+00:00",
        reason="Record the idea independently.",
    )

    with pytest.raises(StudyLifecycleError, match="timestamps_not_strictly"):
        record_study_stage(
            manager=manager,
            hypothesis=manifest.hypothesis_spec,
            to_state="STRUCTURED",
            actor_id="researcher-a",
            recorded_at="2025-12-05T00:00:00+00:00",
            reason="Invalid same-time structuring.",
        )

    assert [
        row["to_state"]
        for row in load_governance_rows(governance_registry_path(manager))
    ] == ["IDEA"]


def test_admission_rejects_tampered_preregistration_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest(tmp_path)
    _record_prevalidation_history(manager=manager, manifest=manifest)
    preregister_study(
        manager=manager,
        hypothesis=manifest.hypothesis_spec,
        design=_design(manifest),
        reason="Freeze the independent design.",
    )
    _disable_unrelated_pit_checks(monkeypatch)
    seed_confirmatory_data_governance(manager=manager, manifest=manifest)
    admission = freeze_validation_admission(
        manager=manager,
        manifest=manifest,
        admitted_at="2026-01-01T00:00:00+00:00",
    )
    path = study_preregistration_registry_path(manager)
    row = json.loads(path.read_text(encoding="utf-8"))
    row["design"]["target_variable"] = "tampered target"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    with pytest.raises(StudyLifecycleError, match="registry_invalid"):
        admit_study_validation(
            manager=manager,
            manifest=manifest,
            validation_admission=admission,
            run_id="RUN-tampered-prereg",
        )


def test_admission_records_one_idempotent_validation_exposure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest(tmp_path)
    _record_prevalidation_history(manager=manager, manifest=manifest)
    preregister_study(
        manager=manager,
        hypothesis=manifest.hypothesis_spec,
        design=_design(manifest),
        reason="Freeze the independent design.",
    )
    _disable_unrelated_pit_checks(monkeypatch)
    seed_confirmatory_data_governance(manager=manager, manifest=manifest)
    admission = freeze_validation_admission(
        manager=manager,
        manifest=manifest,
        admitted_at="2026-01-01T00:00:00+00:00",
    )

    for _attempt in range(2):
        admit_study_validation(
            manager=manager,
            manifest=manifest,
            validation_admission=admission,
            run_id="RUN-validation-exposure",
        )

    rows = split_exposure_rows(
        manager,
        hypothesis_id=manifest.hypothesis_spec.hypothesis_id,
        hypothesis_version=manifest.hypothesis_spec.version,
    )
    assert len(rows) == 1
    assert rows[0]["split_name"] == "validation"
    assert rows[0]["purpose"] == "confirmatory_validation"
    assert rows[0]["actor_id"] == manifest.hypothesis_spec.actor_id
    assert rows[0]["source_artifact_hash"] == manifest.manifest_hash()


def test_phase_purpose_and_confirmatory_exposure_force_new_version(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(SplitUsagePolicyError, match="purpose_phase_mismatch"):
        validate_split_usage(
            split_name="validation",
            purpose="feature_mining",
        )

    first = record_split_exposure(
        manager=manager,
        event_id="validation-access-1",
        hypothesis_id="hypothesis-a",
        hypothesis_version="1.0.0",
        split_name="validation",
        purpose="confirmatory_validation",
        actor_id="validator-a",
        recorded_at="2026-01-01T00:00:00+00:00",
        source_artifact_hash=_hash("1"),
    )
    assert (
        record_split_exposure(
            manager=manager,
            event_id="validation-access-1",
            hypothesis_id="hypothesis-a",
            hypothesis_version="1.0.0",
            split_name="validation",
            purpose="confirmatory_validation",
            actor_id="validator-a",
            recorded_at="2026-01-01T00:00:00+00:00",
            source_artifact_hash=_hash("1"),
        )
        == first
    )
    second = record_split_exposure(
        manager=manager,
        event_id="validation-access-2",
        hypothesis_id="hypothesis-a",
        hypothesis_version="1.0.0",
        split_name="validation",
        purpose="confirmatory_validation",
        actor_id="validator-a",
        recorded_at="2026-01-02T00:00:00+00:00",
        source_artifact_hash=_hash("2"),
    )
    assert first["purity_status"] == "PURE"
    assert second["purity_status"] == "REPEATED_CONFIRMATORY_ACCESS"
    with pytest.raises(
        SplitUsagePolicyError,
        match="timestamps_not_strictly_increasing",
    ):
        record_split_exposure(
            manager=manager,
            event_id="validation-access-backdated",
            hypothesis_id="hypothesis-a",
            hypothesis_version="1.0.0",
            split_name="validation",
            purpose="confirmatory_validation",
            actor_id="validator-a",
            recorded_at="2025-12-31T00:00:00+00:00",
            source_artifact_hash=_hash("3"),
        )
    with pytest.raises(
        SplitUsagePolicyError,
        match="owned_by_experiment_registry",
    ):
        record_split_exposure(
            manager=manager,
            event_id="manual-final-holdout-access",
            hypothesis_id="hypothesis-a",
            hypothesis_version="1.0.0",
            split_name="final_holdout",
            purpose="final_confirmation",
            actor_id="validator-a",
            recorded_at="2026-01-03T00:00:00+00:00",
            source_artifact_hash=_hash("4"),
        )
    with pytest.raises(SplitUsagePolicyError, match="requires_new_hypothesis_version"):
        require_successor_after_confirmatory_exposure(
            manager=manager,
            hypothesis_id="hypothesis-a",
            exposed_version="1.0.0",
            proposed_version="1.0.0",
        )
    require_successor_after_confirmatory_exposure(
        manager=manager,
        hypothesis_id="hypothesis-a",
        exposed_version="1.0.0",
        proposed_version="2.0.0",
    )
