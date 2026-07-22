from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.governance import (
    GovernanceError,
    GovernanceSubject,
    GovernanceSubjectType,
    approve_strategy_candidate,
    append_lifecycle_transition,
    governance_registry_path,
    validate_governance_registry,
    validate_strategy_approval,
)
from market_research.research.hashing import content_hash_payload, sha256_prefixed
from market_research.research.knowledge_contract import authority_ref_from_dict
from market_research.research.knowledge_registry import (
    get_knowledge_record,
    knowledge_registry_path,
    validate_knowledge_registry,
    verify_decision_record,
)
from market_research.settings import ResearchSettings
from tests.independent_verification_fixture import (
    fixture_terminal_source_report,
    publish_pass_verification,
)


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
    if char == "5":
        return str(
            fixture_terminal_source_report(
                experiment_id="decision-record-fixture",
                manifest_hash="sha256:" + "e" * 64,
            )["content_hash"]
        )
    return "sha256:" + char * 64


def _verification_kwargs(manager: ResearchPathManager) -> dict[str, object]:
    result = publish_pass_verification(
        manager=manager,
        verification_id="candidate-a-verification",
        verifier_id="independent-verifier-a",
        experiment_id="decision-record-fixture",
        source_report_hash=_hash("5"),
        manifest_hash=_hash("e"),
    )
    return {
        "independent_verification_ref": result.ref(),
        "experiment_id": result.experiment_id,
        "research_version": result.research_version,
        "originator_actor_ids": frozenset({"researcher-a"}),
    }


def _defined_hypothesis(manager: ResearchPathManager) -> GovernanceSubject:
    subject = GovernanceSubject(GovernanceSubjectType.HYPOTHESIS, "edge", "1")
    append_lifecycle_transition(
        manager=manager,
        subject=subject,
        from_state=None,
        to_state="IDEA",
        actor_id="researcher-a",
        reason="idea recorded",
        evidence_hashes={"hypothesis_semantic_fingerprint": _hash("0")},
    )
    return subject


def _out_of_sample_candidate(manager: ResearchPathManager) -> GovernanceSubject:
    subject = GovernanceSubject(
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
            subject=subject,
            from_state=source,
            to_state=target,
            actor_id="researcher-a",
            reason=f"advance to {target}",
            evidence_hashes=evidence,
        )
    return subject


def _supported_hypothesis(manager: ResearchPathManager) -> GovernanceSubject:
    subject = _defined_hypothesis(manager)
    for source, target, evidence in (
        ("IDEA", "HYPOTHESIS_DEFINED", {"hypothesis_contract_hash": _hash("4")}),
        ("HYPOTHESIS_DEFINED", "EXPLORING", {}),
        ("EXPLORING", "VALIDATING", {"validation_manifest_hash": _hash("6")}),
        ("VALIDATING", "SUPPORTED", {"validation_report_hash": _hash("5")}),
    ):
        append_lifecycle_transition(
            manager=manager,
            subject=subject,
            from_state=source,
            to_state=target,
            actor_id="researcher-a",
            reason=f"advance to {target}",
            evidence_hashes=evidence,
        )
    return subject


def _approval(manager: ResearchPathManager, *, prohibited=frozenset()):
    candidate = _out_of_sample_candidate(manager)
    hypothesis = _supported_hypothesis(manager)
    approval = approve_strategy_candidate(
        manager=manager,
        subject=candidate,
        hypothesis_subject=hypothesis,
        hypothesis_contract_hash=_hash("4"),
        strategy_name="noop_baseline",
        strategy_version="v1",
        strategy_plugin_contract_hash=_hash("a"),
        effective_strategy_parameters_hash=_hash("b"),
        source_report_hash=_hash("5"),
        final_holdout_confirmation_hash=_hash("3"),
        reviewer_id="approver-a",
        rationale="independent evidence review passed",
        prohibited_actor_ids=prohibited,
        **_verification_kwargs(manager),
    )
    return candidate, hypothesis, approval


