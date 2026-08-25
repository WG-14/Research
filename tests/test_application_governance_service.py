from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import market_research.application.governance_service as service_module
from market_research.application import (
    ActorContext,
    ApplicationAuthorizationError,
    GovernanceSubjectRef,
    HumanReviewRequest,
    HumanReviewResult,
    IndependentVerificationReference,
    RequestedChange,
    ResearchGovernanceApplicationService,
    StrategyApprovalRequest,
    StrategyApprovalResult,
    get_capability,
)
from market_research.application.governance_authorization import (
    APPROVE_CANDIDATE_ACTION,
    RECORD_REVIEW_ACTION,
    _authenticated_web_governance_context,
)
from market_research.paths import ResearchPathManager
from market_research.research import cli
from market_research.research.experiment_registry import (
    FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION,
    append_attempt_completion,
    reserve_research_attempt,
)
from market_research.research.final_selection import (
    FINAL_HOLDOUT_RESULT_HASH_SCHEMA_VERSION,
)
from market_research.research.governance import (
    GovernanceError,
    GovernanceSubject,
    GovernanceSubjectType,
    append_lifecycle_transition,
    governance_registry_path,
)
from market_research.research.hashing import (
    report_content_hash_payload,
    sha256_prefixed,
)
from tests.independent_verification_fixture import publish_pass_verification
from tests.test_run_lifecycle import _context
from tests.test_strategy_research_package import (
    _bind_selected_candidate_artifact,
    _bind_validation_admission,
    _bind_validation_experiment,
    _publish_terminal_usage_binding,
    _result,
)


def _actor(
    actor_id: str,
    *,
    role: str,
    permission: str,
) -> ActorContext:
    return ActorContext(
        actor_id=actor_id,
        roles=(role,),
        permissions=frozenset({permission}),
        source="web",
    )


def _review_request(actor: ActorContext) -> HumanReviewRequest:
    return HumanReviewRequest(
        subject=GovernanceSubjectRef(
            subject_type="strategy_candidate",
            subject_id="candidate-1",
            subject_version="1",
        ),
        decision="CHANGES_REQUESTED",
        actor=actor,
        rationale="economic mechanism evidence is incomplete",
        reviewed_artifact_hash="sha256:" + "a" * 64,
        requested_changes=(
            RequestedChange(
                requirement_id="REQ-1",
                description="explain the economic mechanism",
                verification_condition="reviewed report contains mechanism evidence",
            ),
        ),
    )


def _record_review(
    service: ResearchGovernanceApplicationService,
    request: HumanReviewRequest,
):
    actor = request.actor
    assert actor is not None
    with _authenticated_web_governance_context(
        action=RECORD_REVIEW_ACTION,
        session_subject=actor.actor_id,
        actor=actor,
        request=request,
    ):
        return service.record_review(request)


def _approve_candidate(
    service: ResearchGovernanceApplicationService,
    request: StrategyApprovalRequest,
):
    actor = request.actor
    assert actor is not None
    with _authenticated_web_governance_context(
        action=APPROVE_CANDIDATE_ACTION,
        session_subject=actor.actor_id,
        actor=actor,
        request=request,
    ):
        return service.approve_candidate(request)


