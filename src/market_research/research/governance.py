"""Append-only research lifecycle governance contracts.

This module owns hypothesis and strategy-candidate state.  Manifest
classification and automated validation results are evidence for transitions;
they are not authoritative lifecycle state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from market_research.paths import ResearchPathManager
from market_research.storage_io import append_jsonl

from .hash_chain import (
    HashChainSnapshot,
    append_hash_chained_jsonl,
    mutate_hash_chained_jsonl_atomic,
    read_hash_chained_jsonl_snapshot,
)
from .hashing import content_hash_payload, sha256_prefixed


GOVERNANCE_SCHEMA_VERSION = 1
GOVERNANCE_HASH_LABEL = "research_governance"


class GovernanceError(ValueError):
    pass


class GovernanceSubjectType(str, Enum):
    HYPOTHESIS = "hypothesis"
    STRATEGY_CANDIDATE = "strategy_candidate"


class HypothesisLifecycleState(str, Enum):
    IDEA = "IDEA"
    HYPOTHESIS_DEFINED = "HYPOTHESIS_DEFINED"
    EXPLORING = "EXPLORING"
    VALIDATING = "VALIDATING"
    SUPPORTED = "SUPPORTED"
    REJECTED = "REJECTED"
    ARCHIVED = "ARCHIVED"


class StrategyCandidateLifecycleState(str, Enum):
    DRAFT = "DRAFT"
    BACKTESTED = "BACKTESTED"
    ROBUSTNESS_PASSED = "ROBUSTNESS_PASSED"
    OUT_OF_SAMPLE_PASSED = "OUT_OF_SAMPLE_PASSED"
    RESEARCH_APPROVED = "RESEARCH_APPROVED"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"


class HumanReviewDecision(str, Enum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    REJECTED = "REJECTED"


_HYPOTHESIS_TRANSITIONS = {
    HypothesisLifecycleState.IDEA: frozenset(
        {HypothesisLifecycleState.HYPOTHESIS_DEFINED}
    ),
    HypothesisLifecycleState.HYPOTHESIS_DEFINED: frozenset(
        {
            HypothesisLifecycleState.EXPLORING,
            HypothesisLifecycleState.REJECTED,
            HypothesisLifecycleState.ARCHIVED,
        }
    ),
    HypothesisLifecycleState.EXPLORING: frozenset(
        {
            HypothesisLifecycleState.VALIDATING,
            HypothesisLifecycleState.REJECTED,
            HypothesisLifecycleState.ARCHIVED,
        }
    ),
    HypothesisLifecycleState.VALIDATING: frozenset(
        {
            HypothesisLifecycleState.EXPLORING,
            HypothesisLifecycleState.SUPPORTED,
            HypothesisLifecycleState.REJECTED,
        }
    ),
    HypothesisLifecycleState.SUPPORTED: frozenset({HypothesisLifecycleState.ARCHIVED}),
    HypothesisLifecycleState.REJECTED: frozenset({HypothesisLifecycleState.ARCHIVED}),
    HypothesisLifecycleState.ARCHIVED: frozenset(),
}

_STRATEGY_TRANSITIONS = {
    StrategyCandidateLifecycleState.DRAFT: frozenset(
        {
            StrategyCandidateLifecycleState.BACKTESTED,
            StrategyCandidateLifecycleState.REJECTED,
            StrategyCandidateLifecycleState.RETIRED,
        }
    ),
    StrategyCandidateLifecycleState.BACKTESTED: frozenset(
        {
            StrategyCandidateLifecycleState.ROBUSTNESS_PASSED,
            StrategyCandidateLifecycleState.REJECTED,
            StrategyCandidateLifecycleState.RETIRED,
        }
    ),
    StrategyCandidateLifecycleState.ROBUSTNESS_PASSED: frozenset(
        {
            StrategyCandidateLifecycleState.OUT_OF_SAMPLE_PASSED,
            StrategyCandidateLifecycleState.REJECTED,
            StrategyCandidateLifecycleState.RETIRED,
        }
    ),
    StrategyCandidateLifecycleState.OUT_OF_SAMPLE_PASSED: frozenset(
        {
            StrategyCandidateLifecycleState.RESEARCH_APPROVED,
            StrategyCandidateLifecycleState.REJECTED,
            StrategyCandidateLifecycleState.RETIRED,
        }
    ),
    StrategyCandidateLifecycleState.RESEARCH_APPROVED: frozenset(
        {StrategyCandidateLifecycleState.RETIRED}
    ),
    StrategyCandidateLifecycleState.REJECTED: frozenset(
        {StrategyCandidateLifecycleState.RETIRED}
    ),
    StrategyCandidateLifecycleState.RETIRED: frozenset(),
}

_REQUIRED_EVIDENCE = {
    HypothesisLifecycleState.IDEA.value: frozenset({"hypothesis_semantic_fingerprint"}),
    HypothesisLifecycleState.HYPOTHESIS_DEFINED.value: frozenset(
        {"hypothesis_contract_hash"}
    ),
    HypothesisLifecycleState.VALIDATING.value: frozenset({"validation_manifest_hash"}),
    HypothesisLifecycleState.SUPPORTED.value: frozenset({"validation_report_hash"}),
    StrategyCandidateLifecycleState.BACKTESTED.value: frozenset(
        {"backtest_report_hash"}
    ),
    StrategyCandidateLifecycleState.ROBUSTNESS_PASSED.value: frozenset(
        {"stress_suite_hash"}
    ),
    StrategyCandidateLifecycleState.OUT_OF_SAMPLE_PASSED.value: frozenset(
        {"final_holdout_confirmation_hash"}
    ),
    StrategyCandidateLifecycleState.RESEARCH_APPROVED.value: frozenset(
        {
            "human_review_hash",
            "source_report_hash",
        }
    ),
}


@dataclass(frozen=True, slots=True)
class GovernanceSubject:
    subject_type: GovernanceSubjectType
    subject_id: str
    subject_version: str

    def __post_init__(self) -> None:
        if not self.subject_id.strip() or not self.subject_version.strip():
            raise GovernanceError("governance_subject_identity_required")

    def as_dict(self) -> dict[str, str]:
        return {
            "subject_type": self.subject_type.value,
            "subject_id": self.subject_id,
            "subject_version": self.subject_version,
        }


class _AppendStore:
    @staticmethod
    def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
        append_jsonl(path, payload)


def governance_registry_path(manager: ResearchPathManager) -> Path:
    return manager.artifact_path("reports", "research", "_registry", "governance.jsonl")


def load_governance_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise GovernanceError("governance_row_must_be_object")
        rows.append(value)
    return rows


def current_lifecycle_state(
    *, manager: ResearchPathManager, subject: GovernanceSubject
) -> str | None:
    rows = load_governance_rows(governance_registry_path(manager))
    matching = [row for row in rows if _row_matches_subject(row, subject)]
    return str(matching[-1]["to_state"]) if matching else None


def append_lifecycle_transition(
    *,
    manager: ResearchPathManager,
    subject: GovernanceSubject,
    from_state: str | None,
    to_state: str,
    actor_id: str,
    reason: str,
    evidence_hashes: Mapping[str, str] | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Validate and append one lifecycle transition with optimistic locking."""

    return _append_lifecycle_transition(
        manager=manager,
        subject=subject,
        from_state=from_state,
        to_state=to_state,
        actor_id=actor_id,
        reason=reason,
        evidence_hashes=evidence_hashes,
        recorded_at=recorded_at,
        approval_authorized=False,
    )


