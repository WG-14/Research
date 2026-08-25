"""Portable, immutable packages for the classic research pipeline.

The final strategy/research registries are evidence authorities, but their
historical reproduction recipe was only symbolic: it named external refs and
did not preserve the exact manifest, receipt, result, environment contract, or
licensed input bytes needed on another host.  This module is the executable
bridge.  It creates one deterministic content-addressed archive, verifies it
without consulting the checkout, and replays it through the installed
``market-research`` distribution in an isolated working directory.

The archive never embeds credentials, assertions, trust-store private
material, or account-connected state.  Dataset bytes are included only when a
canonical data-governance admission permits the internal research-package
scope.  Otherwise the manifest exposes an exact content-addressed external
input requirement instead of pretending the package is self-contained.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator, Mapping, cast

from market_research.paths import ResearchPathManager
from market_research.research_composition import builtin_strategy_registry

from .data_governance import (
    DataGovernanceError,
    get_data_governance_record,
    require_data_governance_report_binding,
)
from .datasets.artifact_manifest import (
    PORTABLE_ARTIFACT_RESOLUTION_ENV,
    ArtifactManifest,
    load_artifact_manifest,
)
from .datasets.hashing_contract import artifact_manifest_hash
from .experiment_manifest import (
    ExperimentManifest,
    ManifestValidationError,
    load_manifest_with_registry,
)
from .hashing import report_content_hash_payload, sha256_prefixed
from .reproduction import (
    ReproductionContractError,
    load_reproduction_receipt,
    validate_reproduction_receipt_report_binding,
)

PORTABLE_RESEARCH_PACKAGE_SCHEMA_VERSION = 1
PORTABLE_RESEARCH_PACKAGE_TYPE = "portable_classic_research_package"
PORTABLE_RESEARCH_PACKAGE_HASH_LABEL = "portable_research_package_manifest"
PORTABLE_RESEARCH_VERIFICATION_HASH_LABEL = (
    "portable_research_package_verification_receipt"
)
PORTABLE_RESEARCH_REPRODUCTION_HASH_LABEL = (
    "portable_research_package_reproduction_receipt"
)

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_MAX_JSON_BYTES = 256 * 1024 * 1024
_MAX_DATASET_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 3 * 1024 * 1024 * 1024
_MANIFEST_NAME = "package-manifest.json"
_JSON_SUFFIX = ".json"
_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "api_secret",
        "authorization",
        "client_secret",
        "credential",
        "credentials",
        "passphrase",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)
_SENSITIVE_SUFFIXES = (
    "_access_token",
    "_api_key",
    "_api_secret",
    "_authorization",
    "_passphrase",
    "_password",
    "_private_key",
    "_refresh_token",
    "_secret",
    "_token",
)
_SENSITIVE_ENVIRONMENT_NAME = re.compile(
    r"(?:^|_)(?:ACCESS_TOKEN|API_KEY|API_SECRET|CREDENTIALS?|PASSWORD|PASSWD|"
    r"PRIVATE_KEY|REFRESH_TOKEN|SECRET|TOKEN)(?:_|$)",
    re.IGNORECASE,
)
_PRIVATE_MATERIAL_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN PGP PRIVATE KEY BLOCK-----",
)
_PRIVATE_TOKEN_PATTERNS = (
    re.compile(rb"(?<![A-Za-z0-9])ghp_[A-Za-z0-9]{20,}"),
    re.compile(rb"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"(?i)(?<![A-Za-z0-9])bearer[ \t]+[A-Za-z0-9._~+/-]{20,}"),
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "package_type",
        "package_id",
        "package_version",
        "content_hash",
        "publication_status",
        "replay_eligibility",
        "evidence_scope",
        "experiment_id",
        "experiment_manifest_hash",
        "source_report_hash",
        "reproduction_receipt_hash",
        "dataset_mode",
        "dataset_requirement",
        "code_requirement",
        "external_authority_requirements",
        "completeness",
        "artifacts",
        "reproduction_command",
    }
)
_ARTIFACT_FIELDS = frozenset(
    {"logical_id", "version", "role", "relative_path", "content_hash", "size"}
)
_DATASET_REQUIREMENT_FIELDS = frozenset(
    {
        "mode",
        "artifact_manifest_uri",
        "artifact_manifest_hash",
        "artifact_content_hash",
        "artifact_schema_hash",
        "artifact_identity_hash",
        "included_relative_path",
        "external_input_required",
        "license_evidence",
    }
)
_DATASET_MODES = frozenset({"included", "external_content_addressed"})
_BASE_ARTIFACT_ROLE_GRAPH = {
    "RESULT_PACKAGE": ("source-result", "evidence/source-result.json"),
    "REPRODUCTION_RECEIPT": (
        "reproduction-receipt",
        "evidence/reproduction-receipt.json",
    ),
    "EXPERIMENT_MANIFEST": (
        "experiment-manifest",
        "evidence/experiment-manifest.json",
    ),
    "RESEARCH_SUMMARY": ("research-summary", "evidence/research-summary.json"),
    "HYPOTHESIS_DOCUMENT": ("hypothesis", "evidence/hypothesis.json"),
    "DATA_MANIFEST": ("data-manifest", "evidence/data-manifest.json"),
    "CODE_MANIFEST": ("code-manifest", "evidence/code-manifest.json"),
    "ENVIRONMENT_MANIFEST": (
        "environment-manifest",
        "evidence/environment-manifest.json",
    ),
    "PARAMETER_MANIFEST": ("parameters", "evidence/parameters.json"),
    "RESULT_INDEX": ("result-index", "evidence/result-index.json"),
    "VALIDATION_REPORT": (
        "validation-report",
        "evidence/validation-report.json",
    ),
    "LIMITATIONS": ("limitations", "evidence/limitations.json"),
    "REPRODUCTION_PLAN": (
        "reproduction-plan",
        "evidence/reproduction-plan.json",
    ),
    "DATASET_ARTIFACT_MANIFEST": (
        "dataset-artifact-manifest",
        "dataset/artifact.manifest.json",
    ),
}
_INDEPENDENT_REPRODUCTION_ROLE = {
    "INDEPENDENT_REPRODUCTION": (
        "independent-reproduction",
        "evidence/independent-reproduction.json",
    )
}
_INCLUDED_DATASET_ROLE = {
    "IMMUTABLE_DATASET": ("dataset-artifact", "dataset/candles.sqlite")
}
_REPRODUCTION_COMMAND = (
    "market-research research-reproduce-portable-package "
    "--package /abs/package.mrpkg --workspace /abs/replay-workspace "
    "--out /abs/reproduction.json"
)


class PortableResearchPackageError(ValueError):
    """A portable classic package is incomplete, unsafe, or tampered."""


@dataclass(frozen=True, slots=True)
class PortableResearchPackageVerification:
    status: str
    package_file_hash: str
    package_manifest_hash: str
    experiment_id: str
    experiment_manifest_hash: str
    source_report_hash: str
    reproduction_receipt_hash: str
    dataset_mode: str
    artifact_count: int
    publication_status: str
    replay_eligibility: str
    content_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "artifact_type": "portable_research_package_verification_receipt",
            "status": self.status,
            "package_file_hash": self.package_file_hash,
            "package_manifest_hash": self.package_manifest_hash,
            "experiment_id": self.experiment_id,
            "experiment_manifest_hash": self.experiment_manifest_hash,
            "source_report_hash": self.source_report_hash,
            "reproduction_receipt_hash": self.reproduction_receipt_hash,
            "dataset_mode": self.dataset_mode,
            "artifact_count": self.artifact_count,
            "publication_status": self.publication_status,
            "replay_eligibility": self.replay_eligibility,
            "content_hash": self.content_hash,
        }


def build_portable_research_package(
    *,
    manager: ResearchPathManager,
    result_path: str | Path,
    experiment_manifest_path: str | Path,
    reproduction_receipt_path: str | Path,
    output_path: str | Path,
    dataset_mode: str,
    independent_reproduction_path: str | Path | None = None,
) -> dict[str, object]:
    """Build and create-or-verify one deterministic repository-external archive."""

    if dataset_mode not in _DATASET_MODES:
        raise PortableResearchPackageError("portable_package_dataset_mode_invalid")
    result_file = manager.external_input_path(result_path, label="research result")
    manifest_file = manager.external_input_path(
        experiment_manifest_path, label="experiment manifest"
    )
    receipt_file = manager.external_input_path(
        reproduction_receipt_path, label="reproduction receipt"
    )
    output = _external_output_file(manager, output_path)
    independent_file = (
        manager.external_input_path(
            independent_reproduction_path,
            label="independent reproduction result",
        )
        if independent_reproduction_path is not None
        else None
    )

    result_raw = _read_pinned_file(result_file, _MAX_JSON_BYTES, "source_result")
    manifest_raw = _read_pinned_file(
        manifest_file, _MAX_JSON_BYTES, "experiment_manifest"
    )
    receipt_raw = _read_pinned_file(
        receipt_file, _MAX_JSON_BYTES, "reproduction_receipt"
    )
    for label, raw in (
        ("source_result", result_raw),
        ("experiment_manifest", manifest_raw),
        ("reproduction_receipt", receipt_raw),
    ):
        _reject_secret_material(raw, label, scan_sensitive_environment=True)
    result = _json_object(result_raw, "source_result")
    manifest_payload = _json_object(manifest_raw, "experiment_manifest")
    receipt = _json_object(receipt_raw, "reproduction_receipt")
    _reject_unbundled_execution_inputs(manifest_payload)
    _reject_secrets(result, "source_result")
    _reject_secrets(manifest_payload, "experiment_manifest")
    _reject_secrets(receipt, "reproduction_receipt")

    manifest = _load_manifest(manifest_file)
    loaded_receipt = load_reproduction_receipt(receipt_file)
    if loaded_receipt != receipt:
        raise PortableResearchPackageError("portable_package_receipt_parse_mismatch")
    _validate_source_chain(result=result, manifest=manifest, receipt=receipt)

    independent: dict[str, object] | None = None
    independent_raw: bytes | None = None
    if independent_file is not None:
        independent_raw = _read_pinned_file(
            independent_file,
            _MAX_JSON_BYTES,
            "independent_reproduction",
        )
        independent = _json_object(independent_raw, "independent_reproduction")
        _reject_secret_material(
            independent_raw,
            "independent_reproduction",
            scan_sensitive_environment=True,
        )
        _reject_secrets(independent, "independent_reproduction")
        _validate_independent_reproduction(independent, receipt)

    artifact_manifest = _load_primary_artifact_manifest(manifest)
    artifact_ref = manifest.dataset.artifact_ref
    assert artifact_ref is not None
    artifact_manifest_file = Path(artifact_ref.artifact_manifest_uri)
    artifact_manifest_raw = _read_pinned_file(
        artifact_manifest_file,
        _MAX_JSON_BYTES,
        "dataset_artifact_manifest",
    )
    _reject_secret_material(
        artifact_manifest_raw,
        "dataset_artifact_manifest",
        scan_sensitive_environment=True,
    )
    artifact_file = Path(artifact_manifest.locator.path)

    governance = _dataset_export_governance(
        manager=manager,
        result=result,
        require_inclusion=dataset_mode == "included",
    )
    dataset_bytes: bytes | None = None
    if dataset_mode == "included":
        dataset_bytes = _read_pinned_file(
            artifact_file,
            _MAX_DATASET_BYTES,
            "dataset_artifact",
        )
        _reject_secret_material(
            dataset_bytes,
            "dataset_artifact",
            scan_sensitive_environment=True,
        )

    artifacts: dict[str, tuple[str, str, bytes]] = {}

    def add_json(logical_id: str, role: str, value: object) -> None:
        artifacts[f"evidence/{logical_id}.json"] = (
            logical_id,
            role,
            _canonical_json_file(value),
        )

    add_json("source-result", "RESULT_PACKAGE", result)
    add_json("reproduction-receipt", "REPRODUCTION_RECEIPT", receipt)
    add_json("experiment-manifest", "EXPERIMENT_MANIFEST", manifest_payload)
    if independent is not None:
        add_json(
            "independent-reproduction",
            "INDEPENDENT_REPRODUCTION",
            independent,
        )
    add_json(
        "research-summary", "RESEARCH_SUMMARY", _research_summary(result, manifest)
    )
    add_json(
        "hypothesis", "HYPOTHESIS_DOCUMENT", _hypothesis_document(result, manifest)
    )
    add_json(
        "data-manifest",
        "DATA_MANIFEST",
        _data_manifest(
            result=result,
            manifest=manifest,
            artifact_manifest=artifact_manifest,
            dataset_mode=dataset_mode,
            governance=governance,
        ),
    )
    add_json("code-manifest", "CODE_MANIFEST", _code_manifest(receipt))
    add_json(
        "environment-manifest", "ENVIRONMENT_MANIFEST", _environment_manifest(receipt)
    )
    add_json("parameters", "PARAMETER_MANIFEST", _parameter_manifest(manifest))
    add_json("result-index", "RESULT_INDEX", _result_index(result))
    add_json(
        "validation-report",
        "VALIDATION_REPORT",
        _validation_report(result, independent),
    )
    add_json("limitations", "LIMITATIONS", _limitations(result, manifest, dataset_mode))
    add_json(
        "reproduction-plan",
        "REPRODUCTION_PLAN",
        _reproduction_plan(
            result=result,
            receipt=receipt,
            dataset_mode=dataset_mode,
            independent=independent,
        ),
    )
    artifacts["dataset/artifact.manifest.json"] = (
        "dataset-artifact-manifest",
        "DATASET_ARTIFACT_MANIFEST",
        artifact_manifest_raw,
    )
    if dataset_bytes is not None:
        artifacts["dataset/candles.sqlite"] = (
            "dataset-artifact",
            "IMMUTABLE_DATASET",
            dataset_bytes,
        )

    strict = _strict_environment(receipt)
    replay_eligibility = (
        "INSTALLED_WHEEL_COLD_REPLAY_ELIGIBLE"
        if strict.get("source_layout") == "installed_distribution"
        else "PINNED_REPOSITORY_SOURCE_REQUIRES_EQUIVALENT_SOURCE_ENVIRONMENT"
    )
    evidence_scope = str(receipt.get("evidence_scope") or "research_result")
    authoritative = evidence_scope == "validated_research_result"
    independent_pass = independent is not None and independent.get("status") == "PASS"
    # A portable snapshot cannot prove current registry/principal authority on
    # a disconnected host.  Even a supplied PASS result is retained only as
    # evidence; authoritative publication remains the live canonical registry's
    # decision and is never inferred from caller-owned JSON.
    publication_status = (
        "VALIDATED_EVIDENCE_EXTERNAL_AUTHORITY_UNVERIFIED_NON_PROMOTABLE"
        if authoritative and independent_pass
        else "VALIDATED_AWAITING_CANONICAL_INDEPENDENT_REPRODUCTION_NON_PROMOTABLE"
        if authoritative
        else "NON_PROMOTABLE_RESEARCH_ONLY_PACKAGE"
    )
    completeness = _completeness(
        result=result,
        independent=independent,
        dataset_mode=dataset_mode,
        replay_eligibility=replay_eligibility,
    )
    artifact_rows = [
        {
            "logical_id": logical_id,
            "version": "1",
            "role": role,
            "relative_path": relative_path,
            "content_hash": _hash_bytes(raw),
            "size": len(raw),
        }
        for relative_path, (logical_id, role, raw) in sorted(artifacts.items())
    ]
    dataset_requirement = {
        "mode": dataset_mode,
        "artifact_manifest_uri": str(artifact_manifest_file.resolve()),
        "artifact_manifest_hash": artifact_manifest.artifact_manifest_hash,
        "artifact_content_hash": artifact_manifest.content_hash,
        "artifact_schema_hash": artifact_manifest.schema_hash,
        "artifact_identity_hash": artifact_manifest.artifact_identity_hash,
        "included_relative_path": (
            "dataset/candles.sqlite" if dataset_bytes is not None else None
        ),
        "external_input_required": dataset_bytes is None,
        "license_evidence": governance,
    }
    package_material: dict[str, object] = {
        "schema_version": PORTABLE_RESEARCH_PACKAGE_SCHEMA_VERSION,
        "package_type": PORTABLE_RESEARCH_PACKAGE_TYPE,
        "package_id": str(manifest.experiment_id),
        "package_version": str(receipt["manifest_hash"]),
        "publication_status": publication_status,
        "replay_eligibility": replay_eligibility,
        "evidence_scope": evidence_scope,
        "experiment_id": str(receipt["experiment_id"]),
        "experiment_manifest_hash": str(receipt["manifest_hash"]),
        "source_report_hash": str(receipt["source_report_hash"]),
        "reproduction_receipt_hash": str(receipt["receipt_content_hash"]),
        "dataset_mode": dataset_mode,
        "dataset_requirement": dataset_requirement,
        "code_requirement": _code_requirement(receipt),
        "external_authority_requirements": _external_authority_requirements(receipt),
        "completeness": completeness,
        "artifacts": artifact_rows,
        "reproduction_command": _REPRODUCTION_COMMAND,
    }
    package_manifest = {
        **package_material,
        "content_hash": sha256_prefixed(
            package_material,
            label=PORTABLE_RESEARCH_PACKAGE_HASH_LABEL,
        ),
    }
    archive_members = dict(artifacts)
    archive_members[_MANIFEST_NAME] = (
        "package-manifest",
        "PACKAGE_MANIFEST",
        _canonical_json_file(package_manifest),
    )
    archive_bytes = _deterministic_zip(
        {path: raw for path, (_logical_id, _role, raw) in archive_members.items()}
    )
    _publish_bytes_create_or_verify(output, archive_bytes)
    verification = verify_portable_research_package(
        output,
        external_artifact_manifest_path=(
            artifact_manifest_file
            if dataset_mode == "external_content_addressed"
            else None
        ),
        external_dataset_path=(
            artifact_file if dataset_mode == "external_content_addressed" else None
        ),
    )
    if verification.status != "PASS":  # pragma: no cover - verifier is fail closed.
        raise PortableResearchPackageError("portable_package_post_publish_invalid")
    return {
        "schema_version": 1,
        "status": "BUILT_OR_VERIFIED_EXISTING",
        "package_path": str(output),
        "package_file_hash": _hash_bytes(archive_bytes),
        "package_manifest_hash": package_manifest["content_hash"],
        "publication_status": publication_status,
        "replay_eligibility": replay_eligibility,
        "dataset_mode": dataset_mode,
        "completeness": completeness,
    }


def verify_portable_research_package(
    package_path: str | Path,
    *,
    external_artifact_manifest_path: str | Path | None = None,
    external_dataset_path: str | Path | None = None,
) -> PortableResearchPackageVerification:
    """Verify every archive member and the complete result/receipt call graph."""

    package = Path(package_path).expanduser()
    package_raw = _read_pinned_file(
        package,
        _MAX_ARCHIVE_BYTES,
        "portable_package",
    )
    package_file_hash = _hash_bytes(package_raw)
    members = _read_archive_members(package_raw)
    _reject_secret_material(
        members[_MANIFEST_NAME],
        "package_manifest",
        scan_sensitive_environment=False,
    )
    manifest = _json_object(members[_MANIFEST_NAME], "package_manifest")
    _reject_secrets(manifest, "package_manifest")
    _require_exact_fields(manifest, _MANIFEST_FIELDS, "package_manifest")
    if (
        manifest.get("schema_version") != PORTABLE_RESEARCH_PACKAGE_SCHEMA_VERSION
        or manifest.get("package_type") != PORTABLE_RESEARCH_PACKAGE_TYPE
    ):
        raise PortableResearchPackageError("portable_package_schema_unsupported")
    recorded_manifest_hash = _require_hash(
        manifest.get("content_hash"), "package_manifest.content_hash"
    )
    actual_manifest_hash = sha256_prefixed(
        {key: value for key, value in manifest.items() if key != "content_hash"},
        label=PORTABLE_RESEARCH_PACKAGE_HASH_LABEL,
    )
    if recorded_manifest_hash != actual_manifest_hash:
        raise PortableResearchPackageError("portable_package_manifest_hash_mismatch")

    artifact_rows_raw = manifest.get("artifacts")
    if not isinstance(artifact_rows_raw, list) or not artifact_rows_raw:
        raise PortableResearchPackageError("portable_package_artifacts_required")
    artifact_rows: list[dict[str, object]] = []
    expected_paths: set[str] = {_MANIFEST_NAME}
    prior_sort_key: tuple[str, str] | None = None
    logical_ids: set[str] = set()
    for raw_row in artifact_rows_raw:
        if not isinstance(raw_row, dict):
            raise PortableResearchPackageError("portable_package_artifact_invalid")
        _require_exact_fields(raw_row, _ARTIFACT_FIELDS, "package_artifact")
        row = cast(dict[str, object], raw_row)
        logical_id = _require_id(row.get("logical_id"), "artifact.logical_id")
        relative_path = _require_relative_path(row.get("relative_path"))
        role = _require_text(row.get("role"), "artifact.role")
        sort_key = (relative_path, logical_id)
        if prior_sort_key is not None and sort_key <= prior_sort_key:
            raise PortableResearchPackageError(
                "portable_package_artifacts_not_sorted_unique"
            )
        prior_sort_key = sort_key
        if logical_id in logical_ids or relative_path in expected_paths:
            raise PortableResearchPackageError(
                "portable_package_artifact_identity_duplicate"
            )
        logical_ids.add(logical_id)
        expected_paths.add(relative_path)
        raw = members.get(relative_path)
        if raw is None:
            raise PortableResearchPackageError(
                f"portable_package_artifact_missing:{logical_id}"
            )
        if (
            _require_hash(row.get("content_hash"), "artifact.content_hash")
            != _hash_bytes(raw)
            or not isinstance(row.get("size"), int)
            or isinstance(row.get("size"), bool)
            or row["size"] != len(raw)
            or row.get("version") != "1"
        ):
            raise PortableResearchPackageError(
                f"portable_package_artifact_binding_mismatch:{logical_id}"
            )
        _reject_secret_material(
            raw,
            f"artifact:{logical_id}",
            scan_sensitive_environment=False,
        )
        if relative_path.endswith(_JSON_SUFFIX):
            value = _json_value(raw, f"artifact:{logical_id}")
            _reject_secrets(value, f"artifact:{logical_id}")
        artifact_rows.append(row)
        del role
    if set(members) != expected_paths:
        raise PortableResearchPackageError("portable_package_unmanifested_member")

    by_role: dict[str, str] = {}
    for row in artifact_rows:
        role = str(row["role"])
        if role in by_role:
            raise PortableResearchPackageError(
                f"portable_package_artifact_role_duplicate:{role}"
            )
        by_role[role] = str(row["relative_path"])
    if not set(_BASE_ARTIFACT_ROLE_GRAPH).issubset(by_role):
        raise PortableResearchPackageError("portable_package_required_roles_missing")
    dataset_mode = manifest.get("dataset_mode")
    if dataset_mode not in _DATASET_MODES:
        raise PortableResearchPackageError("portable_package_dataset_mode_invalid")
    expected_role_graph = dict(_BASE_ARTIFACT_ROLE_GRAPH)
    if "INDEPENDENT_REPRODUCTION" in by_role:
        expected_role_graph.update(_INDEPENDENT_REPRODUCTION_ROLE)
    if dataset_mode == "included":
        expected_role_graph.update(_INCLUDED_DATASET_ROLE)
    actual_role_graph = {
        str(row["role"]): (str(row["logical_id"]), str(row["relative_path"]))
        for row in artifact_rows
    }
    if actual_role_graph != expected_role_graph:
        raise PortableResearchPackageError(
            "portable_package_artifact_role_graph_invalid"
        )
    result = _json_object(members[by_role["RESULT_PACKAGE"]], "source_result")
    receipt = _json_object(
        members[by_role["REPRODUCTION_RECEIPT"]], "reproduction_receipt"
    )
    independent: dict[str, object] | None = None
    independent_path = by_role.get("INDEPENDENT_REPRODUCTION")
    if independent_path is not None:
        independent = _json_object(
            members[independent_path], "independent_reproduction"
        )
        _validate_independent_reproduction(independent, receipt)
    experiment_raw = members[by_role["EXPERIMENT_MANIFEST"]]
    experiment_payload = _json_object(experiment_raw, "experiment_manifest")
    _reject_unbundled_execution_inputs(experiment_payload)

    with tempfile.TemporaryDirectory(
        prefix="market-research-package-verify-"
    ) as temporary_directory:
        root = Path(temporary_directory)
        dataset_paths = _prepare_dataset_resolution(
            root=root,
            manifest=manifest,
            members=members,
            by_role=by_role,
            external_artifact_manifest_path=external_artifact_manifest_path,
            external_dataset_path=external_dataset_path,
        )
        experiment_path = root / "experiment-manifest.json"
        experiment_path.write_bytes(experiment_raw)
        with _resolution_environment(dataset_paths["resolution_path"]):
            experiment = _load_manifest(experiment_path)
            loaded_artifact = _load_primary_artifact_manifest(experiment)
            _verify_dataset_requirement(
                manifest=manifest,
                artifact_manifest=loaded_artifact,
                dataset_path=dataset_paths["dataset_path"],
            )
    _validate_source_chain(result=result, manifest=experiment, receipt=receipt)
    _validate_manifest_summary_bindings(
        package_manifest=manifest,
        result=result,
        experiment=experiment,
        receipt=receipt,
        independent=independent,
    )
    _validate_derived_artifact_bindings(
        members=members,
        by_role=by_role,
        result=result,
        experiment=experiment,
        receipt=receipt,
        artifact_manifest=loaded_artifact,
        dataset_mode=cast(str, dataset_mode),
        dataset_governance=cast(
            dict[str, object],
            cast(dict[str, object], manifest["dataset_requirement"])[
                "license_evidence"
            ],
        ),
        independent=independent,
    )
    verification_material = {
        "schema_version": 1,
        "artifact_type": "portable_research_package_verification_receipt",
        "status": "PASS",
        "package_file_hash": package_file_hash,
        "package_manifest_hash": recorded_manifest_hash,
        "experiment_id": str(receipt["experiment_id"]),
        "experiment_manifest_hash": str(receipt["manifest_hash"]),
        "source_report_hash": str(receipt["source_report_hash"]),
        "reproduction_receipt_hash": str(receipt["receipt_content_hash"]),
        "dataset_mode": str(manifest["dataset_mode"]),
        "artifact_count": len(artifact_rows),
        "publication_status": str(manifest["publication_status"]),
        "replay_eligibility": str(manifest["replay_eligibility"]),
    }
    return PortableResearchPackageVerification(
        status="PASS",
        package_file_hash=package_file_hash,
        package_manifest_hash=recorded_manifest_hash,
        experiment_id=str(receipt["experiment_id"]),
        experiment_manifest_hash=str(receipt["manifest_hash"]),
        source_report_hash=str(receipt["source_report_hash"]),
        reproduction_receipt_hash=str(receipt["receipt_content_hash"]),
        dataset_mode=str(manifest["dataset_mode"]),
        artifact_count=len(artifact_rows),
        publication_status=str(manifest["publication_status"]),
        replay_eligibility=str(manifest["replay_eligibility"]),
        content_hash=sha256_prefixed(
            verification_material,
            label=PORTABLE_RESEARCH_VERIFICATION_HASH_LABEL,
        ),
    )


def reproduce_portable_research_package(
    package_path: str | Path,
    *,
    workspace: str | Path,
    external_artifact_manifest_path: str | Path | None = None,
    external_dataset_path: str | Path | None = None,
    verification_id: str | None = None,
    verification_version: str | None = None,
    verifier_assertion_path: str | Path | None = None,
) -> dict[str, object]:
    """Replay through an installed wheel with empty cwd/HOME/PYTHONPATH/cache."""

    package = Path(package_path).expanduser().resolve()
    verification = verify_portable_research_package(
        package,
        external_artifact_manifest_path=external_artifact_manifest_path,
        external_dataset_path=external_dataset_path,
    )
    package_raw = _read_pinned_file(
        package,
        _MAX_ARCHIVE_BYTES,
        "portable_package",
    )
    if _hash_bytes(package_raw) != verification.package_file_hash:
        raise PortableResearchPackageError(
            "portable_package_changed_after_verification"
        )
    members = _read_archive_members(package_raw)
    workspace_path = Path(workspace).expanduser()
    if not workspace_path.is_absolute():
        raise PortableResearchPackageError("portable_replay_workspace_must_be_absolute")
    if workspace_path.exists() and any(workspace_path.iterdir()):
        raise PortableResearchPackageError("portable_replay_workspace_not_empty")
    workspace_path.mkdir(parents=True, exist_ok=True)
    _reject_symlink_chain(workspace_path, "portable_replay_workspace")
    package_manifest = _json_object(members[_MANIFEST_NAME], "package_manifest")
    by_role = {
        str(row["role"]): str(row["relative_path"])
        for row in cast(list[dict[str, object]], package_manifest["artifacts"])
    }
    materialized = workspace_path / "materialized-package"
    materialized.mkdir(mode=0o700)
    for relative_path, raw in sorted(members.items()):
        target = materialized / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        target.chmod(0o400)

    dataset_paths = _prepare_dataset_resolution(
        root=workspace_path / "resolved-inputs",
        manifest=package_manifest,
        members=members,
        by_role=by_role,
        external_artifact_manifest_path=external_artifact_manifest_path,
        external_dataset_path=external_dataset_path,
    )
    receipt = _json_object(
        members[by_role["REPRODUCTION_RECEIPT"]], "reproduction_receipt"
    )
    _json_object(members[by_role["RESULT_PACKAGE"]], "source_result")
    experiment_raw = members[by_role["EXPERIMENT_MANIFEST"]]
    strict = _strict_environment(receipt)
    installed_distribution = _require_installed_wheel_runtime(strict)
    evidence_scope = str(receipt.get("evidence_scope") or "research_result")
    if evidence_scope == "validated_research_result":
        if not (
            verification_id
            and verification_version
            and verifier_assertion_path is not None
        ):
            raise PortableResearchPackageError(
                "portable_validated_replay_requires_external_signed_verifier_assertion"
            )
        run_count = 1
    else:
        if any(
            value is not None
            for value in (
                verification_id,
                verification_version,
                verifier_assertion_path,
            )
        ):
            raise PortableResearchPackageError(
                "portable_research_only_replay_rejects_verifier_arguments"
            )
        run_count = 2

    run_results: list[dict[str, object]] = []
    interpreter_probes: list[dict[str, object]] = []
    for index in range(1, run_count + 1):
        run_root = workspace_path / f"cold-run-{index}"
        cwd = run_root / "cwd"
        home = run_root / "home"
        state = run_root / "state"
        cwd.mkdir(parents=True)
        home.mkdir(parents=True)
        for name in ("data", "artifacts", "reports", "cache", "identity"):
            (state / name).mkdir(parents=True)
        experiment_path = run_root / "experiment-manifest.json"
        experiment_path.write_bytes(experiment_raw)
        baseline_receipt_path = (
            state
            / "reports"
            / "research"
            / verification.experiment_id
            / "reproduction_receipt.json"
        )
        baseline_receipt_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_receipt_path.write_bytes(members[by_role["REPRODUCTION_RECEIPT"]])
        stable = cast(dict[str, object], receipt["stable_fingerprint"])
        report_kind = str(stable["report_kind"])
        source_report_path = baseline_receipt_path.with_name(
            f"{report_kind}_report.json"
        )
        source_report_path.write_bytes(members[by_role["RESULT_PACKAGE"]])
        out_path = run_root / "reproduction-result.json"
        command = [
            sys.executable,
            "-I",
            "-m",
            "market_research",
            "research-reproduce-run",
            "--manifest",
            str(experiment_path),
            "--receipt",
            str(baseline_receipt_path),
            "--out",
            str(out_path),
        ]
        if evidence_scope == "validated_research_result":
            assertion = Path(cast(str | Path, verifier_assertion_path)).expanduser()
            _require_absolute_regular_single_link(assertion, "verifier_assertion")
            command.extend(
                (
                    "--verification-id",
                    cast(str, verification_id),
                    "--verification-version",
                    cast(str, verification_version),
                    "--verifier-assertion",
                    str(assertion),
                )
            )
        environment = _cold_environment(
            strict_environment=strict,
            home=home,
            state=state,
            resolution_path=dataset_paths["resolution_path"],
            validated=evidence_scope == "validated_research_result",
        )
        probe = _isolated_interpreter_probe(
            cwd=cwd,
            home=home,
            cache=state / "cache",
            environment=environment,
            installed_distribution=installed_distribution,
        )
        interpreter_probes.append(probe)
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        try:
            replay = _json_object(
                out_path.read_bytes(), f"portable_replay_result_{index}"
            )
        except (OSError, PortableResearchPackageError) as exc:
            raise PortableResearchPackageError(
                "portable_replay_result_unavailable:"
                + _diagnostic_digest(completed.stdout, completed.stderr)
            ) from exc
        if completed.returncode != 0 or replay.get("status") != "PASS":
            raise PortableResearchPackageError(
                "portable_replay_failed:"
                + str(replay.get("error_code") or replay.get("status") or "unknown")
                + ":"
                + _diagnostic_digest(completed.stdout, completed.stderr)
            )
        run_results.append(replay)

    fingerprints = tuple(
        str(item.get("actual_fingerprint_hash") or "") for item in run_results
    )
    if any(not _HASH.fullmatch(item) for item in fingerprints):
        raise PortableResearchPackageError("portable_replay_fingerprint_missing")
    if len(set(fingerprints)) != 1:
        raise PortableResearchPackageError("portable_replay_nondeterministic")
    expected = str(run_results[0].get("expected_fingerprint_hash") or "")
    if expected != fingerprints[0]:
        raise PortableResearchPackageError("portable_replay_expected_hash_mismatch")
    material: dict[str, object] = {
        "schema_version": 1,
        "artifact_type": "portable_research_package_reproduction_receipt",
        "status": "PASS",
        "package_file_hash": verification.package_file_hash,
        "package_manifest_hash": verification.package_manifest_hash,
        "experiment_id": verification.experiment_id,
        "experiment_manifest_hash": verification.experiment_manifest_hash,
        "source_report_hash": verification.source_report_hash,
        "baseline_reproduction_receipt_hash": (verification.reproduction_receipt_hash),
        "dataset_mode": verification.dataset_mode,
        "cold_run_count": run_count,
        "python_isolated_mode": all(
            probe["isolated_mode"] is True for probe in interpreter_probes
        ),
        "source_tree_used": any(
            probe["source_tree_used"] is True for probe in interpreter_probes
        ),
        "pythonpath_used": any(
            probe["pythonpath_used"] is True for probe in interpreter_probes
        ),
        "cache_preexisting": any(
            probe["cache_preexisting"] is True for probe in interpreter_probes
        ),
        "comparison_scope": "exact_stable_semantic_fingerprint",
        "full_report_and_graph_byte_equality_compared": False,
        "runtime_network_namespace_sandboxed": False,
        "runtime_filesystem_namespace_sandboxed": False,
        "independent_host_reproduction_established": False,
        "installed_distribution": installed_distribution,
        "interpreter_probes": interpreter_probes,
        "expected_fingerprint_hash": expected,
        "actual_fingerprint_hashes": list(fingerprints),
        "mismatch_rows": [],
    }
    return {
        **material,
        "content_hash": sha256_prefixed(
            material,
            label=PORTABLE_RESEARCH_REPRODUCTION_HASH_LABEL,
        ),
    }


def _validate_source_chain(
    *,
    result: dict[str, object],
    manifest: ExperimentManifest,
    receipt: dict[str, object],
) -> None:
    recorded_result_hash = _require_hash(
        result.get("content_hash"), "source_result.content_hash"
    )
    if recorded_result_hash != sha256_prefixed(report_content_hash_payload(result)):
        raise PortableResearchPackageError(
            "portable_package_source_result_hash_mismatch"
        )
    if (
        result.get("experiment_id") != manifest.experiment_id
        or result.get("manifest_hash") != manifest.manifest_hash()
        or receipt.get("experiment_id") != manifest.experiment_id
        or receipt.get("manifest_hash") != manifest.manifest_hash()
        or receipt.get("source_report_hash") != recorded_result_hash
    ):
        raise PortableResearchPackageError("portable_package_source_chain_mismatch")
    try:
        validate_reproduction_receipt_report_binding(report=result, receipt=receipt)
    except ReproductionContractError as exc:
        raise PortableResearchPackageError(
            f"portable_package_reproduction_binding_invalid:{exc}"
        ) from exc


def _validate_independent_reproduction(
    value: Mapping[str, object], receipt: Mapping[str, object]
) -> None:
    if value.get("status") != "PASS" or value.get("mismatches") != []:
        raise PortableResearchPackageError(
            "portable_package_independent_reproduction_not_passed"
        )
    if (
        value.get("baseline_receipt_hash") != receipt.get("receipt_content_hash")
        or value.get("expected_fingerprint_hash")
        != cast(Mapping[str, object], receipt["stable_fingerprint"]).get(
            "stable_fingerprint_hash"
        )
        or value.get("actual_fingerprint_hash")
        != value.get("expected_fingerprint_hash")
    ):
        raise PortableResearchPackageError(
            "portable_package_independent_reproduction_binding_mismatch"
        )


def _load_manifest(path: Path) -> ExperimentManifest:
    try:
        return load_manifest_with_registry(path, registry=builtin_strategy_registry())
    except (ManifestValidationError, OSError, ValueError) as exc:
        raise PortableResearchPackageError(
            f"portable_package_experiment_manifest_invalid:{exc}"
        ) from exc


def _load_primary_artifact_manifest(manifest: ExperimentManifest) -> ArtifactManifest:
    ref = manifest.dataset.artifact_ref
    if ref is None:
        raise PortableResearchPackageError(
            "portable_package_content_addressed_dataset_required"
        )
    try:
        return load_artifact_manifest(
            ref.artifact_manifest_uri, ref.artifact_manifest_hash
        )
    except (OSError, ValueError) as exc:
        raise PortableResearchPackageError(
            f"portable_package_dataset_manifest_invalid:{exc}"
        ) from exc


def _dataset_export_governance(
    *,
    manager: ResearchPathManager,
    result: Mapping[str, object],
    require_inclusion: bool,
) -> dict[str, object]:
    if not require_inclusion:
        return {
            "status": "EXTERNAL_CONTENT_ADDRESSED_INPUT_REQUIRED",
            "policy_ref": None,
            "package_export_decision_ref": None,
        }
    try:
        admission_row = require_data_governance_report_binding(
            manager=manager,
            source=result,
            required_purpose="RESEARCH_PACKAGE_EXPORT",
        )
        admission = cast(dict[str, object], admission_row["payload"])
        decision_ref = cast(dict[str, object], admission["package_export_decision_ref"])
        decision_row = get_data_governance_record(
            manager=manager,
            record_type=str(decision_ref["record_type"]),
            logical_id=str(decision_ref["logical_id"]),
            version=str(decision_ref["version"]),
        )
        decision = cast(dict[str, object], decision_row["payload"])
        policy_ref = cast(dict[str, object], decision["policy_ref"])
        policy_row = get_data_governance_record(
            manager=manager,
            record_type=str(policy_ref["record_type"]),
            logical_id=str(policy_ref["logical_id"]),
            version=str(policy_ref["version"]),
        )
        policy = cast(dict[str, object], policy_row["payload"])
    except (DataGovernanceError, KeyError, TypeError, ValueError) as exc:
        raise PortableResearchPackageError(
            f"portable_package_dataset_inclusion_not_authorized:{exc}"
        ) from exc
    if (
        decision.get("purpose") != "RESEARCH_PACKAGE_EXPORT"
        or decision.get("decision") != "ALLOW"
        or decision.get("distribution_scope") != "INTERNAL_RESEARCH_PACKAGE"
        or policy.get("research_package_export_allowed") is not True
        or policy.get("derivative_retention_allowed") is not True
    ):
        raise PortableResearchPackageError(
            "portable_package_dataset_inclusion_not_authorized"
        )
    return {
        "status": "CANONICAL_INTERNAL_PACKAGE_EXPORT_ALLOWED",
        "admission_ref": {
            "record_type": admission_row["record_type"],
            "logical_id": admission_row["logical_id"],
            "version": admission_row["version"],
            "record_hash": admission_row["record_hash"],
            "row_hash": admission_row["row_hash"],
        },
        "package_export_decision_ref": {
            **decision_ref,
            "row_hash": decision_row["row_hash"],
        },
        "policy_ref": {**policy_ref, "row_hash": policy_row["row_hash"]},
        "license_id": policy["license_id"],
        "terms_hash": policy["terms_hash"],
        "distribution_scope": decision["distribution_scope"],
        "external_export_allowed": policy["external_export_allowed"],
        "redistribution_allowed": policy["redistribution_allowed"],
    }


def _research_summary(
    result: Mapping[str, object], manifest: ExperimentManifest
) -> dict[str, object]:
    hypothesis = result.get("hypothesis_spec")
    question: object = manifest.raw.get("hypothesis")
    if isinstance(hypothesis, dict):
        research_question = hypothesis.get("research_question")
        if isinstance(research_question, dict):
            question = research_question.get("question_text") or question
    validated = result.get("artifact_type") == "validated_research_result"
    return {
        "schema_version": 1,
        "research_question": question,
        "core_conclusion": {
            "selected_candidate_id": result.get("selected_candidate_id"),
            "end_to_end_validation_result": result.get("end_to_end_validation_result"),
            "final_selection_gate_result": result.get("final_selection_gate_result"),
            "statistical_gate_result": result.get("statistical_gate_result"),
        },
        "evidence_level": (
            "VALIDATED_TERMINAL_RESULT" if validated else "RESEARCH_ONLY_RESULT"
        ),
        "application_scope": {
            "market": manifest.market,
            "interval": manifest.interval,
            "research_classification": manifest.research_classification,
            "dataset_splits": _split_ranges(manifest),
        },
        "major_limitations_ref": "evidence/limitations.json",
        "operational_permission": False,
    }


def _split_ranges(manifest: ExperimentManifest) -> dict[str, object]:
    return manifest.dataset.split.as_dict()


def _hypothesis_document(
    result: Mapping[str, object], manifest: ExperimentManifest
) -> dict[str, object]:
    hypothesis = result.get("hypothesis_spec") or manifest.raw.get("hypothesis_spec")
    if isinstance(hypothesis, dict):
        return dict(hypothesis)
    return {
        "schema_version": 1,
        "status": "LEGACY_UNSTRUCTURED_RESEARCH_ONLY",
        "phenomenon": manifest.raw.get("hypothesis"),
        "mechanism": None,
        "expected_direction": None,
        "falsification_criteria": [],
    }


def _data_manifest(
    *,
    result: Mapping[str, object],
    manifest: ExperimentManifest,
    artifact_manifest: ArtifactManifest,
    dataset_mode: str,
    governance: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "dataset_id": artifact_manifest.artifact_id,
        "snapshot_id": manifest.dataset.snapshot_id,
        "artifact_manifest_hash": artifact_manifest.artifact_manifest_hash,
        "artifact_identity_hash": artifact_manifest.artifact_identity_hash,
        "artifact_content_hash": artifact_manifest.content_hash,
        "artifact_schema_hash": artifact_manifest.schema_hash,
        "source_provenance_hash": (
            artifact_manifest.source_provenance.provenance_manifest_hash
        ),
        "source_catalog_hash": (
            artifact_manifest.source_provenance.source_catalog.catalog_hash
        ),
        "source_releases": [
            {
                "provider_id": item.provider_id,
                "dataset_id": item.dataset_id,
                "release_id": item.release_id,
                "received_at": item.received_at,
                "content_hash": item.content_hash,
            }
            for item in artifact_manifest.source_provenance.sources
        ],
        "point_in_time_semantics": dict(artifact_manifest.source_provenance.semantics),
        "universe": manifest.raw.get("universe") or manifest.market,
        "quality_checks": result.get("dataset_quality_reports") or {},
        "split_evidence": result.get("dataset_splits") or {},
        "license_and_export": dict(governance),
        "physical_data_mode": dataset_mode,
    }


def _strict_environment(receipt: Mapping[str, object]) -> dict[str, object]:
    stable = receipt.get("stable_fingerprint")
    if not isinstance(stable, dict):
        raise PortableResearchPackageError("portable_package_fingerprint_missing")
    strict = stable.get("strict_environment")
    if not isinstance(strict, dict):
        raise PortableResearchPackageError(
            "portable_package_strict_environment_missing"
        )
    return cast(dict[str, object], strict)


def _code_manifest(receipt: Mapping[str, object]) -> dict[str, object]:
    strict = _strict_environment(receipt)
    return {
        "schema_version": 1,
        "repository_version": strict.get("repository_version"),
        "source_layout": strict.get("source_layout"),
        "git_commit": strict.get("git_commit"),
        "git_dirty": strict.get("git_dirty"),
        "git_status_hash": strict.get("git_status_hash"),
        "git_diff_hash": strict.get("git_diff_hash"),
        "source_tree_hash": strict.get("source_tree_hash"),
        "source_file_count": strict.get("source_file_count"),
        "source_archive_identity": strict.get("source_archive_identity"),
        "dependency_contract_basis": strict.get("dependency_contract_basis"),
        "declared_dependency_contract_hash": strict.get(
            "declared_dependency_contract_hash"
        ),
        "resolved_dependency_contract_hash": strict.get(
            "resolved_dependency_contract_hash"
        ),
        "resolved_dependency_distribution_identities": strict.get(
            "resolved_dependency_distribution_identities"
        ),
        "dependency_contract_hash": strict.get("dependency_contract_hash"),
        "code_provenance_hash": strict.get("code_provenance_hash"),
        "execution_command": "market-research research-reproduce-portable-package",
    }


def _environment_manifest(receipt: Mapping[str, object]) -> dict[str, object]:
    strict = _strict_environment(receipt)
    return {
        "schema_version": 1,
        "python_version": strict.get("python_version"),
        "python_implementation": cast(
            dict[str, object], strict.get("runtime_semantics") or {}
        ).get("python_implementation"),
        "platform": strict.get("platform"),
        "system": strict.get("system"),
        "machine": strict.get("machine"),
        "runtime_semantics": strict.get("runtime_semantics"),
        "runtime_semantics_hash": strict.get("runtime_semantics_hash"),
        "strict_environment_hash": cast(
            dict[str, object], receipt["stable_fingerprint"]
        ).get("strict_environment_hash"),
        "environment_restore_policy": (
            "install the exact hash-bound wheel/dependency set; replay rejects drift"
        ),
    }


def _parameter_manifest(manifest: ExperimentManifest) -> dict[str, object]:
    raw = manifest.raw
    return {
        "schema_version": 1,
        "parameter_space": raw.get("parameter_space"),
        "data_ranges": raw.get("dataset"),
        "universe": raw.get("universe") or raw.get("market"),
        "cost_model": raw.get("cost_model"),
        "portfolio_policy": raw.get("portfolio_policy"),
        "risk_policy": raw.get("risk_policy"),
        "execution_timing": raw.get("execution_timing"),
        "execution_model": raw.get("execution_model"),
        "walk_forward": raw.get("walk_forward"),
        "final_selection": raw.get("final_selection"),
        "missing_data_policy": cast(dict[str, object], raw.get("dataset") or {}).get(
            "options"
        ),
        "normalization_policy": raw.get("feature_contract"),
        "outlier_policy": raw.get("outlier_policy"),
        "simulation_seed_scope_hash": manifest.simulation_seed_scope_hash(),
        "simulation_policy_hash": manifest.simulation_policy_hash(),
    }


def _result_index(result: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "tables": {
            "candidate_results": "evidence/source-result.json#/candidates",
            "dataset_splits": "evidence/source-result.json#/dataset_splits",
        },
        "graphs": result.get("graphs") or [],
        "performance_series": result.get("equity_curve")
        or result.get("performance_series")
        or [],
        "risk_analysis": result.get("risk_analysis") or result.get("risk_policy") or {},
        "contribution_analysis": result.get("contribution_analysis") or {},
        "sensitivity": result.get("sensitivity_analysis")
        or result.get("validation_experiment_bundle")
        or {},
        "robustness": result.get("best_validation_stress_suite")
        or result.get("stress_suite")
        or {},
        "failed_results": result.get("failed_candidates")
        or result.get("failed_results")
        or [],
        "cost_before_after": result.get("cost_sensitivity")
        or result.get("cost_analysis")
        or {},
    }


def _validation_report(
    result: Mapping[str, object], independent: Mapping[str, object] | None
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "validator": (
            cast(
                dict[str, object], independent.get("independent_verification") or {}
            ).get("verifier_id")
            if independent is not None
            else None
        ),
        "independent_reproduction": dict(independent) if independent else None,
        "gate_results": {
            key: value
            for key, value in result.items()
            if isinstance(key, str) and key.endswith("_gate_result")
        },
        "discovered_issues": (
            independent.get("unresolved_issues") if independent else []
        ),
        "corrections": result.get("corrections") or [],
        "unresolved_issues": (
            independent.get("unresolved_issues") if independent else []
        ),
        "decision_basis": result.get("decision_basis")
        or result.get("end_to_end_validation_result"),
    }


def _limitations(
    result: Mapping[str, object],
    manifest: ExperimentManifest,
    dataset_mode: str,
) -> dict[str, object]:
    explicit: list[object] = []
    for key in (
        "data_limitations",
        "execution_limitations",
        "statistical_evidence_limitations",
        "known_limitations",
    ):
        value = result.get(key)
        if value not in (None, {}, []):
            explicit.append({key: value})
    return {
        "schema_version": 1,
        "data_limitations": result.get("data_limitations") or [],
        "sample_limitations": {"split_ranges": _split_ranges(manifest)},
        "cost_estimation_limitations": result.get("cost_limitations") or [],
        "market_structure_dependencies": result.get("execution_limitations") or [],
        "inapplicable_environments": result.get("inapplicable_environments") or [],
        "unverified_risks": result.get("unverified_risks") or [],
        "explicit_source_limitations": explicit,
        "portable_package_limitations": [
            (
                "dataset bytes are not bundled; an exact external content-addressed "
                "input is required"
                if dataset_mode == "external_content_addressed"
                else "dataset export authority is limited to the recorded internal "
                "research-package scope"
            ),
            "validated terminal replay additionally requires external signed identity, "
            "public trust, and the shared one-use holdout authority",
            "cold replay compares the exact stable semantic fingerprint; it does not "
            "claim byte equality for presentation graphs or the complete report",
            "the replay process uses Python isolated mode but does not create a separate "
            "host, filesystem namespace, or network namespace",
        ],
    }


def _reproduction_plan(
    *,
    result: Mapping[str, object],
    receipt: Mapping[str, object],
    dataset_mode: str,
    independent: Mapping[str, object] | None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "single_command": (
            "market-research research-reproduce-portable-package "
            "--package /abs/package.mrpkg --workspace /abs/workspace "
            "--out /abs/reproduction.json"
        ),
        "steps": [
            "verify_archive_and_every_content_hash",
            "verify_result_manifest_receipt_chain",
            "verify_or_resolve_licensed_dataset",
            "verify_installed_code_and_runtime_identity",
            "execute_offline_pipeline_with_python_isolated_mode",
            "compare_exact_stable_fingerprint",
            "repeat_nonterminal_replay_and_compare_hashes",
        ],
        "network_access": "forbidden_by_contract",
        "dataset_mode": dataset_mode,
        "expected_source_report_hash": receipt["source_report_hash"],
        "expected_fingerprint_hash": cast(
            dict[str, object], receipt["stable_fingerprint"]
        )["stable_fingerprint_hash"],
        "tolerance": {
            "stable_fingerprint": "exact_hash_match",
            "mismatch_rows": [],
        },
        "independent_reproduction_status": (
            independent.get("status") if independent else "NOT_PROVIDED"
        ),
        "result_artifact_type": result.get("artifact_type"),
    }


def _code_requirement(receipt: Mapping[str, object]) -> dict[str, object]:
    strict = _strict_environment(receipt)
    return {
        "source_layout": strict.get("source_layout"),
        "repository_version": strict.get("repository_version"),
        "git_commit": strict.get("git_commit"),
        "source_tree_hash": strict.get("source_tree_hash"),
        "dependency_contract_hash": strict.get("dependency_contract_hash"),
        "resolved_dependency_contract_hash": strict.get(
            "resolved_dependency_contract_hash"
        ),
        "resolved_dependency_distribution_identities": strict.get(
            "resolved_dependency_distribution_identities"
        ),
    }


def _external_authority_requirements(
    receipt: Mapping[str, object],
) -> dict[str, object]:
    terminal = receipt.get("evidence_scope") == "validated_research_result"
    return {
        "signed_verifier_assertion": terminal,
        "independent_verifier_public_trust": terminal,
        "shared_final_holdout_authority": terminal,
        "private_keys_in_package": False,
        "credentials_in_package": False,
        "network_market_data_collection": False,
    }


def _completeness(
    *,
    result: Mapping[str, object],
    independent: Mapping[str, object] | None,
    dataset_mode: str,
    replay_eligibility: str,
) -> dict[str, object]:
    missing: list[str] = []
    if result.get("artifact_type") == "validated_research_result":
        if independent is None:
            missing.append("independent_reproduction_result")
    else:
        missing.append("validated_terminal_result")
    if dataset_mode != "included":
        missing.append("bundled_dataset_bytes")
    if replay_eligibility != "INSTALLED_WHEEL_COLD_REPLAY_ELIGIBLE":
        missing.append("installed_distribution_baseline_identity")
    result_index = _result_index(result)
    for field in (
        "graphs",
        "performance_series",
        "risk_analysis",
        "contribution_analysis",
        "sensitivity",
        "robustness",
        "failed_results",
        "cost_before_after",
    ):
        if result_index[field] in ({}, []):
            missing.append(f"result_{field}")
    return {
        "status": "COMPLETE" if not missing else "INCOMPLETE",
        "missing_or_unverified": sorted(set(missing)),
        "h01_sections": [
            "research_summary",
            "hypothesis_document",
            "data_manifest",
            "code_manifest",
            "experiment_manifest",
            "result_package",
            "validation_report",
            "limitations",
            "reproduction_command",
        ],
    }


def _reject_unbundled_execution_inputs(value: Mapping[str, object]) -> None:
    dataset = value.get("dataset")
    if not isinstance(dataset, dict):
        raise PortableResearchPackageError("portable_package_dataset_section_missing")
    extra = [
        key
        for key in ("top_of_book", "depth")
        if dataset.get(key) not in (None, {}, [])
    ]
    for key in ("corporate_action_set", "universe", "market_calendar", "etf_nav"):
        section = value.get(key)
        if isinstance(section, dict) and any(
            isinstance(field, str) and (field.endswith("_uri") or field == "source_uri")
            for field in section
        ):
            extra.append(key)
    if extra:
        raise PortableResearchPackageError(
            "portable_package_additional_execution_inputs_not_portably_bound:"
            + ",".join(sorted(set(extra)))
        )


def _prepare_dataset_resolution(
    *,
    root: Path,
    manifest: Mapping[str, object],
    members: Mapping[str, bytes],
    by_role: Mapping[str, str],
    external_artifact_manifest_path: str | Path | None,
    external_dataset_path: str | Path | None,
) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    requirement = manifest.get("dataset_requirement")
    if not isinstance(requirement, dict):
        raise PortableResearchPackageError(
            "portable_package_dataset_requirement_invalid"
        )
    dataset_mode = manifest.get("dataset_mode")
    if dataset_mode == "included":
        if (
            external_artifact_manifest_path is not None
            or external_dataset_path is not None
        ):
            raise PortableResearchPackageError(
                "portable_package_included_dataset_rejects_external_override"
            )
        manifest_bytes = members[by_role["DATASET_ARTIFACT_MANIFEST"]]
        dataset_member = by_role.get("IMMUTABLE_DATASET")
        if dataset_member is None:
            raise PortableResearchPackageError(
                "portable_package_dataset_member_missing"
            )
        dataset_bytes = members[dataset_member]
    elif dataset_mode == "external_content_addressed":
        if external_artifact_manifest_path is None or external_dataset_path is None:
            raise PortableResearchPackageError(
                "portable_package_external_dataset_inputs_required"
            )
        external_manifest = Path(external_artifact_manifest_path).expanduser()
        external_dataset = Path(external_dataset_path).expanduser()
        _require_absolute_regular_single_link(external_manifest, "external_manifest")
        _require_absolute_regular_single_link(external_dataset, "external_dataset")
        manifest_bytes = _read_pinned_file(
            external_manifest, _MAX_JSON_BYTES, "external_manifest"
        )
        dataset_bytes = _read_pinned_file(
            external_dataset, _MAX_DATASET_BYTES, "external_dataset"
        )
        if _hash_bytes(manifest_bytes) != _hash_bytes(
            members[by_role["DATASET_ARTIFACT_MANIFEST"]]
        ):
            raise PortableResearchPackageError(
                "portable_package_external_manifest_bytes_mismatch"
            )
    else:
        raise PortableResearchPackageError("portable_package_dataset_mode_invalid")
    dataset_root = root / "dataset"
    dataset_root.mkdir()
    resolved_manifest = dataset_root / "artifact.manifest.json"
    resolved_dataset = dataset_root / "candles.sqlite"
    resolved_manifest.write_bytes(manifest_bytes)
    resolved_dataset.write_bytes(dataset_bytes)
    original_manifest_uri = _require_text(
        requirement.get("artifact_manifest_uri"),
        "dataset_requirement.artifact_manifest_uri",
    )
    original_manifest_payload = _json_object(
        manifest_bytes, "dataset_artifact_manifest"
    )
    original_artifact = cast(dict[str, object], original_manifest_payload["artifact"])
    original_artifact_uri = _require_text(
        original_artifact.get("uri"), "dataset_artifact.uri"
    )
    resolution_material: dict[str, object] = {
        "schema_version": 1,
        "artifact_type": "portable_artifact_resolution",
        "entries": [
            {
                "original_manifest_uri": original_manifest_uri,
                "artifact_manifest_hash": requirement["artifact_manifest_hash"],
                "resolved_manifest_uri": str(resolved_manifest.resolve()),
                "original_artifact_uri": original_artifact_uri,
                "artifact_content_hash": requirement["artifact_content_hash"],
                "resolved_artifact_uri": str(resolved_dataset.resolve()),
            }
        ],
    }
    resolution = {
        **resolution_material,
        "content_hash": artifact_manifest_hash(resolution_material),
    }
    resolution_path = root / "artifact-resolution.json"
    resolution_path.write_bytes(_canonical_json_file(resolution))
    return {
        "resolution_path": resolution_path,
        "manifest_path": resolved_manifest,
        "dataset_path": resolved_dataset,
    }


def _verify_dataset_requirement(
    *,
    manifest: Mapping[str, object],
    artifact_manifest: ArtifactManifest,
    dataset_path: Path,
) -> None:
    requirement = cast(dict[str, object], manifest["dataset_requirement"])
    if (
        artifact_manifest.artifact_manifest_hash
        != requirement.get("artifact_manifest_hash")
        or artifact_manifest.content_hash != requirement.get("artifact_content_hash")
        or artifact_manifest.schema_hash != requirement.get("artifact_schema_hash")
        or artifact_manifest.artifact_identity_hash
        != requirement.get("artifact_identity_hash")
        or Path(artifact_manifest.locator.path).resolve() != dataset_path.resolve()
    ):
        raise PortableResearchPackageError(
            "portable_package_dataset_requirement_mismatch"
        )
    # Adapter verification performs the complete canonical SQLite row scan and
    # schema verification.  Calling it through the production adapter keeps the
    # package verifier on the exact same data authority as replay.
    from .dataset_snapshot import FrozenSQLiteCandleAdapter
    from .datasets.contracts import DatasetArtifactHandle, DatasetArtifactRef

    handle = DatasetArtifactHandle(
        reference=DatasetArtifactRef(
            str(requirement["artifact_manifest_uri"]),
            str(requirement["artifact_manifest_hash"]),
        ),
        manifest=artifact_manifest,
    )
    try:
        FrozenSQLiteCandleAdapter().verify(handle)
    except (OSError, ValueError) as exc:
        raise PortableResearchPackageError(
            f"portable_package_dataset_verification_failed:{exc}"
        ) from exc


def _validate_derived_artifact_bindings(
    *,
    members: Mapping[str, bytes],
    by_role: Mapping[str, str],
    result: Mapping[str, object],
    experiment: ExperimentManifest,
    receipt: Mapping[str, object],
    artifact_manifest: ArtifactManifest,
    dataset_mode: str,
    dataset_governance: Mapping[str, object],
    independent: Mapping[str, object] | None,
) -> None:
    """Rebuild every presentation artifact from the verified source graph.

    Artifact hashes alone only prove internal archive consistency: a caller can
    otherwise rewrite a summary or limitation and recompute both hash layers.
    Exact canonical reconstruction binds every derived H-section to the verified
    result, experiment manifest, receipt, dataset authority, and optional
    independent-reproduction object.
    """

    expected_by_role = {
        "RESEARCH_SUMMARY": _canonical_json_file(_research_summary(result, experiment)),
        "HYPOTHESIS_DOCUMENT": _canonical_json_file(
            _hypothesis_document(result, experiment)
        ),
        "DATA_MANIFEST": _canonical_json_file(
            _data_manifest(
                result=result,
                manifest=experiment,
                artifact_manifest=artifact_manifest,
                dataset_mode=dataset_mode,
                governance=dataset_governance,
            )
        ),
        "CODE_MANIFEST": _canonical_json_file(_code_manifest(receipt)),
        "ENVIRONMENT_MANIFEST": _canonical_json_file(_environment_manifest(receipt)),
        "PARAMETER_MANIFEST": _canonical_json_file(_parameter_manifest(experiment)),
        "RESULT_INDEX": _canonical_json_file(_result_index(result)),
        "VALIDATION_REPORT": _canonical_json_file(
            _validation_report(result, independent)
        ),
        "LIMITATIONS": _canonical_json_file(
            _limitations(result, experiment, dataset_mode)
        ),
        "REPRODUCTION_PLAN": _canonical_json_file(
            _reproduction_plan(
                result=result,
                receipt=receipt,
                dataset_mode=dataset_mode,
                independent=independent,
            )
        ),
    }
    for role, expected in expected_by_role.items():
        if members[by_role[role]] != expected:
            raise PortableResearchPackageError(
                f"portable_package_derived_artifact_mismatch:{role}"
            )


def _validate_manifest_summary_bindings(
    *,
    package_manifest: Mapping[str, object],
    result: Mapping[str, object],
    experiment: ExperimentManifest,
    receipt: Mapping[str, object],
    independent: Mapping[str, object] | None,
) -> None:
    if (
        package_manifest.get("package_id") != experiment.experiment_id
        or package_manifest.get("package_version") != experiment.manifest_hash()
        or package_manifest.get("experiment_id") != receipt.get("experiment_id")
        or package_manifest.get("experiment_manifest_hash")
        != receipt.get("manifest_hash")
        or package_manifest.get("source_report_hash") != result.get("content_hash")
        or package_manifest.get("reproduction_receipt_hash")
        != receipt.get("receipt_content_hash")
    ):
        raise PortableResearchPackageError("portable_package_summary_binding_mismatch")
    evidence_scope = str(receipt.get("evidence_scope") or "research_result")
    strict = _strict_environment(receipt)
    expected_replay_eligibility = (
        "INSTALLED_WHEEL_COLD_REPLAY_ELIGIBLE"
        if strict.get("source_layout") == "installed_distribution"
        else "PINNED_REPOSITORY_SOURCE_REQUIRES_EQUIVALENT_SOURCE_ENVIRONMENT"
    )
    validated = evidence_scope == "validated_research_result"
    expected_publication_status = (
        "VALIDATED_EVIDENCE_EXTERNAL_AUTHORITY_UNVERIFIED_NON_PROMOTABLE"
        if validated and independent is not None
        else "VALIDATED_AWAITING_CANONICAL_INDEPENDENT_REPRODUCTION_NON_PROMOTABLE"
        if validated
        else "NON_PROMOTABLE_RESEARCH_ONLY_PACKAGE"
    )
    publication_status = package_manifest.get("publication_status")
    if (
        package_manifest.get("evidence_scope") != evidence_scope
        or package_manifest.get("replay_eligibility") != expected_replay_eligibility
        or publication_status != expected_publication_status
        or "AUTHORITATIVE" in str(publication_status)
    ):
        raise PortableResearchPackageError(
            "portable_package_offline_authority_contract_invalid"
        )
    dataset_mode = package_manifest.get("dataset_mode")
    requirement = package_manifest.get("dataset_requirement")
    if (
        dataset_mode not in _DATASET_MODES
        or not isinstance(requirement, dict)
        or set(requirement) != _DATASET_REQUIREMENT_FIELDS
        or requirement.get("mode") != dataset_mode
        or requirement.get("external_input_required")
        is not (dataset_mode == "external_content_addressed")
        or requirement.get("included_relative_path")
        != ("dataset/candles.sqlite" if dataset_mode == "included" else None)
        or not isinstance(requirement.get("license_evidence"), dict)
    ):
        raise PortableResearchPackageError(
            "portable_package_dataset_transport_contract_invalid"
        )
    expected_completeness = _completeness(
        result=result,
        independent=independent,
        dataset_mode=cast(str, dataset_mode),
        replay_eligibility=expected_replay_eligibility,
    )
    if (
        package_manifest.get("code_requirement") != _code_requirement(receipt)
        or package_manifest.get("external_authority_requirements")
        != _external_authority_requirements(receipt)
        or package_manifest.get("completeness") != expected_completeness
        or package_manifest.get("reproduction_command") != _REPRODUCTION_COMMAND
    ):
        raise PortableResearchPackageError(
            "portable_package_derived_manifest_binding_mismatch"
        )
    # ``validate_reproduction_receipt_report_binding`` above validates the
    # complete hash-bound terminal source contract.  We intentionally do not
    # call the live-authority validator in a degraded offline mode and do not
    # filter its reasons: canonical registry/principal validation is an exact
    # external prerequisite recorded by the package manifest.


def _require_installed_wheel_runtime(
    strict_environment: Mapping[str, object],
) -> dict[str, object]:
    if strict_environment.get("source_layout") != "installed_distribution":
        raise PortableResearchPackageError(
            "portable_replay_baseline_not_created_from_installed_distribution"
        )
    try:
        distribution = importlib.metadata.distribution("market-research")
    except importlib.metadata.PackageNotFoundError as exc:
        raise PortableResearchPackageError(
            "portable_replay_installed_distribution_missing"
        ) from exc
    module_path = Path(__file__).resolve()
    prefix = Path(sys.prefix).resolve()
    try:
        module_path.relative_to(prefix)
    except ValueError as exc:
        raise PortableResearchPackageError(
            "portable_replay_source_or_editable_import_rejected"
        ) from exc
    distribution_files = distribution.files
    if distribution_files is None:
        raise PortableResearchPackageError(
            "portable_replay_installed_distribution_record_missing"
        )
    package_paths = tuple(distribution_files)
    owned_files = {
        Path(str(distribution.locate_file(item))).resolve()
        for item in package_paths
        if Path(str(distribution.locate_file(item))).is_file()
    }
    if module_path not in owned_files:
        raise PortableResearchPackageError(
            "portable_replay_module_not_owned_by_installed_distribution"
        )
    if any(
        item.suffix == ".pth" and "editable" in item.name.lower()
        for item in package_paths
    ):
        raise PortableResearchPackageError(
            "portable_replay_editable_distribution_rejected"
        )
    direct_url = distribution.read_text("direct_url.json")
    if direct_url is not None:
        try:
            direct = _json_object(
                direct_url.encode("utf-8"), "installed_distribution_direct_url"
            )
        except PortableResearchPackageError as exc:
            raise PortableResearchPackageError(
                "portable_replay_installed_distribution_metadata_invalid"
            ) from exc
        if (
            cast(dict[str, object], direct.get("dir_info") or {}).get("editable")
            is True
        ):
            raise PortableResearchPackageError(
                "portable_replay_editable_distribution_rejected"
            )
    identities = strict_environment.get("resolved_dependency_distribution_identities")
    if not isinstance(identities, list):
        raise PortableResearchPackageError(
            "portable_replay_baseline_distribution_identities_missing"
        )
    matching_identities = [
        row
        for row in identities
        if isinstance(row, dict)
        and str(row.get("name") or "").lower().replace("_", "-") == "market-research"
        and row.get("version") == distribution.version
    ]
    if len(matching_identities) != 1:
        raise PortableResearchPackageError(
            "portable_replay_baseline_distribution_identity_not_unique"
        )
    return {
        "name": distribution.metadata["Name"],
        "version": distribution.version,
        "module_origin": str(module_path),
        "python_prefix": str(prefix),
        "record_file_count": len(package_paths),
        "baseline_content_identity": dict(matching_identities[0]),
        "editable": False,
    }


def _isolated_interpreter_probe(
    *,
    cwd: Path,
    home: Path,
    cache: Path,
    environment: Mapping[str, str],
    installed_distribution: Mapping[str, object],
) -> dict[str, object]:
    """Measure the actual child interpreter before executing research code."""

    precondition = {
        "cwd_empty": _directory_is_empty(cwd),
        "home_empty": _directory_is_empty(home),
        "cache_empty": _directory_is_empty(cache),
    }
    if not all(precondition.values()):
        raise PortableResearchPackageError(
            "portable_replay_cold_precondition_not_satisfied"
        )
    probe_program = "\n".join(
        (
            "import importlib.metadata, json, os, pathlib, sys, sysconfig",
            "import market_research.research.portable_research_package as portable",
            "distribution = importlib.metadata.distribution('market-research')",
            "module_origin = pathlib.Path(portable.__file__).resolve()",
            "owned = {pathlib.Path(str(distribution.locate_file(item))).resolve() for item in (distribution.files or ())}",
            "cwd = pathlib.Path.cwd()",
            "home = pathlib.Path(os.environ['HOME'])",
            "cache = pathlib.Path(os.environ['RESEARCH_CACHE_ROOT'])",
            "print(json.dumps({",
            "'schema_version': 1,",
            "'executable': sys.executable,",
            "'prefix': str(pathlib.Path(sys.prefix).resolve()),",
            "'base_prefix': str(pathlib.Path(sys.base_prefix).resolve()),",
            "'stdlib': str(pathlib.Path(sysconfig.get_path('stdlib')).resolve()),",
            "'platstdlib': str(pathlib.Path(sysconfig.get_path('platstdlib')).resolve()),",
            "'stdlib_zip': str((pathlib.Path(sysconfig.get_path('stdlib')).parent / f'python{sys.version_info.major}{sys.version_info.minor}.zip').resolve()),",
            "'isolated': bool(sys.flags.isolated),",
            "'safe_path': bool(sys.flags.safe_path),",
            "'no_user_site': bool(sys.flags.no_user_site),",
            "'module_origin': str(module_origin),",
            "'distribution_name': distribution.metadata['Name'],",
            "'distribution_version': distribution.version,",
            "'distribution_owned_module': module_origin in owned,",
            "'cwd': str(cwd),",
            "'home': str(home),",
            "'cache': str(cache),",
            "'cwd_empty': not any(cwd.iterdir()),",
            "'home_empty': not any(home.iterdir()),",
            "'cache_empty': not any(cache.iterdir()),",
            "'pythonpath_environment': os.environ.get('PYTHONPATH'),",
            "'sys_path': list(sys.path),",
            "}, sort_keys=True, separators=(',', ':')))",
        )
    )
    completed = subprocess.run(
        (sys.executable, "-I", "-c", probe_program),
        cwd=cwd,
        env=dict(environment),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    try:
        probe = _json_object(
            completed.stdout.encode("utf-8"),
            "portable_replay_interpreter_probe",
        )
    except PortableResearchPackageError as exc:
        raise PortableResearchPackageError(
            "portable_replay_interpreter_probe_unavailable:"
            + _diagnostic_digest(completed.stdout, completed.stderr)
        ) from exc
    if completed.returncode != 0:
        raise PortableResearchPackageError(
            "portable_replay_interpreter_probe_failed:"
            + _diagnostic_digest(completed.stdout, completed.stderr)
        )
    sys_path = probe.get("sys_path")
    if not isinstance(sys_path, list) or not all(
        isinstance(item, str) for item in sys_path
    ):
        raise PortableResearchPackageError("portable_replay_interpreter_probe_invalid")
    resolved_cwd = str(cwd.resolve())
    resolved_home = str(home.resolve())
    resolved_cache = str(cache.resolve())
    path_fields = ("prefix", "base_prefix", "stdlib", "platstdlib", "stdlib_zip")
    if not all(isinstance(probe.get(field), str) for field in path_fields):
        raise PortableResearchPackageError("portable_replay_interpreter_probe_invalid")
    prefix = Path(cast(str, probe["prefix"])).resolve()
    stdlib = Path(cast(str, probe["stdlib"])).resolve()
    platstdlib = Path(cast(str, probe["platstdlib"])).resolve()
    stdlib_zip = Path(cast(str, probe["stdlib_zip"])).resolve()

    def allowed_runtime_path(raw: str) -> bool:
        candidate = Path(raw)
        if not candidate.is_absolute():
            return False
        resolved = candidate.resolve()
        if resolved == stdlib_zip:
            return True
        for root in (prefix, stdlib, platstdlib):
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            return True
        return False

    if (
        probe.get("isolated") is not True
        or probe.get("safe_path") is not True
        or probe.get("no_user_site") is not True
        or probe.get("distribution_owned_module") is not True
        or probe.get("module_origin") != installed_distribution.get("module_origin")
        or probe.get("distribution_version") != installed_distribution.get("version")
        or probe.get("prefix") != installed_distribution.get("python_prefix")
        or probe.get("cwd") != resolved_cwd
        or probe.get("home") != resolved_home
        or probe.get("cache") != resolved_cache
        or probe.get("cwd_empty") is not True
        or probe.get("home_empty") is not True
        or probe.get("cache_empty") is not True
        or probe.get("pythonpath_environment") not in (None, "")
        or "" in sys_path
        or resolved_cwd in sys_path
        or not all(allowed_runtime_path(item) for item in sys_path)
    ):
        raise PortableResearchPackageError(
            "portable_replay_interpreter_isolation_invalid"
        )
    return {
        "schema_version": 1,
        "executable": probe["executable"],
        "python_prefix": probe["prefix"],
        "python_base_prefix": probe["base_prefix"],
        "stdlib": probe["stdlib"],
        "platstdlib": probe["platstdlib"],
        "module_origin": probe["module_origin"],
        "distribution_name": probe["distribution_name"],
        "distribution_version": probe["distribution_version"],
        "distribution_owned_module": True,
        "isolated_mode": True,
        "safe_path": True,
        "no_user_site": True,
        "empty_cwd_precondition": precondition["cwd_empty"],
        "empty_home_precondition": precondition["home_empty"],
        "empty_cache_precondition": precondition["cache_empty"],
        "source_tree_used": False,
        "pythonpath_used": False,
        "cache_preexisting": False,
        "sys_path": sys_path,
    }


def _directory_is_empty(path: Path) -> bool:
    return path.is_dir() and not any(path.iterdir())


def _cold_environment(
    *,
    strict_environment: Mapping[str, object],
    home: Path,
    state: Path,
    resolution_path: Path,
    validated: bool,
) -> dict[str, str]:
    runtime = strict_environment.get("runtime_semantics")
    result_environment = (
        cast(dict[str, object], runtime).get("result_affecting_environment")
        if isinstance(runtime, dict)
        else None
    )
    fixed = {
        str(key): str(value)
        for key, value in cast(dict[str, object], result_environment or {}).items()
    }
    environment = {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), "/usr/bin", "/bin")),
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "",
        "RESEARCH_DATA_ROOT": str(state / "data"),
        "RESEARCH_ARTIFACT_ROOT": str(state / "artifacts"),
        "RESEARCH_REPORT_ROOT": str(state / "reports"),
        "RESEARCH_CACHE_ROOT": str(state / "cache"),
        "RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH": str(
            state / "identity" / "experiment-identities.jsonl"
        ),
        PORTABLE_ARTIFACT_RESOLUTION_ENV: str(resolution_path),
        **fixed,
    }
    if validated:
        holdout = os.environ.get("RESEARCH_FINAL_HOLDOUT_REGISTRY_PATH")
        if not holdout or not Path(holdout).is_absolute():
            raise PortableResearchPackageError(
                "portable_validated_replay_shared_holdout_authority_required"
            )
        environment["RESEARCH_FINAL_HOLDOUT_REGISTRY_PATH"] = holdout
        for name in (
            "RESEARCH_INDEPENDENT_VERIFIER_TRUST_STORE_PATH",
            "RESEARCH_INDEPENDENT_VERIFIER_TRUST_STORE_SHA256",
            "RESEARCH_INDEPENDENT_VERIFIER_KEY_ROOT",
            "RESEARCH_RUNTIME_PROFILE",
        ):
            value = os.environ.get(name)
            if value:
                environment[name] = value
    else:
        environment["RESEARCH_FINAL_HOLDOUT_REGISTRY_PATH"] = str(
            state / "identity" / "final-holdout.jsonl"
        )
    return environment


@contextmanager
def _resolution_environment(path: Path) -> Iterator[None]:
    prior = os.environ.get(PORTABLE_ARTIFACT_RESOLUTION_ENV)
    os.environ[PORTABLE_ARTIFACT_RESOLUTION_ENV] = str(path)
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop(PORTABLE_ARTIFACT_RESOLUTION_ENV, None)
        else:
            os.environ[PORTABLE_ARTIFACT_RESOLUTION_ENV] = prior


def _read_archive_members(raw: bytes) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw), mode="r") as archive:
            if archive.comment != b"":
                raise PortableResearchPackageError(
                    "portable_package_archive_metadata_invalid"
                )
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if names != sorted(names) or len(names) != len(set(names)):
                raise PortableResearchPackageError(
                    "portable_package_archive_members_not_sorted_unique"
                )
            if _MANIFEST_NAME not in names:
                raise PortableResearchPackageError("portable_package_manifest_missing")
            total = 0
            members: dict[str, bytes] = {}
            for info in infos:
                _require_relative_path(info.filename)
                if (
                    info.is_dir()
                    or info.compress_type != zipfile.ZIP_STORED
                    or info.date_time != (1980, 1, 1, 0, 0, 0)
                    or info.create_system != 3
                    or info.external_attr != (stat.S_IFREG | 0o400) << 16
                    or info.extra != b""
                    or info.comment != b""
                    or info.flag_bits != 0
                ):
                    raise PortableResearchPackageError(
                        "portable_package_archive_member_invalid"
                    )
                if info.file_size < 0 or info.file_size > _MAX_DATASET_BYTES:
                    raise PortableResearchPackageError(
                        "portable_package_archive_member_too_large"
                    )
                total += info.file_size
                if total > _MAX_ARCHIVE_BYTES:
                    raise PortableResearchPackageError(
                        "portable_package_archive_expanded_too_large"
                    )
                members[info.filename] = archive.read(info)
            return members
    except PortableResearchPackageError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise PortableResearchPackageError("portable_package_archive_invalid") from exc


def _deterministic_zip(members: Mapping[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=zipfile.ZIP_STORED,
        allowZip64=True,
    ) as archive:
        for name, raw in sorted(members.items()):
            _require_relative_path(name)
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o400) << 16
            archive.writestr(info, raw)
    value = buffer.getvalue()
    if len(value) > _MAX_ARCHIVE_BYTES:
        raise PortableResearchPackageError("portable_package_archive_too_large")
    return value


def _publish_bytes_create_or_verify(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_chain(path.parent, "portable_package_parent")
    if path.exists():
        _require_absolute_regular_single_link(path, "portable_package")
        if _read_pinned_file(path, _MAX_ARCHIVE_BYTES, "portable_package") != payload:
            raise PortableResearchPackageError("portable_package_identity_conflict")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o400)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if (
                _read_pinned_file(path, _MAX_ARCHIVE_BYTES, "portable_package")
                != payload
            ):
                raise PortableResearchPackageError("portable_package_identity_conflict")
        else:
            directory = os.open(
                path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _read_pinned_file(path: Path, maximum: int, label: str) -> bytes:
    _require_absolute_regular_single_link(path, label)
    before = path.stat()
    if before.st_size > maximum:
        raise PortableResearchPackageError(f"{label}_too_large")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PortableResearchPackageError(f"{label}_unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise PortableResearchPackageError(f"{label}_too_large")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        or (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
    ):
        raise PortableResearchPackageError(f"{label}_changed_during_read")
    return b"".join(chunks)


def _external_output_file(manager: ResearchPathManager, value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise PortableResearchPackageError("portable_package_output_must_be_absolute")
    lexical = Path(os.path.abspath(os.fspath(raw)))
    if manager.is_within(lexical, manager.project_root):
        raise PortableResearchPackageError(
            "portable_package_output_must_be_repository_external"
        )
    _reject_symlink_chain(lexical.parent, "portable_package_output_parent")
    return lexical


def _require_absolute_regular_single_link(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise PortableResearchPackageError(f"{label}_path_must_be_absolute")
    _reject_symlink_chain(path, label)
    try:
        metadata = path.stat()
    except OSError as exc:
        raise PortableResearchPackageError(f"{label}_unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise PortableResearchPackageError(f"{label}_file_invalid")


def _reject_symlink_chain(path: Path, label: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PortableResearchPackageError(f"{label}_unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise PortableResearchPackageError(f"{label}_symlink_rejected")


def _json_value(raw: bytes, label: str) -> object:
    if len(raw) > _MAX_JSON_BYTES:
        raise PortableResearchPackageError(f"{label}_too_large")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except PortableResearchPackageError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableResearchPackageError(f"{label}_invalid_json") from exc


def _json_object(raw: bytes, label: str) -> dict[str, object]:
    value = _json_value(raw, label)
    if not isinstance(value, dict):
        raise PortableResearchPackageError(f"{label}_object_required")
    return cast(dict[str, object], value)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PortableResearchPackageError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise PortableResearchPackageError(f"nonfinite_json_constant:{value}")


def _canonical_json_file(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PortableResearchPackageError(
            "portable_package_json_not_canonical"
        ) from exc


def _reject_secrets(value: object, path: str) -> None:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key).lower()
            if key in _SENSITIVE_KEYS or key.endswith(_SENSITIVE_SUFFIXES):
                if item not in (None, "", False, [], {}):
                    raise PortableResearchPackageError(
                        f"portable_package_secret_field_forbidden:{path}.{raw_key}"
                    )
            _reject_secrets(item, f"{path}.{raw_key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secrets(item, f"{path}[{index}]")


def _reject_secret_material(
    raw: bytes,
    label: str,
    *,
    scan_sensitive_environment: bool,
) -> None:
    """Reject value leaks that key-name inspection alone cannot detect.

    Static private-key/token signatures are checked during both construction
    and verification.  Construction additionally compares the prospective
    archive material with non-empty sensitive values already present in the
    builder environment; the value itself is never copied into diagnostics.
    """

    if any(marker in raw for marker in _PRIVATE_MATERIAL_MARKERS) or any(
        pattern.search(raw) is not None for pattern in _PRIVATE_TOKEN_PATTERNS
    ):
        raise PortableResearchPackageError(
            f"portable_package_private_material_forbidden:{label}"
        )
    if not scan_sensitive_environment:
        return
    for name, value in os.environ.items():
        if (
            _SENSITIVE_ENVIRONMENT_NAME.search(name) is None
            or not value
            or len(value.encode("utf-8")) < 8
        ):
            continue
        if value.encode("utf-8") in raw:
            raise PortableResearchPackageError(
                f"portable_package_sensitive_environment_value_forbidden:{label}:{name}"
            )


def _require_exact_fields(
    value: Mapping[str, object], expected: frozenset[str], label: str
) -> None:
    if set(value) != expected:
        raise PortableResearchPackageError(f"{label}_fields_invalid")


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise PortableResearchPackageError(f"{label}_invalid")
    return value


def _require_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise PortableResearchPackageError(f"{label}_invalid")
    return value


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise PortableResearchPackageError(f"{label}_invalid")
    return value


def _require_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise PortableResearchPackageError("portable_package_relative_path_invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PortableResearchPackageError("portable_package_relative_path_invalid")
    return value


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _diagnostic_digest(stdout: str, stderr: str) -> str:
    return hashlib.sha256((stdout + "\n" + stderr).encode("utf-8")).hexdigest()


__all__ = [
    "PORTABLE_RESEARCH_PACKAGE_SCHEMA_VERSION",
    "PortableResearchPackageError",
    "PortableResearchPackageVerification",
    "build_portable_research_package",
    "reproduce_portable_research_package",
    "verify_portable_research_package",
]
