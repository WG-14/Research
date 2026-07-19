"""Structured validation decisions and automatic negative-result preservation."""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from market_research.paths import ResearchPathManager
from market_research.storage_io import write_json_atomic_create_or_verify

from .artifact_store import ArtifactStore
from .hash_chain import (
    append_hash_chained_jsonl_idempotent,
    read_hash_chained_jsonl_snapshot,
)
from .hashing import report_content_hash_payload, sha256_prefixed
from .hypothesis_contract import HypothesisSpec
from .knowledge_contract import (
    HypothesisOutcomeSpec,
    KnowledgeRef,
    knowledge_ref_from_dict,
)
from .knowledge_registry import (
    get_knowledge_record,
    publish_hypothesis_outcome,
    publish_manifest_lineage,
    validate_knowledge_registry,
    validation_admission_binding_reasons,
)


VALIDATION_DECISION_SCHEMA_VERSION = 1
VALIDATION_DECISION_HASH_LABEL = "research_validation_decision"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_DECISIONS = frozenset({"REJECTED", "INCONCLUSIVE", "VALIDATED"})
_TERMINAL_RESULT_DECISIONS = {
    "PASS": "VALIDATED",
    "FAIL": "REJECTED",
    "INSUFFICIENT_EVIDENCE": "INCONCLUSIVE",
}
_TERMINAL_REPORT_REF_SCHEMA_VERSION = 1
_TERMINAL_REPORT_SNAPSHOT_HASH_LABEL = "terminal_validation_report_snapshot"


class ValidationDecisionError(ValueError):
    """A validation decision is incomplete or conflicts with prior evidence."""


@dataclass(frozen=True, slots=True)
class CriterionDecision:
    criterion_id: str
    passed: bool
    observed: str
    required: str
    exception: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.criterion_id, "criterion_decision.criterion_id")
        if not isinstance(self.passed, bool):
            raise ValidationDecisionError("criterion_decision.passed_invalid")
        _require_text(self.observed, "criterion_decision.observed")
        _require_text(self.required, "criterion_decision.required")
        if self.exception is not None:
            _require_text(self.exception, "criterion_decision.exception")
        if self.passed and self.exception is not None:
            raise ValidationDecisionError(
                "criterion_decision.passed_cannot_have_exception"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "passed": self.passed,
            "observed": self.observed,
            "required": self.required,
            "exception": self.exception,
        }


@dataclass(frozen=True, slots=True)
class TerminalValidationReportRef:
    """Resolvable, exact-byte binding to a terminal validation report snapshot."""

    schema_version: int
    artifact_type: str
    experiment_id: str
    run_id: str
    content_hash: str
    snapshot_hash: str
    artifact_path: str

    def __post_init__(self) -> None:
        if self.schema_version != _TERMINAL_REPORT_REF_SCHEMA_VERSION:
            raise ValidationDecisionError("terminal_report_ref_schema_invalid")
        if self.artifact_type != "validated_research_result":
            raise ValidationDecisionError("terminal_report_ref_artifact_type_invalid")
        _require_id(self.experiment_id, "terminal_report_ref.experiment_id")
        _require_id(self.run_id, "terminal_report_ref.run_id")
        _require_hashes((self.content_hash,), "terminal_report_ref.content_hash")
        _require_hashes((self.snapshot_hash,), "terminal_report_ref.snapshot_hash")
        path = Path(self.artifact_path).expanduser()
        if not path.is_absolute():
            raise ValidationDecisionError("terminal_report_ref_path_not_absolute")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": self.artifact_type,
            "experiment_id": self.experiment_id,
            "run_id": self.run_id,
            "content_hash": self.content_hash,
            "snapshot_hash": self.snapshot_hash,
            "artifact_path": self.artifact_path,
        }


