from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from market_research.research.multi_asset.cards import (
    CardValidationResult,
    DataCard,
    DataFieldDefinition,
    DataFieldUnit,
    DataRowResolverMetadata,
    DataTemporalSemantics,
    DistributionStatus,
    ModelCard,
    ModelParameter,
    ValidationStatus,
)
from market_research.research.multi_asset.evidence import evidence_hash
from market_research.research.multi_asset.evidence_graph import (
    EvidenceGraph,
    EvidenceGraphResolver,
    EvidenceNodeKind,
    EvidenceRelation,
)
from market_research.research.multi_asset.public_package import (
    PublicPackageMaterials,
    build_public_validated_package_request,
)
from market_research.research.multi_asset.validated_package import (
    PackageArtifactRole,
    build_validated_package,
    reproduce_validated_package,
    verify_validated_package,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _hash(label: str) -> str:
    return evidence_hash({"label": label}, label="public-package-test")


def _field(
    field_name: str,
    data_type: str,
    semantic_type: str,
    description: str,
) -> DataFieldDefinition:
    return DataFieldDefinition(
        field_name=field_name,
        data_type=data_type,
        semantic_type=semantic_type,
        nullable=False,
        description=description,
    )


def _parameter(
    parameter_name: str,
    value: str,
    description: str,
) -> ModelParameter:
    return ModelParameter(
        parameter_name=parameter_name,
        value=value,
        data_type="string",
        unit="NOT_APPLICABLE",
        description=description,
    )


def _cards() -> tuple[DataCard, ModelCard]:
    data_validation = CardValidationResult(
        check_id="immutable_source_coverage",
        status=ValidationStatus.PASS,
        summary="Every canonical input leaf resolves to an immutable source row.",
        evidence_hash=_hash("data-validation"),
    )
    model_validation = CardValidationResult(
        check_id="deterministic_public_profile",
        status=ValidationStatus.PASS,
        summary="The supported public profile produced identical economic hashes.",
        evidence_hash=_hash("model-validation"),
    )
    return (
        DataCard(
            card_id="card:data:public",
            version="v1",
            dataset_id="dataset:public",
            dataset_version="v1",
            source_name="Externally prepared immutable fixture",
            source_reference="input:public",
            license_id="TEST-ONLY",
            license_terms_hash=_hash("license"),
            distribution_status=DistributionStatus.REDISTRIBUTABLE,
            use_constraints=("OFFLINE_RESEARCH_ONLY",),
            snapshot_method="Externally prepared immutable fixture snapshot.",
            coverage_start_at="2025-01-01T00:00:00Z",
            coverage_end_at="2025-01-02T00:00:00Z",
            coverage_markets=("SYNTHETIC",),
            coverage_instruments=("asset_xyz",),
            field_schema=(
                _field(
                    "availability_at",
                    "RFC3339 timestamp",
                    "availability_time",
                    "Earliest research-consumption time.",
                ),
                _field(
                    "instrument_id",
                    "string",
                    "instrument_identifier",
                    "Stable instrument identifier.",
                ),
                _field(
                    "knowledge_at",
                    "RFC3339 timestamp",
                    "knowledge_time",
                    "Provider knowledge time.",
                ),
                _field(
                    "price",
                    "decimal string",
                    "close_price",
                    "Synthetic close price.",
                ),
                _field(
                    "source_artifact_hash",
                    "sha256 string",
                    "source_artifact_hash",
                    "Immutable source artifact hash.",
                ),
                _field(
                    "source_row_hash",
                    "sha256 string",
                    "source_row_hash",
                    "Canonical source-row hash.",
                ),
                _field(
                    "valid_at",
                    "RFC3339 timestamp",
                    "valid_time",
                    "Economic event time.",
                ),
            ),
            units=(
                DataFieldUnit(
                    field_name="availability_at",
                    unit="UTC_TIMESTAMP",
                ),
                DataFieldUnit(field_name="instrument_id", unit="IDENTIFIER"),
                DataFieldUnit(field_name="knowledge_at", unit="UTC_TIMESTAMP"),
                DataFieldUnit(field_name="price", unit="USD_PER_UNIT"),
                DataFieldUnit(field_name="source_artifact_hash", unit="SHA256"),
                DataFieldUnit(field_name="source_row_hash", unit="SHA256"),
                DataFieldUnit(field_name="valid_at", unit="UTC_TIMESTAMP"),
            ),
            temporal_semantics=DataTemporalSemantics(
                valid_time_field="valid_at",
                valid_time_definition="Economic event time represented by the row.",
                knowledge_time_field="knowledge_at",
                knowledge_time_definition="Time the row became provider-known.",
                availability_time_field="availability_at",
                availability_time_definition=(
                    "Earliest time at which research may consume the row."
                ),
                timezone="UTC",
                calendar_ids=("calendar.synthetic",),
            ),
            normalization_transformations=(
                "DECIMAL_PRICE_PRESERVED",
                "UTC_TIMESTAMP_CANONICALIZATION",
            ),
            known_corrections=(),
            missing_data_summary="No missing fields in the declared fixture.",
            missing_data_policy="Fail closed on any absent required field.",
            survivorship_policy="Use only the point-in-time admitted fixture set.",
            corporate_action_policy="No corporate actions exist in this fixture.",
            known_biases=("SYNTHETIC_FIXTURE_NOT_REAL_MARKET_CALIBRATION",),
            known_limitations=("BOUNDED_SYNTHETIC_COVERAGE",),
            intended_uses=("OFFLINE_RESEARCH_CONFORMANCE",),
            prohibited_uses=("LIVE_TRADING_OR_ACCOUNT_USE",),
            revision_policy="Inputs are immutable and revisions require a new version.",
            source_hashes=(_hash("immutable-input"),),
            row_resolver_metadata=DataRowResolverMetadata(
                resolver_id="resolver:public:fixture",
                resolver_version="v1",
                row_identity_fields=("instrument_id", "valid_at"),
                source_artifact_hash_field="source_artifact_hash",
                source_row_hash_field="source_row_hash",
                resolution_policy=(
                    "Resolve each canonical key to exactly one immutable source row."
                ),
            ),
            validation_results=(data_validation,),
            quality_flags=("IMMUTABLE_SOURCE_ROWS",),
        ),
        ModelCard(
            card_id="card:model:public",
            version="v1",
            model_id="model:public",
            model_version="v1",
            model_name="Deterministic public multi-asset conformance profile",
            model_family="Deterministic multi-asset conformance model",
            implementation_hash=_hash("implementation"),
            code_hash=_hash("code"),
            configuration_hash=_hash("configuration"),
            input_schema_hash=_hash("input-schema"),
            output_schema_hash=_hash("output-schema"),
            input_hashes=(_hash("model-input"),),
            output_hashes=(_hash("model-output"),),
            assumptions=("EXTERNALLY_PREPARED_INPUTS",),
            applicability_scope=("SUPPORTED_CLOSED_PROFILE",),
            unsupported_cases=("LIVE_ACCOUNT_OR_NETWORK_STATE",),
            parameters=(
                _parameter(
                    "profile_id",
                    "PUBLIC_CONFORMANCE_V1",
                    "Closed supported conformance profile.",
                ),
            ),
            calibration_data_hashes=(_hash("calibration-data"),),
            calibration_process=(
                "Derive analytics only from source-bound immutable inputs."
            ),
            objective="Produce a deterministic source-bound evidence receipt.",
            diagnostic_results=(
                CardValidationResult(
                    check_id="profile_diagnostics",
                    status=ValidationStatus.PASS,
                    summary="All public profile diagnostics passed.",
                    evidence_hash=_hash("profile-diagnostics"),
                ),
            ),
            convergence_criteria="Deterministic one-pass closed-profile evaluation.",
            convergence_result=CardValidationResult(
                check_id="profile_convergence",
                status=ValidationStatus.PASS,
                summary="Closed-profile evaluation converged deterministically.",
                evidence_hash=_hash("profile-convergence"),
            ),
            failure_conditions=("UNKNOWN_CONVENTION_OR_MISSING_EVIDENCE",),
            failure_behavior="Reject without publishing a profile receipt.",
            validation_results=(model_validation,),
            benchmark_results=(
                CardValidationResult(
                    check_id="reference_benchmark",
                    status=ValidationStatus.PASS,
                    summary="Reference fixture benchmark matched.",
                    evidence_hash=_hash("reference-benchmark"),
                ),
            ),
            sensitivity_results=(
                CardValidationResult(
                    check_id="bounded_sensitivity",
                    status=ValidationStatus.PASS,
                    summary="Bounded input perturbation behavior was retained.",
                    evidence_hash=_hash("bounded-sensitivity"),
                ),
            ),
            deterministic_configuration=(
                _parameter(
                    "random_seed",
                    "0",
                    "Fixed deterministic seed.",
                ),
            ),
            known_limitations=("NOT_A_PROPRIETARY_MARKET_FEED",),
            quality_flags=("DETERMINISTIC_MODEL",),
        ),
    )


def test_public_execution_materials_build_verify_and_reproduce_cold_package(
    tmp_path: Path,
) -> None:
    data_card, model_card = _cards()
    material = PublicPackageMaterials(
        package_id="package:public:fixture",
        package_version="v1",
        seed=7,
        request={"schema_version": 1, "request_id": "request:public"},
        specification={"schema_version": 1, "experiment_id": "experiment:public"},
        immutable_inputs={
            "schema_version": 1,
            "source_rows": [{"row_id": "row:1", "price": "100"}],
        },
        data_card=data_card,
        model_card=model_card,
        policy={"schema_version": 1, "policy_id": "policy:public"},
        configuration={"schema_version": 1, "profile_id": "profile:public"},
        dependency_identity={"schema_version": 1, "lock_hash": _hash("lock")},
        runtime_identity={"schema_version": 1, "network_required": False},
        normalized_evidence={
            "schema_version": 1,
            "normalized_rows": [{"instrument_id": "asset_xyz", "price": "100"}],
        },
        derived_evidence={
            "schema_version": 1,
            "profile_receipt_hash": _hash("receipt"),
        },
        accounting={
            "schema_version": 1,
            "ledger_hash": _hash("ledger"),
            "reconciled": True,
        },
        evidence_quality_flags=(
            "IMMUTABLE_SOURCE_ROWS",
            "PUBLIC_PROFILE_EXECUTED",
        ),
    )

    request = build_public_validated_package_request(material)
    assert {item.role for item in request.artifacts}.issuperset(
        {
            PackageArtifactRole.IMMUTABLE_INPUT,
            PackageArtifactRole.NORMALIZED_EVIDENCE,
            PackageArtifactRole.DERIVED_EVIDENCE,
            PackageArtifactRole.ACCOUNTING,
            PackageArtifactRole.EVIDENCE_GRAPH,
        }
    )
    target = (tmp_path / "public-package").resolve()
    published = build_validated_package(
        request,
        target,
        project_root=PROJECT_ROOT,
    )
    verification = verify_validated_package(published.path)
    reproduction = reproduce_validated_package(published.path)

    assert verification.status == "PASS"
    assert reproduction.status == "PASS"
    assert (
        reproduction.first_study_content_hash == reproduction.second_study_content_hash
    )
    assert (
        reproduction.first_report_content_hash
        == reproduction.second_report_content_hash
    )


def test_public_package_resolves_report_field_to_source_row_in_both_directions() -> (
    None
):
    data_card, model_card = _cards()
    material = PublicPackageMaterials(
        package_id="package:public:lineage",
        package_version="v1",
        seed=11,
        request={"schema_version": 1, "request_id": "request:lineage"},
        specification={
            "schema_version": 1,
            "experiment_id": "experiment:lineage",
        },
        immutable_inputs={
            "schema_version": 1,
            "source_rows": [
                {
                    "row_id": "row:lineage:1",
                    "instrument_id": "asset_xyz",
                    "price": "100",
                }
            ],
        },
        data_card=data_card,
        model_card=model_card,
        policy={"schema_version": 1, "policy_id": "policy:lineage"},
        configuration={"schema_version": 1, "profile_id": "profile:lineage"},
        dependency_identity={"schema_version": 1, "lock_hash": _hash("lineage-lock")},
        runtime_identity={"schema_version": 1, "network_required": False},
        normalized_evidence={
            "schema_version": 1,
            "normalized_rows": [
                {
                    "instrument_id": "asset_xyz",
                    "price": "100",
                    "unit": "USD_PER_UNIT",
                }
            ],
        },
        derived_evidence={
            "schema_version": 1,
            "spot_profile_receipt": {
                "profile_id": "profile:lineage",
                "net_pnl": "9.00",
                "content_hash": _hash("lineage-profile"),
            },
        },
        accounting={
            "schema_version": 1,
            "opening_nav": "100.00",
            "ledger_pnl": "9.00",
            "closing_nav": "109.00",
            "reconciled": True,
        },
        evidence_quality_flags=(
            "IMMUTABLE_SOURCE_ROWS",
            "PUBLIC_PROFILE_EXECUTED",
        ),
    )
    request = build_public_validated_package_request(material)
    graph_artifact = next(
        item
        for item in request.artifacts
        if item.role is PackageArtifactRole.EVIDENCE_GRAPH
    )
    graph = EvidenceGraph.from_dict(json.loads(graph_artifact.payload))
    graph.require_resolvable_report_lineage()
    resolver = EvidenceGraphResolver(graph)

    claim = next(
        item
        for item in graph.nodes
        if item.kind is EvidenceNodeKind.REPORT_CLAIM
        and dict(item.attributes)["json_pointer"] == "/ledger_pnl"
    )
    source = next(
        item for item in graph.nodes if item.kind is EvidenceNodeKind.SOURCE_ROW
    )
    upstream = resolver.machine_query(claim.node_id, direction="upstream")
    upstream_nodes = cast(list[dict[str, object]], upstream["nodes"])
    upstream_edges = cast(list[dict[str, object]], upstream["edges"])
    upstream_kinds = {item["kind"] for item in upstream_nodes}
    assert {
        EvidenceNodeKind.SOURCE_ROW.value,
        EvidenceNodeKind.NORMALIZED.value,
        EvidenceNodeKind.MODEL_CARD.value,
        EvidenceNodeKind.ANALYSIS.value,
        EvidenceNodeKind.ACCOUNTING.value,
        EvidenceNodeKind.REPORT_CLAIM.value,
    }.issubset(upstream_kinds)
    assert source.node_id in {item["node_id"] for item in upstream_nodes}

    downstream = resolver.machine_query(source.node_id, direction="downstream")
    downstream_nodes = cast(list[dict[str, object]], downstream["nodes"])
    assert claim.node_id in {item["node_id"] for item in downstream_nodes}
    relations = {item["relation"] for item in upstream_edges}
    assert {
        EvidenceRelation.NORMALIZES.value,
        EvidenceRelation.CALCULATES.value,
        EvidenceRelation.DERIVES.value,
        EvidenceRelation.RECONCILES.value,
        EvidenceRelation.REPORTS.value,
    }.issubset(relations)
    assert any(
        item.role is PackageArtifactRole.IMMUTABLE_INPUT
        and item.logical_id.startswith("source-row:")
        for item in request.artifacts
    )
    assert any(
        item.role is PackageArtifactRole.NORMALIZED_EVIDENCE
        and item.logical_id.startswith("normalized-record:")
        for item in request.artifacts
    )
    assert any(
        item.role is PackageArtifactRole.DERIVED_EVIDENCE
        and item.logical_id.startswith("profile-output:")
        for item in request.artifacts
    )
    assert any(
        item.role is PackageArtifactRole.DERIVED_EVIDENCE
        and item.logical_id.startswith("report-field:")
        for item in request.artifacts
    )


def test_structured_normalization_lineage_avoids_source_row_cartesian_edges() -> None:
    data_card, model_card = _cards()
    material = PublicPackageMaterials(
        package_id="package:public:bounded-lineage",
        package_version="v1",
        seed=17,
        request={"schema_version": 1, "request_id": "request:bounded-lineage"},
        specification={
            "schema_version": 1,
            "experiment_id": "experiment:bounded-lineage",
        },
        immutable_inputs={
            "schema_version": 1,
            "source_rows": [
                {"row_id": "row:bounded:1", "price": "100"},
                {"row_id": "row:bounded:2", "price": "101"},
            ],
        },
        data_card=data_card,
        model_card=model_card,
        policy={"schema_version": 1, "policy_id": "policy:bounded-lineage"},
        configuration={
            "schema_version": 1,
            "profile_id": "profile:bounded-lineage",
        },
        dependency_identity={
            "schema_version": 1,
            "lock_hash": _hash("bounded-lineage-lock"),
        },
        runtime_identity={"schema_version": 1, "network_required": False},
        normalized_evidence={
            "schema_version": 1,
            "records": [
                {
                    "input_path": "/prices/0",
                    "normalized_value": "100",
                    "source_row_id": "row:bounded:1",
                },
                {
                    "input_path": "/prices/1",
                    "normalized_value": "101",
                    "source_row_id": "row:bounded:2",
                },
            ],
            "coverage_hash": _hash("bounded-lineage-coverage"),
        },
        derived_evidence={
            "schema_version": 1,
            "profile_receipt_hash": _hash("bounded-lineage-receipt"),
        },
        accounting={
            "schema_version": 1,
            "ledger_hash": _hash("bounded-lineage-ledger"),
            "reconciled": True,
        },
        evidence_quality_flags=(
            "IMMUTABLE_SOURCE_ROWS",
            "PUBLIC_PROFILE_EXECUTED",
        ),
    )

    request = build_public_validated_package_request(material)
    graph_artifact = next(
        item
        for item in request.artifacts
        if item.role is PackageArtifactRole.EVIDENCE_GRAPH
    )
    graph = EvidenceGraph.from_dict(json.loads(graph_artifact.payload))
    nodes = {item.node_id: item for item in graph.nodes}
    row_normalization_edges = [
        edge
        for edge in graph.edges
        if edge.relation is EvidenceRelation.NORMALIZES
        and nodes[edge.source_id].kind is EvidenceNodeKind.SOURCE_ROW
    ]

    assert len(row_normalization_edges) == 2
    assert len({edge.source_id for edge in row_normalization_edges}) == 2
    assert len({edge.target_id for edge in row_normalization_edges}) == 2
