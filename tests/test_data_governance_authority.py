from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from market_research.paths import ResearchPathManager
from market_research.research import application as application_module
from market_research.research.application import ResearchApplicationService
from market_research.research.data_governance import (
    DATA_GOVERNANCE_BINDING_SCHEMA_VERSION,
    DataGovernanceAdmission,
    DataGovernanceError,
    DataGovernanceRef,
    DataQualityIssue,
    DataUsageBinding,
    DatasetLicensePolicy,
    DatasetSuitabilityAssessment,
    DatasetUseDecision,
    DatasetVersionRef,
    GovernanceWaiver,
    IssueResolution,
    ProviderComparison,
    data_governance_registry_path,
    dataset_version_ref_from_manifest,
    get_data_governance_record,
    publish_data_governance_record,
    publish_data_usage_binding_for_artifact,
    query_data_governance_impacts,
    require_confirmatory_data_governance,
    require_data_governance_report_binding,
    require_data_usage_binding_for_artifact,
    research_scope_ref_from_manifest,
    validate_data_governance_registry,
)
from market_research.research.hashing import content_hash_payload, sha256_prefixed
from market_research.research.datasets.contracts import DatasetArtifactRef
from market_research.research.hypothesis_contract import parse_hypothesis_spec
from market_research.research.knowledge_contract import AuthorityRef
from market_research.research.knowledge_registry import (
    KnowledgeRegistryError,
    freeze_validation_admission,
    require_validation_admission,
)
from market_research.settings import ResearchSettings
from tests.hypothesis_lineage_fixture import hypothesis_spec_v2
from tests.data_governance_fixture import attach_immutable_dataset_artifact


def _hash(char: str) -> str:
    return "sha256:" + char * 64


def _source_options() -> dict[str, object]:
    return {
        "source_provenance_hash": _hash("3"),
        "source_provider_id": "provider-a",
        "source_catalog_hash": _hash("4"),
        "source_catalog_entry_hash": _hash("f"),
        "source_license_id": "internal-research-license",
        "source_license_terms_hash": _hash("5"),
        "source_redistribution_allowed": False,
    }


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


@dataclass(frozen=True)
class _Manifest:
    experiment_id: str
    hypothesis_spec: Any
    dataset: Any
    canonical: dict[str, Any]
    market: str = "KRW-BTC"
    interval: str = "1m"
    research_classification: str = "validated_candidate"
    raw: dict[str, Any] | None = None
    research_standard_binding: Any = None

    def canonical_payload(self) -> dict[str, Any]:
        return self.canonical

    def manifest_hash(self) -> str:
        return sha256_prefixed(self.canonical)

    def simulation_seed_scope_hash(self) -> str:
        return sha256_prefixed({"seed_scope": self.canonical})


def _manifest(tmp_path: Path | None = None) -> _Manifest:
    hypothesis = parse_hypothesis_spec(
        hypothesis_spec_v2(
            registration_status="pre_registered",
            pre_registered_at="2025-12-05T00:00:00+00:00",
            registration_evidence_hash=_hash("e"),
        )
    )
    split_payload = {
        "train": {"start": "2025-01-01", "end": "2025-06-30"},
        "validation": {"start": "2025-07-01", "end": "2025-09-30"},
        "final_holdout": {"start": "2025-10-01", "end": "2025-12-31"},
    }
    split = SimpleNamespace(
        train=SimpleNamespace(**split_payload["train"]),
        validation=SimpleNamespace(**split_payload["validation"]),
        final_holdout=SimpleNamespace(**split_payload["final_holdout"]),
        as_dict=lambda: split_payload,
    )
    dataset_payload = {
        "source": "immutable_offline_fixture",
        "snapshot_id": "dataset-v1",
        "source_content_hash": _hash("1"),
        "source_schema_hash": _hash("2"),
        "options": _source_options(),
        **split_payload,
    }
    frozen: dict[str, Any] | None = None
    if tmp_path is not None:
        artifact_payload = {
            "market": "KRW-BTC",
            "interval": "1m",
            "dataset": dataset_payload,
        }
        artifact_payload, frozen = attach_immutable_dataset_artifact(
            artifact_payload,
            root=tmp_path,
        )
        dataset_payload = artifact_payload["dataset"]
    dataset = SimpleNamespace(
        source="immutable_offline_fixture",
        snapshot_id="dataset-v1",
        source_content_hash=(None if frozen is not None else _hash("1")),
        source_schema_hash=(None if frozen is not None else _hash("2")),
        options=({} if frozen is not None else _source_options()),
        artifact_ref=(
            DatasetArtifactRef(
                artifact_manifest_uri=str(frozen["artifact_manifest_uri"]),
                artifact_manifest_hash=str(frozen["artifact_manifest_hash"]),
            )
            if frozen is not None
            else None
        ),
        split=split,
    )
    canonical = {
        "experiment_id": "confirmatory-exp-1",
        "hypothesis_spec": hypothesis.as_dict(),
        "dataset": dataset_payload,
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
        "research_classification": "validated_candidate",
    }
    return _Manifest(
        experiment_id="confirmatory-exp-1",
        hypothesis_spec=hypothesis,
        dataset=dataset,
        canonical=canonical,
        raw={},
    )


