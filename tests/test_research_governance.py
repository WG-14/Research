from __future__ import annotations

import json
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.hashing import content_hash_payload, sha256_prefixed
from market_research.research.governance import (
    GovernanceError,
    GovernanceSubject,
    GovernanceSubjectType,
    HumanReviewDecision,
    approve_strategy_candidate,
    append_human_review,
    append_lifecycle_transition,
    current_lifecycle_state,
    governance_registry_path,
    validate_governance_registry,
    validate_strategy_approval,
)
from market_research.settings import ResearchSettings


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


def test_hypothesis_and_strategy_lifecycles_are_independent_and_hash_chained(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    hypothesis = GovernanceSubject(GovernanceSubjectType.HYPOTHESIS, "edge", "1")
    strategy = GovernanceSubject(GovernanceSubjectType.STRATEGY_CANDIDATE, "candidate-a", "1")

    first = append_lifecycle_transition(
        manager=manager, subject=hypothesis, from_state=None, to_state="IDEA",
        actor_id="researcher-a", reason="captured from research backlog",
        evidence_hashes={"hypothesis_semantic_fingerprint": _hash("0")},
    )
    second = append_lifecycle_transition(
        manager=manager, subject=strategy, from_state=None, to_state="DRAFT",
        actor_id="researcher-a", reason="candidate implementation created",
    )

    assert second["prior_hash"] == first["row_hash"]
    assert current_lifecycle_state(manager=manager, subject=hypothesis) == "IDEA"
    assert current_lifecycle_state(manager=manager, subject=strategy) == "DRAFT"
    assert validate_governance_registry(manager)["status"] == "PASS"


def test_transition_rejects_skips_missing_evidence_and_stale_source_state(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    subject = GovernanceSubject(GovernanceSubjectType.STRATEGY_CANDIDATE, "candidate-a", "1")
    append_lifecycle_transition(
        manager=manager, subject=subject, from_state=None, to_state="DRAFT",
        actor_id="researcher-a", reason="candidate implementation created",
    )

    with pytest.raises(GovernanceError, match="transition_not_allowed"):
        append_lifecycle_transition(
            manager=manager, subject=subject, from_state="DRAFT", to_state="OUT_OF_SAMPLE_PASSED",
            actor_id="researcher-a", reason="skip stages", evidence_hashes={"final_holdout_confirmation_hash": _hash("a")},
        )
    with pytest.raises(GovernanceError, match="evidence_missing"):
        append_lifecycle_transition(
            manager=manager, subject=subject, from_state="DRAFT", to_state="BACKTESTED",
            actor_id="researcher-a", reason="backtest complete",
        )
    with pytest.raises(GovernanceError, match="state_conflict"):
        append_lifecycle_transition(
            manager=manager, subject=subject, from_state=None, to_state="DRAFT",
            actor_id="researcher-b", reason="stale duplicate initialization",
        )


def test_rejected_and_terminal_states_cannot_be_silently_reactivated(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    subject = GovernanceSubject(GovernanceSubjectType.HYPOTHESIS, "edge", "1")
    append_lifecycle_transition(
        manager=manager, subject=subject, from_state=None, to_state="IDEA",
        actor_id="researcher-a", reason="idea recorded",
        evidence_hashes={"hypothesis_semantic_fingerprint": _hash("0")},
    )
    append_lifecycle_transition(
        manager=manager, subject=subject, from_state="IDEA", to_state="HYPOTHESIS_DEFINED",
        actor_id="researcher-a", reason="contract completed",
        evidence_hashes={"hypothesis_contract_hash": _hash("b")},
    )
    append_lifecycle_transition(
        manager=manager, subject=subject, from_state="HYPOTHESIS_DEFINED", to_state="REJECTED",
        actor_id="reviewer-a", reason="mechanism contradicted by prior evidence",
    )
    append_lifecycle_transition(
        manager=manager, subject=subject, from_state="REJECTED", to_state="ARCHIVED",
        actor_id="reviewer-a", reason="retain rejected research evidence",
    )

    with pytest.raises(GovernanceError, match="transition_not_allowed"):
        append_lifecycle_transition(
            manager=manager, subject=subject, from_state="ARCHIVED", to_state="EXPLORING",
            actor_id="researcher-a", reason="silent reactivation",
        )


def test_registry_validation_detects_transition_mutation(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    subject = GovernanceSubject(GovernanceSubjectType.HYPOTHESIS, "edge", "1")
    append_lifecycle_transition(
        manager=manager, subject=subject, from_state=None, to_state="IDEA",
        actor_id="researcher-a", reason="idea recorded",
        evidence_hashes={"hypothesis_semantic_fingerprint": _hash("0")},
    )
    path = governance_registry_path(manager)
    row = json.loads(path.read_text(encoding="utf-8"))
    row["reason"] = "tampered"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    assert validate_governance_registry(manager)["status"] == "FAIL"


def test_same_semantic_hypothesis_cannot_be_registered_under_an_alias(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    fingerprint = _hash("9")
    first = GovernanceSubject(GovernanceSubjectType.HYPOTHESIS, "trend-edge", "1")
    alias = GovernanceSubject(GovernanceSubjectType.HYPOTHESIS, "momentum-alpha", "1")
    next_version = GovernanceSubject(GovernanceSubjectType.HYPOTHESIS, "trend-edge", "2")
    append_lifecycle_transition(
        manager=manager, subject=first, from_state=None, to_state="IDEA",
        actor_id="researcher-a", reason="register claim",
        evidence_hashes={"hypothesis_semantic_fingerprint": fingerprint},
    )
    with pytest.raises(GovernanceError, match="hypothesis_semantic_duplicate:trend-edge"):
        append_lifecycle_transition(
            manager=manager, subject=alias, from_state=None, to_state="IDEA",
            actor_id="researcher-b", reason="same claim under another label",
            evidence_hashes={"hypothesis_semantic_fingerprint": fingerprint},
        )
    append_lifecycle_transition(
        manager=manager, subject=next_version, from_state=None, to_state="IDEA",
        actor_id="researcher-a", reason="explicit new version of existing claim",
        evidence_hashes={"hypothesis_semantic_fingerprint": fingerprint},
    )


def _out_of_sample_candidate(manager: ResearchPathManager) -> GovernanceSubject:
    subject = GovernanceSubject(GovernanceSubjectType.STRATEGY_CANDIDATE, "candidate-a", "1")
    transitions = (
        (None, "DRAFT", {}),
        ("DRAFT", "BACKTESTED", {"backtest_report_hash": _hash("1")}),
        ("BACKTESTED", "ROBUSTNESS_PASSED", {"stress_suite_hash": _hash("2")}),
        ("ROBUSTNESS_PASSED", "OUT_OF_SAMPLE_PASSED", {"final_holdout_confirmation_hash": _hash("3")}),
    )
    for source, target, evidence in transitions:
        append_lifecycle_transition(
            manager=manager, subject=subject, from_state=source, to_state=target,
            actor_id="researcher-a", reason=f"advance to {target}", evidence_hashes=evidence,
        )
    return subject


def _supported_hypothesis(
    manager: ResearchPathManager, *, source_report_hash: str
) -> GovernanceSubject:
    subject = GovernanceSubject(GovernanceSubjectType.HYPOTHESIS, "edge", "1")
    for source, target, evidence in (
        (None, "IDEA", {"hypothesis_semantic_fingerprint": _hash("0")}),
        ("IDEA", "HYPOTHESIS_DEFINED", {"hypothesis_contract_hash": _hash("4")}),
        ("HYPOTHESIS_DEFINED", "EXPLORING", {}),
        ("EXPLORING", "VALIDATING", {"validation_manifest_hash": _hash("6")}),
        ("VALIDATING", "SUPPORTED", {"validation_report_hash": source_report_hash}),
    ):
        append_lifecycle_transition(
            manager=manager, subject=subject, from_state=source, to_state=target,
            actor_id="researcher-a", reason=f"advance hypothesis to {target}",
            evidence_hashes=evidence,
        )
    return subject


def test_changes_requested_must_be_resolved_before_human_approval(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    subject = _out_of_sample_candidate(manager)
    hypothesis = _supported_hypothesis(manager, source_report_hash=_hash("5"))
    append_human_review(
        manager=manager, subject=subject, decision=HumanReviewDecision.CHANGES_REQUESTED,
        reviewer_id="reviewer-a", reviewer_role="research_reviewer",
        rationale="economic mechanism needs a sensitivity explanation",
        reviewed_artifact_hash=_hash("4"),
        requested_changes=({
            "requirement_id": "REQ-1",
            "description": "explain sensitivity to the cost assumption",
            "verification_condition": "updated report binds a cost sensitivity artifact",
        },),
    )
    with pytest.raises(GovernanceError, match="unresolved_requirements"):
        approve_strategy_candidate(
            manager=manager, subject=subject, source_report_hash=_hash("5"),
            hypothesis_subject=hypothesis, hypothesis_contract_hash=_hash("4"),
            strategy_name="noop_baseline", strategy_version="v1",
            strategy_plugin_contract_hash=_hash("a"), effective_strategy_parameters_hash=_hash("b"),
            final_holdout_confirmation_hash=_hash("3"),
            reviewer_id="approver-a", rationale="reviewed updated evidence",
        )
    approval = approve_strategy_candidate(
        manager=manager, subject=subject, source_report_hash=_hash("5"),
        hypothesis_subject=hypothesis, hypothesis_contract_hash=_hash("4"),
        strategy_name="noop_baseline", strategy_version="v1",
        strategy_plugin_contract_hash=_hash("a"), effective_strategy_parameters_hash=_hash("b"),
        final_holdout_confirmation_hash=_hash("3"),
        reviewer_id="approver-a", rationale="cost sensitivity requirement resolved",
        resolved_requirement_ids=("REQ-1",),
    )
    assert validate_strategy_approval(
        approval, source_report_hash=_hash("5"), selected_candidate_id="candidate-a"
        , final_holdout_confirmation_hash=_hash("3")
        , hypothesis_id="edge", hypothesis_version="1", hypothesis_contract_hash=_hash("4")
        , strategy_name="noop_baseline", strategy_version="v1", strategy_plugin_contract_hash=_hash("a"), effective_strategy_parameters_hash=_hash("b")
    ) == []
    assert current_lifecycle_state(manager=manager, subject=subject) == "RESEARCH_APPROVED"


def test_approval_is_bound_to_report_candidate_and_current_state(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    subject = _out_of_sample_candidate(manager)
    hypothesis = _supported_hypothesis(manager, source_report_hash=_hash("5"))
    approval = approve_strategy_candidate(
        manager=manager, subject=subject, source_report_hash=_hash("5"),
        hypothesis_subject=hypothesis, hypothesis_contract_hash=_hash("4"),
        strategy_name="noop_baseline", strategy_version="v1",
        strategy_plugin_contract_hash=_hash("a"), effective_strategy_parameters_hash=_hash("b"),
        final_holdout_confirmation_hash=_hash("3"),
        reviewer_id="approver-a", rationale="economic and overfit review passed",
    )
    assert "strategy_approval_source_report_mismatch" in validate_strategy_approval(
        approval, source_report_hash=_hash("6"), selected_candidate_id="candidate-a"
        , final_holdout_confirmation_hash=_hash("3")
        , hypothesis_id="edge", hypothesis_version="1", hypothesis_contract_hash=_hash("4")
        , strategy_name="noop_baseline", strategy_version="v1", strategy_plugin_contract_hash=_hash("a"), effective_strategy_parameters_hash=_hash("b")
    )
    assert "strategy_approval_candidate_mismatch" in validate_strategy_approval(
        approval, source_report_hash=_hash("5"), selected_candidate_id="candidate-b"
        , final_holdout_confirmation_hash=_hash("3")
        , hypothesis_id="edge", hypothesis_version="1", hypothesis_contract_hash=_hash("4")
        , strategy_name="noop_baseline", strategy_version="v1", strategy_plugin_contract_hash=_hash("a"), effective_strategy_parameters_hash=_hash("b")
    )
    append_lifecycle_transition(
        manager=manager, subject=subject, from_state="RESEARCH_APPROVED", to_state="RETIRED",
        actor_id="approver-a", reason="research edge no longer considered sustainable",
    )
    assert "strategy_approval_not_current" in validate_strategy_approval(
        approval, source_report_hash=_hash("5"), selected_candidate_id="candidate-a"
        , final_holdout_confirmation_hash=_hash("3")
        , hypothesis_id="edge", hypothesis_version="1", hypothesis_contract_hash=_hash("4")
        , strategy_name="noop_baseline", strategy_version="v1", strategy_plugin_contract_hash=_hash("a"), effective_strategy_parameters_hash=_hash("b")
    )


def test_approval_rejects_copied_noncanonical_governance_registry(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    subject = _out_of_sample_candidate(manager)
    hypothesis = _supported_hypothesis(manager, source_report_hash=_hash("5"))
    approval = approve_strategy_candidate(
        manager=manager,
        subject=subject,
        source_report_hash=_hash("5"),
        hypothesis_subject=hypothesis,
        hypothesis_contract_hash=_hash("4"),
        strategy_name="noop_baseline",
        strategy_version="v1",
        strategy_plugin_contract_hash=_hash("a"),
        effective_strategy_parameters_hash=_hash("b"),
        final_holdout_confirmation_hash=_hash("3"),
        reviewer_id="approver-a",
        rationale="economic and overfit review passed",
    )
    canonical_path = governance_registry_path(manager)
    copied_path = tmp_path / "copied-governance.jsonl"
    copied_path.write_bytes(canonical_path.read_bytes())
    forged = {**approval, "governance_registry_path": str(copied_path.resolve())}
    forged_material = {
        key: value for key, value in forged.items() if key != "content_hash"
    }
    forged["content_hash"] = sha256_prefixed(
        content_hash_payload(forged_material)
    )

    reasons = validate_strategy_approval(
        forged,
        source_report_hash=_hash("5"),
        selected_candidate_id="candidate-a",
        final_holdout_confirmation_hash=_hash("3"),
        hypothesis_id="edge",
        hypothesis_version="1",
        hypothesis_contract_hash=_hash("4"),
        strategy_name="noop_baseline",
        strategy_version="v1",
        strategy_plugin_contract_hash=_hash("a"),
        effective_strategy_parameters_hash=_hash("b"),
        expected_registry_path=canonical_path,
    )

    assert reasons == ["strategy_approval_registry_path_mismatch"]


def test_generic_transition_api_cannot_self_approve_candidate(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    subject = _out_of_sample_candidate(manager)
    with pytest.raises(GovernanceError, match="requires_approval_service"):
        append_lifecycle_transition(
            manager=manager, subject=subject, from_state="OUT_OF_SAMPLE_PASSED",
            to_state="RESEARCH_APPROVED", actor_id="researcher-a", reason="self approval",
            evidence_hashes={"human_review_hash": _hash("8"), "source_report_hash": _hash("5")},
        )


def test_approval_rejects_holdout_hash_not_bound_to_oos_transition(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    subject = _out_of_sample_candidate(manager)
    hypothesis = _supported_hypothesis(manager, source_report_hash=_hash("5"))
    with pytest.raises(GovernanceError, match="holdout_evidence_mismatch"):
        approve_strategy_candidate(
            manager=manager, subject=subject, source_report_hash=_hash("5"),
            hypothesis_subject=hypothesis, hypothesis_contract_hash=_hash("4"),
            strategy_name="noop_baseline", strategy_version="v1",
            strategy_plugin_contract_hash=_hash("a"), effective_strategy_parameters_hash=_hash("b"),
            final_holdout_confirmation_hash=_hash("7"), reviewer_id="approver-a",
            rationale="attempt approval with unrelated holdout evidence",
        )


def test_strategy_approval_requires_supported_hypothesis(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    subject = _out_of_sample_candidate(manager)
    hypothesis = GovernanceSubject(GovernanceSubjectType.HYPOTHESIS, "edge", "1")
    append_lifecycle_transition(
        manager=manager, subject=hypothesis, from_state=None, to_state="IDEA",
        actor_id="researcher-a", reason="idea registered",
        evidence_hashes={"hypothesis_semantic_fingerprint": _hash("0")},
    )
    with pytest.raises(GovernanceError, match="requires_supported_hypothesis"):
        approve_strategy_candidate(
            manager=manager, subject=subject, hypothesis_subject=hypothesis,
            hypothesis_contract_hash=_hash("4"), source_report_hash=_hash("5"),
            strategy_name="noop_baseline", strategy_version="v1",
            strategy_plugin_contract_hash=_hash("a"), effective_strategy_parameters_hash=_hash("b"),
            final_holdout_confirmation_hash=_hash("3"), reviewer_id="approver-a",
            rationale="attempt premature approval",
        )
