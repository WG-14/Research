"""Deterministic offline Strategy Research Package export."""

from __future__ import annotations

from typing import Any

from .execution_evidence import REQUIRED_FIELDS, REQUIRED_FIELDS_V2
from .final_selection import validate_final_selection_report
from .hashing import report_content_hash_payload, sha256_prefixed


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
    primary_id = str(selected.get("primary_scenario_id") or "")
    scenarios = list(selected.get("scenario_results") or selected.get("scenarios") or ())
    primary = next((item for item in scenarios if str(item.get("scenario_id") or "") == primary_id), None) if primary_id else None
    evidence_sources = []
    if isinstance(primary, dict):
        evidence_sources.extend((primary.get("execution_evidence"), primary.get("validation_execution_event_summary")))
        for split_name in ("validation_resource_usage", "final_holdout_resource_usage"):
            usage = primary.get(split_name)
            if isinstance(usage, dict): evidence_sources.append(usage.get("execution_evidence"))
    evidence_sources.extend((selected.get("execution_evidence"), selected.get("validation_execution_event_summary"), report.get("execution_evidence")))
    evidence = dict(next((item for item in evidence_sources if isinstance(item, dict) and item), {}))
    required = REQUIRED_FIELDS_V2 if int(evidence.get("execution_evidence_schema_version") or 1) >= 2 else REQUIRED_FIELDS
    missing = sorted(required - set(evidence))
    if missing:
        raise StrategyPackageError("strategy_package_missing_execution_evidence:" + ",".join(missing))
    if evidence.get("timing_invariant_status") != "PASS":
        raise StrategyPackageError("strategy_package_timing_invariant_failure")
    if evidence.get("declared_execution_model_hash") != evidence.get("executed_execution_model_hash"):
        raise StrategyPackageError("strategy_package_execution_model_mismatch")
    declared_timing = evidence.get("declared_execution_timing_policy_hash", evidence.get("declared_execution_timing_hash"))
    executed_timing = evidence.get("executed_execution_timing_policy_hash", evidence.get("executed_execution_timing_hash"))
    if int(evidence.get("execution_evidence_schema_version") or 1) >= 2 and declared_timing != executed_timing:
        raise StrategyPackageError("strategy_package_execution_timing_mismatch")
    merged = dict(report) | dict(selected)
    absent = [field for field in _CONTRACT_FIELDS if merged.get(field) is None]
    if absent:
        raise StrategyPackageError("strategy_package_missing_required_contract_field:" + ",".join(absent))
    stable_projection = {"candidate_id": selected_id, "primary_scenario_id": primary_id, "strategy_spec_hash": merged["strategy_spec_hash"], "decision_contract_version": merged["decision_contract_version"], "execution_evidence": evidence}
    candidate_evidence_hash = sha256_prefixed(stable_projection)
    recorded_report_hash = report.get("content_hash")
    if recorded_report_hash is not None:
        actual_report_hash = sha256_prefixed(report_content_hash_payload(report))
        if recorded_report_hash != actual_report_hash:
            raise StrategyPackageError("strategy_package_source_report_content_hash_mismatch")
    package = {
        "schema_version": 2, "selected_candidate_id": selected_id,
        **{field: merged[field] for field in _CONTRACT_FIELDS},
        "execution_timing_hash": executed_timing,
        "execution_timing_stream_hash": evidence.get("execution_timing_stream_hash", evidence.get("executed_execution_timing_hash")),
        "execution_model_hash": evidence["executed_execution_model_hash"],
        "request_stream_hash": evidence["execution_request_stream_hash"],
        "fill_stream_hash": evidence["execution_fill_stream_hash"],
        "ledger_stream_hash": evidence.get("ledger_stream_hash", evidence.get("portfolio_ledger_hash")),
        "validation_result": "PASS",
        "source_report_content_hash": recorded_report_hash or sha256_prefixed(report),
        "selected_candidate_evidence_hash": candidate_evidence_hash,
    }
    forbidden = {"account", "credential", "private_api", "submit_order", "api_key", "api_secret", "order_submission"}
    def contains_forbidden(value: object) -> bool:
        if isinstance(value, dict):
            return any(str(key).lower() in forbidden or contains_forbidden(item) for key, item in value.items())
        if isinstance(value, (list, tuple)):
            return any(contains_forbidden(item) for item in value)
        return False
    if contains_forbidden(package):
        raise StrategyPackageError("strategy_package_operational_field_forbidden")
    package["content_hash"] = sha256_prefixed(package)
    return package
