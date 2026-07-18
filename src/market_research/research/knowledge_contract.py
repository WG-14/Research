"""Typed immutable contracts for cross-experiment research knowledge.

The contracts in this module deliberately contain no persistence or adapter
logic.  :mod:`knowledge_registry` is the single append-only authority for
publishing them outside the repository.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .hashing import sha256_prefixed


KNOWLEDGE_CONTRACT_SCHEMA_VERSION = 1
DECISION_RECORD_SCHEMA_VERSION = 1

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_STABLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_RECORD_TYPES = frozenset(
    {
        "observation",
        "research_question",
        "hypothesis",
        "research_note",
        "literature",
        "hypothesis_outcome",
        "preregistration",
        "decision",
        "ai_advisory",
        "ai_advisory_review",
    }
)
_NOTE_TYPES = frozenset(
    {"research_note", "negative_result", "failed_experiment", "open_question"}
)
_NOTE_STATUSES = frozenset({"active", "superseded", "withdrawn"})
_HYPOTHESIS_OUTCOMES = frozenset(
    {"supported", "rejected", "failed", "inconclusive", "archived"}
)
_RISK_SEVERITIES = frozenset({"low", "medium", "high", "critical"})
_APPROVER_TYPES = frozenset({"human", "policy"})
RESEARCH_NOTE_AUTHORITY_SUBJECT_TYPES = frozenset(
    {
        "dataset",
        "feature",
        "experiment",
        "run",
        "strategy",
        "regime",
        "research_trade",
    }
)
_ADMISSION_STATUSES = frozenset(
    {
        "VALIDATION_FROZEN_AT_ADMISSION",
        "FORMAL_PREREGISTERED_EXTERNAL_EVIDENCE",
    }
)
_AI_TASK_TYPES = frozenset(
    {
        "research_summary",
        "related_hypothesis_search",
        "code_draft",
        "result_comparison",
        "counterargument",
        "failure_classification",
        "report_draft",
    }
)


class KnowledgeContractError(ValueError):
    """A knowledge contract is incomplete or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class KnowledgeRef:
    """Immutable reference to one version in the knowledge authority."""

    record_type: str
    logical_id: str
    version: str
    record_hash: str

    def __post_init__(self) -> None:
        _require_record_type(self.record_type)
        _require_stable_id(self.logical_id, "knowledge_ref.logical_id")
        _require_stable_id(self.version, "knowledge_ref.version")
        _require_hash(self.record_hash, "knowledge_ref.record_hash")

    def as_dict(self) -> dict[str, str]:
        return {
            "record_type": self.record_type,
            "logical_id": self.logical_id,
            "version": self.version,
            "record_hash": self.record_hash,
        }


@dataclass(frozen=True, slots=True)
class AuthorityRef:
    """Versioned reference to a subject owned by another local authority."""

    authority: str
    subject_type: str
    subject_id: str
    subject_version: str
    authority_hash: str

    def __post_init__(self) -> None:
        _require_stable_id(self.authority, "authority_ref.authority")
        _require_stable_id(self.subject_type, "authority_ref.subject_type")
        _require_stable_id(self.subject_id, "authority_ref.subject_id")
        _require_stable_id(self.subject_version, "authority_ref.subject_version")
        _require_hash(self.authority_hash, "authority_ref.authority_hash")

    def as_dict(self) -> dict[str, str]:
        return {
            "authority": self.authority,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "subject_version": self.subject_version,
            "authority_hash": self.authority_hash,
        }