def _append_lifecycle_transition(
    *,
    manager: ResearchPathManager,
    subject: GovernanceSubject,
    from_state: str | None,
    to_state: str,
    actor_id: str,
    reason: str,
    evidence_hashes: Mapping[str, str] | None,
    recorded_at: str | None,
    approval_authorized: bool,
) -> dict[str, Any]:

    actor = actor_id.strip()
    rationale = reason.strip()
    if not actor or not rationale:
        raise GovernanceError("governance_transition_actor_and_reason_required")
    evidence = dict(evidence_hashes or {})
    _validate_hashes(evidence)
    path = governance_registry_path(manager)
    try:
        snapshot = read_hash_chained_jsonl_snapshot(
            path=path,
            label=GOVERNANCE_HASH_LABEL,
        )
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise GovernanceError("governance_hash_chain_invalid") from exc
    chain = snapshot.as_validation()
    if chain["status"] != "PASS":
        raise GovernanceError("governance_hash_chain_invalid")
    rows = list(snapshot.rows)
    actual_state = next(
        (
            str(row["to_state"])
            for row in reversed(rows)
            if _row_matches_subject(row, subject)
        ),
        None,
    )
    if actual_state != from_state:
        raise GovernanceError(
            f"governance_state_conflict:{actual_state!s}!={from_state!s}"
        )
    if subject.subject_type is GovernanceSubjectType.HYPOTHESIS and from_state is None:
        fingerprint = evidence.get("hypothesis_semantic_fingerprint")
        duplicate = next(
            (
                row
                for row in rows
                if row.get("event_type") == "lifecycle_transition"
                and row.get("subject_type") == GovernanceSubjectType.HYPOTHESIS.value
                and row.get("from_state") is None
                and (row.get("evidence_hashes") or {}).get(
                    "hypothesis_semantic_fingerprint"
                )
                == fingerprint
                and row.get("subject_id") != subject.subject_id
            ),
            None,
        )
        if duplicate is not None:
            raise GovernanceError(
                "hypothesis_semantic_duplicate:"
                + str(duplicate.get("subject_id") or "unknown")
            )
    _validate_transition(
        subject.subject_type,
        from_state,
        to_state,
        evidence,
        approval_authorized=approval_authorized,
    )
    timestamp = recorded_at or datetime.now(timezone.utc).isoformat()
    _require_timezone(timestamp)
    payload = {
        "schema_version": GOVERNANCE_SCHEMA_VERSION,
        "event_type": "lifecycle_transition",
        **subject.as_dict(),
        "from_state": from_state,
        "to_state": to_state,
        "actor_id": actor,
        "reason": rationale,
        "evidence_hashes": dict(sorted(evidence.items())),
        "recorded_at": timestamp,
    }
    try:
        return append_hash_chained_jsonl(
            store=_AppendStore(),
            path=path,
            payload=payload,
            label=GOVERNANCE_HASH_LABEL,
            expected_stream_hash=chain["stream_hash"],
        )
    except ValueError as exc:
        raise GovernanceError(str(exc)) from exc


