from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.hashing import content_hash_payload, sha256_prefixed
from market_research.research.hypothesis_contract import parse_hypothesis_spec
from market_research.research.knowledge_contract import (
    HypothesisFailureClassification,
    HypothesisOutcomeSpec,
    InternalHypothesisRelation,
    InternalHypothesisRelationType,
    KnowledgeContractError,
    KnowledgeRef,
    LiteratureReproductionStatus,
    LiteratureSource,
    LiteratureSourceType,
    LiteratureSpec,
    hypothesis_outcome_spec_from_dict,
    literature_spec_from_dict,
)
from market_research.research.knowledge_registry import (
    KNOWLEDGE_REGISTRY_HASH_LABEL,
    KnowledgeRegistryError,
    KnowledgeRegistryProof,
    export_knowledge_registry_proof,
    get_knowledge_record,
    knowledge_registry_path,
    publish_hypothesis_outcome,
    publish_literature,
    publish_manifest_lineage,
    query_outbound_refs,
    validate_knowledge_registry,
    verify_knowledge_registry_external_evidence,
    verify_knowledge_registry_proof,
)
from market_research.settings import ResearchSettings
from tests.hypothesis_lineage_fixture import hypothesis_spec_v2


def _manager(tmp_path: Path) -> ResearchPathManager:
    return ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "data",
            artifact_root=tmp_path / "artifacts",
            report_root=tmp_path / "reports",
            cache_root=tmp_path / "cache",
            db_path=None,
            max_workers=1,
            random_seed=0,
        ),
        project_root=Path.cwd(),
    )


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _lineage(
    manager: ResearchPathManager,
) -> tuple[KnowledgeRef, KnowledgeRef]:
    spec = parse_hypothesis_spec(hypothesis_spec_v2())
    publication = publish_manifest_lineage(manager=manager, hypothesis=spec)
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
        literature_id="paper-cost-aware-edge",
        version="2",
        title="Cost-aware derivative research",
        citation="Research Journal 12 (2025)",
        actor_id="literature-reviewer",
        recorded_at="2026-01-03T00:00:00+00:00",
        source=LiteratureSource(
            source_type=LiteratureSourceType.JOURNAL_ARTICLE,
            publisher="Research Journal",
            locator="https://example.test/papers/cost-aware-edge",
            content_hash=_hash("b"),
        ),
        published_at="2025-06-01T00:00:00+00:00",
        accessed_at="2026-01-02T00:00:00+00:00",
        key_claims=(
            "Reported expectancy is sensitive to transaction costs.",
            "Point-in-time inputs are required for replication.",
        ),
        reproduction_status=LiteratureReproductionStatus.REPRODUCED,
        reproduction_evidence_hashes=(_hash("c"),),
        internal_hypothesis_relations=(
            InternalHypothesisRelation(
                hypothesis_ref=hypothesis_ref,
                relation=InternalHypothesisRelationType.CONTEXTUALIZES,
                rationale="The paper defines a comparable cost-sensitivity mechanism.",
            ),
        ),
    )


def _outcome(
    hypothesis_ref: KnowledgeRef, question_ref: KnowledgeRef
) -> HypothesisOutcomeSpec:
    return HypothesisOutcomeSpec(
        schema_version=2,
        outcome_id="cost-aware-edge-validation",
        version="2",
        hypothesis_ref=hypothesis_ref,
        question_ref=question_ref,
        outcome="failed",
        rationale="The apparent edge vanished under the frozen cost model.",
        actor_id="validation-reviewer",
        recorded_at="2026-01-04T00:00:00+00:00",
        evidence_hashes=(_hash("d"),),
        failure_classification=(HypothesisFailureClassification.ELIMINATED_AFTER_COSTS),
    )


def test_failure_taxonomy_is_exact_and_v1_hashes_remain_stable() -> None:
    assert len(HypothesisFailureClassification) == 16
    assert {item.value for item in HypothesisFailureClassification} == {
        "PHENOMENON_ABSENT",
        "ELIMINATED_AFTER_COSTS",
        "DATA_ERROR",
        "POINT_IN_TIME_ERROR",
        "FUTURE_INFORMATION_LEAKAGE",
        "SURVIVORSHIP_BIAS",
        "OVERFITTING",
        "INSUFFICIENT_SAMPLE",
        "ROLL_POLICY_DEPENDENCE",
        "TERM_STRUCTURE_DEPENDENCE",
        "OPTION_LIQUIDITY_INSUFFICIENT",
        "MIDPOINT_ILLUSION",
        "SURFACE_MODEL_DEPENDENCE",
        "EARLY_EXERCISE_RISK",
        "TAIL_EVENT_CONCENTRATION",
        "MULTI_LEG_EXECUTION_INFEASIBLE",
    }
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
    assert literature_spec_from_dict(legacy_literature.as_dict()) == legacy_literature
    assert hypothesis_outcome_spec_from_dict(legacy_outcome.as_dict()) == legacy_outcome


