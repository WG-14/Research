from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.hashing import content_hash_payload, sha256_prefixed
from market_research.research.hypothesis_contract import parse_hypothesis_spec
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
    ResearchNoteSpec,
)
from market_research.research.knowledge_registry import (
    KnowledgeRegistryError,
    freeze_validation_admission,
    knowledge_registry_path,
    list_competing_hypotheses,
    list_knowledge_versions,
    publish_decision_record,
    publish_hypothesis_outcome,
    publish_literature,
    publish_manifest_lineage,
    publish_research_note,
    publish_research_question,
    query_inbound_refs,
    query_outbound_refs,
    require_validation_admission,
    validate_knowledge_registry,
    verify_decision_record,
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
            db_path=tmp_path / "input.sqlite",
            max_workers=1,
            random_seed=0,
        ),
        project_root=Path.cwd(),
    )


def _hash(char: str) -> str:
    return "sha256:" + char * 64


def _note(
    *,
    version: str = "1",
    body: str = "The result did not survive costs",
    references: tuple[KnowledgeRef, ...] = (),
    authority_refs: tuple[AuthorityRef, ...] = (),
) -> ResearchNoteSpec:
    return ResearchNoteSpec(
        schema_version=1,
        note_id="negative-cost-result",
        version=version,
        note_type="negative_result",
        title="Cost sensitivity",
        body=body,
        actor_id="researcher-a",
        recorded_at="2026-01-01T00:00:00+00:00",
        status="active",
        references=references,
        evidence_hashes=(_hash("a"),),
        authority_refs=authority_refs,
    )


def _domain_authority_refs() -> tuple[AuthorityRef, ...]:
    values = (
        ("dataset_registry", "dataset", "frozen-btc", "1", "1"),
        ("feature_registry", "feature", "sma-20", "2", "2"),
        ("experiment_registry", "experiment", "edge-study", "3", "3"),
        ("experiment_registry", "run", "edge-study-run", "4", "4"),
        ("strategy_registry", "strategy", "sma-with-filter", "5", "5"),
        ("market_regime_registry", "regime", "risk-on", "6", "6"),
        ("execution_evidence", "research_trade", "simulated-trade-7", "7", "7"),
    )
    return tuple(
        AuthorityRef(
            authority=authority,
            subject_type=subject_type,
            subject_id=subject_id,
            subject_version=subject_version,
            authority_hash=_hash(hash_char),
        )
        for authority, subject_type, subject_id, subject_version, hash_char in values
    )


def _decision(
    *,
    version: str = "1",
    supersedes: KnowledgeRef | None = None,
    proposer_ids: tuple[str, ...] = ("researcher-a",),
    approver_id: str = "reviewer-a",
) -> DecisionRecord:
    return DecisionRecord(
        schema_version=1,
        decision_id="decision-edge-validation",
        version=version,
        decision_type="hypothesis_validation_transition",
        subject=AuthorityRef(
            authority="research_governance",
            subject_type="hypothesis",
            subject_id="edge",
            subject_version="1",
            authority_hash=_hash("b"),
        ),
        chosen_action="transition:EXPLORING:VALIDATING",
        rationale="The frozen evidence is sufficient to begin validation.",
        evidence_hashes=(_hash("b"), _hash("c")),
        alternatives=(
            DecisionAlternative(
                alternative_id="remain-exploring",
                description="Keep the hypothesis in exploration.",
                rejection_reason="The preregistered validation contract is complete.",
            ),
        ),
        expected_effects=("Validation may execute against the frozen manifest.",),
        risks=(
            DecisionRisk(
                risk_id="premature-validation",
                description="Weak exploratory evidence could waste validation budget.",
                severity="medium",
                mitigation="Bind the decision to immutable preregistration evidence.",
            ),
        ),
        proposer_ids=proposer_ids,
        approver=DecisionApprover(
            approver_type="human",
            approver_id=approver_id,
            role="research_approver",
        ),
        policy_version="material-transition-policy.v1",
        decided_at="2026-01-02T00:00:00+00:00",
        supersedes=supersedes,
    )