def _matching_policy(
    dataset: DatasetVersionRef,
    *,
    policy_id: str,
    derivative_retention_allowed: bool = True,
    research_package_export_allowed: bool = True,
) -> DatasetLicensePolicy:
    binding = dataset.provider_licenses[0]
    return DatasetLicensePolicy(
        policy_id=policy_id,
        policy_version="1",
        provider_id=binding.provider_id,
        license_id=binding.license_id,
        source_catalog_hash=binding.source_catalog_hash,
        catalog_entry_hash=binding.catalog_entry_hash,
        terms_hash=binding.license_terms_hash,
        confirmatory_research_allowed=True,
        research_package_export_allowed=research_package_export_allowed,
        external_export_allowed=False,
        redistribution_allowed=False,
        derivative_retention_allowed=derivative_retention_allowed,
        allowed_distribution_scopes=(
            "INTERNAL_RESEARCH",
            "INTERNAL_RESEARCH_PACKAGE",
        ),
        effective_at="2025-01-01T00:00:00+00:00",
        expires_at=None,
        approved_by="data-owner",
        approved_at="2025-01-01T00:00:00+00:00",
    )


def _publish_admission(
    manager: ResearchPathManager,
    manifest: _Manifest,
    *,
    package_export_allowed: bool = True,
):
    dataset = dataset_version_ref_from_manifest(manifest)
    scope = research_scope_ref_from_manifest(manifest)
    policy = _matching_policy(
        dataset,
        policy_id="provider-a-license",
        research_package_export_allowed=package_export_allowed,
    )
    publish_data_governance_record(manager=manager, record=policy)
    comparison = ProviderComparison(
        comparison_id="dataset-v1-provider-comparison",
        comparison_version="1",
        dataset=dataset,
        candidate_provider_ids=dataset.source_priority,
        selected_provider_id=dataset.source_priority[0],
        source_priority=dataset.source_priority,
        method="timestamp aligned OHLCV comparison",
        evidence_hashes=(_hash("6"), _hash("7")),
        mismatch_rate=0.0001,
        status="SINGLE_SOURCE_ATTESTED",
        compared_by="data-reviewer",
        compared_at="2025-01-02T00:00:00+00:00",
    )
    publish_data_governance_record(manager=manager, record=comparison)
    suitability = DatasetSuitabilityAssessment(
        assessment_id="confirmatory-exp-1-dataset-suitability",
        assessment_version="1",
        dataset=dataset,
        research_scope=scope,
        license_policy_ref=policy.ref(),
        provider_comparison_ref=comparison.ref(),
        quality_report_hash=_hash("8"),
        quality_gate_status="PASS",
        point_in_time_evidence_hash=_hash("9"),
        revision_evidence_hash=_hash("a"),
        identifier_evidence_hash=_hash("b"),
        corporate_action_evidence_hash=_hash("c"),
        decision="PASS",
        limitations=(),
        assessed_by="data-analyst",
        reviewed_by="data-reviewer",
        assessed_at="2025-01-03T00:00:00+00:00",
    )
    publish_data_governance_record(manager=manager, record=suitability)
    confirmatory = DatasetUseDecision(
        decision_id="confirmatory-exp-1-dataset-use",
        decision_version="1",
        dataset=dataset,
        policy_ref=policy.ref(),
        purpose="CONFIRMATORY_RESEARCH",
        decision="ALLOW",
        distribution_scope="INTERNAL_RESEARCH",
        rationale="Approved for the exact content-bound confirmatory dataset.",
        decided_by="license-reviewer",
        decided_at="2025-01-03T01:00:00+00:00",
    )
    publish_data_governance_record(manager=manager, record=confirmatory)
    package_export = DatasetUseDecision(
        decision_id="confirmatory-exp-1-package-export",
        decision_version="1",
        dataset=dataset,
        policy_ref=policy.ref(),
        purpose="RESEARCH_PACKAGE_EXPORT",
        decision="ALLOW" if package_export_allowed else "DENY",
        distribution_scope="INTERNAL_RESEARCH_PACKAGE",
        rationale=(
            "Approved only for the internal immutable Research Package."
            if package_export_allowed
            else "Confirmatory use is allowed, but package export is denied."
        ),
        decided_by="license-reviewer",
        decided_at="2025-01-03T01:00:00+00:00",
    )
    publish_data_governance_record(manager=manager, record=package_export)
    admission = DataGovernanceAdmission(
        admission_id=manifest.experiment_id,
        admission_version=manifest.manifest_hash(),
        dataset=dataset,
        research_scope=scope,
        suitability_ref=suitability.ref(),
        confirmatory_use_decision_ref=confirmatory.ref(),
        package_export_decision_ref=package_export.ref(),
        waiver_refs=(),
        admitted_by="governance-chair",
        admitted_at="2025-01-04T00:00:00+00:00",
    )
    row = publish_data_governance_record(manager=manager, record=admission)
    return dataset, admission, row, policy


