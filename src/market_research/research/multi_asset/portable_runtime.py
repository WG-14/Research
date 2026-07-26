# mypy: ignore-errors
"""Standard-library-only verifier and deterministic replay runtime.

This file is copied verbatim into every portable multi-asset package.  It must
not import any project module or third-party dependency.

The verifier intentionally avoids project typing imports so the copied file is
fully standalone.  Its dynamic JSON validation is covered by cold-root tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import deque
from pathlib import Path, PurePosixPath


SCHEMA_VERSION = 1
REPLAY_ALGORITHM_VERSION = "multi-asset-portable-replay-v1"
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
    "NORMALIZED_EVIDENCE": frozenset({"NORMALIZED"}),
    "DERIVED_EVIDENCE": frozenset(
        {"DERIVED", "ANALYSIS", "REPORT_CLAIM"}
    ),
    "ACCOUNTING": frozenset({"ACCOUNTING"}),
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
    if not isinstance(value, dict) or any(
        not isinstance(key, str) for key in value
    ):
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
            "use_constraints",
            "coverage_start_at",
            "coverage_end_at",
            "coverage_markets",
            "coverage_instruments",
            "missing_data_summary",
            "missing_data_policy",
            "known_biases",
            "revision_policy",
            "validation_results",
            "quality_flags",
            "content_hash",
        }
        id_fields = ("card_id", "version", "dataset_id", "dataset_version")
        text_fields = (
            "source_name",
            "source_reference",
            "license_id",
            "coverage_start_at",
            "coverage_end_at",
            "missing_data_summary",
            "missing_data_policy",
            "revision_policy",
        )
        list_fields = (
            "use_constraints",
            "coverage_markets",
            "coverage_instruments",
            "known_biases",
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
            "implementation_hash",
            "input_schema_hash",
            "output_schema_hash",
            "assumptions",
            "applicability_scope",
            "failure_conditions",
            "validation_results",
            "known_limitations",
            "quality_flags",
            "content_hash",
        }
        id_fields = ("card_id", "version", "model_id", "model_version")
        text_fields = ("model_name",)
        list_fields = (
            "assumptions",
            "applicability_scope",
            "failure_conditions",
            "known_limitations",
        )
        hash_fields = (
            "implementation_hash",
            "input_schema_hash",
            "output_schema_hash",
        )
    exact_fields(value, expected, expected_type.lower())
    if value["schema_version"] != 1 or value["card_type"] != expected_type:
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
    require_sorted_strings(
        value["quality_flags"],
        expected_type.lower() + ".quality_flags",
        allow_empty=True,
        pattern=QUALITY_FLAG,
    )
    results = value["validation_results"]
    if not isinstance(results, list) or not results:
        raise PortableRuntimeError(
            expected_type.lower() + ".validation_results_required"
        )
    for index, item in enumerate(results):
        validate_validation_result(
            item,
            expected_type.lower() + ".validation_results." + str(index),
        )
    result_ids = [item["check_id"] for item in results]
    if result_ids != sorted(set(result_ids)):
        raise PortableRuntimeError(
            expected_type.lower() + ".validation_results_not_sorted_unique"
        )
    identity = {key: item for key, item in value.items() if key != "content_hash"}
    if value["content_hash"] != hash_payload(identity):
        raise PortableRuntimeError(expected_type.lower() + ".content_hash_mismatch")


def validate_graph(value, artifact_records):
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
        require_text(node["kind"], label + ".kind")
        require_text(node["label"], label + ".label")
        require_hash(node["content_hash"], label + ".content_hash")
        require_sorted_strings(
            node["parent_ids"],
            label + ".parent_ids",
            allow_empty=True,
        )
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
        require_text(edge["relation"], label + ".relation")
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
    counts = {
        role: sum(item["role"] == role for item in artifacts) for role in ROLES
    }
    for role in SINGLETON_ROLES:
        if counts[role] != 1:
            raise PortableRuntimeError("manifest.role_cardinality_invalid:" + role)
    for role in MULTI_ROLES:
        if counts[role] < 1:
            raise PortableRuntimeError("manifest.role_required:" + role)
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
        "model_card_hashes": [
            item["content_hash"] for item in roles["MODEL_CARD"]
        ],
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
    if payload["schema_version"] != 1 or not isinstance(
        payload["checksums"], list
    ):
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
    )
    graph_flags = sorted(
        {
            flag
            for node in graph_nodes.values()
            for flag in node["quality_flags"]
        }
    )
    if graph_flags != graph_record["quality_flags"]:
        raise PortableRuntimeError("evidence_graph.quality_flags_mismatch")
    source_record = roles["SOURCE_IDENTITY"][0]
    verify_source_identity(payloads[source_record["logical_id"]], support)
    checksum_record = roles["CHECKSUMS"][0]
    verify_checksums(payloads[checksum_record["logical_id"]], manifest)
    study, report = canonical_replay_outputs(manifest, payloads)
    expected_study = roles["STUDY"][0]
    expected_report = roles["REPORT"][0]
    if json_file_bytes(study) != raw_by_logical_id[expected_study["logical_id"]]:
        raise PortableRuntimeError("study.replay_mismatch")
    if json_file_bytes(report) != raw_by_logical_id[expected_report["logical_id"]]:
        raise PortableRuntimeError("report.replay_mismatch")
    return {
        "status": "PASS",
        "package_id": manifest["package_id"],
        "package_version": manifest["package_version"],
        "manifest_hash": manifest["content_hash"],
        "study_content_hash": study["content_hash"],
        "report_content_hash": report["content_hash"],
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
    for item in manifest["artifacts"]:
        if item["media_type"] == "application/json":
            payloads[item["logical_id"]] = load_json_bytes(
                read_regular_file(root, item["relative_path"]),
                "artifact." + item["logical_id"],
            )
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
