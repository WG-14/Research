"""Fail-closed retention and legal-hold decisions for research evidence.

This module classifies immutable research objects and produces reviewable
deletion eligibility.  It deliberately does not unlink files: deletion is an
Operations action that must consume an unexpired, two-person authorization
bound to the exact subject, policy, evaluation time, and artifact hashes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import re

from .hashing import sha256_prefixed


RETENTION_POLICY_SCHEMA_VERSION = 1
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_REASON = re.compile(r"^[a-z][a-z0-9_:-]{0,127}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class RetentionPolicyError(ValueError):
    """A retention contract or deletion authorization is unsafe."""


def _require_id(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise RetentionPolicyError(f"{field_name}_invalid")


def _require_reason(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _REASON.fullmatch(value) is None:
        raise RetentionPolicyError(f"{field_name}_invalid")


def _require_hash(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise RetentionPolicyError(f"{field_name}_invalid")


def _timestamp(value: str, field_name: str) -> datetime:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise RetentionPolicyError(f"{field_name}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RetentionPolicyError(f"{field_name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RetentionPolicyError(f"{field_name}_timezone_required")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace(
            "+00:00",
            "Z",
        )
    )


def _canonical_ids(values: tuple[str, ...], field_name: str) -> None:
    if values != tuple(sorted(set(values))) or any(
        _ID.fullmatch(value) is None for value in values
    ):
        raise RetentionPolicyError(f"{field_name}_not_canonical")


def _canonical_hashes(values: tuple[str, ...], field_name: str) -> None:
    if not values or values != tuple(sorted(set(values))):
        raise RetentionPolicyError(f"{field_name}_not_canonical")
    for value in values:
        _require_hash(value, field_name)


class ResearchRetentionClass(StrEnum):
    APPROVED_ACTIVE = "APPROVED_ACTIVE"
    AUDIT_EVIDENCE = "AUDIT_EVIDENCE"
    DATASET_INPUT = "DATASET_INPUT"
    EXPLORATORY = "EXPLORATORY"
    FAILED_RUN = "FAILED_RUN"
    OFFICIAL_RELEASE = "OFFICIAL_RELEASE"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class RetentionLifecycle(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class RetentionDecision(StrEnum):
    BLOCKED_ACTIVE_REFERENCE = "BLOCKED_ACTIVE_REFERENCE"
    BLOCKED_LEGAL_HOLD = "BLOCKED_LEGAL_HOLD"
    BLOCKED_LIFECYCLE = "BLOCKED_LIFECYCLE"
    KEEP_MINIMUM_AGE = "KEEP_MINIMUM_AGE"
    KEEP_PERMANENT = "KEEP_PERMANENT"
    REVIEWED_DELETION_ELIGIBLE = "REVIEWED_DELETION_ELIGIBLE"


@dataclass(frozen=True, slots=True)
class RetentionRule:
    retention_class: ResearchRetentionClass
    minimum_age_days: int | None
    require_archived: bool
    preserve_permanently: bool

    def __post_init__(self) -> None:
        if not isinstance(self.retention_class, ResearchRetentionClass):
            raise RetentionPolicyError("retention_rule_class_invalid")
        if self.preserve_permanently:
            if self.minimum_age_days is not None:
                raise RetentionPolicyError("retention_rule_permanent_age_forbidden")
        elif (
            isinstance(self.minimum_age_days, bool)
            or not isinstance(self.minimum_age_days, int)
            or self.minimum_age_days < 1
        ):
            raise RetentionPolicyError("retention_rule_minimum_age_days_invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "retention_class": self.retention_class.value,
            "minimum_age_days": self.minimum_age_days,
            "require_archived": self.require_archived,
            "preserve_permanently": self.preserve_permanently,
        }


@dataclass(frozen=True, slots=True)
class ResearchRetentionPolicy:
    policy_id: str
    version: str
    rules: tuple[RetentionRule, ...]
    maximum_authorization_seconds: int
    legal_hold_enforcement: bool = True
    two_person_deletion_approval: bool = True
    schema_version: int = RETENTION_POLICY_SCHEMA_VERSION
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != RETENTION_POLICY_SCHEMA_VERSION:
            raise RetentionPolicyError("retention_policy_schema_unsupported")
        _require_id(self.policy_id, "retention_policy.policy_id")
        _require_id(self.version, "retention_policy.version")
        classes = tuple(rule.retention_class for rule in self.rules)
        expected = tuple(sorted(ResearchRetentionClass, key=lambda item: item.value))
        if classes != expected:
            raise RetentionPolicyError("retention_policy_rules_must_cover_every_class")
        if (
            isinstance(self.maximum_authorization_seconds, bool)
            or not isinstance(self.maximum_authorization_seconds, int)
            or not 60 <= self.maximum_authorization_seconds <= 86_400
        ):
            raise RetentionPolicyError("retention_policy_authorization_window_invalid")
        if not self.legal_hold_enforcement:
            raise RetentionPolicyError("retention_policy_legal_hold_must_be_enforced")
        if not self.two_person_deletion_approval:
            raise RetentionPolicyError("retention_policy_two_person_approval_required")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="research_retention_policy",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "version": self.version,
            "rules": [rule.as_dict() for rule in self.rules],
            "maximum_authorization_seconds": self.maximum_authorization_seconds,
            "legal_hold_enforcement": self.legal_hold_enforcement,
            "two_person_deletion_approval": self.two_person_deletion_approval,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def rule_for(self, value: ResearchRetentionClass) -> RetentionRule:
        return next(rule for rule in self.rules if rule.retention_class is value)


def standard_research_retention_policy() -> ResearchRetentionPolicy:
    """Return the reviewed baseline; deployments may version stricter values."""

    rules = (
        RetentionRule(
            ResearchRetentionClass.APPROVED_ACTIVE,
            minimum_age_days=None,
            require_archived=True,
            preserve_permanently=True,
        ),
        RetentionRule(
            ResearchRetentionClass.AUDIT_EVIDENCE,
            minimum_age_days=None,
            require_archived=True,
            preserve_permanently=True,
        ),
        RetentionRule(
            ResearchRetentionClass.DATASET_INPUT,
            minimum_age_days=None,
            require_archived=True,
            preserve_permanently=True,
        ),
        RetentionRule(
            ResearchRetentionClass.EXPLORATORY,
            minimum_age_days=180,
            require_archived=True,
            preserve_permanently=False,
        ),
        RetentionRule(
            ResearchRetentionClass.FAILED_RUN,
            minimum_age_days=None,
            require_archived=True,
            preserve_permanently=True,
        ),
        RetentionRule(
            ResearchRetentionClass.OFFICIAL_RELEASE,
            minimum_age_days=None,
            require_archived=True,
            preserve_permanently=True,
        ),
        RetentionRule(
            ResearchRetentionClass.REJECTED,
            minimum_age_days=None,
            require_archived=True,
            preserve_permanently=True,
        ),
        RetentionRule(
            ResearchRetentionClass.SUPERSEDED,
            minimum_age_days=2_555,
            require_archived=True,
            preserve_permanently=False,
        ),
    )
    return ResearchRetentionPolicy(
        policy_id="standard-offline-research-retention",
        version="2",
        rules=tuple(sorted(rules, key=lambda item: item.retention_class.value)),
        maximum_authorization_seconds=3_600,
    )


@dataclass(frozen=True, slots=True)
class RetentionSubject:
    subject_id: str
    subject_version: str
    project_id: str
    retention_class: ResearchRetentionClass
    lifecycle: RetentionLifecycle
    terminal_at: str | None
    artifact_hashes: tuple[str, ...]
    active_reference_ids: tuple[str, ...] = ()
    legal_hold_ids: tuple[str, ...] = ()
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("subject_id", "subject_version", "project_id"):
            _require_id(getattr(self, name), f"retention_subject.{name}")
        if not isinstance(self.retention_class, ResearchRetentionClass):
            raise RetentionPolicyError("retention_subject_class_invalid")
        if not isinstance(self.lifecycle, RetentionLifecycle):
            raise RetentionPolicyError("retention_subject_lifecycle_invalid")
        if self.terminal_at is None:
            if self.lifecycle is not RetentionLifecycle.ACTIVE:
                raise RetentionPolicyError("retention_subject_terminal_at_required")
        else:
            _timestamp(self.terminal_at, "retention_subject.terminal_at")
            if self.lifecycle is RetentionLifecycle.ACTIVE:
                raise RetentionPolicyError(
                    "retention_subject_active_terminal_at_forbidden"
                )
        _canonical_hashes(
            self.artifact_hashes,
            "retention_subject.artifact_hashes",
        )
        _canonical_ids(
            self.active_reference_ids,
            "retention_subject.active_reference_ids",
        )
        _canonical_ids(
            self.legal_hold_ids,
            "retention_subject.legal_hold_ids",
        )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="research_retention_subject",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "subject_id": self.subject_id,
            "subject_version": self.subject_version,
            "project_id": self.project_id,
            "retention_class": self.retention_class.value,
            "lifecycle": self.lifecycle.value,
            "terminal_at": self.terminal_at,
            "artifact_hashes": list(self.artifact_hashes),
            "active_reference_ids": list(self.active_reference_ids),
            "legal_hold_ids": list(self.legal_hold_ids),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class RetentionEvaluation:
    subject_hash: str
    policy_hash: str
    evaluated_at: str
    decision: RetentionDecision
    reason_code: str
    eligible_at: str | None
    blocking_ids: tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_hash(self.subject_hash, "retention_evaluation.subject_hash")
        _require_hash(self.policy_hash, "retention_evaluation.policy_hash")
        _timestamp(self.evaluated_at, "retention_evaluation.evaluated_at")
        if not isinstance(self.decision, RetentionDecision):
            raise RetentionPolicyError("retention_evaluation_decision_invalid")
        _require_reason(self.reason_code, "retention_evaluation.reason_code")
        if self.eligible_at is not None:
            _timestamp(self.eligible_at, "retention_evaluation.eligible_at")
        _canonical_ids(
            self.blocking_ids,
            "retention_evaluation.blocking_ids",
        )
        if self.decision is RetentionDecision.REVIEWED_DELETION_ELIGIBLE and (
            self.eligible_at is None or self.blocking_ids
        ):
            raise RetentionPolicyError("retention_evaluation_eligible_shape_invalid")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="research_retention_evaluation",
            ),
        )

    @property
    def deletion_eligible(self) -> bool:
        return self.decision is RetentionDecision.REVIEWED_DELETION_ELIGIBLE

    def identity_payload(self) -> dict[str, object]:
        return {
            "subject_hash": self.subject_hash,
            "policy_hash": self.policy_hash,
            "evaluated_at": self.evaluated_at,
            "decision": self.decision.value,
            "reason_code": self.reason_code,
            "eligible_at": self.eligible_at,
            "blocking_ids": list(self.blocking_ids),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def evaluate_research_retention(
    *,
    subject: RetentionSubject,
    policy: ResearchRetentionPolicy,
    evaluated_at: str,
) -> RetentionEvaluation:
    """Evaluate one immutable subject without mutating or deleting evidence."""

    if not isinstance(subject, RetentionSubject):
        raise RetentionPolicyError("retention_subject_required")
    if not isinstance(policy, ResearchRetentionPolicy):
        raise RetentionPolicyError("retention_policy_required")
    observed_at = _timestamp(evaluated_at, "retention.evaluated_at")
    rule = policy.rule_for(subject.retention_class)
    eligible_at: datetime | None = None
    if subject.terminal_at is not None and rule.minimum_age_days is not None:
        eligible_at = _timestamp(
            subject.terminal_at,
            "retention_subject.terminal_at",
        ) + timedelta(days=rule.minimum_age_days)
    if subject.legal_hold_ids:
        decision = RetentionDecision.BLOCKED_LEGAL_HOLD
        reason = "legal_hold_active"
        blockers = subject.legal_hold_ids
    elif subject.active_reference_ids:
        decision = RetentionDecision.BLOCKED_ACTIVE_REFERENCE
        reason = "active_lineage_reference"
        blockers = subject.active_reference_ids
    elif rule.preserve_permanently:
        decision = RetentionDecision.KEEP_PERMANENT
        reason = "permanent_evidence_class"
        blockers = ()
    elif rule.require_archived and subject.lifecycle is not RetentionLifecycle.ARCHIVED:
        decision = RetentionDecision.BLOCKED_LIFECYCLE
        reason = "archive_required"
        blockers = ()
    elif eligible_at is None or observed_at < eligible_at:
        decision = RetentionDecision.KEEP_MINIMUM_AGE
        reason = "minimum_retention_age"
        blockers = ()
    else:
        decision = RetentionDecision.REVIEWED_DELETION_ELIGIBLE
        reason = "policy_age_and_guards_satisfied"
        blockers = ()
    return RetentionEvaluation(
        subject_hash=subject.content_hash,
        policy_hash=policy.content_hash,
        evaluated_at=_iso(observed_at),
        decision=decision,
        reason_code=reason,
        eligible_at=None if eligible_at is None else _iso(eligible_at),
        blocking_ids=blockers,
    )


@dataclass(frozen=True, slots=True)
class RetentionDeletionAuthorization:
    operation_id: str
    subject_hash: str
    evaluation_hash: str
    policy_hash: str
    requester_id: str
    requester_assertion_hash: str
    reviewer_id: str
    reviewer_assertion_hash: str
    reason_code: str
    authorized_at: str
    expires_at: str
    artifact_hashes: tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.operation_id, "retention_authorization.operation_id")
        for name in (
            "subject_hash",
            "evaluation_hash",
            "policy_hash",
            "requester_assertion_hash",
            "reviewer_assertion_hash",
        ):
            _require_hash(
                getattr(self, name),
                f"retention_authorization.{name}",
            )
        for name in ("requester_id", "reviewer_id"):
            _require_id(
                getattr(self, name),
                f"retention_authorization.{name}",
            )
        if self.requester_id == self.reviewer_id:
            raise RetentionPolicyError(
                "retention_authorization_two_person_separation_required"
            )
        _require_reason(
            self.reason_code,
            "retention_authorization.reason_code",
        )
        authorized = _timestamp(
            self.authorized_at,
            "retention_authorization.authorized_at",
        )
        expires = _timestamp(
            self.expires_at,
            "retention_authorization.expires_at",
        )
        if expires <= authorized:
            raise RetentionPolicyError("retention_authorization_time_order_invalid")
        _canonical_hashes(
            self.artifact_hashes,
            "retention_authorization.artifact_hashes",
        )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="research_retention_deletion_authorization",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "subject_hash": self.subject_hash,
            "evaluation_hash": self.evaluation_hash,
            "policy_hash": self.policy_hash,
            "requester_id": self.requester_id,
            "requester_assertion_hash": self.requester_assertion_hash,
            "reviewer_id": self.reviewer_id,
            "reviewer_assertion_hash": self.reviewer_assertion_hash,
            "reason_code": self.reason_code,
            "authorized_at": self.authorized_at,
            "expires_at": self.expires_at,
            "artifact_hashes": list(self.artifact_hashes),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def authorize_retention_deletion(
    *,
    operation_id: str,
    subject: RetentionSubject,
    evaluation: RetentionEvaluation,
    policy: ResearchRetentionPolicy,
    requester_id: str,
    requester_assertion_hash: str,
    reviewer_id: str,
    reviewer_assertion_hash: str,
    reason_code: str,
    authorized_at: str,
    expires_at: str,
) -> RetentionDeletionAuthorization:
    """Authorize, but do not execute, deletion of exact reviewed hashes."""

    if evaluation.subject_hash != subject.content_hash:
        raise RetentionPolicyError("retention_authorization_subject_binding_mismatch")
    if evaluation.policy_hash != policy.content_hash:
        raise RetentionPolicyError("retention_authorization_policy_binding_mismatch")
    if not evaluation.deletion_eligible:
        raise RetentionPolicyError("retention_authorization_subject_not_eligible")
    authorized = _timestamp(authorized_at, "retention.authorized_at")
    evaluated = _timestamp(evaluation.evaluated_at, "retention.evaluated_at")
    expires = _timestamp(expires_at, "retention.expires_at")
    if authorized < evaluated:
        raise RetentionPolicyError("retention_authorization_predates_evaluation")
    if expires - authorized > timedelta(seconds=policy.maximum_authorization_seconds):
        raise RetentionPolicyError("retention_authorization_window_exceeds_policy")
    return RetentionDeletionAuthorization(
        operation_id=operation_id,
        subject_hash=subject.content_hash,
        evaluation_hash=evaluation.content_hash,
        policy_hash=policy.content_hash,
        requester_id=requester_id,
        requester_assertion_hash=requester_assertion_hash,
        reviewer_id=reviewer_id,
        reviewer_assertion_hash=reviewer_assertion_hash,
        reason_code=reason_code,
        authorized_at=_iso(authorized),
        expires_at=_iso(expires),
        artifact_hashes=subject.artifact_hashes,
    )


def validate_retention_deletion_authorization(
    *,
    authorization: RetentionDeletionAuthorization,
    subject: RetentionSubject,
    evaluation: RetentionEvaluation,
    policy: ResearchRetentionPolicy,
    now: str,
) -> None:
    """Fail before an Operations deleter acts on stale or changed evidence."""

    if (
        authorization.subject_hash != subject.content_hash
        or authorization.evaluation_hash != evaluation.content_hash
        or authorization.policy_hash != policy.content_hash
        or authorization.artifact_hashes != subject.artifact_hashes
        or not evaluation.deletion_eligible
    ):
        raise RetentionPolicyError("retention_authorization_binding_invalid")
    observed = _timestamp(now, "retention_authorization.now")
    authorized = _timestamp(
        authorization.authorized_at,
        "retention_authorization.authorized_at",
    )
    expires = _timestamp(
        authorization.expires_at,
        "retention_authorization.expires_at",
    )
    if observed < authorized or observed > expires:
        raise RetentionPolicyError("retention_authorization_not_current")


__all__ = [
    "RETENTION_POLICY_SCHEMA_VERSION",
    "ResearchRetentionClass",
    "ResearchRetentionPolicy",
    "RetentionDecision",
    "RetentionDeletionAuthorization",
    "RetentionEvaluation",
    "RetentionLifecycle",
    "RetentionPolicyError",
    "RetentionRule",
    "RetentionSubject",
    "authorize_retention_deletion",
    "evaluate_research_retention",
    "standard_research_retention_policy",
    "validate_retention_deletion_authorization",
]