def test_lineage_publication_is_atomic_queryable_and_reverse_queryable(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    spec = parse_hypothesis_spec(hypothesis_spec_v2())

    publication = publish_manifest_lineage(manager=manager, hypothesis=spec)
    replay = publish_manifest_lineage(manager=manager, hypothesis=spec)

    assert replay == publication
    assert validate_knowledge_registry(manager)["status"] == "PASS"
    assert validate_knowledge_registry(manager)["row_count"] == 3
    question = publication["research_question"]
    outbound = query_outbound_refs(
        manager=manager,
        record_type="research_question",
        logical_id=question["logical_id"],
        version=question["version"],
    )
    assert [row["record_type"] for row in outbound] == ["observation"]
    observation = publication["observations"][0]
    inbound = query_inbound_refs(
        manager=manager,
        target=KnowledgeRef(
            "observation",
            observation["logical_id"],
            observation["version"],
            observation["record_hash"],
        ),
    )
    assert {row["record_type"] for row in inbound} == {
        "research_question",
        "hypothesis",
    }


def test_orphan_question_rejects_without_partial_publication(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    spec = parse_hypothesis_spec(hypothesis_spec_v2())
    assert spec.research_question is not None

    with pytest.raises(KnowledgeRegistryError, match="knowledge_reference_orphan"):
        publish_research_question(manager=manager, question=spec.research_question)

    assert not knowledge_registry_path(manager).exists()


def test_version_collision_and_cas_are_fail_closed(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    authority_refs = _domain_authority_refs()
    first = publish_research_note(
        manager=manager,
        note=_note(authority_refs=authority_refs),
    )
    assert (
        publish_research_note(
            manager=manager,
            note=_note(authority_refs=authority_refs),
        )
        == first
    )

    with pytest.raises(KnowledgeRegistryError, match="version_collision"):
        publish_research_note(
            manager=manager,
            note=_note(
                body="Changed content under the same version",
                authority_refs=authority_refs,
            ),
        )
    with pytest.raises(KnowledgeRegistryError, match="version_cas_conflict"):
        publish_research_note(
            manager=manager,
            note=_note(version="2", authority_refs=authority_refs),
        )

    second = publish_research_note(
        manager=manager,
        note=_note(version="2", authority_refs=authority_refs),
        expected_previous_record_hash=first["record_hash"],
    )
    assert second["previous_record_hash"] == first["record_hash"]
    assert [
        row["version"]
        for row in list_knowledge_versions(
            manager=manager,
            record_type="research_note",
            logical_id="negative-cost-result",
        )
    ] == ["1", "2"]


def test_research_note_binds_every_supported_domain_authority_and_reverse_query(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    spec = parse_hypothesis_spec(hypothesis_spec_v2())
    lineage = publish_manifest_lineage(manager=manager, hypothesis=spec)
    literature = LiteratureSpec(
        schema_version=1,
        literature_id="paper-cost-sensitive-edges",
        version="1",
        title="Cost-sensitive research edges",
        citation="Research Journal 1 (2026)",
        actor_id="researcher-a",
        recorded_at="2026-01-01T00:00:00+00:00",
        source_content_hash=_hash("8"),
    )
    literature_row = publish_literature(manager=manager, literature=literature)
    knowledge_refs = (
        KnowledgeRef(
            "observation",
            lineage["observations"][0]["logical_id"],
            lineage["observations"][0]["version"],
            lineage["observations"][0]["record_hash"],
        ),
        KnowledgeRef(
            "hypothesis",
            lineage["hypothesis"]["logical_id"],
            lineage["hypothesis"]["version"],
            lineage["hypothesis"]["record_hash"],
        ),
        KnowledgeRef(
            "literature",
            literature_row["logical_id"],
            literature_row["version"],
            literature_row["record_hash"],
        ),
    )
    authority_refs = _domain_authority_refs()

    note = publish_research_note(
        manager=manager,
        note=_note(references=knowledge_refs, authority_refs=authority_refs),
    )

    assert note["payload"]["authority_refs"] == [
        item.as_dict() for item in authority_refs
    ]
    outbound = query_outbound_refs(
        manager=manager,
        record_type="research_note",
        logical_id=note["logical_id"],
        version=note["version"],
    )
    assert [item["record_type"] for item in outbound[:3]] == [
        "observation",
        "hypothesis",
        "literature",
    ]
    assert {item["subject_type"] for item in outbound[3:]} == {
        "dataset",
        "feature",
        "experiment",
        "run",
        "strategy",
        "regime",
        "research_trade",
    }
    assert all(item["reference_kind"] == "authority" for item in outbound[3:])
    for authority_ref in authority_refs:
        inbound = query_inbound_refs(manager=manager, target=authority_ref)
        assert [item["row_hash"] for item in inbound] == [note["row_hash"]]
    assert validate_knowledge_registry(manager)["status"] == "PASS"


def test_research_note_rejects_unknown_authority_type_and_orphan_knowledge(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    unknown = AuthorityRef(
        authority="account_system",
        subject_type="account",
        subject_id="forbidden-account",
        subject_version="1",
        authority_hash=_hash("9"),
    )
    with pytest.raises(
        KnowledgeContractError,
        match="authority_ref_subject_type_unknown",
    ):
        _note(authority_refs=(unknown,))

    orphan = KnowledgeRef("hypothesis", "missing-hypothesis", "1", _hash("a"))
    with pytest.raises(KnowledgeRegistryError, match="knowledge_reference_orphan"):
        publish_research_note(
            manager=manager,
            note=_note(
                references=(orphan,),
                authority_refs=_domain_authority_refs(),
            ),
        )
    assert not knowledge_registry_path(manager).exists()


def test_self_consistent_authority_ref_tamper_is_semantically_rejected(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    publish_research_note(
        manager=manager,
        note=_note(authority_refs=_domain_authority_refs()),
    )
    path = knowledge_registry_path(manager)
    row = json.loads(path.read_text(encoding="utf-8"))
    row["authority_refs"][0]["subject_type"] = "account"
    row["payload"]["authority_refs"][0]["subject_type"] = "account"
    row["record_hash"] = sha256_prefixed(row["payload"])
    material = {key: value for key, value in row.items() if key != "row_hash"}
    row["row_hash"] = sha256_prefixed(
        content_hash_payload(material),
        label="research_knowledge_registry_row",
    )
    path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")

    validation = validate_knowledge_registry(manager)
    assert validation["status"] == "FAIL"
    assert any(
        "authority_ref_subject_type_unknown" in reason
        for reason in validation["reasons"]
    )


def test_concurrent_exact_retry_publishes_one_row(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        rows = list(
            executor.map(
                lambda _index: publish_research_note(manager=manager, note=_note()),
                range(16),
            )
        )

    assert len({row["row_hash"] for row in rows}) == 1
    assert validate_knowledge_registry(manager)["row_count"] == 1


def test_failed_competing_hypothesis_remains_visible(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    competitors = [
        {
            "hypothesis_id": "sma-uptrend-edge",
            "version": "1.0.0",
            "hypothesis_text": (
                "SMA crossovers have positive conditional expectancy after costs."
            ),
        },
        {
            "hypothesis_id": "sma-uptrend-null",
            "version": "1.0.0",
            "hypothesis_text": (
                "SMA crossovers have no positive conditional expectancy after costs."
            ),
        },
    ]
    edge = parse_hypothesis_spec(hypothesis_spec_v2(competing_hypotheses=competitors))
    null = parse_hypothesis_spec(
        hypothesis_spec_v2(
            hypothesis_id="sma-uptrend-null",
            hypothesis_text=competitors[1]["hypothesis_text"],
            phenomenon="SMA crossovers do not have positive expectancy.",
            mechanism="Observed crossover returns are sampling noise.",
            competing_hypotheses=competitors,
        )
    )
    publish_manifest_lineage(manager=manager, hypothesis=edge)
    null_publication = publish_manifest_lineage(manager=manager, hypothesis=null)
    failed_ref = KnowledgeRef(
        "hypothesis",
        null.hypothesis_id,
        null.version,
        null.contract_hash(),
    )
    question_ref = KnowledgeRef(
        "research_question",
        null.research_question_ref.question_id,
        null.research_question_ref.version,
        null.research_question_ref.question_hash,
    )
    outcome = HypothesisOutcomeSpec(
        schema_version=1,
        outcome_id="sma-null-first-validation",
        version="1",
        hypothesis_ref=failed_ref,
        question_ref=question_ref,
        outcome="failed",
        rationale="The null sibling failed its preregistered robustness gate.",
        actor_id="researcher-a",
        recorded_at="2026-01-03T00:00:00+00:00",
        evidence_hashes=(_hash("d"),),
    )
    published_outcome = publish_hypothesis_outcome(manager=manager, outcome=outcome)

    siblings = list_competing_hypotheses(
        manager=manager,
        question_id=question_ref.logical_id,
        include_failed=True,
    )
    failed = next(
        item for item in siblings if item["hypothesis_id"] == null.hypothesis_id
    )
    assert failed["published"] is True
    assert failed["outcome"] == "failed"
    assert failed["outcome_row_hash"] == published_outcome["row_hash"]
    assert (
        null_publication["hypothesis"]["record_hash"]
        == failed["hypothesis_record_hash"]
    )
    assert all(
        item["hypothesis_id"] != null.hypothesis_id
        for item in list_competing_hypotheses(
            manager=manager,
            question_id=question_ref.logical_id,
            include_failed=False,
        )
    )


@dataclass(frozen=True)
class _ManifestStub:
    experiment_id: str
    hypothesis_spec: Any
    canonical: dict[str, Any]
    dataset: Any
    raw: dict[str, Any]

    def canonical_payload(self) -> dict[str, Any]:
        return self.canonical

    def manifest_hash(self) -> str:
        return sha256_prefixed(self.canonical)

    def simulation_seed_scope_hash(self) -> str:
        return sha256_prefixed({"seed_scope": self.canonical})


def _manifest_stub() -> _ManifestStub:
    spec = parse_hypothesis_spec(
        hypothesis_spec_v2(
            registration_status="pre_registered",
            pre_registered_at="2025-12-05T00:00:00+00:00",
            registration_evidence_hash=_hash("e"),
        )
    )
    split = {
        "train": {"start": "2025-01-01", "end": "2025-06-30"},
        "validation": {"start": "2025-07-01", "end": "2025-09-30"},
        "final_holdout": {"start": "2025-10-01", "end": "2025-12-31"},
    }
    canonical = {
        "experiment_id": "admission-exp-1",
        "hypothesis_spec": spec.as_dict(),
        "dataset": {"source": "immutable", "snapshot_id": "snapshot-1", **split},
        "parameter_space": {"threshold": [1, 2]},
        "acceptance_gate": {"min_trade_count": 10},
        "statistical_validation": {"seed_policy": "derived"},
        "stress_suite": {"seed_policy": "derived"},
        "final_selection": {"metric": "return_pct"},
        "walk_forward": None,
        "cost_model": {"fee_rate": 0.001},
        "execution_model": {"type": "fixed_bps"},
        "execution_timing": {"decision": "close", "fill": "next_open"},
        "portfolio_policy": {"starting_cash_krw": 1_000_000},
        "risk_policy": {"max_position_pct": 100},
        "research_run": {"max_workers": 1},
    }
    return _ManifestStub(
        experiment_id="admission-exp-1",
        hypothesis_spec=spec,
        canonical=canonical,
        dataset=SimpleNamespace(split=SimpleNamespace(as_dict=lambda: split)),
        raw={},
    )


def test_admission_is_atomic_retry_safe_and_preserves_external_evidence(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest_stub()

    first = freeze_validation_admission(manager=manager, manifest=manifest)
    replay = freeze_validation_admission(manager=manager, manifest=manifest)

    assert replay["admission"] == first["admission"]
    assert validate_knowledge_registry(manager)["row_count"] == 4
    payload = first["admission"]["payload"]
    assert payload["admission_status"] == "FORMAL_PREREGISTERED_EXTERNAL_EVIDENCE"
    assert payload["external_registration_evidence_hash"] == _hash("e")
    assert (
        require_validation_admission(
            manager=manager,
            manifest=manifest,
            expected_row_hash=first["admission_row_hash"],
        )
        == first["admission"]
    )


def test_decision_record_requires_complete_fields_separation_and_version_cas(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(KnowledgeContractError, match="alternatives_required"):
        replace(_decision(), alternatives=())
    with pytest.raises(KnowledgeContractError, match="approver_separation_violation"):
        _decision(proposer_ids=("reviewer-a",), approver_id="reviewer-a")

    decision = _decision()
    first = publish_decision_record(manager=manager, decision=decision)
    assert (
        verify_decision_record(
            manager=manager,
            decision_id=decision.decision_id,
            version=decision.version,
            expected_subject=decision.subject,
            expected_chosen_action=decision.chosen_action,
            required_evidence_hashes=decision.evidence_hashes,
            expected_record_hash=decision.contract_hash(),
            expected_row_hash=first["row_hash"],
        )
        == first
    )
    second_decision = _decision(version="2", supersedes=decision.ref())
    with pytest.raises(KnowledgeRegistryError, match="version_cas_conflict"):
        publish_decision_record(manager=manager, decision=second_decision)
    second = publish_decision_record(
        manager=manager,
        decision=second_decision,
        expected_previous_record_hash=decision.contract_hash(),
    )
    assert second["previous_record_hash"] == decision.contract_hash()


def test_tamper_and_truncation_are_detected(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    publish_research_note(manager=manager, note=_note())
    path = knowledge_registry_path(manager)
    original = path.read_text(encoding="utf-8")
    path.write_text(
        original.replace("Cost sensitivity", "Forged sensitivity"), encoding="utf-8"
    )
    assert validate_knowledge_registry(manager)["status"] == "FAIL"
    path.write_text(original.rstrip("\n"), encoding="utf-8")
    assert validate_knowledge_registry(manager)["status"] == "FAIL"