def _report_binding(
    *,
    manager: ResearchPathManager,
    dataset: DatasetVersionRef,
    row: dict[str, Any],
    validation_admission: dict[str, Any],
) -> dict[str, Any]:
    research_scope = row["payload"]["research_scope"]
    return {
        "experiment_id": research_scope["experiment_id"],
        "manifest_hash": research_scope["manifest_hash"],
        "dataset_content_hash": dataset.content_hash,
        "validation_admission_record_hash": validation_admission[
            "admission_record_hash"
        ],
        "validation_admission_row_hash": validation_admission["admission_row_hash"],
        "validation_admission": validation_admission["admission"],
        "data_governance_policy": "GOVERNED",
        "data_governance_binding_schema_version": (
            DATA_GOVERNANCE_BINDING_SCHEMA_VERSION
        ),
        "data_governance_registry_path": str(data_governance_registry_path(manager)),
        "data_governance_admission_record_hash": row["record_hash"],
        "data_governance_admission_row_hash": row["row_hash"],
        "data_governance_dataset_version_hash": dataset.version_hash,
        "data_governance_dataset_content_hash": dataset.content_hash,
        "data_governance_admission": row,
    }


def test_confirmatory_admission_is_hash_bound_and_precedes_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest(tmp_path)

    with pytest.raises(
        KnowledgeRegistryError, match="validation_data_governance_admission_failed"
    ):
        monkeypatch.setattr(
            "market_research.research.knowledge_registry.require_point_in_time_scope",
            lambda *_args, **_kwargs: None,
        )
        freeze_validation_admission(manager=manager, manifest=manifest)

    dataset, _admission, _row, _policy = _publish_admission(manager, manifest)
    frozen = freeze_validation_admission(
        manager=manager,
        manifest=manifest,
        admitted_at="2025-01-05T00:00:00+00:00",
    )

    assert frozen["data_governance"]["dataset_version_hash"] == dataset.version_hash
    assert frozen["component_hashes"]["data_governance_dataset_version"] == (
        dataset.version_hash
    )
    assert (
        require_validation_admission(manager=manager, manifest=manifest)
        == (frozen["admission"])
    )
    assert validate_data_governance_registry(manager)["status"] == "PASS"


