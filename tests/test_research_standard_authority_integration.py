from __future__ import annotations

import copy
import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.datasets.contracts import DatasetArtifactRef
from market_research.research.experiment_manifest import ManifestValidationError
from market_research.research.governance import (
    governance_registry_path,
    load_governance_rows,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.hypothesis_contract import (
    HypothesisSpec,
    parse_hypothesis_spec,
)
from market_research.research.knowledge_registry import (
    KnowledgeRegistryError,
    freeze_validation_admission,
    publish_manifest_lineage,
    require_validation_admission,
    validate_knowledge_registry,
)
from market_research.research.research_standard import (
    CompetingHypothesis,
    ExpectedDirection,
    HypothesisRelation,
    HypothesisVersion,
    InstrumentKind,
    Mechanism,
    NullHypothesis,
    Observation,
    ResearchQuestion,
    ResearchStandardBinding,
    ResearchStandardError,
    ResearchStatus,
    parse_research_standard_binding,
)
from market_research.research.strategy_package import (
    StrategyPackageError,
    _research_standard_package_contract,
)
from market_research.research.study_lifecycle import admit_study_validation
from market_research.research.validation_pipeline import (
    _validated_research_standard_binding_reasons,
    run_research_validation,
    validate_validated_research_result,
)
from market_research.research_composition import (
    builtin_strategy_registry,
    parse_builtin_manifest,
)
from market_research.settings import ResearchSettings
from tests.hypothesis_lineage_fixture import hypothesis_spec_v2
from tests.test_hypothesis_contract import _structured_manifest_payload
from tests.test_validation_admission_integration import _install_fast_validation
from tests.data_governance_fixture import (
    attach_immutable_dataset_artifact,
    seed_confirmatory_data_governance,
)


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


def _binding(spec: HypothesisSpec) -> ResearchStandardBinding:
    assert spec.hypothesis_text is not None
    assert spec.actor_id is not None
    assert spec.created_at is not None
    assert spec.research_question is not None
    observations = tuple(
        Observation(
            observation_id=item.observation_id,
            version=int(item.version.split(".", maxsplit=1)[0]),
            observed_at=item.observed_at,
            recorded_at=item.recorded_at,
            target_ids=(item.market,),
            dataset_snapshot_hashes=(_hash("1"),),
            available_information_hash=_hash("2"),
            statement=item.statement,
            researcher_interpretation="A causal mechanism remains to be tested.",
            uncertainty="Sampling and regime dependence remain possible.",
            attachment_hashes=tuple(item.evidence_hashes),
            linked_question_ids=(spec.research_question.question_id,),
            linked_hypothesis_ids=(spec.hypothesis_id,),
            created_by=item.actor_id,
        )
        for item in spec.observations
    )
    question = ResearchQuestion(
        research_question_id=spec.research_question.question_id,
        version=int(spec.research_question.version.split(".", maxsplit=1)[0]),
        title=spec.research_question.question_text,
        description="Evaluate the claim using only information available at decision time.",
        target_market=spec.observations[0].market,
        target_instrument_types=(InstrumentKind.SPOT,),
        research_horizon="next_closed_candle",
        research_scope="immutable spot candle research dataset",
        created_by=spec.research_question.actor_id,
        created_at=spec.research_question.recorded_at,
        status=ResearchStatus.STRUCTURED,
        observation_hashes=tuple(item.content_hash for item in observations),
    )
    mechanism = Mechanism(
        mechanism_id="mechanism-sma-delayed-adjustment",
        version=1,
        causal_chain=(
            spec.mechanism,
            "information arrives gradually",
            "trend persists after crossover",
            "conditional return remains after costs",
        ),
        assumptions=("closed candles are causally available",),
        observable_implications=("net conditional return is positive",),
    )
    hypothesis = HypothesisVersion(
        hypothesis_id=spec.hypothesis_id,
        version=int(spec.version.split(".", maxsplit=1)[0]),
        relation=HypothesisRelation.ORIGINAL,
        parent_version_hashes=(),
        research_question_hash=question.content_hash,
        claim=spec.hypothesis_text,
        expected_direction=ExpectedDirection.POSITIVE,
        target_ids=(spec.observations[0].market,),
        conditions=tuple(spec.observation_conditions),
        outcome_variables=("net_conditional_return",),
        prediction_horizon="next_closed_candle",
        mechanism=mechanism,
        null_hypothesis=NullHypothesis(
            null_hypothesis_id="null-sma-no-net-edge",
            statement=(
                "The conditional net return relative to "
                f"{spec.comparison_target.replace('_', ' ')} "
                "is not positive."
            ),
            rejection_metric="validation_net_return",
            rejection_threshold="strictly_positive_after_costs",
        ),
        competing_hypotheses=(
            CompetingHypothesis(
                competing_hypothesis_id="competing-sma-regime-selection",
                statement="The observed relation is caused by regime selection.",
                differentiating_predictions=(
                    "the effect disappears across prespecified regimes",
                ),
            ),
        ),
        confounders=("market regime", "spread and slippage"),
        falsification_conditions=tuple(spec.falsification_criteria),
        required_dataset_kinds=("immutable_spot_candles",),
        created_by=spec.actor_id,
        created_at=spec.created_at,
        preregistration_hash=spec.registration_evidence_hash,
    )
    return ResearchStandardBinding(
        observations=observations,
        research_question=question,
        mechanism=mechanism,
        hypothesis_version=hypothesis,
        legacy_hypothesis_contract_hash=spec.contract_hash(),
        preregistration_evidence_hash=spec.registration_evidence_hash,
    )


def _manifest_payload_with_binding() -> tuple[
    dict[str, object], ResearchStandardBinding
]:
    payload = _structured_manifest_payload()
    spec = parse_hypothesis_spec(payload["hypothesis_spec"])
    binding = _binding(spec)
    payload["hypothesis"] = spec.hypothesis_text
    payload["research_standard_binding"] = binding.as_dict()
    return payload, binding


def test_manifest_parses_exact_standard_authority_and_legacy_stays_compatible() -> None:
    legacy = parse_builtin_manifest(_structured_manifest_payload())
    assert "research_standard_binding" not in legacy.canonical_payload()

    payload, binding = _manifest_payload_with_binding()
    manifest = parse_builtin_manifest(payload)

    assert manifest.research_standard_binding == binding
    assert (
        manifest.canonical_payload()["research_standard_binding"] == binding.as_dict()
    )
    assert manifest.manifest_hash() != legacy.manifest_hash()


def test_semantic_bridge_normalizes_legitimate_comparison_target_spelling() -> None:
    payload = _structured_manifest_payload()
    legacy_payload = payload["hypothesis_spec"]
    assert isinstance(legacy_payload, dict)
    legacy_payload["comparison_target"] = "buy_and_hold"
    spec = parse_hypothesis_spec(legacy_payload)
    binding = _binding(spec)
    payload["hypothesis"] = spec.hypothesis_text
    payload["research_standard_binding"] = binding.as_dict()

    manifest = parse_builtin_manifest(payload)

    assert manifest.research_standard_binding == binding


def test_manifest_rejects_unknown_fields_hash_drift_and_bridge_mismatch() -> None:
    payload, _binding_value = _manifest_payload_with_binding()
    unknown = copy.deepcopy(payload)
    unknown["research_standard_binding"]["mechanism"]["legacy_name"] = "forbidden"
    with pytest.raises(ManifestValidationError, match="unknown_fields:legacy_name"):
        parse_builtin_manifest(unknown)

    drifted = copy.deepcopy(payload)
    drifted["research_standard_binding"]["observations"][0]["statement"] = "changed"
    with pytest.raises(ManifestValidationError, match="content_hash_mismatch"):
        parse_builtin_manifest(drifted)

    bridge = copy.deepcopy(payload)
    bridge["hypothesis_spec"]["mechanism"] = "a different compatibility mechanism"
    with pytest.raises(
        ManifestValidationError,
        match="legacy_hypothesis_contract_hash_mismatch",
    ):
        parse_builtin_manifest(bridge)


@pytest.mark.parametrize(
    ("legacy_field", "replacement", "expected_error"),
    (
        (
            "mechanism",
            "A contradictory instantaneous-reaction mechanism.",
            "legacy_mechanism_semantic_mismatch",
        ),
        (
            "observation_conditions",
            ["downtrend", "insufficient candle coverage"],
            "legacy_observation_conditions_semantic_mismatch",
        ),
        (
            "comparison_target",
            "buy_and_hold",
            "legacy_comparison_target_semantic_mismatch",
        ),
        (
            "falsification_criteria",
            ["losses count as confirmation"],
            "legacy_falsification_semantic_mismatch",
        ),
    ),
)
def test_manifest_rejects_semantic_bridge_contradictions_even_with_repinned_hash(
    legacy_field: str,
    replacement: object,
    expected_error: str,
) -> None:
    payload, binding = _manifest_payload_with_binding()
    legacy_payload = copy.deepcopy(payload["hypothesis_spec"])
    assert isinstance(legacy_payload, dict)
    legacy_payload[legacy_field] = replacement
    contradictory = parse_hypothesis_spec(legacy_payload)
    repinned_binding = replace(
        binding,
        legacy_hypothesis_contract_hash=contradictory.contract_hash(),
    )
    payload["hypothesis_spec"] = contradictory.as_dict()
    payload["research_standard_binding"] = repinned_binding.as_dict()

    with pytest.raises(ManifestValidationError, match=expected_error):
        parse_builtin_manifest(payload)


@dataclass(frozen=True)
class _ManifestStub:
    experiment_id: str
    hypothesis_spec: HypothesisSpec
    research_standard_binding: ResearchStandardBinding
    research_classification: str
    canonical: dict[str, Any]
    dataset: Any
    raw: dict[str, Any]
    market: str = "KRW-BTC"
    interval: str = "1m"

    def canonical_payload(self) -> dict[str, Any]:
        return self.canonical

    def manifest_hash(self) -> str:
        return sha256_prefixed(self.canonical)

    def simulation_seed_scope_hash(self) -> str:
        return sha256_prefixed({"seed_scope": self.canonical})


def _manifest_stub(tmp_path: Path) -> _ManifestStub:
    spec = parse_hypothesis_spec(hypothesis_spec_v2())
    binding = _binding(spec)
    split = {
        "train": {"start": "2025-01-01", "end": "2025-06-30"},
        "validation": {"start": "2025-07-01", "end": "2025-09-30"},
        "final_holdout": {"start": "2025-10-01", "end": "2025-12-31"},
    }
    canonical = {
        "experiment_id": "research-standard-authority-study",
        "market": "KRW-BTC",
        "interval": "1m",
        "hypothesis_spec": spec.as_dict(),
        "research_standard_binding": binding.as_dict(),
        "dataset": {
            "snapshot_id": "immutable-snapshot-1",
            **split,
        },
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
    canonical, frozen = attach_immutable_dataset_artifact(
        canonical,
        root=tmp_path,
    )
    return _ManifestStub(
        experiment_id="research-standard-authority-study",
        hypothesis_spec=spec,
        research_standard_binding=binding,
        research_classification="validated_candidate",
        canonical=canonical,
        dataset=SimpleNamespace(
            snapshot_id="immutable-snapshot-1",
            source_content_hash=None,
            source_schema_hash=None,
            options={},
            artifact_ref=DatasetArtifactRef(
                artifact_manifest_uri=str(frozen["artifact_manifest_uri"]),
                artifact_manifest_hash=str(frozen["artifact_manifest_hash"]),
            ),
            split=SimpleNamespace(
                train=SimpleNamespace(**split["train"]),
                validation=SimpleNamespace(**split["validation"]),
                final_holdout=SimpleNamespace(**split["final_holdout"]),
                as_dict=lambda: split,
            ),
        ),
        raw={},
    )


def _successor_manifest(
    original: _ManifestStub,
) -> tuple[_ManifestStub, ResearchStandardBinding]:
    successor_raw = hypothesis_spec_v2(version="2.0.0")
    successor_raw["created_at"] = "2025-12-05T00:00:00+00:00"
    successor_question = successor_raw["research_question"]
    assert isinstance(successor_question, dict)
    successor_question["version"] = "2.0.0"
    successor_raw["research_question_ref"] = {
        "question_id": successor_question["question_id"],
        "version": successor_question["version"],
        "question_hash": sha256_prefixed(successor_question),
    }
    successor_spec = parse_hypothesis_spec(successor_raw)
    candidate_binding = _binding(successor_spec)
    successor_hypothesis = replace(
        candidate_binding.hypothesis_version,
        relation=HypothesisRelation.REVISED_AFTER_FALSIFICATION,
        parent_version_hashes=(
            original.research_standard_binding.hypothesis_version.content_hash,
        ),
    )
    successor_binding = replace(
        candidate_binding,
        hypothesis_version=successor_hypothesis,
    )
    canonical = copy.deepcopy(original.canonical)
    canonical.update(
        {
            "experiment_id": "research-standard-authority-study-v2",
            "hypothesis_spec": successor_spec.as_dict(),
            "research_standard_binding": successor_binding.as_dict(),
        }
    )
    return (
        replace(
            original,
            experiment_id="research-standard-authority-study-v2",
            hypothesis_spec=successor_spec,
            research_standard_binding=successor_binding,
            canonical=canonical,
        ),
        successor_binding,
    )


def test_admission_lifecycle_and_package_bind_standard_registry_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest_stub(tmp_path)
    monkeypatch.setattr(
        "market_research.research.knowledge_registry.require_point_in_time_scope",
        lambda *_args, **_kwargs: None,
    )
    seed_confirmatory_data_governance(manager=manager, manifest=manifest)

    admission = freeze_validation_admission(
        manager=manager,
        manifest=manifest,
        admitted_at="2026-01-01T00:00:00+00:00",
    )

    assert validate_knowledge_registry(manager)["status"] == "PASS"
    assert admission["component_hashes"]["research_standard_binding"] == (
        sha256_prefixed(manifest.research_standard_binding.as_dict())
    )
    assert admission["research_standard_lineage"]["binding_hash"] == (
        manifest.research_standard_binding.content_hash
    )
    assert any(
        ref["record_type"] == "research_standard_binding"
        for ref in admission["admission"]["outbound_refs"]
    )
    assert (
        require_validation_admission(
            manager=manager,
            manifest=manifest,
            expected_row_hash=admission["admission_row_hash"],
        )
        == admission["admission"]
    )

    publication = admit_study_validation(
        manager=manager,
        manifest=manifest,
        validation_admission=admission,
        run_id="RUN-standard-001",
    )
    assert publication.state == "VALIDATING"
    lifecycle_rows = load_governance_rows(governance_registry_path(manager))
    assert any(
        "research_standard_binding_hash" in row.get("evidence_hashes", {})
        for row in lifecycle_rows
    )

    report = {
        "research_standard_binding_schema_version": 2,
        "research_standard_binding": manifest.research_standard_binding.as_dict(),
        "research_standard_binding_hash": (
            manifest.research_standard_binding.content_hash
        ),
        "research_standard_lineage": admission["research_standard_lineage"],
        "hypothesis_contract_hash": manifest.hypothesis_spec.contract_hash(),
    }
    package_contract = _research_standard_package_contract(report)
    assert package_contract["research_standard_binding_hash"] == (
        manifest.research_standard_binding.content_hash
    )
    assert (
        package_contract["research_standard_lineage"]
        == admission["research_standard_lineage"]
    )

    tampered = copy.deepcopy(report)
    tampered["research_standard_lineage"]["binding_hash"] = _hash("f")
    with pytest.raises(
        StrategyPackageError,
        match="research_standard_binding_mismatch",
    ):
        _research_standard_package_contract(tampered)


@pytest.mark.parametrize(
    "retained_evidence",
    [
        {
            "validation_admission": {
                "payload": {
                    "component_hashes": {
                        "research_standard_binding": _hash("a"),
                    }
                },
                "outbound_refs": [],
            }
        },
        {
            "reproduction_binding": {
                "research_standard_binding_hash": _hash("b"),
            }
        },
        {"research_standard_lineage": {"binding_hash": _hash("c")}},
    ],
    ids=("admission", "reproduction", "lineage"),
)
def test_standard_binding_cannot_be_stripped_into_a_legacy_result(
    retained_evidence: dict[str, Any],
) -> None:
    reasons = _validated_research_standard_binding_reasons(retained_evidence)

    assert reasons == ["validated_research_result_research_standard_binding_stripped"]
    with pytest.raises(
        StrategyPackageError,
        match="strategy_package_research_standard_binding_stripped",
    ):
        _research_standard_package_contract(retained_evidence)


def test_successor_parent_must_preexist_and_is_persisted_as_outbound_ref(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    original = _manifest_stub(tmp_path)
    successor, successor_binding = _successor_manifest(original)

    with pytest.raises(
        KnowledgeRegistryError,
        match="research_standard_hypothesis_parent_unpublished",
    ):
        publish_manifest_lineage(
            manager=manager,
            hypothesis=successor.hypothesis_spec,
            research_standard_binding=successor_binding,
        )

    original_publication = publish_manifest_lineage(
        manager=manager,
        hypothesis=original.hypothesis_spec,
        research_standard_binding=original.research_standard_binding,
    )
    successor_publication = publish_manifest_lineage(
        manager=manager,
        hypothesis=successor.hypothesis_spec,
        research_standard_binding=successor_binding,
    )

    original_row = original_publication["research_standard_lineage"][
        "hypothesis_version"
    ]
    successor_row = successor_publication["research_standard_lineage"][
        "hypothesis_version"
    ]
    assert {
        "record_type": original_row["record_type"],
        "logical_id": original_row["logical_id"],
        "version": original_row["version"],
        "record_hash": original_row["record_hash"],
    } in successor_row["outbound_refs"]
    assert validate_knowledge_registry(manager)["status"] == "PASS"


def test_successor_admission_requires_and_rechecks_published_parent_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    original = _manifest_stub(tmp_path)
    successor, _successor_binding = _successor_manifest(original)
    monkeypatch.setattr(
        "market_research.research.knowledge_registry.require_point_in_time_scope",
        lambda *_args, **_kwargs: None,
    )
    seed_confirmatory_data_governance(manager=manager, manifest=successor)

    with pytest.raises(
        KnowledgeRegistryError,
        match="research_standard_hypothesis_parent_unpublished",
    ):
        freeze_validation_admission(
            manager=manager,
            manifest=successor,
            admitted_at="2026-01-02T00:00:00+00:00",
        )

    seed_confirmatory_data_governance(manager=manager, manifest=original)
    freeze_validation_admission(
        manager=manager,
        manifest=original,
        admitted_at="2026-01-01T00:00:00+00:00",
    )
    successor_admission = freeze_validation_admission(
        manager=manager,
        manifest=successor,
        admitted_at="2026-01-02T00:00:00+00:00",
    )

    assert (
        require_validation_admission(
            manager=manager,
            manifest=successor,
            expected_row_hash=successor_admission["admission_row_hash"],
        )
        == successor_admission["admission"]
    )


def test_binding_parser_rejects_top_level_hash_substitution() -> None:
    _payload, binding = _manifest_payload_with_binding()
    raw = binding.as_dict()
    raw["content_hash"] = _hash("f")
    with pytest.raises(
        ResearchStandardError,
        match="research_standard_binding_content_hash_mismatch",
    ):
        parse_research_standard_binding(raw)


def test_validation_artifact_exposes_and_verifies_standard_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.loads(
        Path("examples/research/sma_filter_manifest.example.json").read_text(
            encoding="utf-8"
        )
    )
    spec = parse_hypothesis_spec(payload["hypothesis_spec"])
    binding = _binding(spec)
    payload["hypothesis"] = spec.hypothesis_text
    payload["research_standard_binding"] = binding.as_dict()
    manifest = parse_builtin_manifest(payload)
    manager = _manager(tmp_path)
    order: list[str] = []
    _install_fast_validation(monkeypatch, manager, manifest, order)

    report = run_research_validation(
        manifest=manifest,
        db_path=tmp_path / "unused.sqlite",
        manager=manager,
        manifest_path=str(tmp_path / "manifest.json"),
        generated_at="2026-01-01T00:00:00+00:00",
        strategy_registry=builtin_strategy_registry(),
    )

    assert order == ["dataset-execution"]
    assert report["research_standard_binding_hash"] == binding.content_hash
    assert report["research_standard_lineage"]["object_hashes"] == (
        binding.lineage_hashes()
    )
    assert report["reproduction_binding"]["research_standard_binding_hash"] == (
        binding.content_hash
    )

    tampered = copy.deepcopy(report)
    tampered["research_standard_binding_hash"] = _hash("f")
    reasons = validate_validated_research_result(tampered, manager=manager)
    assert (
        "validated_research_result_research_standard_binding_hash_mismatch" in reasons
    )
