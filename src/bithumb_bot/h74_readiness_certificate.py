from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping

from .config import runtime_code_provenance
from .decision_equivalence import sha256_prefixed


class H74ReadinessCertificateError(ValueError):
    pass


def _file_hash(path: str | None) -> str:
    if not path:
        return "sha256:env-file-missing"
    candidate = Path(path)
    if not candidate.exists():
        return "sha256:env-file-missing"
    return sha256_prefixed({"path": str(candidate), "content": candidate.read_text(encoding="utf-8")})


def build_h74_readiness_certificate(
    rehearsal: Mapping[str, Any],
    *,
    env_file: str | None,
    expires_at_sec: float | None = None,
    schema_hash: str = "sha256:operational-schema-v1",
) -> dict[str, Any]:
    if str(rehearsal.get("artifact_type") or "") != "h74_live_rehearsal":
        raise H74ReadinessCertificateError("h74_certificate_requires_h74_live_rehearsal")
    if str(rehearsal.get("source_artifact_status") or "") != "loaded":
        raise H74ReadinessCertificateError("h74_certificate_source_artifact_not_loaded")
    if str(rehearsal.get("experiment_equivalence_status") or "") != "pass":
        raise H74ReadinessCertificateError("h74_certificate_experiment_equivalence_not_pass")
    gate_trace = rehearsal.get("gate_trace")
    if not isinstance(gate_trace, list):
        raise H74ReadinessCertificateError("h74_certificate_gate_trace_missing")
    if any(bool(entry.get("blocking")) for entry in gate_trace if isinstance(entry, Mapping)):
        raise H74ReadinessCertificateError("h74_certificate_gate_trace_blocking")
    required = {
        "pre_submit_risk_status": "ALLOW",
        "submit_authority_reason": "allowed_target_delta",
        "broker_submit_reached": True,
        "actual_submit": False,
    }
    for key, expected in required.items():
        if rehearsal.get(key) != expected:
            raise H74ReadinessCertificateError(f"h74_certificate_rehearsal_requirement_failed:{key}")
    provenance = runtime_code_provenance()
    env_hash = _file_hash(env_file)
    payload: dict[str, Any] = {
        "artifact_type": "h74_readiness_certificate",
        "schema_version": 1,
        "status": "pass",
        "commit_sha": str(provenance.get("commit_sha") or "unavailable"),
        "env_file_hash": env_hash,
        "db_schema_hash": schema_hash,
        "h74_authority_hash": str(rehearsal.get("rehearsal_hash") or ""),
        "broker_balance_snapshot_hash": str(rehearsal.get("broker_balance_snapshot_hash") or ""),
        "order_rule_fee_authority_hash": sha256_prefixed(
            {
                "fee": rehearsal.get("fee_comparison"),
                "order_rules": rehearsal.get("order_rule_comparison"),
                "fee_authority_source": rehearsal.get("fee_authority_source"),
            }
        ),
        "gate_trace_hash": str(rehearsal.get("gate_trace_hash") or ""),
        "would_submit_plan_hash": str(rehearsal.get("would_submit_plan_hash") or ""),
        "pre_submit_risk_status": str(rehearsal.get("pre_submit_risk_status") or ""),
        "submit_authority": str(rehearsal.get("submit_authority_reason") or ""),
        "broker_submit_reached": bool(rehearsal.get("broker_submit_reached")),
        "actual_submit": bool(rehearsal.get("actual_submit")),
        "issued_at_sec": float(time.time()),
        "expires_at_sec": float(expires_at_sec if expires_at_sec is not None else time.time() + 3600),
    }
    payload["certificate_hash"] = sha256_prefixed(payload)
    return payload


def validate_h74_readiness_certificate(
    certificate: Mapping[str, Any],
    *,
    env_file: str | None,
    broker_balance_snapshot_hash: str,
    now_sec: float | None = None,
    current_commit_sha: str | None = None,
    current_db_schema_hash: str | None = None,
    current_order_rule_fee_authority_hash: str | None = None,
    current_gate_trace_hash: str | None = None,
    current_would_submit_plan_hash: str | None = None,
) -> dict[str, Any]:
    expected_env_hash = _file_hash(env_file)
    reasons: list[str] = []
    if str(certificate.get("env_file_hash") or "") != expected_env_hash:
        reasons.append("env_hash_changed")
    if str(certificate.get("broker_balance_snapshot_hash") or "") != str(broker_balance_snapshot_hash):
        reasons.append("broker_balance_snapshot_changed")
    if float(certificate.get("expires_at_sec") or 0.0) <= float(time.time() if now_sec is None else now_sec):
        reasons.append("certificate_expired")
    comparisons = (
        ("commit_sha", current_commit_sha, "commit_sha_changed"),
        ("db_schema_hash", current_db_schema_hash, "db_schema_hash_changed"),
        (
            "order_rule_fee_authority_hash",
            current_order_rule_fee_authority_hash,
            "order_rule_fee_authority_hash_changed",
        ),
        ("gate_trace_hash", current_gate_trace_hash, "gate_trace_hash_changed"),
        ("would_submit_plan_hash", current_would_submit_plan_hash, "would_submit_plan_hash_changed"),
    )
    for field, current, reason in comparisons:
        if current is None:
            continue
        if str(certificate.get(field) or "") != str(current):
            reasons.append(reason)
    return {
        "valid": not reasons,
        "status": "pass" if not reasons else "invalid",
        "reasons": reasons,
    }


__all__ = [
    "H74ReadinessCertificateError",
    "build_h74_readiness_certificate",
    "validate_h74_readiness_certificate",
]