def test_schema_v2_outcome_requires_a_controlled_failure_classification() -> None:
    hypothesis_ref = KnowledgeRef("hypothesis", "hyp", "1", _hash("a"))
    with pytest.raises(KnowledgeContractError, match="failure_classification_required"):
        HypothesisOutcomeSpec(
            schema_version=2,
            outcome_id="missing-classification",
            version="2",
            hypothesis_ref=hypothesis_ref,
            outcome="failed",
            rationale="Failed.",
            actor_id="actor",
            recorded_at="2026-01-02T00:00:00+00:00",
            evidence_hashes=(_hash("b"),),
        )

    valid = HypothesisOutcomeSpec(
        schema_version=2,
        outcome_id="classified",
        version="2",
        hypothesis_ref=hypothesis_ref,
        outcome="failed",
        rationale="Future information contaminated the feature.",
        actor_id="actor",
        recorded_at="2026-01-02T00:00:00+00:00",
        evidence_hashes=(_hash("b"),),
        failure_classification=(
            HypothesisFailureClassification.FUTURE_INFORMATION_LEAKAGE
        ),
    )
    assert hypothesis_outcome_spec_from_dict(valid.as_dict()) == valid
    unknown = valid.as_dict()
    unknown["failure_classification"] = "UNCONTROLLED_REASON"
    with pytest.raises(KnowledgeContractError, match="unknown"):
        hypothesis_outcome_spec_from_dict(unknown)
    missing = valid.as_dict()
    missing.pop("failure_classification")
    with pytest.raises(KnowledgeContractError, match="fields_invalid"):
        hypothesis_outcome_spec_from_dict(missing)


def test_literature_v2_is_strict_versioned_and_temporally_ordered() -> None:
    hypothesis_ref = KnowledgeRef("hypothesis", "hyp", "1", _hash("a"))
    literature = _literature(hypothesis_ref)
    assert literature_spec_from_dict(literature.as_dict()) == literature

    with pytest.raises(KnowledgeContractError, match="date_order_invalid"):
        replace(
            literature,
            published_at="2026-01-03T00:00:00+00:00",
            accessed_at="2026-01-02T00:00:00+00:00",
        )
    with pytest.raises(KnowledgeContractError, match="evidence_required"):
        replace(literature, reproduction_evidence_hashes=())

    unknown = literature.as_dict()
    unknown["unreviewed_extension"] = True
    with pytest.raises(KnowledgeContractError, match="fields_invalid"):
        literature_spec_from_dict(unknown)
    missing = literature.as_dict()
    missing.pop("key_claims")
    with pytest.raises(KnowledgeContractError, match="fields_invalid"):
        literature_spec_from_dict(missing)


def test_registry_publishes_resolves_and_validates_v2_knowledge(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    hypothesis_ref, question_ref = _lineage(manager)
    literature = _literature(hypothesis_ref)
    outcome = _outcome(hypothesis_ref, question_ref)

    literature_row = publish_literature(manager=manager, literature=literature)
    outcome_row = publish_hypothesis_outcome(manager=manager, outcome=outcome)

    assert validate_knowledge_registry(manager)["status"] == "PASS"
    assert (
        get_knowledge_record(
            manager=manager,
            record_type="literature",
            logical_id=literature.literature_id,
            version=literature.version,
        )
        == literature_row
    )
    assert (
        get_knowledge_record(
            manager=manager,
            record_type="hypothesis_outcome",
            logical_id=outcome.outcome_id,
            version=outcome.version,
        )
        == outcome_row
    )
    outbound = query_outbound_refs(
        manager=manager,
        record_type="literature",
        logical_id=literature.literature_id,
        version=literature.version,
    )
    assert [item["logical_id"] for item in outbound] == [hypothesis_ref.logical_id]


@pytest.mark.parametrize("tamper", ["unknown_field", "date_inversion"])
def test_self_consistent_v2_literature_tamper_is_rejected(
    tmp_path: Path, tamper: str
) -> None:
    manager = _manager(tmp_path)
    hypothesis_ref, _question_ref = _lineage(manager)
    publish_literature(manager=manager, literature=_literature(hypothesis_ref))
    path = knowledge_registry_path(manager)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    row = rows[-1]
    if tamper == "unknown_field":
        row["payload"]["unreviewed_extension"] = True
    else:
        row["payload"]["published_at"] = "2026-01-03T00:00:00+00:00"
        row["payload"]["accessed_at"] = "2026-01-02T00:00:00+00:00"
    row["record_hash"] = sha256_prefixed(row["payload"])
    material = {key: value for key, value in row.items() if key != "row_hash"}
    row["row_hash"] = sha256_prefixed(
        content_hash_payload(material),
        label=f"{KNOWLEDGE_REGISTRY_HASH_LABEL}_row",
    )
    path.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows),
        encoding="utf-8",
    )

    validation = validate_knowledge_registry(manager)
    assert validation["status"] == "FAIL"
    assert any(
        "literature" in reason or "fields_invalid" in reason
        for reason in validation["reasons"]
    )


def test_self_contained_prefix_proof_round_trips_and_is_append_stable(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    hypothesis_ref, question_ref = _lineage(manager)
    literature = _literature(hypothesis_ref)
    publish_literature(manager=manager, literature=literature)

    proof = export_knowledge_registry_proof(
        manager=manager, target_ref=literature.ref()
    )
    serialized = proof.as_dict()
    assert verify_knowledge_registry_proof(serialized) == proof
    assert KnowledgeRegistryProof.from_dict(serialized) == proof
    assert (
        verify_knowledge_registry_external_evidence(proof.as_external_evidence())
        == proof
    )

    publish_hypothesis_outcome(
        manager=manager,
        outcome=_outcome(hypothesis_ref, question_ref),
    )
    assert (
        export_knowledge_registry_proof(manager=manager, target_ref=literature.ref())
        == proof
    )

    tampered = deepcopy(serialized)
    tampered["rows"][0]["actor_id"] = "forged-actor"
    with pytest.raises(KnowledgeRegistryError, match="row_hash_mismatch"):
        verify_knowledge_registry_proof(tampered)
    unknown = deepcopy(serialized)
    unknown["unknown"] = True
    with pytest.raises(KnowledgeRegistryError, match="fields_invalid"):
        verify_knowledge_registry_proof(unknown)
