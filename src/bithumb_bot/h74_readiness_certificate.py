from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping

from .config import runtime_code_provenance
from .decision_equivalence import sha256_prefixed
from .experiment_execution_contract import experiment_execution_contract_from_mapping


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
    negative_rehearsal: Mapping[str, Any] | None = None,
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
    plan = rehearsal.get("would_submit_plan")
    if not isinstance(plan, Mapping):
        raise H74ReadinessCertificateError("h74_certificate_would_submit_plan_missing")
    payload_preview = rehearsal.get("broker_payload_preview")
    if not isinstance(payload_preview, Mapping):
        raise H74ReadinessCertificateError("h74_certificate_broker_payload_preview_missing")
    try:
        plan_notional = float(plan.get("notional_krw") or 0.0)
        exchange_notional = float(plan.get("exchange_submit_notional_krw") or 0.0)
    except (TypeError, ValueError):
        raise H74ReadinessCertificateError("h74_certificate_quote_notional_invalid") from None
    if not (99_999.0 <= plan_notional <= 100_001.0 and 99_999.0 <= exchange_notional <= 100_001.0):
        raise H74ReadinessCertificateError("h74_certificate_quote_notional_not_100000")
    if str(plan.get("exchange_order_type") or "") != "price":
        raise H74ReadinessCertificateError("h74_certificate_exchange_order_type_not_price")
    if str(plan.get("exchange_submit_field") or "") != "price":
        raise H74ReadinessCertificateError("h74_certificate_exchange_submit_field_not_price")
    if payload_preview.get("volume_present") is not False:
        raise H74ReadinessCertificateError("h74_certificate_payload_volume_present")
    submit_semantics_hash = str(rehearsal.get("submit_semantics_hash") or "")
    if not submit_semantics_hash:
        raise H74ReadinessCertificateError("h74_certificate_submit_semantics_hash_missing")
    broker_payload_preview_hash = str(rehearsal.get("broker_payload_preview_hash") or "")
    if not broker_payload_preview_hash:
        raise H74ReadinessCertificateError("h74_certificate_broker_payload_preview_hash_missing")
    if negative_rehearsal is None:
        from .h74_live_rehearsal import H74LiveRehearsalConfig, run_h74_live_rehearsal

        negative_rehearsal = run_h74_live_rehearsal(
            H74LiveRehearsalConfig(
                kst_time="18:00",
                no_submit=True,
                source_artifact_path=str(rehearsal.get("source_artifact_path") or "") or None,
            )
        )
    if str(negative_rehearsal.get("artifact_type") or "") != "h74_live_rehearsal":
        raise H74ReadinessCertificateError("h74_certificate_requires_negative_h74_live_rehearsal")
    negative_blocks_entry = (
        negative_rehearsal.get("broker_submit_reached") is False
        and negative_rehearsal.get("would_submit") is False
        and negative_rehearsal.get("actual_submit") is False
        and negative_rehearsal.get("primary_block_gate") == "entry_authority"
        and negative_rehearsal.get("entry_authority_status") == "BLOCK"
    )
    if not negative_blocks_entry:
        raise H74ReadinessCertificateError("h74_certificate_negative_rehearsal_kst_18_did_not_block_entry")
    if not bool(negative_rehearsal.get("entry_authority_gate_present")):
        raise H74ReadinessCertificateError("h74_certificate_entry_authority_gate_missing")
    entry_authority_gate_hash = str(negative_rehearsal.get("entry_authority_gate_hash") or "")
    if not entry_authority_gate_hash:
        raise H74ReadinessCertificateError("h74_certificate_entry_authority_gate_hash_missing")
    provenance = runtime_code_provenance()
    env_hash = _file_hash(env_file)
    payload: dict[str, Any] = {
        "artifact_type": "h74_readiness_certificate",
        "schema_version": 1,
        "status": "pass",
        "positive_rehearsal_kst_10_pass": True,
        "negative_rehearsal_kst_18_blocks_entry": True,
        "entry_authority_gate_present": True,
        "out_of_window_buy_blocked": True,
        "entry_authority_gate_hash": entry_authority_gate_hash,
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
        "behavior_comparison_hash": str(rehearsal.get("behavior_comparison_hash") or ""),
        "gate_trace_hash": str(rehearsal.get("gate_trace_hash") or ""),
        "negative_rehearsal_gate_trace_hash": str(negative_rehearsal.get("gate_trace_hash") or ""),
        "would_submit_plan_hash": str(rehearsal.get("would_submit_plan_hash") or ""),
        "submit_semantics_hash": submit_semantics_hash,
        "entry_quote_notional_krw": plan_notional,
        "exchange_order_type": str(plan.get("exchange_order_type") or ""),
        "exchange_submit_field": str(plan.get("exchange_submit_field") or ""),
        "broker_payload_preview_hash": broker_payload_preview_hash,
        "negative_rehearsal_would_submit_plan_hash": str(
            negative_rehearsal.get("would_submit_plan_hash") or ""
        ),
        "pre_submit_risk_status": str(rehearsal.get("pre_submit_risk_status") or ""),
        "submit_authority": str(rehearsal.get("submit_authority_reason") or ""),
        "broker_submit_reached": bool(rehearsal.get("broker_submit_reached")),
        "actual_submit": bool(rehearsal.get("actual_submit")),
        "negative_rehearsal_actual_submit": bool(negative_rehearsal.get("actual_submit")),
        "issued_at_sec": float(time.time()),
        "expires_at_sec": float(expires_at_sec if expires_at_sec is not None else time.time() + 3600),
    }
    contract_payload = experiment_execution_contract_from_mapping(
        {
            "source_artifact_hash": rehearsal.get("source_artifact_hash"),
            "authority_hash": rehearsal.get("authority_content_hash") or rehearsal.get("rehearsal_hash"),
            "code_commit_sha": provenance.get("commit_sha") or "unavailable",
            "env_file_hash": env_hash,
            "strategy_parameter_hash": rehearsal.get("authority_parameter_hash"),
            "position_mode": rehearsal.get("position_mode"),
            "quantity_contract_hash": rehearsal.get("quantity_contract_hash"),
            "order_rule_snapshot_hash": rehearsal.get("order_rule_snapshot_hash"),
            "fee_slippage_timing_hash": sha256_prefixed(
                {
                    "fee": rehearsal.get("fee_comparison"),
                    "slippage_bps": rehearsal.get("slippage_bps"),
                    "candle_timing": rehearsal.get("candle_timing"),
                }
            ),
            "startup_gate_hash": rehearsal.get("startup_gate_hash") or rehearsal.get("gate_trace_hash"),
            "submit_semantics_hash": submit_semantics_hash,
            "entry_quote_notional_krw": plan_notional,
            "exchange_order_type": str(plan.get("exchange_order_type") or ""),
            "exchange_submit_field": str(plan.get("exchange_submit_field") or ""),
            "would_submit_plan_hash": str(rehearsal.get("would_submit_plan_hash") or ""),
            "broker_payload_preview_hash": broker_payload_preview_hash,
        }
    ).as_payload()
    payload["experiment_execution_contract"] = contract_payload
    payload["contract_hash"] = str(contract_payload["contract_hash"])
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
    current_behavior_comparison_hash: str | None = None,
    current_contract_hash: str | None = None,
    current_submit_semantics_hash: str | None = None,
    current_entry_quote_notional_krw: float | None = None,
    current_exchange_order_type: str | None = None,
    current_exchange_submit_field: str | None = None,
    current_broker_payload_preview_hash: str | None = None,
    strict: bool = False,
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
        ("commit_sha", current_commit_sha, "commit_sha_changed", "missing_current_commit_sha"),
        ("db_schema_hash", current_db_schema_hash, "db_schema_hash_changed", "missing_current_db_schema_hash"),
        (
            "order_rule_fee_authority_hash",
            current_order_rule_fee_authority_hash,
            "order_rule_fee_authority_hash_changed",
            "missing_current_order_rule_fee_authority_hash",
        ),
        ("gate_trace_hash", current_gate_trace_hash, "gate_trace_hash_changed", "missing_current_gate_trace_hash"),
        (
            "would_submit_plan_hash",
            current_would_submit_plan_hash,
            "would_submit_plan_hash_changed",
            "missing_current_would_submit_plan_hash",
        ),
        (
            "behavior_comparison_hash",
            current_behavior_comparison_hash,
            "behavior_comparison_hash_changed",
            "missing_current_behavior_comparison_hash",
        ),
        ("contract_hash", current_contract_hash, "contract_hash_mismatch", "missing_current_contract_hash"),
        (
            "submit_semantics_hash",
            current_submit_semantics_hash,
            "submit_semantics_hash_changed",
            "missing_current_submit_semantics_hash",
        ),
        (
            "exchange_order_type",
            current_exchange_order_type,
            "payload_order_type_changed",
            "missing_current_exchange_order_type",
        ),
        (
            "exchange_submit_field",
            current_exchange_submit_field,
            "exchange_submit_field_changed",
            "missing_current_exchange_submit_field",
        ),
        (
            "broker_payload_preview_hash",
            current_broker_payload_preview_hash,
            "broker_payload_preview_hash_changed",
            "missing_current_broker_payload_preview_hash",
        ),
    )
    for field, current, reason, missing_reason in comparisons:
        if current is None:
            if strict:
                reasons.append(missing_reason)
            continue
        if str(certificate.get(field) or "") != str(current):
            reasons.append(reason)
    if current_entry_quote_notional_krw is None:
        if strict:
            reasons.append("missing_current_entry_quote_notional_krw")
    else:
        try:
            if abs(float(certificate.get("entry_quote_notional_krw") or 0.0) - float(current_entry_quote_notional_krw)) > 1.0:
                reasons.append("entry_quote_notional_krw_changed")
        except (TypeError, ValueError):
            reasons.append("entry_quote_notional_krw_changed")
    return {
        "valid": not reasons,
        "status": "pass" if not reasons else "invalid",
        "reasons": reasons,
    }


