from __future__ import annotations

import json
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.knowledge_contract import (
    AIAdvisoryReview,
    AIAdvisorySpec,
    AuthorityRef,
    KnowledgeContractError,
)
from market_research.research.knowledge_registry import (
    KnowledgeRegistryError,
    knowledge_registry_path,
    publish_ai_advisory,
    publish_ai_advisory_review,
    query_inbound_refs,
    validate_knowledge_registry,
)
from market_research.settings import ResearchSettings


def _hash(char: str) -> str:
    return "sha256:" + char * 64


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


def _advisory(**overrides: object) -> AIAdvisorySpec:
    values: dict[str, object] = {
        "schema_version": 1,
        "advisory_id": "ai_summary_0001",
        "version": "1",
        "task_type": "research_summary",
        "generator_id": "assistant-model-run-7",
        "provider_id": "offline-model-adapter",
        "model_id": "summarizer-v3",
        "model_configuration_hash": _hash("a"),
        "prompt_hash": _hash("b"),
        "source_refs": (),
        "source_authority_refs": (
            AuthorityRef(
                authority="experiment_registry",
                subject_type="run",
                subject_id="research-run-17",
                subject_version="1",
                authority_hash=_hash("c"),
            ),
            AuthorityRef(
                authority="dataset_registry",
                subject_type="dataset",
                subject_id="immutable-dataset-4",
                subject_version="2",
                authority_hash=_hash("d"),
            ),
        ),
        "output_text": "The candidate failed the declared cost stress.",
        "generated_at": "2026-07-17T00:00:00+00:00",
    }
    values.update(overrides)
    return AIAdvisorySpec(**values)  # type: ignore[arg-type]


def _review(advisory: AIAdvisorySpec, **overrides: object) -> AIAdvisoryReview:
    values: dict[str, object] = {
        "schema_version": 1,
        "review_id": "ai_summary_review_0001",
        "version": "1",
        "advisory_ref": advisory.ref(),
        "reviewer_id": "human-reviewer-2",
        "reviewer_role": "research_reviewer",
        "decision": "accepted_as_advisory",
        "rationale": "The summary faithfully cites the failed stress evidence.",
        "evidence_hashes": (_hash("e"),),
        "reviewed_at": "2026-07-17T01:00:00+00:00",
    }
    values.update(overrides)
    return AIAdvisoryReview(**values)  # type: ignore[arg-type]


def test_ai_advisory_is_append_only_provenance_bound_and_human_reviewed(tmp_path):
    manager = _manager(tmp_path)
    advisory = _advisory()
    advisory_row = publish_ai_advisory(manager=manager, advisory=advisory)
    review = _review(advisory)
    review_row = publish_ai_advisory_review(manager=manager, review=review)

    assert advisory_row["payload"]["review_status"] == "pending_human_review"
    assert advisory_row["payload"]["authority_scope"] == (
        "advisory_only_no_domain_mutation"
    )
    assert review_row["payload"]["authority_scope"] == "advisory_output_only"
    assert review_row["payload"]["reviewer_type"] == "human"
    assert review_row["outbound_refs"] == [advisory.ref().as_dict()]
    assert query_inbound_refs(manager=manager, target=advisory.ref()) == [review_row]
    assert validate_knowledge_registry(manager)["status"] == "PASS"


def test_ai_cannot_self_approve_or_reuse_generator_as_human_reviewer(tmp_path):
    with pytest.raises(KnowledgeContractError, match="cannot_self_approve"):
        _advisory(review_status="approved")

    manager = _manager(tmp_path)
    advisory = _advisory()
    publish_ai_advisory(manager=manager, advisory=advisory)
    with pytest.raises(KnowledgeRegistryError, match="separation_violation"):
        publish_ai_advisory_review(
            manager=manager,
            review=_review(advisory, reviewer_id=advisory.generator_id),
        )


def test_ai_advisory_requires_internal_sources_and_cannot_claim_domain_authority():
    with pytest.raises(KnowledgeContractError, match="sources_required"):
        _advisory(source_refs=(), source_authority_refs=())
    with pytest.raises(KnowledgeContractError, match="authority_scope_invalid"):
        _advisory(authority_scope="strategy_approval")


def test_ai_review_must_be_human_and_cannot_predate_generation(tmp_path):
    advisory = _advisory()
    with pytest.raises(KnowledgeContractError, match="must_be_human"):
        _review(advisory, reviewer_type="ai")

    manager = _manager(tmp_path)
    publish_ai_advisory(manager=manager, advisory=advisory)
    with pytest.raises(KnowledgeRegistryError, match="before_generation"):
        publish_ai_advisory_review(
            manager=manager,
            review=_review(advisory, reviewed_at="2026-07-16T23:59:59+00:00"),
        )


def test_tampered_ai_review_fails_registry_semantics(tmp_path):
    manager = _manager(tmp_path)
    advisory = _advisory()
    publish_ai_advisory(manager=manager, advisory=advisory)
    publish_ai_advisory_review(manager=manager, review=_review(advisory))
    path = knowledge_registry_path(manager)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[1]["payload"]["authority_scope"] = "strategy_approval"
    path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    assert validate_knowledge_registry(manager)["status"] == "FAIL"
