from __future__ import annotations

from pathlib import Path
from typing import Any

from bithumb_bot.evidence_safety import diagnostic_feature_mining_taxonomy
from bithumb_bot.paths import PathManager
from bithumb_bot.research.experiment_manifest import ExperimentManifest
from bithumb_bot.research.hashing import report_content_hash_payload, sha256_prefixed
from bithumb_bot.storage_io import write_json_atomic


POLICY_DENIAL_ARTIFACT_TYPE = "forward_return_diagnostic_policy_denial"
POLICY_DENIAL_STATUS = "policy_denied"
POLICY_DENIAL_NEXT_ACTION = "rerun_with_explicit_override_or_use_train_validation"


def forward_diagnostics_policy_denial_path(*, manager: PathManager, experiment_id: str) -> Path:
    return manager.data_dir() / "reports" / "research" / experiment_id / "forward_diagnostics_policy_denial.json"


def build_forward_diagnostics_policy_denial_payload(
    *,
    manifest: ExperimentManifest | None,
    reason: str,
    split_name: str,
    feature_names: tuple[str, ...],
    horizon_steps: tuple[int, ...],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": POLICY_DENIAL_ARTIFACT_TYPE,
        "diagnostic_only": True,
        "promotion_evidence": False,
        "approved_profile_evidence": False,
        "live_readiness_evidence": False,
        "capital_allocation_evidence": False,
        "diagnostic_status": POLICY_DENIAL_STATUS,
        "reason": str(reason),
        "split_name": str(split_name),
        "feature_names": list(feature_names),
        "horizon_steps": list(horizon_steps),
        "operator_next_action": POLICY_DENIAL_NEXT_ACTION,
        **diagnostic_feature_mining_taxonomy(operator_next_action=POLICY_DENIAL_NEXT_ACTION),
    }
    if manifest is not None:
        payload["experiment_id"] = manifest.experiment_id
        payload["manifest_hash"] = manifest.manifest_hash()
    validate_forward_diagnostics_policy_denial_flags(payload)
    payload["content_hash"] = sha256_prefixed(report_content_hash_payload(payload))
    return payload


def write_forward_diagnostics_policy_denial_artifact(
    *,
    manager: PathManager,
    manifest: ExperimentManifest,
    reason: str,
    split_name: str,
    feature_names: tuple[str, ...],
    horizon_steps: tuple[int, ...],
) -> dict[str, Any]:
    path = forward_diagnostics_policy_denial_path(manager=manager, experiment_id=manifest.experiment_id)
    payload = build_forward_diagnostics_policy_denial_payload(
        manifest=manifest,
        reason=reason,
        split_name=split_name,
        feature_names=feature_names,
        horizon_steps=horizon_steps,
    )
    payload["artifact_paths"] = {"policy_denial": str(path)}
    payload["content_hash"] = sha256_prefixed(report_content_hash_payload(payload))
    validate_forward_diagnostics_policy_denial_flags(payload)
    write_json_atomic(path, payload)
    return payload


def validate_forward_diagnostics_policy_denial_flags(payload: dict[str, Any]) -> None:
    if payload.get("artifact_type") != POLICY_DENIAL_ARTIFACT_TYPE:
        raise ValueError("forward diagnostics policy denial artifact_type required")
    if payload.get("diagnostic_status") != POLICY_DENIAL_STATUS:
        raise ValueError("forward diagnostics policy denial must use policy_denied status")
    if payload.get("diagnostic_only") is not True:
        raise ValueError("forward diagnostics policy denial must be diagnostic_only")
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
        raise ValueError("forward diagnostics policy denial must remain diagnostic-only")
    if payload.get("non_promotable") is not True:
        raise ValueError("forward diagnostics policy denial must be non_promotable")
    if payload.get("evidence_scope") != "diagnostic_feature_mining":
        raise ValueError("forward diagnostics policy denial evidence_scope mismatch")
    forbidden_uses = payload.get("forbidden_uses")
    if not isinstance(forbidden_uses, list) or not {
        "strategy_promotion",
        "approved_profile",
        "live_readiness",
        "capital_allocation",
    }.issubset({str(item) for item in forbidden_uses}):
        raise ValueError("forward diagnostics policy denial forbidden_uses incomplete")
    if not str(payload.get("operator_next_action") or "").strip():
        raise ValueError("forward diagnostics policy denial operator_next_action required")
