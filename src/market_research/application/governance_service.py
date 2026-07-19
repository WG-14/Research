"""UI-neutral application service for authoritative research governance."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from market_research.paths import ResearchPathManager
from market_research.research.experiment_registry import (
    experiment_registry_path,
    validate_experiment_registry_binding,
)
from market_research.research.final_selection import (
    validate_confirmation_artifact,
    validate_final_selection_report,
)
from market_research.research.governance import (
    GovernanceError,
    GovernanceSubject,
    GovernanceSubjectType,
    HumanReviewDecision,
    append_human_review,
    approve_strategy_candidate,
)
from market_research.research.hashing import (
    report_content_hash_payload,
    sha256_prefixed,
)
from market_research.research.validation_pipeline import (
    ValidationRunError,
    resolve_bound_selected_candidate,
    validate_validated_research_result,
)
from market_research.storage_io import write_json_atomic_create_or_verify

from .authorization import ensure_capability_authorized
from .contracts import (
    ActorContext,
    ArtifactReference,
    GovernanceSubjectRef,
    HumanReviewRequest,
    HumanReviewResult,
    ResultStatus,
    StrategyApprovalRequest,
    StrategyApprovalResult,
)


@dataclass(frozen=True, slots=True)
class _ApprovalEvidence:
    candidate_id: str
    hypothesis_id: str
    hypothesis_version: str
    hypothesis_contract_hash: str
    strategy_name: str
    strategy_version: str
    strategy_plugin_contract_hash: str
    effective_strategy_parameters_hash: str
    source_report_hash: str
    final_holdout_confirmation_hash: str


@dataclass(frozen=True, slots=True)
class ResearchGovernanceApplicationService:
    """Apply catalog authorization before calling canonical governance code."""

    paths: ResearchPathManager

    def record_review(self, request: HumanReviewRequest) -> HumanReviewResult:
        """Record CHANGES_REQUESTED or REJECTED through the common boundary."""

        ensure_capability_authorized("research-record-human-review", request.actor)
        actor = _required_actor(request.actor)
        _ensure_actor_is_not_prohibited(actor, request.prohibited_actor_ids)
        if request.decision == HumanReviewDecision.APPROVED.value:
            raise GovernanceError(
                "human_review_approved_requires_candidate_approval_service"
            )
        reviewer_role = _reviewer_role(actor)
        row = append_human_review(
            manager=self.paths,
            subject=_subject(request.subject),
            decision=HumanReviewDecision(request.decision),
            reviewer_id=actor.actor_id,
            reviewer_role=reviewer_role,
            rationale=request.rationale,
            reviewed_artifact_hash=request.reviewed_artifact_hash,
            requested_changes=tuple(
                change.model_dump() for change in request.requested_changes
            ),
            resolved_requirement_ids=request.resolved_requirement_ids,
            review_request_id=request.idempotency_key or request.request_id,
        )
        row_hash = str(row["row_hash"])
        return HumanReviewResult(
            capability_id="research-record-human-review",
            request_id=request.request_id,
            status=ResultStatus.SUCCEEDED,
            exit_code=0,
            content_hash=row_hash,
            subject=request.subject,
            decision=request.decision,
            reviewer_id=actor.actor_id,
            reviewer_role=reviewer_role,
            row_hash=row_hash,
            review=row,
        )

    def approve_candidate(
        self,
        request: StrategyApprovalRequest,
    ) -> StrategyApprovalResult:
        """Validate report evidence, approve once, and atomically write output."""

        ensure_capability_authorized(
            "research-approve-strategy-candidate",
            request.actor,
        )
        actor = _required_actor(request.actor)
        _ensure_actor_is_not_prohibited(actor, request.prohibited_actor_ids)
        if "research_approver" not in actor.roles:
            raise GovernanceError("strategy_approval_actor_role_required")

        report = self._load_and_validate_source_report(
            request.source_report_path,
            expected_source_report_hash=request.expected_source_report_hash,
        )
        evidence = self._extract_approval_evidence(report)
        requested_target = Path(request.output_path).expanduser()
        target = Path(os.path.abspath(requested_target))
        resolved_target = target.resolve(strict=False)
        if target.is_symlink() or resolved_target != target:
            raise GovernanceError("strategy_approval_output_path_must_not_use_symlink")
        if ResearchPathManager.is_within(resolved_target, self.paths.project_root):
            raise GovernanceError(
                "strategy_approval_output_must_be_repository_external"
            )

        subject_ref = GovernanceSubjectRef(
            subject_type="strategy_candidate",
            subject_id=evidence.candidate_id,
            subject_version=request.subject_version,
        )
        approval = approve_strategy_candidate(
            manager=self.paths,
            subject=_subject(subject_ref),
            hypothesis_subject=GovernanceSubject(
                GovernanceSubjectType.HYPOTHESIS,
                evidence.hypothesis_id,
                evidence.hypothesis_version,
            ),
            hypothesis_contract_hash=evidence.hypothesis_contract_hash,
            strategy_name=evidence.strategy_name,
            strategy_version=evidence.strategy_version,
            strategy_plugin_contract_hash=evidence.strategy_plugin_contract_hash,
            effective_strategy_parameters_hash=(
                evidence.effective_strategy_parameters_hash
            ),
            source_report_hash=evidence.source_report_hash,
            final_holdout_confirmation_hash=(evidence.final_holdout_confirmation_hash),
            reviewer_id=actor.actor_id,
            rationale=request.rationale,
            resolved_requirement_ids=request.resolved_requirement_ids,
            approval_request_id=request.idempotency_key,
            prohibited_actor_ids=request.prohibited_actor_ids,
        )
        write_json_atomic_create_or_verify(target, approval)
        content_hash = str(approval["content_hash"])
        return StrategyApprovalResult(
            capability_id="research-approve-strategy-candidate",
            request_id=request.request_id,
            status=ResultStatus.SUCCEEDED,
            exit_code=0,
            content_hash=content_hash,
            artifacts=(
                ArtifactReference(
                    kind="strategy_research_approval",
                    uri=str(target),
                    content_hash=content_hash,
                ),
            ),
            subject=subject_ref,
            reviewer_id=actor.actor_id,
            reviewer_role="research_approver",
            source_report_hash=evidence.source_report_hash,
            review_row_hash=str(approval["review_row_hash"]),
            transition_row_hash=str(approval["transition_row_hash"]),
            approval=approval,
        )

    def _load_and_validate_source_report(
        self,
        source_report_path: str,
        *,
        expected_source_report_hash: str | None,
    ) -> dict[str, Any]:
        report = json.loads(Path(source_report_path).read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise GovernanceError("strategy_approval_report_must_be_object")
        recorded_hash = str(report.get("content_hash") or "")
        actual_hash = sha256_prefixed(report_content_hash_payload(report))
        if recorded_hash != actual_hash:
            raise GovernanceError(
                "strategy_approval_source_report_content_hash_mismatch"
            )
        if (
            expected_source_report_hash is not None
            and recorded_hash != expected_source_report_hash
        ):
            raise GovernanceError(
                "strategy_approval_expected_source_report_hash_mismatch"
            )
        result_reasons = validate_validated_research_result(
            report,
            manager=self.paths,
        )
        if result_reasons:
            raise GovernanceError(
                "strategy_approval_validated_result_invalid:" + ",".join(result_reasons)
            )
        selection_reasons = validate_final_selection_report(report)
        if selection_reasons:
            raise GovernanceError(
                "strategy_approval_final_selection_invalid:"
                + ",".join(selection_reasons)
            )
        return report

    def _extract_approval_evidence(
        self,
        report: dict[str, Any],
    ) -> _ApprovalEvidence:
        candidate_id = str(report.get("selected_candidate_id") or "").strip()
        if not candidate_id:
            raise GovernanceError("strategy_approval_selected_candidate_missing")

        confirmation = report.get("final_holdout_confirmation")
        if not isinstance(confirmation, dict):
            raise GovernanceError(
                "strategy_approval_final_holdout_confirmation_missing"
            )
        confirmation_hash = str(confirmation.get("content_hash") or "")
        if not confirmation_hash:
            raise GovernanceError(
                "strategy_approval_final_holdout_confirmation_missing"
            )
        selection_artifact = report.get("selection_artifact")
        if not isinstance(selection_artifact, dict):
            raise GovernanceError("strategy_approval_selection_artifact_missing")
        confirmation_reasons = validate_confirmation_artifact(
            confirmation,
            selection_artifact=selection_artifact,
        )
        confirmation_reasons.extend(
            validate_experiment_registry_binding(
                report=confirmation,
                require_complete=True,
                expected_registry_path=experiment_registry_path(manager=self.paths),
            )
        )
        if confirmation.get("confirmation_gate_result") != "PASS":
            confirmation_reasons.append("final_holdout_confirmation_not_passed")
        if confirmation_reasons:
            raise GovernanceError(
                "strategy_approval_final_holdout_invalid:"
                + ",".join(sorted(set(confirmation_reasons)))
            )

        hypothesis_id = str(report.get("hypothesis_id") or "").strip()
        hypothesis_version = str(report.get("hypothesis_version") or "").strip()
        hypothesis_contract_hash = str(
            report.get("hypothesis_contract_hash") or ""
        ).strip()
        if not hypothesis_id or not hypothesis_version or not hypothesis_contract_hash:
            raise GovernanceError("strategy_approval_hypothesis_identity_missing")

        selected: dict[str, Any] | None
        if report.get("selected_candidate_binding_schema_version") == 1:
            try:
                selected = resolve_bound_selected_candidate(
                    report,
                    manager=self.paths,
                )
            except ValidationRunError as exc:
                raise GovernanceError(
                    f"strategy_approval_selected_candidate_artifact_invalid:{exc}"
                ) from exc
        else:
            candidates = [
                item
                for item in report.get("candidates") or []
                if isinstance(item, dict)
            ]
            selected = next(
                (
                    item
                    for item in candidates
                    if str(
                        item.get("parameter_candidate_id")
                        or item.get("candidate_id")
                        or ""
                    )
                    == candidate_id
                ),
                None,
            )
        compiled = (
            selected.get("compiled_strategy_contract")
            if isinstance(selected, dict)
            else None
        )
        if not isinstance(selected, dict) or not isinstance(compiled, dict):
            raise GovernanceError(
                "strategy_approval_selected_candidate_contract_missing"
            )
        return _ApprovalEvidence(
            candidate_id=candidate_id,
            hypothesis_id=hypothesis_id,
            hypothesis_version=hypothesis_version,
            hypothesis_contract_hash=hypothesis_contract_hash,
            strategy_name=str(compiled.get("strategy_name") or ""),
            strategy_version=str(compiled.get("strategy_version") or ""),
            strategy_plugin_contract_hash=str(
                selected.get("strategy_plugin_contract_hash") or ""
            ),
            effective_strategy_parameters_hash=str(
                selected.get("effective_strategy_parameters_hash") or ""
            ),
            source_report_hash=str(report["content_hash"]),
            final_holdout_confirmation_hash=confirmation_hash,
        )


def _required_actor(actor: ActorContext | None) -> ActorContext:
    if actor is None:
        raise GovernanceError("governance_actor_context_required")
    return actor


def _ensure_actor_is_not_prohibited(
    actor: ActorContext,
    prohibited_actor_ids: frozenset[str],
) -> None:
    if actor.actor_id in prohibited_actor_ids:
        raise GovernanceError("governance_separation_of_duties_violation")


def _reviewer_role(actor: ActorContext) -> str:
    for role in ("research_reviewer", "research_approver"):
        if role in actor.roles:
            return role
    if actor.roles:
        return actor.roles[0]
    raise GovernanceError("human_review_actor_role_required")


def _subject(subject: GovernanceSubjectRef) -> GovernanceSubject:
    return GovernanceSubject(
        GovernanceSubjectType(subject.subject_type),
        subject.subject_id,
        subject.subject_version,
    )