def test_designated_transition_materializes_complete_policy_decision(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    subject = _defined_hypothesis(manager)

    transition = append_lifecycle_transition(
        manager=manager,
        subject=subject,
        from_state="IDEA",
        to_state="HYPOTHESIS_DEFINED",
        actor_id="researcher-a",
        reason="the falsifiable hypothesis contract is complete",
        evidence_hashes={"hypothesis_contract_hash": _hash("4")},
    )

    assert transition["knowledge_registry_path"] == str(
        knowledge_registry_path(manager).resolve()
    )
    decision = get_knowledge_record(
        manager=manager,
        record_type="decision",
        logical_id=transition["decision_id"],
        version=transition["decision_version"],
    )
    payload = decision["payload"]
    assert payload["alternatives"]
    assert payload["expected_effects"]
    assert payload["risks"]
    assert payload["policy_version"] == "material-transition-policy.v1"
    assert payload["approver"]["approver_type"] == "policy"
    assert (
        verify_decision_record(
            manager=manager,
            decision_id=transition["decision_id"],
            version=transition["decision_version"],
            expected_subject=authority_ref_from_dict(payload["subject"]),
            expected_chosen_action="transition:IDEA->HYPOTHESIS_DEFINED",
            required_evidence_hashes=(_hash("4"),),
            expected_record_hash=transition["decision_record_hash"],
            expected_row_hash=transition["decision_registry_row_hash"],
        )
        == decision
    )
    assert validate_governance_registry(manager)["status"] == "PASS"


def test_concurrent_material_transition_reuses_decision_but_advances_once(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    subject = _defined_hypothesis(manager)
    barrier = Barrier(2)

    def advance() -> tuple[str, str | None]:
        barrier.wait(timeout=5)
        try:
            row = append_lifecycle_transition(
                manager=manager,
                subject=subject,
                from_state="IDEA",
                to_state="HYPOTHESIS_DEFINED",
                actor_id="researcher-a",
                reason="hypothesis contract completed",
                evidence_hashes={"hypothesis_contract_hash": _hash("4")},
            )
        except GovernanceError as exc:
            return str(exc), None
        return "success", str(row["decision_registry_row_hash"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: advance(), range(2)))

    assert [status for status, _row_hash in outcomes].count("success") == 1
    assert validate_knowledge_registry(manager)["row_count"] == 1
    assert validate_governance_registry(manager)["status"] == "PASS"


def test_semantically_valid_chain_without_required_decision_binding_rejects(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    subject = _defined_hypothesis(manager)
    append_lifecycle_transition(
        manager=manager,
        subject=subject,
        from_state="IDEA",
        to_state="HYPOTHESIS_DEFINED",
        actor_id="researcher-a",
        reason="hypothesis contract completed",
        evidence_hashes={"hypothesis_contract_hash": _hash("4")},
    )
    path = governance_registry_path(manager)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    material = rows[-1]
    for field in (
        "knowledge_registry_path",
        "decision_id",
        "decision_version",
        "decision_subject_hash",
        "decision_record_hash",
        "decision_registry_row_hash",
    ):
        material.pop(field)
    material.pop("row_hash")
    rows[-1] = {
        **material,
        "row_hash": sha256_prefixed(
            content_hash_payload(material),
            label="research_governance_row",
        ),
    }
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )

    validation = validate_governance_registry(manager)
    assert validation["status"] == "FAIL"
    assert any("material_transition" in reason for reason in validation["reasons"])


def test_human_approval_decision_is_idempotent_verified_and_retained_on_retirement(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    candidate, _hypothesis, approval = _approval(manager)
    replay = approve_strategy_candidate(
        manager=manager,
        subject=candidate,
        hypothesis_subject=GovernanceSubject(
            GovernanceSubjectType.HYPOTHESIS,
            "edge",
            "1",
        ),
        hypothesis_contract_hash=_hash("4"),
        strategy_name="noop_baseline",
        strategy_version="v1",
        strategy_plugin_contract_hash=_hash("a"),
        effective_strategy_parameters_hash=_hash("b"),
        source_report_hash=_hash("5"),
        final_holdout_confirmation_hash=_hash("3"),
        reviewer_id="approver-a",
        rationale="independent evidence review passed",
        **_verification_kwargs(manager),
    )
    assert replay == approval
    decision = get_knowledge_record(
        manager=manager,
        record_type="decision",
        logical_id=approval["decision_id"],
        version=approval["decision_version"],
    )
    assert decision["payload"]["approver"] == {
        "approver_type": "human",
        "approver_id": "approver-a",
        "role": "research_approver",
    }
    assert decision["payload"]["proposer_ids"] == ["researcher-a"]
    assert (
        validate_strategy_approval(
            approval,
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
            expected_registry_path=governance_registry_path(manager),
            manager=manager,
        )
        == []
    )

    append_lifecycle_transition(
        manager=manager,
        subject=candidate,
        from_state="RESEARCH_APPROVED",
        to_state="RETIRED",
        actor_id="approver-a",
        reason="the research edge is no longer considered sustainable",
    )
    retained = get_knowledge_record(
        manager=manager,
        record_type="decision",
        logical_id=approval["decision_id"],
        version=approval["decision_version"],
    )
    assert retained == decision
    assert validate_governance_registry(manager)["status"] == "PASS"


def test_approval_separation_rejects_before_human_decision_publication(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    candidate = _out_of_sample_candidate(manager)
    hypothesis = _supported_hypothesis(manager)
    before = validate_knowledge_registry(manager)["row_count"]

    with pytest.raises(GovernanceError, match="separation_of_duties"):
        approve_strategy_candidate(
            manager=manager,
            subject=candidate,
            hypothesis_subject=hypothesis,
            hypothesis_contract_hash=_hash("4"),
            strategy_name="noop_baseline",
            strategy_version="v1",
            strategy_plugin_contract_hash=_hash("a"),
            effective_strategy_parameters_hash=_hash("b"),
            source_report_hash=_hash("5"),
            final_holdout_confirmation_hash=_hash("3"),
            reviewer_id="approver-a",
            rationale="attempt self approval",
            prohibited_actor_ids=frozenset({"approver-a"}),
        )

    assert validate_knowledge_registry(manager)["row_count"] == before