def test_confirmatory_admission_rejects_self_declared_nonartifact_provenance(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    nonartifact_manifest = _manifest()
    diagnostic_ref = dataset_version_ref_from_manifest(nonartifact_manifest)
    _publish_admission(manager, nonartifact_manifest)

    assert diagnostic_ref.dataset_id == "dataset-v1"
    with pytest.raises(
        DataGovernanceError,
        match="data_governance_confirmatory_artifact_ref_required",
    ):
        require_confirmatory_data_governance(
            manager=manager,
            manifest=nonartifact_manifest,
        )


def test_confirmatory_allow_does_not_require_package_export_allow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest(tmp_path)
    monkeypatch.setattr(
        "market_research.research.knowledge_registry.require_point_in_time_scope",
        lambda *_args, **_kwargs: None,
    )
    dataset, _admission, row, _policy = _publish_admission(
        manager,
        manifest,
        package_export_allowed=False,
    )
    governed = require_confirmatory_data_governance(
        manager=manager,
        manifest=manifest,
    )
    frozen = freeze_validation_admission(manager=manager, manifest=manifest)
    source = _report_binding(
        manager=manager,
        dataset=dataset,
        row=row,
        validation_admission=frozen,
    )

    assert governed["admission_record_hash"] == row["record_hash"]
    assert (
        require_data_governance_report_binding(
            manager=manager,
            source=source,
            required_purpose="CONFIRMATORY_RESEARCH",
        )
        == row
    )
    with pytest.raises(
        DataGovernanceError,
        match="governance_admission_package_export_decision_not_allowed",
    ):
        require_data_governance_report_binding(
            manager=manager,
            source=source,
            required_purpose="RESEARCH_PACKAGE_EXPORT",
        )


def test_report_binding_cross_checks_research_dataset_and_validation_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest(tmp_path)
    monkeypatch.setattr(
        "market_research.research.knowledge_registry.require_point_in_time_scope",
        lambda *_args, **_kwargs: None,
    )
    dataset, _admission, row, _policy = _publish_admission(manager, manifest)
    frozen = freeze_validation_admission(manager=manager, manifest=manifest)
    source = _report_binding(
        manager=manager,
        dataset=dataset,
        row=row,
        validation_admission=frozen,
    )
    assert (
        require_data_governance_report_binding(
            manager=manager,
            source=source,
        )
        == row
    )

    wrong_experiment = deepcopy(source)
    wrong_experiment["experiment_id"] = "different-experiment"
    with pytest.raises(DataGovernanceError, match="experiment_id_mismatch"):
        require_data_governance_report_binding(
            manager=manager,
            source=wrong_experiment,
        )

    wrong_manifest = deepcopy(source)
    wrong_manifest["manifest_hash"] = _hash("0")
    with pytest.raises(DataGovernanceError, match="manifest_hash_mismatch"):
        require_data_governance_report_binding(
            manager=manager,
            source=wrong_manifest,
        )

    wrong_dataset = deepcopy(source)
    wrong_dataset["data_governance_dataset_content_hash"] = _hash("f")
    with pytest.raises(DataGovernanceError, match="dataset_content_mismatch"):
        require_data_governance_report_binding(
            manager=manager,
            source=wrong_dataset,
        )

    execution_fingerprint = deepcopy(source)
    execution_fingerprint["dataset_content_hash"] = _hash("f")
    execution_fingerprint["dataset_content_hash_semantics"] = (
        "combined_run_dataset_fingerprint"
    )
    assert (
        require_data_governance_report_binding(
            manager=manager,
            source=execution_fingerprint,
        )
        == row
    )
    wrong_execution_semantics = deepcopy(execution_fingerprint)
    wrong_execution_semantics["dataset_content_hash_semantics"] = (
        "frozen_artifact_byte_hash"
    )
    with pytest.raises(DataGovernanceError, match="hash_semantics_invalid"):
        require_data_governance_report_binding(
            manager=manager,
            source=wrong_execution_semantics,
        )

    wrong_validation_component = deepcopy(source)
    wrong_validation_component["validation_admission"]["payload"]["component_hashes"][
        "data_governance_admission_record"
    ] = _hash("f")
    validation_row = wrong_validation_component["validation_admission"]
    validation_row["record_hash"] = sha256_prefixed(validation_row["payload"])
    wrong_validation_component["validation_admission_record_hash"] = validation_row[
        "record_hash"
    ]
    validation_row_material = {
        key: value for key, value in validation_row.items() if key != "row_hash"
    }
    validation_row["row_hash"] = sha256_prefixed(
        content_hash_payload(validation_row_material),
        label="research_knowledge_registry_row",
    )
    wrong_validation_component["validation_admission_row_hash"] = validation_row[
        "row_hash"
    ]
    with pytest.raises(
        DataGovernanceError,
        match="validation_admission_component_mismatch",
    ):
        require_data_governance_report_binding(
            manager=manager,
            source=wrong_validation_component,
        )


def test_package_export_waiver_cannot_authorize_confirmatory_admission(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest(tmp_path)
    dataset, admission, _row, policy = _publish_admission(manager, manifest)
    comparison_row = get_data_governance_record(
        manager=manager,
        record_type="provider_comparison",
        logical_id="dataset-v1-provider-comparison",
        version="1",
    )
    comparison_ref = DataGovernanceRef(
        record_type="provider_comparison",
        logical_id="dataset-v1-provider-comparison",
        version="1",
        record_hash=comparison_row["record_hash"],
    )
    research_scope = replace(
        admission.research_scope,
        experiment_id="waiver-purpose-exp",
        manifest_hash=_hash("d"),
    )
    suitability = DatasetSuitabilityAssessment(
        assessment_id="waiver-purpose-suitability",
        assessment_version="1",
        dataset=dataset,
        research_scope=research_scope,
        license_policy_ref=policy.ref(),
        provider_comparison_ref=comparison_ref,
        quality_report_hash=_hash("8"),
        quality_gate_status="WARN",
        point_in_time_evidence_hash=_hash("9"),
        revision_evidence_hash=_hash("a"),
        identifier_evidence_hash=_hash("b"),
        corporate_action_evidence_hash=_hash("c"),
        decision="CONDITIONAL",
        limitations=("Internal package-only evidence limitation.",),
        assessed_by="data-analyst",
        reviewed_by="data-reviewer",
        assessed_at="2025-01-03T00:30:00+00:00",
    )
    publish_data_governance_record(manager=manager, record=suitability)
    package_only_waiver = GovernanceWaiver(
        waiver_id="waiver-purpose-package-only",
        waiver_version="1",
        target_ref=suitability.ref(),
        purpose="RESEARCH_PACKAGE_EXPORT",
        rationale="The limitation is accepted only for internal package export.",
        compensating_controls=("Retain package inside the research trust domain.",),
        evidence_hashes=(_hash("e"),),
        requested_by="data-analyst",
        approved_by="governance-chair",
        approved_at="2025-01-03T02:00:00+00:00",
        expires_at="2035-01-01T00:00:00+00:00",
    )
    publish_data_governance_record(manager=manager, record=package_only_waiver)
    wrong_purpose_admission = DataGovernanceAdmission(
        admission_id=research_scope.experiment_id,
        admission_version=research_scope.manifest_hash,
        dataset=dataset,
        research_scope=research_scope,
        suitability_ref=suitability.ref(),
        confirmatory_use_decision_ref=admission.confirmatory_use_decision_ref,
        package_export_decision_ref=admission.package_export_decision_ref,
        waiver_refs=(package_only_waiver.ref(),),
        admitted_by="governance-chair",
        admitted_at="2025-01-04T00:00:00+00:00",
    )
    with pytest.raises(DataGovernanceError, match="suitability_waiver_required"):
        publish_data_governance_record(
            manager=manager,
            record=wrong_purpose_admission,
        )


def test_governance_chronology_is_fail_closed(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest(tmp_path)
    dataset, admission, _row, policy = _publish_admission(manager, manifest)
    with pytest.raises(DataGovernanceError, match="approval_after_effective_at"):
        replace(
            policy,
            policy_id="late-approved-policy",
            approved_at="2025-01-02T00:00:00+00:00",
        )

    early_use = DatasetUseDecision(
        decision_id="early-confirmatory-use",
        decision_version="1",
        dataset=dataset,
        policy_ref=policy.ref(),
        purpose="CONFIRMATORY_RESEARCH",
        decision="ALLOW",
        distribution_scope="INTERNAL_RESEARCH",
        rationale="This decision intentionally predates the policy.",
        decided_by="license-reviewer",
        decided_at="2024-12-31T00:00:00+00:00",
    )
    with pytest.raises(DataGovernanceError, match="before_policy_approval"):
        publish_data_governance_record(manager=manager, record=early_use)

    comparison_row = get_data_governance_record(
        manager=manager,
        record_type="provider_comparison",
        logical_id="dataset-v1-provider-comparison",
        version="1",
    )
    comparison_ref = DataGovernanceRef(
        record_type="provider_comparison",
        logical_id="dataset-v1-provider-comparison",
        version="1",
        record_hash=comparison_row["record_hash"],
    )
    research_scope = replace(
        admission.research_scope,
        experiment_id="chronology-exp",
        manifest_hash=_hash("e"),
    )

    def suitability(*, assessment_id: str, assessed_at: str):
        return DatasetSuitabilityAssessment(
            assessment_id=assessment_id,
            assessment_version="1",
            dataset=dataset,
            research_scope=research_scope,
            license_policy_ref=policy.ref(),
            provider_comparison_ref=comparison_ref,
            quality_report_hash=_hash("8"),
            quality_gate_status="PASS",
            point_in_time_evidence_hash=_hash("9"),
            revision_evidence_hash=_hash("a"),
            identifier_evidence_hash=_hash("b"),
            corporate_action_evidence_hash=_hash("c"),
            decision="PASS",
            limitations=(),
            assessed_by="data-analyst",
            reviewed_by="data-reviewer",
            assessed_at=assessed_at,
        )

    with pytest.raises(DataGovernanceError, match="precedes_provider_comparison"):
        publish_data_governance_record(
            manager=manager,
            record=suitability(
                assessment_id="early-suitability",
                assessed_at="2025-01-01T12:00:00+00:00",
            ),
        )

    valid_suitability = suitability(
        assessment_id="chronology-suitability",
        assessed_at="2025-01-03T00:30:00+00:00",
    )
    publish_data_governance_record(manager=manager, record=valid_suitability)
    early_admission = DataGovernanceAdmission(
        admission_id=research_scope.experiment_id,
        admission_version=research_scope.manifest_hash,
        dataset=dataset,
        research_scope=research_scope,
        suitability_ref=valid_suitability.ref(),
        confirmatory_use_decision_ref=admission.confirmatory_use_decision_ref,
        package_export_decision_ref=admission.package_export_decision_ref,
        waiver_refs=(),
        admitted_by="governance-chair",
        admitted_at="2025-01-03T00:45:00+00:00",
    )
    with pytest.raises(DataGovernanceError, match="precedes_confirmatory_decision"):
        publish_data_governance_record(manager=manager, record=early_admission)

    future_policy = replace(
        policy,
        policy_id="future-effective-policy",
        effective_at="2999-01-01T00:00:00+00:00",
    )
    publish_data_governance_record(manager=manager, record=future_policy)
    future_decision = DatasetUseDecision(
        decision_id="future-dated-confirmatory-use",
        decision_version="1",
        dataset=dataset,
        policy_ref=future_policy.ref(),
        purpose="CONFIRMATORY_RESEARCH",
        decision="ALLOW",
        distribution_scope="INTERNAL_RESEARCH",
        rationale="A future authority must not become usable today.",
        decided_by="license-reviewer",
        decided_at="2999-01-02T00:00:00+00:00",
    )
    with pytest.raises(DataGovernanceError, match="record_time_in_future"):
        publish_data_governance_record(manager=manager, record=future_decision)


def test_legacy_validation_admission_identity_requires_new_version(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    candidate_manifest = _manifest(tmp_path)
    legacy_manifest = replace(
        candidate_manifest,
        research_classification="exploratory",
        canonical={
            **candidate_manifest.canonical,
            "research_classification": "exploratory",
        },
    )
    legacy = freeze_validation_admission(
        manager=manager,
        manifest=legacy_manifest,
        admitted_at="2025-01-05T00:00:00+00:00",
    )
    assert not any(
        key.startswith("data_governance_") for key in legacy["component_hashes"]
    )
    _publish_admission(manager, legacy_manifest)

    with pytest.raises(
        KnowledgeRegistryError,
        match=(
            "legacy_non_governed_v1_immutable:"
            "issue_new_manifest_version_or_experiment_id"
        ),
    ):
        freeze_validation_admission(
            manager=manager,
            manifest=legacy_manifest,
            admitted_at="2025-01-05T00:00:00+00:00",
            bind_data_governance=True,
        )
    with pytest.raises(
        KnowledgeRegistryError,
        match="legacy_non_governed_v1_immutable",
    ):
        require_validation_admission(
            manager=manager,
            manifest=legacy_manifest,
            bind_data_governance=True,
        )


def test_strategy_package_publication_records_real_usage_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest(tmp_path)
    monkeypatch.setattr(
        "market_research.research.knowledge_registry.require_point_in_time_scope",
        lambda *_args, **_kwargs: None,
    )
    dataset, _admission, row, _policy = _publish_admission(manager, manifest)
    frozen = freeze_validation_admission(manager=manager, manifest=manifest)
    report = _report_binding(
        manager=manager,
        dataset=dataset,
        row=row,
        validation_admission=frozen,
    )
    package_hash = _hash("f")
    monkeypatch.setattr(
        application_module,
        "build_strategy_research_package",
        lambda *_args, **_kwargs: {
            "authoritative": True,
            "package_authority_result": "PASS",
            "content_hash": package_hash,
        },
    )
    service = ResearchApplicationService(manager, strategy_registry=object())
    service.export_strategy_package(
        report=report,
        approval={
            "reviewer_id": "package-reviewer",
            "approved_at": "2025-01-08T00:00:00+00:00",
        },
        out_path=tmp_path / "published-strategy-package.json",
    )

    impacts = query_data_governance_impacts(
        manager=manager,
        dataset_version_hash=dataset.version_hash,
    )
    assert any(
        item["authority"] == "strategy_package_export"
        and item["subject_type"] == "research_package"
        and item["subject_id"] == manifest.experiment_id
        and item["subject_version"] == package_hash
        and item["authority_hash"] == package_hash
        for item in impacts["affected_authority_refs"]
    )


def test_data_usage_binding_retry_keeps_first_timestamp_and_validates_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest(tmp_path)
    monkeypatch.setattr(
        "market_research.research.knowledge_registry.require_point_in_time_scope",
        lambda *_args, **_kwargs: None,
    )
    dataset, _admission, row, _policy = _publish_admission(manager, manifest)
    frozen = freeze_validation_admission(manager=manager, manifest=manifest)
    source = _report_binding(
        manager=manager,
        dataset=dataset,
        row=row,
        validation_admission=frozen,
    )
    affected_ref = AuthorityRef(
        authority="validated_research_result",
        subject_type="research_report",
        subject_id=manifest.experiment_id,
        subject_version=manifest.manifest_hash(),
        authority_hash=_hash("f"),
    )

    class _Clock(datetime):
        current = datetime(2025, 1, 8, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz: timezone | None = None) -> datetime:
            value = cls.current
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr("market_research.research.data_governance.datetime", _Clock)
    with pytest.raises(DataGovernanceError, match="data_usage_binding_not_found"):
        require_data_usage_binding_for_artifact(
            manager=manager,
            source=source,
            affected_authority_refs=(affected_ref,),
        )
    first = publish_data_usage_binding_for_artifact(
        manager=manager,
        source=source,
        affected_authority_refs=(affected_ref,),
        recorded_by="validation-pipeline",
    )
    _Clock.current = datetime(2025, 1, 9, tzinfo=timezone.utc)
    retry = publish_data_usage_binding_for_artifact(
        manager=manager,
        source=source,
        affected_authority_refs=(affected_ref,),
        recorded_by="validation-pipeline",
    )

    assert retry == first
    assert (
        require_data_usage_binding_for_artifact(
            manager=manager,
            source=source,
            affected_authority_refs=(affected_ref,),
        )
        == first
    )
    assert retry["payload"]["recorded_at"] == "2025-01-08T00:00:00+00:00"
    assert validate_data_governance_registry(manager)["row_count"] == 7

    wrong_ref = AuthorityRef(
        authority=affected_ref.authority,
        subject_type=affected_ref.subject_type,
        subject_id=affected_ref.subject_id,
        subject_version=affected_ref.subject_version,
        authority_hash=_hash("e"),
    )
    extra_ref = AuthorityRef(
        authority="strategy_package_export",
        subject_type="research_package",
        subject_id=manifest.experiment_id,
        subject_version=_hash("d"),
        authority_hash=_hash("d"),
    )
    for refs in ((wrong_ref,), (affected_ref, extra_ref)):
        with pytest.raises(DataGovernanceError, match="data_usage_binding_not_found"):
            require_data_usage_binding_for_artifact(
                manager=manager,
                source=source,
                affected_authority_refs=refs,
            )

    with pytest.raises(DataGovernanceError, match="precedes_admission"):
        publish_data_usage_binding_for_artifact(
            manager=manager,
            source=source,
            affected_authority_refs=(affected_ref,),
            recorded_by="validation-pipeline",
            recorded_at="2025-01-03T00:00:00+00:00",
        )
    with pytest.raises(DataGovernanceError, match="identity_conflict"):
        publish_data_usage_binding_for_artifact(
            manager=manager,
            source=source,
            affected_authority_refs=(affected_ref,),
            recorded_by="different-actor",
        )


def test_unresolved_critical_issue_blocks_report_and_existing_validation_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest(tmp_path)
    monkeypatch.setattr(
        "market_research.research.knowledge_registry.require_point_in_time_scope",
        lambda *_args, **_kwargs: None,
    )
    dataset, _admission, row, _policy = _publish_admission(manager, manifest)
    frozen = freeze_validation_admission(manager=manager, manifest=manifest)
    issue = DataQualityIssue(
        issue_id="dataset-v1-close-corruption",
        issue_version="1",
        dataset=dataset,
        severity="CRITICAL",
        status="OPEN",
        affected_start_ts=dataset.start_ts,
        affected_end_ts=dataset.end_ts,
        affected_instruments=("KRW-BTC",),
        affected_fields=("close",),
        summary="Close values are corrupt for the admitted dataset version.",
        evidence_hashes=(_hash("d"),),
        discovered_by="quality-monitor",
        discovered_at="2025-01-06T00:00:00+00:00",
        known_at="2025-01-06T00:00:00+00:00",
    )
    publish_data_governance_record(manager=manager, record=issue)

    with pytest.raises(DataGovernanceError, match="unresolved_critical_issue"):
        require_data_governance_report_binding(
            manager=manager,
            source=_report_binding(
                manager=manager,
                dataset=dataset,
                row=row,
                validation_admission=frozen,
            ),
        )
    with pytest.raises(KnowledgeRegistryError, match="unresolved_critical_issue"):
        require_validation_admission(manager=manager, manifest=manifest)

    resolution = IssueResolution(
        resolution_id="dataset-v1-close-corruption-resolution",
        resolution_version="1",
        issue_ref=issue.ref(),
        status="RESOLVED",
        resolution_summary="Hash-bound correction evidence proves the issue false.",
        evidence_hashes=(_hash("f"),),
        replacement_dataset=None,
        resolved_by="quality-reviewer",
        resolved_at="2025-01-07T00:00:00+00:00",
    )
    publish_data_governance_record(manager=manager, record=resolution)
    assert (
        require_data_governance_report_binding(
            manager=manager,
            source=_report_binding(
                manager=manager,
                dataset=dataset,
                row=row,
                validation_admission=frozen,
            ),
        )
        == row
    )

    reopened = IssueResolution(
        resolution_id="dataset-v1-close-corruption-reopened",
        resolution_version="1",
        issue_ref=issue.ref(),
        status="REOPENED",
        resolution_summary="Later evidence invalidates the prior resolution.",
        evidence_hashes=(_hash("1"),),
        replacement_dataset=None,
        resolved_by="quality-reviewer",
        resolved_at="2025-01-08T00:00:00+00:00",
    )
    publish_data_governance_record(manager=manager, record=reopened)
    stale_resolution = IssueResolution(
        resolution_id="dataset-v1-close-corruption-stale-resolution",
        resolution_version="1",
        issue_ref=issue.ref(),
        status="RESOLVED",
        resolution_summary="A late append attempts to restore an older state.",
        evidence_hashes=(_hash("2"),),
        replacement_dataset=None,
        resolved_by="quality-reviewer",
        resolved_at="2025-01-07T12:00:00+00:00",
    )
    with pytest.raises(
        DataGovernanceError,
        match="issue_resolution_timestamp_not_monotonic",
    ):
        publish_data_governance_record(
            manager=manager,
            record=stale_resolution,
        )
    with pytest.raises(DataGovernanceError, match="unresolved_critical_issue"):
        require_data_governance_report_binding(
            manager=manager,
            source=_report_binding(
                manager=manager,
                dataset=dataset,
                row=row,
                validation_admission=frozen,
            ),
        )


def test_license_allow_cannot_override_denied_export_policy(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest(tmp_path)
    dataset = dataset_version_ref_from_manifest(manifest)
    policy = replace(
        _matching_policy(
            dataset,
            policy_id="restricted-license",
            derivative_retention_allowed=False,
            research_package_export_allowed=False,
        ),
        allowed_distribution_scopes=("INTERNAL_RESEARCH",),
    )
    publish_data_governance_record(manager=manager, record=policy)
    forged_allow = DatasetUseDecision(
        decision_id="forged-export-allow",
        decision_version="1",
        dataset=dataset,
        policy_ref=policy.ref(),
        purpose="RESEARCH_PACKAGE_EXPORT",
        decision="ALLOW",
        distribution_scope="INTERNAL_RESEARCH_PACKAGE",
        rationale="Attempt to override the source policy.",
        decided_by="license-reviewer",
        decided_at="2025-01-02T00:00:00+00:00",
    )
    with pytest.raises(DataGovernanceError, match="scope_not_allowed"):
        publish_data_governance_record(manager=manager, record=forged_allow)
    assert validate_data_governance_registry(manager)["row_count"] == 1


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("provider_id", "provider-b", "license_policy_provider_mismatch"),
        (
            "source_catalog_hash",
            _hash("6"),
            "license_policy_source_catalog_mismatch",
        ),
        (
            "catalog_entry_hash",
            _hash("7"),
            "license_policy_catalog_entry_mismatch",
        ),
        ("license_id", "wrong-license", "license_policy_license_mismatch"),
        ("terms_hash", _hash("8"), "license_policy_terms_mismatch"),
    ),
)
def test_use_decision_rejects_policy_not_bound_to_dataset_provenance(
    tmp_path: Path,
    field: str,
    value: object,
    error: str,
) -> None:
    manager = _manager(tmp_path)
    dataset = dataset_version_ref_from_manifest(_manifest(tmp_path))
    policy = replace(
        _matching_policy(dataset, policy_id="mismatched-policy"),
        **{field: value},
    )
    publish_data_governance_record(manager=manager, record=policy)
    decision = DatasetUseDecision(
        decision_id="mismatched-policy-use",
        decision_version="1",
        dataset=dataset,
        policy_ref=policy.ref(),
        purpose="CONFIRMATORY_RESEARCH",
        decision="ALLOW",
        distribution_scope="INTERNAL_RESEARCH",
        rationale="Must be rejected because policy identity differs from provenance.",
        decided_by="license-reviewer",
        decided_at="2025-01-02T00:00:00+00:00",
    )

    with pytest.raises(DataGovernanceError, match=error):
        publish_data_governance_record(manager=manager, record=decision)


def test_package_export_requires_derivative_retention_permission(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    dataset = dataset_version_ref_from_manifest(_manifest(tmp_path))
    policy = _matching_policy(
        dataset,
        policy_id="no-derivative-retention",
        derivative_retention_allowed=False,
        research_package_export_allowed=True,
    )
    publish_data_governance_record(manager=manager, record=policy)
    decision = DatasetUseDecision(
        decision_id="retention-forbidden-export",
        decision_version="1",
        dataset=dataset,
        policy_ref=policy.ref(),
        purpose="RESEARCH_PACKAGE_EXPORT",
        decision="ALLOW",
        distribution_scope="INTERNAL_RESEARCH_PACKAGE",
        rationale="Attempt to retain a derivative package despite source terms.",
        decided_by="license-reviewer",
        decided_at="2025-01-02T00:00:00+00:00",
    )

    with pytest.raises(
        DataGovernanceError,
        match="license_use_decision_derivative_retention_not_allowed",
    ):
        publish_data_governance_record(manager=manager, record=decision)


def test_policy_cannot_grant_redistribution_for_catalog_that_forbids_it(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    dataset = dataset_version_ref_from_manifest(_manifest(tmp_path))
    policy = replace(
        _matching_policy(dataset, policy_id="overbroad-redistribution"),
        redistribution_allowed=True,
    )
    publish_data_governance_record(manager=manager, record=policy)
    decision = DatasetUseDecision(
        decision_id="overbroad-redistribution-use",
        decision_version="1",
        dataset=dataset,
        policy_ref=policy.ref(),
        purpose="CONFIRMATORY_RESEARCH",
        decision="ALLOW",
        distribution_scope="INTERNAL_RESEARCH",
        rationale="Attempt to broaden the immutable catalog permission.",
        decided_by="license-reviewer",
        decided_at="2025-01-02T00:00:00+00:00",
    )

    with pytest.raises(
        DataGovernanceError,
        match="license_policy_exceeds_catalog_redistribution",
    ):
        publish_data_governance_record(manager=manager, record=decision)


def test_reverse_impact_query_and_identity_conflict_are_fail_closed(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest(tmp_path)
    dataset, admission, _row, policy = _publish_admission(manager, manifest)
    refs = tuple(
        sorted(
            (
                AuthorityRef(
                    authority="knowledge_registry",
                    subject_type="experiment",
                    subject_id=manifest.experiment_id,
                    subject_version=manifest.manifest_hash(),
                    authority_hash=_hash("a"),
                ),
                AuthorityRef(
                    authority="research_package_registry",
                    subject_type="research_package",
                    subject_id="package-1",
                    subject_version="1",
                    authority_hash=_hash("b"),
                ),
            ),
            key=lambda item: json.dumps(item.as_dict(), sort_keys=True),
        )
    )
    binding = DataUsageBinding(
        binding_id="dataset-v1-impact-binding",
        binding_version="1",
        dataset=dataset,
        governance_admission_ref=admission.ref(),
        affected_authority_refs=refs,
        recorded_by="research-librarian",
        recorded_at="2025-01-08T00:00:00+00:00",
    )
    publish_data_governance_record(manager=manager, record=binding)

    impacts = query_data_governance_impacts(
        manager=manager, dataset_version_hash=dataset.version_hash
    )
    assert [item["subject_type"] for item in impacts["affected_authority_refs"]] == [
        "experiment",
        "research_package",
    ]

    conflict = replace(policy, terms_hash=_hash("c"))
    with pytest.raises(DataGovernanceError, match="identity_conflict"):
        publish_data_governance_record(manager=manager, record=conflict)

    registry_path = data_governance_registry_path(manager)
    raw = registry_path.read_text(encoding="utf-8")
    registry_path.write_text(
        raw.replace(policy.terms_hash, _hash("0"), 1), encoding="utf-8"
    )
    assert validate_data_governance_registry(manager)["status"] == "FAIL"
