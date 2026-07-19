from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.governance import (
    GovernanceSubject,
    GovernanceSubjectType,
    append_lifecycle_transition,
    approve_strategy_candidate,
    governance_registry_path,
    load_governance_rows,
    validate_governance_registry,
    validate_strategy_approval,
)
from market_research.research.hashing import report_content_hash_payload, sha256_prefixed
from market_research.research.hypothesis_contract import parse_hypothesis_spec
from market_research.research.knowledge_registry import (
    freeze_validation_admission,
    get_knowledge_record,
    validate_knowledge_registry,
)
from market_research.research.study_lifecycle import (
    StudyLifecycleError,
    admit_study_validation,
    complete_study_validation,
    preserve_study_validation_failure,
    register_posthoc_followup,
)
from market_research.research.validation_decision import query_validation_decisions
from market_research.settings import ResearchSettings
from tests.hypothesis_lineage_fixture import hypothesis_spec_v2


def _manager(tmp_path: Path) -> ResearchPathManager:
    return ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "data",
            artifact_root=tmp_path / "artifacts",
            report_root=tmp_path / "reports",
            cache_root=tmp_path / "cache",
            db_path=tmp_path / "input.sqlite",
            max_workers=1,
            random_seed=0,
        ),
        project_root=Path.cwd(),
    )


def _hash(char: str) -> str:
    return "sha256:" + char * 64


@dataclass(frozen=True)
class _ManifestStub:
    experiment_id: str
    hypothesis_spec: Any
    research_classification: str
    canonical: dict[str, Any]
    dataset: Any
    raw: dict[str, Any]

    def canonical_payload(self) -> dict[str, Any]:
        return self.canonical

    def manifest_hash(self) -> str:
        return sha256_prefixed(self.canonical)

    def simulation_seed_scope_hash(self) -> str:
        return sha256_prefixed({"seed_scope": self.canonical})


def _manifest() -> _ManifestStub:
    hypothesis = parse_hypothesis_spec(hypothesis_spec_v2())
    split = {
        "train": {"start": "2025-01-01", "end": "2025-06-30"},
        "validation": {"start": "2025-07-01", "end": "2025-09-30"},
        "final_holdout": {"start": "2025-10-01", "end": "2025-12-31"},
    }
    canonical = {
        "experiment_id": "study-lifecycle-experiment",
        "hypothesis_spec": hypothesis.as_dict(),
        "dataset": {"snapshot_id": "immutable-snapshot-1", **split},
        "parameter_space": {"threshold": [1, 2]},
        "acceptance_gate": {"min_trade_count": 10},
        "statistical_validation": {"seed_policy": "derived"},
        "stress_suite": {"seed_policy": "derived"},
        "final_selection": {"metric": "return_pct"},
        "walk_forward": None,
        "cost_model": {"fee_rate": 0.001},
        "execution_model": {"type": "fixed_bps"},
        "execution_timing": {"decision": "close", "fill": "next_open"},
        "portfolio_policy": {"starting_cash_krw": 1_000_000},
        "risk_policy": {"max_position_pct": 100},
        "research_run": {"max_workers": 1},
    }
    return _ManifestStub(
        experiment_id="study-lifecycle-experiment",
        hypothesis_spec=hypothesis,
        research_classification="validated_candidate",
        canonical=canonical,
        dataset=SimpleNamespace(split=SimpleNamespace(as_dict=lambda: split)),
        raw={},
    )


def _freeze_and_admit(
    *,
    manager: ResearchPathManager,
    manifest: _ManifestStub,
    monkeypatch: pytest.MonkeyPatch,
    run_id: str = "RUN-study-001",
) -> dict[str, Any]:
    # Point-in-time authority has its own focused contract tests. This fixture
    # isolates lifecycle behavior after that authority gate has passed.
    monkeypatch.setattr(
        "market_research.research.knowledge_registry.require_point_in_time_scope",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "market_research.research.validation_decision.validation_admission_binding_reasons",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "market_research.research.validation_pipeline.validate_validated_research_result",
        lambda *_args, **_kwargs: [],
    )
    admission = freeze_validation_admission(
        manager=manager,
        manifest=manifest,
        admitted_at="2026-01-01T00:00:00+00:00",
    )
    publication = admit_study_validation(
        manager=manager,
        manifest=manifest,
        validation_admission=admission,
        run_id=run_id,
    )
    assert publication.state == "VALIDATING"
    return admission


