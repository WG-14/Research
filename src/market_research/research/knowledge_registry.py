"""Repository-external append-only authority for research knowledge.

The registry is a single hash-chained JSONL stream.  Observation, question,
hypothesis and preregistration publication is one locked mutation so readers
can never observe a partially published lineage.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
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
    knowledge_ref_from_dict,
    validate_research_note_authority_refs,
)


KNOWLEDGE_REGISTRY_SCHEMA_VERSION = 1
KNOWLEDGE_REGISTRY_HASH_LABEL = "research_knowledge_registry"


class KnowledgeRegistryError(ValueError):
    """The knowledge stream or a requested registry mutation is invalid."""


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
    descriptor = _Descriptor(
        record_type="literature",
        logical_id=literature.literature_id,
        version=literature.version,
        record_hash=literature.contract_hash(),
        payload=literature.as_dict(),
        outbound_refs=literature.references,
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
) -> dict[str, Any]:
    """Atomically publish the complete inline observation/question lineage."""

    descriptors = _lineage_descriptors(hypothesis)
    rows = _publish_descriptors(manager=manager, descriptors=descriptors)
    return _lineage_publication(rows=rows, descriptors=descriptors, manager=manager)


def freeze_validation_admission(
    *,
    manager: ResearchPathManager,
    manifest: Any,
    admitted_at: str | None = None,
) -> dict[str, Any]:
    """Materialize lineage and freeze the validation manifest before data access.

    An inline ``pre_registered`` hypothesis may bind immutable evidence created
    outside this registry.  Admission retains that evidence hash while making
    the current manifest a canonical, queryable registry record.
    """

    hypothesis = getattr(manifest, "hypothesis_spec", None)
    if not isinstance(hypothesis, HypothesisSpec) or hypothesis.schema_version != 2:
        raise KnowledgeRegistryError("validation_admission_hypothesis_lineage_required")
    if admitted_at is not None:
        _require_timestamp(admitted_at, "validation_admission.admitted_at")
    timestamp = admitted_at or datetime.now(timezone.utc).isoformat()
    component_hashes = _manifest_component_hashes(manifest)
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
    descriptors = (
        *_lineage_descriptors(hypothesis),
        _preregistration_descriptor(preregistration),
    )
    rows = _publish_descriptors(manager=manager, descriptors=descriptors)
    publication = _lineage_publication(
        rows=rows,
        descriptors=descriptors[:-1],
        manager=manager,
    )
    admission_row = rows[descriptors[-1].key]
    return {
        **publication,
        "admission": deepcopy(admission_row),
        "admission_record_hash": admission_row["record_hash"],
        "admission_row_hash": admission_row["row_hash"],
        "manifest_hash": manifest_hash,
        "component_hashes": component_hashes,
    }


def require_validation_admission(
    *,
    manager: ResearchPathManager,
    manifest: Any,
    expected_row_hash: str | None = None,
) -> dict[str, Any]:
    """Return the exact canonical admission or fail on manifest/component drift."""

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
    if payload.get("component_hashes") != _manifest_component_hashes(manifest):
        raise KnowledgeRegistryError("validation_admission_component_hash_mismatch")
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


def _preregistration_descriptor(record: PreregistrationRecord) -> _Descriptor:
    return _Descriptor(
        record_type="preregistration",
        logical_id=record.registration_id,
        version=record.version,
        record_hash=record.contract_hash(),
        payload=record.as_dict(),
        outbound_refs=(record.hypothesis_ref,),
        actor_id=record.actor_id,
        recorded_at=record.frozen_at,
        allow_implicit_cas=True,
        volatile_replay_fields=("frozen_at",),
    )


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


def _require_timestamp(value: str, context: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise KnowledgeRegistryError(f"{context}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise KnowledgeRegistryError(f"{context}_timezone_required")
