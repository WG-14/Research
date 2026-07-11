from __future__ import annotations

from pathlib import Path
from typing import Any

from bithumb_research.paths import PathManager
from bithumb_research.research.diagnostic_availability import DiagnosticAvailability
from bithumb_research.research.experiment_manifest import ExperimentManifest
from bithumb_research.research.artifact_contract import apply_artifact_contract, validate_artifact_contract
from bithumb_research.research.hashing import report_content_hash_payload, sha256_prefixed
from bithumb_research.storage_io import write_json_atomic


FAILURE_ARTIFACT_TYPE = "forward_return_diagnostic_failure"


def forward_diagnostics_failure_path(*, manager: PathManager, experiment_id: str) -> Path:
    return manager.data_dir() / "reports" / "research" / experiment_id / "forward_diagnostics_failure.json"


def build_forward_diagnostics_failure_payload(
    *,
    manifest: ExperimentManifest,
    split_name: str,
    feature_names: tuple[str, ...],
    horizon_steps: tuple[int, ...],
    fail_reasons: tuple[str, ...],
    availability: DiagnosticAvailability | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = apply_artifact_contract({
        "schema_version": 1,
        "artifact_type": FAILURE_ARTIFACT_TYPE,
        "diagnostic_status": "unavailable",
        "fail_reasons": list(fail_reasons),
        "manifest_hash": manifest.manifest_hash(),
        "split_name": split_name,
        "feature_names": list(feature_names),
        "horizon_steps": list(horizon_steps),
    })
    if availability is not None:
        payload["availability"] = availability.as_dict()
    validate_forward_diagnostics_failure_flags(payload)
    payload["content_hash"] = sha256_prefixed(report_content_hash_payload(payload))
    return payload


def write_forward_diagnostics_failure_artifact(
    *,
    manager: PathManager,
    manifest: ExperimentManifest,
    split_name: str,
    feature_names: tuple[str, ...],
    horizon_steps: tuple[int, ...],
    fail_reasons: tuple[str, ...],
    availability: DiagnosticAvailability | None = None,
) -> dict[str, Any]:
    path = forward_diagnostics_failure_path(manager=manager, experiment_id=manifest.experiment_id)
    payload = build_forward_diagnostics_failure_payload(
        manifest=manifest,
        split_name=split_name,
        feature_names=feature_names,
        horizon_steps=horizon_steps,
        fail_reasons=fail_reasons,
        availability=availability,
    )
    payload["artifact_paths"] = {"failure": str(path)}
    payload["content_hash"] = sha256_prefixed(report_content_hash_payload(payload))
    write_json_atomic(path, payload)
    return payload


def validate_forward_diagnostics_failure_flags(payload: dict[str, Any]) -> None:
    if payload.get("artifact_type") == "forward_return_diagnostic_report":
        raise ValueError("forward diagnostics failure artifact must not use success report artifact_type")
    validate_artifact_contract(payload)
