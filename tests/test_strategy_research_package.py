from __future__ import annotations

import pytest

from market_research.research.strategy_package import StrategyPackageError, build_strategy_research_package


def _result():
    evidence = {"declared_execution_timing_hash": "sha256:t", "executed_execution_timing_hash": "sha256:et", "declared_execution_model_hash": "sha256:m", "executed_execution_model_hash": "sha256:m", "execution_request_count": 1, "execution_model_invocation_count": 1, "fill_count": 1, "execution_request_stream_hash": "sha256:r", "execution_fill_stream_hash": "sha256:f", "portfolio_ledger_hash": "sha256:l", "timing_invariant_status": "PASS"}
    candidate = {"parameter_candidate_id": "candidate-1", "strategy_spec_hash": "sha256:spec", "decision_contract_version": "v1", "execution_evidence": evidence, "data_requirements": {}, "execution_timing_policy": {}, "execution_model": {}, "cost_assumption": {}, "partial_fill_assumptions": {}, "order_failure_assumptions": {}, "portfolio_policy": {}, "risk_policy": {}, "execution_limitations": [], "suspension_or_invalidation_criteria": []}
    return {"final_selection_gate_result": "PASS", "selected_candidate_id": "candidate-1", "candidates": [candidate]}


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
