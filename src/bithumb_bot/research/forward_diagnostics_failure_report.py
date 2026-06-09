from __future__ import annotations

from pathlib import Path
from typing import Any

from bithumb_bot.evidence_safety import diagnostic_feature_mining_taxonomy
from bithumb_bot.paths import PathManager
from bithumb_bot.research.diagnostic_availability import DiagnosticAvailability
from bithumb_bot.research.experiment_manifest import ExperimentManifest
from bithumb_bot.research.hashing import report_content_hash_payload, sha256_prefixed
from bithumb_bot.storage_io import write_json_atomic


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
    payload: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": FAILURE_ARTIFACT_TYPE,
        "diagnostic_only": True,
        "promotion_evidence": False,
        "approved_profile_evidence": False,
        "live_readiness_evidence": False,
        "capital_allocation_evidence": False,
        **diagnostic_feature_mining_taxonomy(),
        "diagnostic_status": "unavailable",
        "fail_reasons": list(fail_reasons),
        "manifest_hash": manifest.manifest_hash(),
        "split_name": split_name,
        "feature_names": list(feature_names),
        "horizon_steps": list(horizon_steps),
    }
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
    if payload.get("diagnostic_only") is not True:
        raise ValueError("forward diagnostics failure artifact must be diagnostic_only")
    if any(
        bool(payload.get(field))
        for field in (
            "promotion_evidence",
            "approved_profile_evidence",
            "live_readiness_evidence",
            "capital_allocation_evidence",
            "promotion_eligible",
            "promotion_grade",
        )
    ):
        raise ValueError("forward diagnostics failure artifact must remain diagnostic-only")
    if payload.get("non_promotable") is not True:
        raise ValueError("forward diagnostics failure artifact must be non_promotable")
    if payload.get("evidence_scope") != "diagnostic_feature_mining":
        raise ValueError("forward diagnostics failure artifact evidence_scope mismatch")
    forbidden_uses = payload.get("forbidden_uses")
    if not isinstance(forbidden_uses, list) or not {
        "strategy_promotion",
        "approved_profile",
        "live_readiness",
        "capital_allocation",
    }.issubset({str(item) for item in forbidden_uses}):
        raise ValueError("forward diagnostics failure artifact forbidden_uses incomplete")
    if not str(payload.get("operator_next_action") or "").strip():
        raise ValueError("forward diagnostics failure artifact operator_next_action required")
