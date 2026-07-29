"""Bridge one public multi-asset execution into the portable package authority.

The portable package builder deliberately remains generic.  This module gives
the supported public execution path a closed, typed mapping from its actual
request, immutable inputs, normalized evidence, derived receipts, and
accounting output into that builder.  The resulting evidence graph contains
every traceable package artifact exactly once and terminates at the accounting
receipt.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping, Sequence

from market_research.research.multi_asset.cards import DataCard, ModelCard
from market_research.research.multi_asset.evidence_graph import (
    EvidenceEdge,
    EvidenceGraph,
    EvidenceNode,
    EvidenceNodeKind,
    EvidenceRelation,
)
from market_research.research.multi_asset.validated_package import (
    PackageArtifactRole,
    PortablePackageBuildRequest,
    PortableSourceArtifact,
)


@dataclass(frozen=True, slots=True)
class PublicPackageMaterials:
    """Actual public-run objects required for an independently replayable package."""

    package_id: str
    package_version: str
    seed: int
    request: Mapping[str, object]
    specification: Mapping[str, object]
    immutable_inputs: Mapping[str, object]
    data_card: DataCard
    model_card: ModelCard
    policy: Mapping[str, object]
    configuration: Mapping[str, object]
    dependency_identity: Mapping[str, object]
    runtime_identity: Mapping[str, object]
    normalized_evidence: Mapping[str, object]
    derived_evidence: Mapping[str, object]
    accounting: Mapping[str, object]
    evidence_quality_flags: tuple[str, ...]
    additional_model_cards: tuple[ModelCard, ...] = ()
    engine_source_archive: bytes | None = None
    engine_replay_descriptor: Mapping[str, object] | None = None
    expected_study: Mapping[str, object] | None = None
    expected_report: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "request",
            "specification",
            "immutable_inputs",
            "policy",
            "configuration",
            "dependency_identity",
            "runtime_identity",
            "normalized_evidence",
            "derived_evidence",
            "accounting",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, Mapping) or not value:
                raise ValueError(f"public_package.{field_name}_required")
        if not isinstance(self.data_card, DataCard):
            raise ValueError("public_package.data_card_required")
        if not isinstance(self.model_card, ModelCard):
            raise ValueError("public_package.model_card_required")
        additional_cards = tuple(self.additional_model_cards)
        if any(not isinstance(item, ModelCard) for item in additional_cards):
            raise ValueError("public_package.additional_model_cards_invalid")
        additional_ids = tuple(item.card_id for item in additional_cards)
        if additional_ids != tuple(sorted(set(additional_ids))):
            raise ValueError(
                "public_package.additional_model_cards_must_be_sorted_unique"
            )
        if self.model_card.card_id in set(additional_ids):
            raise ValueError("public_package.model_card_duplicate")
        object.__setattr__(self, "additional_model_cards", additional_cards)
        if not self.evidence_quality_flags or self.evidence_quality_flags != tuple(
            sorted(set(self.evidence_quality_flags))
        ):
            raise ValueError(
                "public_package.evidence_quality_flags_must_be_sorted_unique"
            )
        executable_values = (
            self.engine_source_archive,
            self.engine_replay_descriptor,
            self.expected_study,
            self.expected_report,
        )
        if any(item is None for item in executable_values) and any(
            item is not None for item in executable_values
        ):
            raise ValueError("public_package.executable_replay_fields_incomplete")
        if self.engine_source_archive is not None and not self.engine_source_archive:
            raise ValueError("public_package.engine_source_archive_required")
        for field_name in (
            "engine_replay_descriptor",
            "expected_study",
            "expected_report",
        ):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, Mapping) or not value):
                raise ValueError(f"public_package.{field_name}_required")


def _json_artifact(
    *,
    logical_id: str,
    version: str,
    role: PackageArtifactRole,
    payload: object,
    quality_flags: tuple[str, ...] = (),
) -> PortableSourceArtifact:
    return PortableSourceArtifact.from_json(
        logical_id=logical_id,
        version=version,
        role=role,
        payload=payload,
        quality_flags=quality_flags,
    )


def _node(
    artifact: PortableSourceArtifact,
    *,
    kind: EvidenceNodeKind,
    parents: tuple[str, ...] = (),
    label: str | None = None,
    attributes: Sequence[tuple[str, str]] = (),
) -> EvidenceNode:
    return EvidenceNode(
        node_id=artifact.logical_id,
        version=artifact.version,
        kind=kind,
        label=label or f"{artifact.role.value} {artifact.logical_id}",
        content_hash=artifact.content_hash,
        parent_ids=parents,
        quality_flags=artifact.quality_flags,
        attributes=tuple(
            sorted(
                (
                    ("package_role", artifact.role.value),
                    *attributes,
                )
            )
        ),
    )


def _edge(
    nodes: Mapping[str, EvidenceNode],
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


@dataclass(frozen=True, slots=True)
class _ComponentArtifact:
    artifact: PortableSourceArtifact
    aggregate_artifact_id: str
    component_kind: str
    json_pointer: str
    source_lineage_reference: tuple[str, str] | None = None


def _json_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _json_pointer(parent: str, token: str) -> str:
    encoded = _json_pointer_token(token)
    return f"{parent}/{encoded}" if parent else f"/{encoded}"


def _canonical_component_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("public_package.component_not_canonical_json") from exc


def _component_artifact(
    *,
    id_prefix: str,
    version: str,
    role: PackageArtifactRole,
    aggregate_artifact_id: str,
    component_kind: str,
    json_pointer: str,
    value: object,
    quality_flags: tuple[str, ...],
    source_lineage_reference: tuple[str, str] | None = None,
) -> _ComponentArtifact:
    payload = {
        "schema_version": 1,
        "component_kind": component_kind,
        "aggregate_artifact_id": aggregate_artifact_id,
        "json_pointer": json_pointer,
        "value": value,
    }
    digest = hashlib.sha256(_canonical_component_bytes(payload)).hexdigest()
    artifact = _json_artifact(
        logical_id=f"{id_prefix}:{digest}",
        version=version,
        role=role,
        payload=payload,
        quality_flags=quality_flags,
    )
    return _ComponentArtifact(
        artifact=artifact,
        aggregate_artifact_id=aggregate_artifact_id,
        component_kind=component_kind,
        json_pointer=json_pointer,
        source_lineage_reference=source_lineage_reference,
    )


def _source_row_components(
    payload: Mapping[str, object],
    *,
    version: str,
    aggregate_artifact_id: str,
    quality_flags: tuple[str, ...],
) -> tuple[_ComponentArtifact, ...]:
    raw_rows = payload.get("source_rows", payload.get("resolver_rows"))
    if not isinstance(raw_rows, (list, tuple)) or not raw_rows:
        raise ValueError("public_package.source_rows_required")
    pointer_root = "/source_rows" if "source_rows" in payload else "/resolver_rows"
    components = []
    for index, row in enumerate(raw_rows):
        if not isinstance(row, Mapping) or not row:
            raise ValueError("public_package.source_row_object_required")
        row_id = row.get("row_id")
        input_path = row.get("input_path", "")
        source_lineage_reference = (
            (row_id, input_path if isinstance(input_path, str) else "")
            if isinstance(row_id, str)
            else None
        )
        components.append(
            _component_artifact(
                id_prefix="source-row",
                version=version,
                role=PackageArtifactRole.IMMUTABLE_INPUT,
                aggregate_artifact_id=aggregate_artifact_id,
                component_kind="SOURCE_ROW",
                json_pointer=f"{pointer_root}/{index}",
                value=dict(row),
                quality_flags=quality_flags,
                source_lineage_reference=source_lineage_reference,
            )
        )
    return tuple(components)


def _record_values(
    value: object,
    *,
    pointer: str = "",
) -> tuple[tuple[str, object], ...]:
    rows: list[tuple[str, object]] = []
    if isinstance(value, Mapping):
        for key in sorted(value):
            if not isinstance(key, str):
                raise ValueError("public_package.component_key_invalid")
            if key == "schema_version":
                continue
            item = value[key]
            child_pointer = _json_pointer(pointer, key)
            if isinstance(item, Mapping):
                rows.extend(_record_values(item, pointer=child_pointer))
            elif isinstance(item, (list, tuple)):
                rows.extend(
                    (f"{child_pointer}/{index}", child)
                    for index, child in enumerate(item)
                )
            else:
                rows.append((child_pointer, item))
    elif isinstance(value, (list, tuple)):
        rows.extend((f"{pointer}/{index}", item) for index, item in enumerate(value))
    else:
        rows.append((pointer or "/", value))
    return tuple(rows)


def _normalized_components(
    payload: Mapping[str, object],
    *,
    version: str,
    aggregate_artifact_id: str,
    quality_flags: tuple[str, ...],
) -> tuple[_ComponentArtifact, ...]:
    values = _record_values(payload)
    if not values:
        values = (("/", dict(payload)),)
    components = []
    for pointer, value in values:
        source_row_id = (
            value.get("source_row_id") if isinstance(value, Mapping) else None
        )
        input_path = value.get("input_path", "") if isinstance(value, Mapping) else ""
        source_lineage_reference = (
            (
                source_row_id,
                input_path if isinstance(input_path, str) else "",
            )
            if isinstance(source_row_id, str)
            else None
        )
        components.append(
            _component_artifact(
                id_prefix="normalized-record",
                version=version,
                role=PackageArtifactRole.NORMALIZED_EVIDENCE,
                aggregate_artifact_id=aggregate_artifact_id,
                component_kind="NORMALIZED_RECORD",
                json_pointer=pointer,
                value=value,
                quality_flags=quality_flags,
                source_lineage_reference=source_lineage_reference,
            )
        )
    return tuple(components)


def _profile_output_values(
    derived_payload: Mapping[str, object],
    accounting_payload: Mapping[str, object],
) -> tuple[tuple[str, object, str], ...]:
    values: list[tuple[str, object, str]] = []
    for key in sorted(derived_payload):
        if not isinstance(key, str):
            raise ValueError("public_package.derived_component_key_invalid")
        lowered = key.lower()
        if key in {
            "authoritative_input_receipt",
            "content_hash",
            "experiment_spec_hash",
            "output_bindings",
            "schema_version",
        }:
            continue
        if any(
            token in lowered
            for token in ("profile", "model", "analysis", "output", "result", "receipt")
        ):
            values.append(
                (
                    _json_pointer("", key),
                    derived_payload[key],
                    "derived:public",
                )
            )
    raw_scenarios = accounting_payload.get("scenario_evidence")
    if isinstance(raw_scenarios, (list, tuple)):
        values.extend(
            (
                f"/scenario_evidence/{index}",
                scenario,
                "accounting:public",
            )
            for index, scenario in enumerate(raw_scenarios)
        )
    if values:
        return tuple(values)
    fallback = tuple(
        (
            _json_pointer("", key),
            derived_payload[key],
            "derived:public",
        )
        for key in sorted(derived_payload)
        if key not in {"content_hash", "schema_version"}
    )
    return fallback or (("/", dict(derived_payload), "derived:public"),)


def _profile_output_components(
    derived_payload: Mapping[str, object],
    accounting_payload: Mapping[str, object],
    *,
    version: str,
    quality_flags: tuple[str, ...],
) -> tuple[_ComponentArtifact, ...]:
    return tuple(
        _component_artifact(
            id_prefix="profile-output",
            version=version,
            role=PackageArtifactRole.DERIVED_EVIDENCE,
            aggregate_artifact_id=aggregate_artifact_id,
            component_kind="MODEL_OR_PROFILE_OUTPUT",
            json_pointer=pointer,
            value=value,
            quality_flags=quality_flags,
        )
        for pointer, value, aggregate_artifact_id in _profile_output_values(
            derived_payload,
            accounting_payload,
        )
    )


def _scalar_leaves(
    value: object,
    *,
    pointer: str,
) -> tuple[tuple[str, object], ...]:
    if isinstance(value, Mapping):
        rows: list[tuple[str, object]] = []
        for key in sorted(value):
            if not isinstance(key, str):
                raise ValueError("public_package.report_field_key_invalid")
            if key == "schema_version":
                continue
            rows.extend(
                _scalar_leaves(
                    value[key],
                    pointer=_json_pointer(pointer, key),
                )
            )
        return tuple(rows)
    if isinstance(value, (list, tuple)):
        return tuple(
            row
            for index, item in enumerate(value)
            for row in _scalar_leaves(item, pointer=f"{pointer}/{index}")
        )
    return ((pointer or "/", value),)


def _report_field_values(
    accounting_payload: Mapping[str, object],
) -> tuple[tuple[str, object], ...]:
    evidence = accounting_payload.get("accounting_evidence")
    if isinstance(evidence, Mapping):
        report = evidence.get("report")
        if isinstance(report, Mapping) and report:
            values = list(
                _scalar_leaves(
                    report,
                    pointer="/accounting_evidence/report",
                )
            )
            if "study_content_hash" in accounting_payload:
                values.append(
                    (
                        "/study_content_hash",
                        accounting_payload["study_content_hash"],
                    )
                )
            return tuple(values)
    fallback_values = _scalar_leaves(accounting_payload, pointer="")
    return fallback_values or (("/", dict(accounting_payload)),)


def _report_field_components(
    accounting_payload: Mapping[str, object],
    *,
    version: str,
    quality_flags: tuple[str, ...],
) -> tuple[_ComponentArtifact, ...]:
    return tuple(
        _component_artifact(
            id_prefix="report-field",
            version=version,
            role=PackageArtifactRole.DERIVED_EVIDENCE,
            aggregate_artifact_id="accounting:public",
            component_kind="REPORT_FIELD",
            json_pointer=pointer,
            value=value,
            quality_flags=quality_flags,
        )
        for pointer, value in _report_field_values(accounting_payload)
    )


def _component_node(
    component: _ComponentArtifact,
    *,
    kind: EvidenceNodeKind,
    parents: tuple[str, ...],
) -> EvidenceNode:
    return _node(
        component.artifact,
        kind=kind,
        parents=parents,
        label=f"{component.component_kind} {component.json_pointer}",
        attributes=(
            ("aggregate_artifact_id", component.aggregate_artifact_id),
            ("component_kind", component.component_kind),
            ("json_pointer", component.json_pointer),
        ),
    )


def build_public_validated_package_request(
    materials: PublicPackageMaterials,
) -> PortablePackageBuildRequest:
    """Map one real public execution into the canonical portable package schema."""

    if not isinstance(materials, PublicPackageMaterials):
        raise ValueError("public_package.materials_required")
    version = materials.package_version
    evidence_flags = materials.evidence_quality_flags
    request = _json_artifact(
        logical_id="request:public",
        version=version,
        role=PackageArtifactRole.REQUEST,
        payload=materials.request,
    )
    specification = _json_artifact(
        logical_id="spec:public",
        version=version,
        role=PackageArtifactRole.SPEC,
        payload=materials.specification,
    )
    immutable_inputs = _json_artifact(
        logical_id="input:public",
        version=version,
        role=PackageArtifactRole.IMMUTABLE_INPUT,
        payload=materials.immutable_inputs,
        quality_flags=evidence_flags,
    )
    data_card = _json_artifact(
        logical_id=materials.data_card.card_id,
        version=materials.data_card.version,
        role=PackageArtifactRole.DATA_CARD,
        payload=materials.data_card.as_dict(),
        quality_flags=materials.data_card.quality_flags,
    )
    model_cards = tuple(
        _json_artifact(
            logical_id=item.card_id,
            version=item.version,
            role=PackageArtifactRole.MODEL_CARD,
            payload=item.as_dict(),
            quality_flags=item.quality_flags,
        )
        for item in (
            materials.model_card,
            *materials.additional_model_cards,
        )
    )
    model_card_ids = tuple(item.logical_id for item in model_cards)
    policy = _json_artifact(
        logical_id="policy:public",
        version=version,
        role=PackageArtifactRole.POLICY,
        payload=materials.policy,
    )
    configuration = _json_artifact(
        logical_id="configuration:public",
        version=version,
        role=PackageArtifactRole.CONFIGURATION,
        payload=materials.configuration,
    )
    dependency_identity = _json_artifact(
        logical_id="identity:dependencies",
        version=version,
        role=PackageArtifactRole.DEPENDENCY_IDENTITY,
        payload=materials.dependency_identity,
    )
    runtime_identity = _json_artifact(
        logical_id="identity:runtime",
        version=version,
        role=PackageArtifactRole.RUNTIME_IDENTITY,
        payload=materials.runtime_identity,
    )
    normalized = _json_artifact(
        logical_id="normalized:public",
        version=version,
        role=PackageArtifactRole.NORMALIZED_EVIDENCE,
        payload=materials.normalized_evidence,
        quality_flags=evidence_flags,
    )
    derived = _json_artifact(
        logical_id="derived:public",
        version=version,
        role=PackageArtifactRole.DERIVED_EVIDENCE,
        payload=materials.derived_evidence,
        quality_flags=evidence_flags,
    )
    accounting = _json_artifact(
        logical_id="accounting:public",
        version=version,
        role=PackageArtifactRole.ACCOUNTING,
        payload=materials.accounting,
        quality_flags=evidence_flags,
    )
    engine_source = (
        PortableSourceArtifact(
            logical_id="source:research-engine",
            version=version,
            role=PackageArtifactRole.ENGINE_SOURCE,
            media_type="application/zip",
            payload=materials.engine_source_archive,
            quality_flags=evidence_flags,
        )
        if materials.engine_source_archive is not None
        else None
    )
    engine_replay = (
        _json_artifact(
            logical_id="replay:research-engine",
            version=version,
            role=PackageArtifactRole.ENGINE_REPLAY_DESCRIPTOR,
            payload=materials.engine_replay_descriptor,
            quality_flags=evidence_flags,
        )
        if materials.engine_replay_descriptor is not None
        else None
    )
    expected_study = (
        _json_artifact(
            logical_id="study:portable",
            version=version,
            role=PackageArtifactRole.STUDY,
            payload=materials.expected_study,
            quality_flags=evidence_flags,
        )
        if materials.expected_study is not None
        else None
    )
    expected_report = (
        _json_artifact(
            logical_id="report:portable",
            version=version,
            role=PackageArtifactRole.REPORT,
            payload=materials.expected_report,
            quality_flags=evidence_flags,
        )
        if materials.expected_report is not None
        else None
    )

    source_rows = _source_row_components(
        materials.immutable_inputs,
        version=version,
        aggregate_artifact_id=immutable_inputs.logical_id,
        quality_flags=evidence_flags,
    )
    normalized_records = _normalized_components(
        materials.normalized_evidence,
        version=version,
        aggregate_artifact_id=normalized.logical_id,
        quality_flags=evidence_flags,
    )
    profile_outputs = _profile_output_components(
        materials.derived_evidence,
        materials.accounting,
        version=version,
        quality_flags=evidence_flags,
    )
    report_fields = _report_field_components(
        materials.accounting,
        version=version,
        quality_flags=evidence_flags,
    )

    source_row_ids = tuple(item.artifact.logical_id for item in source_rows)
    source_row_ids_by_reference: dict[tuple[str, str], list[str]] = {}
    source_row_ids_by_row_id: dict[str, list[str]] = {}
    for component in source_rows:
        reference = component.source_lineage_reference
        if reference is None:
            continue
        source_row_ids_by_reference.setdefault(reference, []).append(
            component.artifact.logical_id
        )
        source_row_ids_by_row_id.setdefault(reference[0], []).append(
            component.artifact.logical_id
        )
    normalized_record_ids = tuple(
        item.artifact.logical_id for item in normalized_records
    )
    profile_output_ids = tuple(item.artifact.logical_id for item in profile_outputs)
    has_structured_normalization_lineage = any(
        item.source_lineage_reference is not None for item in normalized_records
    )
    normalization_sources: dict[str, tuple[str, ...]] = {}
    for component in normalized_records:
        reference = component.source_lineage_reference
        if reference is not None:
            matched_ids = source_row_ids_by_reference.get(reference)
            if not matched_ids:
                matched_ids = source_row_ids_by_row_id.get(reference[0])
            if not matched_ids:
                raise ValueError(
                    "public_package.normalized_source_row_reference_missing"
                )
            source_ids = tuple(sorted(set(matched_ids)))
        elif has_structured_normalization_lineage:
            source_ids = (immutable_inputs.logical_id,)
        else:
            source_ids = source_row_ids
        normalization_sources[component.artifact.logical_id] = source_ids
    normalized_parents = tuple(
        sorted(
            (
                immutable_inputs.logical_id,
                data_card.logical_id,
                *normalized_record_ids,
            )
        )
    )
    profile_output_parents = tuple(
        sorted(
            (
                request.logical_id,
                specification.logical_id,
                *model_card_ids,
                policy.logical_id,
                configuration.logical_id,
                normalized.logical_id,
                *((engine_source.logical_id,) if engine_source is not None else ()),
                *((engine_replay.logical_id,) if engine_replay is not None else ()),
            )
        )
    )
    node_values = [
        _node(request, kind=EvidenceNodeKind.CONFIGURATION),
        _node(specification, kind=EvidenceNodeKind.CONFIGURATION),
        _node(immutable_inputs, kind=EvidenceNodeKind.IMMUTABLE_INPUT),
        _node(data_card, kind=EvidenceNodeKind.DATA_CARD),
        *(_node(item, kind=EvidenceNodeKind.MODEL_CARD) for item in model_cards),
        _node(policy, kind=EvidenceNodeKind.POLICY),
        _node(configuration, kind=EvidenceNodeKind.CONFIGURATION),
        _node(
            normalized,
            kind=EvidenceNodeKind.NORMALIZED,
            parents=normalized_parents,
        ),
        _node(
            derived,
            kind=EvidenceNodeKind.DERIVED,
            parents=tuple(sorted(profile_output_ids)),
        ),
        _node(
            accounting,
            kind=EvidenceNodeKind.ACCOUNTING,
            parents=(derived.logical_id,),
        ),
    ]
    if engine_source is not None and engine_replay is not None:
        node_values.extend(
            (
                _node(engine_source, kind=EvidenceNodeKind.CODE),
                _node(
                    engine_replay,
                    kind=EvidenceNodeKind.CONFIGURATION,
                ),
            )
        )
    node_values.extend(
        _component_node(
            component,
            kind=EvidenceNodeKind.SOURCE_ROW,
            parents=(),
        )
        for component in source_rows
    )
    node_values.extend(
        _component_node(
            component,
            kind=EvidenceNodeKind.NORMALIZED,
            parents=tuple(
                sorted(
                    (
                        *normalization_sources[component.artifact.logical_id],
                        data_card.logical_id,
                    )
                )
            ),
        )
        for component in normalized_records
    )
    node_values.extend(
        _component_node(
            component,
            kind=EvidenceNodeKind.ANALYSIS,
            parents=profile_output_parents,
        )
        for component in profile_outputs
    )
    node_values.extend(
        _component_node(
            component,
            kind=EvidenceNodeKind.REPORT_CLAIM,
            parents=(accounting.logical_id,),
        )
        for component in report_fields
    )
    nodes = {item.node_id: item for item in node_values}
    edges: list[EvidenceEdge] = []
    for component in normalized_records:
        target_id = component.artifact.logical_id
        edges.extend(
            _edge(
                nodes,
                source_id,
                target_id,
                EvidenceRelation.NORMALIZES,
            )
            for source_id in normalization_sources[target_id]
        )
        edges.append(
            _edge(
                nodes,
                data_card.logical_id,
                target_id,
                EvidenceRelation.DESCRIBES,
            )
        )
        edges.append(
            _edge(
                nodes,
                target_id,
                normalized.logical_id,
                EvidenceRelation.SUPPORTS,
            )
        )
    edges.extend(
        (
            _edge(
                nodes,
                immutable_inputs.logical_id,
                normalized.logical_id,
                EvidenceRelation.NORMALIZES,
            ),
            _edge(
                nodes,
                data_card.logical_id,
                normalized.logical_id,
                EvidenceRelation.DESCRIBES,
            ),
        )
    )
    for component in profile_outputs:
        target_id = component.artifact.logical_id
        for parent_id in profile_output_parents:
            if parent_id in set(model_card_ids):
                relation = EvidenceRelation.CALCULATES
            elif engine_source is not None and parent_id == engine_source.logical_id:
                relation = EvidenceRelation.CALCULATES
            elif parent_id == normalized.logical_id:
                relation = EvidenceRelation.DERIVES
            else:
                relation = EvidenceRelation.CONFIGURES
            edges.append(_edge(nodes, parent_id, target_id, relation))
        edges.append(
            _edge(
                nodes,
                target_id,
                derived.logical_id,
                EvidenceRelation.SUPPORTS,
            )
        )
    edges.append(
        _edge(
            nodes,
            derived.logical_id,
            accounting.logical_id,
            EvidenceRelation.RECONCILES,
        )
    )
    edges.extend(
        _edge(
            nodes,
            accounting.logical_id,
            component.artifact.logical_id,
            EvidenceRelation.REPORTS,
        )
        for component in report_fields
    )
    graph = EvidenceGraph(
        graph_id="graph:public",
        version=version,
        nodes=tuple(sorted(nodes.values(), key=lambda item: item.node_id)),
        edges=tuple(sorted(edges, key=lambda item: item.identity)),
        terminal_ids=tuple(sorted(item.artifact.logical_id for item in report_fields)),
    )
    graph_flags = tuple(
        sorted({flag for item in graph.nodes for flag in item.quality_flags})
    )
    graph_artifact = _json_artifact(
        logical_id=graph.graph_id,
        version=graph.version,
        role=PackageArtifactRole.EVIDENCE_GRAPH,
        payload=graph.as_dict(),
        quality_flags=graph_flags,
    )
    artifacts = (
        request,
        specification,
        immutable_inputs,
        data_card,
        *model_cards,
        policy,
        configuration,
        dependency_identity,
        runtime_identity,
        normalized,
        derived,
        accounting,
        *((engine_source,) if engine_source is not None else ()),
        *((engine_replay,) if engine_replay is not None else ()),
        *(item.artifact for item in source_rows),
        *(item.artifact for item in normalized_records),
        *(item.artifact for item in profile_outputs),
        *(item.artifact for item in report_fields),
        graph_artifact,
    )
    return PortablePackageBuildRequest(
        package_id=materials.package_id,
        package_version=materials.package_version,
        seed=materials.seed,
        artifacts=tuple(
            sorted(
                artifacts,
                key=lambda item: (
                    item.role.value,
                    item.logical_id,
                    item.version,
                ),
            )
        ),
        expected_study=expected_study,
        expected_report=expected_report,
    )


__all__ = [
    "PublicPackageMaterials",
    "build_public_validated_package_request",
]
