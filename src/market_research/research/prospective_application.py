"""Application service joining prospective evidence to research governance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from market_research.paths import ResearchPathManager

from .governance import (
    GovernanceSubject,
    GovernanceSubjectType,
    HypothesisLifecycleState,
    append_lifecycle_transition,
    current_lifecycle_state,
)
from .prospective_validation import (
    ImmutableEvidenceRef,
    ProspectiveObservation,
    ProspectiveEvaluation,
    ProspectiveValidationError,
    ProspectiveValidationSpec,
    ResearchConclusion,
    build_research_conclusion,
    evaluate_prospective_validation,
    publish_prospective_spec,
    publish_research_conclusion,
    record_prospective_observation,
    verify_published_prospective_conclusion,
)
from .research_package_registry import (
    ResearchPackageManifest,
    ResearchPackageRegistry,
    build_research_package_manifest,
)


@dataclass(frozen=True, slots=True)
class ProspectiveValidationApplicationService:
    """The one state-aware path for an offline prospective research study."""

    paths: ResearchPathManager

    def start(
        self,
        *,
        spec: ProspectiveValidationSpec,
        actor_id: str,
        reason: str,
        recorded_at: str,
    ) -> dict[str, Any]:
        subject = _subject(spec)
        source = current_lifecycle_state(manager=self.paths, subject=subject)
        allowed = {
            HypothesisLifecycleState.VALIDATED.value,
            HypothesisLifecycleState.SUPPORTED.value,
        }
        if source not in allowed:
            raise ProspectiveValidationError(
                f"prospective_lifecycle_source_invalid:{source}"
            )
        spec_row = publish_prospective_spec(
            manager=self.paths,
            spec=spec,
            published_at=recorded_at,
        )
        transition = append_lifecycle_transition(
            manager=self.paths,
            subject=subject,
            from_state=source,
            to_state=HypothesisLifecycleState.PROSPECTIVE_VALIDATION.value,
            actor_id=actor_id,
            reason=reason,
            evidence_hashes={"prospective_validation_spec_hash": spec.contract_hash()},
            recorded_at=recorded_at,
        )
        return {
            "spec_record_hash": spec_row["record_hash"],
            "spec_row_hash": spec_row["row_hash"],
            "lifecycle_transition_row_hash": transition["row_hash"],
            "lifecycle_state": transition["to_state"],
        }

    def record(
        self,
        *,
        spec: ProspectiveValidationSpec,
        observation: ProspectiveObservation,
    ) -> dict[str, Any]:
        subject = _subject(spec)
        state = current_lifecycle_state(manager=self.paths, subject=subject)
        if state != HypothesisLifecycleState.PROSPECTIVE_VALIDATION.value:
            raise ProspectiveValidationError(
                f"prospective_lifecycle_not_active:{state}"
            )
        return record_prospective_observation(
            manager=self.paths,
            spec=spec,
            observation=observation,
        )

    def evaluate_and_conclude(
        self,
        *,
        spec: ProspectiveValidationSpec,
        evaluated_at: str,
        conclusion_id: str,
        conclusion_version: str,
        rationale: str,
        known_limitations: tuple[str, ...],
        decided_by: str,
        decided_at: str,
        transition_reason: str,
    ) -> dict[str, Any]:
        subject = _subject(spec)
        state = current_lifecycle_state(manager=self.paths, subject=subject)
        if state != HypothesisLifecycleState.PROSPECTIVE_VALIDATION.value:
            raise ProspectiveValidationError(
                f"prospective_lifecycle_not_active:{state}"
            )
        evaluation = evaluate_prospective_validation(
            manager=self.paths,
            spec=spec,
            evaluated_at=evaluated_at,
        )
        conclusion = build_research_conclusion(
            spec=spec,
            evaluation=evaluation,
            conclusion_id=conclusion_id,
            version=conclusion_version,
            rationale=rationale,
            known_limitations=known_limitations,
            decided_by=decided_by,
            decided_at=decided_at,
        )
        conclusion_row = publish_research_conclusion(
            manager=self.paths,
            spec=spec,
            evaluation=evaluation,
            conclusion=conclusion,
        )
        transition = append_lifecycle_transition(
            manager=self.paths,
            subject=subject,
            from_state=HypothesisLifecycleState.PROSPECTIVE_VALIDATION.value,
            to_state=evaluation.status.value,
            actor_id=decided_by,
            reason=transition_reason,
            evidence_hashes={
                "prospective_evaluation_hash": evaluation.content_hash(),
                "research_conclusion_hash": conclusion.content_hash(),
            },
            recorded_at=decided_at,
        )
        return {
            "evaluation": evaluation,
            "conclusion": conclusion,
            "conclusion_row_hash": conclusion_row["row_hash"],
            "lifecycle_transition_row_hash": transition["row_hash"],
            "lifecycle_state": transition["to_state"],
        }

    def finalize_research_package(
        self,
        *,
        package_id: str,
        version: str,
        base_package: dict[str, Any],
        spec: ProspectiveValidationSpec,
        evaluation: ProspectiveEvaluation,
        conclusion: ResearchConclusion,
        experiment_run_ref: ImmutableEvidenceRef,
        dataset_snapshot_ref: ImmutableEvidenceRef,
        feature_definition_ref: ImmutableEvidenceRef,
        experiment_spec_ref: ImmutableEvidenceRef,
        validation_decision_ref: ImmutableEvidenceRef,
        reproduction_receipt_ref: ImmutableEvidenceRef,
        supersedes: ImmutableEvidenceRef | None = None,
    ) -> tuple[ResearchPackageManifest, dict[str, Any]]:
        """Build and publish the final package only after lifecycle conclusion."""

        subject = _subject(spec)
        state = current_lifecycle_state(manager=self.paths, subject=subject)
        if state != evaluation.status.value or conclusion.status != evaluation.status:
            raise ProspectiveValidationError(
                "research_package_lifecycle_conclusion_mismatch"
            )
        verify_published_prospective_conclusion(
            manager=self.paths,
            spec=spec,
            evaluation=evaluation,
            conclusion=conclusion,
        )
        package = build_research_package_manifest(
            package_id=package_id,
            version=version,
            base_package=base_package,
            prospective_spec=spec,
            prospective_evaluation=evaluation,
            research_conclusion=conclusion,
            experiment_run_ref=experiment_run_ref,
            dataset_snapshot_ref=dataset_snapshot_ref,
            feature_definition_ref=feature_definition_ref,
            experiment_spec_ref=experiment_spec_ref,
            validation_decision_ref=validation_decision_ref,
            reproduction_receipt_ref=reproduction_receipt_ref,
            supersedes=supersedes,
        )
        receipt = ResearchPackageRegistry(self.paths).publish(package)
        return package, receipt


def _subject(spec: ProspectiveValidationSpec) -> GovernanceSubject:
    return GovernanceSubject(
        subject_type=GovernanceSubjectType.HYPOTHESIS,
        subject_id=spec.hypothesis_ref.logical_id,
        subject_version=spec.hypothesis_ref.version,
    )
