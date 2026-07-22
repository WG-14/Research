from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.derivatives.knowledge_evidence import (
    DerivativeKnowledgeEvidenceArchive,
    DerivativeKnowledgeEvidenceError,
    verify_derivative_knowledge_evidence_archive,
)
from market_research.research.hashing import (
    content_hash_payload,
    sha256_prefixed,
)
from market_research.research.hypothesis_contract import parse_hypothesis_spec
from market_research.research.knowledge_contract import (
    AuthorityRef,
    DecisionAlternative,
    DecisionApprover,
    DecisionRecord,
    DecisionRisk,
    HypothesisFailureClassification,
    HypothesisOutcomeSpec,
    InternalHypothesisRelation,
    InternalHypothesisRelationType,
    KnowledgeRef,
    LiteratureReproductionStatus,
    LiteratureSource,
    LiteratureSourceType,
    LiteratureSpec,
    ResearchNoteSpec,
)
from market_research.research.knowledge_registry import (
    KNOWLEDGE_REGISTRY_HASH_LABEL,
    KnowledgeRegistryProof,
    export_knowledge_registry_proof,
    publish_decision_record,
    publish_hypothesis_outcome,
    publish_literature,
    publish_manifest_lineage,
    publish_research_note,
)
from market_research.settings import ResearchSettings
from tests.hypothesis_lineage_fixture import hypothesis_spec_v2


def _manager(root: Path) -> ResearchPathManager:
    return ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=root / "data",
            artifact_root=root / "artifacts",
            report_root=root / "reports",
            cache_root=root / "cache",
            db_path=None,
            max_workers=1,
            random_seed=0,
        ),
        project_root=Path.cwd(),
    )


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _lineage(
    manager: ResearchPathManager, suffix: str
) -> tuple[KnowledgeRef, KnowledgeRef]:
    raw = hypothesis_spec_v2(
        hypothesis_id=f"derivative-hypothesis-{suffix}",
        hypothesis_text=f"Derivative hypothesis {suffix} survives frozen costs.",
        experiment_family_id=f"derivative-family-{suffix}",
    )
    observations = raw["observations"]
    assert isinstance(observations, list)
    observation = observations[0]
    assert isinstance(observation, dict)
    observation["observation_id"] = f"derivative-observation-{suffix}"
    observation_ref = {
        "observation_id": observation["observation_id"],
        "version": observation["version"],
        "observation_hash": sha256_prefixed(observation),
    }

    question = raw["research_question"]
    assert isinstance(question, dict)
    question["question_id"] = f"derivative-question-{suffix}"
    question["observation_refs"] = [deepcopy(observation_ref)]
    question["competing_hypotheses"] = [
        {
            "hypothesis_id": raw["hypothesis_id"],
            "version": raw["version"],
            "hypothesis_text": raw["hypothesis_text"],
        },
        {
            "hypothesis_id": f"derivative-null-{suffix}",
            "version": "1.0.0",
            "hypothesis_text": "The frozen derivative edge is absent.",
        },
    ]
    question_ref = {
        "question_id": question["question_id"],
        "version": question["version"],
        "question_hash": sha256_prefixed(question),
    }
    raw["observation_refs"] = [deepcopy(observation_ref)]
    raw["research_question_ref"] = question_ref

    publication = publish_manifest_lineage(
        manager=manager,
        hypothesis=parse_hypothesis_spec(raw),
    )
    hypothesis_row = publication["hypothesis"]
    question_row = publication["research_question"]
    return (
        KnowledgeRef(
            "hypothesis",
            str(hypothesis_row["logical_id"]),
            str(hypothesis_row["version"]),
            str(hypothesis_row["record_hash"]),
        ),
        KnowledgeRef(
            "research_question",
            str(question_row["logical_id"]),
            str(question_row["version"]),
            str(question_row["record_hash"]),
        ),
    )


def _literature(hypothesis_ref: KnowledgeRef) -> LiteratureSpec:
    return LiteratureSpec(
        schema_version=2,
        literature_id="derivative-liquidity-paper",
        version="2",
        title="Derivative liquidity and execution",
        citation="Journal of Derivative Research 7 (2025)",
        actor_id="literature-reviewer",
        recorded_at="2026-01-03T00:00:00+00:00",
        source=LiteratureSource(
            source_type=LiteratureSourceType.JOURNAL_ARTICLE,
            publisher="Journal of Derivative Research",
            locator="https://example.test/derivative-liquidity",
            content_hash=_hash("a"),
        ),
        published_at="2025-06-01T00:00:00+00:00",
        accessed_at="2026-01-02T00:00:00+00:00",
        key_claims=("Quoted option liquidity overstates executable liquidity.",),
        reproduction_status=LiteratureReproductionStatus.REPRODUCED,
        reproduction_evidence_hashes=(_hash("b"),),
        internal_hypothesis_relations=(
            InternalHypothesisRelation(
                hypothesis_ref=hypothesis_ref,
                relation=InternalHypothesisRelationType.CONTEXTUALIZES,
                rationale="The source identifies the same liquidity mechanism.",
            ),
        ),
    )