@dataclass(frozen=True, slots=True)
class ResearchNoteSpec:
    schema_version: int
    note_id: str
    version: str
    note_type: str
    title: str
    body: str
    actor_id: str
    recorded_at: str
    status: str
    references: tuple[KnowledgeRef, ...] = ()
    evidence_hashes: tuple[str, ...] = ()
    authority_refs: tuple[AuthorityRef, ...] = ()

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, "research_note")
        _require_stable_id(self.note_id, "research_note.note_id")
        _require_stable_id(self.version, "research_note.version")
        if self.note_type not in _NOTE_TYPES:
            raise KnowledgeContractError("research_note.note_type_unknown")
        _require_text(self.title, "research_note.title")
        _require_text(self.body, "research_note.body")
        _require_text(self.actor_id, "research_note.actor_id")
        _require_timestamp(self.recorded_at, "research_note.recorded_at")
        if self.status not in _NOTE_STATUSES:
            raise KnowledgeContractError("research_note.status_unknown")
        _require_unique_refs(self.references, "research_note.references")
        _require_hashes(self.evidence_hashes, "research_note.evidence_hashes")
        validate_research_note_authority_refs(self.authority_refs)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "note_id": self.note_id,
            "version": self.version,
            "note_type": self.note_type,
            "title": self.title,
            "body": self.body,
            "actor_id": self.actor_id,
            "recorded_at": self.recorded_at,
            "status": self.status,
            "references": [item.as_dict() for item in self.references],
            "evidence_hashes": list(self.evidence_hashes),
        }
        # Omit an empty extension so schema-1 notes created before typed
        # cross-authority references retain their exact serialization/hash.
        if self.authority_refs:
            payload["authority_refs"] = [item.as_dict() for item in self.authority_refs]
        return payload

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict())

    def ref(self) -> KnowledgeRef:
        return KnowledgeRef(
            "research_note", self.note_id, self.version, self.contract_hash()
        )


@dataclass(frozen=True, slots=True)
class LiteratureSpec:
    schema_version: int
    literature_id: str
    version: str
    title: str
    citation: str
    actor_id: str
    recorded_at: str
    source_uri: str | None = None
    source_content_hash: str | None = None
    references: tuple[KnowledgeRef, ...] = ()

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, "literature")
        _require_stable_id(self.literature_id, "literature.literature_id")
        _require_stable_id(self.version, "literature.version")
        _require_text(self.title, "literature.title")
        _require_text(self.citation, "literature.citation")
        _require_text(self.actor_id, "literature.actor_id")
        _require_timestamp(self.recorded_at, "literature.recorded_at")
        if self.source_uri is not None:
            _require_text(self.source_uri, "literature.source_uri")
        if self.source_content_hash is not None:
            _require_hash(self.source_content_hash, "literature.source_content_hash")
        _require_unique_refs(self.references, "literature.references")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "literature_id": self.literature_id,
            "version": self.version,
            "title": self.title,
            "citation": self.citation,
            "actor_id": self.actor_id,
            "recorded_at": self.recorded_at,
            "source_uri": self.source_uri,
            "source_content_hash": self.source_content_hash,
            "references": [item.as_dict() for item in self.references],
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict())

    def ref(self) -> KnowledgeRef:
        return KnowledgeRef(
            "literature", self.literature_id, self.version, self.contract_hash()
        )


@dataclass(frozen=True, slots=True)
class HypothesisOutcomeSpec:
    schema_version: int
    outcome_id: str
    version: str
    hypothesis_ref: KnowledgeRef
    outcome: str
    rationale: str
    actor_id: str
    recorded_at: str
    evidence_hashes: tuple[str, ...]
    question_ref: KnowledgeRef | None = None

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, "hypothesis_outcome")
        _require_stable_id(self.outcome_id, "hypothesis_outcome.outcome_id")
        _require_stable_id(self.version, "hypothesis_outcome.version")
        if self.hypothesis_ref.record_type != "hypothesis":
            raise KnowledgeContractError("hypothesis_outcome.hypothesis_ref_invalid")
        if (
            self.question_ref is not None
            and self.question_ref.record_type != "research_question"
        ):
            raise KnowledgeContractError("hypothesis_outcome.question_ref_invalid")
        if self.outcome not in _HYPOTHESIS_OUTCOMES:
            raise KnowledgeContractError("hypothesis_outcome.outcome_unknown")
        _require_text(self.rationale, "hypothesis_outcome.rationale")
        _require_text(self.actor_id, "hypothesis_outcome.actor_id")
        _require_timestamp(self.recorded_at, "hypothesis_outcome.recorded_at")
        _require_hashes(
            self.evidence_hashes, "hypothesis_outcome.evidence_hashes", required=True
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "outcome_id": self.outcome_id,
            "version": self.version,
            "hypothesis_ref": self.hypothesis_ref.as_dict(),
            "question_ref": self.question_ref.as_dict() if self.question_ref else None,
            "outcome": self.outcome,
            "rationale": self.rationale,
            "actor_id": self.actor_id,
            "recorded_at": self.recorded_at,
            "evidence_hashes": list(self.evidence_hashes),
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict())

    def ref(self) -> KnowledgeRef:
        return KnowledgeRef(
            "hypothesis_outcome", self.outcome_id, self.version, self.contract_hash()
        )


