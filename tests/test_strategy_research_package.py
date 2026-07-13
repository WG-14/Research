from __future__ import annotations

import pytest

from market_research.research.strategy_package import StrategyPackageError, build_strategy_research_package
from market_research.research.hashing import sha256_prefixed
from market_research.research.hashing import report_content_hash_payload
from market_research.research.builtin_registry import builtin_strategy_registry
from market_research.research.strategy_compiler import StrategyCompiler


def _result():
    compiled = StrategyCompiler(builtin_strategy_registry()).compile(
        strategy_name="noop_baseline", raw_parameters={}, fee_rate=0, slippage_bps=0).as_dict()
    capability = compiled["capability_contract"]
    evidence = {"declared_execution_timing_hash": "sha256:t", "executed_execution_timing_hash": "sha256:t", "declared_execution_model_hash": "sha256:m", "executed_execution_model_hash": "sha256:m", "execution_request_count": 1, "execution_model_invocation_count": 1, "fill_count": 1, "decision_stream_hash": "sha256:d", "metrics_hash": "sha256:metrics", "execution_request_stream_hash": "sha256:r", "execution_fill_stream_hash": "sha256:f", "portfolio_ledger_hash": "sha256:l", "timing_invariant_status": "PASS"}
    scenario = {"scenario_id": "base", "compiled_strategy_contract": compiled,
                "compiled_strategy_contract_hash": compiled["compiled_contract_hash"],
                "execution_evidence": evidence}
    candidate = {"parameter_candidate_id": "candidate-1", "primary_scenario_id": "base",
                 "scenario_results": [scenario], "strategy_spec_hash": "sha256:spec",
                 "strategy_registry_hash": compiled["strategy_registry_hash"],
                 "strategy_plugin_contract_hash": compiled["strategy_plugin_contract_hash"],
                 "compiled_strategy_contract": compiled, "compiled_strategy_contract_hash": compiled["compiled_contract_hash"],
                 "capability_contract_hash": compiled["capability_contract_hash"], "capability_contract": capability,
                 "metrics_hash": "sha256:metrics", "decision_contract_version": "v1", "execution_evidence": evidence,
                 "data_requirements": {}, "execution_timing_policy": {}, "execution_model": {}, "cost_assumption": {},
                 "partial_fill_assumptions": {}, "order_failure_assumptions": {}, "portfolio_policy": {}, "risk_policy": {},
                 "execution_limitations": [], "suspension_or_invalidation_criteria": []}
    report = {"final_selection_gate_result": "PASS", "selected_candidate_id": "candidate-1", "candidates": [candidate]}
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))
    return report


def test_package_contains_execution_and_ledger_contract_hashes(monkeypatch):
    monkeypatch.setattr("market_research.research.strategy_package.validate_final_selection_report", lambda report: [])
    package = build_strategy_research_package(_result())
    assert package["execution_model_hash"] == "sha256:m" and package["ledger_stream_hash"] == "sha256:l"
    assert build_strategy_research_package(_result())["content_hash"] == package["content_hash"]


def test_package_rejects_missing_execution_evidence(monkeypatch):
    monkeypatch.setattr("market_research.research.strategy_package.validate_final_selection_report", lambda report: [])
    value = _result(); value["candidates"][0]["execution_evidence"].pop("portfolio_ledger_hash")
    with pytest.raises(StrategyPackageError, match="missing_execution_evidence"):
        build_strategy_research_package(value)
