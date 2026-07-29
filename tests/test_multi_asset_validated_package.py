from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

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
    ResearchCardError,
    ValidationStatus,
)
from market_research.research.multi_asset.evidence_graph import (
    EvidenceEdge,
    EvidenceGraph,
    EvidenceGraphError,
    EvidenceGraphResolver,
    EvidenceResolver,
    EvidenceNode,
    EvidenceNodeKind,
    EvidenceRelation,
)
from market_research.research.multi_asset.portable_runtime import (
    PortableRuntimeError,
    validate_card,
)
from market_research.research.multi_asset.validated_package import (
    PackageArtifactRole,
    PortablePackageBuildRequest,
    PortableSourceArtifact,
    ValidatedPackageError,
    ValidatedPackageVerifier,
    build_validated_package,
    reproduce_validated_package,
    verify_validated_package,
)
from market_research.research_cli.context import ResearchAppContext
from market_research.research_cli.registry import command_registry
from market_research.paths import ResearchPathManager
from market_research.settings import ResearchSettings


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_GOLDEN_MANIFEST_HASH = (
    "sha256:ca288aae80b0b12a833b761c9eacd95f668ba76cb29c7ec4f8ee2c551750c7ad"
)


def _hash(label: str) -> str:
    return f"sha256:{hashlib.sha256(label.encode('utf-8')).hexdigest()}"


def _validation(check_id: str, label: str) -> CardValidationResult:
    return CardValidationResult(
        check_id=check_id,
        status=ValidationStatus.PASS,
        summary=f"{label} validation passed.",
        evidence_hash=_hash(f"{label}:validation"),
    )


def _data_field_schema() -> tuple[DataFieldDefinition, ...]:
    return tuple(
        DataFieldDefinition(
            field_name=field_name,
            data_type=data_type,
            semantic_type=semantic_type,
            nullable=False,
            description=description,
        )
        for field_name, data_type, semantic_type, description in (
            (
                "availability_at",
                "RFC3339 timestamp",
                "availability_time",
                "Time at which the row may be consumed by research.",
            ),
            (
                "instrument_id",
                "string",
                "instrument_identifier",
                "Stable instrument identifier.",
            ),
            (
                "knowledge_at",
                "RFC3339 timestamp",
                "knowledge_time",
                "First time the provider states the row was known.",
            ),
            ("price", "decimal string", "close_price", "Observed close price."),
            (
                "source_artifact_hash",
                "sha256 string",
                "source_artifact_hash",
                "Immutable source artifact hash.",
            ),
            (
                "source_row_hash",
                "sha256 string",
                "source_row_hash",
                "Canonical source-row hash.",
            ),
            (
                "valid_at",
                "RFC3339 timestamp",
                "valid_time",
                "Economic time represented by the row.",
            ),
        )
    )


def _data_units() -> tuple[DataFieldUnit, ...]:
    return tuple(
        DataFieldUnit(field_name=field_name, unit=unit)
        for field_name, unit in (
            ("availability_at", "UTC_TIMESTAMP"),
            ("instrument_id", "IDENTIFIER"),
            ("knowledge_at", "UTC_TIMESTAMP"),
            ("price", "USD_PER_UNIT"),
            ("source_artifact_hash", "SHA256"),
            ("source_row_hash", "SHA256"),
            ("valid_at", "UTC_TIMESTAMP"),
        )
    )