def validate_h74_long_run_preflight(certificate: Mapping[str, Any]) -> dict[str, Any]:
    required_true = (
        "positive_rehearsal_kst_10_pass",
        "negative_rehearsal_kst_18_blocks_entry",
        "entry_authority_gate_present",
        "out_of_window_buy_blocked",
    )
    reasons: list[str] = []
    if str(certificate.get("status") or "") != "pass":
        reasons.append("certificate_not_pass")
    for key in required_true:
        if certificate.get(key) is not True:
            reasons.append(f"{key}_missing_or_false")
    if not str(certificate.get("entry_authority_gate_hash") or "").strip():
        reasons.append("entry_authority_gate_hash_missing")
    if not str(certificate.get("contract_hash") or "").strip():
        reasons.append("contract_hash_missing")
    if not str(certificate.get("submit_semantics_hash") or "").strip():
        reasons.append("submit_semantics_hash_missing")
    if not str(certificate.get("would_submit_plan_hash") or "").strip():
        reasons.append("would_submit_plan_hash_missing")
    if not str(certificate.get("broker_payload_preview_hash") or "").strip():
        reasons.append("broker_payload_preview_hash_missing")
    try:
        if not (99_999.0 <= float(certificate.get("entry_quote_notional_krw") or 0.0) <= 100_001.0):
            reasons.append("entry_quote_notional_krw_not_100000")
    except (TypeError, ValueError):
        reasons.append("entry_quote_notional_krw_not_100000")
    if str(certificate.get("exchange_order_type") or "") != "price":
        reasons.append("payload_order_type_mismatch")
    if str(certificate.get("exchange_submit_field") or "") != "price":
        reasons.append("exchange_submit_field_mismatch")
    return {
        "valid": not reasons,
        "status": "pass" if not reasons else "blocked",
        "reasons": reasons,
        "run_startup_enforced": True,
    }


__all__ = [
    "H74ReadinessCertificateError",
    "build_h74_readiness_certificate",
    "validate_h74_long_run_preflight",
    "validate_h74_readiness_certificate",
]
