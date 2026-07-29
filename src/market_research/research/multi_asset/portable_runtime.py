# mypy: ignore-errors
"""Standard-library-only verifier and deterministic replay runtime.

This file is copied verbatim into every portable multi-asset package.  It must
not import any project module or third-party dependency.

The verifier intentionally avoids project typing imports so the copied file is
fully standalone.  Its dynamic JSON validation is covered by cold-root tests.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections import deque
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath


SCHEMA_VERSION = 2
REPLAY_ALGORITHM_VERSION = "multi-asset-portable-replay-v2"
MAX_COMPONENT_BYTES = 64 * 1024 * 1024

HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
QUALITY_FLAG = re.compile(r"^[A-Z][A-Z0-9_:-]{0,127}$")

ROLES = frozenset(
    {
        "REQUEST",
        "SPEC",
        "IMMUTABLE_INPUT",
        "DATA_CARD",
        "MODEL_CARD",
        "POLICY",
        "CONFIGURATION",
        "SOURCE_IDENTITY",
        "DEPENDENCY_IDENTITY",
        "RUNTIME_IDENTITY",
        "ENGINE_SOURCE",
        "ENGINE_REPLAY_DESCRIPTOR",
        "NORMALIZED_EVIDENCE",
        "DERIVED_EVIDENCE",
        "ACCOUNTING",
        "EVIDENCE_GRAPH",
        "QUALITY_FLAGS",
        "CHECKSUMS",
        "STUDY",
        "REPORT",
    }
)
OPTIONAL_EXECUTABLE_ROLES = frozenset({"ENGINE_SOURCE", "ENGINE_REPLAY_DESCRIPTOR"})
SINGLETON_ROLES = frozenset(
    {
        "REQUEST",
        "SPEC",
        "POLICY",
        "CONFIGURATION",
        "SOURCE_IDENTITY",
        "DEPENDENCY_IDENTITY",
        "RUNTIME_IDENTITY",
        "ACCOUNTING",
        "EVIDENCE_GRAPH",
        "QUALITY_FLAGS",
        "CHECKSUMS",
        "STUDY",
        "REPORT",
    }
)
MULTI_ROLES = frozenset(
    {
        "IMMUTABLE_INPUT",
        "DATA_CARD",
        "MODEL_CARD",
        "NORMALIZED_EVIDENCE",
        "DERIVED_EVIDENCE",
    }
)
SUPPORT_PATHS = frozenset(
    {
        "INSTRUCTIONS.txt",
        "portable_runtime.py",
        "reproduce.py",
        "verify.py",
    }
)
GRAPH_ROLE_KINDS = {
    "REQUEST": frozenset({"CONFIGURATION"}),
    "SPEC": frozenset({"CONFIGURATION"}),
    "IMMUTABLE_INPUT": frozenset({"SOURCE_ROW", "IMMUTABLE_INPUT"}),
    "DATA_CARD": frozenset({"DATA_CARD"}),
    "MODEL_CARD": frozenset({"MODEL_CARD"}),
    "POLICY": frozenset({"POLICY"}),
    "CONFIGURATION": frozenset({"CONFIGURATION"}),
    "ENGINE_SOURCE": frozenset({"CODE"}),
    "ENGINE_REPLAY_DESCRIPTOR": frozenset({"CONFIGURATION"}),
    "NORMALIZED_EVIDENCE": frozenset({"NORMALIZED"}),
    "DERIVED_EVIDENCE": frozenset({"DERIVED", "ANALYSIS", "REPORT_CLAIM"}),
    "ACCOUNTING": frozenset({"ACCOUNTING"}),
}
NODE_KINDS = frozenset(
    {
        "SOURCE_ROW",
        "IMMUTABLE_INPUT",
        "DATA_CARD",
        "MODEL_CARD",
        "POLICY",
        "CONFIGURATION",
        "CODE",
        "NORMALIZED",
        "DERIVED",
        "ANALYSIS",
        "ACCOUNTING",
        "REPORT_CLAIM",
    }
)
ROOT_NODE_KINDS = frozenset(
    {
        "SOURCE_ROW",
        "IMMUTABLE_INPUT",
        "DATA_CARD",
        "MODEL_CARD",
        "POLICY",
        "CONFIGURATION",
        "CODE",
    }
)
RELATIONS = frozenset(
    {
        "DESCRIBES",
        "NORMALIZES",
        "DERIVES",
        "CONFIGURES",
        "CALCULATES",
        "RECONCILES",
        "SUPPORTS",
        "REPORTS",
    }
)
RELATION_KIND_SIGNATURES = {
    "DESCRIBES": frozenset(
        {
            ("DATA_CARD", "SOURCE_ROW"),
            ("DATA_CARD", "NORMALIZED"),
        }
    ),
    "NORMALIZES": frozenset(
        {
            ("SOURCE_ROW", "NORMALIZED"),
            ("IMMUTABLE_INPUT", "NORMALIZED"),
        }
    ),
    "DERIVES": frozenset(
        {
            ("NORMALIZED", "ANALYSIS"),
            ("ANALYSIS", "DERIVED"),
            ("DERIVED", "DERIVED"),
        }
    ),
    "CONFIGURES": frozenset(
        {
            ("CONFIGURATION", "ANALYSIS"),
            ("POLICY", "ANALYSIS"),
        }
    ),
    "CALCULATES": frozenset(
        {
            ("CODE", "ANALYSIS"),
            ("MODEL_CARD", "ANALYSIS"),
        }
    ),
    "RECONCILES": frozenset(
        {
            ("ANALYSIS", "ACCOUNTING"),
            ("DERIVED", "ACCOUNTING"),
        }
    ),
    "SUPPORTS": frozenset(
        {
            ("NORMALIZED", "NORMALIZED"),
            ("ANALYSIS", "DERIVED"),
        }
    ),
    "REPORTS": frozenset(
        {
            ("ACCOUNTING", "REPORT_CLAIM"),
            ("ANALYSIS", "REPORT_CLAIM"),
            ("DERIVED", "REPORT_CLAIM"),
        }
    ),
}
COMPONENT_KIND_NODE_KINDS = {
    "SOURCE_ROW": "SOURCE_ROW",
    "NORMALIZED_RECORD": "NORMALIZED",
    "MODEL_OR_PROFILE_OUTPUT": "ANALYSIS",
    "REPORT_FIELD": "REPORT_CLAIM",
}


class PortableRuntimeError(ValueError):
    pass


def canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def json_file_bytes(value):
    return canonical_bytes(value) + b"\n"


def hash_payload(value):
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def hash_bytes(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PortableRuntimeError("duplicate_json_key:" + key)
        result[key] = value
    return result


def reject_constant(value):
    raise PortableRuntimeError("nonfinite_json_constant:" + value)


def load_json_bytes(raw, label):
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableRuntimeError(label + "_invalid_json") from exc


def require_object(value, label):
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PortableRuntimeError(label + "_object_required")
    return value


def exact_fields(value, expected, label):
    if set(value) != set(expected):
        raise PortableRuntimeError(label + "_fields_invalid")


def require_id(value, label):
    if not isinstance(value, str) or not STABLE_ID.fullmatch(value):
        raise PortableRuntimeError(label + "_invalid")
    return value


def require_text(value, label):
    if not isinstance(value, str) or not value or value.strip() != value:
        raise PortableRuntimeError(label + "_invalid")
    return value


def require_hash(value, label):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise PortableRuntimeError(label + "_invalid")
    return value


def require_timestamp(value, label):
    require_text(value, label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PortableRuntimeError(label + "_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PortableRuntimeError(label + "_timezone_required")
    canonical = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if value != canonical:
        raise PortableRuntimeError(label + "_not_canonical")
    return canonical


def require_sorted_strings(
    value,
    label,
    *,
    allow_empty=False,
    pattern=None,
):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PortableRuntimeError(label + "_array_required")
    if not allow_empty and not value:
        raise PortableRuntimeError(label + "_required")
    if value != sorted(set(value)):
        raise PortableRuntimeError(label + "_must_be_sorted_unique")
    if any(
        not item
        or item.strip() != item
        or (pattern is not None and not pattern.fullmatch(item))
        for item in value
    ):
        raise PortableRuntimeError(label + "_invalid")
    return value


def safe_relative(value, label):
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or value.startswith("/")
    ):
        raise PortableRuntimeError(label + "_invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise PortableRuntimeError(label + "_invalid")
    return path


def artifact_path_for(content_hash):
    return "objects/sha256/" + require_hash(
        content_hash, "artifact.content_hash"
    ).removeprefix("sha256:")


def validate_validation_result(value, label):
    value = require_object(value, label)
    exact_fields(
        value,
        {"check_id", "status", "summary", "evidence_hash", "content_hash"},
        label,
    )
    require_id(value["check_id"], label + ".check_id")
    if value["status"] not in {"PASS", "WARN", "FAIL"}:
        raise PortableRuntimeError(label + ".status_invalid")
    require_text(value["summary"], label + ".summary")
    require_hash(value["evidence_hash"], label + ".evidence_hash")
    identity = {key: item for key, item in value.items() if key != "content_hash"}
    if value["content_hash"] != hash_payload(identity):
        raise PortableRuntimeError(label + ".content_hash_mismatch")


def validate_hashed_record(value, expected, label):
    value = require_object(value, label)
    exact_fields(value, set(expected) | {"content_hash"}, label)
    return value


def validate_record_hash(value, label):
    identity = {key: item for key, item in value.items() if key != "content_hash"}
    if value["content_hash"] != hash_payload(identity):
        raise PortableRuntimeError(label + ".content_hash_mismatch")


def validate_data_field(value, label):
    value = validate_hashed_record(
        value,
        {"field_name", "data_type", "semantic_type", "nullable", "description"},
        label,
    )
    require_id(value["field_name"], label + ".field_name")
    require_text(value["data_type"], label + ".data_type")
    require_text(value["semantic_type"], label + ".semantic_type")
    if not isinstance(value["nullable"], bool):
        raise PortableRuntimeError(label + ".nullable_invalid")
    require_text(value["description"], label + ".description")
    validate_record_hash(value, label)


def validate_data_unit(value, label):
    value = validate_hashed_record(value, {"field_name", "unit"}, label)
    require_id(value["field_name"], label + ".field_name")
    require_text(value["unit"], label + ".unit")
    validate_record_hash(value, label)


def validate_data_temporal(value, label):
    value = validate_hashed_record(
        value,
        {
            "valid_time_field",
            "valid_time_definition",
            "knowledge_time_field",
            "knowledge_time_definition",
            "availability_time_field",
            "availability_time_definition",
            "timezone",
            "calendar_ids",
        },
        label,
    )
    for field_name in (
        "valid_time_field",
        "knowledge_time_field",
        "availability_time_field",
    ):
        require_id(value[field_name], label + "." + field_name)
    for field_name in (
        "valid_time_definition",
        "knowledge_time_definition",
        "availability_time_definition",
        "timezone",
    ):
        require_text(value[field_name], label + "." + field_name)
    require_sorted_strings(
        value["calendar_ids"],
        label + ".calendar_ids",
        pattern=STABLE_ID,
    )
    validate_record_hash(value, label)
    return value


def validate_data_resolver(value, label):
    value = validate_hashed_record(
        value,
        {
            "resolver_id",
            "resolver_version",
            "row_identity_fields",
            "source_artifact_hash_field",
            "source_row_hash_field",
            "resolution_policy",
        },
        label,
    )
    for field_name in (
        "resolver_id",
        "resolver_version",
        "source_artifact_hash_field",
        "source_row_hash_field",
    ):
        require_id(value[field_name], label + "." + field_name)
    require_sorted_strings(
        value["row_identity_fields"],
        label + ".row_identity_fields",
        pattern=STABLE_ID,
    )
    require_text(value["resolution_policy"], label + ".resolution_policy")
    validate_record_hash(value, label)
    return value


def validate_model_parameter(value, label):
    value = validate_hashed_record(
        value,
        {"parameter_name", "value", "data_type", "unit", "description"},
        label,
    )
    require_id(value["parameter_name"], label + ".parameter_name")
    for field_name in ("value", "data_type", "unit", "description"):
        require_text(value[field_name], label + "." + field_name)
    validate_record_hash(value, label)


def validate_sorted_records(value, label, key, validator):
    if not isinstance(value, list) or not value:
        raise PortableRuntimeError(label + "_required")
    for index, item in enumerate(value):
        validator(item, label + "." + str(index))
    keys = [item[key] for item in value]
    if keys != sorted(set(keys)):
        raise PortableRuntimeError(label + "_not_sorted_unique")
    return value


def validate_validation_results(value, label):
    return validate_sorted_records(
        value,
        label,
        "check_id",
        validate_validation_result,
    )


def validate_card(value, expected_type):
    value = require_object(value, expected_type.lower())
    if expected_type == "DATA_CARD":
        expected = {
            "schema_version",
            "card_type",
            "card_id",
            "version",
            "dataset_id",
            "dataset_version",
            "source_name",
            "source_reference",
            "license_id",
            "license_terms_hash",
            "distribution_status",
            "use_constraints",
            "snapshot_method",
            "coverage_start_at",
            "coverage_end_at",
            "coverage_markets",
            "coverage_instruments",
            "field_schema",
            "units",
            "temporal_semantics",
            "normalization_transformations",
            "known_corrections",
            "missing_data_summary",
            "missing_data_policy",
            "survivorship_policy",
            "corporate_action_policy",
            "known_biases",
            "known_limitations",
            "intended_uses",
            "prohibited_uses",
            "revision_policy",
            "source_hashes",
            "row_resolver_metadata",
            "validation_results",
            "quality_flags",
            "content_hash",
        }
        id_fields = ("card_id", "version", "dataset_id", "dataset_version")
        text_fields = (
            "source_name",
            "source_reference",
            "license_id",
            "snapshot_method",
            "coverage_start_at",
            "coverage_end_at",
            "missing_data_summary",
            "missing_data_policy",
            "survivorship_policy",
            "corporate_action_policy",
            "revision_policy",
        )
        list_fields = (
            "use_constraints",
            "coverage_markets",
            "coverage_instruments",
            "known_biases",
            "known_limitations",
            "intended_uses",
            "prohibited_uses",
        )
        hash_fields = ("license_terms_hash",)
    else:
        expected = {
            "schema_version",
            "card_type",
            "card_id",
            "version",
            "model_id",
            "model_version",
            "model_name",
            "model_family",
            "implementation_hash",
            "code_hash",
            "configuration_hash",
            "input_schema_hash",
            "output_schema_hash",
            "input_hashes",
            "output_hashes",
            "assumptions",
            "applicability_scope",
            "unsupported_cases",
            "parameters",
            "calibration_data_hashes",
            "calibration_process",
            "objective",
            "diagnostic_results",
            "convergence_criteria",
            "convergence_result",
            "failure_conditions",
            "failure_behavior",
            "validation_results",
            "benchmark_results",
            "sensitivity_results",
            "deterministic_configuration",
            "known_limitations",
            "quality_flags",
            "content_hash",
        }
        id_fields = ("card_id", "version", "model_id", "model_version")
        text_fields = (
            "model_name",
            "model_family",
            "calibration_process",
            "objective",
            "convergence_criteria",
            "failure_behavior",
        )
        list_fields = (
            "assumptions",
            "applicability_scope",
            "unsupported_cases",
            "failure_conditions",
            "known_limitations",
        )
        hash_fields = (
            "implementation_hash",
            "code_hash",
            "configuration_hash",
            "input_schema_hash",
            "output_schema_hash",
        )
    exact_fields(value, expected, expected_type.lower())
    if value["schema_version"] != 2 or value["card_type"] != expected_type:
        raise PortableRuntimeError(expected_type.lower() + "_version_or_type_invalid")
    for field_name in id_fields:
        require_id(value[field_name], expected_type.lower() + "." + field_name)
    for field_name in text_fields:
        require_text(value[field_name], expected_type.lower() + "." + field_name)
    for field_name in hash_fields:
        require_hash(value[field_name], expected_type.lower() + "." + field_name)
    for field_name in list_fields:
        require_sorted_strings(
            value[field_name],
            expected_type.lower() + "." + field_name,
        )
    if expected_type == "DATA_CARD":
        coverage_start = require_timestamp(
            value["coverage_start_at"],
            "data_card.coverage_start_at",
        )
        coverage_end = require_timestamp(
            value["coverage_end_at"],
            "data_card.coverage_end_at",
        )
        if coverage_end < coverage_start:
            raise PortableRuntimeError("data_card.coverage_time_order_invalid")
        if value["distribution_status"] not in {
            "PUBLIC",
            "REDISTRIBUTABLE",
            "LICENSE_RESTRICTED",
            "INTERNAL_ONLY",
            "NON_REDISTRIBUTABLE",
        }:
            raise PortableRuntimeError("data_card.distribution_status_invalid")
        fields = validate_sorted_records(
            value["field_schema"],
            "data_card.field_schema",
            "field_name",
            validate_data_field,
        )
        units = validate_sorted_records(
            value["units"],
            "data_card.units",
            "field_name",
            validate_data_unit,
        )
        field_names = {item["field_name"] for item in fields}
        if {item["field_name"] for item in units} != field_names:
            raise PortableRuntimeError("data_card.units_field_coverage_mismatch")
        temporal = validate_data_temporal(
            value["temporal_semantics"],
            "data_card.temporal_semantics",
        )
        if {
            temporal["valid_time_field"],
            temporal["knowledge_time_field"],
            temporal["availability_time_field"],
        } - field_names:
            raise PortableRuntimeError(
                "data_card.temporal_semantics_field_coverage_mismatch"
            )
        require_sorted_strings(
            value["normalization_transformations"],
            "data_card.normalization_transformations",
            allow_empty=True,
        )
        require_sorted_strings(
            value["known_corrections"],
            "data_card.known_corrections",
            allow_empty=True,
        )
        require_sorted_strings(
            value["source_hashes"],
            "data_card.source_hashes",
            pattern=HASH,
        )
        resolver = validate_data_resolver(
            value["row_resolver_metadata"],
            "data_card.row_resolver_metadata",
        )
        resolver_fields = {
            *resolver["row_identity_fields"],
            resolver["source_artifact_hash_field"],
            resolver["source_row_hash_field"],
        }
        if resolver_fields - field_names:
            raise PortableRuntimeError("data_card.row_resolver_field_coverage_mismatch")
        if set(value["intended_uses"]) & set(value["prohibited_uses"]):
            raise PortableRuntimeError("data_card.use_scope_overlap")
    else:
        for field_name in (
            "input_hashes",
            "output_hashes",
            "calibration_data_hashes",
        ):
            require_sorted_strings(
                value[field_name],
                "model_card." + field_name,
                pattern=HASH,
            )
        validate_sorted_records(
            value["parameters"],
            "model_card.parameters",
            "parameter_name",
            validate_model_parameter,
        )
        validate_validation_results(
            value["diagnostic_results"],
            "model_card.diagnostic_results",
        )
        validate_validation_result(
            value["convergence_result"],
            "model_card.convergence_result",
        )
        validate_validation_results(
            value["benchmark_results"],
            "model_card.benchmark_results",
        )
        validate_validation_results(
            value["sensitivity_results"],
            "model_card.sensitivity_results",
        )
        validate_sorted_records(
            value["deterministic_configuration"],
            "model_card.deterministic_configuration",
            "parameter_name",
            validate_model_parameter,
        )
    require_sorted_strings(
        value["quality_flags"],
        expected_type.lower() + ".quality_flags",
        pattern=QUALITY_FLAG,
    )
    validate_validation_results(
        value["validation_results"],
        expected_type.lower() + ".validation_results",
    )
    identity = {key: item for key, item in value.items() if key != "content_hash"}
    if value["content_hash"] != hash_payload(identity):
        raise PortableRuntimeError(expected_type.lower() + ".content_hash_mismatch")


def resolve_component_pointer(value, pointer):
    if (
        not isinstance(pointer, str)
        or pointer in {"", "/"}
        or not pointer.startswith("/")
    ):
        raise PortableRuntimeError("evidence_graph.component_pointer_not_granular")
    current = value
    for encoded in pointer[1:].split("/"):
        token = ""
        index = 0
        while index < len(encoded):
            if encoded[index] != "~":
                token += encoded[index]
                index += 1
                continue
            if index + 1 >= len(encoded) or encoded[index + 1] not in {"0", "1"}:
                raise PortableRuntimeError("evidence_graph.component_pointer_invalid")
            token += "~" if encoded[index + 1] == "0" else "/"
            index += 2
        if isinstance(current, dict):
            if token not in current:
                raise PortableRuntimeError("evidence_graph.component_pointer_missing")
            current = current[token]
        elif isinstance(current, list):
            if (
                not token.isdigit()
                or (len(token) > 1 and token.startswith("0"))
                or int(token) >= len(current)
            ):
                raise PortableRuntimeError(
                    "evidence_graph.component_pointer_index_invalid"
                )
            current = current[int(token)]
        else:
            raise PortableRuntimeError(
                "evidence_graph.component_pointer_traversal_invalid"
            )
    return current


def graph_lineage_ids(root_id, adjacency):
    seen = set()
    queue = deque([root_id])
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        queue.extend(sorted(adjacency[current]))
    return seen


def validate_graph_report_lineage(
    nodes,
    edges,
    incoming,
    outgoing,
    terminals,
):
    by_kind = {
        kind: [item for item in nodes.values() if item["kind"] == kind]
        for kind in NODE_KINDS
    }
    required = ("SOURCE_ROW", "NORMALIZED", "ANALYSIS", "ACCOUNTING", "REPORT_CLAIM")
    if any(not by_kind[kind] for kind in required):
        raise PortableRuntimeError("evidence_graph.aggregate_only_lineage_forbidden")
    if not by_kind["MODEL_CARD"]:
        raise PortableRuntimeError("evidence_graph.report_lineage_model_card_required")
    report_ids = {item["node_id"] for item in by_kind["REPORT_CLAIM"]}
    if report_ids != set(terminals):
        raise PortableRuntimeError("evidence_graph.report_claim_terminals_required")
    if not any(
        nodes[edge["source_id"]]["kind"] == "SOURCE_ROW"
        and nodes[edge["target_id"]]["kind"] == "NORMALIZED"
        and edge["relation"] == "NORMALIZES"
        for edge in edges
    ):
        raise PortableRuntimeError("evidence_graph.source_normalization_edge_required")
    for analysis in by_kind["ANALYSIS"]:
        analysis_id = analysis["node_id"]
        upstream = graph_lineage_ids(analysis_id, incoming)
        upstream_kinds = {nodes[item]["kind"] for item in upstream}
        if not {"SOURCE_ROW", "NORMALIZED", "MODEL_CARD"}.issubset(upstream_kinds):
            raise PortableRuntimeError(
                "evidence_graph.analysis_lineage_incomplete:" + analysis_id
            )
        if not any(
            edge["target_id"] == analysis_id
            and nodes[edge["source_id"]]["kind"] == "MODEL_CARD"
            and edge["relation"] == "CALCULATES"
            for edge in edges
        ):
            raise PortableRuntimeError(
                "evidence_graph.analysis_calculation_edge_missing:" + analysis_id
            )
    required_claim_kinds = {
        "SOURCE_ROW",
        "NORMALIZED",
        "MODEL_CARD",
        "ANALYSIS",
        "ACCOUNTING",
        "REPORT_CLAIM",
    }
    for claim in by_kind["REPORT_CLAIM"]:
        claim_id = claim["node_id"]
        upstream = graph_lineage_ids(claim_id, incoming)
        if not required_claim_kinds.issubset(
            {nodes[item]["kind"] for item in upstream}
        ):
            raise PortableRuntimeError(
                "evidence_graph.report_claim_lineage_incomplete:" + claim_id
            )
        if not any(
            edge["target_id"] == claim_id
            and nodes[edge["source_id"]]["kind"] == "ACCOUNTING"
            and edge["relation"] == "REPORTS"
            for edge in edges
        ):
            raise PortableRuntimeError(
                "evidence_graph.report_claim_accounting_edge_missing:" + claim_id
            )
    for source in by_kind["SOURCE_ROW"]:
        source_id = source["node_id"]
        downstream = graph_lineage_ids(source_id, outgoing)
        if not report_ids.intersection(downstream):
            raise PortableRuntimeError(
                "evidence_graph.source_row_unresolved:" + source_id
            )


def validate_graph_components(nodes, payloads):
    counts = {kind: 0 for kind in COMPONENT_KIND_NODE_KINDS}
    seen = 0
    for node in nodes.values():
        attributes = node["attributes"]
        component_kind = attributes.get("component_kind")
        if component_kind is None:
            continue
        if (
            component_kind not in COMPONENT_KIND_NODE_KINDS
            or node["kind"] != COMPONENT_KIND_NODE_KINDS[component_kind]
        ):
            raise PortableRuntimeError(
                "evidence_graph.component_node_kind_mismatch:" + node["node_id"]
            )
        seen += 1
        counts[component_kind] += 1
        payload = require_object(
            payloads.get(node["node_id"]),
            "evidence_graph.component." + node["node_id"],
        )
        exact_fields(
            payload,
            {
                "schema_version",
                "component_kind",
                "aggregate_artifact_id",
                "json_pointer",
                "value",
            },
            "evidence_graph.component",
        )
        if payload["schema_version"] != 1:
            raise PortableRuntimeError(
                "evidence_graph.component_schema_version_invalid"
            )
        aggregate_id = payload["aggregate_artifact_id"]
        pointer = payload["json_pointer"]
        if (
            payload["component_kind"] != component_kind
            or aggregate_id != attributes.get("aggregate_artifact_id")
            or pointer != attributes.get("json_pointer")
        ):
            raise PortableRuntimeError(
                "evidence_graph.component_attributes_mismatch:" + node["node_id"]
            )
        if (
            not isinstance(aggregate_id, str)
            or aggregate_id == node["node_id"]
            or aggregate_id not in payloads
        ):
            raise PortableRuntimeError(
                "evidence_graph.component_aggregate_invalid:" + node["node_id"]
            )
        selected = resolve_component_pointer(payloads[aggregate_id], pointer)
        if canonical_bytes(selected) != canonical_bytes(payload["value"]):
            raise PortableRuntimeError(
                "evidence_graph.component_value_mismatch:" + node["node_id"]
            )
    if seen and any(value == 0 for value in counts.values()):
        raise PortableRuntimeError("evidence_graph.component_lineage_incomplete")


def validate_graph(value, artifact_records, payloads):
    value = require_object(value, "evidence_graph")
    exact_fields(
        value,
        {
            "schema_version",
            "graph_id",
            "version",
            "nodes",
            "edges",
            "terminal_ids",
            "content_hash",
        },
        "evidence_graph",
    )
    if value["schema_version"] != 1:
        raise PortableRuntimeError("evidence_graph.schema_version_invalid")
    require_id(value["graph_id"], "evidence_graph.graph_id")
    require_id(value["version"], "evidence_graph.version")
    nodes = value["nodes"]
    edges = value["edges"]
    terminals = value["terminal_ids"]
    if not isinstance(nodes, list) or not nodes:
        raise PortableRuntimeError("evidence_graph.nodes_required")
    if not isinstance(edges, list):
        raise PortableRuntimeError("evidence_graph.edges_array_required")
    require_sorted_strings(terminals, "evidence_graph.terminal_ids")
    node_by_id = {}
    node_ids = []
    for index, node in enumerate(nodes):
        label = "evidence_graph.node." + str(index)
        node = require_object(node, label)
        exact_fields(
            node,
            {
                "node_id",
                "version",
                "kind",
                "label",
                "content_hash",
                "parent_ids",
                "quality_flags",
                "attributes",
                "node_hash",
            },
            label,
        )
        node_id = require_id(node["node_id"], label + ".node_id")
        require_id(node["version"], label + ".version")
        if node["kind"] not in NODE_KINDS:
            raise PortableRuntimeError(label + ".kind_invalid")
        require_text(node["label"], label + ".label")
        require_hash(node["content_hash"], label + ".content_hash")
        require_sorted_strings(
            node["parent_ids"],
            label + ".parent_ids",
            allow_empty=True,
        )
        if node["kind"] in ROOT_NODE_KINDS and node["parent_ids"]:
            raise PortableRuntimeError(label + ".root_has_parents")
        if node["kind"] not in ROOT_NODE_KINDS and not node["parent_ids"]:
            raise PortableRuntimeError(label + ".non_root_parent_required")
        require_sorted_strings(
            node["quality_flags"],
            label + ".quality_flags",
            allow_empty=True,
            pattern=QUALITY_FLAG,
        )
        attributes = require_object(node["attributes"], label + ".attributes")
        for key, item in attributes.items():
            require_id(key, label + ".attribute_key")
            require_text(item, label + ".attribute_value")
        identity = {key: item for key, item in node.items() if key != "node_hash"}
        if node["node_hash"] != hash_payload(identity):
            raise PortableRuntimeError(label + ".node_hash_mismatch")
        node_by_id[node_id] = node
        node_ids.append(node_id)
    if node_ids != sorted(set(node_ids)):
        raise PortableRuntimeError("evidence_graph.nodes_not_sorted_unique")
    incoming = {node_id: set() for node_id in node_by_id}
    outgoing = {node_id: set() for node_id in node_by_id}
    edge_keys = []
    for index, edge in enumerate(edges):
        label = "evidence_graph.edge." + str(index)
        edge = require_object(edge, label)
        exact_fields(
            edge,
            {
                "source_id",
                "target_id",
                "relation",
                "source_content_hash",
                "target_content_hash",
                "edge_hash",
            },
            label,
        )
        source_id = require_id(edge["source_id"], label + ".source_id")
        target_id = require_id(edge["target_id"], label + ".target_id")
        if source_id not in node_by_id or target_id not in node_by_id:
            raise PortableRuntimeError(label + ".endpoint_missing")
        if (
            edge["source_content_hash"] != node_by_id[source_id]["content_hash"]
            or edge["target_content_hash"] != node_by_id[target_id]["content_hash"]
        ):
            raise PortableRuntimeError(label + ".content_hash_mismatch")
        if edge["relation"] not in RELATIONS:
            raise PortableRuntimeError(label + ".relation_invalid")
        if (
            node_by_id[source_id]["kind"],
            node_by_id[target_id]["kind"],
        ) not in RELATION_KIND_SIGNATURES[edge["relation"]]:
            raise PortableRuntimeError(label + ".relation_kind_mismatch")
        identity = {key: item for key, item in edge.items() if key != "edge_hash"}
        if edge["edge_hash"] != hash_payload(identity):
            raise PortableRuntimeError(label + ".edge_hash_mismatch")
        incoming[target_id].add(source_id)
        outgoing[source_id].add(target_id)
        edge_keys.append((source_id, target_id))
    if edge_keys != sorted(set(edge_keys)):
        raise PortableRuntimeError("evidence_graph.edges_not_sorted_unique")
    for node_id, node in node_by_id.items():
        if incoming[node_id] != set(node["parent_ids"]):
            raise PortableRuntimeError(
                "evidence_graph.missing_or_extra_parent_edge:" + node_id
            )
    actual_terminals = sorted(
        node_id for node_id, targets in outgoing.items() if not targets
    )
    if actual_terminals != terminals:
        raise PortableRuntimeError("evidence_graph.terminal_ids_mismatch")
    indegree = {node_id: len(parents) for node_id, parents in incoming.items()}
    queue = deque(sorted(node_id for node_id, count in indegree.items() if count == 0))
    visited = 0
    while queue:
        node_id = queue.popleft()
        visited += 1
        for target_id in sorted(outgoing[node_id]):
            indegree[target_id] -= 1
            if indegree[target_id] == 0:
                queue.append(target_id)
    if visited != len(node_by_id):
        raise PortableRuntimeError("evidence_graph.cycle_detected")
    traceable = {
        item["logical_id"]: item
        for item in artifact_records
        if item["role"] in GRAPH_ROLE_KINDS
    }
    if set(traceable) != set(node_by_id):
        raise PortableRuntimeError("evidence_graph.artifact_node_coverage_mismatch")
    for logical_id, record in traceable.items():
        node = node_by_id[logical_id]
        if (
            node["kind"] not in GRAPH_ROLE_KINDS[record["role"]]
            or node["version"] != record["version"]
            or node["content_hash"] != record["content_hash"]
        ):
            raise PortableRuntimeError(
                "evidence_graph.artifact_node_binding_mismatch:" + logical_id
            )
    validate_graph_report_lineage(
        node_by_id,
        edges,
        incoming,
        outgoing,
        terminals,
    )
    validate_graph_components(node_by_id, payloads)
    identity = {key: item for key, item in value.items() if key != "content_hash"}
    if value["content_hash"] != hash_payload(identity):
        raise PortableRuntimeError("evidence_graph.content_hash_mismatch")
    return node_by_id


def validate_artifact_record(value):
    value = require_object(value, "artifact_record")
    exact_fields(
        value,
        {
            "logical_id",
            "version",
            "role",
            "relative_path",
            "content_hash",
            "byte_length",
            "media_type",
            "quality_flags",
        },
        "artifact_record",
    )
    require_id(value["logical_id"], "artifact_record.logical_id")
    require_id(value["version"], "artifact_record.version")
    if value["role"] not in ROLES:
        raise PortableRuntimeError("artifact_record.role_invalid")
    safe_relative(value["relative_path"], "artifact_record.relative_path")
    require_hash(value["content_hash"], "artifact_record.content_hash")
    if value["relative_path"] != artifact_path_for(value["content_hash"]):
        raise PortableRuntimeError("artifact_record.path_not_content_addressed")
    if (
        isinstance(value["byte_length"], bool)
        or not isinstance(value["byte_length"], int)
        or value["byte_length"] <= 0
        or value["byte_length"] > MAX_COMPONENT_BYTES
    ):
        raise PortableRuntimeError("artifact_record.byte_length_invalid")
    require_text(value["media_type"], "artifact_record.media_type")
    if value["role"] == "ENGINE_SOURCE" and value["media_type"] != "application/zip":
        raise PortableRuntimeError("artifact_record.engine_source_media_invalid")
    if (
        value["role"] == "ENGINE_REPLAY_DESCRIPTOR"
        and value["media_type"] != "application/json"
    ):
        raise PortableRuntimeError(
            "artifact_record.engine_replay_descriptor_media_invalid"
        )
    require_sorted_strings(
        value["quality_flags"],
        "artifact_record.quality_flags",
        allow_empty=True,
        pattern=QUALITY_FLAG,
    )
    return value


def validate_support_record(value):
    value = require_object(value, "support_record")
    exact_fields(
        value,
        {"relative_path", "content_hash", "byte_length", "purpose"},
        "support_record",
    )
    safe_relative(value["relative_path"], "support_record.relative_path")
    require_hash(value["content_hash"], "support_record.content_hash")
    if (
        isinstance(value["byte_length"], bool)
        or not isinstance(value["byte_length"], int)
        or value["byte_length"] <= 0
    ):
        raise PortableRuntimeError("support_record.byte_length_invalid")
    require_id(value["purpose"], "support_record.purpose")
    return value


def validate_manifest(value):
    value = require_object(value, "manifest")
    exact_fields(
        value,
        {
            "schema_version",
            "package_type",
            "package_id",
            "package_version",
            "replay_algorithm_version",
            "seed",
            "artifacts",
            "support_files",
            "package_quality_flags",
            "content_hash",
        },
        "manifest",
    )
    if (
        value["schema_version"] != SCHEMA_VERSION
        or value["package_type"] != "PORTABLE_VALIDATED_MULTI_ASSET_RESEARCH"
        or value["replay_algorithm_version"] != REPLAY_ALGORITHM_VERSION
    ):
        raise PortableRuntimeError("manifest.version_type_or_algorithm_invalid")
    require_id(value["package_id"], "manifest.package_id")
    require_id(value["package_version"], "manifest.package_version")
    if (
        isinstance(value["seed"], bool)
        or not isinstance(value["seed"], int)
        or value["seed"] < 0
    ):
        raise PortableRuntimeError("manifest.seed_invalid")
    if not isinstance(value["artifacts"], list):
        raise PortableRuntimeError("manifest.artifacts_array_required")
    if not isinstance(value["support_files"], list):
        raise PortableRuntimeError("manifest.support_files_array_required")
    artifacts = [validate_artifact_record(item) for item in value["artifacts"]]
    support = [validate_support_record(item) for item in value["support_files"]]
    artifact_keys = [
        (item["role"], item["logical_id"], item["version"]) for item in artifacts
    ]
    if artifact_keys != sorted(set(artifact_keys)):
        raise PortableRuntimeError("manifest.artifacts_not_sorted_unique")
    logical_ids = [item["logical_id"] for item in artifacts]
    if len(logical_ids) != len(set(logical_ids)):
        raise PortableRuntimeError("manifest.logical_id_duplicate")
    support_paths = [item["relative_path"] for item in support]
    if support_paths != sorted(SUPPORT_PATHS):
        raise PortableRuntimeError("manifest.support_files_incomplete")
    counts = {role: sum(item["role"] == role for item in artifacts) for role in ROLES}
    for role in SINGLETON_ROLES:
        if counts[role] != 1:
            raise PortableRuntimeError("manifest.role_cardinality_invalid:" + role)
    for role in MULTI_ROLES:
        if counts[role] < 1:
            raise PortableRuntimeError("manifest.role_required:" + role)
    executable_counts = tuple(
        counts[role] for role in sorted(OPTIONAL_EXECUTABLE_ROLES)
    )
    if executable_counts not in {(0, 0), (1, 1)}:
        raise PortableRuntimeError("manifest.executable_replay_pair_required")
    package_flags = require_sorted_strings(
        value["package_quality_flags"],
        "manifest.package_quality_flags",
        allow_empty=True,
        pattern=QUALITY_FLAG,
    )
    propagated = sorted(
        {
            flag
            for item in artifacts
            if item["role"] != "QUALITY_FLAGS"
            for flag in item["quality_flags"]
        }
    )
    if package_flags != propagated:
        raise PortableRuntimeError("manifest.quality_flag_propagation_mismatch")
    identity = {key: item for key, item in value.items() if key != "content_hash"}
    if value["content_hash"] != hash_payload(identity):
        raise PortableRuntimeError("manifest.content_hash_mismatch")
    return value


def read_regular_file(root, relative_path):
    relative = safe_relative(relative_path, "package.relative_path")
    path = root.joinpath(*relative.parts)
    try:
        stat_result = path.lstat()
    except OSError as exc:
        raise PortableRuntimeError("package.file_missing:" + relative_path) from exc
    if path.is_symlink() or not path.is_file():
        raise PortableRuntimeError("package.regular_file_required:" + relative_path)
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PortableRuntimeError("package.path_escape:" + relative_path) from exc
    if stat_result.st_size <= 0 or stat_result.st_size > MAX_COMPONENT_BYTES:
        raise PortableRuntimeError("package.file_size_invalid:" + relative_path)
    return path.read_bytes()


def exact_package_paths(root, expected_files):
    expected_directories = {""}
    for relative in expected_files:
        path = PurePosixPath(relative)
        for parent in path.parents:
            if parent.as_posix() != ".":
                expected_directories.add(parent.as_posix())
    actual_files = set()
    actual_directories = {""}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise PortableRuntimeError("package.symlink_forbidden:" + relative)
        if path.is_dir():
            actual_directories.add(relative)
        elif path.is_file():
            actual_files.add(relative)
        else:
            raise PortableRuntimeError("package.non_regular_entry:" + relative)
    if actual_files != set(expected_files):
        missing = sorted(set(expected_files) - actual_files)
        unexpected = sorted(actual_files - set(expected_files))
        raise PortableRuntimeError(
            "package.file_set_mismatch:missing="
            + ",".join(missing)
            + ":unexpected="
            + ",".join(unexpected)
        )
    if actual_directories != expected_directories:
        raise PortableRuntimeError("package.directory_set_mismatch")


def records_by_role(manifest):
    result = {}
    for item in manifest["artifacts"]:
        result.setdefault(item["role"], []).append(item)
    return result


def canonical_replay_outputs(manifest, payloads):
    roles = records_by_role(manifest)
    quality_payload = require_object(
        payloads[roles["QUALITY_FLAGS"][0]["logical_id"]],
        "quality_flags",
    )
    exact_fields(
        quality_payload,
        {"schema_version", "quality_flags"},
        "quality_flags",
    )
    if quality_payload["schema_version"] != 1:
        raise PortableRuntimeError("quality_flags.schema_version_invalid")
    quality_flags = require_sorted_strings(
        quality_payload["quality_flags"],
        "quality_flags.quality_flags",
        allow_empty=True,
        pattern=QUALITY_FLAG,
    )
    if quality_flags != manifest["package_quality_flags"]:
        raise PortableRuntimeError("quality_flags.manifest_mismatch")
    replay_components = [
        {
            "logical_id": item["logical_id"],
            "version": item["version"],
            "role": item["role"],
            "content_hash": item["content_hash"],
        }
        for item in manifest["artifacts"]
        if item["role"] not in {"STUDY", "REPORT"}
    ]
    study_identity = {
        "schema_version": 1,
        "artifact_type": "PORTABLE_VALIDATED_MULTI_ASSET_STUDY",
        "package_id": manifest["package_id"],
        "package_version": manifest["package_version"],
        "replay_algorithm_version": manifest["replay_algorithm_version"],
        "seed": manifest["seed"],
        "components": replay_components,
        "immutable_input_hashes": [
            item["content_hash"] for item in roles["IMMUTABLE_INPUT"]
        ],
        "data_card_hashes": [item["content_hash"] for item in roles["DATA_CARD"]],
        "model_card_hashes": [item["content_hash"] for item in roles["MODEL_CARD"]],
        "normalized_evidence_hashes": [
            item["content_hash"] for item in roles["NORMALIZED_EVIDENCE"]
        ],
        "derived_evidence_hashes": [
            item["content_hash"] for item in roles["DERIVED_EVIDENCE"]
        ],
        "accounting_hash": roles["ACCOUNTING"][0]["content_hash"],
        "evidence_graph_hash": roles["EVIDENCE_GRAPH"][0]["content_hash"],
        "quality_flags": quality_flags,
    }
    study = {**study_identity, "content_hash": hash_payload(study_identity)}
    report_identity = {
        "schema_version": 1,
        "artifact_type": "PORTABLE_VALIDATED_MULTI_ASSET_REPORT",
        "package_id": manifest["package_id"],
        "package_version": manifest["package_version"],
        "study_content_hash": study["content_hash"],
        "evidence_graph_hash": roles["EVIDENCE_GRAPH"][0]["content_hash"],
        "accounting_hash": roles["ACCOUNTING"][0]["content_hash"],
        "source_identity_hash": roles["SOURCE_IDENTITY"][0]["content_hash"],
        "quality_flags": quality_flags,
        "checks": {
            "accounting_present": True,
            "cards_present": True,
            "content_addressed_inputs": True,
            "evidence_graph_resolved": True,
            "portable_replay": True,
        },
        "status": "VERIFIED",
    }
    report = {**report_identity, "content_hash": hash_payload(report_identity)}
    return study, report


def verify_checksums(payload, manifest):
    payload = require_object(payload, "checksums")
    exact_fields(payload, {"schema_version", "checksums"}, "checksums")
    if payload["schema_version"] != 1 or not isinstance(payload["checksums"], list):
        raise PortableRuntimeError("checksums.schema_or_rows_invalid")
    expected = [
        {
            "logical_id": item["logical_id"],
            "relative_path": item["relative_path"],
            "content_hash": item["content_hash"],
            "byte_length": item["byte_length"],
        }
        for item in manifest["artifacts"]
        if item["role"] not in {"CHECKSUMS", "STUDY", "REPORT"}
    ]
    if payload["checksums"] != expected:
        raise PortableRuntimeError("checksums.rows_mismatch")


def verify_source_identity(payload, support):
    payload = require_object(payload, "source_identity")
    exact_fields(
        payload,
        {
            "schema_version",
            "source_id",
            "source_version",
            "replay_source_path",
            "replay_source_hash",
            "verify_wrapper_hash",
            "reproduce_wrapper_hash",
        },
        "source_identity",
    )
    if (
        payload["schema_version"] != 1
        or payload["source_id"] != "portable-multi-asset-runtime"
        or payload["source_version"] != REPLAY_ALGORITHM_VERSION
        or payload["replay_source_path"] != "portable_runtime.py"
    ):
        raise PortableRuntimeError("source_identity.identity_invalid")
    support_by_path = {item["relative_path"]: item for item in support}
    if (
        payload["replay_source_hash"]
        != support_by_path["portable_runtime.py"]["content_hash"]
        or payload["verify_wrapper_hash"]
        != support_by_path["verify.py"]["content_hash"]
        or payload["reproduce_wrapper_hash"]
        != support_by_path["reproduce.py"]["content_hash"]
    ):
        raise PortableRuntimeError("source_identity.support_hash_mismatch")


def validate_engine_source_archive(raw, target=None):
    if not isinstance(raw, bytes) or not raw:
        raise PortableRuntimeError("engine_source.archive_required")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw), mode="r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise PortableRuntimeError("engine_source.archive_invalid") from exc
    with archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if (
            not names
            or names != sorted(set(names))
            or "SOURCE_MANIFEST.json" not in names
        ):
            raise PortableRuntimeError("engine_source.entries_invalid")
        total = 0
        payloads = {}
        for info in infos:
            relative = safe_relative(info.filename, "engine_source.relative_path")
            mode = info.external_attr >> 16
            if (
                info.is_dir()
                or info.flag_bits & 1
                or (mode and not stat.S_ISREG(mode))
                or info.file_size < 0
                or info.file_size > 32 * 1024 * 1024
            ):
                raise PortableRuntimeError("engine_source.entry_invalid")
            total += info.file_size
            if total > 32 * 1024 * 1024:
                raise PortableRuntimeError("engine_source.total_size_invalid")
            item_raw = archive.read(info)
            if len(item_raw) != info.file_size:
                raise PortableRuntimeError("engine_source.entry_size_mismatch")
            payloads[info.filename] = item_raw
            if target is not None and info.filename != "SOURCE_MANIFEST.json":
                destination = target.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with destination.open("xb") as handle:
                        handle.write(item_raw)
                except FileExistsError as exc:
                    raise PortableRuntimeError(
                        "engine_source.extraction_collision"
                    ) from exc
        manifest = require_object(
            load_json_bytes(
                payloads["SOURCE_MANIFEST.json"],
                "engine_source.manifest",
            ),
            "engine_source.manifest",
        )
        exact_fields(
            manifest,
            {
                "schema_version",
                "archive_type",
                "files",
                "content_hash",
            },
            "engine_source.manifest",
        )
        if (
            manifest["schema_version"] != 1
            or manifest["archive_type"] != "MARKET_RESEARCH_ENGINE_SOURCE"
            or not isinstance(manifest["files"], list)
        ):
            raise PortableRuntimeError("engine_source.manifest_identity_invalid")
        records = []
        for index, item in enumerate(manifest["files"]):
            label = "engine_source.manifest.file." + str(index)
            item = require_object(item, label)
            exact_fields(
                item,
                {"relative_path", "content_hash", "byte_length"},
                label,
            )
            relative_path = safe_relative(
                item["relative_path"],
                label + ".relative_path",
            ).as_posix()
            require_hash(item["content_hash"], label + ".content_hash")
            byte_length = item["byte_length"]
            if (
                isinstance(byte_length, bool)
                or not isinstance(byte_length, int)
                or byte_length < 0
            ):
                raise PortableRuntimeError(label + ".byte_length_invalid")
            item_raw = payloads.get(relative_path)
            if (
                item_raw is None
                or len(item_raw) != byte_length
                or hash_bytes(item_raw) != item["content_hash"]
            ):
                raise PortableRuntimeError(label + ".content_mismatch")
            records.append(item)
        if [item["relative_path"] for item in records] != sorted(
            set(item["relative_path"] for item in records)
        ):
            raise PortableRuntimeError("engine_source.manifest.files_not_sorted")
        if set(payloads) != {
            "SOURCE_MANIFEST.json",
            *(item["relative_path"] for item in records),
        }:
            raise PortableRuntimeError("engine_source.manifest_coverage_mismatch")
        identity = {
            key: value for key, value in manifest.items() if key != "content_hash"
        }
        if manifest["content_hash"] != hash_bytes(json_file_bytes(identity)):
            raise PortableRuntimeError("engine_source.manifest_hash_mismatch")


def validate_engine_descriptor(
    descriptor,
    *,
    source_record,
    study_record,
    report_record,
    study_payload,
    report_payload,
    accounting_payload,
):
    descriptor = require_object(descriptor, "engine_replay_descriptor")
    exact_fields(
        descriptor,
        {
            "schema_version",
            "request",
            "evidence_envelopes",
            "expected_study_content_hash",
            "expected_study_artifact_hash",
            "expected_report_artifact_hash",
            "expected_accounting_reconciliation_hash",
            "engine_source_content_hash",
        },
        "engine_replay_descriptor",
    )
    if descriptor["schema_version"] != 1:
        raise PortableRuntimeError("engine_replay_descriptor.version_invalid")
    for field_name in (
        "expected_study_content_hash",
        "expected_study_artifact_hash",
        "expected_report_artifact_hash",
        "expected_accounting_reconciliation_hash",
        "engine_source_content_hash",
    ):
        require_hash(
            descriptor[field_name],
            "engine_replay_descriptor." + field_name,
        )
    if descriptor["engine_source_content_hash"] != source_record["content_hash"]:
        raise PortableRuntimeError("engine_replay_descriptor.source_hash_mismatch")
    if descriptor["expected_study_artifact_hash"] != study_record["content_hash"]:
        raise PortableRuntimeError("engine_replay_descriptor.study_artifact_mismatch")
    if descriptor["expected_report_artifact_hash"] != report_record["content_hash"]:
        raise PortableRuntimeError("engine_replay_descriptor.report_artifact_mismatch")
    study_payload = require_object(study_payload, "engine_study")
    study_identity = {
        key: value for key, value in study_payload.items() if key != "content_hash"
    }
    if (
        study_payload.get("content_hash") != hash_payload(study_identity)
        or descriptor["expected_study_content_hash"] != study_payload["content_hash"]
    ):
        raise PortableRuntimeError("engine_replay_descriptor.study_content_mismatch")
    report_payload = require_object(report_payload, "engine_report")
    accounting_payload = require_object(accounting_payload, "engine_accounting")
    expected_accounting_hash = descriptor["expected_accounting_reconciliation_hash"]
    if (
        study_payload.get("accounting_reconciliation_hash") != expected_accounting_hash
        or report_payload.get("accounting_reconciliation_hash")
        != expected_accounting_hash
        or accounting_payload.get("study_content_hash")
        != descriptor["expected_study_content_hash"]
    ):
        raise PortableRuntimeError(
            "engine_replay_descriptor.accounting_binding_mismatch"
        )
    request = require_object(descriptor["request"], "engine_replay.request")
    references = request.get("evidence_references")
    entries = descriptor["evidence_envelopes"]
    if (
        not isinstance(references, list)
        or not references
        or not isinstance(entries, list)
        or not entries
    ):
        raise PortableRuntimeError("engine_replay_descriptor.evidence_required")
    reference_identities = []
    for index, reference in enumerate(references):
        label = "engine_replay.reference." + str(index)
        reference = require_object(reference, label)
        required = {
            "role",
            "logical_id",
            "version",
            "uri",
            "content_hash",
            "schema_hash",
            "byte_length",
        }
        if not required.issubset(reference):
            raise PortableRuntimeError(label + ".fields_invalid")
        reference_identities.append(
            (
                reference["role"],
                reference["logical_id"],
                reference["version"],
                reference["content_hash"],
                reference["schema_hash"],
                reference["byte_length"],
            )
        )
    entry_identities = []
    for index, entry in enumerate(entries):
        label = "engine_replay.evidence." + str(index)
        entry = require_object(entry, label)
        exact_fields(
            entry,
            {
                "role",
                "logical_id",
                "version",
                "content_hash",
                "schema_hash",
                "byte_length",
                "payload_base64",
            },
            label,
        )
        for field_name in ("role", "logical_id", "version"):
            require_text(entry[field_name], label + "." + field_name)
        require_hash(entry["content_hash"], label + ".content_hash")
        require_hash(entry["schema_hash"], label + ".schema_hash")
        byte_length = entry["byte_length"]
        if (
            isinstance(byte_length, bool)
            or not isinstance(byte_length, int)
            or byte_length <= 0
        ):
            raise PortableRuntimeError(label + ".byte_length_invalid")
        encoded = require_text(entry["payload_base64"], label + ".payload_base64")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise PortableRuntimeError(label + ".payload_base64_invalid") from exc
        if (
            base64.b64encode(raw).decode("ascii") != encoded
            or len(raw) != byte_length
            or hash_bytes(raw) != entry["content_hash"]
        ):
            raise PortableRuntimeError(label + ".payload_binding_mismatch")
        entry_identities.append(
            (
                entry["role"],
                entry["logical_id"],
                entry["version"],
                entry["content_hash"],
                entry["schema_hash"],
                entry["byte_length"],
            )
        )
    if sorted(reference_identities) != sorted(entry_identities):
        raise PortableRuntimeError(
            "engine_replay_descriptor.reference_coverage_mismatch"
        )
    return descriptor


def execute_engine_replay(package_root, manifest, payloads, raw_by_logical_id):
    roles = records_by_role(manifest)
    source_record = roles["ENGINE_SOURCE"][0]
    descriptor_record = roles["ENGINE_REPLAY_DESCRIPTOR"][0]
    source_raw = raw_by_logical_id[source_record["logical_id"]]
    validate_engine_source_archive(source_raw)
    temp_base = Path("/tmp/codex-gap-closure")
    temp_base.mkdir(parents=True, exist_ok=True)
    if temp_base.is_symlink() or not temp_base.is_dir():
        raise PortableRuntimeError("engine_replay.temp_root_invalid")
    child_code = """