def _report(
    admission: dict[str, Any],
    *,
    result: str,
    content_hash: str,
) -> dict[str, Any]:
    del content_hash
    manifest = _manifest()
    payload = {
        "schema_version": 3,
        "artifact_type": "validated_research_result",
        "experiment_id": manifest.experiment_id,
        "run_id": "RUN-study-001",
        "manifest_hash": manifest.manifest_hash(),
        "hypothesis_id": manifest.hypothesis_spec.hypothesis_id,
        "hypothesis_version": manifest.hypothesis_spec.version,
        "hypothesis_contract_hash": manifest.hypothesis_spec.contract_hash(),
        "end_to_end_validation_result": result,
        "generated_at": "2026-01-02T00:00:00+00:00",
        "validation_blocking_reasons": (
            [] if result == "PASS" else ["synthetic_frozen_gate_not_passed"]
        ),
        "validation_stages": [{"name": "final_holdout", "status": result}],
        "validation_admission": deepcopy(admission["admission"]),
        "validation_admission_record_hash": admission["admission_record_hash"],
        "validation_admission_row_hash": admission["admission_row_hash"],
    }
    payload["content_hash"] = sha256_prefixed(report_content_hash_payload(payload))
    return payload


def test_admission_aligns_standard_states_with_evidence_decisions_and_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest()
    admission = _freeze_and_admit(
        manager=manager,
        manifest=manifest,
        monkeypatch=monkeypatch,
    )

    rows = load_governance_rows(governance_registry_path(manager))
    assert [row["to_state"] for row in rows] == [
        "IDEA",
        "STRUCTURED",
        "EXPLORATORY",
        "PREREGISTERED",
        "VALIDATING",
    ]
    assert all(row["evidence_hashes"] for row in rows)
    assert all(
        row.get("decision_record_hash") and row.get("decision_registry_row_hash")
        for row in rows[1:]
    )
    assert rows[-2]["evidence_hashes"]["preregistration_hash"] == (
        admission["admission_record_hash"]
    )
    original_counts = (
        validate_governance_registry(manager)["row_count"],
        validate_knowledge_registry(manager)["row_count"],
    )

    replay = admit_study_validation(
        manager=manager,
        manifest=manifest,
        validation_admission=admission,
        run_id="RUN-study-001",
    )
    assert replay.state == "VALIDATING"
    assert original_counts == (
        validate_governance_registry(manager)["row_count"],
        validate_knowledge_registry(manager)["row_count"],
    )
    with pytest.raises(StudyLifecycleError, match="evidence_conflict:VALIDATING"):
        admit_study_validation(
            manager=manager,
            manifest=manifest,
            validation_admission=admission,
            run_id="RUN-study-002",
        )
    assert validate_governance_registry(manager)["status"] == "PASS"
    assert validate_knowledge_registry(manager)["status"] == "PASS"


