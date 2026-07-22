"""Detached knowledge-registry evidence for derivative research packages.

The archive in this module is deliberately package-local.  It does not publish
knowledge, read a live registry, or refer back to a derivative package.  Its
embedded prefix proofs are sufficient to verify the literature, hypothesis
outcome, and human/policy decision that support one already-hashed research
conclusion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from market_research.research.hashing import sha256_prefixed
from market_research.research.knowledge_contract import (
    AuthorityRef,
    DecisionAlternative,
    DecisionApprover,
    DecisionRecord,
    DecisionRisk,
    HypothesisOutcomeSpec,
    KnowledgeContractError,
    KnowledgeRef,
    LiteratureSpec,
    hypothesis_outcome_spec_from_dict,
    literature_spec_from_dict,
)
from market_research.research.knowledge_registry import (
    KnowledgeRegistryError,
    KnowledgeRegistryProof,
)


DERIVATIVE_KNOWLEDGE_EVIDENCE_SCHEMA_VERSION = 1
DERIVATIVE_RESEARCH_CONCLUSION_AUTHORITY = "derivative_research_conclusion"
DERIVATIVE_RESEARCH_CONCLUSION_SUBJECT_TYPE = "research_conclusion"

_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_STABLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")


class DerivativeKnowledgeEvidenceError(ValueError):
    """A detached derivative knowledge archive is incomplete or inconsistent."""


@dataclass(frozen=True, slots=True)
class DerivativeKnowledgeEvidenceArchive:
    """Hash-bound, self-contained knowledge evidence for one conclusion.

    The decision proof must be the longest prefix.  Every literature and
    outcome proof must be an exact prefix of it, which proves that all records
    came from the same append-only registry without consulting that registry.
    """

    archive_id: str
    version: str
    conclusion_id: str
    conclusion_version: str
    conclusion_hash: str
    outcome_proof: KnowledgeRegistryProof
    literature_proofs: tuple[KnowledgeRegistryProof, ...]
    decision_proof: KnowledgeRegistryProof
    assembled_at: str
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_KNOWLEDGE_EVIDENCE_SCHEMA_VERSION
    _hypothesis_outcome: HypothesisOutcomeSpec = field(
        init=False, repr=False, compare=False
    )
    _literature_records: tuple[LiteratureSpec, ...] = field(
        init=False, repr=False, compare=False
    )
    _decision_record: DecisionRecord = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.schema_version != DERIVATIVE_KNOWLEDGE_EVIDENCE_SCHEMA_VERSION:
            raise DerivativeKnowledgeEvidenceError(
                "derivative_knowledge_evidence_schema_unsupported"
            )
        _require_stable_id(self.archive_id, "archive_id")
        _require_stable_id(self.version, "version")
        _require_stable_id(self.conclusion_id, "conclusion_id")
        _require_stable_id(self.conclusion_version, "conclusion_version")
        _require_hash(self.conclusion_hash, "conclusion_hash")
        assembled_at = _require_timestamp(self.assembled_at, "assembled_at")

        outcome_proof = _verified_proof(self.outcome_proof, "outcome_proof")
        if not isinstance(self.literature_proofs, tuple):
            raise DerivativeKnowledgeEvidenceError(
                "derivative_knowledge_evidence_literature_proofs_tuple_required"
            )
        if not self.literature_proofs:
            raise DerivativeKnowledgeEvidenceError(
                "derivative_knowledge_evidence_literature_proofs_required"
            )
        literature_proofs = tuple(
            _verified_proof(proof, f"literature_proofs[{index}]")
            for index, proof in enumerate(self.literature_proofs)
        )
        literature_proofs = tuple(sorted(literature_proofs, key=_proof_target_identity))
        decision_proof = _verified_proof(self.decision_proof, "decision_proof")

        outcome = _outcome_from_proof(outcome_proof)
        literature = tuple(_literature_from_proof(proof) for proof in literature_proofs)
        decision = _decision_from_proof(decision_proof)

        supporting_proofs = (outcome_proof, *literature_proofs)
        identities = tuple(_proof_target_identity(proof) for proof in supporting_proofs)
        if len(set(identities)) != len(identities):
            raise DerivativeKnowledgeEvidenceError(
                "derivative_knowledge_evidence_target_duplicate"
            )
        for proof in supporting_proofs:
            _require_exact_prefix(proof, decision_proof)

        expected_hypothesis = outcome.hypothesis_ref
        for record in literature:
            if any(
                relation.hypothesis_ref != expected_hypothesis
                for relation in record.internal_hypothesis_relations
            ):
                raise DerivativeKnowledgeEvidenceError(
                    "derivative_knowledge_evidence_hypothesis_relation_mismatch"
                )

        subject = decision.subject
        if (
            subject.authority != DERIVATIVE_RESEARCH_CONCLUSION_AUTHORITY
            or subject.subject_type != DERIVATIVE_RESEARCH_CONCLUSION_SUBJECT_TYPE
        ):
            raise DerivativeKnowledgeEvidenceError(
                "derivative_knowledge_evidence_decision_authority_mismatch"
            )
        if (
            subject.subject_id != self.conclusion_id
            or subject.subject_version != self.conclusion_version
            or subject.authority_hash != self.conclusion_hash
        ):
            raise DerivativeKnowledgeEvidenceError(
                "derivative_knowledge_evidence_decision_conclusion_mismatch"
            )

        required_evidence_hashes = {
            self.conclusion_hash,
            outcome_proof.target_ref.record_hash,
            *(proof.target_ref.record_hash for proof in literature_proofs),
        }
        if not required_evidence_hashes.issubset(decision.evidence_hashes):
            raise DerivativeKnowledgeEvidenceError(
                "derivative_knowledge_evidence_decision_evidence_missing"
            )

        decided_at = _require_timestamp(decision.decided_at, "decision.decided_at")
        recorded_times = (
            _require_timestamp(outcome.recorded_at, "outcome.recorded_at"),
            *(
                _require_timestamp(record.recorded_at, "literature.recorded_at")
                for record in literature
            ),
        )
        if any(recorded_at > decided_at for recorded_at in recorded_times):
            raise DerivativeKnowledgeEvidenceError(
                "derivative_knowledge_evidence_decision_before_evidence"
            )
        if assembled_at < decided_at:
            raise DerivativeKnowledgeEvidenceError(
                "derivative_knowledge_evidence_assembled_before_decision"
            )

        object.__setattr__(self, "outcome_proof", outcome_proof)
        object.__setattr__(self, "literature_proofs", literature_proofs)
        object.__setattr__(self, "decision_proof", decision_proof)
        object.__setattr__(self, "_hypothesis_outcome", outcome)
        object.__setattr__(self, "_literature_records", literature)
        object.__setattr__(self, "_decision_record", decision)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="derivative_knowledge_evidence_archive",
            ),
        )

    @property
    def hypothesis_outcome(self) -> HypothesisOutcomeSpec:
        """Return the strictly parsed schema-v2 outcome."""

        return self._hypothesis_outcome

    @property
    def literature_records(self) -> tuple[LiteratureSpec, ...]:
        """Return the canonical, strictly parsed schema-v2 literature records."""

        return self._literature_records

    @property
    def decision_record(self) -> DecisionRecord:
        """Return the strictly reconstructed conclusion decision."""

        return self._decision_record

    @property
    def hypothesis_ref(self) -> KnowledgeRef:
        """Return the single hypothesis shared by outcome and literature."""

        return self._hypothesis_outcome.hypothesis_ref

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": "derivative_knowledge_evidence_archive",
            "archive_id": self.archive_id,
            "version": self.version,
            "conclusion_id": self.conclusion_id,
            "conclusion_version": self.conclusion_version,
            "conclusion_hash": self.conclusion_hash,
            "outcome_proof": self.outcome_proof.as_dict(),
            "literature_proofs": [proof.as_dict() for proof in self.literature_proofs],
            "decision_proof": self.decision_proof.as_dict(),
            "assembled_at": self.assembled_at,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> DerivativeKnowledgeEvidenceArchive:
        payload = _mapping(value, "archive")
        _require_exact_fields(
            payload,
            {
                "schema_version",
                "artifact_type",
                "archive_id",
                "version",
                "conclusion_id",
                "conclusion_version",
                "conclusion_hash",
                "outcome_proof",
                "literature_proofs",
                "decision_proof",
                "assembled_at",
                "content_hash",
            },
            "archive",
        )
        if payload["artifact_type"] != "derivative_knowledge_evidence_archive":
            raise DerivativeKnowledgeEvidenceError(
                "derivative_knowledge_evidence_artifact_type_invalid"
            )
        try:
            outcome_proof = KnowledgeRegistryProof.from_dict(payload["outcome_proof"])
            literature_proofs = tuple(
                KnowledgeRegistryProof.from_dict(item)
                for item in _array(
                    payload["literature_proofs"], "archive.literature_proofs"
                )
            )
            decision_proof = KnowledgeRegistryProof.from_dict(payload["decision_proof"])
        except KnowledgeRegistryError as exc:
            raise DerivativeKnowledgeEvidenceError(
                "derivative_knowledge_evidence_embedded_proof_invalid"
            ) from exc
        result = cls(
            schema_version=_integer(
                payload["schema_version"], "archive.schema_version"
            ),
            archive_id=_text(payload["archive_id"], "archive.archive_id"),
            version=_text(payload["version"], "archive.version"),
            conclusion_id=_text(payload["conclusion_id"], "archive.conclusion_id"),
            conclusion_version=_text(
                payload["conclusion_version"], "archive.conclusion_version"
            ),
            conclusion_hash=_text(
                payload["conclusion_hash"], "archive.conclusion_hash"
            ),
            outcome_proof=outcome_proof,
            literature_proofs=literature_proofs,
            decision_proof=decision_proof,
            assembled_at=_text(payload["assembled_at"], "archive.assembled_at"),
        )
        if (
            _text(payload["content_hash"], "archive.content_hash")
            != result.content_hash
        ):
            raise DerivativeKnowledgeEvidenceError(
                "derivative_knowledge_evidence_content_hash_mismatch"
            )
        if result.as_dict() != dict(payload):
            raise DerivativeKnowledgeEvidenceError(
                "derivative_knowledge_evidence_payload_not_canonical"
            )
        return result


def verify_derivative_knowledge_evidence_archive(
    value: object,
) -> DerivativeKnowledgeEvidenceArchive:
    """Strictly verify serialized or already-typed detached evidence."""

    if isinstance(value, DerivativeKnowledgeEvidenceArchive):
        value = value.as_dict()
    return DerivativeKnowledgeEvidenceArchive.from_dict(value)


def _verified_proof(value: object, label: str) -> KnowledgeRegistryProof:
    if not isinstance(value, KnowledgeRegistryProof):
        raise DerivativeKnowledgeEvidenceError(
            f"derivative_knowledge_evidence_{label}_invalid"
        )
    try:
        return KnowledgeRegistryProof.from_dict(value.as_dict())
    except (KnowledgeRegistryError, TypeError, ValueError) as exc:
        raise DerivativeKnowledgeEvidenceError(
            f"derivative_knowledge_evidence_{label}_invalid"
        ) from exc


def _proof_target_identity(proof: KnowledgeRegistryProof) -> tuple[str, str, str, str]:
    ref = proof.target_ref
    return (ref.record_type, ref.logical_id, ref.version, ref.record_hash)


def _target_payload(
    proof: KnowledgeRegistryProof, expected_record_type: str
) -> Mapping[str, object]:
    if proof.target_ref.record_type != expected_record_type:
        raise DerivativeKnowledgeEvidenceError(
            "derivative_knowledge_evidence_record_type_mismatch"
        )
    payload = proof.rows[-1].get("payload")
    if not isinstance(payload, Mapping) or any(
        not isinstance(key, str) for key in payload
    ):
        raise DerivativeKnowledgeEvidenceError(
            "derivative_knowledge_evidence_target_payload_invalid"
        )
    return payload


def _outcome_from_proof(proof: KnowledgeRegistryProof) -> HypothesisOutcomeSpec:
    payload = _target_payload(proof, "hypothesis_outcome")
    try:
        outcome = hypothesis_outcome_spec_from_dict(payload)
    except KnowledgeContractError as exc:
        raise DerivativeKnowledgeEvidenceError(
            "derivative_knowledge_evidence_outcome_invalid"
        ) from exc
    if outcome.schema_version != 2:
        raise DerivativeKnowledgeEvidenceError(
            "derivative_knowledge_evidence_outcome_v2_required"
        )
    if outcome.ref() != proof.target_ref:
        raise DerivativeKnowledgeEvidenceError(
            "derivative_knowledge_evidence_outcome_proof_binding_mismatch"
        )
    return outcome


def _literature_from_proof(proof: KnowledgeRegistryProof) -> LiteratureSpec:
    payload = _target_payload(proof, "literature")
    try:
        literature = literature_spec_from_dict(payload)
    except KnowledgeContractError as exc:
        raise DerivativeKnowledgeEvidenceError(
            "derivative_knowledge_evidence_literature_invalid"
        ) from exc
    if literature.schema_version != 2:
        raise DerivativeKnowledgeEvidenceError(
            "derivative_knowledge_evidence_literature_v2_required"
        )
    if literature.ref() != proof.target_ref:
        raise DerivativeKnowledgeEvidenceError(
            "derivative_knowledge_evidence_literature_proof_binding_mismatch"
        )
    return literature


def _decision_from_proof(proof: KnowledgeRegistryProof) -> DecisionRecord:
    payload = _target_payload(proof, "decision")
    try:
        decision = _decision_record_from_dict(payload)
    except (KnowledgeContractError, DerivativeKnowledgeEvidenceError) as exc:
        raise DerivativeKnowledgeEvidenceError(
            "derivative_knowledge_evidence_decision_invalid"
        ) from exc
    if decision.ref() != proof.target_ref:
        raise DerivativeKnowledgeEvidenceError(
            "derivative_knowledge_evidence_decision_proof_binding_mismatch"
        )
    terminal = proof.rows[-1]
    expected_refs = (
        [] if decision.supersedes is None else [decision.supersedes.as_dict()]
    )
    if (
        terminal.get("actor_id") != decision.approver.approver_id
        or terminal.get("recorded_at") != decision.decided_at
        or terminal.get("outbound_refs") != expected_refs
    ):
        raise DerivativeKnowledgeEvidenceError(
            "derivative_knowledge_evidence_decision_registry_binding_mismatch"
        )
    return decision


def _decision_record_from_dict(value: object) -> DecisionRecord:
    """Strict local parser kept here to avoid changing schema-v1 serialization."""

    payload = _mapping(value, "decision")
    _require_exact_fields(
        payload,
        {
            "schema_version",
            "decision_id",
            "version",
            "decision_type",
            "subject",
            "chosen_action",
            "rationale",
            "evidence_hashes",
            "alternatives",
            "expected_effects",
            "risks",
            "proposer_ids",
            "approver",
            "policy_version",
            "decided_at",
            "supersedes",
        },
        "decision",
    )
    alternatives = tuple(
        _decision_alternative(item, index)
        for index, item in enumerate(
            _array(payload["alternatives"], "decision.alternatives")
        )
    )
    risks = tuple(
        _decision_risk(item, index)
        for index, item in enumerate(_array(payload["risks"], "decision.risks"))
    )
    approver_payload = _mapping(payload["approver"], "decision.approver")
    _require_exact_fields(
        approver_payload,
        {"approver_type", "approver_id", "role"},
        "decision.approver",
    )
    raw_supersedes = payload["supersedes"]
    result = DecisionRecord(
        schema_version=_integer(payload["schema_version"], "decision.schema_version"),
        decision_id=_text(payload["decision_id"], "decision.decision_id"),
        version=_text(payload["version"], "decision.version"),
        decision_type=_text(payload["decision_type"], "decision.decision_type"),
        subject=_authority_ref(payload["subject"], "decision.subject"),
        chosen_action=_text(payload["chosen_action"], "decision.chosen_action"),
        rationale=_text(payload["rationale"], "decision.rationale"),
        evidence_hashes=_text_array(
            payload["evidence_hashes"], "decision.evidence_hashes"
        ),
        alternatives=alternatives,
        expected_effects=_text_array(
            payload["expected_effects"], "decision.expected_effects"
        ),
        risks=risks,
        proposer_ids=_text_array(payload["proposer_ids"], "decision.proposer_ids"),
        approver=DecisionApprover(
            approver_type=_text(
                approver_payload["approver_type"],
                "decision.approver.approver_type",
            ),
            approver_id=_text(
                approver_payload["approver_id"],
                "decision.approver.approver_id",
            ),
            role=_text(approver_payload["role"], "decision.approver.role"),
        ),
        policy_version=_text(payload["policy_version"], "decision.policy_version"),
        decided_at=_text(payload["decided_at"], "decision.decided_at"),
        supersedes=(
            None
            if raw_supersedes is None
            else _knowledge_ref(raw_supersedes, "decision.supersedes")
        ),
    )
    if result.as_dict() != dict(payload):
        raise DerivativeKnowledgeEvidenceError(
            "derivative_knowledge_evidence_decision_payload_not_canonical"
        )
    return result


def _decision_alternative(value: object, index: int) -> DecisionAlternative:
    label = f"decision.alternatives[{index}]"
    payload = _mapping(value, label)
    _require_exact_fields(
        payload,
        {"alternative_id", "description", "rejection_reason"},
        label,
    )
    return DecisionAlternative(
        alternative_id=_text(payload["alternative_id"], f"{label}.alternative_id"),
        description=_text(payload["description"], f"{label}.description"),
        rejection_reason=_text(
            payload["rejection_reason"], f"{label}.rejection_reason"
        ),
    )


def _decision_risk(value: object, index: int) -> DecisionRisk:
    label = f"decision.risks[{index}]"
    payload = _mapping(value, label)
    _require_exact_fields(
        payload,
        {"risk_id", "description", "severity", "mitigation"},
        label,
    )
    return DecisionRisk(
        risk_id=_text(payload["risk_id"], f"{label}.risk_id"),
        description=_text(payload["description"], f"{label}.description"),
        severity=_text(payload["severity"], f"{label}.severity"),
        mitigation=_text(payload["mitigation"], f"{label}.mitigation"),
    )


def _authority_ref(value: object, label: str) -> AuthorityRef:
    payload = _mapping(value, label)
    _require_exact_fields(
        payload,
        {
            "authority",
            "subject_type",
            "subject_id",
            "subject_version",
            "authority_hash",
        },
        label,
    )
    return AuthorityRef(
        authority=_text(payload["authority"], f"{label}.authority"),
        subject_type=_text(payload["subject_type"], f"{label}.subject_type"),
        subject_id=_text(payload["subject_id"], f"{label}.subject_id"),
        subject_version=_text(payload["subject_version"], f"{label}.subject_version"),
        authority_hash=_text(payload["authority_hash"], f"{label}.authority_hash"),
    )


def _knowledge_ref(value: object, label: str) -> KnowledgeRef:
    payload = _mapping(value, label)
    _require_exact_fields(
        payload,
        {"record_type", "logical_id", "version", "record_hash"},
        label,
    )
    return KnowledgeRef(
        record_type=_text(payload["record_type"], f"{label}.record_type"),
        logical_id=_text(payload["logical_id"], f"{label}.logical_id"),
        version=_text(payload["version"], f"{label}.version"),
        record_hash=_text(payload["record_hash"], f"{label}.record_hash"),
    )


def _require_exact_prefix(
    supporting: KnowledgeRegistryProof, decision: KnowledgeRegistryProof
) -> None:
    if len(supporting.rows) >= len(decision.rows):
        raise DerivativeKnowledgeEvidenceError(
            "derivative_knowledge_evidence_decision_not_after_support"
        )
    if supporting.rows != decision.rows[: len(supporting.rows)]:
        raise DerivativeKnowledgeEvidenceError(
            "derivative_knowledge_evidence_registry_prefix_mismatch"
        )


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise DerivativeKnowledgeEvidenceError(
            f"derivative_knowledge_evidence_{label}_object_required"
        )
    return value


def _array(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise DerivativeKnowledgeEvidenceError(
            f"derivative_knowledge_evidence_{label}_array_required"
        )
    return value


def _require_exact_fields(
    payload: Mapping[str, object], expected: set[str], label: str
) -> None:
    if set(payload) != expected:
        raise DerivativeKnowledgeEvidenceError(
            f"derivative_knowledge_evidence_{label}_fields_invalid"
        )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise DerivativeKnowledgeEvidenceError(
            f"derivative_knowledge_evidence_{label}_string_required"
        )
    return value


def _text_array(value: object, label: str) -> tuple[str, ...]:
    return tuple(
        _text(item, f"{label}[{index}]")
        for index, item in enumerate(_array(value, label))
    )


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DerivativeKnowledgeEvidenceError(
            f"derivative_knowledge_evidence_{label}_integer_required"
        )
    return value


def _require_stable_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _STABLE_ID_PATTERN.fullmatch(value):
        raise DerivativeKnowledgeEvidenceError(
            f"derivative_knowledge_evidence_{label}_invalid"
        )
    return value


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or not _HASH_PATTERN.fullmatch(value):
        raise DerivativeKnowledgeEvidenceError(
            f"derivative_knowledge_evidence_{label}_invalid"
        )
    return value


def _require_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise DerivativeKnowledgeEvidenceError(
            f"derivative_knowledge_evidence_{label}_invalid"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DerivativeKnowledgeEvidenceError(
            f"derivative_knowledge_evidence_{label}_invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DerivativeKnowledgeEvidenceError(
            f"derivative_knowledge_evidence_{label}_timezone_required"
        )
    return parsed


__all__ = [
    "DERIVATIVE_KNOWLEDGE_EVIDENCE_SCHEMA_VERSION",
    "DERIVATIVE_RESEARCH_CONCLUSION_AUTHORITY",
    "DERIVATIVE_RESEARCH_CONCLUSION_SUBJECT_TYPE",
    "DerivativeKnowledgeEvidenceArchive",
    "DerivativeKnowledgeEvidenceError",
    "verify_derivative_knowledge_evidence_archive",
]
