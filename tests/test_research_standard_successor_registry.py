from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from market_research.research.knowledge_registry import (
    KnowledgeRegistryError,
    publish_manifest_lineage,
)
from market_research.research.hypothesis_contract import parse_hypothesis_spec
from market_research.research.research_standard import HypothesisRelation
from tests.hypothesis_lineage_fixture import hypothesis_spec_v2
from tests.test_research_standard_authority_integration import (
    _binding,
    _manager,
    _manifest_stub,
    _successor_manifest,
)


def test_registry_rejects_original_relation_for_second_version(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    original = _manifest_stub(tmp_path)
    successor, binding = _successor_manifest(original)
    invalid_binding = replace(
        binding,
        hypothesis_version=replace(
            binding.hypothesis_version,
            relation=HypothesisRelation.ORIGINAL,
            parent_version_hashes=(),
        ),
    )
    publish_manifest_lineage(
        manager=manager,
        hypothesis=original.hypothesis_spec,
        research_standard_binding=original.research_standard_binding,
    )

    with pytest.raises(KnowledgeRegistryError, match="hypothesis_successor_"):
        publish_manifest_lineage(
            manager=manager,
            hypothesis=successor.hypothesis_spec,
            research_standard_binding=invalid_binding,
        )


def test_registry_rejects_skipped_same_id_version(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    original = _manifest_stub(tmp_path)
    successor, binding = _successor_manifest(original)
    version_three_spec = replace(successor.hypothesis_spec, version="3.0.0")
    skipped_binding = replace(
        binding,
        hypothesis_version=replace(binding.hypothesis_version, version=3),
        legacy_hypothesis_contract_hash=version_three_spec.contract_hash(),
    )
    publish_manifest_lineage(
        manager=manager,
        hypothesis=original.hypothesis_spec,
        research_standard_binding=original.research_standard_binding,
    )

    with pytest.raises(
        KnowledgeRegistryError,
        match="hypothesis_successor_version_not_monotonic",
    ):
        publish_manifest_lineage(
            manager=manager,
            hypothesis=version_three_spec,
            research_standard_binding=skipped_binding,
        )


def test_registry_requires_immediately_prior_same_id_parent(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    original = _manifest_stub(tmp_path)
    successor, binding = _successor_manifest(original)
    publish_manifest_lineage(
        manager=manager,
        hypothesis=original.hypothesis_spec,
        research_standard_binding=original.research_standard_binding,
    )
    publish_manifest_lineage(
        manager=manager,
        hypothesis=successor.hypothesis_spec,
        research_standard_binding=binding,
    )
    version_three_spec = replace(successor.hypothesis_spec, version="3.0.0")
    missing_prior_binding = replace(
        binding,
        hypothesis_version=replace(
            binding.hypothesis_version,
            version=3,
            parent_version_hashes=(
                original.research_standard_binding.hypothesis_version.content_hash,
            ),
        ),
        legacy_hypothesis_contract_hash=version_three_spec.contract_hash(),
    )

    with pytest.raises(
        KnowledgeRegistryError,
        match="hypothesis_successor_parent_hash_missing",
    ):
        publish_manifest_lineage(
            manager=manager,
            hypothesis=version_three_spec,
            research_standard_binding=missing_prior_binding,
        )


def test_registry_preserves_cross_id_derived_parent_semantics(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    original_id = "sma-uptrend-edge"
    derived_id = "sma-derived-edge"
    claim = "SMA crossovers have positive conditional expectancy after costs."
    competitors = [
        {
            "hypothesis_id": derived_id,
            "version": "1.0.0",
            "hypothesis_text": claim,
        },
        {
            "hypothesis_id": original_id,
            "version": "1.0.0",
            "hypothesis_text": claim,
        },
        {
            "hypothesis_id": "sma-uptrend-null",
            "version": "1.0.0",
            "hypothesis_text": "SMA crossovers have no net edge.",
        },
    ]
    original_spec = parse_hypothesis_spec(
        hypothesis_spec_v2(competing_hypotheses=competitors)
    )
    derived_spec = parse_hypothesis_spec(
        hypothesis_spec_v2(
            hypothesis_id=derived_id,
            competing_hypotheses=competitors,
        )
    )
    base_binding = _binding(original_spec)
    shared_observations = tuple(
        replace(
            observation,
            linked_hypothesis_ids=(original_id, derived_id),
        )
        for observation in base_binding.observations
    )
    shared_question = replace(
        base_binding.research_question,
        observation_hashes=tuple(
            observation.content_hash for observation in shared_observations
        ),
    )
    original_hypothesis = replace(
        base_binding.hypothesis_version,
        research_question_hash=shared_question.content_hash,
    )
    original_binding = replace(
        base_binding,
        observations=shared_observations,
        research_question=shared_question,
        hypothesis_version=original_hypothesis,
    )
    derived_binding = replace(
        original_binding,
        hypothesis_version=replace(
            original_hypothesis,
            hypothesis_id=derived_id,
            relation=HypothesisRelation.DERIVED,
            parent_version_hashes=(original_hypothesis.content_hash,),
        ),
        legacy_hypothesis_contract_hash=derived_spec.contract_hash(),
    )
    original_publication = publish_manifest_lineage(
        manager=manager,
        hypothesis=original_spec,
        research_standard_binding=original_binding,
    )

    derived_publication = publish_manifest_lineage(
        manager=manager,
        hypothesis=derived_spec,
        research_standard_binding=derived_binding,
    )

    original_row = original_publication["research_standard_lineage"][
        "hypothesis_version"
    ]
    assert {
        "record_type": original_row["record_type"],
        "logical_id": original_row["logical_id"],
        "version": original_row["version"],
        "record_hash": original_row["record_hash"],
    } in derived_publication["research_standard_lineage"]["hypothesis_version"][
        "outbound_refs"
    ]


def test_public_lineage_publication_rejects_contradictory_compatibility_graph(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest_stub(tmp_path)
    contradictory_spec = replace(
        manifest.hypothesis_spec,
        hypothesis_text="A contradictory compatibility-only claim.",
    )
    contradictory_binding = replace(
        manifest.research_standard_binding,
        legacy_hypothesis_contract_hash=contradictory_spec.contract_hash(),
    )

    with pytest.raises(
        KnowledgeRegistryError,
        match="research_standard_legacy_hypothesis_identity_mismatch",
    ):
        publish_manifest_lineage(
            manager=manager,
            hypothesis=contradictory_spec,
            research_standard_binding=contradictory_binding,
        )
