"""Actual built-in study replay from package-contained source and evidence.

This module is bundled with the source archive in a portable validated
package.  It does not consult Git, an installed distribution, a virtual
environment, a cache, a previous result, or a network service.  The portable
runtime extracts the bound source archive, materializes the bound immutable
evidence envelopes in a fresh external root, and calls the function below.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Mapping, cast

from market_research.paths import ResearchPathManager
from market_research.research.multi_asset.application import (
    execute_deterministic_study_core,
)
from market_research.research.multi_asset.builtin_runner import (
    BuiltinMultiAssetRequest,
)
from market_research.research.multi_asset.evidence import (
    validated_study_report_payload,
)
from market_research.research.multi_asset.research_package import (
    BoundedEvidenceArtifactResolver,
    EvidenceArtifactRef,
    EvidenceArtifactRole,
    RuntimeEnvironment,
    bytes_sha256,
)
from market_research.settings import ResearchSettings


PORTABLE_ENGINE_REPLAY_DESCRIPTOR_SCHEMA_VERSION = 1


class PortableEngineReplayError(ValueError):
    """The executable replay descriptor is incomplete or does not reproduce."""


def _canonical_file_bytes(value: object) -> bytes:
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


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PortableEngineReplayError(f"{field_name}_object_required")
    return cast(Mapping[str, object], value)


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise PortableEngineReplayError(f"{field_name}_invalid")
    return value


def _runtime_from_payload(payload: Mapping[str, object]) -> RuntimeEnvironment:
    expected = {
        "git_commit",
        "dirty_worktree",
        "working_tree_hash",
        "python_version",
        "python_implementation",
        "platform",
        "dependency_versions",
    }
    if set(payload) != expected:
        raise PortableEngineReplayError("portable_replay.runtime_fields_invalid")
    dirty = payload["dirty_worktree"]
    dependencies = payload["dependency_versions"]
    if not isinstance(dirty, bool):
        raise PortableEngineReplayError("portable_replay.runtime_dirty_invalid")
    if not isinstance(dependencies, list) or any(
        not isinstance(item, str) for item in dependencies
    ):
        raise PortableEngineReplayError("portable_replay.runtime_dependencies_invalid")
    return RuntimeEnvironment(
        git_commit=_text(payload["git_commit"], "portable_replay.git_commit"),
        dirty_worktree=dirty,
        working_tree_hash=_text(
            payload["working_tree_hash"],
            "portable_replay.working_tree_hash",
        ),
        python_version=_text(
            payload["python_version"],
            "portable_replay.python_version",
        ),
        python_implementation=_text(
            payload["python_implementation"],
            "portable_replay.python_implementation",
        ),
        platform=_text(payload["platform"], "portable_replay.platform"),
        dependency_versions=tuple(cast(list[str], dependencies)),
    )


def _prepare_workspace(root: Path) -> ResearchPathManager:
    if not root.is_absolute():
        raise PortableEngineReplayError("portable_replay.root_absolute_required")
    root.mkdir(parents=True, exist_ok=False)
    project_root = root / "project"
    state_root = root / "state"
    project_root.mkdir()
    settings = ResearchSettings(
        data_root=state_root / "data",
        artifact_root=state_root / "artifacts",
        report_root=state_root / "reports",
        cache_root=state_root / "cache",
        db_path=None,
        max_workers=1,
        random_seed=0,
    )
    paths = ResearchPathManager.from_settings(
        settings,
        project_root=project_root,
    )
    paths.ensure_roots()
    return paths


def replay_builtin_engine(
    descriptor_value: object,
    *,
    workspace_root: str | Path,
) -> dict[str, object]:
    """Recompute the actual study and report twice from immutable inputs."""

    descriptor = _mapping(descriptor_value, "portable_replay.descriptor")
    expected_fields = {
        "schema_version",
        "request",
        "evidence_envelopes",
        "expected_study_content_hash",
        "expected_study_artifact_hash",
        "expected_report_artifact_hash",
        "expected_accounting_reconciliation_hash",
        "engine_source_content_hash",
    }
    if set(descriptor) != expected_fields:
        raise PortableEngineReplayError("portable_replay.descriptor_fields_invalid")
    if descriptor["schema_version"] != PORTABLE_ENGINE_REPLAY_DESCRIPTOR_SCHEMA_VERSION:
        raise PortableEngineReplayError("portable_replay.schema_version_invalid")
    _text(
        descriptor["engine_source_content_hash"],
        "portable_replay.engine_source_content_hash",
    )
    raw_entries = descriptor["evidence_envelopes"]
    if not isinstance(raw_entries, list) or not raw_entries:
        raise PortableEngineReplayError("portable_replay.evidence_required")

    request = BuiltinMultiAssetRequest.from_dict(
        _mapping(descriptor["request"], "portable_replay.request")
    )
    paths = _prepare_workspace(Path(workspace_root))
    expected_entries: dict[
        tuple[str, str, str, str, str, int],
        Mapping[str, object],
    ] = {}
    for index, value in enumerate(raw_entries):
        entry = _mapping(value, f"portable_replay.evidence.{index}")
        entry_fields = {
            "role",
            "logical_id",
            "version",
            "content_hash",
            "schema_hash",
            "byte_length",
            "payload_base64",
        }
        if set(entry) != entry_fields:
            raise PortableEngineReplayError("portable_replay.evidence_fields_invalid")
        byte_length = entry["byte_length"]
        if isinstance(byte_length, bool) or not isinstance(byte_length, int):
            raise PortableEngineReplayError(
                "portable_replay.evidence_byte_length_invalid"
            )
        identity = (
            _text(entry["role"], "portable_replay.evidence.role"),
            _text(entry["logical_id"], "portable_replay.evidence.logical_id"),
            _text(entry["version"], "portable_replay.evidence.version"),
            _text(entry["content_hash"], "portable_replay.evidence.content_hash"),
            _text(entry["schema_hash"], "portable_replay.evidence.schema_hash"),
            byte_length,
        )
        if identity in expected_entries:
            raise PortableEngineReplayError(
                "portable_replay.evidence_identity_duplicate"
            )
        expected_entries[identity] = entry

    rebased_references: list[EvidenceArtifactRef] = []
    for index, original in enumerate(request.evidence_references):
        identity = (
            original.role.value,
            original.logical_id,
            original.version,
            original.content_hash,
            original.schema_hash,
            original.byte_length,
        )
        matched_entry = expected_entries.get(identity)
        if matched_entry is None:
            raise PortableEngineReplayError(
                "portable_replay.evidence_reference_missing"
            )
        del expected_entries[identity]
        encoded = _text(
            matched_entry["payload_base64"],
            "portable_replay.evidence.payload_base64",
        )
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise PortableEngineReplayError(
                "portable_replay.evidence_base64_invalid"
            ) from exc
        if base64.b64encode(raw).decode("ascii") != encoded:
            raise PortableEngineReplayError(
                "portable_replay.evidence_base64_noncanonical"
            )
        if len(raw) != original.byte_length or bytes_sha256(raw) != (
            original.content_hash
        ):
            raise PortableEngineReplayError("portable_replay.evidence_content_mismatch")
        target = (
            paths.data_root
            / f"{index:03d}-{original.content_hash.removeprefix('sha256:')}.json"
        )
        target.write_bytes(raw)
        rebased_references.append(
            replace(
                original,
                uri=target.resolve(strict=True).as_uri(),
            )
        )
    if expected_entries:
        raise PortableEngineReplayError("portable_replay.evidence_unreferenced")

    replay_request = replace(
        request,
        evidence_references=tuple(
            sorted(
                rebased_references,
                key=lambda item: (
                    item.role.value,
                    item.logical_id,
                    item.version,
                ),
            )
        ),
    )
    resolver = BoundedEvidenceArtifactResolver.from_paths(paths)
    verified_at = replay_request.spec.frozen_at
    first_artifacts = resolver.resolve_all(
        replay_request.evidence_references,
        verified_at=verified_at,
    )
    repeated_artifacts = resolver.resolve_all(
        replay_request.evidence_references,
        verified_at=verified_at,
    )
    environment = tuple(
        item
        for item in first_artifacts
        if item.reference.role is EvidenceArtifactRole.ENVIRONMENT
    )
    if len(environment) != 1:
        raise PortableEngineReplayError(
            "portable_replay.environment_cardinality_invalid"
        )
    runtime = _runtime_from_payload(environment[0].payload)
    application_request = replay_request.to_application_request(
        paths=paths,
        command=("portable-engine-replay",),
        run_id="run:portable-engine-replay",
    )
    core = execute_deterministic_study_core(
        spec=replay_request.spec,
        first_artifacts=first_artifacts,
        repeated_artifacts=repeated_artifacts,
        runners=application_request.runners,
        runtime=runtime,
    )
    study_payload = core.study.as_dict()
    report_payload = validated_study_report_payload(core.study)
    study_artifact_hash = _sha256(_canonical_file_bytes(study_payload))
    report_artifact_hash = _sha256(_canonical_file_bytes(report_payload))
    expected_study_hash = _text(
        descriptor["expected_study_content_hash"],
        "portable_replay.expected_study_content_hash",
    )
    expected_study_artifact_hash = _text(
        descriptor["expected_study_artifact_hash"],
        "portable_replay.expected_study_artifact_hash",
    )
    expected_report_artifact_hash = _text(
        descriptor["expected_report_artifact_hash"],
        "portable_replay.expected_report_artifact_hash",
    )
    expected_accounting_hash = _text(
        descriptor["expected_accounting_reconciliation_hash"],
        "portable_replay.expected_accounting_reconciliation_hash",
    )
    mismatches: list[str] = []
    if core.study.content_hash != expected_study_hash:
        mismatches.append("study_content_hash")
    if study_artifact_hash != expected_study_artifact_hash:
        mismatches.append("study_artifact_hash")
    if report_artifact_hash != expected_report_artifact_hash:
        mismatches.append("report_artifact_hash")
    if core.study.accounting_reconciliation_hash != expected_accounting_hash:
        mismatches.append("accounting_reconciliation_hash")
    if mismatches:
        raise PortableEngineReplayError(
            "portable_replay.output_mismatch:" + ",".join(sorted(mismatches))
        )
    return {
        "schema_version": 1,
        "study": study_payload,
        "report": report_payload,
        "study_content_hash": core.study.content_hash,
        "study_artifact_hash": study_artifact_hash,
        "report_artifact_hash": report_artifact_hash,
        "accounting_reconciliation_hash": (core.study.accounting_reconciliation_hash),
        "mismatch_fields": [],
    }


__all__ = [
    "PORTABLE_ENGINE_REPLAY_DESCRIPTOR_SCHEMA_VERSION",
    "PortableEngineReplayError",
    "replay_builtin_engine",
]