@pytest.mark.parametrize(
    ("terminal_result", "expected_state", "expected_outcome", "hash_char"),
    (
        ("PASS", "VALIDATED", "supported", "5"),
        ("FAIL", "REJECTED", "rejected", "6"),
        ("INSUFFICIENT_EVIDENCE", "INCONCLUSIVE", "inconclusive", "7"),
    ),
)
def test_terminal_result_automatically_publishes_decision_outcome_and_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_result: str,
    expected_state: str,
    expected_outcome: str,
    hash_char: str,
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest()
    admission = _freeze_and_admit(
        manager=manager,
        manifest=manifest,
        monkeypatch=monkeypatch,
    )
    report = _report(
        admission,
        result=terminal_result,
        content_hash=_hash(hash_char),
    )

    publication = complete_study_validation(
        manager=manager,
        manifest=manifest,
        run_id="RUN-study-001",
        report=report,
    )
    assert publication.state == expected_state
    assert publication.decision_row is not None
    assert publication.decision_row["payload"]["decision"] == expected_state
    assert publication.transition_row is not None
    assert publication.transition_row["evidence_hashes"][
        "validation_report_hash"
    ] == report["content_hash"]
    decisions = query_validation_decisions(
        manager=manager,
        hypothesis_id=manifest.hypothesis_spec.hypothesis_id,
        decision=expected_state,
    )
    assert len(decisions) == 1
    outcome = get_knowledge_record(
        manager=manager,
        record_type="hypothesis_outcome",
        logical_id=(
            "outcome:validation-result:"
            f"{manifest.experiment_id}:RUN-study-001"
        ),
        version="1",
    )
    assert outcome["payload"]["outcome"] == expected_outcome
    original_counts = (
        validate_governance_registry(manager)["row_count"],
        validate_knowledge_registry(manager)["row_count"],
    )

    replay = complete_study_validation(
        manager=manager,
        manifest=manifest,
        run_id="RUN-study-001",
        report=report,
    )
    assert replay == publication
    assert original_counts == (
        validate_governance_registry(manager)["row_count"],
        validate_knowledge_registry(manager)["row_count"],
    )
    conflicting_report = deepcopy(report)
    conflicting_report["validation_stages"] = [
        *conflicting_report["validation_stages"],
        {"name": "synthetic_conflict", "status": "FAIL"},
    ]
    conflicting_report["content_hash"] = sha256_prefixed(
        report_content_hash_payload(conflicting_report)
    )
    with pytest.raises(ValueError, match="validation_decision_subject_conflict"):
        complete_study_validation(
            manager=manager,
            manifest=manifest,
            run_id="RUN-study-001",
            report=conflicting_report,
        )


def test_execution_failure_is_inconclusive_and_replay_is_immutable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest()
    _freeze_and_admit(
        manager=manager,
        manifest=manifest,
        monkeypatch=monkeypatch,
    )

    publication = preserve_study_validation_failure(
        manager=manager,
        manifest=manifest,
        run_id="RUN-study-001",
        error=RuntimeError("synthetic execution detail"),
    )
    assert publication.state == "INCONCLUSIVE"
    assert publication.decision_row is not None
    assert publication.decision_row["payload"]["failure_type"] == (
        "execution_failure"
    )
    assert "synthetic execution detail" not in str(publication.decision_row)
    original_counts = (
        validate_governance_registry(manager)["row_count"],
        validate_knowledge_registry(manager)["row_count"],
    )

    replay = preserve_study_validation_failure(
        manager=manager,
        manifest=manifest,
        run_id="RUN-study-001",
        error=RuntimeError("synthetic execution detail"),
    )
    assert replay == publication
    assert original_counts == (
        validate_governance_registry(manager)["row_count"],
        validate_knowledge_registry(manager)["row_count"],
    )
    with pytest.raises(ValueError, match="validation_decision_subject_conflict"):
        preserve_study_validation_failure(
            manager=manager,
            manifest=manifest,
            run_id="RUN-study-001",
            error=RuntimeError("different execution detail"),
        )