@dataclass(frozen=True, slots=True)
class PreregistrationRecord:
    schema_version: int
    registration_id: str
    version: str
    experiment_id: str
    manifest_hash: str
    hypothesis_ref: KnowledgeRef
    component_hashes: tuple[tuple[str, str], ...]
    admission_status: str
    actor_id: str
    frozen_at: str
    external_registration_evidence_hash: str | None = None

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, "preregistration")
        _require_stable_id(self.registration_id, "preregistration.registration_id")
        _require_stable_id(self.version, "preregistration.version")
        _require_stable_id(self.experiment_id, "preregistration.experiment_id")
        _require_hash(self.manifest_hash, "preregistration.manifest_hash")
        if self.hypothesis_ref.record_type != "hypothesis":
            raise KnowledgeContractError("preregistration.hypothesis_ref_invalid")
        if not self.component_hashes:
            raise KnowledgeContractError("preregistration.component_hashes_required")
        names = [name for name, _value in self.component_hashes]
        if any(not _STABLE_ID_PATTERN.fullmatch(name) for name in names) or len(
            set(names)
        ) != len(names):
            raise KnowledgeContractError("preregistration.component_hash_names_invalid")
        for name, value in self.component_hashes:
            _require_hash(value, f"preregistration.component_hashes.{name}")
        if tuple(sorted(self.component_hashes)) != self.component_hashes:
            raise KnowledgeContractError("preregistration.component_hashes_not_sorted")
        if self.admission_status not in _ADMISSION_STATUSES:
            raise KnowledgeContractError("preregistration.admission_status_unknown")
        _require_text(self.actor_id, "preregistration.actor_id")
        _require_timestamp(self.frozen_at, "preregistration.frozen_at")
        if self.external_registration_evidence_hash is not None:
            _require_hash(
                self.external_registration_evidence_hash,
                "preregistration.external_registration_evidence_hash",
            )
        if (
            self.admission_status == "FORMAL_PREREGISTERED_EXTERNAL_EVIDENCE"
            and self.external_registration_evidence_hash is None
        ):
            raise KnowledgeContractError("preregistration.external_evidence_required")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "registration_id": self.registration_id,
            "version": self.version,
            "experiment_id": self.experiment_id,
            "manifest_hash": self.manifest_hash,
            "hypothesis_ref": self.hypothesis_ref.as_dict(),
            "component_hashes": {name: value for name, value in self.component_hashes},
            "admission_status": self.admission_status,
            "actor_id": self.actor_id,
            "frozen_at": self.frozen_at,
            "external_registration_evidence_hash": self.external_registration_evidence_hash,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict())

    def ref(self) -> KnowledgeRef:
        return KnowledgeRef(
            "preregistration", self.registration_id, self.version, self.contract_hash()
        )


@dataclass(frozen=True, slots=True)
class DecisionAlternative:
    alternative_id: str
    description: str
    rejection_reason: str

    def __post_init__(self) -> None:
        _require_stable_id(self.alternative_id, "decision.alternative_id")
        _require_text(self.description, "decision.alternative.description")
        _require_text(self.rejection_reason, "decision.alternative.rejection_reason")

    def as_dict(self) -> dict[str, str]:
        return {
            "alternative_id": self.alternative_id,
            "description": self.description,
            "rejection_reason": self.rejection_reason,
        }


@dataclass(frozen=True, slots=True)
class DecisionRisk:
    risk_id: str
    description: str
    severity: str
    mitigation: str

    def __post_init__(self) -> None:
        _require_stable_id(self.risk_id, "decision.risk_id")
        _require_text(self.description, "decision.risk.description")
        if self.severity not in _RISK_SEVERITIES:
            raise KnowledgeContractError("decision.risk.severity_unknown")
        _require_text(self.mitigation, "decision.risk.mitigation")

    def as_dict(self) -> dict[str, str]:
        return {
            "risk_id": self.risk_id,
            "description": self.description,
            "severity": self.severity,
            "mitigation": self.mitigation,
        }


