"""Deterministic offline Strategy Research Package export."""

from __future__ import annotations

from typing import Any

from .execution_evidence import REQUIRED_FIELDS
from .final_selection import validate_final_selection_report
from .hashing import sha256_prefixed


class StrategyPackageError(ValueError):
    pass


_CONTRACT_FIELDS = (
    "strategy_spec_hash", "decision_contract_version", "data_requirements",
    "execution_timing_policy", "execution_model", "cost_assumption",
    "partial_fill_assumptions", "order_failure_assumptions", "portfolio_policy",
    "risk_policy", "execution_limitations", "suspension_or_invalidation_criteria",
)


def build_strategy_research_package(report: dict[str, Any]) -> dict[str, Any]:
    """Build only from an internally valid authoritative final-selection report."""
    reasons = validate_final_selection_report(report)
    if reasons:
        raise StrategyPackageError("strategy_package_final_selection_invalid:" + ",".join(reasons))
    if report.get("final_selection_gate_result") != "PASS":
        raise StrategyPackageError("strategy_package_requires_final_selection_pass")
    selected_id = str(report.get("selected_candidate_id") or "")
    if not selected_id:
        raise StrategyPackageError("strategy_package_selected_candidate_missing")
    candidates = list(report.get("candidates") or ())
    selected = next((item for item in candidates if str(item.get("parameter_candidate_id") or item.get("candidate_id") or "") == selected_id), None)
    if selected is None:
        raise StrategyPackageError("strategy_package_selected_candidate_mismatch")
    if selected.get("legacy_execution_authority") or selected.get("legacy_vertical_kernel") or report.get("legacy_execution_authority"):
        raise StrategyPackageError("strategy_package_rejects_legacy_execution")
    evidence = dict(selected.get("execution_evidence") or selected.get("validation_execution_event_summary") or report.get("execution_evidence") or {})
    missing = sorted(REQUIRED_FIELDS - set(evidence))
    if missing:
        raise StrategyPackageError("strategy_package_missing_execution_evidence:" + ",".join(missing))
    if evidence.get("timing_invariant_status") != "PASS":
        raise StrategyPackageError("strategy_package_timing_invariant_failure")
    if evidence.get("declared_execution_model_hash") != evidence.get("executed_execution_model_hash"):
        raise StrategyPackageError("strategy_package_execution_model_mismatch")
    merged = dict(report) | dict(selected)
    absent = [field for field in _CONTRACT_FIELDS if merged.get(field) is None]
    if absent:
        raise StrategyPackageError("strategy_package_missing_required_contract_field:" + ",".join(absent))
    candidate_evidence_hash = sha256_prefixed({"candidate_id": selected_id, "candidate": selected, "execution_evidence": evidence})
    package = {
        "schema_version": 2, "selected_candidate_id": selected_id,
        **{field: merged[field] for field in _CONTRACT_FIELDS},
        "execution_timing_hash": evidence["executed_execution_timing_hash"],
        "execution_model_hash": evidence["executed_execution_model_hash"],
        "request_stream_hash": evidence["execution_request_stream_hash"],
        "fill_stream_hash": evidence["execution_fill_stream_hash"],
        "ledger_stream_hash": evidence["portfolio_ledger_hash"],
        "validation_result": "PASS",
        "source_report_content_hash": sha256_prefixed(report),
        "selected_candidate_evidence_hash": candidate_evidence_hash,
    }
    forbidden = {"account", "credential", "private_api", "submit_order", "api_key", "api_secret"}
    if forbidden & set(package):
        raise StrategyPackageError("strategy_package_operational_field_forbidden")
    package["content_hash"] = sha256_prefixed(package)
    return package