def append_human_review(
    *,
    manager: ResearchPathManager,
    subject: GovernanceSubject,
    decision: HumanReviewDecision | str,
    reviewer_id: str,
    reviewer_role: str,
    rationale: str,
    reviewed_artifact_hash: str,
    requested_changes: tuple[Mapping[str, str], ...] = (),
    resolved_requirement_ids: tuple[str, ...] = (),
    decided_at: str | None = None,
) -> dict[str, Any]:
    """Append a human judgment separately from automated lifecycle evidence."""

    try:
        normalized_decision = HumanReviewDecision(decision)
    except ValueError as exc:
        raise GovernanceError("human_review_decision_unknown") from exc
    reviewer = reviewer_id.strip()
    role = reviewer_role.strip()
    reason = rationale.strip()
    if not reviewer or not role or not reason:
        raise GovernanceError("human_review_identity_role_and_rationale_required")
    _validate_hashes({"reviewed_artifact_hash": reviewed_artifact_hash})
    changes = _normalize_requested_changes(requested_changes)
    resolved = tuple(str(item).strip() for item in resolved_requirement_ids)
    if any(not item for item in resolved) or len(set(resolved)) != len(resolved):
        raise GovernanceError("human_review_resolved_requirement_ids_invalid")
    if normalized_decision is HumanReviewDecision.CHANGES_REQUESTED and not changes:
        raise GovernanceError("human_review_changes_requested_requires_items")
    if normalized_decision is not HumanReviewDecision.CHANGES_REQUESTED and changes:
        raise GovernanceError("human_review_requested_changes_not_allowed_for_decision")
    if normalized_decision is HumanReviewDecision.APPROVED:
        raise GovernanceError(
            "human_review_approved_requires_candidate_approval_service"
        )
    if normalized_decision is not HumanReviewDecision.APPROVED and resolved:
        raise GovernanceError("human_review_resolutions_allowed_only_for_approval")
    timestamp = decided_at or datetime.now(timezone.utc).isoformat()
    _require_timezone(timestamp)

    def mutation(
        snapshot: HashChainSnapshot,
        stage: Any,
    ) -> dict[str, Any]:
        rows = list(snapshot.rows)
        actual_state = _current_state_from_rows(rows, subject)
        if (
            subject.subject_type is GovernanceSubjectType.STRATEGY_CANDIDATE
            and actual_state
            in {
                StrategyCandidateLifecycleState.RESEARCH_APPROVED.value,
                StrategyCandidateLifecycleState.RETIRED.value,
            }
        ):
            raise GovernanceError(
                "human_review_candidate_lifecycle_not_reviewable:" + str(actual_state)
            )
        return stage(
            {
                "schema_version": GOVERNANCE_SCHEMA_VERSION,
                "event_type": "human_review_decision",
                **subject.as_dict(),
                "decision": normalized_decision.value,
                "reviewer_id": reviewer,
                "reviewer_role": role,
                "rationale": reason,
                "reviewed_artifact_hash": reviewed_artifact_hash,
                "requested_changes": list(changes),
                "resolved_requirement_ids": list(resolved),
                "decided_at": timestamp,
            }
        )

    try:
        return mutate_hash_chained_jsonl_atomic(
            path=governance_registry_path(manager),
            label=GOVERNANCE_HASH_LABEL,
            mutation=mutation,
        ).value
    except (RuntimeError, ValueError) as exc:
        raise GovernanceError(str(exc)) from exc