@dataclass(frozen=True, slots=True)
class DecisionApprover:
    approver_type: str
    approver_id: str
    role: str

    def __post_init__(self) -> None:
        if self.approver_type not in _APPROVER_TYPES:
            raise KnowledgeContractError("decision.approver_type_unknown")
        _require_text(self.approver_id, "decision.approver_id")
        _require_text(self.role, "decision.approver.role")

    def as_dict(self) -> dict[str, str]:
        return {
            "approver_type": self.approver_type,
            "approver_id": self.approver_id,
            "role": self.role,
        }


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """One immutable, evidence-bound material research decision."""

    schema_version: int
    decision_id: str
    version: str
    decision_type: str
    subject: AuthorityRef
    chosen_action: str
    rationale: str
    evidence_hashes: tuple[str, ...]
    alternatives: tuple[DecisionAlternative, ...]
    expected_effects: tuple[str, ...]
    risks: tuple[DecisionRisk, ...]
    proposer_ids: tuple[str, ...]
    approver: DecisionApprover
    policy_version: str
    decided_at: str
    supersedes: KnowledgeRef | None = None

    def __post_init__(self) -> None:
        if self.schema_version != DECISION_RECORD_SCHEMA_VERSION:
            raise KnowledgeContractError("decision.schema_version_unsupported")
        _require_stable_id(self.decision_id, "decision.decision_id")
        _require_stable_id(self.version, "decision.version")
        _require_stable_id(self.decision_type, "decision.decision_type")
        _require_text(self.chosen_action, "decision.chosen_action")
        _require_text(self.rationale, "decision.rationale")
        _require_hashes(self.evidence_hashes, "decision.evidence_hashes", required=True)
        if not self.alternatives:
            raise KnowledgeContractError("decision.alternatives_required")
        if len({item.alternative_id for item in self.alternatives}) != len(
            self.alternatives
        ):
            raise KnowledgeContractError("decision.alternatives_duplicate")
        _require_texts(
            self.expected_effects, "decision.expected_effects", required=True
        )
        if not self.risks:
            raise KnowledgeContractError("decision.risks_required")
        if len({item.risk_id for item in self.risks}) != len(self.risks):
            raise KnowledgeContractError("decision.risks_duplicate")
        _require_texts(self.proposer_ids, "decision.proposer_ids", required=True)
        if len(set(self.proposer_ids)) != len(self.proposer_ids):
            raise KnowledgeContractError("decision.proposer_ids_duplicate")
        if (
            self.approver.approver_type == "human"
            and self.approver.approver_id in self.proposer_ids
        ):
            raise KnowledgeContractError("decision.approver_separation_violation")
        _require_stable_id(self.policy_version, "decision.policy_version")
        _require_timestamp(self.decided_at, "decision.decided_at")
        if self.supersedes is not None and self.supersedes.record_type != "decision":
            raise KnowledgeContractError("decision.supersedes_ref_invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "version": self.version,
            "decision_type": self.decision_type,
            "subject": self.subject.as_dict(),
            "chosen_action": self.chosen_action,
            "rationale": self.rationale,
            "evidence_hashes": list(self.evidence_hashes),
            "alternatives": [item.as_dict() for item in self.alternatives],
            "expected_effects": list(self.expected_effects),
            "risks": [item.as_dict() for item in self.risks],
            "proposer_ids": list(self.proposer_ids),
            "approver": self.approver.as_dict(),
            "policy_version": self.policy_version,
            "decided_at": self.decided_at,
            "supersedes": self.supersedes.as_dict() if self.supersedes else None,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict())

    def ref(self) -> KnowledgeRef:
        return KnowledgeRef(
            "decision", self.decision_id, self.version, self.contract_hash()
        )