def _model_parameter(
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


def _data_card(version: str = "v1") -> DataCard:
    return DataCard(
        card_id="card:data:prices",
        version=version,
        dataset_id="dataset:prices",
        dataset_version=version,
        source_name="externally-prepared-fixture",
        source_reference=f"catalog:prices:{version}",
        license_id="fixture-research-license",
        license_terms_hash=_hash("license-terms"),
        distribution_status=DistributionStatus.LICENSE_RESTRICTED,
        use_constraints=(
            "NO_REDISTRIBUTION",
            "OFFLINE_RESEARCH_ONLY",
        ),
        snapshot_method="Externally prepared immutable point-in-time snapshot.",
        coverage_start_at="2025-01-01T00:00:00Z",
        coverage_end_at="2025-01-03T00:00:00Z",
        coverage_markets=("TEST_MARKET",),
        coverage_instruments=("asset_xyz",),
        field_schema=_data_field_schema(),
        units=_data_units(),
        temporal_semantics=DataTemporalSemantics(
            valid_time_field="valid_at",
            valid_time_definition="Economic event time represented by the row.",
            knowledge_time_field="knowledge_at",
            knowledge_time_definition="Time the observation became provider-known.",
            availability_time_field="availability_at",
            availability_time_definition=(
                "Earliest point at which the research engine may consume the row."
            ),
            timezone="UTC",
            calendar_ids=("calendar.test_market",),
        ),
        normalization_transformations=(
            "CANONICAL_UTC_TIMESTAMP_NORMALIZATION",
            "DECIMAL_PRICE_PRESERVED",
        ),
        known_corrections=(),
        missing_data_summary="No missing rows in the bounded fixture.",
        missing_data_policy="Reject any timestamp gap.",
        survivorship_policy="Use the point-in-time admitted instrument set only.",
        corporate_action_policy="No corporate actions exist in the fixture.",
        known_biases=("SYNTHETIC_FIXTURE",),
        known_limitations=("BOUNDED_THREE_DAY_TEST_WINDOW",),
        intended_uses=("DETERMINISTIC_OFFLINE_RESEARCH_VALIDATION",),
        prohibited_uses=("LIVE_TRADING_OR_ACCOUNT_DECISIONS",),
        revision_policy="Immutable; revisions require a new dataset version.",
        source_hashes=(_hash("source-document"),),
        row_resolver_metadata=DataRowResolverMetadata(
            resolver_id="resolver:fixture:prices",
            resolver_version="v1",
            row_identity_fields=("instrument_id", "valid_at"),
            source_artifact_hash_field="source_artifact_hash",
            source_row_hash_field="source_row_hash",
            resolution_policy=(
                "Resolve the instrument and valid-time key to exactly one "
                "source-row and source-artifact hash."
            ),
        ),
        validation_results=(_validation("data:row-count", "data"),),
        quality_flags=("SOURCE_COMPLETE",),
    )


def _model_card(version: str = "v1") -> ModelCard:
    return ModelCard(
        card_id="card:model:pnl",
        version=version,
        model_id="model:deterministic-pnl",
        model_version=version,
        model_name="Deterministic fixture PnL",
        model_family="Deterministic arithmetic research model",
        implementation_hash=_hash("model-implementation"),
        code_hash=_hash("model-code"),
        configuration_hash=_hash("model-configuration"),
        input_schema_hash=_hash("model-input-schema"),
        output_schema_hash=_hash("model-output-schema"),
        input_hashes=(_hash("model-input"),),
        output_hashes=(_hash("model-output"),),
        assumptions=("DECIMAL_ARITHMETIC", "NO_EXTERNAL_STATE"),
        applicability_scope=("BOUNDED_FIXTURE", "OFFLINE_RESEARCH"),
        unsupported_cases=("LIVE_ACCOUNT_OR_NETWORK_STATE",),
        parameters=(
            _model_parameter(
                "arithmetic_mode",
                "DECIMAL",
                "Use exact decimal arithmetic.",
            ),
            _model_parameter(
                "fee_rate",
                "0",
                "Fixture fee rate.",
            ),
        ),
        calibration_data_hashes=(_hash("calibration-data"),),
        calibration_process="No fitting; validate exact fixture arithmetic.",
        objective="Reproduce source-bound fixture PnL exactly.",
        diagnostic_results=(_validation("model:diagnostic", "diagnostic"),),
        convergence_criteria="Exact one-pass arithmetic evaluation.",
        convergence_result=_validation("model:convergence", "convergence"),
        failure_conditions=("INPUT_HASH_MISMATCH", "MISSING_PRICE_ROW"),
        failure_behavior="Reject without emitting a model result.",
        validation_results=(_validation("model:golden", "model"),),
        benchmark_results=(_validation("model:benchmark", "benchmark"),),
        sensitivity_results=(_validation("model:sensitivity", "sensitivity"),),
        deterministic_configuration=(
            _model_parameter(
                "parallel_workers",
                "1",
                "Single deterministic worker.",
            ),
            _model_parameter(
                "random_seed",
                "0",
                "No stochastic path; fixed seed retained as evidence.",
            ),
        ),
        known_limitations=("NOT_FOR_LIVE_TRADING",),
        quality_flags=("MODEL_VALIDATED",),
    )


def _json_artifact(
    role: PackageArtifactRole,
    logical_id: str,
    payload: object,
    *,
    version: str = "v1",
    quality_flags: tuple[str, ...] = (),
) -> PortableSourceArtifact:
    return PortableSourceArtifact.from_json(
        role=role,
        logical_id=logical_id,
        version=version,
        payload=payload,
        quality_flags=quality_flags,
    )


def _edge(
    nodes: dict[str, EvidenceNode],
    source_id: str,
    target_id: str,
    relation: EvidenceRelation,
) -> EvidenceEdge:
    return EvidenceEdge(
        source_id=source_id,
        target_id=target_id,
        relation=relation,
        source_content_hash=nodes[source_id].content_hash,
        target_content_hash=nodes[target_id].content_hash,
    )


def _build_request(
    *,
    data_card_version: str = "v1",
) -> tuple[PortablePackageBuildRequest, EvidenceGraph]:
    data_card = _data_card(data_card_version)
    model_card = _model_card()
    sources = [
        _json_artifact(
            PackageArtifactRole.REQUEST,
            "config:request",
            {"schema_version": 1, "request_id": "request:fixture"},
        ),
        _json_artifact(
            PackageArtifactRole.SPEC,
            "config:spec",
            {
                "schema_version": 1,
                "experiment_id": "experiment:portable",
                "seed": 7,
            },
        ),
        _json_artifact(
            PackageArtifactRole.IMMUTABLE_INPUT,
            "row:prices:2025-01-02",
            {
                "schema_version": 1,
                "instrument_id": "asset_xyz",
                "event_at": "2025-01-02T00:00:00Z",
                "knowledge_at": "2025-01-02T00:00:00Z",
                "price": "100.00",
            },
            quality_flags=("SOURCE_COMPLETE",),
        ),
        _json_artifact(
            PackageArtifactRole.DATA_CARD,
            data_card.card_id,
            data_card.as_dict(),
            version=data_card.version,
            quality_flags=data_card.quality_flags,
        ),
        _json_artifact(
            PackageArtifactRole.MODEL_CARD,
            model_card.card_id,
            model_card.as_dict(),
            version=model_card.version,
            quality_flags=model_card.quality_flags,
        ),
        _json_artifact(
            PackageArtifactRole.POLICY,
            "policy:selection",
            {
                "schema_version": 1,
                "policy_id": "policy:selection",
                "version": "v1",
                "rule": "select_known_rows_only",
            },
        ),
        _json_artifact(
            PackageArtifactRole.CONFIGURATION,
            "config:run",
            {
                "schema_version": 1,
                "base_currency": "USD",
                "calendar_id": "calendar:fixture",
            },
        ),
        _json_artifact(
            PackageArtifactRole.DEPENDENCY_IDENTITY,
            "identity:dependencies",
            {
                "schema_version": 1,
                "python": "3.12",
                "dependencies": [],
            },
        ),
        _json_artifact(
            PackageArtifactRole.RUNTIME_IDENTITY,
            "identity:runtime",
            {
                "schema_version": 1,
                "platform": "portable-stdlib",
                "git_required": False,
                "network_required": False,
            },
        ),
        _json_artifact(
            PackageArtifactRole.NORMALIZED_EVIDENCE,
            "normalized:prices",
            {
                "schema_version": 1,
                "rows": [
                    {
                        "instrument_id": "asset_xyz",
                        "event_at": "2025-01-02T00:00:00Z",
                        "price": "100.00",
                    }
                ],
            },
            quality_flags=("SOURCE_COMPLETE",),
        ),
        _json_artifact(
            PackageArtifactRole.DERIVED_EVIDENCE,
            "analysis:pnl",
            {
                "schema_version": 1,
                "gross_pnl": "10.00",
                "cost": "1.00",
                "net_pnl": "9.00",
            },
            quality_flags=("MODEL_VALIDATED",),
        ),
        _json_artifact(
            PackageArtifactRole.ACCOUNTING,
            "accounting:ledger",
            {
                "schema_version": 1,
                "opening_nav": "100.00",
                "external_cash_flow": "0.00",
                "ledger_pnl": "9.00",
                "closing_nav": "109.00",
            },
        ),
        _json_artifact(
            PackageArtifactRole.DERIVED_EVIDENCE,
            "claim:report:net-pnl",
            {
                "schema_version": 1,
                "claim_id": "report:net-pnl",
                "value": "9.00",
                "unit": "USD",
            },
            quality_flags=("MODEL_VALIDATED", "SOURCE_COMPLETE"),
        ),
    ]
    by_id = {item.logical_id: item for item in sources}
    parent_ids = {
        "normalized:prices": (
            "card:data:prices",
            "row:prices:2025-01-02",
        ),
        "analysis:pnl": (
            "card:model:pnl",
            "config:request",
            "config:run",
            "config:spec",
            "normalized:prices",
            "policy:selection",
        ),
        "accounting:ledger": ("analysis:pnl",),
        "claim:report:net-pnl": ("accounting:ledger", "analysis:pnl"),
    }
    kinds = {
        "config:request": EvidenceNodeKind.CONFIGURATION,
        "config:spec": EvidenceNodeKind.CONFIGURATION,
        "row:prices:2025-01-02": EvidenceNodeKind.SOURCE_ROW,
        "card:data:prices": EvidenceNodeKind.DATA_CARD,
        "card:model:pnl": EvidenceNodeKind.MODEL_CARD,
        "policy:selection": EvidenceNodeKind.POLICY,
        "config:run": EvidenceNodeKind.CONFIGURATION,
        "normalized:prices": EvidenceNodeKind.NORMALIZED,
        "analysis:pnl": EvidenceNodeKind.ANALYSIS,
        "accounting:ledger": EvidenceNodeKind.ACCOUNTING,
        "claim:report:net-pnl": EvidenceNodeKind.REPORT_CLAIM,
    }
    nodes = {
        logical_id: EvidenceNode(
            node_id=logical_id,
            version=by_id[logical_id].version,
            kind=kind,
            label=logical_id.replace(":", " "),
            content_hash=by_id[logical_id].content_hash,
            parent_ids=tuple(sorted(parent_ids.get(logical_id, ()))),
            quality_flags=by_id[logical_id].quality_flags,
        )
        for logical_id, kind in kinds.items()
    }
    edges = (
        _edge(
            nodes,
            "card:data:prices",
            "normalized:prices",
            EvidenceRelation.DESCRIBES,
        ),
        _edge(
            nodes,
            "row:prices:2025-01-02",
            "normalized:prices",
            EvidenceRelation.NORMALIZES,
        ),
        _edge(
            nodes,
            "card:model:pnl",
            "analysis:pnl",
            EvidenceRelation.CALCULATES,
        ),
        _edge(
            nodes,
            "config:request",
            "analysis:pnl",
            EvidenceRelation.CONFIGURES,
        ),
        _edge(
            nodes,
            "config:run",
            "analysis:pnl",
            EvidenceRelation.CONFIGURES,
        ),
        _edge(
            nodes,
            "config:spec",
            "analysis:pnl",
            EvidenceRelation.CONFIGURES,
        ),
        _edge(
            nodes,
            "normalized:prices",
            "analysis:pnl",
            EvidenceRelation.DERIVES,
        ),
        _edge(
            nodes,
            "policy:selection",
            "analysis:pnl",
            EvidenceRelation.CONFIGURES,
        ),
        _edge(
            nodes,
            "analysis:pnl",
            "accounting:ledger",
            EvidenceRelation.RECONCILES,
        ),
        _edge(
            nodes,
            "accounting:ledger",
            "claim:report:net-pnl",
            EvidenceRelation.REPORTS,
        ),
        _edge(
            nodes,
            "analysis:pnl",
            "claim:report:net-pnl",
            EvidenceRelation.REPORTS,
        ),
    )
    graph = EvidenceGraph(
        graph_id="graph:portable-evidence",
        version="v1",
        nodes=tuple(sorted(nodes.values(), key=lambda item: item.node_id)),
        edges=tuple(sorted(edges, key=lambda item: item.identity)),
        terminal_ids=("claim:report:net-pnl",),
    )
    graph_flags = tuple(
        sorted({flag for node in graph.nodes for flag in node.quality_flags})
    )
    sources.append(
        _json_artifact(
            PackageArtifactRole.EVIDENCE_GRAPH,
            "graph:portable-evidence",
            graph.as_dict(),
            quality_flags=graph_flags,
        )
    )
    return (
        PortablePackageBuildRequest(
            package_id="package:portable-fixture",
            package_version="v1",
            seed=7,
            artifacts=tuple(
                sorted(
                    sources,
                    key=lambda item: (
                        item.role.value,
                        item.logical_id,
                        item.version,
                    ),
                )
            ),
        ),
        graph,
    )


def _artifact_payload(package: Path, role: PackageArtifactRole) -> dict[str, object]:
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    row = next(item for item in manifest["artifacts"] if item["role"] == role.value)
    value = json.loads((package / row["relative_path"]).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_data_and_model_cards_are_strict_versioned_and_hash_bound() -> None:
    data = _data_card()
    model = _model_card()

    assert DataCard.from_dict(data.as_dict()) == data
    assert ModelCard.from_dict(model.as_dict()) == model
    assert data.as_dict()["schema_version"] == 2
    assert model.as_dict()["schema_version"] == 2
    assert {
        "distribution_status",
        "snapshot_method",
        "field_schema",
        "units",
        "temporal_semantics",
        "normalization_transformations",
        "known_corrections",
        "survivorship_policy",
        "corporate_action_policy",
        "known_limitations",
        "intended_uses",
        "prohibited_uses",
        "source_hashes",
        "row_resolver_metadata",
    } <= data.as_dict().keys()
    assert {
        "model_family",
        "unsupported_cases",
        "parameters",
        "calibration_data_hashes",
        "calibration_process",
        "objective",
        "diagnostic_results",
        "convergence_criteria",
        "convergence_result",
        "benchmark_results",
        "sensitivity_results",
        "failure_behavior",
        "deterministic_configuration",
        "code_hash",
        "configuration_hash",
        "input_hashes",
        "output_hashes",
    } <= model.as_dict().keys()

    missing = data.as_dict()
    del missing["known_biases"]
    with pytest.raises(ResearchCardError, match="fields_invalid"):
        DataCard.from_dict(missing)

    tampered = model.as_dict()
    tampered["model_version"] = "v2"
    with pytest.raises(ResearchCardError, match="content_hash_mismatch"):
        ModelCard.from_dict(tampered)

    nested_tamper = data.as_dict()
    units = cast(list[dict[str, object]], nested_tamper["units"])
    units[0]["unit"] = "LOCAL_TIME"
    with pytest.raises(ResearchCardError, match="content_hash_mismatch"):
        DataCard.from_dict(nested_tamper)

    with pytest.raises(ResearchCardError, match="must_be_sorted_unique"):
        replace(data, field_schema=tuple(reversed(data.field_schema)))
    with pytest.raises(ResearchCardError, match="must_be_sorted_unique"):
        replace(
            model,
            deterministic_configuration=tuple(
                reversed(model.deterministic_configuration)
            ),
        )


def test_portable_runtime_enforces_complete_nested_card_schema() -> None:
    data_payload = _data_card().as_dict()
    model_payload = _model_card().as_dict()

    validate_card(data_payload, "DATA_CARD")
    validate_card(model_payload, "MODEL_CARD")

    missing = _data_card().as_dict()
    del missing["snapshot_method"]
    with pytest.raises(PortableRuntimeError, match="fields_invalid"):
        validate_card(missing, "DATA_CARD")

    nested_tamper = _model_card().as_dict()
    parameters = cast(
        list[dict[str, object]],
        nested_tamper["deterministic_configuration"],
    )
    parameters[0]["value"] = "8"
    with pytest.raises(PortableRuntimeError, match="content_hash_mismatch"):
        validate_card(nested_tamper, "MODEL_CARD")


def test_evidence_graph_resolves_report_claim_to_source_row_in_both_forms() -> None:
    _request, graph = _build_request()
    resolver = EvidenceGraphResolver(graph)
    assert EvidenceResolver(graph).graph.content_hash == graph.content_hash

    machine = resolver.machine_query("claim:report:net-pnl")
    node_ids = {item["node_id"] for item in machine["nodes"]}
    assert "row:prices:2025-01-02" in node_ids
    assert "normalized:prices" in node_ids
    assert "analysis:pnl" in node_ids
    assert "row:prices:2025-01-02" in resolver.human_query("claim:report:net-pnl")
    downstream = resolver.machine_query(
        "row:prices:2025-01-02",
        direction="downstream",
    )
    assert "claim:report:net-pnl" in {item["node_id"] for item in downstream["nodes"]}

    source = next(
        item for item in graph.nodes if item.node_id == "row:prices:2025-01-02"
    )
    source_payload = next(
        item
        for item in _request.artifacts
        if item.logical_id == "row:prices:2025-01-02"
    ).payload
    assert source.content_hash.startswith("sha256:")
    resolver.verify_content(source.node_id, source_payload)


def test_evidence_graph_rejects_missing_edge_cycle_and_tamper() -> None:
    _request, graph = _build_request()

    with pytest.raises(EvidenceGraphError, match="missing_or_extra_parent_edge"):
        EvidenceGraph(
            graph_id=graph.graph_id,
            version=graph.version,
            nodes=graph.nodes,
            edges=graph.edges[1:],
            terminal_ids=graph.terminal_ids,
        )

    a = EvidenceNode(
        node_id="derived:a",
        version="v1",
        kind=EvidenceNodeKind.DERIVED,
        label="A",
        content_hash=_hash("a"),
        parent_ids=("derived:b",),
    )
    b = EvidenceNode(
        node_id="derived:b",
        version="v1",
        kind=EvidenceNodeKind.DERIVED,
        label="B",
        content_hash=_hash("b"),
        parent_ids=("derived:a",),
    )
    terminal = EvidenceNode(
        node_id="claim:terminal",
        version="v1",
        kind=EvidenceNodeKind.REPORT_CLAIM,
        label="terminal",
        content_hash=_hash("terminal"),
        parent_ids=("derived:b",),
    )
    cycle_nodes = {item.node_id: item for item in (a, b, terminal)}
    with pytest.raises(EvidenceGraphError, match="cycle_detected"):
        EvidenceGraph(
            graph_id="graph:cycle",
            version="v1",
            nodes=(terminal, a, b),
            edges=tuple(
                sorted(
                    (
                        _edge(
                            cycle_nodes,
                            "derived:a",
                            "derived:b",
                            EvidenceRelation.DERIVES,
                        ),
                        _edge(
                            cycle_nodes,
                            "derived:b",
                            "derived:a",
                            EvidenceRelation.DERIVES,
                        ),
                        _edge(
                            cycle_nodes,
                            "derived:b",
                            "claim:terminal",
                            EvidenceRelation.REPORTS,
                        ),
                    ),
                    key=lambda item: item.identity,
                )
            ),
            terminal_ids=("claim:terminal",),
        )

    tampered = graph.as_dict()
    tampered["edges"][0]["relation"] = "SUPPORTS"
    with pytest.raises(EvidenceGraphError, match="edge_hash_mismatch"):
        EvidenceGraph.from_dict(tampered)


def test_evidence_graph_rejects_self_consistent_relation_kind_rewrite() -> None:
    _request, graph = _build_request()
    target = next(
        item for item in graph.edges if item.relation is EvidenceRelation.CALCULATES
    )
    rewritten = replace(target, relation=EvidenceRelation.REPORTS)

    with pytest.raises(EvidenceGraphError, match="relation_kind_mismatch"):
        EvidenceGraph(
            graph_id=graph.graph_id,
            version=graph.version,
            nodes=graph.nodes,
            edges=tuple(
                sorted(
                    (
                        rewritten if item.identity == target.identity else item
                        for item in graph.edges
                    ),
                    key=lambda item: item.identity,
                )
            ),
            terminal_ids=graph.terminal_ids,
        )


def test_package_build_rejects_aggregate_only_evidence_graph() -> None:
    request, graph = _build_request()
    claim_id = "claim:report:net-pnl"
    aggregate_nodes = tuple(
        sorted(
            (
                replace(
                    item,
                    kind=(
                        EvidenceNodeKind.IMMUTABLE_INPUT
                        if item.node_id == "row:prices:2025-01-02"
                        else item.kind
                    ),
                )
                for item in graph.nodes
                if item.node_id != claim_id
            ),
            key=lambda item: item.node_id,
        )
    )
    aggregate_edges = tuple(
        item
        for item in graph.edges
        if item.source_id != claim_id and item.target_id != claim_id
    )
    aggregate_graph = EvidenceGraph(
        graph_id=graph.graph_id,
        version=graph.version,
        nodes=aggregate_nodes,
        edges=aggregate_edges,
        terminal_ids=("accounting:ledger",),
    )
    graph_artifact = next(
        item
        for item in request.artifacts
        if item.role is PackageArtifactRole.EVIDENCE_GRAPH
    )
    aggregate_graph_artifact = _json_artifact(
        PackageArtifactRole.EVIDENCE_GRAPH,
        graph_artifact.logical_id,
        aggregate_graph.as_dict(),
        version=graph_artifact.version,
        quality_flags=graph_artifact.quality_flags,
    )
    aggregate_artifacts = tuple(
        sorted(
            (
                *(
                    item
                    for item in request.artifacts
                    if item.logical_id not in {claim_id, graph_artifact.logical_id}
                ),
                aggregate_graph_artifact,
            ),
            key=lambda item: (
                item.role.value,
                item.logical_id,
                item.version,
            ),
        )
    )

    with pytest.raises(
        ValidatedPackageError,
        match="aggregate_only_lineage_forbidden",
    ):
        PortablePackageBuildRequest(
            package_id=request.package_id,
            package_version=request.package_version,
            seed=request.seed,
            artifacts=aggregate_artifacts,
        )


def test_portable_package_golden_verify_reproduce_and_cold_root(
    tmp_path: Path,
) -> None:
    request, _graph = _build_request()
    package = tmp_path / "portable-package"
    published = build_validated_package(
        request,
        package,
        project_root=PROJECT_ROOT,
    )

    assert published.created
    assert published.manifest.content_hash == EXPECTED_GOLDEN_MANIFEST_HASH
    verified = verify_validated_package(package)
    reproduced = reproduce_validated_package(package)
    verifier = ValidatedPackageVerifier()
    assert verified.status == "PASS"
    assert reproduced.status == "PASS"
    assert reproduced.mismatch_fields == ()
    assert reproduced.first_study_content_hash == reproduced.second_study_content_hash
    assert reproduced.first_report_content_hash == reproduced.second_report_content_hash
    assert verifier.verify(package) == verified
    assert verifier.reproduce(package) == reproduced
    assert verified.quality_flags == ("MODEL_VALIDATED", "SOURCE_COMPLETE")
    assert str(PROJECT_ROOT) not in (package / "manifest.json").read_text(
        encoding="utf-8"
    )

    second = build_validated_package(
        request,
        package,
        project_root=PROJECT_ROOT,
    )
    assert not second.created
    other = build_validated_package(
        request,
        tmp_path / "portable-package-copy",
        project_root=PROJECT_ROOT,
    )
    assert other.manifest.content_hash == published.manifest.content_hash

    cold_root = tmp_path / "empty-cold-root"
    cold_root.mkdir()
    python = Path("/usr/bin/python3")
    assert python.is_file()
    environment = {
        "HOME": str(cold_root),
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "",
    }
    verify_run = subprocess.run(
        [str(python), "-I", str(package / "verify.py"), str(package)],
        cwd=cold_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert verify_run.returncode == 0, verify_run.stderr
    assert json.loads(verify_run.stdout)["status"] == "PASS"
    reproduce_run = subprocess.run(
        [str(python), "-I", str(package / "reproduce.py"), str(package)],
        cwd=cold_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert reproduce_run.returncode == 0, reproduce_run.stderr
    assert json.loads(reproduce_run.stdout)["status"] == "PASS"
    assert tuple(cold_root.iterdir()) == ()


def test_card_version_changes_package_and_replayed_outputs(tmp_path: Path) -> None:
    first_request, _ = _build_request(data_card_version="v1")
    second_request, _ = _build_request(data_card_version="v2")
    first = build_validated_package(
        first_request,
        tmp_path / "first",
        project_root=PROJECT_ROOT,
    )
    second = build_validated_package(
        second_request,
        tmp_path / "second",
        project_root=PROJECT_ROOT,
    )

    first_verify = verify_validated_package(first.path)
    second_verify = verify_validated_package(second.path)
    assert first.manifest.content_hash != second.manifest.content_hash
    assert first_verify.study_content_hash != second_verify.study_content_hash
    assert first_verify.report_content_hash != second_verify.report_content_hash


def test_quality_flags_reach_manifest_study_and_report(tmp_path: Path) -> None:
    request, _ = _build_request()
    package = build_validated_package(
        request,
        tmp_path / "quality",
        project_root=PROJECT_ROOT,
    ).path

    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    study = _artifact_payload(package, PackageArtifactRole.STUDY)
    report = _artifact_payload(package, PackageArtifactRole.REPORT)
    flags = ["MODEL_VALIDATED", "SOURCE_COMPLETE"]
    assert manifest["package_quality_flags"] == flags
    assert study["quality_flags"] == flags
    assert report["quality_flags"] == flags


@pytest.mark.parametrize(
    "role",
    (
        PackageArtifactRole.REPORT,
        PackageArtifactRole.DATA_CARD,
    ),
)
def test_package_rejects_missing_report_or_card(
    tmp_path: Path,
    role: PackageArtifactRole,
) -> None:
    request, _ = _build_request()
    package = build_validated_package(
        request,
        tmp_path / role.value.lower(),
        project_root=PROJECT_ROOT,
    ).path
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    record = next(item for item in manifest["artifacts"] if item["role"] == role.value)
    (package / record["relative_path"]).unlink()

    with pytest.raises(ValidatedPackageError):
        verify_validated_package(package)


def test_package_rejects_missing_manifest_tamper_and_unexpected_file(
    tmp_path: Path,
) -> None:
    request, _ = _build_request()
    original = build_validated_package(
        request,
        tmp_path / "original",
        project_root=PROJECT_ROOT,
    ).path

    missing = tmp_path / "missing-manifest"
    shutil.copytree(original, missing)
    (missing / "manifest.json").unlink()
    with pytest.raises(ValidatedPackageError):
        verify_validated_package(missing)

    tampered = tmp_path / "tampered"
    shutil.copytree(original, tampered)
    manifest = json.loads((tampered / "manifest.json").read_text(encoding="utf-8"))
    report = next(item for item in manifest["artifacts"] if item["role"] == "REPORT")
    report_path = tampered / report["relative_path"]
    report_path.write_bytes(report_path.read_bytes() + b" ")
    with pytest.raises(ValidatedPackageError):
        verify_validated_package(tampered)

    unexpected = tmp_path / "unexpected"
    shutil.copytree(original, unexpected)
    (unexpected / "unlisted.txt").write_text("not allowed", encoding="utf-8")
    with pytest.raises(ValidatedPackageError, match="file_set_mismatch"):
        verify_validated_package(unexpected)


def test_public_cli_build_verify_and_reproduce_package(tmp_path: Path) -> None:
    request, _ = _build_request()
    source_root = tmp_path / "sources"
    source_root.mkdir()
    descriptor_rows = []
    for index, artifact in enumerate(request.artifacts):
        source = (source_root / f"{index:02d}.json").resolve()
        source.write_bytes(artifact.payload)
        descriptor_rows.append(
            {
                "logical_id": artifact.logical_id,
                "version": artifact.version,
                "role": artifact.role.value,
                "path": str(source),
                "media_type": artifact.media_type,
                "quality_flags": list(artifact.quality_flags),
            }
        )
    descriptor = (tmp_path / "descriptor.json").resolve()
    descriptor.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "package_id": request.package_id,
                "package_version": request.package_version,
                "seed": request.seed,
                "artifacts": descriptor_rows,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    settings = ResearchSettings(
        data_root=(tmp_path / "data").resolve(),
        artifact_root=(tmp_path / "artifacts").resolve(),
        report_root=(tmp_path / "reports").resolve(),
        cache_root=(tmp_path / "cache").resolve(),
        db_path=None,
        max_workers=1,
        random_seed=0,
    )
    context = ResearchAppContext(
        settings=settings,
        paths=ResearchPathManager.from_settings(
            settings,
            project_root=PROJECT_ROOT,
        ),
        printer=lambda _message: None,
    )
    package = (tmp_path / "cli-package").resolve()
    verification = (tmp_path / "verification.json").resolve()
    reproduction = (tmp_path / "reproduction.json").resolve()
    registry = command_registry()

    assert (
        registry["research-multi-asset-build-package"].handler(
            argparse.Namespace(descriptor=str(descriptor), out=str(package)),
            context,
        )
        == 0
    )
    assert (
        registry["research-multi-asset-verify-package"].handler(
            argparse.Namespace(package=str(package), out=str(verification)),
            context,
        )
        == 0
    )
    assert (
        registry["research-multi-asset-reproduce-package"].handler(
            argparse.Namespace(package=str(package), out=str(reproduction)),
            context,
        )
        == 0
    )
    assert json.loads(verification.read_text(encoding="utf-8"))["status"] == "PASS"
    assert json.loads(reproduction.read_text(encoding="utf-8"))["status"] == "PASS"


def test_build_rejects_graph_quality_flag_self_certification() -> None:
    request, _ = _build_request()
    graph_index = next(
        index
        for index, item in enumerate(request.artifacts)
        if item.role is PackageArtifactRole.EVIDENCE_GRAPH
    )
    graph_artifact = request.artifacts[graph_index]
    forged_graph = PortableSourceArtifact(
        logical_id=graph_artifact.logical_id,
        version=graph_artifact.version,
        role=graph_artifact.role,
        media_type=graph_artifact.media_type,
        payload=graph_artifact.payload,
        quality_flags=(),
    )
    artifacts = list(request.artifacts)
    artifacts[graph_index] = forged_graph

    with pytest.raises(ValidatedPackageError, match="graph_quality_flags_mismatch"):
        PortablePackageBuildRequest(
            package_id=request.package_id,
            package_version=request.package_version,
            seed=request.seed,
            artifacts=tuple(artifacts),
        )