import json
import sys
from pathlib import Path
sys.dont_write_bytecode = True
def deny_network(event, args):
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system"}:
        raise RuntimeError("portable_engine_replay_external_effect_forbidden:" + event)
sys.addaudithook(deny_network)
sys.path.insert(0, sys.argv[1])
from market_research.research.multi_asset.portable_engine_replay import replay_builtin_engine
descriptor = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
result = replay_builtin_engine(descriptor, workspace_root=sys.argv[3])
sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\\n")
"""
    with tempfile.TemporaryDirectory(
        prefix="engine-replay.",
        dir=temp_base,
    ) as temporary:
        root = Path(temporary)
        source_root = root / "source"
        source_root.mkdir()
        validate_engine_source_archive(source_raw, target=source_root)
        descriptor_path = package_root / descriptor_record["relative_path"]

        def run_once(name):
            home = root / ("home-" + name)
            home.mkdir()
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    child_code,
                    str(source_root),
                    str(descriptor_path),
                    str(root / ("workspace-" + name)),
                ],
                cwd=root,
                env={
                    "HOME": str(home),
                    "LANG": "C.UTF-8",
                    "PATH": "/usr/bin:/bin",
                    "PYTHONNOUSERSITE": "1",
                    "PYTHONPATH": "",
                    "TZ": "UTC",
                },
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if completed.returncode != 0:
                raise PortableRuntimeError(
                    "engine_replay.child_failed:" + completed.stderr.strip()[-1000:]
                )
            return require_object(
                load_json_bytes(
                    completed.stdout.encode("utf-8"),
                    "engine_replay.child_result",
                ),
                "engine_replay.child_result",
            )

        return run_once("first"), run_once("second")


def verify_package(package_root):
    root = Path(package_root).expanduser().resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise PortableRuntimeError("package.root_directory_required")
    manifest_raw = read_regular_file(root, "manifest.json")
    manifest = validate_manifest(load_json_bytes(manifest_raw, "manifest"))
    expected_files = {
        "manifest.json",
        *(item["relative_path"] for item in manifest["artifacts"]),
        *(item["relative_path"] for item in manifest["support_files"]),
    }
    exact_package_paths(root, expected_files)
    support = manifest["support_files"]
    for item in support:
        raw = read_regular_file(root, item["relative_path"])
        if len(raw) != item["byte_length"] or hash_bytes(raw) != item["content_hash"]:
            raise PortableRuntimeError(
                "support_file_hash_or_length_mismatch:" + item["relative_path"]
            )
    payloads = {}
    raw_by_logical_id = {}
    for item in manifest["artifacts"]:
        raw = read_regular_file(root, item["relative_path"])
        if len(raw) != item["byte_length"] or hash_bytes(raw) != item["content_hash"]:
            raise PortableRuntimeError(
                "artifact_hash_or_length_mismatch:" + item["logical_id"]
            )
        raw_by_logical_id[item["logical_id"]] = raw
        if item["media_type"] == "application/json":
            payloads[item["logical_id"]] = load_json_bytes(
                raw, "artifact." + item["logical_id"]
            )
    roles = records_by_role(manifest)
    for item in roles["DATA_CARD"]:
        validate_card(payloads[item["logical_id"]], "DATA_CARD")
        if payloads[item["logical_id"]]["quality_flags"] != item["quality_flags"]:
            raise PortableRuntimeError("data_card.quality_flags_mismatch")
    for item in roles["MODEL_CARD"]:
        validate_card(payloads[item["logical_id"]], "MODEL_CARD")
        if payloads[item["logical_id"]]["quality_flags"] != item["quality_flags"]:
            raise PortableRuntimeError("model_card.quality_flags_mismatch")
    graph_record = roles["EVIDENCE_GRAPH"][0]
    graph_nodes = validate_graph(
        payloads[graph_record["logical_id"]],
        manifest["artifacts"],
        payloads,
    )
    graph_flags = sorted(
        {flag for node in graph_nodes.values() for flag in node["quality_flags"]}
    )
    if graph_flags != graph_record["quality_flags"]:
        raise PortableRuntimeError("evidence_graph.quality_flags_mismatch")
    source_record = roles["SOURCE_IDENTITY"][0]
    verify_source_identity(payloads[source_record["logical_id"]], support)
    checksum_record = roles["CHECKSUMS"][0]
    verify_checksums(payloads[checksum_record["logical_id"]], manifest)
    expected_study = roles["STUDY"][0]
    expected_report = roles["REPORT"][0]
    if roles.get("ENGINE_SOURCE"):
        engine_source_record = roles["ENGINE_SOURCE"][0]
        descriptor_record = roles["ENGINE_REPLAY_DESCRIPTOR"][0]
        validate_engine_source_archive(
            raw_by_logical_id[engine_source_record["logical_id"]]
        )
        study = payloads[expected_study["logical_id"]]
        report = payloads[expected_report["logical_id"]]
        validate_engine_descriptor(
            payloads[descriptor_record["logical_id"]],
            source_record=engine_source_record,
            study_record=expected_study,
            report_record=expected_report,
            study_payload=study,
            report_payload=report,
            accounting_payload=payloads[roles["ACCOUNTING"][0]["logical_id"]],
        )
        study_content_hash = study["content_hash"]
        report_content_hash = expected_report["content_hash"]
    else:
        study, report = canonical_replay_outputs(manifest, payloads)
        if json_file_bytes(study) != raw_by_logical_id[expected_study["logical_id"]]:
            raise PortableRuntimeError("study.replay_mismatch")
        if json_file_bytes(report) != raw_by_logical_id[expected_report["logical_id"]]:
            raise PortableRuntimeError("report.replay_mismatch")
        study_content_hash = study["content_hash"]
        report_content_hash = report["content_hash"]
    return {
        "status": "PASS",
        "package_id": manifest["package_id"],
        "package_version": manifest["package_version"],
        "manifest_hash": manifest["content_hash"],
        "study_content_hash": study_content_hash,
        "report_content_hash": report_content_hash,
        "files_verified": len(expected_files),
        "quality_flags": manifest["package_quality_flags"],
    }


def reproduce_package(package_root):
    verification = verify_package(package_root)
    root = Path(package_root).expanduser().resolve(strict=True)
    manifest = validate_manifest(
        load_json_bytes(read_regular_file(root, "manifest.json"), "manifest")
    )
    payloads = {}
    raw_by_logical_id = {}
    for item in manifest["artifacts"]:
        raw = read_regular_file(root, item["relative_path"])
        raw_by_logical_id[item["logical_id"]] = raw
        if item["media_type"] == "application/json":
            payloads[item["logical_id"]] = load_json_bytes(
                raw,
                "artifact." + item["logical_id"],
            )
    roles = records_by_role(manifest)
    if roles.get("ENGINE_SOURCE"):
        first, second = execute_engine_replay(
            root,
            manifest,
            payloads,
            raw_by_logical_id,
        )
        mismatches = []
        for field_name in (
            "study_content_hash",
            "study_artifact_hash",
            "report_artifact_hash",
            "accounting_reconciliation_hash",
        ):
            if first.get(field_name) != second.get(field_name):
                mismatches.append(field_name.upper())
        if first.get("study_content_hash") != verification["study_content_hash"]:
            mismatches.append("EXPECTED_STUDY")
        if first.get("report_artifact_hash") != verification["report_content_hash"]:
            mismatches.append("EXPECTED_REPORT")
        if first.get("mismatch_fields") != [] or second.get("mismatch_fields") != []:
            mismatches.append("ENGINE_MISMATCH_FIELDS")
        return {
            "status": "PASS" if not mismatches else "FAIL",
            "package_id": manifest["package_id"],
            "package_version": manifest["package_version"],
            "manifest_hash": manifest["content_hash"],
            "first_study_content_hash": first["study_content_hash"],
            "second_study_content_hash": second["study_content_hash"],
            "first_report_content_hash": first["report_artifact_hash"],
            "second_report_content_hash": second["report_artifact_hash"],
            "mismatch_fields": sorted(set(mismatches)),
        }
    first_study, first_report = canonical_replay_outputs(manifest, payloads)
    second_study, second_report = canonical_replay_outputs(manifest, payloads)
    mismatches = []
    if canonical_bytes(first_study) != canonical_bytes(second_study):
        mismatches.append("STUDY")
    if canonical_bytes(first_report) != canonical_bytes(second_report):
        mismatches.append("REPORT")
    if first_study["content_hash"] != verification["study_content_hash"]:
        mismatches.append("EXPECTED_STUDY")
    if first_report["content_hash"] != verification["report_content_hash"]:
        mismatches.append("EXPECTED_REPORT")
    return {
        "status": "PASS" if not mismatches else "FAIL",
        "package_id": manifest["package_id"],
        "package_version": manifest["package_version"],
        "manifest_hash": manifest["content_hash"],
        "first_study_content_hash": first_study["content_hash"],
        "second_study_content_hash": second_study["content_hash"],
        "first_report_content_hash": first_report["content_hash"],
        "second_report_content_hash": second_report["content_hash"],
        "mismatch_fields": mismatches,
    }


def write_receipt(path_value, payload, package_root):
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        raise PortableRuntimeError("receipt.absolute_path_required")
    path = path.resolve()
    package = Path(package_root).expanduser().resolve(strict=True)
    try:
        path.relative_to(package)
    except ValueError:
        pass
    else:
        raise PortableRuntimeError("receipt.package_internal_path_forbidden")
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json_file_bytes(payload)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        if path.read_bytes() != raw:
            raise PortableRuntimeError("receipt.existing_content_mismatch")
        return
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(mode=None):
    selected = mode or (
        "reproduce" if Path(sys.argv[0]).name == "reproduce.py" else "verify"
    )
    parser = argparse.ArgumentParser(
        description="Verify or reproduce one portable multi-asset package."
    )
    parser.add_argument("package")
    parser.add_argument("--out")
    args = parser.parse_args()
    try:
        if selected == "verify":
            result = verify_package(args.package)
        elif selected == "reproduce":
            result = reproduce_package(args.package)
        else:
            raise PortableRuntimeError("runtime.mode_invalid")
        if args.out:
            write_receipt(args.out, result, args.package)
        sys.stdout.write(canonical_bytes(result).decode("utf-8") + "\n")
        return 0 if result["status"] == "PASS" else 1
    except Exception as exc:
        sys.stderr.write(
            canonical_bytes(
                {
                    "status": "FAIL",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            ).decode("utf-8")
            + "\n"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