@dataclass(frozen=True, slots=True)
class AIAdvisorySpec:
    """Immutable AI output that has no domain mutation or approval authority."""

    schema_version: int
    advisory_id: str
    version: str
    task_type: str
    generator_id: str
    provider_id: str
    model_id: str
    model_configuration_hash: str
    prompt_hash: str
    source_refs: tuple[KnowledgeRef, ...]
    source_authority_refs: tuple[AuthorityRef, ...]
    output_text: str
    generated_at: str
    review_status: str = "pending_human_review"
    authority_scope: str = "advisory_only_no_domain_mutation"

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, "ai_advisory")
        _require_stable_id(self.advisory_id, "ai_advisory.advisory_id")
        _require_stable_id(self.version, "ai_advisory.version")
        if self.task_type not in _AI_TASK_TYPES:
            raise KnowledgeContractError("ai_advisory.task_type_unknown")
        for context, value in (
            ("generator_id", self.generator_id),
            ("provider_id", self.provider_id),
            ("model_id", self.model_id),
        ):
            _require_stable_id(value, f"ai_advisory.{context}")
        _require_hash(
            self.model_configuration_hash,
            "ai_advisory.model_configuration_hash",
        )
        _require_hash(self.prompt_hash, "ai_advisory.prompt_hash")
        if not self.source_refs and not self.source_authority_refs:
            raise KnowledgeContractError("ai_advisory.sources_required")
        _require_unique_refs(self.source_refs, "ai_advisory.source_refs")
        validate_research_note_authority_refs(self.source_authority_refs)
        _require_text(self.output_text, "ai_advisory.output_text")
        _require_timestamp(self.generated_at, "ai_advisory.generated_at")
        if self.review_status != "pending_human_review":
            raise KnowledgeContractError("ai_advisory_cannot_self_approve")
        if self.authority_scope != "advisory_only_no_domain_mutation":
            raise KnowledgeContractError("ai_advisory.authority_scope_invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "advisory_id": self.advisory_id,
            "version": self.version,
            "task_type": self.task_type,
            "generator_id": self.generator_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_configuration_hash": self.model_configuration_hash,
            "prompt_hash": self.prompt_hash,
            "source_refs": [item.as_dict() for item in self.source_refs],
            "source_authority_refs": [
                item.as_dict() for item in self.source_authority_refs
            ],
            "output_text": self.output_text,
            "output_hash": sha256_prefixed(
                {"output_text": self.output_text}, label="ai_advisory_output"
            ),
            "generated_at": self.generated_at,
            "review_status": self.review_status,
            "authority_scope": self.authority_scope,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict())

    def ref(self) -> KnowledgeRef:
        return KnowledgeRef(
            "ai_advisory", self.advisory_id, self.version, self.contract_hash()
        )


@dataclass(frozen=True, slots=True)
class AIAdvisoryReview:
    """Human disposition of an advisory, never strategy or execution approval."""

    schema_version: int
    review_id: str
    version: str
    advisory_ref: KnowledgeRef
    reviewer_id: str
    reviewer_role: str
    decision: str
    rationale: str
    evidence_hashes: tuple[str, ...]
    reviewed_at: str
    authority_scope: str = "advisory_output_only"
    reviewer_type: str = "human"

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, "ai_advisory_review")
        _require_stable_id(self.review_id, "ai_advisory_review.review_id")
        _require_stable_id(self.version, "ai_advisory_review.version")
        if self.advisory_ref.record_type != "ai_advisory":
            raise KnowledgeContractError("ai_advisory_review.advisory_ref_invalid")
        _require_text(self.reviewer_id, "ai_advisory_review.reviewer_id")
        _require_text(self.reviewer_role, "ai_advisory_review.reviewer_role")
        if self.decision not in {"accepted_as_advisory", "rejected", "needs_revision"}:
            raise KnowledgeContractError("ai_advisory_review.decision_unknown")
        _require_text(self.rationale, "ai_advisory_review.rationale")
        _require_hashes(
            self.evidence_hashes,
            "ai_advisory_review.evidence_hashes",
            required=True,
        )
        _require_timestamp(self.reviewed_at, "ai_advisory_review.reviewed_at")
        if self.authority_scope != "advisory_output_only":
            raise KnowledgeContractError("ai_advisory_review.authority_scope_invalid")
        if self.reviewer_type != "human":
            raise KnowledgeContractError(
                "ai_advisory_review.reviewer_type_must_be_human"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "review_id": self.review_id,
            "version": self.version,
            "advisory_ref": self.advisory_ref.as_dict(),
            "reviewer_id": self.reviewer_id,
            "reviewer_role": self.reviewer_role,
            "decision": self.decision,
            "rationale": self.rationale,
            "evidence_hashes": list(self.evidence_hashes),
            "reviewed_at": self.reviewed_at,
            "authority_scope": self.authority_scope,
            "reviewer_type": self.reviewer_type,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict())

    def ref(self) -> KnowledgeRef:
        return KnowledgeRef(
            "ai_advisory_review", self.review_id, self.version, self.contract_hash()
        )