def test_validated_standard_state_remains_compatible_with_research_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest()
    admission = _freeze_and_admit(
        manager=manager,
        manifest=manifest,
        monkeypatch=monkeypatch,
    )
    report = _report(admission, result="PASS", content_hash=_hash("5"))
    complete_study_validation(
        manager=manager,
        manifest=manifest,
        run_id="RUN-study-001",
        report=report,
    )
    candidate = GovernanceSubject(
        GovernanceSubjectType.STRATEGY_CANDIDATE,
        "candidate-a",
        "1",
    )
    for source, target, evidence in (
        (None, "DRAFT", {}),
        ("DRAFT", "BACKTESTED", {"backtest_report_hash": _hash("1")}),
        ("BACKTESTED", "ROBUSTNESS_PASSED", {"stress_suite_hash": _hash("2")}),
        (
            "ROBUSTNESS_PASSED",
            "OUT_OF_SAMPLE_PASSED",
            {"final_holdout_confirmation_hash": _hash("3")},
        ),
    ):
        append_lifecycle_transition(
            manager=manager,
            subject=candidate,
            from_state=source,
            to_state=target,
            actor_id="researcher-a",
            reason=f"advance candidate to {target}",
            evidence_hashes=evidence,
            recorded_at="2026-01-03T00:00:00+00:00",
        )
    hypothesis = GovernanceSubject(
        GovernanceSubjectType.HYPOTHESIS,
        manifest.hypothesis_spec.hypothesis_id,
        manifest.hypothesis_spec.version,
    )
    approval = approve_strategy_candidate(
        manager=manager,
        subject=candidate,
        hypothesis_subject=hypothesis,
        hypothesis_contract_hash=manifest.hypothesis_spec.contract_hash(),
        strategy_name="noop_baseline",
        strategy_version="v1",
        strategy_plugin_contract_hash=_hash("a"),
        effective_strategy_parameters_hash=_hash("b"),
        source_report_hash=report["content_hash"],
        final_holdout_confirmation_hash=_hash("3"),
        reviewer_id="approver-a",
        rationale="independent review accepted the frozen research evidence",
        decided_at="2026-01-04T00:00:00+00:00",
    )

    assert validate_strategy_approval(
        approval,
        source_report_hash=report["content_hash"],
        selected_candidate_id="candidate-a",
        final_holdout_confirmation_hash=_hash("3"),
        hypothesis_id=manifest.hypothesis_spec.hypothesis_id,
        hypothesis_version=manifest.hypothesis_spec.version,
        hypothesis_contract_hash=manifest.hypothesis_spec.contract_hash(),
        strategy_name="noop_baseline",
        strategy_version="v1",
        strategy_plugin_contract_hash=_hash("a"),
        effective_strategy_parameters_hash=_hash("b"),
        expected_registry_path=governance_registry_path(manager),
        manager=manager,
    ) == []
    validated_row = next(
        row
        for row in load_governance_rows(governance_registry_path(manager))
        if row.get("subject_id") == hypothesis.subject_id
        and row.get("to_state") == "VALIDATED"
    )
    assert approval["hypothesis_supported_transition_row_hash"] == (
        validated_row["row_hash"]
    )


def test_posthoc_condition_requires_a_new_hypothesis_version_and_reference(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    original = parse_hypothesis_spec(hypothesis_spec_v2())
    same_version_payload = hypothesis_spec_v2()
    same_version_payload["observation_conditions"].append("post-hoc volatility regime")
    same_version = parse_hypothesis_spec(same_version_payload)

    with pytest.raises(StudyLifecycleError, match="new_version_required"):
        register_posthoc_followup(
            manager=manager,
            original=original,
            followup=same_version,
        )

    followup_payload = hypothesis_spec_v2(version="2.0.0")
    followup_payload["observation_conditions"].append("post-hoc volatility regime")
    question = followup_payload["research_question"]
    question["version"] = "2.0.0"
    followup_payload["research_question_ref"] = {
        "question_id": question["question_id"],
        "version": question["version"],
        "question_hash": sha256_prefixed(question),
    }
    followup = parse_hypothesis_spec(followup_payload)
    ref = register_posthoc_followup(
        manager=manager,
        original=original,
        followup=followup,
    )

    assert ref.logical_id == original.hypothesis_id
    assert ref.version == "2.0.0"
    assert get_knowledge_record(
        manager=manager,
        record_type="hypothesis",
        logical_id=original.hypothesis_id,
        version=original.version,
    )["record_hash"] == original.contract_hash()
    assert get_knowledge_record(
        manager=manager,
        record_type="hypothesis",
        logical_id=followup.hypothesis_id,
        version=followup.version,
    )["record_hash"] == followup.contract_hash()
    assert validate_knowledge_registry(manager)["status"] == "PASS"