@dataclass(frozen=True, slots=True)
class ValidationDecision:
    schema_version: int
    decision_id: str
    version: str
    hypothesis_ref: KnowledgeRef
    experiment_id: str
    run_id: str
    decision: str
    criterion_results: tuple[CriterionDecision, ...]
    evidence_hashes: tuple[str, ...]
    researcher_interpretation: str
    reviewer_comment: str
    decided_by: str
    decided_at: str
    terminal_report_ref: TerminalValidationReportRef | None = None
    failure_type: str | None = None
    learned: tuple[str, ...] = ()
    followup_hypothesis_refs: tuple[KnowledgeRef, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != VALIDATION_DECISION_SCHEMA_VERSION:
            raise ValidationDecisionError(
                "validation_decision_schema_version_unsupported"
            )
        _require_id(self.decision_id, "validation_decision.decision_id")
        _require_id(self.version, "validation_decision.version")
        _require_id(self.experiment_id, "validation_decision.experiment_id")
        _require_id(self.run_id, "validation_decision.run_id")
        if self.hypothesis_ref.record_type != "hypothesis":
            raise ValidationDecisionError(
                "validation_decision_hypothesis_reference_invalid"
            )
        if self.decision not in _DECISIONS:
            raise ValidationDecisionError("validation_decision_value_unknown")
        if not self.criterion_results:
            raise ValidationDecisionError(
                "validation_decision_criterion_results_required"
            )
        identities = [item.criterion_id for item in self.criterion_results]
        if len(identities) != len(set(identities)):
            raise ValidationDecisionError(
                "validation_decision_criterion_results_duplicate"
            )
        if self.decision == "VALIDATED" and any(
            not item.passed for item in self.criterion_results
        ):
            raise ValidationDecisionError(
                "validated_decision_contains_failed_criterion"
            )
        if self.decision == "REJECTED" and all(
            item.passed for item in self.criterion_results
        ):
            raise ValidationDecisionError(
                "rejected_decision_requires_failed_criterion"
            )
        _require_hashes(self.evidence_hashes, "validation_decision.evidence_hashes")
        _require_text(
            self.researcher_interpretation,
            "validation_decision.researcher_interpretation",
        )
        _require_text(self.reviewer_comment, "validation_decision.reviewer_comment")
        _require_text(self.decided_by, "validation_decision.decided_by")
        _require_timestamp(self.decided_at, "validation_decision.decided_at")
        if self.failure_type is not None:
            _require_id(self.failure_type, "validation_decision.failure_type")
        if self.decision == "REJECTED" and self.failure_type is None:
            raise ValidationDecisionError(
                "rejected_decision_failure_type_required"
            )
        if self.decision_id.startswith("validation-result:"):
            if self.terminal_report_ref is None:
                raise ValidationDecisionError(
                    "validation_result_terminal_report_ref_required"
                )
            if (
                self.terminal_report_ref.experiment_id != self.experiment_id
                or self.terminal_report_ref.run_id != self.run_id
                or self.terminal_report_ref.content_hash not in self.evidence_hashes
            ):
                raise ValidationDecisionError(
                    "validation_result_terminal_report_ref_mismatch"
                )
        _require_unique_text(self.learned, "validation_decision.learned", required=False)
        followup_ids = [
            (item.record_type, item.logical_id, item.version)
            for item in self.followup_hypothesis_refs
        ]
        if any(item.record_type != "hypothesis" for item in self.followup_hypothesis_refs):
            raise ValidationDecisionError(
                "validation_decision_followup_reference_invalid"
            )
        if len(followup_ids) != len(set(followup_ids)):
            raise ValidationDecisionError(
                "validation_decision_followup_reference_duplicate"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "version": self.version,
            "hypothesis_ref": self.hypothesis_ref.as_dict(),
            "experiment_id": self.experiment_id,
            "run_id": self.run_id,
            "decision": self.decision,
            "criterion_results": [item.as_dict() for item in self.criterion_results],
            "evidence_hashes": list(self.evidence_hashes),
            "researcher_interpretation": self.researcher_interpretation,
            "reviewer_comment": self.reviewer_comment,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "terminal_report_ref": (
                self.terminal_report_ref.as_dict()
                if self.terminal_report_ref is not None
                else None
            ),
            "failure_type": self.failure_type,
            "learned": list(self.learned),
            "followup_hypothesis_refs": [
                item.as_dict() for item in self.followup_hypothesis_refs
            ],
        }

    def content_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="validation_decision")


def validation_decision_registry_path(manager: ResearchPathManager) -> Path:
    return manager.artifact_path(
        "reports", "research", "_registry", "validation_decisions.jsonl"
    )


