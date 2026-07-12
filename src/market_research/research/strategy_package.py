"""Deterministic offline Strategy Research Package export."""

from __future__ import annotations

from typing import Any

from .execution_evidence import REQUIRED_FIELDS
from .hashing import sha256_prefixed


class StrategyPackageError(ValueError):
    pass


def build_strategy_research_package(result: dict[str, Any]) -> dict[str, Any]:
    """Build an immutable review artifact; this repository never submits orders."""
    if str(result.get("final_selection_result") or result.get("acceptance_gate_result") or "") != "PASS":
        raise StrategyPackageError("strategy_package_requires_final_selection_pass")
    if result.get("legacy_execution_authority") or result.get("legacy_vertical_kernel"):
        raise StrategyPackageError("strategy_package_rejects_legacy_execution")
    evidence = dict(result.get("execution_evidence") or result.get("validation_execution_event_summary") or {})
    missing = sorted(REQUIRED_FIELDS - set(evidence))
    if missing:
        raise StrategyPackageError("strategy_package_missing_execution_evidence:" + ",".join(missing))
    if evidence.get("timing_invariant_status") != "PASS":
        raise StrategyPackageError("strategy_package_timing_invariant_failure")
    package = {
        "schema_version": 1,
        "strategy_spec_hash": result.get("strategy_spec_hash"),
        "decision_contract_version": result.get("decision_contract_version"),
        "data_requirements": result.get("data_requirements"),
        "execution_timing_policy": result.get("execution_timing_policy"),
        "execution_timing_hash": evidence["executed_execution_timing_hash"],
        "execution_model": result.get("execution_model"),
        "execution_model_hash": evidence["executed_execution_model_hash"],
        "cost_assumption": result.get("cost_assumption"),
        "partial_fill_assumptions": result.get("partial_fill_assumptions"),
        "order_failure_assumptions": result.get("order_failure_assumptions"),
        "portfolio_policy": result.get("portfolio_policy"),
        "risk_policy": result.get("risk_policy"),
        "execution_limitations": result.get("execution_limitations", ()),
        "request_stream_hash": evidence["execution_request_stream_hash"],
        "fill_stream_hash": evidence["execution_fill_stream_hash"],
        "ledger_stream_hash": evidence["portfolio_ledger_hash"],
        "validation_result": "PASS",
        "suspension_or_invalidation_criteria": result.get("suspension_or_invalidation_criteria", ()),
    }
    package["content_hash"] = sha256_prefixed(package)
    return package