def knowledge_ref_from_dict(
    value: object, *, context: str = "knowledge_ref"
) -> KnowledgeRef:
    if not isinstance(value, dict) or set(value) != {
        "record_type",
        "logical_id",
        "version",
        "record_hash",
    }:
        raise KnowledgeContractError(f"{context}_invalid")
    return KnowledgeRef(
        record_type=str(value["record_type"]),
        logical_id=str(value["logical_id"]),
        version=str(value["version"]),
        record_hash=str(value["record_hash"]),
    )


def authority_ref_from_dict(
    value: object, *, context: str = "authority_ref"
) -> AuthorityRef:
    if not isinstance(value, dict) or set(value) != {
        "authority",
        "subject_type",
        "subject_id",
        "subject_version",
        "authority_hash",
    }:
        raise KnowledgeContractError(f"{context}_invalid")
    return AuthorityRef(
        authority=str(value["authority"]),
        subject_type=str(value["subject_type"]),
        subject_id=str(value["subject_id"]),
        subject_version=str(value["subject_version"]),
        authority_hash=str(value["authority_hash"]),
    )


def validate_research_note_authority_refs(
    values: tuple[AuthorityRef, ...],
) -> None:
    identities = [
        (
            item.authority,
            item.subject_type,
            item.subject_id,
            item.subject_version,
            item.authority_hash,
        )
        for item in values
    ]
    if len(set(identities)) != len(identities):
        raise KnowledgeContractError("research_note.authority_refs_duplicate")
    unknown = sorted(
        {
            item.subject_type
            for item in values
            if item.subject_type not in RESEARCH_NOTE_AUTHORITY_SUBJECT_TYPES
        }
    )
    if unknown:
        raise KnowledgeContractError(
            "research_note.authority_ref_subject_type_unknown:" + ",".join(unknown)
        )


def _require_schema(value: int, context: str) -> None:
    if value != KNOWLEDGE_CONTRACT_SCHEMA_VERSION:
        raise KnowledgeContractError(f"{context}.schema_version_unsupported")


def _require_record_type(value: str) -> None:
    if value not in _RECORD_TYPES:
        raise KnowledgeContractError("knowledge_ref.record_type_unknown")


def _require_stable_id(value: str, context: str) -> None:
    if not isinstance(value, str) or not _STABLE_ID_PATTERN.fullmatch(value):
        raise KnowledgeContractError(f"{context}_invalid")


def _require_text(value: str, context: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise KnowledgeContractError(f"{context}_required")


def _require_texts(
    values: tuple[str, ...], context: str, *, required: bool = False
) -> None:
    if required and not values:
        raise KnowledgeContractError(f"{context}_required")
    for value in values:
        _require_text(value, context)


def _require_hash(value: str, context: str) -> None:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise KnowledgeContractError(f"{context}_invalid")


def _require_hashes(
    values: tuple[str, ...], context: str, *, required: bool = False
) -> None:
    if required and not values:
        raise KnowledgeContractError(f"{context}_required")
    if len(set(values)) != len(values):
        raise KnowledgeContractError(f"{context}_duplicate")
    for value in values:
        _require_hash(value, context)


def _require_unique_refs(values: tuple[KnowledgeRef, ...], context: str) -> None:
    identities = [(item.record_type, item.logical_id, item.version) for item in values]
    if len(set(identities)) != len(identities):
        raise KnowledgeContractError(f"{context}_duplicate")


def _require_timestamp(value: str, context: str) -> None:
    _require_text(value, context)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise KnowledgeContractError(f"{context}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise KnowledgeContractError(f"{context}_timezone_required")