def terminal_validation_report_path(
    manager: ResearchPathManager,
    *,
    experiment_id: str,
    run_id: str,
    snapshot_hash: str,
) -> Path:
    _require_id(experiment_id, "terminal_report.experiment_id")
    _require_id(run_id, "terminal_report.run_id")
    _require_hashes((snapshot_hash,), "terminal_report.snapshot_hash")
    return manager.artifact_path(
        "reports",
        "research",
        "_registry",
        "terminal_validation_reports",
        experiment_id,
        run_id,
        f"{snapshot_hash.removeprefix('sha256:')}.json",
    )


def publish_validation_decision(
    *,
    manager: ResearchPathManager,
    hypothesis: HypothesisSpec,
    decision: ValidationDecision,
) -> dict[str, Any]:
    """Publish a structured decision and its searchable hypothesis outcome."""

    expected_ref = _hypothesis_ref(hypothesis)
    if decision.hypothesis_ref != expected_ref:
        raise ValidationDecisionError("validation_decision_hypothesis_hash_mismatch")
    with _decision_publication_lock(manager):
        publish_manifest_lineage(manager=manager, hypothesis=hypothesis)
        existing = _existing_subject_decision(manager=manager, decision=decision)
        if existing is not None:
            _require_outcome_binding(manager=manager, row=existing, decision=decision)
            return existing
        outcome = HypothesisOutcomeSpec(
            schema_version=1,
            outcome_id=f"outcome:{decision.decision_id}",
            version=decision.version,
            hypothesis_ref=decision.hypothesis_ref,
            question_ref=_question_ref(hypothesis),
            outcome={
                "VALIDATED": "supported",
                "REJECTED": "rejected",
                "INCONCLUSIVE": "inconclusive",
            }[decision.decision],
            rationale=decision.researcher_interpretation,
            actor_id=decision.decided_by,
            recorded_at=decision.decided_at,
            evidence_hashes=tuple(
                sorted({*decision.evidence_hashes, decision.content_hash()})
            ),
        )
        outcome_row = publish_hypothesis_outcome(manager=manager, outcome=outcome)
        payload = {
            "event_id": f"decision:{decision.decision_id}:{decision.version}",
            "record_type": "VALIDATION_DECISION",
            "logical_id": decision.decision_id,
            "version": decision.version,
            "record_hash": decision.content_hash(),
            "hypothesis_outcome_record_hash": outcome_row["record_hash"],
            "hypothesis_outcome_row_hash": outcome_row["row_hash"],
            "payload": decision.as_dict(),
        }
        return append_hash_chained_jsonl_idempotent(
            store=ArtifactStore(root=manager.artifact_root),
            path=validation_decision_registry_path(manager),
            payload=payload,
            label=VALIDATION_DECISION_HASH_LABEL,
        )