def _outcome(
    hypothesis_ref: KnowledgeRef, question_ref: KnowledgeRef
) -> HypothesisOutcomeSpec:
    return HypothesisOutcomeSpec(
        schema_version=2,
        outcome_id="derivative-confirmatory-outcome",
        version="2",
        hypothesis_ref=hypothesis_ref,
        question_ref=question_ref,
        outcome="failed",
        rationale="The edge disappeared under executable bid-ask assumptions.",
        actor_id="validation-reviewer",
        recorded_at="2026-01-04T00:00:00+00:00",
        evidence_hashes=(_hash("c"),),
        failure_classification=HypothesisFailureClassification.MIDPOINT_ILLUSION,
    )


def _decision(
    *,
    outcome: HypothesisOutcomeSpec,
    literature: LiteratureSpec,
    conclusion_hash: str,
    authority: str = "derivative_research_conclusion",
    subject_type: str = "research_conclusion",
    include_literature_hash: bool = True,
) -> DecisionRecord:
    evidence_hashes = [conclusion_hash, outcome.contract_hash()]
    if include_literature_hash:
        evidence_hashes.append(literature.contract_hash())
    return DecisionRecord(
        schema_version=1,
        decision_id="derivative-conclusion-decision",
        version="1",
        decision_type="derivative_research_conclusion_review",
        subject=AuthorityRef(
            authority=authority,
            subject_type=subject_type,
            subject_id="derivative-conclusion",
            subject_version="1",
            authority_hash=conclusion_hash,
        ),
        chosen_action="retain_failed_hypothesis_outcome",
        rationale="Immutable evidence supports retaining the negative result.",
        evidence_hashes=tuple(evidence_hashes),
        alternatives=(
            DecisionAlternative(
                alternative_id="discard-negative-result",
                description="Discard the failed derivative hypothesis.",
                rejection_reason="Failure evidence is required organizational knowledge.",
            ),
        ),
        expected_effects=("The failed result remains reproducible and searchable.",),
        risks=(
            DecisionRisk(
                risk_id="future-market-change",
                description="A later market structure may differ.",
                severity="medium",
                mitigation="Version a new hypothesis instead of rewriting this result.",
            ),
        ),
        proposer_ids=("derivative-researcher",),
        approver=DecisionApprover(
            approver_type="human",
            approver_id="derivative-reviewer",
            role="research_reviewer",
        ),
        policy_version="derivative-conclusion-policy.v1",
        decided_at="2026-01-05T00:00:00+00:00",
    )


def _archive(
    root: Path,
    *,
    relation_to_other_hypothesis: bool = False,
    authority: str = "derivative_research_conclusion",
    subject_type: str = "research_conclusion",
    include_literature_hash: bool = True,
    insert_registry_row: bool = False,
) -> DerivativeKnowledgeEvidenceArchive:
    manager = _manager(root)
    hypothesis_ref, question_ref = _lineage(manager, "primary")
    relation_ref = hypothesis_ref
    if relation_to_other_hypothesis:
        relation_ref, _ = _lineage(manager, "other")
    if insert_registry_row:
        publish_research_note(
            manager=manager,
            note=ResearchNoteSpec(
                schema_version=1,
                note_id="independent-registry-note",
                version="1",
                note_type="research_note",
                title="Independent branch marker",
                body="This record changes the detached registry prefix.",
                actor_id="researcher",
                recorded_at="2026-01-01T00:00:00+00:00",
                status="active",
            ),
        )
    literature = _literature(relation_ref)
    outcome = _outcome(hypothesis_ref, question_ref)
    publish_literature(manager=manager, literature=literature)
    publish_hypothesis_outcome(manager=manager, outcome=outcome)
    conclusion_hash = _hash("f")
    decision = _decision(
        outcome=outcome,
        literature=literature,
        conclusion_hash=conclusion_hash,
        authority=authority,
        subject_type=subject_type,
        include_literature_hash=include_literature_hash,
    )
    publish_decision_record(manager=manager, decision=decision)
    return DerivativeKnowledgeEvidenceArchive(
        archive_id="derivative-knowledge-archive",
        version="1",
        conclusion_id="derivative-conclusion",
        conclusion_version="1",
        conclusion_hash=conclusion_hash,
        outcome_proof=export_knowledge_registry_proof(
            manager=manager, target_ref=outcome.ref()
        ),
        literature_proofs=(
            export_knowledge_registry_proof(
                manager=manager, target_ref=literature.ref()
            ),
        ),
        decision_proof=export_knowledge_registry_proof(
            manager=manager, target_ref=decision.ref()
        ),
        assembled_at="2026-01-06T00:00:00+00:00",
    )


def test_archive_round_trips_typed_hash_bound_registry_proofs(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path)

    assert archive.hypothesis_outcome.schema_version == 2
    assert all(item.schema_version == 2 for item in archive.literature_records)
    assert archive.hypothesis_ref == archive.hypothesis_outcome.hypothesis_ref
    assert archive.decision_record.subject.subject_type == "research_conclusion"
    assert archive.content_hash == sha256_prefixed(
        archive.identity_payload(), label="derivative_knowledge_evidence_archive"
    )
    assert DerivativeKnowledgeEvidenceArchive.from_dict(archive.as_dict()) == archive
    assert verify_derivative_knowledge_evidence_archive(archive) == archive


