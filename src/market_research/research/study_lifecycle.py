"""Evidence-bound orchestration for one hypothesis validation lifecycle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from market_research.paths import ResearchPathManager

from .governance import (
    GovernanceError,
    GovernanceSubject,
    GovernanceSubjectType,
    append_lifecycle_transition,
    current_lifecycle_state,
    load_governance_rows,
    validate_governance_registry,
)
from .hashing import sha256_prefixed
from .hash_chain import (
    HashChainSnapshot,
    mutate_hash_chained_jsonl_atomic,
    read_hash_chained_jsonl_snapshot,
)
from .hypothesis_contract import HypothesisSpec
from .knowledge_contract import KnowledgeRef
from .knowledge_registry import (
    KnowledgeRegistryError,
    publish_manifest_lineage,
    require_validation_admission,
)
from .research_classification import requires_candidate_validation
from .research_standard import (
    PreregisteredResearchDesign,
    ResearchPhase,
    ResearchStandardBinding,
    parse_preregistered_research_design,
    parse_timestamp,
    require_hash,
    require_stable_id,
)
from .split_usage_policy import (
    SplitExposurePurpose,
    latest_confirmatory_exposure_at,
    record_split_exposure,
    require_successor_after_confirmatory_exposure,
)
from .validation_decision import (
    classify_validation_result,
    preserve_failed_validation,
    preserve_validation_result,
)


class StudyLifecycleError(ValueError):
    """The requested lifecycle operation conflicts with immutable evidence."""


@dataclass(frozen=True, slots=True)
class StudyLifecyclePublication:
    hypothesis_id: str
    hypothesis_version: str
    state: str | None
    decision_row: dict[str, Any] | None = None
    transition_row: dict[str, Any] | None = None


_TERMINAL_VALIDATION_STATES = frozenset(
    {"VALIDATED", "REJECTED", "INCONCLUSIVE", "SUPPORTED"}
)
_POLICY_ACTOR = "study-lifecycle-policy"
_PREREGISTRATION_SCHEMA_VERSION = 1
_PREREGISTRATION_HASH_LABEL = "study_preregistration_registry"
_VALIDATION_TRANSITION_REASON = (
    "Begin validation from an independently frozen preregistration."
)


def study_preregistration_registry_path(manager: ResearchPathManager) -> Path:
    path = manager.artifact_path(
        "reports",
        "research",
        "_registry",
        "study_preregistrations.jsonl",
    )
    if manager.is_within(path, manager.project_root):
        raise StudyLifecycleError(
            "study_preregistration_registry_must_be_repository_external"
        )
    return path


def record_study_stage(
    *,
    manager: ResearchPathManager,
    hypothesis: HypothesisSpec,
    to_state: str,
    actor_id: str,
    recorded_at: str,
    reason: str,
    exploration_evidence_hash: str | None = None,
) -> dict[str, Any]:
    """Record exactly one pre-validation stage as an independent event."""

    if hypothesis.schema_version != 2:
        raise StudyLifecycleError("study_lifecycle_hypothesis_lineage_required")
    target = str(to_state).strip().upper()
    sources = {"IDEA": None, "STRUCTURED": "IDEA", "EXPLORATORY": "STRUCTURED"}
    if target not in sources:
        raise StudyLifecycleError("study_lifecycle_stage_not_recordable")
    normalized_actor = require_stable_id(actor_id, "study_lifecycle.actor_id")
    if actor_id == _POLICY_ACTOR:
        raise StudyLifecycleError("study_lifecycle_human_stage_actor_required")
    normalized_reason = str(reason).strip()
    if not normalized_reason:
        raise StudyLifecycleError("study_lifecycle_stage_reason_required")
    proposed_at = parse_timestamp(recorded_at, "study_lifecycle.recorded_at")
    subject = _subject(hypothesis)
    existing_rows = _subject_transition_rows(manager, subject)
    existing_target = next(
        (row for row in existing_rows if row.get("to_state") == target), None
    )
    if existing_target is None and existing_rows:
        current_at = parse_timestamp(
            str(existing_rows[-1].get("recorded_at") or ""),
            "study_lifecycle.recorded_at",
        )
        if proposed_at <= current_at:
            raise StudyLifecycleError(
                "study_lifecycle_stage_timestamps_not_strictly_increasing"
            )
    publish_manifest_lineage(manager=manager, hypothesis=hypothesis)
    evidence = _prevalidation_stage_evidence(
        hypothesis=hypothesis,
        target=target,
        exploration_evidence_hash=exploration_evidence_hash,
    )
    row = _ensure_transition(
        manager=manager,
        subject=subject,
        source=sources[target],
        target=target,
        evidence=evidence,
        recorded_at=recorded_at,
        reason=normalized_reason,
        actor_id=normalized_actor,
    )
    if (
        row.get("actor_id") != normalized_actor
        or row.get("recorded_at") != recorded_at
        or row.get("reason") != normalized_reason
    ):
        raise StudyLifecycleError("study_lifecycle_stage_event_conflict")
    _require_strict_prevalidation_history(manager, subject, hypothesis=hypothesis)
    return row


def preregister_study(
    *,
    manager: ResearchPathManager,
    hypothesis: HypothesisSpec,
    design: PreregisteredResearchDesign,
    reason: str,
) -> dict[str, Any]:
    """Freeze D-06 material and transition an existing exploratory study."""

    if hypothesis.schema_version != 2:
        raise StudyLifecycleError("study_lifecycle_hypothesis_lineage_required")
    if not hypothesis.pre_registration_verified:
        raise StudyLifecycleError(
            "study_lifecycle_formal_preregistration_evidence_required"
        )
    if (
        design.hypothesis_contract_hash != hypothesis.contract_hash()
        or design.registered_at != hypothesis.pre_registered_at
        or design.external_registration_evidence_hash
        != hypothesis.registration_evidence_hash
    ):
        raise StudyLifecycleError("study_lifecycle_preregistration_binding_mismatch")
    if design.registered_by == _POLICY_ACTOR:
        raise StudyLifecycleError("study_lifecycle_human_stage_actor_required")
    require_stable_id(design.registered_by, "study_preregistration.registered_by")
    normalized_reason = str(reason).strip()
    if not normalized_reason:
        raise StudyLifecycleError("study_lifecycle_stage_reason_required")
    subject = _subject(hypothesis)
    rows = _subject_transition_rows(manager, subject)
    if not rows or rows[-1].get("to_state") != "EXPLORATORY":
        raise StudyLifecycleError(
            "study_lifecycle_preregistration_requires_exploratory_state"
        )
    prior_at = parse_timestamp(
        str(rows[-1]["recorded_at"]), "study_lifecycle.recorded_at"
    )
    registered_at = parse_timestamp(
        design.registered_at, "study_preregistration.registered_at"
    )
    if registered_at <= prior_at:
        raise StudyLifecycleError(
            "study_lifecycle_stage_timestamps_not_strictly_increasing"
        )
    exposure_at = latest_confirmatory_exposure_at(
        manager=manager,
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_version=hypothesis.version,
    )
    if exposure_at is not None:
        raise StudyLifecycleError(
            "study_lifecycle_preregistration_after_confirmatory_exposure"
        )
    registry_row = _publish_preregistration(manager=manager, design=design)
    transition = _ensure_transition(
        manager=manager,
        subject=subject,
        source="EXPLORATORY",
        target="PREREGISTERED",
        evidence={
            "preregistration_hash": design.content_hash,
            "preregistration_registry_row_hash": str(registry_row["row_hash"]),
            "external_preregistration_evidence_hash": (
                design.external_registration_evidence_hash
            ),
            "validation_manifest_hash": design.manifest_hash,
        },
        recorded_at=design.registered_at,
        reason=normalized_reason,
        actor_id=design.registered_by,
    )
    if (
        transition.get("actor_id") != design.registered_by
        or transition.get("recorded_at") != design.registered_at
        or transition.get("reason") != normalized_reason
    ):
        raise StudyLifecycleError("study_lifecycle_stage_event_conflict")
    _require_strict_prevalidation_history(manager, subject, hypothesis=hypothesis)
    return {"preregistration": registry_row, "transition": transition}


def admit_study_validation(
    *,
    manager: ResearchPathManager,
    manifest: Any,
    validation_admission: Mapping[str, Any],
    run_id: str | None = None,
) -> StudyLifecyclePublication:
    """Align one admitted validation-bound hypothesis with its guarded states."""

    if not requires_candidate_validation(
        getattr(manifest, "research_classification", None)
    ):
        raise StudyLifecycleError("study_lifecycle_validation_bound_manifest_required")
    hypothesis = _required_hypothesis(manifest)
    admission = _normalize_admission(manager, manifest, validation_admission)
    standard_binding = getattr(manifest, "research_standard_binding", None)
    if standard_binding is not None and not isinstance(
        standard_binding, ResearchStandardBinding
    ):
        raise StudyLifecycleError("study_lifecycle_research_standard_invalid")
    subject = _subject(hypothesis)
    timestamp = _admission_timestamp(admission)
    run_hash = _run_identity_hash(manifest, hypothesis, run_id) if run_id else None
    standard_evidence = _research_standard_lifecycle_evidence(standard_binding)
    design, preregistration_row = _require_preexisting_preregistration(
        manager=manager,
        hypothesis=hypothesis,
        manifest_hash=str(manifest.manifest_hash()),
        admission=admission,
    )
    publish_manifest_lineage(
        manager=manager,
        hypothesis=hypothesis,
        research_standard_binding=standard_binding,
    )
    validation_evidence = {
        "validation_manifest_hash": str(manifest.manifest_hash()),
        "validation_admission_row_hash": admission["row_hash"],
        "preregistration_hash": design.content_hash,
        "preregistration_registry_row_hash": str(preregistration_row["row_hash"]),
        **(
            {"research_standard_binding_hash": standard_evidence["binding_hash"]}
            if standard_evidence
            else {}
        ),
        **({"validation_run_identity_hash": run_hash} if run_hash is not None else {}),
    }
    transition = _ensure_transition(
        manager=manager,
        subject=subject,
        source="PREREGISTERED",
        target="VALIDATING",
        evidence=validation_evidence,
        recorded_at=timestamp,
        reason=_VALIDATION_TRANSITION_REASON,
    )
    if (
        transition.get("actor_id") != _POLICY_ACTOR
        or transition.get("recorded_at") != timestamp
        or transition.get("reason") != _VALIDATION_TRANSITION_REASON
    ):
        raise StudyLifecycleError("study_lifecycle_validation_event_conflict")
    admission_row = admission.get("admission")
    admission_payload = (
        admission_row.get("payload") if isinstance(admission_row, Mapping) else None
    )
    admission_actor = (
        admission_payload.get("actor_id")
        if isinstance(admission_payload, Mapping)
        else None
    )
    if not isinstance(admission_actor, str):
        raise StudyLifecycleError("study_lifecycle_admission_actor_missing")
    record_split_exposure(
        manager=manager,
        event_id="validation-exposure:"
        + sha256_prefixed(
            {
                "hypothesis_id": hypothesis.hypothesis_id,
                "hypothesis_version": hypothesis.version,
                "admission_row_hash": admission["row_hash"],
            },
            label="study_validation_exposure_identity",
        )[7:],
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_version=hypothesis.version,
        split_name=ResearchPhase.VALIDATION,
        purpose=SplitExposurePurpose.CONFIRMATORY_VALIDATION,
        actor_id=admission_actor,
        recorded_at=timestamp,
        source_artifact_hash=str(manifest.manifest_hash()),
    )
    state = current_lifecycle_state(manager=manager, subject=subject)
    if state not in {"VALIDATING", *_TERMINAL_VALIDATION_STATES}:
        raise StudyLifecycleError(f"study_lifecycle_admission_state_invalid:{state}")
    return StudyLifecyclePublication(
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_version=hypothesis.version,
        state=state,
    )


def complete_study_validation(
    *,
    manager: ResearchPathManager,
    manifest: Any,
    run_id: str,
    report: Mapping[str, Any],
    validation_admission: Mapping[str, Any] | None = None,
    decided_at: str | None = None,
) -> StudyLifecyclePublication:
    """Publish the terminal decision and atomically guarded lifecycle outcome."""

    hypothesis = _required_hypothesis(manifest)
    admission = _resolve_admission(
        manager=manager,
        manifest=manifest,
        source=validation_admission or report,
    )
    if admission is not None:
        admit_study_validation(
            manager=manager,
            manifest=manifest,
            validation_admission=admission,
            run_id=run_id,
        )
    target = classify_validation_result(report)
    subject = _subject(hypothesis)
    if admission is not None:
        _require_terminal_source(manager, subject, target)
    run_hash = _run_identity_hash(manifest, hypothesis, run_id)
    extra_hashes = {run_hash}
    if admission is not None:
        extra_hashes.add(admission["row_hash"])
    timestamp = (
        decided_at
        or _report_timestamp(report)
        or (
            _admission_timestamp(admission)
            if admission is not None
            else hypothesis.created_at
        )
    )
    decision_row = preserve_validation_result(
        manager=manager,
        manifest=manifest,
        run_id=run_id,
        report=report,
        decided_at=timestamp,
        additional_evidence_hashes=tuple(sorted(extra_hashes)),
    )
    transition = None
    if admission is not None:
        transition = _ensure_transition(
            manager=manager,
            subject=subject,
            source="VALIDATING",
            target=target,
            evidence={
                "validation_decision_hash": str(decision_row["record_hash"]),
                "validation_report_hash": str(report["content_hash"]),
                "validation_run_identity_hash": run_hash,
            },
            recorded_at=timestamp,
            reason=f"Record the terminal validation decision {target}.",
        )
    return StudyLifecyclePublication(
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_version=hypothesis.version,
        state=current_lifecycle_state(manager=manager, subject=subject),
        decision_row=decision_row,
        transition_row=transition,
    )


def preserve_study_validation_failure(
    *,
    manager: ResearchPathManager,
    manifest: Any,
    run_id: str,
    error: BaseException,
    decided_at: str | None = None,
) -> StudyLifecyclePublication:
    """Preserve an execution failure without treating it as falsification."""

    if isinstance(error, StudyLifecycleError):
        raise error
    hypothesis = _required_hypothesis(manifest)
    admission = _resolve_admission(manager=manager, manifest=manifest, source=None)
    if admission is not None:
        admit_study_validation(
            manager=manager,
            manifest=manifest,
            validation_admission=admission,
            run_id=run_id,
        )
    subject = _subject(hypothesis)
    if admission is not None:
        _require_terminal_source(manager, subject, "INCONCLUSIVE")
    run_hash = _run_identity_hash(manifest, hypothesis, run_id)
    extra_hashes = {run_hash}
    if admission is not None:
        extra_hashes.add(admission["row_hash"])
    timestamp = decided_at or (
        _admission_timestamp(admission)
        if admission is not None
        else hypothesis.created_at
    )
    decision_row = preserve_failed_validation(
        manager=manager,
        manifest=manifest,
        run_id=run_id,
        error=error,
        decided_at=timestamp,
        additional_evidence_hashes=tuple(sorted(extra_hashes)),
    )
    transition = None
    if admission is not None:
        transition = _ensure_transition(
            manager=manager,
            subject=subject,
            source="VALIDATING",
            target="INCONCLUSIVE",
            evidence={
                "validation_decision_hash": str(decision_row["record_hash"]),
                "validation_manifest_hash": str(manifest.manifest_hash()),
                "validation_run_identity_hash": run_hash,
            },
            recorded_at=timestamp,
            reason=(
                "Record the admitted study as inconclusive after an execution "
                "failure produced no admissible validation result."
            ),
        )
    return StudyLifecyclePublication(
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_version=hypothesis.version,
        state=current_lifecycle_state(manager=manager, subject=subject),
        decision_row=decision_row,
        transition_row=transition,
    )


def register_posthoc_followup(
    *,
    manager: ResearchPathManager,
    original: HypothesisSpec,
    followup: HypothesisSpec,
) -> KnowledgeRef:
    """Register a post-hoc condition only as a new immutable hypothesis ref."""

    if original.schema_version != 2 or followup.schema_version != 2:
        raise StudyLifecycleError("posthoc_followup_lineage_schema_required")
    if (
        original.hypothesis_id == followup.hypothesis_id
        and original.version == followup.version
    ):
        raise StudyLifecycleError("posthoc_followup_new_version_required")
    if original.contract_hash() == followup.contract_hash():
        raise StudyLifecycleError("posthoc_followup_distinct_contract_required")
    require_successor_after_confirmatory_exposure(
        manager=manager,
        hypothesis_id=original.hypothesis_id,
        exposed_version=original.version,
        proposed_version=followup.version,
    )
    original_question = original.research_question_ref
    followup_question = followup.research_question_ref
    if (
        original_question is None
        or followup_question is None
        or original_question.question_id != followup_question.question_id
    ):
        raise StudyLifecycleError("posthoc_followup_question_lineage_mismatch")
    publish_manifest_lineage(manager=manager, hypothesis=original)
    publish_manifest_lineage(manager=manager, hypothesis=followup)
    return KnowledgeRef(
        "hypothesis",
        followup.hypothesis_id,
        followup.version,
        followup.contract_hash(),
    )


def _prevalidation_stage_evidence(
    *,
    hypothesis: HypothesisSpec,
    target: str,
    exploration_evidence_hash: str | None,
) -> dict[str, str]:
    if target == "IDEA":
        return {"hypothesis_semantic_fingerprint": hypothesis.semantic_fingerprint()}
    if target == "STRUCTURED":
        return {
            "hypothesis_contract_hash": hypothesis.contract_hash(),
            "hypothesis_lineage_hash": str(hypothesis.lineage_hash()),
            "testable_hypothesis_definition_hash": sha256_prefixed(
                {
                    "targets": list(hypothesis.targets),
                    "measurement_method": hypothesis.measurement_method,
                    "expected_direction": hypothesis.expected_direction,
                    "evaluation_period": hypothesis.evaluation_period,
                    "mechanism": hypothesis.mechanism,
                    "comparison_target": hypothesis.comparison_target,
                    "observation_conditions": list(hypothesis.observation_conditions),
                    "falsification_criteria": list(hypothesis.falsification_criteria),
                },
                label="testable_hypothesis_definition",
            ),
        }
    if target != "EXPLORATORY":
        raise StudyLifecycleError("study_lifecycle_stage_not_recordable")
    if not isinstance(exploration_evidence_hash, str):
        raise StudyLifecycleError("study_lifecycle_exploration_evidence_required")
    try:
        require_hash(
            exploration_evidence_hash,
            "study_lifecycle.exploration_evidence_hash",
        )
    except ValueError as exc:
        raise StudyLifecycleError(
            "study_lifecycle_exploration_evidence_required"
        ) from exc
    return {
        "hypothesis_lineage_hash": str(hypothesis.lineage_hash()),
        "exploration_evidence_hash": exploration_evidence_hash,
    }


def _required_hypothesis(manifest: Any) -> HypothesisSpec:
    hypothesis = getattr(manifest, "hypothesis_spec", None)
    if not isinstance(hypothesis, HypothesisSpec) or hypothesis.schema_version != 2:
        raise StudyLifecycleError("study_lifecycle_hypothesis_lineage_required")
    return hypothesis


def _research_standard_lifecycle_evidence(
    binding: ResearchStandardBinding | None,
) -> dict[str, str]:
    if binding is None:
        return {}
    evidence = {
        "binding_hash": binding.content_hash,
        "observation_set_hash": sha256_prefixed(
            [item.content_hash for item in binding.observations],
            label="research_standard_observation_set",
        ),
        "research_question_hash": binding.research_question.content_hash,
        "mechanism_hash": binding.mechanism.content_hash,
        "hypothesis_version_hash": binding.hypothesis_version.content_hash,
    }
    if binding.preregistration_evidence_hash is not None:
        evidence["preregistration_evidence_hash"] = (
            binding.preregistration_evidence_hash
        )
    return evidence


def _subject(hypothesis: HypothesisSpec) -> GovernanceSubject:
    return GovernanceSubject(
        GovernanceSubjectType.HYPOTHESIS,
        hypothesis.hypothesis_id,
        hypothesis.version,
    )


def _publish_preregistration(
    *,
    manager: ResearchPathManager,
    design: PreregisteredResearchDesign,
) -> dict[str, Any]:
    path = study_preregistration_registry_path(manager)
    event_id = f"{design.registration_id}.{design.version}"
    payload = {
        "schema_version": _PREREGISTRATION_SCHEMA_VERSION,
        "event_id": event_id,
        "registration_id": design.registration_id,
        "version": design.version,
        "registered_at": design.registered_at,
        "registered_by": design.registered_by,
        "design": design.as_dict(),
        "design_hash": design.content_hash,
    }

    def mutation(
        snapshot: HashChainSnapshot,
        stage: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        matches = [row for row in snapshot.rows if row.get("event_id") == event_id]
        if matches:
            if len(matches) != 1 or any(
                matches[0].get(key) != value for key, value in payload.items()
            ):
                raise StudyLifecycleError("study_preregistration_identity_conflict")
            return dict(matches[0])
        prior_versions = [
            row
            for row in snapshot.rows
            if row.get("registration_id") == design.registration_id
        ]
        if prior_versions:
            latest_version = max(int(row.get("version") or 0) for row in prior_versions)
            latest_timestamp = max(
                parse_timestamp(
                    str(row.get("registered_at") or ""),
                    "study_preregistration.registered_at",
                )
                for row in prior_versions
            )
            if design.version <= latest_version:
                raise StudyLifecycleError(
                    "study_preregistration_version_not_strictly_increasing"
                )
            if (
                parse_timestamp(
                    design.registered_at,
                    "study_preregistration.registered_at",
                )
                <= latest_timestamp
            ):
                raise StudyLifecycleError(
                    "study_preregistration_timestamp_not_strictly_increasing"
                )
        return stage(payload)

    try:
        return dict(
            mutate_hash_chained_jsonl_atomic(
                path=path,
                label=_PREREGISTRATION_HASH_LABEL,
                mutation=mutation,
            ).value
        )
    except StudyLifecycleError:
        raise
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise StudyLifecycleError(
            f"study_preregistration_registry_write_failed:{exc}"
        ) from exc


def _preregistration_rows(manager: ResearchPathManager) -> tuple[dict[str, Any], ...]:
    try:
        snapshot = read_hash_chained_jsonl_snapshot(
            path=study_preregistration_registry_path(manager),
            label=_PREREGISTRATION_HASH_LABEL,
        )
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise StudyLifecycleError(
            f"study_preregistration_registry_invalid:{exc}"
        ) from exc
    if snapshot.status != "PASS":
        raise StudyLifecycleError("study_preregistration_registry_invalid")
    return tuple(dict(row) for row in snapshot.rows)


def _require_preexisting_preregistration(
    *,
    manager: ResearchPathManager,
    hypothesis: HypothesisSpec,
    manifest_hash: str,
    admission: Mapping[str, Any],
) -> tuple[PreregisteredResearchDesign, dict[str, Any]]:
    if not hypothesis.pre_registration_verified:
        raise StudyLifecycleError(
            "study_lifecycle_formal_preregistration_evidence_required"
        )
    admission_row = admission.get("admission")
    admission_payload = (
        admission_row.get("payload") if isinstance(admission_row, Mapping) else None
    )
    if (
        not isinstance(admission_payload, Mapping)
        or admission_payload.get("admission_status")
        != "FORMAL_PREREGISTERED_EXTERNAL_EVIDENCE"
    ):
        raise StudyLifecycleError(
            "study_lifecycle_formal_preregistration_admission_required"
        )
    registration_id = str(admission_payload.get("experiment_id") or "")
    rows = []
    for row in _preregistration_rows(manager):
        raw_design = row.get("design")
        if (
            row.get("registration_id") == registration_id
            and isinstance(raw_design, Mapping)
            and raw_design.get("manifest_hash") == manifest_hash
            and raw_design.get("hypothesis_contract_hash") == hypothesis.contract_hash()
        ):
            rows.append(row)
    if len(rows) != 1:
        raise StudyLifecycleError("study_lifecycle_preexisting_preregistration_missing")
    row = rows[0]
    try:
        design = parse_preregistered_research_design(row.get("design"))
    except (TypeError, ValueError) as exc:
        raise StudyLifecycleError(
            "study_lifecycle_preregistration_payload_invalid"
        ) from exc
    recorded_payload = {
        key: value
        for key, value in row.items()
        if key not in {"sequence", "prior_hash", "row_hash"}
    }
    expected_payload = {
        "schema_version": _PREREGISTRATION_SCHEMA_VERSION,
        "event_id": f"{design.registration_id}.{design.version}",
        "registration_id": design.registration_id,
        "version": design.version,
        "registered_at": design.registered_at,
        "registered_by": design.registered_by,
        "design": design.as_dict(),
        "design_hash": design.content_hash,
    }
    if (
        recorded_payload != expected_payload
        or row.get("design_hash") != design.content_hash
        or design.registration_id != registration_id
        or design.manifest_hash != manifest_hash
        or design.hypothesis_contract_hash != hypothesis.contract_hash()
        or design.registered_at != hypothesis.pre_registered_at
        or design.external_registration_evidence_hash
        != hypothesis.registration_evidence_hash
    ):
        raise StudyLifecycleError("study_lifecycle_preregistration_binding_mismatch")
    subject = _subject(hypothesis)
    history = _require_strict_prevalidation_history(
        manager,
        subject,
        hypothesis=hypothesis,
    )
    if tuple(row.get("to_state") for row in history) != (
        "IDEA",
        "STRUCTURED",
        "EXPLORATORY",
        "PREREGISTERED",
    ):
        raise StudyLifecycleError("study_lifecycle_preexisting_preregistration_missing")
    preregistered = history[-1]
    evidence = preregistered.get("evidence_hashes")
    if not isinstance(evidence, Mapping) or (
        evidence.get("preregistration_hash") != design.content_hash
        or evidence.get("preregistration_registry_row_hash") != row.get("row_hash")
        or evidence.get("validation_manifest_hash") != manifest_hash
    ):
        raise StudyLifecycleError(
            "study_lifecycle_preregistration_transition_binding_mismatch"
        )
    admitted_at = parse_timestamp(
        _admission_timestamp(admission), "study_lifecycle.admitted_at"
    )
    registered_at = parse_timestamp(
        design.registered_at, "study_lifecycle.registered_at"
    )
    if admitted_at <= registered_at:
        raise StudyLifecycleError("study_lifecycle_admission_not_after_preregistration")
    exposure_at = latest_confirmatory_exposure_at(
        manager=manager,
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_version=hypothesis.version,
    )
    if exposure_at is not None and exposure_at < admitted_at:
        raise StudyLifecycleError(
            "study_lifecycle_confirmatory_exposure_precedes_admission"
        )
    return design, row


def _require_strict_prevalidation_history(
    manager: ResearchPathManager,
    subject: GovernanceSubject,
    *,
    hypothesis: HypothesisSpec,
) -> tuple[dict[str, Any], ...]:
    rows = _subject_transition_rows(manager, subject)
    expected = ("IDEA", "STRUCTURED", "EXPLORATORY", "PREREGISTERED")
    prefix = tuple(row for row in rows if row.get("to_state") in expected)
    actual = tuple(str(row.get("to_state")) for row in prefix)
    if actual not in {expected[: len(actual)], expected}:
        raise StudyLifecycleError("study_lifecycle_prevalidation_history_invalid")
    if len(actual) > len(expected):
        raise StudyLifecycleError("study_lifecycle_prevalidation_history_invalid")
    timestamps: list[datetime] = []
    for row in prefix:
        if row.get("actor_id") == _POLICY_ACTOR:
            raise StudyLifecycleError(
                "study_lifecycle_synthetic_prevalidation_history_forbidden"
            )
        timestamps.append(
            parse_timestamp(
                str(row.get("recorded_at") or ""),
                "study_lifecycle.recorded_at",
            )
        )
        state = str(row.get("to_state") or "")
        evidence = row.get("evidence_hashes")
        if not isinstance(evidence, Mapping):
            raise StudyLifecycleError("study_lifecycle_prevalidation_evidence_invalid")
        if state in {"IDEA", "STRUCTURED"}:
            expected_evidence = _prevalidation_stage_evidence(
                hypothesis=hypothesis,
                target=state,
                exploration_evidence_hash=None,
            )
            if dict(evidence) != expected_evidence:
                raise StudyLifecycleError(
                    "study_lifecycle_prevalidation_evidence_invalid"
                )
        elif state == "EXPLORATORY":
            exploration_hash = evidence.get("exploration_evidence_hash")
            try:
                expected_evidence = _prevalidation_stage_evidence(
                    hypothesis=hypothesis,
                    target=state,
                    exploration_evidence_hash=(
                        exploration_hash if isinstance(exploration_hash, str) else None
                    ),
                )
            except StudyLifecycleError as exc:
                raise StudyLifecycleError(
                    "study_lifecycle_prevalidation_evidence_invalid"
                ) from exc
            if dict(evidence) != expected_evidence:
                raise StudyLifecycleError(
                    "study_lifecycle_prevalidation_evidence_invalid"
                )
    if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        raise StudyLifecycleError(
            "study_lifecycle_stage_timestamps_not_strictly_increasing"
        )
    return prefix


def _normalize_admission(
    manager: ResearchPathManager,
    manifest: Any,
    source: Mapping[str, Any],
) -> dict[str, Any]:
    row_value = source.get("admission", source.get("validation_admission", source))
    if not isinstance(row_value, Mapping):
        raise StudyLifecycleError("study_lifecycle_validation_admission_missing")
    row = dict(row_value)
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        raise StudyLifecycleError("study_lifecycle_validation_admission_invalid")
    record_hash = str(
        source.get("admission_record_hash")
        or source.get("validation_admission_record_hash")
        or row.get("record_hash")
        or ""
    )
    row_hash = str(
        source.get("admission_row_hash")
        or source.get("validation_admission_row_hash")
        or row.get("row_hash")
        or ""
    )
    manifest_hash = str(manifest.manifest_hash())
    hypothesis = _required_hypothesis(manifest)
    expected_ref = KnowledgeRef(
        "hypothesis",
        hypothesis.hypothesis_id,
        hypothesis.version,
        hypothesis.contract_hash(),
    )
    if (
        payload.get("manifest_hash") != manifest_hash
        or payload.get("hypothesis_ref") != expected_ref.as_dict()
        or row.get("record_hash") != record_hash
        or row.get("row_hash") != row_hash
        or not record_hash.startswith("sha256:")
        or not row_hash.startswith("sha256:")
    ):
        raise StudyLifecycleError("study_lifecycle_validation_admission_conflict")
    try:
        canonical = require_validation_admission(
            manager=manager,
            manifest=manifest,
            expected_row_hash=row_hash,
        )
    except KnowledgeRegistryError as exc:
        raise StudyLifecycleError(
            f"study_lifecycle_validation_admission_unverified:{exc}"
        ) from exc
    if canonical != row:
        raise StudyLifecycleError("study_lifecycle_validation_admission_not_canonical")
    return {
        "admission": row,
        "record_hash": record_hash,
        "row_hash": row_hash,
    }


def _resolve_admission(
    *,
    manager: ResearchPathManager,
    manifest: Any,
    source: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not requires_candidate_validation(
        getattr(manifest, "research_classification", None)
    ):
        return None
    if source is not None and (
        source.get("admission") is not None
        or source.get("validation_admission") is not None
    ):
        return _normalize_admission(manager, manifest, source)
    try:
        row = require_validation_admission(manager=manager, manifest=manifest)
    except (KeyError, KnowledgeRegistryError):
        return None
    return _normalize_admission(manager, manifest, row)


def _admission_timestamp(admission: Mapping[str, Any]) -> str:
    row = admission.get("admission")
    payload = row.get("payload") if isinstance(row, Mapping) else None
    timestamp = payload.get("frozen_at") if isinstance(payload, Mapping) else None
    if not isinstance(timestamp, str) or not timestamp:
        raise StudyLifecycleError("study_lifecycle_admission_timestamp_missing")
    return timestamp


def _report_timestamp(report: Mapping[str, Any]) -> str | None:
    value = report.get("generated_at")
    return value if isinstance(value, str) and value else None


def _run_identity_hash(
    manifest: Any,
    hypothesis: HypothesisSpec,
    run_id: str,
) -> str:
    return sha256_prefixed(
        {
            "schema_version": 1,
            "experiment_id": str(manifest.experiment_id),
            "manifest_hash": str(manifest.manifest_hash()),
            "hypothesis_id": hypothesis.hypothesis_id,
            "hypothesis_version": hypothesis.version,
            "run_id": run_id,
        },
        label="study_validation_run_identity",
    )


def _subject_transition_rows(
    manager: ResearchPathManager,
    subject: GovernanceSubject,
) -> list[dict[str, Any]]:
    validation = validate_governance_registry(manager)
    if validation["status"] != "PASS":
        raise StudyLifecycleError("study_lifecycle_governance_registry_invalid")
    return [
        row
        for row in load_governance_rows(
            manager.artifact_path(
                "reports", "research", "_registry", "governance.jsonl"
            )
        )
        if row.get("event_type") == "lifecycle_transition"
        and row.get("subject_type") == subject.subject_type.value
        and row.get("subject_id") == subject.subject_id
        and row.get("subject_version") == subject.subject_version
    ]


def _ensure_transition(
    *,
    manager: ResearchPathManager,
    subject: GovernanceSubject,
    source: str | None,
    target: str,
    evidence: Mapping[str, str],
    recorded_at: str | None,
    reason: str,
    actor_id: str = _POLICY_ACTOR,
) -> dict[str, Any]:
    for _attempt in range(8):
        rows = _subject_transition_rows(manager, subject)
        existing = next(
            (row for row in reversed(rows) if row.get("to_state") == target),
            None,
        )
        if existing is not None:
            _verify_evidence(existing, evidence, target)
            return existing
        current = str(rows[-1]["to_state"]) if rows else None
        if current != source:
            raise StudyLifecycleError(
                f"study_lifecycle_state_conflict:{current}->{target}"
            )
        try:
            return append_lifecycle_transition(
                manager=manager,
                subject=subject,
                from_state=source,
                to_state=target,
                actor_id=actor_id,
                reason=reason,
                evidence_hashes=evidence,
                recorded_at=recorded_at,
            )
        except GovernanceError as exc:
            if "governance_state_conflict" in str(exc):
                continue
            raise StudyLifecycleError(str(exc)) from exc
    raise StudyLifecycleError("study_lifecycle_concurrent_transition_retry_exhausted")


def _verify_evidence(
    row: Mapping[str, Any],
    expected: Mapping[str, str],
    state: str,
) -> None:
    actual = row.get("evidence_hashes")
    if not isinstance(actual, Mapping) or any(
        actual.get(key) != value for key, value in expected.items()
    ):
        raise StudyLifecycleError(f"study_lifecycle_evidence_conflict:{state}")


def _require_terminal_source(
    manager: ResearchPathManager,
    subject: GovernanceSubject,
    target: str,
) -> None:
    state = current_lifecycle_state(manager=manager, subject=subject)
    if state not in {"VALIDATING", target}:
        raise StudyLifecycleError(
            f"study_lifecycle_terminal_state_conflict:{state}->{target}"
        )