def preserve_failed_validation(
    *,
    manager: ResearchPathManager,
    manifest: Any,
    run_id: str,
    error: BaseException,
    decided_at: str | None = None,
    decided_by: str = "validation-failure-policy",
    additional_evidence_hashes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Turn an execution failure into immutable, searchable negative evidence."""

    hypothesis = getattr(manifest, "hypothesis_spec", None)
    if not isinstance(hypothesis, HypothesisSpec) or hypothesis.schema_version != 2:
        raise ValidationDecisionError("validation_failure_hypothesis_lineage_required")
    manifest_hash = str(manifest.manifest_hash())
    error_message_hash = sha256_prefixed(
        str(error), label="validation_failure_error_message"
    )
    error_evidence_hash = sha256_prefixed(
        {
            "experiment_id": str(manifest.experiment_id),
            "manifest_hash": manifest_hash,
            "run_id": run_id,
            "error_type": type(error).__name__,
            "error_message_hash": error_message_hash,
        },
        label="validation_failure_evidence",
    )
    timestamp = decided_at or datetime.now(timezone.utc).isoformat()
    decision = ValidationDecision(
        schema_version=VALIDATION_DECISION_SCHEMA_VERSION,
        decision_id=f"validation-failure:{manifest.experiment_id}:{run_id}",
        version="1",
        hypothesis_ref=_hypothesis_ref(hypothesis),
        experiment_id=str(manifest.experiment_id),
        run_id=run_id,
        decision="INCONCLUSIVE",
        criterion_results=(
            CriterionDecision(
                criterion_id="validation_execution_completed",
                passed=False,
                observed=f"error_type:{type(error).__name__}",
                required="terminal validation report with complete evidence",
                exception="execution_failure",
            ),
        ),
        evidence_hashes=tuple(
            sorted({manifest_hash, error_evidence_hash, *additional_evidence_hashes})
        ),
        researcher_interpretation=(
            "The validation attempt failed before it produced admissible evidence; "
            "the attempt cannot validate or reject the hypothesis."
        ),
        reviewer_comment="Automatically preserved by the validation failure policy.",
        decided_by=decided_by,
        decided_at=timestamp,
        failure_type="execution_failure",
        learned=(
            "Treat this attempt as inconclusive process evidence and diagnose the "
            "failure before retrying the preregistered study.",
        ),
    )
    return publish_validation_decision(
        manager=manager,
        hypothesis=hypothesis,
        decision=decision,
    )


def preserve_validation_result(
    *,
    manager: ResearchPathManager,
    manifest: Any,
    run_id: str,
    report: Mapping[str, Any],
    decided_at: str | None = None,
    decided_by: str = "validation-decision-policy",
    additional_evidence_hashes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Preserve a terminal report as an explicit validated/rejected decision."""

    hypothesis = getattr(manifest, "hypothesis_spec", None)
    if not isinstance(hypothesis, HypothesisSpec) or hypothesis.schema_version != 2:
        raise ValidationDecisionError("validation_result_hypothesis_lineage_required")
    terminal_result = str(report.get("end_to_end_validation_result") or "")
    decision_value = classify_validation_result(report)
    manifest_hash = str(manifest.manifest_hash())
    reported_hash = _validate_terminal_report(
        manager=manager,
        manifest=manifest,
        run_id=run_id,
        report=report,
        hypothesis=hypothesis,
        manifest_hash=manifest_hash,
    )
    report_ref = _publish_terminal_report_snapshot(
        manager=manager,
        experiment_id=str(manifest.experiment_id),
        run_id=run_id,
        report=report,
        content_hash=reported_hash,
    )
    timestamp = decided_at or datetime.now(timezone.utc).isoformat()
    rejected = decision_value == "REJECTED"
    inconclusive = decision_value == "INCONCLUSIVE"
    decision = ValidationDecision(
        schema_version=VALIDATION_DECISION_SCHEMA_VERSION,
        decision_id=f"validation-result:{manifest.experiment_id}:{run_id}",
        version="1",
        hypothesis_ref=_hypothesis_ref(hypothesis),
        experiment_id=str(manifest.experiment_id),
        run_id=run_id,
        decision=decision_value,
        criterion_results=(
            CriterionDecision(
                criterion_id="end_to_end_validation_result",
                passed=decision_value == "VALIDATED",
                observed=terminal_result,
                required="PASS",
                exception=(
                    "validation_criteria_failed"
                    if rejected
                    else "insufficient_evidence"
                    if inconclusive
                    else None
                ),
            ),
        ),
        evidence_hashes=tuple(
            sorted({manifest_hash, reported_hash, *additional_evidence_hashes})
        ),
        researcher_interpretation=(
            "The frozen validation criteria were satisfied."
            if decision_value == "VALIDATED"
            else "The frozen validation produced insufficient evidence for either "
            "validation or rejection."
            if inconclusive
            else "At least one frozen validation criterion failed; the hypothesis "
            "is not validated and the negative result remains searchable."
        ),
        reviewer_comment=(
            "Recorded automatically from the immutable terminal validation report."
        ),
        decided_by=decided_by,
        decided_at=timestamp,
        terminal_report_ref=report_ref,
        failure_type=(
            "validation_criteria_failed"
            if rejected
            else "insufficient_evidence"
            if inconclusive
            else None
        ),
        learned=(
            ("Review the failed criteria before registering a follow-up hypothesis.",)
            if rejected
            else (
                "Collect the preregistered minimum evidence before a new decision.",
            )
            if inconclusive
            else ()
        ),
    )
    return publish_validation_decision(
        manager=manager,
        hypothesis=hypothesis,
        decision=decision,
    )


def classify_validation_result(report: Mapping[str, Any]) -> str:
    terminal_result = str(report.get("end_to_end_validation_result") or "")
    try:
        return _TERMINAL_RESULT_DECISIONS[terminal_result]
    except KeyError as exc:
        raise ValidationDecisionError(
            "validation_result_terminal_status_invalid"
        ) from exc


def query_validation_decisions(
    *,
    manager: ResearchPathManager,
    hypothesis_id: str | None = None,
    decision: str | None = None,
    failure_type: str | None = None,
) -> list[dict[str, Any]]:
    validation = validate_validation_decision_registry(manager)
    if validation["status"] != "PASS":
        raise ValidationDecisionError("validation_decision_registry_invalid")
    snapshot = read_hash_chained_jsonl_snapshot(
        path=validation_decision_registry_path(manager),
        label=VALIDATION_DECISION_HASH_LABEL,
    )
    if snapshot.status != "PASS":
        raise ValidationDecisionError("validation_decision_registry_invalid")
    result: list[dict[str, Any]] = []
    for row in snapshot.rows:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            raise ValidationDecisionError("validation_decision_payload_invalid")
        ref = payload.get("hypothesis_ref")
        if hypothesis_id is not None and (
            not isinstance(ref, dict) or ref.get("logical_id") != hypothesis_id
        ):
            continue
        if decision is not None and payload.get("decision") != decision:
            continue
        if failure_type is not None and payload.get("failure_type") != failure_type:
            continue
        result.append(dict(row))
    return result


def validate_validation_decision_registry(
    manager: ResearchPathManager,
) -> dict[str, Any]:
    snapshot = read_hash_chained_jsonl_snapshot(
        path=validation_decision_registry_path(manager),
        label=VALIDATION_DECISION_HASH_LABEL,
    )
    reasons = list(snapshot.reasons)
    knowledge_validation = validate_knowledge_registry(manager)
    if knowledge_validation["status"] != "PASS":
        reasons.append("validation_decision_knowledge_registry_invalid")
    identities: set[tuple[str, str]] = set()
    subjects: dict[tuple[str, str, str], str] = {}
    expected_row_fields = {
        "event_id",
        "record_type",
        "logical_id",
        "version",
        "record_hash",
        "hypothesis_outcome_record_hash",
        "hypothesis_outcome_row_hash",
        "payload",
        "sequence",
        "prior_hash",
        "row_hash",
    }
    for index, row in enumerate(snapshot.rows):
        try:
            if set(row) != expected_row_fields:
                raise ValidationDecisionError("row_fields_invalid")
            payload = row.get("payload")
            parsed = _validation_decision_from_dict(payload)
            identity = (str(row.get("logical_id")), str(row.get("version")))
            if identity in identities:
                raise ValidationDecisionError("identity_duplicate")
            identities.add(identity)
            if (
                row.get("event_id")
                != f"decision:{parsed.decision_id}:{parsed.version}"
                or row.get("record_type") != "VALIDATION_DECISION"
                or row.get("logical_id") != parsed.decision_id
                or row.get("version") != parsed.version
                or row.get("record_hash") != parsed.content_hash()
            ):
                raise ValidationDecisionError("row_envelope_mismatch")
            subject = _decision_subject(parsed)
            prior = subjects.get(subject)
            if prior is not None and prior != parsed.content_hash():
                raise ValidationDecisionError("subject_conflict")
            subjects[subject] = parsed.content_hash()
            _require_outcome_binding(manager=manager, row=row, decision=parsed)
            if parsed.terminal_report_ref is not None:
                _validate_terminal_report_ref(
                    manager=manager,
                    decision=parsed,
                    report_ref=parsed.terminal_report_ref,
                )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            reasons.append(
                f"validation_decision_semantic_invalid:{index}:{exc}"
            )
    return {
        "status": "PASS" if not reasons else "FAIL",
        "reasons": sorted(set(reasons)),
        "row_count": snapshot.row_count,
        "stream_hash": snapshot.stream_hash,
        "path": str(validation_decision_registry_path(manager).resolve()),
    }


def _validate_terminal_report(
    *,
    manager: ResearchPathManager,
    manifest: Any,
    run_id: str,
    report: Mapping[str, Any],
    hypothesis: HypothesisSpec,
    manifest_hash: str,
) -> str:
    if not isinstance(report, dict):
        raise ValidationDecisionError("validation_result_report_must_be_dict")
    reported_hash = str(report.get("content_hash") or "")
    if not _SHA256.fullmatch(reported_hash):
        raise ValidationDecisionError("validation_result_content_hash_invalid")
    computed_hash = sha256_prefixed(report_content_hash_payload(dict(report)))
    if reported_hash != computed_hash:
        raise ValidationDecisionError("validation_result_content_hash_mismatch")
    expected = {
        "schema_version": 3,
        "artifact_type": "validated_research_result",
        "experiment_id": str(manifest.experiment_id),
        "run_id": run_id,
        "manifest_hash": manifest_hash,
        "hypothesis_id": hypothesis.hypothesis_id,
        "hypothesis_version": hypothesis.version,
        "hypothesis_contract_hash": hypothesis.contract_hash(),
    }
    mismatches = [key for key, value in expected.items() if report.get(key) != value]
    if mismatches:
        raise ValidationDecisionError(
            "validation_result_authority_binding_mismatch:" + ",".join(mismatches)
        )
    admission_reasons = validation_admission_binding_reasons(report, manager=manager)
    if admission_reasons:
        raise ValidationDecisionError(
            "validation_result_admission_binding_invalid:"
            + ",".join(admission_reasons)
        )
    terminal_result = str(report.get("end_to_end_validation_result") or "")
    if terminal_result == "PASS":
        # Local import avoids making the terminal pipeline depend on its decision
        # projection while still applying the full approval/package validator.
        from .validation_pipeline import validate_validated_research_result

        reasons = validate_validated_research_result(dict(report), manager=manager)
        if reasons:
            raise ValidationDecisionError(
                "validation_result_semantic_invalid:" + ",".join(reasons)
            )
    else:
        blocking = report.get("validation_blocking_reasons")
        stages = report.get("validation_stages")
        if not isinstance(blocking, list) or not blocking:
            raise ValidationDecisionError(
                "nonpassing_validation_result_blocking_reasons_required"
            )
        if not isinstance(stages, list) or not all(
            isinstance(stage, dict) for stage in stages
        ):
            raise ValidationDecisionError(
                "nonpassing_validation_result_stages_required"
            )
    return reported_hash


def _publish_terminal_report_snapshot(
    *,
    manager: ResearchPathManager,
    experiment_id: str,
    run_id: str,
    report: Mapping[str, Any],
    content_hash: str,
) -> TerminalValidationReportRef:
    payload = dict(report)
    snapshot_hash = sha256_prefixed(
        payload,
        label=_TERMINAL_REPORT_SNAPSHOT_HASH_LABEL,
    )
    path = terminal_validation_report_path(
        manager,
        experiment_id=experiment_id,
        run_id=run_id,
        snapshot_hash=snapshot_hash,
    )
    try:
        write_json_atomic_create_or_verify(path, payload)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ValidationDecisionError(
            f"terminal_report_snapshot_publication_failed:{exc}"
        ) from exc
    return TerminalValidationReportRef(
        schema_version=_TERMINAL_REPORT_REF_SCHEMA_VERSION,
        artifact_type="validated_research_result",
        experiment_id=experiment_id,
        run_id=run_id,
        content_hash=content_hash,
        snapshot_hash=snapshot_hash,
        artifact_path=str(path.resolve()),
    )


def _validate_terminal_report_ref(
    *,
    manager: ResearchPathManager,
    decision: ValidationDecision,
    report_ref: TerminalValidationReportRef,
) -> None:
    expected_path = terminal_validation_report_path(
        manager,
        experiment_id=decision.experiment_id,
        run_id=decision.run_id,
        snapshot_hash=report_ref.snapshot_hash,
    ).resolve()
    actual_path = Path(report_ref.artifact_path).expanduser()
    if actual_path.is_symlink() or actual_path.resolve() != expected_path:
        raise ValidationDecisionError("terminal_report_ref_path_mismatch")
    try:
        payload = json.loads(expected_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationDecisionError("terminal_report_ref_unreadable") from exc
    if not isinstance(payload, dict):
        raise ValidationDecisionError("terminal_report_ref_payload_invalid")
    if sha256_prefixed(
        payload, label=_TERMINAL_REPORT_SNAPSHOT_HASH_LABEL
    ) != report_ref.snapshot_hash:
        raise ValidationDecisionError("terminal_report_ref_snapshot_hash_mismatch")
    if (
        sha256_prefixed(report_content_hash_payload(payload))
        != report_ref.content_hash
        or payload.get("content_hash") != report_ref.content_hash
        or payload.get("experiment_id") != decision.experiment_id
        or payload.get("run_id") != decision.run_id
    ):
        raise ValidationDecisionError("terminal_report_ref_content_mismatch")


def _validation_decision_from_dict(value: object) -> ValidationDecision:
    expected_fields = {
        "schema_version",
        "decision_id",
        "version",
        "hypothesis_ref",
        "experiment_id",
        "run_id",
        "decision",
        "criterion_results",
        "evidence_hashes",
        "researcher_interpretation",
        "reviewer_comment",
        "decided_by",
        "decided_at",
        "terminal_report_ref",
        "failure_type",
        "learned",
        "followup_hypothesis_refs",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ValidationDecisionError("validation_decision_payload_fields_invalid")
    raw_criteria = value["criterion_results"]
    if not isinstance(raw_criteria, list):
        raise ValidationDecisionError("validation_decision_criteria_invalid")
    criteria: list[CriterionDecision] = []
    for item in raw_criteria:
        if not isinstance(item, dict) or set(item) != {
            "criterion_id",
            "passed",
            "observed",
            "required",
            "exception",
        }:
            raise ValidationDecisionError("validation_decision_criterion_invalid")
        criteria.append(
            CriterionDecision(
                criterion_id=item["criterion_id"],
                passed=item["passed"],
                observed=item["observed"],
                required=item["required"],
                exception=item["exception"],
            )
        )
    raw_followups = value["followup_hypothesis_refs"]
    if not isinstance(raw_followups, list):
        raise ValidationDecisionError("validation_decision_followups_invalid")
    raw_report_ref = value["terminal_report_ref"]
    report_ref = (
        None
        if raw_report_ref is None
        else _terminal_report_ref_from_dict(raw_report_ref)
    )
    evidence_hashes = value["evidence_hashes"]
    learned = value["learned"]
    if not isinstance(evidence_hashes, list) or not isinstance(learned, list):
        raise ValidationDecisionError("validation_decision_collections_invalid")
    return ValidationDecision(
        schema_version=value["schema_version"],
        decision_id=value["decision_id"],
        version=value["version"],
        hypothesis_ref=knowledge_ref_from_dict(
            value["hypothesis_ref"], context="validation_decision.hypothesis_ref"
        ),
        experiment_id=value["experiment_id"],
        run_id=value["run_id"],
        decision=value["decision"],
        criterion_results=tuple(criteria),
        evidence_hashes=tuple(evidence_hashes),
        researcher_interpretation=value["researcher_interpretation"],
        reviewer_comment=value["reviewer_comment"],
        decided_by=value["decided_by"],
        decided_at=value["decided_at"],
        terminal_report_ref=report_ref,
        failure_type=value["failure_type"],
        learned=tuple(learned),
        followup_hypothesis_refs=tuple(
            knowledge_ref_from_dict(
                item, context="validation_decision.followup_hypothesis_ref"
            )
            for item in raw_followups
        ),
    )


def _terminal_report_ref_from_dict(value: object) -> TerminalValidationReportRef:
    expected = {
        "schema_version",
        "artifact_type",
        "experiment_id",
        "run_id",
        "content_hash",
        "snapshot_hash",
        "artifact_path",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValidationDecisionError("terminal_report_ref_fields_invalid")
    return TerminalValidationReportRef(**value)


def _decision_subject(decision: ValidationDecision) -> tuple[str, str, str]:
    return (
        decision.hypothesis_ref.record_hash,
        decision.experiment_id,
        decision.run_id,
    )


def _existing_subject_decision(
    *, manager: ResearchPathManager, decision: ValidationDecision
) -> dict[str, Any] | None:
    snapshot = read_hash_chained_jsonl_snapshot(
        path=validation_decision_registry_path(manager),
        label=VALIDATION_DECISION_HASH_LABEL,
    )
    if snapshot.status != "PASS":
        raise ValidationDecisionError("validation_decision_registry_invalid")
    matches: list[tuple[dict[str, Any], ValidationDecision]] = []
    for row in snapshot.rows:
        parsed = _validation_decision_from_dict(row.get("payload"))
        if _decision_subject(parsed) == _decision_subject(decision):
            matches.append((dict(row), parsed))
    if not matches:
        return None
    if len(matches) != 1 or matches[0][1].content_hash() != decision.content_hash():
        raise ValidationDecisionError("validation_decision_subject_conflict")
    return matches[0][0]


def _require_outcome_binding(
    *, manager: ResearchPathManager, row: Mapping[str, Any], decision: ValidationDecision
) -> None:
    outcome = get_knowledge_record(
        manager=manager,
        record_type="hypothesis_outcome",
        logical_id=f"outcome:{decision.decision_id}",
        version=decision.version,
    )
    expected_outcome = {
        "VALIDATED": "supported",
        "REJECTED": "rejected",
        "INCONCLUSIVE": "inconclusive",
    }[decision.decision]
    payload = outcome.get("payload")
    if (
        row.get("hypothesis_outcome_record_hash") != outcome.get("record_hash")
        or row.get("hypothesis_outcome_row_hash") != outcome.get("row_hash")
        or not isinstance(payload, dict)
        or payload.get("hypothesis_ref") != decision.hypothesis_ref.as_dict()
        or payload.get("outcome") != expected_outcome
        or payload.get("rationale") != decision.researcher_interpretation
        or payload.get("actor_id") != decision.decided_by
        or payload.get("recorded_at") != decision.decided_at
        or decision.content_hash() not in (payload.get("evidence_hashes") or [])
    ):
        raise ValidationDecisionError("validation_decision_outcome_binding_invalid")


@contextmanager
def _decision_publication_lock(manager: ResearchPathManager) -> Iterator[None]:
    path = validation_decision_registry_path(manager).with_suffix(
        ".jsonl.publication.lock"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    lock_module: Any | None = None
    try:
        try:
            import fcntl
        except ImportError as exc:
            raise RuntimeError("validation_decision_process_lock_unavailable") from exc
        lock_module = fcntl
        lock_module.flock(fd, lock_module.LOCK_EX)
        yield
    finally:
        try:
            if lock_module is not None:
                lock_module.flock(fd, lock_module.LOCK_UN)
        finally:
            os.close(fd)


def _hypothesis_ref(hypothesis: HypothesisSpec) -> KnowledgeRef:
    return KnowledgeRef(
        "hypothesis",
        hypothesis.hypothesis_id,
        hypothesis.version,
        hypothesis.contract_hash(),
    )


def _question_ref(hypothesis: HypothesisSpec) -> KnowledgeRef | None:
    ref = hypothesis.research_question_ref
    if ref is None:
        return None
    return KnowledgeRef(
        "research_question", ref.question_id, ref.version, ref.question_hash
    )


def _require_id(value: str, label: str) -> None:
    if not isinstance(value, str) or not _STABLE_ID.fullmatch(value):
        raise ValidationDecisionError(f"{label}_invalid")


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValidationDecisionError(f"{label}_required")


def _require_hashes(values: tuple[str, ...], label: str) -> None:
    if not values or len(values) != len(set(values)):
        raise ValidationDecisionError(f"{label}_required_or_duplicate")
    if any(not isinstance(value, str) or not _SHA256.fullmatch(value) for value in values):
        raise ValidationDecisionError(f"{label}_invalid")


def _require_timestamp(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationDecisionError(f"{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationDecisionError(f"{label}_timezone_required")


def _require_unique_text(
    values: Iterable[str], label: str, *, required: bool = True
) -> None:
    items = tuple(values)
    if required and not items:
        raise ValidationDecisionError(f"{label}_required")
    if any(not isinstance(item, str) or not item.strip() for item in items):
        raise ValidationDecisionError(f"{label}_invalid")
    if len(items) != len(set(items)):
        raise ValidationDecisionError(f"{label}_duplicate")