def approve_strategy_candidate(
    *,
    manager: ResearchPathManager,
    subject: GovernanceSubject,
    hypothesis_subject: GovernanceSubject,
    hypothesis_contract_hash: str,
    strategy_name: str,
    strategy_version: str,
    strategy_plugin_contract_hash: str,
    effective_strategy_parameters_hash: str,
    source_report_hash: str,
    final_holdout_confirmation_hash: str,
    reviewer_id: str,
    rationale: str,
    resolved_requirement_ids: tuple[str, ...] = (),
    decided_at: str | None = None,
    approval_request_id: str | None = None,
    prohibited_actor_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Atomically record approval review and guarded lifecycle transition."""

    if subject.subject_type is not GovernanceSubjectType.STRATEGY_CANDIDATE:
        raise GovernanceError("strategy_approval_requires_strategy_candidate")
    if hypothesis_subject.subject_type is not GovernanceSubjectType.HYPOTHESIS:
        raise GovernanceError("strategy_approval_requires_hypothesis_subject")
    reviewer = reviewer_id.strip()
    reason = rationale.strip()
    if not reviewer or not reason:
        raise GovernanceError("strategy_approval_reviewer_and_rationale_required")
    resolved = tuple(str(item).strip() for item in resolved_requirement_ids)
    if any(not item for item in resolved) or len(set(resolved)) != len(resolved):
        raise GovernanceError("human_review_resolved_requirement_ids_invalid")
    prohibited = frozenset(str(item).strip() for item in prohibited_actor_ids)
    if any(not item for item in prohibited):
        raise GovernanceError("governance_prohibited_actor_ids_invalid")
    if decided_at is not None:
        _require_timezone(decided_at)
    _validate_hashes(
        {
            "source_report_hash": source_report_hash,
            "final_holdout_confirmation_hash": final_holdout_confirmation_hash,
            "hypothesis_contract_hash": hypothesis_contract_hash,
        }
    )
    _validate_hashes(
        {
            "strategy_plugin_contract_hash": strategy_plugin_contract_hash,
            "effective_strategy_parameters_hash": effective_strategy_parameters_hash,
        }
    )
    if not strategy_name.strip() or not strategy_version.strip():
        raise GovernanceError("strategy_approval_strategy_identity_required")
    semantic_request = {
        "schema_version": 1,
        "subject": subject.as_dict(),
        "hypothesis_subject": hypothesis_subject.as_dict(),
        "hypothesis_contract_hash": hypothesis_contract_hash,
        "strategy_name": strategy_name.strip(),
        "strategy_version": strategy_version.strip(),
        "strategy_plugin_contract_hash": strategy_plugin_contract_hash,
        "effective_strategy_parameters_hash": effective_strategy_parameters_hash,
        "source_report_hash": source_report_hash,
        "final_holdout_confirmation_hash": final_holdout_confirmation_hash,
        "reviewer_id": reviewer,
        "rationale": reason,
        "resolved_requirement_ids": list(resolved),
        "decided_at": decided_at,
    }
    request_hash = sha256_prefixed(
        content_hash_payload(semantic_request),
        label="strategy_approval_request",
    )
    request_id = str(approval_request_id or request_hash).strip()
    if not request_id or len(request_id) > 255:
        raise GovernanceError("strategy_approval_request_id_invalid")

    def mutation(snapshot: HashChainSnapshot, stage: Any) -> dict[str, Any]:
        rows = list(snapshot.rows)
        if reviewer in prohibited:
            raise GovernanceError("governance_separation_of_duties_violation")
        if _unpaired_approved_review_hashes(rows):
            raise GovernanceError("strategy_approval_orphan_review_present")
        if _review_exists_after_candidate_approval(rows):
            raise GovernanceError("strategy_approval_registry_review_after_approval")
        replay = _approval_pair_for_request(rows, request_id)
        if replay is not None:
            review, transition = replay
            if (
                review.get("approval_request_hash") != request_hash
                or transition.get("approval_request_hash") != request_hash
            ):
                raise GovernanceError("strategy_approval_idempotency_conflict")
            hypothesis_supported = _hypothesis_supported_row(
                rows,
                hypothesis_subject,
                source_report_hash,
            )
            return _strategy_approval_artifact(
                manager=manager,
                subject=subject,
                hypothesis_subject=hypothesis_subject,
                hypothesis_contract_hash=hypothesis_contract_hash,
                hypothesis_supported=hypothesis_supported,
                strategy_name=strategy_name,
                strategy_version=strategy_version,
                strategy_plugin_contract_hash=strategy_plugin_contract_hash,
                effective_strategy_parameters_hash=effective_strategy_parameters_hash,
                source_report_hash=source_report_hash,
                final_holdout_confirmation_hash=final_holdout_confirmation_hash,
                reviewer=reviewer,
                rationale=reason,
                request_id=request_id,
                request_hash=request_hash,
                review=review,
                transition=transition,
            )
        if any(row.get("approval_request_id") == request_id for row in rows):
            raise GovernanceError("strategy_approval_idempotency_partial_commit")
        state = _current_state_from_rows(rows, subject)
        if state != StrategyCandidateLifecycleState.OUT_OF_SAMPLE_PASSED.value:
            raise GovernanceError("strategy_approval_requires_out_of_sample_passed")
        if (
            _current_state_from_rows(rows, hypothesis_subject)
            != HypothesisLifecycleState.SUPPORTED.value
        ):
            raise GovernanceError("strategy_approval_requires_supported_hypothesis")
        out_of_sample = _latest_transition_to(
            rows,
            subject,
            StrategyCandidateLifecycleState.OUT_OF_SAMPLE_PASSED.value,
        )
        if (
            out_of_sample is None
            or (out_of_sample.get("evidence_hashes") or {}).get(
                "final_holdout_confirmation_hash"
            )
            != final_holdout_confirmation_hash
        ):
            raise GovernanceError("strategy_approval_holdout_evidence_mismatch")
        hypothesis_defined = _latest_transition_to(
            rows,
            hypothesis_subject,
            HypothesisLifecycleState.HYPOTHESIS_DEFINED.value,
        )
        if (
            hypothesis_defined is None
            or (hypothesis_defined.get("evidence_hashes") or {}).get(
                "hypothesis_contract_hash"
            )
            != hypothesis_contract_hash
        ):
            raise GovernanceError("strategy_approval_hypothesis_contract_mismatch")
        hypothesis_supported = _hypothesis_supported_row(
            rows,
            hypothesis_subject,
            source_report_hash,
        )
        if any(
            row.get("event_type") == "human_review_decision"
            and _row_matches_identity(row, subject)
            and row.get("reviewed_artifact_hash") == source_report_hash
            and row.get("reviewer_id") == reviewer
            for row in rows
        ):
            raise GovernanceError("governance_prior_reviewer_cannot_approve")
        outstanding = _outstanding_requirement_ids(rows, subject)
        if set(resolved) != outstanding:
            raise GovernanceError(
                "human_review_unresolved_requirements:"
                + ",".join(sorted(outstanding - set(resolved)))
            )
        timestamp = decided_at or datetime.now(timezone.utc).isoformat()
        _require_timezone(timestamp)
        review = stage(
            {
                "schema_version": GOVERNANCE_SCHEMA_VERSION,
                "event_type": "human_review_decision",
                **subject.as_dict(),
                "decision": HumanReviewDecision.APPROVED.value,
                "reviewer_id": reviewer,
                "reviewer_role": "research_approver",
                "rationale": reason,
                "reviewed_artifact_hash": source_report_hash,
                "requested_changes": [],
                "resolved_requirement_ids": list(resolved),
                "approval_request_id": request_id,
                "approval_request_hash": request_hash,
                "decided_at": timestamp,
            }
        )
        transition = stage(
            {
                "schema_version": GOVERNANCE_SCHEMA_VERSION,
                "event_type": "lifecycle_transition",
                **subject.as_dict(),
                "from_state": StrategyCandidateLifecycleState.OUT_OF_SAMPLE_PASSED.value,
                "to_state": StrategyCandidateLifecycleState.RESEARCH_APPROVED.value,
                "actor_id": reviewer,
                "reason": reason,
                "evidence_hashes": {
                    "human_review_hash": str(review["row_hash"]),
                    "source_report_hash": source_report_hash,
                },
                "approval_request_id": request_id,
                "approval_request_hash": request_hash,
                "recorded_at": timestamp,
            }
        )
        return _strategy_approval_artifact(
            manager=manager,
            subject=subject,
            hypothesis_subject=hypothesis_subject,
            hypothesis_contract_hash=hypothesis_contract_hash,
            hypothesis_supported=hypothesis_supported,
            strategy_name=strategy_name,
            strategy_version=strategy_version,
            strategy_plugin_contract_hash=strategy_plugin_contract_hash,
            effective_strategy_parameters_hash=effective_strategy_parameters_hash,
            source_report_hash=source_report_hash,
            final_holdout_confirmation_hash=final_holdout_confirmation_hash,
            reviewer=reviewer,
            rationale=reason,
            request_id=request_id,
            request_hash=request_hash,
            review=review,
            transition=transition,
        )

    try:
        return mutate_hash_chained_jsonl_atomic(
            path=governance_registry_path(manager),
            label=GOVERNANCE_HASH_LABEL,
            mutation=mutation,
        ).value
    except (RuntimeError, ValueError) as exc:
        raise GovernanceError(str(exc)) from exc


def _strategy_approval_artifact(
    *,
    manager: ResearchPathManager,
    subject: GovernanceSubject,
    hypothesis_subject: GovernanceSubject,
    hypothesis_contract_hash: str,
    hypothesis_supported: Mapping[str, Any],
    strategy_name: str,
    strategy_version: str,
    strategy_plugin_contract_hash: str,
    effective_strategy_parameters_hash: str,
    source_report_hash: str,
    final_holdout_confirmation_hash: str,
    reviewer: str,
    rationale: str,
    request_id: str,
    request_hash: str,
    review: Mapping[str, Any],
    transition: Mapping[str, Any],
) -> dict[str, Any]:
    material = {
        "schema_version": 1,
        "artifact_type": "strategy_research_approval",
        **subject.as_dict(),
        "approved_state": StrategyCandidateLifecycleState.RESEARCH_APPROVED.value,
        "source_report_hash": source_report_hash,
        "final_holdout_confirmation_hash": final_holdout_confirmation_hash,
        "reviewer_id": reviewer,
        "rationale": rationale,
        "approved_at": review["decided_at"],
        "review_row_hash": review["row_hash"],
        "transition_row_hash": transition["row_hash"],
        "approval_request_id": request_id,
        "approval_request_hash": request_hash,
        "governance_registry_path": str(governance_registry_path(manager).resolve()),
        "hypothesis_id": hypothesis_subject.subject_id,
        "hypothesis_version": hypothesis_subject.subject_version,
        "hypothesis_contract_hash": hypothesis_contract_hash,
        "hypothesis_supported_transition_row_hash": hypothesis_supported["row_hash"],
        "strategy_name": strategy_name.strip(),
        "strategy_version": strategy_version.strip(),
        "strategy_plugin_contract_hash": strategy_plugin_contract_hash,
        "effective_strategy_parameters_hash": effective_strategy_parameters_hash,
    }
    return {
        **material,
        "content_hash": sha256_prefixed(content_hash_payload(material)),
    }


def _approval_pair_for_request(
    rows: list[dict[str, Any]],
    request_id: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    matching = [row for row in rows if row.get("approval_request_id") == request_id]
    if not matching:
        return None
    if len(matching) != 2:
        raise GovernanceError("strategy_approval_idempotency_partial_commit")
    reviews = [
        row
        for row in matching
        if row.get("event_type") == "human_review_decision"
        and row.get("decision") == HumanReviewDecision.APPROVED.value
    ]
    transitions = [
        row
        for row in matching
        if row.get("event_type") == "lifecycle_transition"
        and row.get("to_state")
        == StrategyCandidateLifecycleState.RESEARCH_APPROVED.value
    ]
    if len(reviews) != 1 or len(transitions) != 1:
        raise GovernanceError("strategy_approval_idempotency_partial_commit")
    review = reviews[0]
    transition = transitions[0]
    if (transition.get("evidence_hashes") or {}).get("human_review_hash") != review.get(
        "row_hash"
    ) or _row_subject_key(review) != _row_subject_key(transition):
        raise GovernanceError("strategy_approval_idempotency_pair_invalid")
    return review, transition


def _current_state_from_rows(
    rows: list[dict[str, Any]],
    subject: GovernanceSubject,
) -> str | None:
    transition = next(
        (
            row
            for row in reversed(rows)
            if row.get("event_type") == "lifecycle_transition"
            and _row_matches_identity(row, subject)
        ),
        None,
    )
    return str(transition.get("to_state")) if transition is not None else None


def _latest_transition_to(
    rows: list[dict[str, Any]],
    subject: GovernanceSubject,
    state: str,
) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in reversed(rows)
            if row.get("event_type") == "lifecycle_transition"
            and _row_matches_identity(row, subject)
            and row.get("to_state") == state
        ),
        None,
    )


def _hypothesis_supported_row(
    rows: list[dict[str, Any]],
    subject: GovernanceSubject,
    source_report_hash: str,
) -> dict[str, Any]:
    row = _latest_transition_to(
        rows,
        subject,
        HypothesisLifecycleState.SUPPORTED.value,
    )
    if (
        row is None
        or (row.get("evidence_hashes") or {}).get("validation_report_hash")
        != source_report_hash
    ):
        raise GovernanceError("strategy_approval_hypothesis_evidence_mismatch")
    return row


def _unpaired_approved_review_hashes(rows: list[dict[str, Any]]) -> set[str]:
    referenced = {
        str((row.get("evidence_hashes") or {}).get("human_review_hash") or "")
        for row in rows
        if row.get("event_type") == "lifecycle_transition"
        and row.get("to_state")
        == StrategyCandidateLifecycleState.RESEARCH_APPROVED.value
    }
    return {
        str(row.get("row_hash") or "")
        for row in rows
        if row.get("event_type") == "human_review_decision"
        and row.get("decision") == HumanReviewDecision.APPROVED.value
        and row.get("row_hash") not in referenced
    }


def _review_exists_after_candidate_approval(rows: list[dict[str, Any]]) -> bool:
    states: dict[tuple[Any, Any, Any], str] = {}
    for row in rows:
        key = _row_subject_key(row)
        if row.get("event_type") == "lifecycle_transition":
            states[key] = str(row.get("to_state") or "")
        elif row.get("event_type") == "human_review_decision" and states.get(key) in {
            StrategyCandidateLifecycleState.RESEARCH_APPROVED.value,
            StrategyCandidateLifecycleState.RETIRED.value,
        }:
            return True
    return False


def validate_strategy_approval(
    approval: object,
    *,
    source_report_hash: str,
    selected_candidate_id: str,
    final_holdout_confirmation_hash: str,
    hypothesis_id: str,
    hypothesis_version: str,
    hypothesis_contract_hash: str,
    strategy_name: str,
    strategy_version: str,
    strategy_plugin_contract_hash: str,
    effective_strategy_parameters_hash: str,
    expected_registry_path: Path | None = None,
) -> list[str]:
    """Validate an approval artifact against the authoritative governance log.

    ``expected_registry_path`` lets production consumers bind the approval to
    the repository's canonical governance registry.  It remains optional for
    compatibility with callers that validate a self-contained artifact, but a
    caller with a :class:`ResearchPathManager` should always provide it.
    """

    if not isinstance(approval, dict):
        return ["strategy_approval_missing"]
    reasons: list[str] = []
    material = {key: value for key, value in approval.items() if key != "content_hash"}
    if approval.get("content_hash") != sha256_prefixed(content_hash_payload(material)):
        reasons.append("strategy_approval_content_hash_mismatch")
    if (
        approval.get("artifact_type") != "strategy_research_approval"
        or approval.get("schema_version") != 1
    ):
        reasons.append("strategy_approval_schema_invalid")
    if approval.get("subject_type") != GovernanceSubjectType.STRATEGY_CANDIDATE.value:
        reasons.append("strategy_approval_subject_type_mismatch")
    if approval.get("subject_id") != selected_candidate_id:
        reasons.append("strategy_approval_candidate_mismatch")
    if approval.get("source_report_hash") != source_report_hash:
        reasons.append("strategy_approval_source_report_mismatch")
    if (
        approval.get("final_holdout_confirmation_hash")
        != final_holdout_confirmation_hash
    ):
        reasons.append("strategy_approval_holdout_evidence_mismatch")
    if (
        approval.get("approved_state")
        != StrategyCandidateLifecycleState.RESEARCH_APPROVED.value
    ):
        reasons.append("strategy_approval_state_mismatch")
    if (
        approval.get("hypothesis_id") != hypothesis_id
        or approval.get("hypothesis_version") != hypothesis_version
    ):
        reasons.append("strategy_approval_hypothesis_identity_mismatch")
    if approval.get("hypothesis_contract_hash") != hypothesis_contract_hash:
        reasons.append("strategy_approval_hypothesis_contract_mismatch")
    approval_request_id = approval.get("approval_request_id")
    approval_request_hash = approval.get("approval_request_hash")
    if approval_request_id is not None or approval_request_hash is not None:
        if (
            not isinstance(approval_request_id, str)
            or not approval_request_id
            or not isinstance(approval_request_hash, str)
        ):
            reasons.append("strategy_approval_request_binding_invalid")
        else:
            try:
                _validate_hashes({"approval_request_hash": approval_request_hash})
            except GovernanceError:
                reasons.append("strategy_approval_request_binding_invalid")
    for field, expected in (
        ("strategy_name", strategy_name),
        ("strategy_version", strategy_version),
        ("strategy_plugin_contract_hash", strategy_plugin_contract_hash),
        ("effective_strategy_parameters_hash", effective_strategy_parameters_hash),
    ):
        if approval.get(field) != expected:
            reasons.append(f"strategy_approval_{field}_mismatch")
    path_value = str(approval.get("governance_registry_path") or "")
    if not path_value:
        return sorted(set(reasons + ["strategy_approval_registry_missing"]))
    path = Path(path_value).expanduser()
    if (
        expected_registry_path is not None
        and path.resolve() != expected_registry_path.expanduser().resolve()
    ):
        return sorted(set(reasons + ["strategy_approval_registry_path_mismatch"]))
    try:
        snapshot = read_hash_chained_jsonl_snapshot(
            path=path,
            label=GOVERNANCE_HASH_LABEL,
        )
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError):
        return sorted(set(reasons + ["strategy_approval_registry_invalid"]))
    chain = snapshot.as_validation()
    if chain["status"] != "PASS":
        return sorted(set(reasons + ["strategy_approval_registry_invalid"]))
    rows = list(snapshot.rows)
    review = next(
        (row for row in rows if row.get("row_hash") == approval.get("review_row_hash")),
        None,
    )
    transition = next(
        (
            row
            for row in rows
            if row.get("row_hash") == approval.get("transition_row_hash")
        ),
        None,
    )
    hypothesis_transition = next(
        (
            row
            for row in rows
            if row.get("row_hash")
            == approval.get("hypothesis_supported_transition_row_hash")
        ),
        None,
    )
    expected_subject = (
        approval.get("subject_type"),
        approval.get("subject_id"),
        approval.get("subject_version"),
    )
    if not isinstance(review, dict):
        reasons.append("strategy_approval_review_missing")
    else:
        if (
            review.get("event_type") != "human_review_decision"
            or review.get("decision") != "APPROVED"
        ):
            reasons.append("strategy_approval_review_invalid")
        if review.get("reviewed_artifact_hash") != source_report_hash:
            reasons.append("strategy_approval_review_report_mismatch")
        if _row_subject_key(review) != expected_subject:
            reasons.append("strategy_approval_review_subject_mismatch")
        if approval_request_id is not None and (
            review.get("approval_request_id") != approval_request_id
            or review.get("approval_request_hash") != approval_request_hash
        ):
            reasons.append("strategy_approval_review_request_mismatch")
    if not isinstance(transition, dict):
        reasons.append("strategy_approval_transition_missing")
    else:
        if (
            transition.get("event_type") != "lifecycle_transition"
            or transition.get("to_state") != "RESEARCH_APPROVED"
        ):
            reasons.append("strategy_approval_transition_invalid")
        if transition.get("from_state") != "OUT_OF_SAMPLE_PASSED":
            reasons.append("strategy_approval_transition_invalid")
        evidence = transition.get("evidence_hashes") or {}
        if (
            evidence.get("human_review_hash") != approval.get("review_row_hash")
            or evidence.get("source_report_hash") != source_report_hash
        ):
            reasons.append("strategy_approval_transition_evidence_mismatch")
        if _row_subject_key(transition) != expected_subject:
            reasons.append("strategy_approval_transition_subject_mismatch")
        if approval_request_id is not None and (
            transition.get("approval_request_id") != approval_request_id
            or transition.get("approval_request_hash") != approval_request_hash
        ):
            reasons.append("strategy_approval_transition_request_mismatch")
    matching_transitions = [
        row
        for row in rows
        if row.get("event_type") == "lifecycle_transition"
        and _row_subject_key(row) == expected_subject
    ]
    if (
        not matching_transitions
        or matching_transitions[-1].get("to_state") != "RESEARCH_APPROVED"
    ):
        reasons.append("strategy_approval_not_current")
    out_of_sample = next(
        (
            row
            for row in reversed(matching_transitions)
            if row.get("to_state") == "OUT_OF_SAMPLE_PASSED"
        ),
        None,
    )
    if (
        not isinstance(out_of_sample, dict)
        or (out_of_sample.get("evidence_hashes") or {}).get(
            "final_holdout_confirmation_hash"
        )
        != final_holdout_confirmation_hash
    ):
        reasons.append("strategy_approval_holdout_evidence_mismatch")
    if not isinstance(hypothesis_transition, dict):
        reasons.append("strategy_approval_hypothesis_transition_missing")
    else:
        if (
            hypothesis_transition.get("subject_type")
            != GovernanceSubjectType.HYPOTHESIS.value
            or hypothesis_transition.get("subject_id") != hypothesis_id
            or hypothesis_transition.get("subject_version") != hypothesis_version
            or hypothesis_transition.get("to_state")
            != HypothesisLifecycleState.SUPPORTED.value
            or (hypothesis_transition.get("evidence_hashes") or {}).get(
                "validation_report_hash"
            )
            != source_report_hash
        ):
            reasons.append("strategy_approval_hypothesis_transition_invalid")
        hypothesis_rows = [
            row
            for row in rows
            if row.get("event_type") == "lifecycle_transition"
            and row.get("subject_type") == GovernanceSubjectType.HYPOTHESIS.value
            and row.get("subject_id") == hypothesis_id
            and row.get("subject_version") == hypothesis_version
        ]
        if (
            not hypothesis_rows
            or hypothesis_rows[-1].get("to_state")
            != HypothesisLifecycleState.SUPPORTED.value
        ):
            reasons.append("strategy_approval_hypothesis_not_current")
    return sorted(set(reasons))


def validate_governance_registry(manager: ResearchPathManager) -> dict[str, Any]:
    path = governance_registry_path(manager)
    try:
        snapshot = read_hash_chained_jsonl_snapshot(
            path=path,
            label=GOVERNANCE_HASH_LABEL,
        )
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        return {
            "status": "FAIL",
            "reasons": [f"governance_registry_invalid:{type(exc).__name__}"],
            "row_count": 0,
            "stream_hash": None,
            "subject_count": 0,
            "path": str(path.resolve()),
        }
    chain = snapshot.as_validation()
    reasons = list(chain["reasons"])
    states: dict[tuple[str, str, str], str] = {}
    rows = list(snapshot.rows)
    outstanding_by_subject: dict[tuple[str, str, str], set[str]] = {}
    approved_reviews: dict[str, tuple[int, Mapping[str, Any]]] = {}
    approval_review_references: dict[str, int] = {}
    for index, row in enumerate(rows):
        try:
            if row.get("schema_version") != GOVERNANCE_SCHEMA_VERSION:
                raise GovernanceError("schema_version_unsupported")
            subject_type = GovernanceSubjectType(str(row["subject_type"]))
            key = (
                subject_type.value,
                str(row["subject_id"]),
                str(row["subject_version"]),
            )
            if row.get("event_type") == "lifecycle_transition":
                expected_from = states.get(key)
                evidence = row.get("evidence_hashes")
                if not isinstance(evidence, dict):
                    raise GovernanceError("evidence_hashes_invalid")
                _validate_transition(
                    subject_type, expected_from, str(row["to_state"]), evidence
                )
                if row.get("from_state") != expected_from:
                    raise GovernanceError("from_state_mismatch")
                if (
                    not str(row.get("actor_id") or "").strip()
                    or not str(row.get("reason") or "").strip()
                ):
                    raise GovernanceError("actor_and_reason_required")
                _require_timezone(str(row.get("recorded_at") or ""))
                if (
                    subject_type is GovernanceSubjectType.STRATEGY_CANDIDATE
                    and row.get("to_state")
                    == StrategyCandidateLifecycleState.RESEARCH_APPROVED.value
                ):
                    _validate_approval_transition_reference(
                        row,
                        key=key,
                        approved_reviews=approved_reviews,
                    )
                    review_hash = str(
                        (row.get("evidence_hashes") or {}).get("human_review_hash")
                    )
                    approval_review_references[review_hash] = (
                        approval_review_references.get(review_hash, 0) + 1
                    )
                states[key] = str(row["to_state"])
            elif row.get("event_type") == "human_review_decision":
                if states.get(key) in {
                    StrategyCandidateLifecycleState.RESEARCH_APPROVED.value,
                    StrategyCandidateLifecycleState.RETIRED.value,
                }:
                    raise GovernanceError("review_after_candidate_approval")
                _validate_review_row(row, outstanding_by_subject.setdefault(key, set()))
                if row.get("decision") == HumanReviewDecision.APPROVED.value:
                    review_hash = str(row.get("row_hash") or "")
                    if not review_hash or review_hash in approved_reviews:
                        raise GovernanceError("approval_review_hash_duplicate")
                    approved_reviews[review_hash] = (index, row)
            else:
                raise GovernanceError("event_type_unknown")
        except (KeyError, TypeError, ValueError, GovernanceError) as exc:
            reasons.append(f"lifecycle_event_invalid:{index}:{exc}")
    for review_hash, (index, _row) in approved_reviews.items():
        if approval_review_references.get(review_hash) != 1:
            reasons.append(f"approval_review_unpaired:{index}")
    for key, outstanding in outstanding_by_subject.items():
        if outstanding and states.get(key) in {
            StrategyCandidateLifecycleState.RESEARCH_APPROVED.value,
            StrategyCandidateLifecycleState.RETIRED.value,
        }:
            reasons.append(
                "approved_candidate_has_outstanding_requirements:" + ":".join(key)
            )
    return {
        **chain,
        "status": "PASS" if not reasons else "FAIL",
        "reasons": sorted(set(reasons)),
        "subject_count": len(states),
        "path": str(path.resolve()),
    }


def _validate_approval_transition_reference(
    transition: Mapping[str, Any],
    *,
    key: tuple[str, str, str],
    approved_reviews: Mapping[str, tuple[int, Mapping[str, Any]]],
) -> None:
    evidence = transition.get("evidence_hashes") or {}
    review_hash = str(evidence.get("human_review_hash") or "")
    matched = approved_reviews.get(review_hash)
    if matched is None:
        raise GovernanceError("approval_transition_review_missing")
    _index, review = matched
    if (
        _row_subject_key(review) != key
        or review.get("reviewer_id") != transition.get("actor_id")
        or review.get("reviewed_artifact_hash") != evidence.get("source_report_hash")
    ):
        raise GovernanceError("approval_transition_review_binding_invalid")
    review_request_id = review.get("approval_request_id")
    transition_request_id = transition.get("approval_request_id")
    review_request_hash = review.get("approval_request_hash")
    transition_request_hash = transition.get("approval_request_hash")
    if any(
        value is not None
        for value in (
            review_request_id,
            transition_request_id,
            review_request_hash,
            transition_request_hash,
        )
    ) and (
        not isinstance(review_request_id, str)
        or not review_request_id
        or review_request_id != transition_request_id
        or not isinstance(review_request_hash, str)
        or review_request_hash != transition_request_hash
    ):
        raise GovernanceError("approval_transition_request_binding_invalid")


def _validate_transition(
    subject_type: GovernanceSubjectType,
    from_state: str | None,
    to_state: str,
    evidence: Mapping[str, str],
    *,
    approval_authorized: bool = True,
) -> None:
    if subject_type is GovernanceSubjectType.HYPOTHESIS:
        enum = HypothesisLifecycleState
        transitions = _HYPOTHESIS_TRANSITIONS
        initial = HypothesisLifecycleState.IDEA
    else:
        enum = StrategyCandidateLifecycleState
        transitions = _STRATEGY_TRANSITIONS
        initial = StrategyCandidateLifecycleState.DRAFT
    try:
        target = enum(to_state)
        source = enum(from_state) if from_state is not None else None
    except ValueError as exc:
        raise GovernanceError("governance_state_unknown") from exc
    if (
        target is StrategyCandidateLifecycleState.RESEARCH_APPROVED
        and not approval_authorized
    ):
        raise GovernanceError("governance_research_approval_requires_approval_service")
    if source is None:
        if target is not initial:
            raise GovernanceError(f"governance_initial_state_must_be:{initial.value}")
    elif target not in transitions[source]:
        raise GovernanceError(
            f"governance_transition_not_allowed:{source.value}->{target.value}"
        )
    required = _REQUIRED_EVIDENCE.get(target.value, frozenset())
    missing = sorted(required - set(evidence))
    if missing:
        raise GovernanceError(
            "governance_transition_evidence_missing:" + ",".join(missing)
        )


def _validate_hashes(values: Mapping[str, str]) -> None:
    invalid = sorted(
        key
        for key, value in values.items()
        if not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in value[7:])
    )
    if invalid:
        raise GovernanceError("governance_evidence_hash_invalid:" + ",".join(invalid))


def _normalize_requested_changes(
    changes: tuple[Mapping[str, str], ...],
) -> tuple[dict[str, str], ...]:
    normalized: list[dict[str, str]] = []
    for item in changes:
        requirement_id = str(item.get("requirement_id") or "").strip()
        description = str(item.get("description") or "").strip()
        verification = str(item.get("verification_condition") or "").strip()
        if not requirement_id or not description or not verification:
            raise GovernanceError("human_review_requested_change_fields_required")
        normalized.append(
            {
                "requirement_id": requirement_id,
                "description": description,
                "verification_condition": verification,
            }
        )
    ids = [item["requirement_id"] for item in normalized]
    if len(ids) != len(set(ids)):
        raise GovernanceError("human_review_requested_change_ids_duplicate")
    return tuple(normalized)


def _outstanding_requirement_ids(
    rows: list[dict[str, Any]], subject: GovernanceSubject
) -> set[str]:
    outstanding: set[str] = set()
    for row in rows:
        if row.get(
            "event_type"
        ) != "human_review_decision" or not _row_matches_identity(row, subject):
            continue
        if row.get("decision") == "CHANGES_REQUESTED":
            outstanding.update(
                str(item.get("requirement_id"))
                for item in row.get("requested_changes") or []
                if isinstance(item, dict)
            )
        elif row.get("decision") == "APPROVED":
            outstanding.difference_update(
                str(item) for item in row.get("resolved_requirement_ids") or []
            )
    return outstanding


def _validate_review_row(row: Mapping[str, Any], outstanding: set[str]) -> None:
    decision = HumanReviewDecision(str(row.get("decision") or ""))
    if (
        not str(row.get("reviewer_id") or "").strip()
        or not str(row.get("reviewer_role") or "").strip()
    ):
        raise GovernanceError("reviewer_identity_missing")
    if not str(row.get("rationale") or "").strip():
        raise GovernanceError("review_rationale_missing")
    _validate_hashes(
        {"reviewed_artifact_hash": str(row.get("reviewed_artifact_hash") or "")}
    )
    _require_timezone(str(row.get("decided_at") or ""))
    changes = _normalize_requested_changes(tuple(row.get("requested_changes") or ()))
    resolved = {str(item) for item in row.get("resolved_requirement_ids") or []}
    if decision is HumanReviewDecision.CHANGES_REQUESTED:
        if not changes:
            raise GovernanceError("review_changes_missing")
        outstanding.update(item["requirement_id"] for item in changes)
    elif decision is HumanReviewDecision.APPROVED:
        if row.get("reviewer_role") != "research_approver" or resolved != outstanding:
            raise GovernanceError("review_approval_requirements_invalid")
        outstanding.clear()
    elif changes or resolved:
        raise GovernanceError("review_fields_invalid_for_decision")


def _row_subject_key(row: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    return row.get("subject_type"), row.get("subject_id"), row.get("subject_version")


def _row_matches_identity(row: Mapping[str, Any], subject: GovernanceSubject) -> bool:
    return _row_subject_key(row) == (
        subject.subject_type.value,
        subject.subject_id,
        subject.subject_version,
    )


def _require_timezone(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise GovernanceError("governance_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GovernanceError("governance_timestamp_timezone_required")


def _row_matches_subject(row: Mapping[str, Any], subject: GovernanceSubject) -> bool:
    return (
        row.get("event_type") == "lifecycle_transition"
        and row.get("subject_type") == subject.subject_type.value
        and row.get("subject_id") == subject.subject_id
        and row.get("subject_version") == subject.subject_version
    )
