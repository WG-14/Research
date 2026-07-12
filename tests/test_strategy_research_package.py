from __future__ import annotations

import pytest

from market_research.research.strategy_package import StrategyPackageError, build_strategy_research_package


def _result():
    return {"final_selection_result": "PASS", "strategy_spec_hash": "sha256:spec", "decision_contract_version": "v1", "execution_evidence": {"declared_execution_timing_hash": "sha256:t", "executed_execution_timing_hash": "sha256:t", "declared_execution_model_hash": "sha256:m", "executed_execution_model_hash": "sha256:m", "execution_request_count": 1, "execution_model_invocation_count": 1, "fill_count": 1, "execution_request_stream_hash": "sha256:r", "execution_fill_stream_hash": "sha256:f", "portfolio_ledger_hash": "sha256:l", "timing_invariant_status": "PASS"}}


def test_package_contains_execution_and_ledger_contract_hashes():
    package = build_strategy_research_package(_result())
    assert package["execution_model_hash"] == "sha256:m" and package["ledger_stream_hash"] == "sha256:l"
    assert build_strategy_research_package(_result())["content_hash"] == package["content_hash"]


def test_package_rejects_missing_execution_evidence():
    value = _result(); value["execution_evidence"].pop("portfolio_ledger_hash")
    with pytest.raises(StrategyPackageError, match="missing_execution_evidence"):
        build_strategy_research_package(value)
