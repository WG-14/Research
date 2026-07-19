"""Evidence-bound orchestration for one hypothesis validation lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
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
from .hypothesis_contract import HypothesisSpec
from .knowledge_contract import KnowledgeRef
from .knowledge_registry import (
    KnowledgeRegistryError,
    publish_manifest_lineage,
    require_validation_admission,
)
from .research_classification import requires_candidate_validation
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


_STANDARD_STATES = (
    "IDEA",
    "STRUCTURED",
    "EXPLORATORY",
    "PREREGISTERED",
    "VALIDATING",
)
_LEGACY_STATES = (
    "IDEA",
    "HYPOTHESIS_DEFINED",
    "EXPLORING",
    "VALIDATING",
)
_TERMINAL_VALIDATION_STATES = frozenset(
    {"VALIDATED", "REJECTED", "INCONCLUSIVE", "SUPPORTED"}
)
_POLICY_ACTOR = "study-lifecycle-policy"


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
    publish_manifest_lineage(manager=manager, hypothesis=hypothesis)
    subject = _subject(hypothesis)
    timestamp = _admission_timestamp(admission)
    run_hash = _run_identity_hash(manifest, hypothesis, run_id) if run_id else None
    standard_steps = (
        (
            None,
            "IDEA",
            {"hypothesis_semantic_fingerprint": hypothesis.semantic_fingerprint()},
        ),
        (
            "IDEA",
            "STRUCTURED",
            {
                "hypothesis_contract_hash": hypothesis.contract_hash(),
                "hypothesis_lineage_hash": str(hypothesis.lineage_hash()),
            },
        ),
        (
            "STRUCTURED",
            "EXPLORATORY",
            {
                "hypothesis_lineage_hash": str(hypothesis.lineage_hash()),
            },
        ),
        (
            "EXPLORATORY",
            "PREREGISTERED",
            {
                "preregistration_hash": admission["record_hash"],
                "validation_admission_row_hash": admission["row_hash"],
            },
        ),
        (
            "PREREGISTERED",
            "VALIDATING",
            {
                "validation_manifest_hash": str(manifest.manifest_hash()),
                "validation_admission_row_hash": admission["row_hash"],
                **(
                    {"validation_run_identity_hash": run_hash}
                    if run_hash is not None
                    else {}
                ),
            },
        ),
    )
    legacy_steps: tuple[tuple[str | None, str, dict[str, str]], ...] = (
        standard_steps[0],
        (
            "IDEA",
            "HYPOTHESIS_DEFINED",
            {"hypothesis_contract_hash": hypothesis.contract_hash()},
        ),
        ("HYPOTHESIS_DEFINED", "EXPLORING", {}),
        (
            "EXPLORING",
            "VALIDATING",
            {"validation_manifest_hash": str(manifest.manifest_hash())},
        ),
    )
    existing = _subject_transition_rows(manager, subject)
    legacy = any(row.get("to_state") in _LEGACY_STATES[1:3] for row in existing)
    steps = legacy_steps if legacy else standard_steps
    for source, target, evidence in steps:
        _ensure_transition(
            manager=manager,
            subject=subject,
            source=source,
            target=target,
            evidence=evidence,
            recorded_at=timestamp,
            reason=f"Advance the immutable admitted study to {target}.",
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


def _required_hypothesis(manifest: Any) -> HypothesisSpec:
    hypothesis = getattr(manifest, "hypothesis_spec", None)
    if not isinstance(hypothesis, HypothesisSpec) or hypothesis.schema_version != 2:
        raise StudyLifecycleError("study_lifecycle_hypothesis_lineage_required")
    return hypothesis


def _subject(hypothesis: HypothesisSpec) -> GovernanceSubject:
    return GovernanceSubject(
        GovernanceSubjectType.HYPOTHESIS,
        hypothesis.hypothesis_id,
        hypothesis.version,
    )


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
                actor_id=_POLICY_ACTOR,
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
