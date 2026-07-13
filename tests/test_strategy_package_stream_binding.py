import copy

import pytest

from market_research.research.hashing import report_content_hash_payload, sha256_prefixed
from market_research.research.strategy_package import StrategyPackageError, build_strategy_research_package
from tests.test_strategy_research_package import _approval, _result
from market_research.research_composition import builtin_strategy_registry
from market_research.research.strategy_compiler import StrategyCompiler


def _rehash(report):
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))


def test_metrics_hash_mismatch_is_rejected(monkeypatch):
    monkeypatch.setattr("market_research.research.strategy_package.validate_final_selection_report", lambda report: [])
    report = copy.deepcopy(_result())
    report["candidates"][0]["execution_evidence"]["metrics_hash"] = "sha256:other"
    report["candidates"][0]["scenario_results"][0]["execution_evidence"]["metrics_hash"] = "sha256:other"
    _rehash(report)
    with pytest.raises(StrategyPackageError, match="metrics_hash_mismatch"):
        build_strategy_research_package(report)


def test_missing_source_report_content_hash_is_rejected(monkeypatch):
    monkeypatch.setattr("market_research.research.strategy_package.validate_final_selection_report", lambda report: [])
    report = _result(); report.pop("content_hash")
    with pytest.raises(StrategyPackageError, match="source_report_content_hash_missing"):
        build_strategy_research_package(report)


@pytest.mark.parametrize(("payload_key", "expected"), (
    ("decision_stream", "decision_stream_tampered"),
    ("execution_request_stream", "execution_request_stream_tampered"),
    ("execution_fill_stream", "execution_fill_stream_tampered"),
    ("ledger_stream", "ledger_stream_tampered"),
))
def test_tampered_authoritative_stream_is_rejected(monkeypatch, payload_key, expected):
    monkeypatch.setattr("market_research.research.strategy_package.validate_final_selection_report", lambda report: [])
    report = copy.deepcopy(_result())
    report["candidates"][0]["scenario_results"][0][payload_key] = [{"tampered": True}]
    _rehash(report)
    with pytest.raises(StrategyPackageError, match=expected):
        build_strategy_research_package(report)


def test_package_uses_primary_scenario_compiled_contract(monkeypatch, tmp_path):
    monkeypatch.setattr("market_research.research.strategy_package.validate_final_selection_report", lambda report: [])
    report = copy.deepcopy(_result())
    compiler = StrategyCompiler(builtin_strategy_registry())
    base = compiler.compile(strategy_name="sma_with_filter", raw_parameters={"SMA_SHORT": 1, "SMA_LONG": 2},
                            fee_rate=.001, slippage_bps=10).as_dict()
    stress = compiler.compile(strategy_name="sma_with_filter", raw_parameters={"SMA_SHORT": 1, "SMA_LONG": 2},
                              fee_rate=.002, slippage_bps=25).as_dict()
    strategy_spec = builtin_strategy_registry().resolve("sma_with_filter").spec.as_dict()
    candidate = report["candidates"][0]
    evidence = candidate["execution_evidence"]
    candidate.update({"primary_scenario_id": "stress", "compiled_strategy_contract": stress,
        "compiled_strategy_contract_hash": stress["compiled_contract_hash"],
        "strategy_registry_hash": stress["strategy_registry_hash"],
        "strategy_plugin_contract_hash": stress["strategy_plugin_contract_hash"],
        "capability_contract": stress["capability_contract"],
        "capability_contract_hash": stress["capability_contract_hash"],
        "effective_strategy_parameters": stress["materialized_parameters"],
        "effective_strategy_parameters_hash": stress["materialized_parameters_hash"],
        "strategy_spec": strategy_spec, "strategy_spec_hash": sha256_prefixed(strategy_spec)})
    report["strategy_spec"] = strategy_spec
    report["strategy_spec_hash"] = sha256_prefixed(strategy_spec)
    candidate["scenario_results"] = [
        {"scenario_id": "base", "compiled_strategy_contract": base,
         "compiled_strategy_contract_hash": base["compiled_contract_hash"], "execution_evidence": evidence,
         "validation_metrics": {"return_pct": 1.0}},
        {"scenario_id": "stress", "compiled_strategy_contract": stress,
         "compiled_strategy_contract_hash": stress["compiled_contract_hash"], "execution_evidence": evidence,
         "validation_metrics": {"return_pct": 0.5}},
    ]
    _rehash(report)
    package = build_strategy_research_package(report, approval=_approval(report, tmp_path))
    assert package["compiled_strategy_contract_hash"] == stress["compiled_contract_hash"]
