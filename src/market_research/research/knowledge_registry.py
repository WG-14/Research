"""Repository-external append-only authority for research knowledge.

The registry is a single hash-chained JSONL stream.  Observation, question,
hypothesis and preregistration publication is one locked mutation so readers
can never observe a partially published lineage.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from market_research.paths import ResearchPathManager

from .hash_chain import (
    HashChainSnapshot,
    mutate_hash_chained_jsonl_atomic,
    read_hash_chained_jsonl_snapshot,
)
from .hashing import canonical_json_bytes, content_hash_payload, sha256_prefixed
from .data_governance import (
    DataGovernanceError,
    require_confirmatory_data_governance,
)
from .hypothesis_contract import HypothesisSpec, ObservationSpec, ResearchQuestionSpec
from .knowledge_contract import (
    AIAdvisoryReview,
    AIAdvisorySpec,
    AuthorityRef,
    DecisionRecord,
    HypothesisOutcomeSpec,
    KnowledgeContractError,
    KnowledgeRef,
    LiteratureSpec,
    PreregistrationRecord,
    ResearchNoteSpec,
    authority_ref_from_dict,
    hypothesis_outcome_spec_from_dict,
    knowledge_ref_from_dict,
    literature_spec_from_dict,
    validate_research_note_authority_refs,
)
from .point_in_time_selection import (
    PointInTimeSelectionError,
    require_point_in_time_scope,
)
from .research_classification import requires_candidate_validation
from .research_standard import (
    HypothesisVersion,
    ResearchStandardBinding,
    ResearchStandardError,
    parse_hypothesis_version,
    validate_compatibility_hypothesis_binding,
    verify_hypothesis_successor,
)


KNOWLEDGE_REGISTRY_SCHEMA_VERSION = 1
KNOWLEDGE_REGISTRY_HASH_LABEL = "research_knowledge_registry"
VALIDATION_ADMISSION_GOVERNANCE_BINDING_VERSION = 1
_VALIDATION_ADMISSION_GOVERNANCE_COMPONENTS = frozenset(
    {
        "data_governance_admission_record",
        "data_governance_admission_row",
        "data_governance_dataset_version",
    }
)


class KnowledgeRegistryError(ValueError):
    """The knowledge stream or a requested registry mutation is invalid."""


@dataclass(frozen=True, slots=True)
class KnowledgeRegistryProof:
    """Self-contained validated prefix proof for one knowledge record."""

    target_ref: KnowledgeRef
    rows: tuple[Mapping[str, Any], ...]
    stream_hash: str
    content_hash: str = dataclass_field(init=False)
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise KnowledgeRegistryError("knowledge_proof_schema_unsupported")
        if not isinstance(self.target_ref, KnowledgeRef):
            raise KnowledgeRegistryError("knowledge_proof_target_ref_invalid")
        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(self.rows):
            if not isinstance(raw, Mapping) or any(
                not isinstance(key, str) for key in raw
            ):
                raise KnowledgeRegistryError(f"knowledge_proof_row_invalid:{index}")
            normalized.append(deepcopy(dict(raw)))
        _validate_knowledge_proof_material(
            target_ref=self.target_ref,
            rows=normalized,
            stream_hash=self.stream_hash,
        )
        object.__setattr__(self, "rows", tuple(normalized))
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(), label="knowledge_registry_prefix_proof"
            ),
        )

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": "knowledge_registry_hash_chain_proof",
            "hash_chain_label": KNOWLEDGE_REGISTRY_HASH_LABEL,
            "target_ref": self.target_ref.as_dict(),
            "row_count": len(self.rows),
            "stream_hash": self.stream_hash,
            "rows": [deepcopy(dict(row)) for row in self.rows],
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def as_external_evidence(self) -> dict[str, Any]:
        """Adapt the proof to a repository-independent supporting payload."""

        return {
            "schema_version": 1,
            "artifact_type": "knowledge_registry_external_evidence",
            "authority": "knowledge_registry",
            "logical_id": self.target_ref.logical_id,
            "version": self.target_ref.version,
            "content_hash": self.content_hash,
            "payload": self.as_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> KnowledgeRegistryProof:
        payload = _proof_mapping(value, "knowledge_proof")
        expected = {
            "schema_version",
            "artifact_type",
            "hash_chain_label",
            "target_ref",
            "row_count",
            "stream_hash",
            "rows",
            "content_hash",
        }
        if set(payload) != expected:
            raise KnowledgeRegistryError("knowledge_proof_fields_invalid")
        if payload["artifact_type"] != "knowledge_registry_hash_chain_proof":
            raise KnowledgeRegistryError("knowledge_proof_artifact_type_invalid")
        if payload["hash_chain_label"] != KNOWLEDGE_REGISTRY_HASH_LABEL:
            raise KnowledgeRegistryError("knowledge_proof_label_invalid")
        schema_version = _proof_integer(
            payload["schema_version"], "knowledge_proof.schema_version"
        )
        rows_raw = _proof_list(payload["rows"], "knowledge_proof.rows")
        if _proof_integer(payload["row_count"], "knowledge_proof.row_count") != len(
            rows_raw
        ):
            raise KnowledgeRegistryError("knowledge_proof_row_count_mismatch")
        rows = tuple(
            _proof_mapping(item, f"knowledge_proof.rows[{index}]")
            for index, item in enumerate(rows_raw)
        )
        target_payload = _proof_mapping(
            payload["target_ref"], "knowledge_proof.target_ref"
        )
        target_fields = {"record_type", "logical_id", "version", "record_hash"}
        if set(target_payload) != target_fields or any(
            not isinstance(target_payload[field], str) for field in target_fields
        ):
            raise KnowledgeRegistryError("knowledge_proof_target_ref_invalid")
        try:
            target_ref = knowledge_ref_from_dict(
                dict(target_payload), context="knowledge_proof.target_ref"
            )
        except KnowledgeContractError as exc:
            raise KnowledgeRegistryError("knowledge_proof_target_ref_invalid") from exc
        result = cls(
            schema_version=schema_version,
            target_ref=target_ref,
            rows=rows,
            stream_hash=_proof_text(
                payload["stream_hash"], "knowledge_proof.stream_hash"
            ),
        )
        if payload["content_hash"] != result.content_hash:
            raise KnowledgeRegistryError("knowledge_proof_content_hash_mismatch")
        return result


@dataclass(frozen=True, slots=True)
class _Descriptor:
    record_type: str
    logical_id: str
    version: str
    record_hash: str
    payload: dict[str, Any]
    outbound_refs: tuple[KnowledgeRef, ...]
    actor_id: str
    recorded_at: str
    authority_refs: tuple[AuthorityRef, ...] = ()
    expected_previous_record_hash: str | None = None
    allow_implicit_cas: bool = False
    volatile_replay_fields: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.record_type, self.logical_id, self.version)


def knowledge_registry_path(manager: ResearchPathManager) -> Path:
    return manager.artifact_path("reports", "research", "_registry", "knowledge.jsonl")


def validate_knowledge_registry(manager: ResearchPathManager) -> dict[str, Any]:
    """Validate both the physical chain and cross-record reference semantics."""

    path = knowledge_registry_path(manager)
    try:
        snapshot = read_hash_chained_jsonl_snapshot(
            path=path,
            label=KNOWLEDGE_REGISTRY_HASH_LABEL,
        )
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        return {
            "status": "FAIL",
            "reasons": [f"knowledge_registry_invalid:{type(exc).__name__}"],
            "row_count": 0,
            "stream_hash": None,
            "path": str(path.resolve()),
        }
    reasons = list(snapshot.reasons)
    reasons.extend(_semantic_reasons(list(snapshot.rows)))
    return {
        "status": "PASS" if not reasons else "FAIL",
        "reasons": sorted(set(reasons)),
        "row_count": snapshot.row_count,
        "stream_hash": snapshot.stream_hash,
        "path": str(path.resolve()),
    }


def export_knowledge_registry_proof(
    *, manager: ResearchPathManager, target_ref: KnowledgeRef
) -> KnowledgeRegistryProof:
    """Export the immutable stream prefix ending at ``target_ref``."""

    rows = _validated_rows(manager)
    matches = [
        row
        for row in rows
        if (
            row.get("record_type"),
            row.get("logical_id"),
            row.get("version"),
            row.get("record_hash"),
        )
        == (
            target_ref.record_type,
            target_ref.logical_id,
            target_ref.version,
            target_ref.record_hash,
        )
    ]
    if len(matches) != 1:
        raise KnowledgeRegistryError("knowledge_proof_target_unresolved")
    sequence = matches[0].get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        raise KnowledgeRegistryError("knowledge_proof_target_sequence_invalid")
    prefix = tuple(rows[: sequence + 1])
    stream_hash = prefix[-1].get("row_hash")
    if not isinstance(stream_hash, str):
        raise KnowledgeRegistryError("knowledge_proof_stream_hash_invalid")
    return KnowledgeRegistryProof(
        target_ref=target_ref,
        rows=prefix,
        stream_hash=stream_hash,
    )


def verify_knowledge_registry_proof(value: object) -> KnowledgeRegistryProof:
    """Strictly verify a detached proof without access to the source registry."""

    return KnowledgeRegistryProof.from_dict(value)


def verify_knowledge_registry_external_evidence(
    value: object,
) -> KnowledgeRegistryProof:
    """Verify the external-evidence adapter and return its typed proof."""

    payload = _proof_mapping(value, "knowledge_external_evidence")
    expected = {
        "schema_version",
        "artifact_type",
        "authority",
        "logical_id",
        "version",
        "content_hash",
        "payload",
    }
    if set(payload) != expected:
        raise KnowledgeRegistryError("knowledge_external_evidence_fields_invalid")
    if (
        _proof_integer(
            payload["schema_version"], "knowledge_external_evidence.schema_version"
        )
        != 1
    ):
        raise KnowledgeRegistryError("knowledge_external_evidence_schema_unsupported")
    if payload["artifact_type"] != "knowledge_registry_external_evidence":
        raise KnowledgeRegistryError(
            "knowledge_external_evidence_artifact_type_invalid"
        )
    if payload["authority"] != "knowledge_registry":
        raise KnowledgeRegistryError("knowledge_external_evidence_authority_invalid")
    proof = KnowledgeRegistryProof.from_dict(payload["payload"])
    if (
        payload["logical_id"] != proof.target_ref.logical_id
        or payload["version"] != proof.target_ref.version
        or payload["content_hash"] != proof.content_hash
    ):
        raise KnowledgeRegistryError("knowledge_external_evidence_binding_mismatch")
    return proof


def publish_observation(
    *,
    manager: ResearchPathManager,
    observation: ObservationSpec,
    expected_previous_record_hash: str | None = None,
) -> dict[str, Any]:
    descriptor = _observation_descriptor(
        observation,
        expected_previous_record_hash=expected_previous_record_hash,
    )
    return _publish_descriptors(manager=manager, descriptors=(descriptor,))[
        descriptor.key
    ]


def publish_research_question(
    *,
    manager: ResearchPathManager,
    question: ResearchQuestionSpec,
    expected_previous_record_hash: str | None = None,
) -> dict[str, Any]:
    descriptor = _question_descriptor(
        question,
        expected_previous_record_hash=expected_previous_record_hash,
    )
    return _publish_descriptors(manager=manager, descriptors=(descriptor,))[
        descriptor.key
    ]


def publish_hypothesis(
    *,
    manager: ResearchPathManager,
    hypothesis: HypothesisSpec,
    expected_previous_record_hash: str | None = None,
) -> dict[str, Any]:
    descriptor = _hypothesis_descriptor(
        hypothesis,
        expected_previous_record_hash=expected_previous_record_hash,
    )
    return _publish_descriptors(manager=manager, descriptors=(descriptor,))[
        descriptor.key
    ]


def publish_research_note(
    *,
    manager: ResearchPathManager,
    note: ResearchNoteSpec,
    expected_previous_record_hash: str | None = None,
) -> dict[str, Any]:
    descriptor = _Descriptor(
        record_type="research_note",
        logical_id=note.note_id,
        version=note.version,
        record_hash=note.contract_hash(),
        payload=note.as_dict(),
        outbound_refs=note.references,
        actor_id=note.actor_id,
        recorded_at=note.recorded_at,
        authority_refs=note.authority_refs,
        expected_previous_record_hash=expected_previous_record_hash,
    )
    return _publish_descriptors(manager=manager, descriptors=(descriptor,))[
        descriptor.key
    ]


def publish_literature(
    *,
    manager: ResearchPathManager,
    literature: LiteratureSpec,
    expected_previous_record_hash: str | None = None,
) -> dict[str, Any]:
    relation_refs = tuple(
        item.hypothesis_ref for item in literature.internal_hypothesis_relations
    )
    descriptor = _Descriptor(
        record_type="literature",
        logical_id=literature.literature_id,
        version=literature.version,
        record_hash=literature.contract_hash(),
        payload=literature.as_dict(),
        outbound_refs=(*literature.references, *relation_refs),
        actor_id=literature.actor_id,
        recorded_at=literature.recorded_at,
        expected_previous_record_hash=expected_previous_record_hash,
    )
    return _publish_descriptors(manager=manager, descriptors=(descriptor,))[
        descriptor.key
    ]


def publish_hypothesis_outcome(
    *,
    manager: ResearchPathManager,
    outcome: HypothesisOutcomeSpec,
    expected_previous_record_hash: str | None = None,
) -> dict[str, Any]:
    refs = (outcome.hypothesis_ref,) + (
        (outcome.question_ref,) if outcome.question_ref is not None else ()
    )
    descriptor = _Descriptor(
        record_type="hypothesis_outcome",
        logical_id=outcome.outcome_id,
        version=outcome.version,
        record_hash=outcome.contract_hash(),
        payload=outcome.as_dict(),
        outbound_refs=refs,
        actor_id=outcome.actor_id,
        recorded_at=outcome.recorded_at,
        expected_previous_record_hash=expected_previous_record_hash,
    )
    return _publish_descriptors(manager=manager, descriptors=(descriptor,))[
        descriptor.key
    ]


def publish_ai_advisory(
    *,
    manager: ResearchPathManager,
    advisory: AIAdvisorySpec,
    expected_previous_record_hash: str | None = None,
) -> dict[str, Any]:
    descriptor = _Descriptor(
        record_type="ai_advisory",
        logical_id=advisory.advisory_id,
        version=advisory.version,
        record_hash=advisory.contract_hash(),
        payload=advisory.as_dict(),
        outbound_refs=advisory.source_refs,
        authority_refs=advisory.source_authority_refs,
        actor_id=advisory.generator_id,
        recorded_at=advisory.generated_at,
        expected_previous_record_hash=expected_previous_record_hash,
    )
    return _publish_descriptors(manager=manager, descriptors=(descriptor,))[
        descriptor.key
    ]


def publish_ai_advisory_review(
    *,
    manager: ResearchPathManager,
    review: AIAdvisoryReview,
    expected_previous_record_hash: str | None = None,
) -> dict[str, Any]:
    advisory = get_knowledge_record(
        manager=manager,
        record_type="ai_advisory",
        logical_id=review.advisory_ref.logical_id,
        version=review.advisory_ref.version,
    )
    if advisory.get("record_hash") != review.advisory_ref.record_hash:
        raise KnowledgeRegistryError("ai_advisory_review_reference_hash_mismatch")
    generator_id = str((advisory.get("payload") or {}).get("generator_id") or "")
    if generator_id == review.reviewer_id:
        raise KnowledgeRegistryError("ai_advisory_review_separation_violation")
    generated_at = str((advisory.get("payload") or {}).get("generated_at") or "")
    if datetime.fromisoformat(review.reviewed_at) < datetime.fromisoformat(
        generated_at
    ):
        raise KnowledgeRegistryError("ai_advisory_review_before_generation")
    descriptor = _Descriptor(
        record_type="ai_advisory_review",
        logical_id=review.review_id,
        version=review.version,
        record_hash=review.contract_hash(),
        payload=review.as_dict(),
        outbound_refs=(review.advisory_ref,),
        actor_id=review.reviewer_id,
        recorded_at=review.reviewed_at,
        expected_previous_record_hash=expected_previous_record_hash,
    )
    return _publish_descriptors(manager=manager, descriptors=(descriptor,))[
        descriptor.key
    ]


def publish_decision_record(
    *,
    manager: ResearchPathManager,
    decision: DecisionRecord,
    expected_previous_record_hash: str | None = None,
) -> dict[str, Any]:
    refs = (decision.supersedes,) if decision.supersedes is not None else ()
    descriptor = _Descriptor(
        record_type="decision",
        logical_id=decision.decision_id,
        version=decision.version,
        record_hash=decision.contract_hash(),
        payload=decision.as_dict(),
        outbound_refs=refs,
        actor_id=decision.approver.approver_id,
        recorded_at=decision.decided_at,
        expected_previous_record_hash=expected_previous_record_hash,
    )
    return _publish_descriptors(manager=manager, descriptors=(descriptor,))[
        descriptor.key
    ]


def publish_idempotent_decision_record(
    *,
    manager: ResearchPathManager,
    decision: DecisionRecord,
) -> dict[str, Any]:
    """Publish a workflow decision once and preserve its first decision time.

    Durable workflow retries may reconstruct the same semantic decision after a
    crash between knowledge publication and governance publication.  The first
    immutable timestamp remains authoritative; every other field must match.
    Deliberate amendments still require a new version through
    :func:`publish_decision_record` and its explicit CAS argument.
    """

    refs = (decision.supersedes,) if decision.supersedes is not None else ()
    descriptor = _Descriptor(
        record_type="decision",
        logical_id=decision.decision_id,
        version=decision.version,
        record_hash=decision.contract_hash(),
        payload=decision.as_dict(),
        outbound_refs=refs,
        actor_id=decision.approver.approver_id,
        recorded_at=decision.decided_at,
        volatile_replay_fields=("decided_at",),
    )
    return _publish_descriptors(manager=manager, descriptors=(descriptor,))[
        descriptor.key
    ]


def publish_manifest_lineage(
    *,
    manager: ResearchPathManager,
    hypothesis: HypothesisSpec,
    research_standard_binding: ResearchStandardBinding | None = None,
) -> dict[str, Any]:
    """Atomically publish the complete inline observation/question lineage."""

    if research_standard_binding is not None:
        _require_compatible_research_standard_lineage(
            hypothesis=hypothesis,
            binding=research_standard_binding,
        )
    lineage_descriptors = _lineage_descriptors(hypothesis)
    standard_descriptors = (
        _research_standard_descriptors(
            research_standard_binding,
            parent_refs=_research_standard_parent_refs(
                manager=manager,
                binding=research_standard_binding,
            ),
        )
        if research_standard_binding is not None
        else ()
    )
    descriptors = (*lineage_descriptors, *standard_descriptors)
    rows = _publish_descriptors(manager=manager, descriptors=descriptors)
    publication = _lineage_publication(
        rows=rows, descriptors=lineage_descriptors, manager=manager
    )
    if research_standard_binding is not None:
        publication["research_standard_lineage"] = (
            _research_standard_lineage_publication(
                rows=rows,
                descriptors=standard_descriptors,
                binding=research_standard_binding,
            )
        )
    return publication


def freeze_validation_admission(
    *,
    manager: ResearchPathManager,
    manifest: Any,
    admitted_at: str | None = None,
    bind_data_governance: bool = False,
) -> dict[str, Any]:
    """Materialize lineage and freeze the validation manifest before data access.

    An inline ``pre_registered`` hypothesis may bind immutable evidence created
    outside this registry.  Admission retains that evidence hash while making
    the current manifest a canonical, queryable registry record.
    """

    candidate_validation = requires_candidate_validation(
        getattr(manifest, "research_classification", None)
    )
    data_governance_admission: dict[str, Any] | None = None
    governance_required = candidate_validation or bind_data_governance
    if candidate_validation:
        try:
            require_point_in_time_scope(manifest, verify_source_content=True)
        except PointInTimeSelectionError as exc:
            raise KnowledgeRegistryError(
                f"validation_point_in_time_admission_failed:{exc}"
            ) from exc
    if governance_required:
        try:
            data_governance_admission = require_confirmatory_data_governance(
                manager=manager,
                manifest=manifest,
            )
        except DataGovernanceError as exc:
            raise KnowledgeRegistryError(
                f"validation_data_governance_admission_failed:{exc}"
            ) from exc
    hypothesis = getattr(manifest, "hypothesis_spec", None)
    if not isinstance(hypothesis, HypothesisSpec) or hypothesis.schema_version != 2:
        raise KnowledgeRegistryError("validation_admission_hypothesis_lineage_required")
    if admitted_at is not None:
        _require_timestamp(admitted_at, "validation_admission.admitted_at")
    timestamp = admitted_at or datetime.now(timezone.utc).isoformat()
    component_hashes = _manifest_component_hashes(manifest)
    if data_governance_admission is not None:
        component_hashes.update(
            {
                "data_governance_admission_record": data_governance_admission[
                    "admission_record_hash"
                ],
                "data_governance_admission_row": data_governance_admission[
                    "admission_row_hash"
                ],
                "data_governance_dataset_version": data_governance_admission[
                    "dataset_version_hash"
                ],
            }
        )
        component_hashes = dict(sorted(component_hashes.items()))
    manifest_hash = str(manifest.manifest_hash())
    hypothesis_ref = KnowledgeRef(
        "hypothesis",
        hypothesis.hypothesis_id,
        hypothesis.version,
        hypothesis.contract_hash(),
    )
    status = (
        "FORMAL_PREREGISTERED_EXTERNAL_EVIDENCE"
        if hypothesis.registration_status == "pre_registered"
        else "VALIDATION_FROZEN_AT_ADMISSION"
    )
    preregistration = PreregistrationRecord(
        schema_version=1,
        registration_id=str(manifest.experiment_id),
        version=manifest_hash,
        experiment_id=str(manifest.experiment_id),
        manifest_hash=manifest_hash,
        hypothesis_ref=hypothesis_ref,
        component_hashes=tuple(sorted(component_hashes.items())),
        admission_status=status,
        actor_id=str(hypothesis.actor_id or "validation-admission"),
        frozen_at=timestamp,
        external_registration_evidence_hash=hypothesis.registration_evidence_hash,
    )
    lineage_descriptors = _lineage_descriptors(hypothesis)
    standard_binding = getattr(manifest, "research_standard_binding", None)
    if standard_binding is not None and not isinstance(
        standard_binding, ResearchStandardBinding
    ):
        raise KnowledgeRegistryError("validation_admission_research_standard_invalid")
    if standard_binding is not None:
        _require_compatible_research_standard_lineage(
            hypothesis=hypothesis,
            binding=standard_binding,
        )
    standard_descriptors = (
        _research_standard_descriptors(
            standard_binding,
            parent_refs=_research_standard_parent_refs(
                manager=manager,
                binding=standard_binding,
            ),
        )
        if standard_binding is not None
        else ()
    )
    preregistration_descriptor = _preregistration_descriptor(
        preregistration,
        research_standard_ref=(
            _research_standard_binding_ref(standard_binding)
            if standard_binding is not None
            else None
        ),
    )
    descriptors = (
        *lineage_descriptors,
        *standard_descriptors,
        preregistration_descriptor,
    )
    rows = _publish_descriptors(manager=manager, descriptors=descriptors)
    publication = _lineage_publication(
        rows=rows,
        descriptors=lineage_descriptors,
        manager=manager,
    )
    if standard_binding is not None:
        publication["research_standard_lineage"] = (
            _research_standard_lineage_publication(
                rows=rows,
                descriptors=standard_descriptors,
                binding=standard_binding,
            )
        )
    admission_row = rows[preregistration_descriptor.key]
    result = {
        **publication,
        "admission": deepcopy(admission_row),
        "admission_record_hash": admission_row["record_hash"],
        "admission_row_hash": admission_row["row_hash"],
        "manifest_hash": manifest_hash,
        "component_hashes": component_hashes,
    }
    if data_governance_admission is not None:
        result["data_governance"] = deepcopy(data_governance_admission)
    return result


def require_validation_admission(
    *,
    manager: ResearchPathManager,
    manifest: Any,
    expected_row_hash: str | None = None,
    bind_data_governance: bool = False,
) -> dict[str, Any]:
    """Return the exact canonical admission or fail on manifest/component drift."""

    candidate_validation = requires_candidate_validation(
        getattr(manifest, "research_classification", None)
    )
    data_governance_admission: dict[str, Any] | None = None
    governance_required = candidate_validation or bind_data_governance
    if candidate_validation:
        try:
            require_point_in_time_scope(manifest, verify_source_content=True)
        except PointInTimeSelectionError as exc:
            raise KnowledgeRegistryError(
                f"validation_point_in_time_admission_failed:{exc}"
            ) from exc
    if governance_required:
        try:
            data_governance_admission = require_confirmatory_data_governance(
                manager=manager,
                manifest=manifest,
            )
        except DataGovernanceError as exc:
            raise KnowledgeRegistryError(
                f"validation_data_governance_admission_failed:{exc}"
            ) from exc
    validation = validate_knowledge_registry(manager)
    if validation["status"] != "PASS":
        raise KnowledgeRegistryError("knowledge_registry_invalid")
    manifest_hash = str(manifest.manifest_hash())
    row = get_knowledge_record(
        manager=manager,
        record_type="preregistration",
        logical_id=str(manifest.experiment_id),
        version=manifest_hash,
    )
    payload = row.get("payload")
    if not isinstance(payload, dict):
        raise KnowledgeRegistryError("validation_admission_payload_invalid")
    hypothesis = getattr(manifest, "hypothesis_spec", None)
    if not isinstance(hypothesis, HypothesisSpec):
        raise KnowledgeRegistryError("validation_admission_hypothesis_lineage_required")
    expected_ref = KnowledgeRef(
        "hypothesis",
        hypothesis.hypothesis_id,
        hypothesis.version,
        hypothesis.contract_hash(),
    )
    if payload.get("manifest_hash") != manifest_hash:
        raise KnowledgeRegistryError("validation_admission_manifest_hash_mismatch")
    if payload.get("hypothesis_ref") != expected_ref.as_dict():
        raise KnowledgeRegistryError("validation_admission_hypothesis_ref_mismatch")
    if governance_required and not _has_validation_governance_components(payload):
        raise KnowledgeRegistryError(
            "validation_admission_legacy_non_governed_v1_immutable:"
            "issue_new_manifest_version_or_experiment_id"
        )
    expected_component_hashes = _manifest_component_hashes(manifest)
    if data_governance_admission is not None:
        expected_component_hashes.update(
            {
                "data_governance_admission_record": data_governance_admission[
                    "admission_record_hash"
                ],
                "data_governance_admission_row": data_governance_admission[
                    "admission_row_hash"
                ],
                "data_governance_dataset_version": data_governance_admission[
                    "dataset_version_hash"
                ],
            }
        )
        expected_component_hashes = dict(sorted(expected_component_hashes.items()))
    if payload.get("component_hashes") != expected_component_hashes:
        raise KnowledgeRegistryError("validation_admission_component_hash_mismatch")
    standard_binding = getattr(manifest, "research_standard_binding", None)
    if standard_binding is not None:
        if not isinstance(standard_binding, ResearchStandardBinding):
            raise KnowledgeRegistryError(
                "validation_admission_research_standard_invalid"
            )
        expected_standard_ref = _research_standard_binding_ref(
            standard_binding
        ).as_dict()
        if expected_standard_ref not in row.get("outbound_refs", []):
            raise KnowledgeRegistryError(
                "validation_admission_research_standard_ref_mismatch"
            )
        _require_research_standard_parent_lineage(
            manager=manager,
            binding=standard_binding,
        )
    if expected_row_hash is not None and row.get("row_hash") != expected_row_hash:
        raise KnowledgeRegistryError("validation_admission_row_hash_mismatch")
    return row


def validation_admission_binding_reasons(
    source: Mapping[str, Any],
    *,
    manager: ResearchPathManager | None = None,
) -> list[str]:
    """Validate a result/package binding to one canonical admission row."""

    reasons: list[str] = []
    row = source.get("validation_admission")
    if not isinstance(row, dict):
        return ["validation_admission_binding_missing"]
    if source.get("validation_admission_record_hash") != row.get("record_hash"):
        reasons.append("validation_admission_record_hash_mismatch")
    if source.get("validation_admission_row_hash") != row.get("row_hash"):
        reasons.append("validation_admission_row_hash_mismatch")
    if (
        row.get("record_type") != "preregistration"
        or row.get("logical_id") != source.get("experiment_id")
        or row.get("version") != source.get("manifest_hash")
    ):
        reasons.append("validation_admission_identity_mismatch")
    payload = row.get("payload")
    if not isinstance(payload, dict):
        reasons.append("validation_admission_payload_invalid")
    else:
        if payload.get("manifest_hash") != source.get("manifest_hash"):
            reasons.append("validation_admission_manifest_hash_mismatch")
        hypothesis_ref = payload.get("hypothesis_ref")
        if not isinstance(hypothesis_ref, dict) or (
            hypothesis_ref.get("record_type") != "hypothesis"
            or hypothesis_ref.get("logical_id") != source.get("hypothesis_id")
            or hypothesis_ref.get("version") != source.get("hypothesis_version")
            or hypothesis_ref.get("record_hash")
            != source.get("hypothesis_contract_hash")
        ):
            reasons.append("validation_admission_hypothesis_ref_mismatch")
        if row.get("record_hash") != sha256_prefixed(payload):
            reasons.append("validation_admission_record_hash_invalid")
    material = {key: value for key, value in row.items() if key != "row_hash"}
    if row.get("row_hash") != sha256_prefixed(
        content_hash_payload(material),
        label=f"{KNOWLEDGE_REGISTRY_HASH_LABEL}_row",
    ):
        reasons.append("validation_admission_row_hash_invalid")
    path_value = source.get("knowledge_registry_path")
    if not isinstance(path_value, str) or not path_value.strip():
        reasons.append("knowledge_registry_path_missing")
    if manager is not None:
        expected_path = knowledge_registry_path(manager).resolve()
        try:
            actual_path = Path(str(path_value)).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            actual_path = None
        if actual_path != expected_path:
            reasons.append("knowledge_registry_path_mismatch")
        validation = validate_knowledge_registry(manager)
        if validation["status"] != "PASS":
            reasons.append("knowledge_registry_invalid")
        else:
            try:
                canonical = get_knowledge_record(
                    manager=manager,
                    record_type="preregistration",
                    logical_id=str(source.get("experiment_id") or ""),
                    version=str(source.get("manifest_hash") or ""),
                )
            except KnowledgeRegistryError:
                reasons.append("validation_admission_registry_row_missing")
            else:
                if canonical != row:
                    reasons.append("validation_admission_registry_row_mismatch")
    return sorted(set(reasons))


def get_knowledge_record(
    *,
    manager: ResearchPathManager,
    record_type: str,
    logical_id: str,
    version: str | None = None,
) -> dict[str, Any]:
    rows = _validated_rows(manager)
    matches = [
        row
        for row in rows
        if row.get("record_type") == record_type
        and row.get("logical_id") == logical_id
        and (version is None or row.get("version") == version)
    ]
    if not matches:
        raise KnowledgeRegistryError("knowledge_record_missing")
    if version is not None and len(matches) != 1:
        raise KnowledgeRegistryError("knowledge_record_identity_duplicate")
    return deepcopy(matches[-1])


def list_knowledge_versions(
    *, manager: ResearchPathManager, record_type: str, logical_id: str
) -> list[dict[str, Any]]:
    return [
        deepcopy(row)
        for row in _validated_rows(manager)
        if row.get("record_type") == record_type and row.get("logical_id") == logical_id
    ]


def query_outbound_refs(
    *,
    manager: ResearchPathManager,
    record_type: str,
    logical_id: str,
    version: str,
) -> list[dict[str, Any]]:
    rows = _validated_rows(manager)
    row = get_knowledge_record(
        manager=manager,
        record_type=record_type,
        logical_id=logical_id,
        version=version,
    )
    knowledge_rows = [_resolve_ref(rows, value) for value in row["outbound_refs"]]
    authority_refs = [
        {
            "reference_kind": "authority",
            **authority_ref_from_dict(
                value,
                context="registry.authority_ref",
            ).as_dict(),
        }
        for value in row.get("authority_refs") or []
    ]
    return [*knowledge_rows, *authority_refs]


def query_inbound_refs(
    *, manager: ResearchPathManager, target: KnowledgeRef | AuthorityRef
) -> list[dict[str, Any]]:
    target_dict = target.as_dict()
    field = "outbound_refs" if isinstance(target, KnowledgeRef) else "authority_refs"
    return [
        deepcopy(row)
        for row in _validated_rows(manager)
        if target_dict in (row.get(field) or [])
    ]


def list_competing_hypotheses(
    *,
    manager: ResearchPathManager,
    question_id: str,
    question_version: str | None = None,
    include_failed: bool = True,
) -> list[dict[str, Any]]:
    rows = _validated_rows(manager)
    question = get_knowledge_record(
        manager=manager,
        record_type="research_question",
        logical_id=question_id,
        version=question_version,
    )
    payload = question.get("payload") or {}
    competitors = payload.get("competing_hypotheses") or []
    result: list[dict[str, Any]] = []
    for competitor in competitors:
        identity = (competitor.get("hypothesis_id"), competitor.get("version"))
        hypothesis_row = next(
            (
                row
                for row in rows
                if row.get("record_type") == "hypothesis"
                and (row.get("logical_id"), row.get("version")) == identity
            ),
            None,
        )
        outcome_rows = []
        if hypothesis_row is not None:
            ref = {
                "record_type": "hypothesis",
                "logical_id": hypothesis_row["logical_id"],
                "version": hypothesis_row["version"],
                "record_hash": hypothesis_row["record_hash"],
            }
            outcome_rows = [
                row
                for row in rows
                if row.get("record_type") == "hypothesis_outcome"
                and ref in (row.get("outbound_refs") or [])
            ]
        outcome_row = outcome_rows[-1] if outcome_rows else None
        outcome = (
            str((outcome_row.get("payload") or {}).get("outcome"))
            if outcome_row is not None
            else "unrecorded"
        )
        if not include_failed and outcome in {"failed", "rejected", "inconclusive"}:
            continue
        result.append(
            {
                "hypothesis_id": identity[0],
                "version": identity[1],
                "hypothesis_text": competitor.get("hypothesis_text"),
                "published": hypothesis_row is not None,
                "hypothesis_record_hash": (
                    hypothesis_row.get("record_hash") if hypothesis_row else None
                ),
                "outcome": outcome,
                "outcome_row_hash": outcome_row.get("row_hash")
                if outcome_row
                else None,
            }
        )
    return result


def verify_decision_record(
    *,
    manager: ResearchPathManager,
    decision_id: str,
    version: str,
    expected_subject: AuthorityRef | None = None,
    expected_chosen_action: str | None = None,
    required_evidence_hashes: Iterable[str] = (),
    expected_record_hash: str | None = None,
    expected_row_hash: str | None = None,
) -> dict[str, Any]:
    row = get_knowledge_record(
        manager=manager,
        record_type="decision",
        logical_id=decision_id,
        version=version,
    )
    payload = row.get("payload") or {}
    if (
        expected_subject is not None
        and payload.get("subject") != expected_subject.as_dict()
    ):
        raise KnowledgeRegistryError("decision_subject_binding_mismatch")
    if (
        expected_chosen_action is not None
        and payload.get("chosen_action") != expected_chosen_action
    ):
        raise KnowledgeRegistryError("decision_action_binding_mismatch")
    evidence = set(payload.get("evidence_hashes") or [])
    if not set(required_evidence_hashes).issubset(evidence):
        raise KnowledgeRegistryError("decision_evidence_binding_mismatch")
    if (
        expected_record_hash is not None
        and row.get("record_hash") != expected_record_hash
    ):
        raise KnowledgeRegistryError("decision_record_hash_mismatch")
    if expected_row_hash is not None and row.get("row_hash") != expected_row_hash:
        raise KnowledgeRegistryError("decision_registry_row_hash_mismatch")
    return row


def _validation_governance_components(
    payload: Mapping[str, Any] | object,
) -> dict[str, str]:
    if not isinstance(payload, Mapping):
        return {}
    component_hashes = payload.get("component_hashes")
    if not isinstance(component_hashes, Mapping):
        return {}
    return {
        key: str(component_hashes[key])
        for key in _VALIDATION_ADMISSION_GOVERNANCE_COMPONENTS
        if isinstance(component_hashes.get(key), str)
    }


def _has_validation_governance_components(
    payload: Mapping[str, Any] | object,
) -> bool:
    return (
        set(_validation_governance_components(payload))
        == _VALIDATION_ADMISSION_GOVERNANCE_COMPONENTS
    )


def _raise_validation_governance_identity_conflict(
    *, existing: Mapping[str, Any], descriptor: _Descriptor
) -> None:
    existing_components = _validation_governance_components(existing.get("payload"))
    incoming_components = _validation_governance_components(descriptor.payload)
    existing_governed = (
        set(existing_components) == _VALIDATION_ADMISSION_GOVERNANCE_COMPONENTS
    )
    incoming_governed = (
        set(incoming_components) == _VALIDATION_ADMISSION_GOVERNANCE_COMPONENTS
    )
    migration = "issue_new_manifest_version_or_experiment_id"
    version = VALIDATION_ADMISSION_GOVERNANCE_BINDING_VERSION
    if incoming_governed and not existing_governed:
        raise KnowledgeRegistryError(
            f"validation_admission_legacy_non_governed_v{version}_immutable:{migration}"
        )
    if existing_governed and not incoming_governed:
        raise KnowledgeRegistryError(
            f"validation_admission_governance_v{version}_downgrade_forbidden:"
            f"{migration}"
        )
    if (
        existing_governed
        and incoming_governed
        and (existing_components != incoming_components)
    ):
        raise KnowledgeRegistryError(
            f"validation_admission_governance_v{version}_identity_conflict:{migration}"
        )


def _publish_descriptors(
    *, manager: ResearchPathManager, descriptors: tuple[_Descriptor, ...]
) -> dict[tuple[str, str, str], dict[str, Any]]:
    if not descriptors:
        raise KnowledgeRegistryError("knowledge_publication_empty")

    def mutation(
        snapshot: HashChainSnapshot, stage: Any
    ) -> dict[tuple[str, str, str], dict[str, Any]]:
        working = [deepcopy(row) for row in snapshot.rows]
        reasons = _semantic_reasons(working)
        if reasons:
            raise KnowledgeRegistryError(
                "knowledge_registry_invalid:" + ",".join(reasons)
            )
        published: dict[tuple[str, str, str], dict[str, Any]] = {}
        for descriptor in descriptors:
            matches = [row for row in working if _row_key(row) == descriptor.key]
            if len(matches) > 1:
                raise KnowledgeRegistryError("knowledge_record_identity_duplicate")
            if matches:
                existing = matches[0]
                if _descriptor_replays_existing(descriptor, existing):
                    published[descriptor.key] = deepcopy(existing)
                    continue
                if descriptor.record_type == "preregistration":
                    _raise_validation_governance_identity_conflict(
                        existing=existing,
                        descriptor=descriptor,
                    )
                raise KnowledgeRegistryError("knowledge_record_version_collision")
            previous = next(
                (
                    row
                    for row in reversed(working)
                    if row.get("record_type") == descriptor.record_type
                    and row.get("logical_id") == descriptor.logical_id
                ),
                None,
            )
            previous_hash = previous.get("record_hash") if previous else None
            if previous is not None and not descriptor.allow_implicit_cas:
                if descriptor.expected_previous_record_hash != previous_hash:
                    raise KnowledgeRegistryError(
                        "knowledge_record_version_cas_conflict"
                    )
            elif (
                previous is None
                and descriptor.expected_previous_record_hash is not None
            ):
                raise KnowledgeRegistryError("knowledge_record_version_cas_conflict")
            _require_resolved_refs(working, descriptor.outbound_refs)
            validate_research_note_authority_refs(descriptor.authority_refs)
            event_id = _event_id(descriptor.key)
            material = {
                "schema_version": KNOWLEDGE_REGISTRY_SCHEMA_VERSION,
                "event_id": event_id,
                "event_type": "record_published",
                "record_type": descriptor.record_type,
                "logical_id": descriptor.logical_id,
                "version": descriptor.version,
                "record_hash": descriptor.record_hash,
                "previous_record_hash": previous_hash,
                "outbound_refs": [item.as_dict() for item in descriptor.outbound_refs],
                "payload": deepcopy(descriptor.payload),
                "actor_id": descriptor.actor_id,
                "recorded_at": descriptor.recorded_at,
            }
            if descriptor.authority_refs:
                material["authority_refs"] = [
                    item.as_dict() for item in descriptor.authority_refs
                ]
            row = stage(material)
            working.append(row)
            published[descriptor.key] = deepcopy(row)
        return published

    try:
        return mutate_hash_chained_jsonl_atomic(
            path=knowledge_registry_path(manager),
            label=KNOWLEDGE_REGISTRY_HASH_LABEL,
            mutation=mutation,
        ).value
    except KnowledgeRegistryError:
        raise
    except (KnowledgeContractError, RuntimeError, TypeError, ValueError) as exc:
        raise KnowledgeRegistryError(str(exc)) from exc


def _descriptor_replays_existing(
    descriptor: _Descriptor, row: Mapping[str, Any]
) -> bool:
    expected = {
        "schema_version": KNOWLEDGE_REGISTRY_SCHEMA_VERSION,
        "event_id": _event_id(descriptor.key),
        "event_type": "record_published",
        "record_type": descriptor.record_type,
        "logical_id": descriptor.logical_id,
        "version": descriptor.version,
        "record_hash": descriptor.record_hash,
        "outbound_refs": [item.as_dict() for item in descriptor.outbound_refs],
        "payload": descriptor.payload,
        "actor_id": descriptor.actor_id,
        "recorded_at": descriptor.recorded_at,
    }
    if descriptor.authority_refs:
        expected["authority_refs"] = [
            item.as_dict() for item in descriptor.authority_refs
        ]
    actual = {key: row.get(key) for key in expected}
    if canonical_json_bytes(actual) == canonical_json_bytes(expected):
        return True
    if not descriptor.volatile_replay_fields:
        return False
    expected_payload = deepcopy(descriptor.payload)
    actual_payload = deepcopy(row.get("payload"))
    if not isinstance(actual_payload, dict):
        return False
    for field in descriptor.volatile_replay_fields:
        expected_payload.pop(field, None)
        actual_payload.pop(field, None)
    expected_without_volatile = {
        **expected,
        "payload": expected_payload,
        "record_hash": None,
        "recorded_at": None,
    }
    actual_without_volatile = {
        **actual,
        "payload": actual_payload,
        "record_hash": None,
        "recorded_at": None,
    }
    return canonical_json_bytes(actual_without_volatile) == canonical_json_bytes(
        expected_without_volatile
    )


def _lineage_descriptors(hypothesis: HypothesisSpec) -> tuple[_Descriptor, ...]:
    if hypothesis.schema_version != 2 or hypothesis.research_question is None:
        raise KnowledgeRegistryError("knowledge_lineage_schema_2_required")
    return (
        *(
            _observation_descriptor(item, allow_implicit_cas=True)
            for item in hypothesis.observations
        ),
        _question_descriptor(hypothesis.research_question, allow_implicit_cas=True),
        _hypothesis_descriptor(hypothesis, allow_implicit_cas=True),
    )


def _observation_descriptor(
    observation: ObservationSpec,
    *,
    expected_previous_record_hash: str | None = None,
    allow_implicit_cas: bool = False,
) -> _Descriptor:
    return _Descriptor(
        record_type="observation",
        logical_id=observation.observation_id,
        version=observation.version,
        record_hash=observation.contract_hash(),
        payload=observation.as_dict(),
        outbound_refs=(),
        actor_id=observation.actor_id,
        recorded_at=observation.recorded_at,
        expected_previous_record_hash=expected_previous_record_hash,
        allow_implicit_cas=allow_implicit_cas,
    )


def _question_descriptor(
    question: ResearchQuestionSpec,
    *,
    expected_previous_record_hash: str | None = None,
    allow_implicit_cas: bool = False,
) -> _Descriptor:
    refs = tuple(
        KnowledgeRef(
            "observation", item.observation_id, item.version, item.observation_hash
        )
        for item in question.observation_refs
    )
    return _Descriptor(
        record_type="research_question",
        logical_id=question.question_id,
        version=question.version,
        record_hash=question.contract_hash(),
        payload=question.as_dict(),
        outbound_refs=refs,
        actor_id=question.actor_id,
        recorded_at=question.recorded_at,
        expected_previous_record_hash=expected_previous_record_hash,
        allow_implicit_cas=allow_implicit_cas,
    )


def _hypothesis_descriptor(
    hypothesis: HypothesisSpec,
    *,
    expected_previous_record_hash: str | None = None,
    allow_implicit_cas: bool = False,
) -> _Descriptor:
    if hypothesis.schema_version != 2 or hypothesis.research_question_ref is None:
        raise KnowledgeRegistryError("knowledge_hypothesis_lineage_required")
    refs = (
        KnowledgeRef(
            "research_question",
            hypothesis.research_question_ref.question_id,
            hypothesis.research_question_ref.version,
            hypothesis.research_question_ref.question_hash,
        ),
        *(
            KnowledgeRef(
                "observation", item.observation_id, item.version, item.observation_hash
            )
            for item in hypothesis.observation_refs
        ),
    )
    return _Descriptor(
        record_type="hypothesis",
        logical_id=hypothesis.hypothesis_id,
        version=hypothesis.version,
        record_hash=hypothesis.contract_hash(),
        payload=hypothesis.as_dict(),
        outbound_refs=refs,
        actor_id=str(hypothesis.actor_id),
        recorded_at=str(hypothesis.created_at),
        expected_previous_record_hash=expected_previous_record_hash,
        allow_implicit_cas=allow_implicit_cas,
    )


def _preregistration_descriptor(
    record: PreregistrationRecord,
    *,
    research_standard_ref: KnowledgeRef | None = None,
) -> _Descriptor:
    return _Descriptor(
        record_type="preregistration",
        logical_id=record.registration_id,
        version=record.version,
        record_hash=record.contract_hash(),
        payload=record.as_dict(),
        outbound_refs=(
            record.hypothesis_ref,
            *((research_standard_ref,) if research_standard_ref is not None else ()),
        ),
        actor_id=record.actor_id,
        recorded_at=record.frozen_at,
        allow_implicit_cas=True,
        volatile_replay_fields=("frozen_at",),
    )


def _research_standard_descriptors(
    binding: ResearchStandardBinding,
    *,
    parent_refs: tuple[KnowledgeRef, ...],
) -> tuple[_Descriptor, ...]:
    observation_descriptors: list[_Descriptor] = []
    observation_refs: list[KnowledgeRef] = []
    for observation in binding.observations:
        payload = {**observation.as_dict(), "content_hash": observation.content_hash}
        ref = KnowledgeRef(
            "research_standard_observation",
            observation.observation_id,
            str(observation.version),
            sha256_prefixed(payload),
        )
        observation_refs.append(ref)
        observation_descriptors.append(
            _Descriptor(
                record_type=ref.record_type,
                logical_id=ref.logical_id,
                version=ref.version,
                record_hash=ref.record_hash,
                payload=payload,
                outbound_refs=(),
                actor_id=observation.created_by,
                recorded_at=observation.recorded_at,
                allow_implicit_cas=True,
            )
        )
    question = binding.research_question
    question_payload = {
        **question.as_dict(),
        "content_hash": question.content_hash,
    }
    question_ref = KnowledgeRef(
        "research_standard_question",
        question.research_question_id,
        str(question.version),
        sha256_prefixed(question_payload),
    )
    question_descriptor = _Descriptor(
        record_type=question_ref.record_type,
        logical_id=question_ref.logical_id,
        version=question_ref.version,
        record_hash=question_ref.record_hash,
        payload=question_payload,
        outbound_refs=tuple(observation_refs),
        actor_id=question.created_by,
        recorded_at=question.created_at,
        allow_implicit_cas=True,
    )
    mechanism = binding.mechanism
    mechanism_payload = {
        **mechanism.as_dict(),
        "content_hash": mechanism.content_hash,
    }
    mechanism_ref = KnowledgeRef(
        "research_standard_mechanism",
        mechanism.mechanism_id,
        str(mechanism.version),
        sha256_prefixed(mechanism_payload),
    )
    mechanism_descriptor = _Descriptor(
        record_type=mechanism_ref.record_type,
        logical_id=mechanism_ref.logical_id,
        version=mechanism_ref.version,
        record_hash=mechanism_ref.record_hash,
        payload=mechanism_payload,
        outbound_refs=(),
        actor_id=binding.hypothesis_version.created_by,
        recorded_at=binding.hypothesis_version.created_at,
        allow_implicit_cas=True,
        volatile_replay_fields=("recorded_at",),
    )
    hypothesis = binding.hypothesis_version
    hypothesis_payload = hypothesis.as_dict()
    hypothesis_ref = _research_standard_hypothesis_ref(binding)
    hypothesis_descriptor = _Descriptor(
        record_type=hypothesis_ref.record_type,
        logical_id=hypothesis_ref.logical_id,
        version=hypothesis_ref.version,
        record_hash=hypothesis_ref.record_hash,
        payload=hypothesis_payload,
        outbound_refs=(question_ref, mechanism_ref, *parent_refs),
        actor_id=hypothesis.created_by,
        recorded_at=hypothesis.created_at,
        allow_implicit_cas=True,
    )
    binding_payload = binding.as_dict()
    binding_ref = _research_standard_binding_ref(binding)
    binding_descriptor = _Descriptor(
        record_type=binding_ref.record_type,
        logical_id=binding_ref.logical_id,
        version=binding_ref.version,
        record_hash=binding_ref.record_hash,
        payload=binding_payload,
        outbound_refs=(
            *observation_refs,
            question_ref,
            mechanism_ref,
            hypothesis_ref,
        ),
        actor_id=hypothesis.created_by,
        recorded_at=hypothesis.created_at,
        allow_implicit_cas=True,
    )
    return (
        *observation_descriptors,
        question_descriptor,
        mechanism_descriptor,
        hypothesis_descriptor,
        binding_descriptor,
    )


def _research_standard_binding_ref(
    binding: ResearchStandardBinding,
) -> KnowledgeRef:
    return KnowledgeRef(
        "research_standard_binding",
        binding.hypothesis_version.hypothesis_id,
        str(binding.hypothesis_version.version),
        sha256_prefixed(binding.as_dict()),
    )


def _research_standard_hypothesis_ref(
    binding: ResearchStandardBinding,
) -> KnowledgeRef:
    hypothesis = binding.hypothesis_version
    return KnowledgeRef(
        "research_standard_hypothesis",
        hypothesis.hypothesis_id,
        str(hypothesis.version),
        sha256_prefixed(hypothesis.as_dict()),
    )


def _research_standard_parent_refs(
    *,
    manager: ResearchPathManager,
    binding: ResearchStandardBinding,
) -> tuple[KnowledgeRef, ...]:
    """Resolve every declared parent to a previously published version row."""

    rows = _validated_rows(manager)
    parent_hashes = binding.hypothesis_version.parent_version_hashes
    refs: list[KnowledgeRef] = []
    for parent_hash in parent_hashes:
        matches = [
            row
            for row in rows
            if row.get("record_type") == "research_standard_hypothesis"
            and isinstance(row.get("payload"), dict)
            and row["payload"].get("content_hash") == parent_hash
        ]
        if not matches:
            raise KnowledgeRegistryError(
                "research_standard_hypothesis_parent_unpublished"
            )
        if len(matches) != 1:
            raise KnowledgeRegistryError(
                "research_standard_hypothesis_parent_ambiguous"
            )
        row = matches[0]
        refs.append(
            KnowledgeRef(
                "research_standard_hypothesis",
                str(row["logical_id"]),
                str(row["version"]),
                str(row["record_hash"]),
            )
        )
    _require_research_standard_successor(
        prior_rows=rows,
        successor=binding.hypothesis_version,
    )
    return tuple(refs)


def _require_research_standard_parent_lineage(
    *,
    manager: ResearchPathManager,
    binding: ResearchStandardBinding,
) -> None:
    expected_parent_refs = _research_standard_parent_refs(
        manager=manager,
        binding=binding,
    )
    hypothesis_ref = _research_standard_hypothesis_ref(binding)
    row = get_knowledge_record(
        manager=manager,
        record_type=hypothesis_ref.record_type,
        logical_id=hypothesis_ref.logical_id,
        version=hypothesis_ref.version,
    )
    if row.get("record_hash") != hypothesis_ref.record_hash:
        raise KnowledgeRegistryError(
            "validation_admission_research_standard_hypothesis_mismatch"
        )
    actual_parent_refs = [
        ref
        for ref in row.get("outbound_refs", [])
        if isinstance(ref, dict)
        and ref.get("record_type") == "research_standard_hypothesis"
    ]
    if actual_parent_refs != [ref.as_dict() for ref in expected_parent_refs]:
        raise KnowledgeRegistryError(
            "validation_admission_research_standard_parent_lineage_mismatch"
        )


def _research_standard_lineage_publication(
    *,
    rows: Mapping[tuple[str, str, str], dict[str, Any]],
    descriptors: tuple[_Descriptor, ...],
    binding: ResearchStandardBinding,
) -> dict[str, Any]:
    by_type: dict[str, list[dict[str, Any]]] = {}
    for descriptor in descriptors:
        by_type.setdefault(descriptor.record_type, []).append(
            deepcopy(rows[descriptor.key])
        )
    return {
        "schema_version": binding.schema_version,
        "binding_hash": binding.content_hash,
        "object_hashes": binding.lineage_hashes(),
        "binding": by_type["research_standard_binding"][0],
        "hypothesis_version": by_type["research_standard_hypothesis"][0],
        "research_question": by_type["research_standard_question"][0],
        "mechanism": by_type["research_standard_mechanism"][0],
        "observations": by_type["research_standard_observation"],
    }


def _lineage_publication(
    *,
    rows: Mapping[tuple[str, str, str], dict[str, Any]],
    descriptors: tuple[_Descriptor, ...],
    manager: ResearchPathManager,
) -> dict[str, Any]:
    hypothesis_descriptor = descriptors[-1]
    question_descriptor = next(
        item for item in descriptors if item.record_type == "research_question"
    )
    observation_descriptors = [
        item for item in descriptors if item.record_type == "observation"
    ]
    return {
        "path": str(knowledge_registry_path(manager).resolve()),
        "hypothesis": deepcopy(rows[hypothesis_descriptor.key]),
        "research_question": deepcopy(rows[question_descriptor.key]),
        "observations": [deepcopy(rows[item.key]) for item in observation_descriptors],
    }


def _manifest_component_hashes(manifest: Any) -> dict[str, str]:
    canonical = manifest.canonical_payload()
    component_material = {
        "manifest": canonical,
        "dataset": canonical.get("dataset"),
        "splits": getattr(manifest.dataset, "split").as_dict(),
        "parameter_space": canonical.get("parameter_space"),
        "gates": {
            "acceptance_gate": canonical.get("acceptance_gate"),
            "statistical_validation": canonical.get("statistical_validation"),
            "stress_suite": canonical.get("stress_suite"),
            "final_selection": canonical.get("final_selection"),
            "walk_forward": canonical.get("walk_forward"),
        },
        "metrics_and_exclusions": {
            "objective_metric": getattr(manifest, "raw", {}).get("objective_metric"),
            "metrics": getattr(manifest, "raw", {}).get("metrics"),
            "exclusions": getattr(manifest, "raw", {}).get("exclusions"),
            "final_selection": canonical.get("final_selection"),
        },
        "execution_and_cost": {
            "cost_model": canonical.get("cost_model"),
            "execution_model": canonical.get("execution_model"),
            "execution_timing": canonical.get("execution_timing"),
        },
        "portfolio_and_risk": {
            "portfolio_policy": canonical.get("portfolio_policy"),
            "risk_policy": canonical.get("risk_policy"),
        },
        "seed_policy": {
            "research_run": canonical.get("research_run"),
            "statistical_validation": canonical.get("statistical_validation"),
            "stress_suite": canonical.get("stress_suite"),
            "simulation_seed_scope_hash": manifest.simulation_seed_scope_hash(),
        },
    }
    if canonical.get("instrument") is not None:
        component_material["instrument_and_events"] = {
            "instrument": canonical.get("instrument"),
            "corporate_action_set": canonical.get("corporate_action_set"),
            "corporate_action_policy": canonical.get("corporate_action_policy"),
        }
    if (
        canonical.get("universe") is not None
        or canonical.get("market_calendar") is not None
    ):
        component_material["point_in_time_scope"] = {
            "universe": canonical.get("universe"),
            "market_calendar": canonical.get("market_calendar"),
            "selection_policy": (
                "membership_effective_and_observed_calendar_known_at_decision_"
                "latest_corporate_action_effective_and_observed_fail_closed_v1"
            ),
        }
    if canonical.get("research_standard_binding") is not None:
        component_material["research_standard_binding"] = canonical.get(
            "research_standard_binding"
        )
    hashes = {
        name: sha256_prefixed(value) for name, value in component_material.items()
    }
    if hashes["manifest"] != manifest.manifest_hash():
        raise KnowledgeRegistryError("validation_admission_manifest_hash_inconsistent")
    return dict(sorted(hashes.items()))


def _semantic_reasons(rows: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    latest: dict[tuple[str, str], str] = {}
    event_ids: set[str] = set()
    for index, row in enumerate(rows):
        key = _row_key(row)
        try:
            if row.get("schema_version") != KNOWLEDGE_REGISTRY_SCHEMA_VERSION:
                raise KnowledgeRegistryError("schema_version_unsupported")
            if row.get("event_type") != "record_published":
                raise KnowledgeRegistryError("event_type_unsupported")
            if any(not isinstance(item, str) or not item for item in key):
                raise KnowledgeRegistryError("record_identity_invalid")
            if key in seen:
                raise KnowledgeRegistryError("record_identity_duplicate")
            if row.get("event_id") != _event_id(key):
                raise KnowledgeRegistryError("event_id_mismatch")
            if row.get("event_id") in event_ids:
                raise KnowledgeRegistryError("event_id_duplicate")
            payload = row.get("payload")
            if not isinstance(payload, dict):
                raise KnowledgeRegistryError("payload_invalid")
            if row.get("record_hash") != sha256_prefixed(payload):
                raise KnowledgeRegistryError("record_hash_mismatch")
            expected_previous = latest.get((key[0], key[1]))
            if row.get("previous_record_hash") != expected_previous:
                raise KnowledgeRegistryError("previous_record_hash_mismatch")
            refs = row.get("outbound_refs")
            if not isinstance(refs, list):
                raise KnowledgeRegistryError("outbound_refs_invalid")
            parsed_refs = tuple(
                knowledge_ref_from_dict(item, context="registry.outbound_ref")
                for item in refs
            )
            _require_resolved_refs(list(seen.values()), parsed_refs)
            raw_authority_refs = row.get("authority_refs", [])
            if not isinstance(raw_authority_refs, list):
                raise KnowledgeRegistryError("authority_refs_invalid")
            parsed_authority_refs = tuple(
                authority_ref_from_dict(item, context="registry.authority_ref")
                for item in raw_authority_refs
            )
            validate_research_note_authority_refs(parsed_authority_refs)
            payload_authority_refs = payload.get(
                "source_authority_refs"
                if key[0] == "ai_advisory"
                else "authority_refs",
                [],
            )
            if key[0] in {"research_note", "ai_advisory"} and (
                payload_authority_refs != raw_authority_refs
            ):
                raise KnowledgeRegistryError("research_note_authority_refs_mismatch")
            if key[0] not in {"research_note", "ai_advisory"} and raw_authority_refs:
                raise KnowledgeRegistryError("authority_refs_record_type_invalid")
            _require_timestamp(
                str(row.get("recorded_at") or ""), "registry.recorded_at"
            )
            if not str(row.get("actor_id") or "").strip():
                raise KnowledgeRegistryError("actor_id_required")
            if key[0] == "decision":
                _validate_decision_payload(payload)
            if key[0] == "research_standard_hypothesis":
                _validate_research_standard_hypothesis_parent_refs(
                    payload=payload,
                    refs=[ref.as_dict() for ref in parsed_refs],
                    prior_rows=list(seen.values()),
                )
            if key[0] == "literature":
                literature = literature_spec_from_dict(payload)
                if literature.as_dict() != payload:
                    raise KnowledgeRegistryError("literature_payload_not_canonical")
                expected_refs = [
                    item.as_dict()
                    for item in (
                        *literature.references,
                        *(
                            relation.hypothesis_ref
                            for relation in literature.internal_hypothesis_relations
                        ),
                    )
                ]
                if refs != expected_refs:
                    raise KnowledgeRegistryError("literature_outbound_refs_mismatch")
                if (
                    key[1] != literature.literature_id
                    or key[2] != literature.version
                    or row.get("actor_id") != literature.actor_id
                    or row.get("recorded_at") != literature.recorded_at
                ):
                    raise KnowledgeRegistryError("literature_registry_binding_mismatch")
            if key[0] == "hypothesis_outcome":
                outcome = hypothesis_outcome_spec_from_dict(payload)
                if outcome.as_dict() != payload:
                    raise KnowledgeRegistryError(
                        "hypothesis_outcome_payload_not_canonical"
                    )
                expected_outcome_refs = [outcome.hypothesis_ref.as_dict()]
                if outcome.question_ref is not None:
                    expected_outcome_refs.append(outcome.question_ref.as_dict())
                if refs != expected_outcome_refs:
                    raise KnowledgeRegistryError(
                        "hypothesis_outcome_outbound_refs_mismatch"
                    )
                if (
                    key[1] != outcome.outcome_id
                    or key[2] != outcome.version
                    or row.get("actor_id") != outcome.actor_id
                    or row.get("recorded_at") != outcome.recorded_at
                ):
                    raise KnowledgeRegistryError(
                        "hypothesis_outcome_registry_binding_mismatch"
                    )
            if key[0] == "ai_advisory":
                _validate_ai_advisory_payload(payload)
                if payload.get("source_refs") != refs:
                    raise KnowledgeRegistryError("ai_advisory_source_refs_mismatch")
                if row.get("actor_id") != payload.get("generator_id"):
                    raise KnowledgeRegistryError("ai_advisory_generator_actor_mismatch")
            if key[0] == "ai_advisory_review":
                _validate_ai_advisory_review_payload(payload, seen)
                if refs != [payload.get("advisory_ref")]:
                    raise KnowledgeRegistryError("ai_advisory_review_ref_mismatch")
                if row.get("actor_id") != payload.get("reviewer_id"):
                    raise KnowledgeRegistryError("ai_advisory_review_actor_mismatch")
            seen[key] = row
            event_ids.add(str(row["event_id"]))
            latest[(key[0], key[1])] = str(row["record_hash"])
        except (
            KnowledgeContractError,
            KnowledgeRegistryError,
            TypeError,
            ValueError,
        ) as exc:
            reasons.append(f"knowledge_semantic_invalid:{index}:{exc}")
    return sorted(set(reasons))


def _validate_decision_payload(payload: Mapping[str, Any]) -> None:
    required = {
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
    }
    if set(payload) != required or payload.get("schema_version") != 1:
        raise KnowledgeRegistryError("decision_payload_fields_invalid")
    authority_ref_from_dict(payload.get("subject"), context="decision.subject")
    for field in (
        "evidence_hashes",
        "alternatives",
        "expected_effects",
        "risks",
        "proposer_ids",
    ):
        value = payload.get(field)
        if not isinstance(value, list) or not value:
            raise KnowledgeRegistryError(f"decision_{field}_required")
    approver = payload.get("approver")
    if not isinstance(approver, dict) or set(approver) != {
        "approver_type",
        "approver_id",
        "role",
    }:
        raise KnowledgeRegistryError("decision_approver_invalid")
    if approver.get("approver_type") == "human" and approver.get(
        "approver_id"
    ) in payload.get("proposer_ids", []):
        raise KnowledgeRegistryError("decision_approver_separation_violation")
    if payload.get("supersedes") is not None:
        ref = knowledge_ref_from_dict(
            payload["supersedes"], context="decision.supersedes"
        )
        if ref.record_type != "decision":
            raise KnowledgeRegistryError("decision_supersedes_invalid")


def _validate_ai_advisory_payload(payload: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "advisory_id",
        "version",
        "task_type",
        "generator_id",
        "provider_id",
        "model_id",
        "model_configuration_hash",
        "prompt_hash",
        "source_refs",
        "source_authority_refs",
        "output_text",
        "output_hash",
        "generated_at",
        "review_status",
        "authority_scope",
    }
    if set(payload) != required or payload.get("schema_version") != 1:
        raise KnowledgeRegistryError("ai_advisory_payload_fields_invalid")
    if payload.get("review_status") != "pending_human_review":
        raise KnowledgeRegistryError("ai_advisory_cannot_self_approve")
    if payload.get("authority_scope") != "advisory_only_no_domain_mutation":
        raise KnowledgeRegistryError("ai_advisory_authority_scope_invalid")
    if payload.get("output_hash") != sha256_prefixed(
        {"output_text": payload.get("output_text")}, label="ai_advisory_output"
    ):
        raise KnowledgeRegistryError("ai_advisory_output_hash_mismatch")
    try:
        parsed = AIAdvisorySpec(
            schema_version=int(payload["schema_version"]),
            advisory_id=str(payload["advisory_id"]),
            version=str(payload["version"]),
            task_type=str(payload["task_type"]),
            generator_id=str(payload["generator_id"]),
            provider_id=str(payload["provider_id"]),
            model_id=str(payload["model_id"]),
            model_configuration_hash=str(payload["model_configuration_hash"]),
            prompt_hash=str(payload["prompt_hash"]),
            source_refs=tuple(
                knowledge_ref_from_dict(item, context="ai_advisory.source_ref")
                for item in payload["source_refs"]
            ),
            source_authority_refs=tuple(
                authority_ref_from_dict(
                    item, context="ai_advisory.source_authority_ref"
                )
                for item in payload["source_authority_refs"]
            ),
            output_text=str(payload["output_text"]),
            generated_at=str(payload["generated_at"]),
            review_status=str(payload["review_status"]),
            authority_scope=str(payload["authority_scope"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise KnowledgeRegistryError("ai_advisory_payload_invalid") from exc
    if parsed.as_dict() != dict(payload):
        raise KnowledgeRegistryError("ai_advisory_payload_not_canonical")


def _validate_ai_advisory_review_payload(
    payload: Mapping[str, Any],
    seen: Mapping[tuple[str, str, str], dict[str, Any]],
) -> None:
    required = {
        "schema_version",
        "review_id",
        "version",
        "advisory_ref",
        "reviewer_id",
        "reviewer_role",
        "decision",
        "rationale",
        "evidence_hashes",
        "reviewed_at",
        "authority_scope",
        "reviewer_type",
    }
    if set(payload) != required or payload.get("schema_version") != 1:
        raise KnowledgeRegistryError("ai_advisory_review_payload_fields_invalid")
    if payload.get("authority_scope") != "advisory_output_only":
        raise KnowledgeRegistryError("ai_advisory_review_authority_scope_invalid")
    ref = knowledge_ref_from_dict(
        payload.get("advisory_ref"), context="ai_advisory_review.advisory_ref"
    )
    if ref.record_type != "ai_advisory":
        raise KnowledgeRegistryError("ai_advisory_review_reference_invalid")
    advisory = seen.get((ref.record_type, ref.logical_id, ref.version))
    if advisory is None or advisory.get("record_hash") != ref.record_hash:
        raise KnowledgeRegistryError("ai_advisory_review_reference_missing")
    generator_id = str((advisory.get("payload") or {}).get("generator_id") or "")
    if generator_id == payload.get("reviewer_id"):
        raise KnowledgeRegistryError("ai_advisory_review_separation_violation")
    try:
        parsed = AIAdvisoryReview(
            schema_version=int(payload["schema_version"]),
            review_id=str(payload["review_id"]),
            version=str(payload["version"]),
            advisory_ref=ref,
            reviewer_id=str(payload["reviewer_id"]),
            reviewer_role=str(payload["reviewer_role"]),
            decision=str(payload["decision"]),
            rationale=str(payload["rationale"]),
            evidence_hashes=tuple(str(item) for item in payload["evidence_hashes"]),
            reviewed_at=str(payload["reviewed_at"]),
            authority_scope=str(payload["authority_scope"]),
            reviewer_type=str(payload["reviewer_type"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise KnowledgeRegistryError("ai_advisory_review_payload_invalid") from exc
    if parsed.as_dict() != dict(payload):
        raise KnowledgeRegistryError("ai_advisory_review_payload_not_canonical")
    advisory_generated_at = str(
        (advisory.get("payload") or {}).get("generated_at") or ""
    )
    if datetime.fromisoformat(parsed.reviewed_at) < datetime.fromisoformat(
        advisory_generated_at
    ):
        raise KnowledgeRegistryError("ai_advisory_review_before_generation")


def _require_resolved_refs(
    rows: list[dict[str, Any]], refs: tuple[KnowledgeRef, ...]
) -> None:
    index = {
        (
            str(row.get("record_type")),
            str(row.get("logical_id")),
            str(row.get("version")),
            str(row.get("record_hash")),
        )
        for row in rows
    }
    for ref in refs:
        key = (ref.record_type, ref.logical_id, ref.version, ref.record_hash)
        if key not in index:
            raise KnowledgeRegistryError(
                "knowledge_reference_orphan:"
                + ":".join((ref.record_type, ref.logical_id, ref.version))
            )


def _validate_research_standard_hypothesis_parent_refs(
    *,
    payload: Mapping[str, Any],
    refs: list[dict[str, Any]],
    prior_rows: list[dict[str, Any]],
) -> None:
    successor = _parse_registry_hypothesis_version(payload)
    _require_research_standard_successor(
        prior_rows=prior_rows,
        successor=successor,
    )
    parent_hashes = payload.get("parent_version_hashes")
    if not isinstance(parent_hashes, list) or any(
        not isinstance(value, str) for value in parent_hashes
    ):
        raise KnowledgeRegistryError(
            "research_standard_hypothesis_parent_hashes_invalid"
        )
    identity_payload = {
        key: value for key, value in payload.items() if key != "content_hash"
    }
    if payload.get("content_hash") != sha256_prefixed(
        identity_payload,
        label="hypothesis_version_v2",
    ):
        raise KnowledgeRegistryError(
            "research_standard_hypothesis_content_hash_mismatch"
        )
    expected_parent_refs: list[dict[str, Any]] = []
    for parent_hash in parent_hashes:
        matches = [
            row
            for row in prior_rows
            if row.get("record_type") == "research_standard_hypothesis"
            and isinstance(row.get("payload"), dict)
            and row["payload"].get("content_hash") == parent_hash
        ]
        if len(matches) != 1:
            raise KnowledgeRegistryError(
                "research_standard_hypothesis_parent_unresolved"
            )
        parent = matches[0]
        expected_parent_refs.append(
            {
                "record_type": "research_standard_hypothesis",
                "logical_id": parent["logical_id"],
                "version": parent["version"],
                "record_hash": parent["record_hash"],
            }
        )
    actual_parent_refs = [
        ref for ref in refs if ref.get("record_type") == "research_standard_hypothesis"
    ]
    if actual_parent_refs != expected_parent_refs:
        raise KnowledgeRegistryError(
            "research_standard_hypothesis_parent_refs_mismatch"
        )


def _parse_registry_hypothesis_version(
    payload: Mapping[str, Any],
) -> HypothesisVersion:
    try:
        return parse_hypothesis_version(payload)
    except ResearchStandardError as exc:
        raise KnowledgeRegistryError(
            f"research_standard_hypothesis_payload_invalid:{exc}"
        ) from exc


def _require_research_standard_successor(
    *,
    prior_rows: list[dict[str, Any]],
    successor: HypothesisVersion,
) -> None:
    """Enforce same-identity continuity without blocking cross-ID derivation."""

    previous_rows = [
        row
        for row in prior_rows
        if row.get("record_type") == "research_standard_hypothesis"
        and row.get("logical_id") == successor.hypothesis_id
        and row.get("version") != str(successor.version)
    ]
    if not previous_rows:
        if successor.version != 1:
            raise KnowledgeRegistryError(
                "hypothesis_successor_previous_version_missing"
            )
        return
    previous_payload = previous_rows[-1].get("payload")
    if not isinstance(previous_payload, Mapping):
        raise KnowledgeRegistryError(
            "research_standard_hypothesis_previous_payload_invalid"
        )
    previous = _parse_registry_hypothesis_version(previous_payload)
    try:
        verify_hypothesis_successor(previous, successor)
    except ResearchStandardError as exc:
        raise KnowledgeRegistryError(str(exc)) from exc


def _require_compatible_research_standard_lineage(
    *,
    hypothesis: HypothesisSpec,
    binding: ResearchStandardBinding,
) -> None:
    """Reject a publication whose rich and compatibility graphs disagree."""

    market = hypothesis.observations[0].market if hypothesis.observations else ""
    try:
        validate_compatibility_hypothesis_binding(
            binding,
            hypothesis,
            manifest_hypothesis=str(hypothesis.hypothesis_text or ""),
            market=market,
        )
    except ResearchStandardError as exc:
        raise KnowledgeRegistryError(str(exc)) from exc


def _validated_rows(manager: ResearchPathManager) -> list[dict[str, Any]]:
    path = knowledge_registry_path(manager)
    try:
        snapshot = read_hash_chained_jsonl_snapshot(
            path=path,
            label=KNOWLEDGE_REGISTRY_HASH_LABEL,
        )
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise KnowledgeRegistryError("knowledge_registry_invalid") from exc
    reasons = [*snapshot.reasons, *_semantic_reasons(list(snapshot.rows))]
    if reasons:
        raise KnowledgeRegistryError("knowledge_registry_invalid:" + ",".join(reasons))
    return [deepcopy(row) for row in snapshot.rows]


def _resolve_ref(rows: list[dict[str, Any]], value: object) -> dict[str, Any]:
    ref = knowledge_ref_from_dict(value)
    matches = [
        row
        for row in rows
        if (
            row.get("record_type"),
            row.get("logical_id"),
            row.get("version"),
            row.get("record_hash"),
        )
        == (ref.record_type, ref.logical_id, ref.version, ref.record_hash)
    ]
    if len(matches) != 1:
        raise KnowledgeRegistryError("knowledge_reference_resolution_failed")
    return deepcopy(matches[0])


def _row_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("record_type") or ""),
        str(row.get("logical_id") or ""),
        str(row.get("version") or ""),
    )


def _event_id(key: tuple[str, str, str]) -> str:
    return sha256_prefixed(
        {"record_type": key[0], "logical_id": key[1], "version": key[2]},
        label="knowledge_registry_event",
    )


def _validate_knowledge_proof_material(
    *,
    target_ref: KnowledgeRef,
    rows: list[dict[str, Any]],
    stream_hash: str,
) -> None:
    if not rows:
        raise KnowledgeRegistryError("knowledge_proof_rows_required")
    prior_hash: str | None = None
    required_row_fields = {
        "schema_version",
        "event_id",
        "event_type",
        "record_type",
        "logical_id",
        "version",
        "record_hash",
        "previous_record_hash",
        "outbound_refs",
        "payload",
        "actor_id",
        "recorded_at",
        "sequence",
        "prior_hash",
        "row_hash",
    }
    for index, row in enumerate(rows):
        row_fields = frozenset(row)
        if row_fields not in {
            frozenset(required_row_fields),
            frozenset((*required_row_fields, "authority_refs")),
        }:
            raise KnowledgeRegistryError(f"knowledge_proof_row_fields_invalid:{index}")
        if row.get("sequence") != index:
            raise KnowledgeRegistryError(f"knowledge_proof_sequence_mismatch:{index}")
        if row.get("prior_hash") != prior_hash:
            raise KnowledgeRegistryError(f"knowledge_proof_prior_hash_mismatch:{index}")
        material = {key: value for key, value in row.items() if key != "row_hash"}
        try:
            expected_hash = sha256_prefixed(
                content_hash_payload(material),
                label=f"{KNOWLEDGE_REGISTRY_HASH_LABEL}_row",
            )
        except (TypeError, ValueError) as exc:
            raise KnowledgeRegistryError("knowledge_proof_row_not_canonical") from exc
        if row.get("row_hash") != expected_hash:
            raise KnowledgeRegistryError(f"knowledge_proof_row_hash_mismatch:{index}")
        prior_hash = expected_hash
    if stream_hash != prior_hash:
        raise KnowledgeRegistryError("knowledge_proof_stream_hash_mismatch")
    last = rows[-1]
    if (
        last.get("record_type"),
        last.get("logical_id"),
        last.get("version"),
        last.get("record_hash"),
    ) != (
        target_ref.record_type,
        target_ref.logical_id,
        target_ref.version,
        target_ref.record_hash,
    ):
        raise KnowledgeRegistryError("knowledge_proof_target_not_terminal")
    semantic_reasons = _semantic_reasons(rows)
    if semantic_reasons:
        raise KnowledgeRegistryError(
            "knowledge_proof_semantic_invalid:" + ",".join(semantic_reasons)
        )


def _proof_mapping(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise KnowledgeRegistryError(f"{context}_object_required")
    return value


def _proof_list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise KnowledgeRegistryError(f"{context}_array_required")
    return value


def _proof_text(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise KnowledgeRegistryError(f"{context}_string_required")
    return value


def _proof_integer(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise KnowledgeRegistryError(f"{context}_integer_required")
    return value


def _require_timestamp(value: str, context: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise KnowledgeRegistryError(f"{context}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise KnowledgeRegistryError(f"{context}_timezone_required")