def _isolate_approval_commit_mechanics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep transaction tests separate from terminal-result construction.

    The synthetic ``_result`` fixture predates the manifest-native validation
    and immutable pre-holdout gate contracts.  A dedicated negative test below
    proves that production rejects it; tests using this helper exercise only
    approval commit, idempotency, and projection behavior.
    """

    monkeypatch.setattr(
        service_module,
        "validate_final_selection_report",
        lambda report: [],
    )
    monkeypatch.setattr(
        service_module,
        "validate_validated_research_result",
        lambda report, *, manager=None: [],
    )


def _prepare_reviewable_candidate(manager: ResearchPathManager) -> None:
    subject = GovernanceSubject(
        GovernanceSubjectType.STRATEGY_CANDIDATE,
        "candidate-1",
        "1",
    )
    for source, target, evidence in (
        (None, "DRAFT", {}),
        (
            "DRAFT",
            "BACKTESTED",
            {"backtest_report_hash": "sha256:" + "1" * 64},
        ),
        (
            "BACKTESTED",
            "ROBUSTNESS_PASSED",
            {"stress_suite_hash": "sha256:" + "2" * 64},
        ),
        (
            "ROBUSTNESS_PASSED",
            "OUT_OF_SAMPLE_PASSED",
            {"final_holdout_confirmation_hash": "sha256:" + "3" * 64},
        ),
    ):
        append_lifecycle_transition(
            manager=manager,
            subject=subject,
            from_state=source,
            to_state=target,
            actor_id="researcher-a",
            reason=f"advance candidate to {target}",
            evidence_hashes=evidence,
        )


def _prepare_approval_report(
    tmp_path: Path,
) -> tuple[object, dict[str, object], IndependentVerificationReference]:
    context = _context(tmp_path)
    manager = context.paths
    report = _result()
    _bind_validation_admission(report, manager)
    confirmation = report["final_holdout_confirmation"]
    selection_artifact = report["selection_artifact"]
    reservation = reserve_research_attempt(
        manager=manager,
        base_payload={
            "experiment_id": "application-governance-fixture",
            "experiment_family_id": "edge-family",
            "hypothesis_id": "edge",
            "manifest_hash": confirmation["manifest_hash"],
            "selection_artifact_hash": selection_artifact["content_hash"],
            "selected_candidate_id": "candidate-1",
            "final_holdout_content_pending_until_completion": True,
        },
    )
    completion = append_attempt_completion(
        manager=manager,
        reservation=reservation,
        updates={
            "dataset_artifact_evidence_hash": "sha256:" + "a" * 64,
            "final_holdout_query_hash": "sha256:" + "b" * 64,
            "final_holdout_data_hash": "sha256:" + "c" * 64,
            "final_holdout_fingerprint_hash": "sha256:" + "d" * 64,
            "final_holdout_quality_hash": "sha256:" + "e" * 64,
            "final_holdout_reuse_key_hash": "sha256:" + "f" * 64,
            "final_holdout_reuse_key_schema_version": (
                FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION
            ),
            "selection_artifact_hash": selection_artifact["content_hash"],
            "selected_candidate_id": "candidate-1",
            "candidate_count": 1,
            "confirmation_gate_result": "PASS",
            "final_holdout_result_hash_schema_version": (
                FINAL_HOLDOUT_RESULT_HASH_SCHEMA_VERSION
            ),
            "final_holdout_result_hash": confirmation["final_holdout_result_hash"],
        },
    )
    confirmation.update(
        {
            "experiment_registry_path": reservation["path"],
            "experiment_registry_prior_hash": reservation["prior_hash"],
            "experiment_registry_row_hash": reservation["row_hash"],
            "experiment_registry_completion_row_hash": completion["row_hash"],
            "authorization_row_hash": reservation["row_hash"],
            "completion_row_hash": completion["row_hash"],
        }
    )
    confirmation_material = {
        key: value
        for key, value in confirmation.items()
        if key not in {"content_hash", "confirmation_artifact_path"}
    }
    confirmation["content_hash"] = sha256_prefixed(
        confirmation_material,
        label="final_holdout_confirmation",
    )
    _bind_selected_candidate_artifact(report, manager)
    _bind_validation_experiment(report)
    report.update(
        {
            "reproduction_receipt_status": "AVAILABLE",
            "reproduction_receipt_scope": "validated_research_result",
        }
    )
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))

    candidate = GovernanceSubject(
        GovernanceSubjectType.STRATEGY_CANDIDATE,
        "candidate-1",
        "1",
    )
    for source, target, evidence in (
        (None, "DRAFT", {}),
        (
            "DRAFT",
            "BACKTESTED",
            {"backtest_report_hash": "sha256:" + "1" * 64},
        ),
        (
            "BACKTESTED",
            "ROBUSTNESS_PASSED",
            {"stress_suite_hash": "sha256:" + "2" * 64},
        ),
        (
            "ROBUSTNESS_PASSED",
            "OUT_OF_SAMPLE_PASSED",
            {"final_holdout_confirmation_hash": confirmation["content_hash"]},
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
        )

    hypothesis = GovernanceSubject(
        GovernanceSubjectType.HYPOTHESIS,
        "edge",
        "1",
    )
    for source, target, evidence in (
        (
            None,
            "IDEA",
            {"hypothesis_semantic_fingerprint": "sha256:" + "0" * 64},
        ),
        (
            "IDEA",
            "HYPOTHESIS_DEFINED",
            {"hypothesis_contract_hash": report["hypothesis_contract_hash"]},
        ),
        ("HYPOTHESIS_DEFINED", "EXPLORING", {}),
        (
            "EXPLORING",
            "VALIDATING",
            {"validation_manifest_hash": "sha256:" + "6" * 64},
        ),
        (
            "VALIDATING",
            "SUPPORTED",
            {"validation_report_hash": report["content_hash"]},
        ),
    ):
        append_lifecycle_transition(
            manager=manager,
            subject=hypothesis,
            from_state=source,
            to_state=target,
            actor_id="researcher-a",
            reason=f"advance hypothesis to {target}",
            evidence_hashes=evidence,
        )
    verification_result = publish_pass_verification(
        manager=manager,
        verification_id="application-governance-verification",
        verifier_id="independent-verifier-a",
        experiment_id=str(report["experiment_id"]),
        source_report_hash=str(report["content_hash"]),
        manifest_hash=str(report["manifest_hash"]),
        source_report=report,
    )
    report.update(
        {
            "reproduction_receipt_path": verification_result.baseline_receipt_path,
            "reproduction_receipt_hash": verification_result.baseline_receipt_hash,
        }
    )
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))
    _publish_terminal_usage_binding(report, manager)
    ref = verification_result.ref()
    return (
        context,
        report,
        IndependentVerificationReference(
            verification_id=ref.verification_id,
            version=ref.version,
            content_hash=ref.content_hash,
        ),
    )


def _approval_request(
    *,
    report_path: Path,
    output_path: Path,
    idempotency_key: str | None = None,
    independent_verification: IndependentVerificationReference | None = None,
) -> StrategyApprovalRequest:
    return StrategyApprovalRequest(
        source_report_path=str(report_path),
        subject_version="1",
        actor=_actor(
            "approver-a",
            role="research_approver",
            permission="research.approve",
        ),
        rationale="human research review passed",
        output_path=str(output_path),
        idempotency_key=idempotency_key,
        independent_verification=(
            independent_verification
            or IndependentVerificationReference(
                verification_id="unresolved-verification",
                version="1",
                content_hash="sha256:" + "0" * 64,
            )
        ),
        originator_actor_ids=frozenset({"researcher-a"}),
        prohibited_actor_ids=frozenset({"researcher-a"}),
    )


def test_governance_capabilities_use_concrete_service_contracts() -> None:
    review = get_capability("research-record-human-review")
    approval = get_capability("research-approve-strategy-candidate")

    assert review.request_model is HumanReviewRequest
    assert review.result_model is HumanReviewResult
    assert review.service_id == "ResearchGovernanceApplicationService.record_review"
    assert approval.request_model is StrategyApprovalRequest
    assert approval.result_model is StrategyApprovalResult
    assert (
        approval.service_id == "ResearchGovernanceApplicationService.approve_candidate"
    )


def test_record_review_enforces_permission_before_append(tmp_path: Path) -> None:
    context = _context(tmp_path)
    service = ResearchGovernanceApplicationService(context.paths)
    actor = _actor(
        "reviewer-a",
        role="research_reviewer",
        permission="research.view",
    )

    with pytest.raises(ApplicationAuthorizationError):
        service.record_review(_review_request(actor))

    assert not (tmp_path / "artifacts").exists()


def test_governance_service_requires_exact_one_shot_web_session_capability(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    service = ResearchGovernanceApplicationService(context.paths)
    actor = _actor(
        "reviewer-a",
        role="research_reviewer",
        permission="research.review.record",
    )
    request = _review_request(actor)

    with pytest.raises(
        GovernanceError,
        match="authenticated_web_governance_capability_required",
    ):
        service.record_review(request)

    changed = request.model_copy(update={"rationale": "different reviewed intent"})
    with _authenticated_web_governance_context(
        action=RECORD_REVIEW_ACTION,
        session_subject=actor.actor_id,
        actor=actor,
        request=request,
    ):
        with pytest.raises(
            GovernanceError,
            match="web_governance_capability_request_mismatch",
        ):
            service.record_review(changed)

    with _authenticated_web_governance_context(
        action=RECORD_REVIEW_ACTION,
        session_subject=actor.actor_id,
        actor=actor,
        request=request,
    ):
        with pytest.raises(GovernanceError, match="subject_lifecycle_missing"):
            service.record_review(request)
        with pytest.raises(
            GovernanceError,
            match="web_governance_capability_replayed",
        ):
            service.record_review(request)

    assert not governance_registry_path(context.paths).exists()


def test_governance_service_rejects_cli_and_wildcard_actor_claims(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    service = ResearchGovernanceApplicationService(context.paths)
    cli_actor = ActorContext(
        actor_id="forged-cli-reviewer",
        roles=("research_reviewer",),
        permissions=frozenset({"research.review.record"}),
        source="cli",
    )
    wildcard_actor = ActorContext(
        actor_id="forged-wildcard-reviewer",
        roles=("research_reviewer",),
        permissions=frozenset({"*", "research.review.record"}),
        source="web",
    )

    with pytest.raises(
        GovernanceError,
        match="authenticated_web_governance_actor_required",
    ):
        service.record_review(_review_request(cli_actor))
    with pytest.raises(
        GovernanceError,
        match="governance_wildcard_permission_forbidden",
    ):
        service.record_review(_review_request(wildcard_actor))

    assert not governance_registry_path(context.paths).exists()


def test_record_review_derives_actor_identity_and_rejects_self_action(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    service = ResearchGovernanceApplicationService(context.paths)
    actor = _actor(
        "reviewer-a",
        role="research_reviewer",
        permission="research.review.record",
    )

    request = _review_request(actor).model_copy(
        update={"idempotency_key": "review-request-1"}
    )
    with pytest.raises(GovernanceError, match="subject_lifecycle_missing"):
        _record_review(service, request)

    _prepare_reviewable_candidate(context.paths)
    result = _record_review(service, request)
    replay = _record_review(service, request)

    assert result.reviewer_id == "reviewer-a"
    assert result.reviewer_role == "research_reviewer"
    assert result.review["reviewer_id"] == actor.actor_id
    assert result.review["reviewer_role"] == actor.roles[0]
    assert result.review["review_request_id"] == "review-request-1"
    assert replay.row_hash == result.row_hash

    forged_payload = _review_request(actor).model_dump()
    forged_payload["reviewer_id"] = "forged-reviewer"
    with pytest.raises(ValidationError):
        HumanReviewRequest.model_validate(forged_payload)

    prohibited = _review_request(actor).model_copy(
        update={"prohibited_actor_ids": frozenset({actor.actor_id})}
    )
    with pytest.raises(
        GovernanceError,
        match="governance_separation_of_duties_violation",
    ):
        _record_review(service, prohibited)


def test_record_review_rejects_approved_decision_outside_approval_service(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    actor = _actor(
        "approver-a",
        role="research_approver",
        permission="research.review.record",
    )
    request = HumanReviewRequest(
        subject=GovernanceSubjectRef(
            subject_type="strategy_candidate",
            subject_id="candidate-1",
            subject_version="1",
        ),
        decision="APPROVED",
        actor=actor,
        rationale="review passed",
        reviewed_artifact_hash="sha256:" + "a" * 64,
    )

    with pytest.raises(
        GovernanceError,
        match="human_review_approved_requires_candidate_approval_service",
    ):
        _record_review(ResearchGovernanceApplicationService(context.paths), request)


def test_approval_rejects_invalid_source_report_before_writing(tmp_path: Path) -> None:
    context = _context(tmp_path)
    report = _result()
    report["selected_candidate_id"] = "tampered"
    report_path = tmp_path / "stale-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    output_path = tmp_path / "approval.json"

    with pytest.raises(
        GovernanceError,
        match="strategy_approval_source_report_content_hash_mismatch",
    ):
        _approve_candidate(
            ResearchGovernanceApplicationService(context.paths),
            _approval_request(report_path=report_path, output_path=output_path)
        )

    assert not output_path.exists()


def test_approval_enforces_its_own_permission_before_report_access(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    request = StrategyApprovalRequest(
        source_report_path=str(tmp_path / "does-not-exist.json"),
        subject_version="1",
        actor=_actor(
            "approver-a",
            role="research_approver",
            permission="research.review.record",
        ),
        rationale="review passed",
        output_path=str(tmp_path / "approval.json"),
        independent_verification=IndependentVerificationReference(
            verification_id="unresolved-verification",
            version="1",
            content_hash="sha256:" + "0" * 64,
        ),
        originator_actor_ids=frozenset({"researcher-a"}),
        prohibited_actor_ids=frozenset({"researcher-a"}),
    )

    with pytest.raises(ApplicationAuthorizationError):
        ResearchGovernanceApplicationService(context.paths).approve_candidate(request)


def test_approval_rejects_report_without_native_and_pre_holdout_authorities(
    tmp_path: Path,
) -> None:
    context, report, verification = _prepare_approval_report(tmp_path)
    report_path = tmp_path / "legacy-synthetic-report.json"
    output_path = tmp_path / "approval.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(
        GovernanceError,
        match=(
            "strategy_approval_validated_result_invalid:.*"
            "validated_research_result_native_validation_authority_missing"
        ),
    ):
        _approve_candidate(
            ResearchGovernanceApplicationService(context.paths),
            _approval_request(
                report_path=report_path,
                output_path=output_path,
                independent_verification=verification,
            ),
        )

    assert not output_path.exists()


def test_identical_approval_replay_materializes_same_artifact_and_changed_intent_rejects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_approval_commit_mechanics(monkeypatch)
    context, report, verification = _prepare_approval_report(tmp_path)
    report_path = tmp_path / "validated-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    first_output = tmp_path / "approval-1.json"
    request = _approval_request(
        report_path=report_path,
        output_path=first_output,
        independent_verification=verification,
    )
    service = ResearchGovernanceApplicationService(context.paths)

    result = _approve_candidate(service, request)

    assert first_output.exists()
    assert json.loads(first_output.read_text(encoding="utf-8")) == result.approval
    assert result.reviewer_id == request.actor.actor_id
    assert result.approval["reviewer_id"] == request.actor.actor_id
    second_output = tmp_path / "approval-2.json"
    replay = _approve_candidate(
        service,
        request.model_copy(update={"output_path": str(second_output)}),
    )
    assert replay.approval == result.approval
    assert json.loads(second_output.read_text(encoding="utf-8")) == result.approval

    with pytest.raises(
        GovernanceError,
        match="strategy_approval_requires_out_of_sample_passed",
    ):
        _approve_candidate(
            service,
            request.model_copy(
                update={
                    "rationale": "materially different approval intent",
                    "output_path": str(tmp_path / "approval-3.json"),
                }
            ),
        )


def test_approval_commit_survives_projection_failure_and_exact_replay_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_approval_commit_mechanics(monkeypatch)
    context, report, verification = _prepare_approval_report(tmp_path)
    report_path = tmp_path / "validated-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    output_path = tmp_path / "approval.json"
    request = _approval_request(
        report_path=report_path,
        output_path=output_path,
        idempotency_key="recoverable-approval-request",
        independent_verification=verification,
    )
    service = ResearchGovernanceApplicationService(context.paths)
    real_publish = service_module.write_json_atomic_create_or_verify

    def fail_publish(*_args, **_kwargs):
        raise OSError("injected_projection_failure")

    monkeypatch.setattr(
        service_module,
        "write_json_atomic_create_or_verify",
        fail_publish,
    )
    with pytest.raises(OSError, match="injected_projection_failure"):
        _approve_candidate(service, request)
    assert not output_path.exists()
    rows_after_failure = (
        governance_registry_path(context.paths).read_text(encoding="utf-8").splitlines()
    )
    assert sum('"decision":"APPROVED"' in row for row in rows_after_failure) == 1
    assert (
        sum('"to_state":"RESEARCH_APPROVED"' in row for row in rows_after_failure) == 1
    )

    monkeypatch.setattr(
        service_module,
        "write_json_atomic_create_or_verify",
        real_publish,
    )
    recovered = _approve_candidate(service, request)

    assert json.loads(output_path.read_text(encoding="utf-8")) == recovered.approval
    assert (
        governance_registry_path(context.paths).read_text(encoding="utf-8").splitlines()
        == rows_after_failure
    )


def test_approval_explicit_key_conflict_and_projection_no_clobber(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_approval_commit_mechanics(monkeypatch)
    context, report, verification = _prepare_approval_report(tmp_path)
    report_path = tmp_path / "validated-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    output_path = tmp_path / "approval.json"
    output_path.write_text('{"unrelated":true}\n', encoding="utf-8")
    request = _approval_request(
        report_path=report_path,
        output_path=output_path,
        idempotency_key="fixed-request-key",
        independent_verification=verification,
    )
    service = ResearchGovernanceApplicationService(context.paths)

    with pytest.raises(ValueError, match="atomic_json_target_conflict"):
        _approve_candidate(service, request)
    assert output_path.read_text(encoding="utf-8") == '{"unrelated":true}\n'
    with pytest.raises(GovernanceError, match="idempotency_conflict"):
        _approve_candidate(
            service,
            request.model_copy(update={"rationale": "different intent"}),
        )


def test_approval_projection_rejects_symlink_path_before_governance_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_approval_commit_mechanics(monkeypatch)
    context, report, verification = _prepare_approval_report(tmp_path)
    report_path = tmp_path / "validated-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    real_target = tmp_path / "real-approval.json"
    real_target.write_text("sentinel\n", encoding="utf-8")
    link_target = tmp_path / "approval-link.json"
    link_target.symlink_to(real_target)

    with pytest.raises(GovernanceError, match="output_path_must_not_use_symlink"):
        _approve_candidate(
            ResearchGovernanceApplicationService(context.paths),
            _approval_request(
                report_path=report_path,
                output_path=link_target,
                independent_verification=verification,
            ),
        )

    rows = (
        governance_registry_path(context.paths).read_text(encoding="utf-8").splitlines()
    )
    assert not any('"decision":"APPROVED"' in row for row in rows)
    assert real_target.read_text(encoding="utf-8") == "sentinel\n"


def test_cli_approval_adapter_is_fail_closed_even_for_valid_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "market_research.application.governance_service.validate_final_selection_report",
        lambda report: [],
    )
    context, report, verification = _prepare_approval_report(tmp_path)
    output: list[str] = []
    context.printer = output.append
    report_path = tmp_path / "validated-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    approval_path = tmp_path / "approval.json"
    registry_before = governance_registry_path(context.paths).read_bytes()

    rc = cli.cmd_research_approve_strategy_candidate(
        context=context,
        result_path=str(report_path),
        subject_version="1",
        reviewer_id="approver-a",
        rationale="human research review passed",
        resolved_requirement_ids=(),
        verification_id=verification.verification_id,
        verification_version=verification.version,
        verification_hash=verification.content_hash,
        originator_ids=("researcher-a",),
        out_path=str(approval_path),
    )

    assert rc == 1
    assert not approval_path.exists()
    assert governance_registry_path(context.paths).read_bytes() == registry_before
    assert output == [
        "[RESEARCH-GOVERNANCE-CLI-DISABLED] "
        "command=research-approve-strategy-candidate "
        "error=authenticated_internal_web_governance_required"
    ]


def test_cli_approval_rejects_verifier_declared_as_originator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "market_research.application.governance_service.validate_final_selection_report",
        lambda report: [],
    )
    context, report, verification = _prepare_approval_report(tmp_path)
    output: list[str] = []
    context.printer = output.append
    report_path = tmp_path / "validated-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    approval_path = tmp_path / "approval.json"

    rc = cli.cmd_research_approve_strategy_candidate(
        context=context,
        result_path=str(report_path),
        subject_version="1",
        reviewer_id="approver-a",
        rationale="must reject an originator acting as verifier",
        resolved_requirement_ids=(),
        verification_id=verification.verification_id,
        verification_version=verification.version,
        verification_hash=verification.content_hash,
        originator_ids=("independent-verifier-a",),
        out_path=str(approval_path),
    )

    assert rc == 1
    assert not approval_path.exists()
    assert output == [
        "[RESEARCH-GOVERNANCE-CLI-DISABLED] "
        "command=research-approve-strategy-candidate "
        "error=authenticated_internal_web_governance_required"
    ]
