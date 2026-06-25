from __future__ import annotations

import shutil
from collections.abc import Mapping

from .h74_authority_alignment import validate_h74_authority_env_alignment
from .research.hashing import sha256_prefixed


class H74PreSubmitEvidenceError(RuntimeError):
    pass


def build_h74_pre_submit_evidence_bundle(
    *,
    authority_payload: Mapping[str, object],
    settings_obj: object,
    env_hash: str,
    risk_baseline_certificate_hash: str,
    db_snapshot_hash: str = "",
    db_snapshot_locator: str = "",
    starting_broker_position: Mapping[str, object],
    starting_local_position: Mapping[str, object],
    flat_start_proof: Mapping[str, object],
    disk_capacity_path: str,
    min_free_bytes: int = 1,
) -> dict[str, object]:
    alignment = validate_h74_authority_env_alignment(authority_payload, settings_obj=settings_obj)
    if not bool(flat_start_proof.get("flat")):
        raise H74PreSubmitEvidenceError("pre_submit_evidence_flat_start_required")
    free = int(shutil.disk_usage(disk_capacity_path).free)
    if free < int(min_free_bytes):
        raise H74PreSubmitEvidenceError("pre_submit_evidence_disk_capacity_insufficient")
    if not db_snapshot_hash and not db_snapshot_locator:
        raise H74PreSubmitEvidenceError("pre_submit_evidence_db_snapshot_required")
    payload = {
        "artifact_type": "h74_pre_submit_evidence_bundle",
        "authority_hash": str(authority_payload.get("authority_content_hash") or ""),
        "env_hash": str(env_hash or ""),
        "effective_behavior_parameters": dict(alignment.effective_behavior_parameters),
        "variant_overrides": dict(authority_payload.get("variant_overrides") or {}),
        "risk_baseline_certificate_hash": str(risk_baseline_certificate_hash or ""),
        "db_snapshot_hash": str(db_snapshot_hash or ""),
        "db_snapshot_locator": str(db_snapshot_locator or ""),
        "starting_broker_position": dict(starting_broker_position),
        "starting_local_position": dict(starting_local_position),
        "flat_start_proof": dict(flat_start_proof),
        "disk_capacity_proof": {"path": str(disk_capacity_path), "free_bytes": free, "min_free_bytes": int(min_free_bytes)},
        "authority_env_alignment": alignment.as_dict(),
    }
    missing = [key for key in ("authority_hash", "env_hash", "risk_baseline_certificate_hash") if not payload[key]]
    if missing:
        raise H74PreSubmitEvidenceError("pre_submit_evidence_missing:" + ",".join(missing))
    payload["pre_submit_evidence_hash"] = sha256_prefixed(payload)
    return payload


def require_pre_submit_bundle_hash(bundle: Mapping[str, object] | None) -> None:
    if not bundle or not str(bundle.get("pre_submit_evidence_hash") or "").strip():
        raise H74PreSubmitEvidenceError("h74_no_window_probe_pre_submit_evidence_hash_required")
    payload = dict(bundle)
    expected_hash = str(payload.pop("pre_submit_evidence_hash") or "").strip()
    if sha256_prefixed(payload) != expected_hash:
        raise H74PreSubmitEvidenceError("h74_no_window_probe_pre_submit_evidence_hash_mismatch")
    alignment = bundle.get("authority_env_alignment")
    if not isinstance(alignment, Mapping) or not bool(alignment.get("ok")):
        raise H74PreSubmitEvidenceError("h74_no_window_probe_authority_env_alignment_failed")
    flat_start_proof = bundle.get("flat_start_proof")
    if not isinstance(flat_start_proof, Mapping) or not bool(flat_start_proof.get("flat")):
        raise H74PreSubmitEvidenceError("h74_no_window_probe_flat_start_proof_failed")
    disk_capacity_proof = bundle.get("disk_capacity_proof")
    if not isinstance(disk_capacity_proof, Mapping):
        raise H74PreSubmitEvidenceError("h74_no_window_probe_disk_capacity_proof_missing")
    if int(disk_capacity_proof.get("free_bytes") or 0) < int(disk_capacity_proof.get("min_free_bytes") or 1):
        raise H74PreSubmitEvidenceError("h74_no_window_probe_disk_capacity_proof_failed")
    if not str(bundle.get("db_snapshot_hash") or "").strip() and not str(bundle.get("db_snapshot_locator") or "").strip():
        raise H74PreSubmitEvidenceError("h74_no_window_probe_db_snapshot_proof_failed")