@pytest.mark.parametrize(
    ("authority", "subject_type"),
    [
        ("research_governance", "research_conclusion"),
        ("derivative_research_conclusion", "conclusion"),
    ],
)
def test_archive_rejects_wrong_decision_subject_authority_or_type(
    tmp_path: Path, authority: str, subject_type: str
) -> None:
    with pytest.raises(
        DerivativeKnowledgeEvidenceError, match="decision_authority_mismatch"
    ):
        _archive(tmp_path, authority=authority, subject_type=subject_type)


def test_archive_rejects_mismatched_hypothesis_relation_and_missing_evidence(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        DerivativeKnowledgeEvidenceError, match="hypothesis_relation_mismatch"
    ):
        _archive(tmp_path / "relation", relation_to_other_hypothesis=True)
    with pytest.raises(
        DerivativeKnowledgeEvidenceError, match="decision_evidence_missing"
    ):
        _archive(tmp_path / "evidence", include_literature_hash=False)


def test_archive_rejects_proofs_from_a_different_registry_prefix(
    tmp_path: Path,
) -> None:
    first = _archive(tmp_path / "first")
    second = _archive(tmp_path / "second", insert_registry_row=True)

    with pytest.raises(
        DerivativeKnowledgeEvidenceError, match="registry_prefix_mismatch"
    ):
        replace(first, decision_proof=second.decision_proof)


def test_archive_rejects_serialized_tamper_and_unknown_fields(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path)
    tampered = deepcopy(archive.as_dict())
    literature_proofs = tampered["literature_proofs"]
    assert isinstance(literature_proofs, list)
    proof = literature_proofs[0]
    assert isinstance(proof, dict)
    rows = proof["rows"]
    assert isinstance(rows, list)
    row = rows[-1]
    assert isinstance(row, dict)
    payload = row["payload"]
    assert isinstance(payload, dict)
    payload["title"] = "Forged literature title"
    with pytest.raises(
        DerivativeKnowledgeEvidenceError, match="embedded_proof_invalid"
    ):
        DerivativeKnowledgeEvidenceArchive.from_dict(tampered)

    unknown = deepcopy(archive.as_dict())
    unknown["unreviewed_extension"] = True
    with pytest.raises(DerivativeKnowledgeEvidenceError, match="fields_invalid"):
        DerivativeKnowledgeEvidenceArchive.from_dict(unknown)


def test_strict_decision_parser_rejects_self_consistent_nested_extension(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path)
    rows = [deepcopy(dict(row)) for row in archive.decision_proof.rows]
    terminal = rows[-1]
    payload = terminal["payload"]
    assert isinstance(payload, dict)
    alternatives = payload["alternatives"]
    assert isinstance(alternatives, list)
    alternative = alternatives[0]
    assert isinstance(alternative, dict)
    alternative["unreviewed_extension"] = True
    terminal["record_hash"] = sha256_prefixed(payload)
    material = {key: value for key, value in terminal.items() if key != "row_hash"}
    terminal["row_hash"] = sha256_prefixed(
        content_hash_payload(material),
        label=f"{KNOWLEDGE_REGISTRY_HASH_LABEL}_row",
    )
    forged_ref = KnowledgeRef(
        "decision",
        archive.decision_proof.target_ref.logical_id,
        archive.decision_proof.target_ref.version,
        str(terminal["record_hash"]),
    )
    forged_proof = KnowledgeRegistryProof(
        target_ref=forged_ref,
        rows=tuple(rows),
        stream_hash=str(terminal["row_hash"]),
    )

    with pytest.raises(DerivativeKnowledgeEvidenceError, match="decision_invalid"):
        replace(archive, decision_proof=forged_proof)


def test_knowledge_archive_addition_preserves_schema_v1_contract_hashes() -> None:
    legacy_literature = LiteratureSpec(
        schema_version=1,
        literature_id="paper",
        version="1",
        title="Title",
        citation="Citation",
        actor_id="actor",
        recorded_at="2026-01-01T00:00:00+00:00",
        source_uri="https://example.test/paper",
        source_content_hash=_hash("a"),
    )
    legacy_outcome = HypothesisOutcomeSpec(
        schema_version=1,
        outcome_id="outcome",
        version="1",
        hypothesis_ref=KnowledgeRef("hypothesis", "hyp", "1", _hash("a")),
        outcome="failed",
        rationale="Failed.",
        actor_id="actor",
        recorded_at="2026-01-02T00:00:00+00:00",
        evidence_hashes=(_hash("a"),),
    )

    assert legacy_literature.contract_hash() == (
        "sha256:913f705c7f844ff2ce73d0687697dc60c7665c47b308f192aaca06efb896c012"
    )
    assert legacy_outcome.contract_hash() == (
        "sha256:5b8f4765a344e2bc7164e17636018f039258abd08868e6156df7c17667de4aed"
    )
