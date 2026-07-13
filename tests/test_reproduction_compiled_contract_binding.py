import copy

import pytest

from market_research.research.hashing import sha256_prefixed
from market_research.research.strategy_compiler import StrategyCompilationError, compiled_contract_from_payload
from tests.test_strategy_research_package import _result
from market_research.research.reproduction import ReproductionContractError, _scenario_fingerprint


def test_incomplete_compiled_payload_is_rejected_even_when_self_hashed():
    payload = copy.deepcopy(_result()["candidates"][0]["compiled_strategy_contract"])
    payload.pop("data_requirements")
    payload["compiled_contract_hash"] = sha256_prefixed(
        {key: value for key, value in payload.items() if key != "compiled_contract_hash"})
    with pytest.raises(StrategyCompilationError, match="required_field_missing"):
        compiled_contract_from_payload(payload)


def test_embedded_registry_hash_mismatch_is_rejected():
    payload = copy.deepcopy(_result()["candidates"][0]["compiled_strategy_contract"])
    with pytest.raises(StrategyCompilationError, match="identity_mismatch"):
        compiled_contract_from_payload(payload, expected_registry_hash=sha256_prefixed("different"))


def test_scenario_fingerprint_contains_compiled_contract_hash():
    compiled = _result()["candidates"][0]["compiled_strategy_contract"]
    digest = lambda value: sha256_prefixed(value)
    evidence = {"decision_stream_hash": digest("d"), "execution_request_stream_hash": digest("r"),
                "execution_fill_stream_hash": digest("f"), "ledger_stream_hash": digest("l")}
    scenario = {"scenario_index": 0, "scenario_id": "base", "scenario_role": "base",
        "compiled_strategy_contract": compiled, "compiled_strategy_contract_hash": compiled["compiled_contract_hash"],
        "metrics_hash": digest("m"), "behavior_hash": digest("b"),
        "strategy_behavior_hash": digest("sb"), "trade_ledger_hash": digest("tl"),
        "equity_curve_hash": digest("e"), "composite_behavior_hash": digest("c"),
        "execution_model_hash": digest("x"), "portfolio_policy_hash": digest("p"),
        "execution_evidence": evidence}
    fingerprint = _scenario_fingerprint(scenario, "candidate")
    assert fingerprint["compiled_strategy_contract_hash"] == compiled["compiled_contract_hash"]


def test_scenario_fingerprint_rejects_missing_execution_evidence():
    compiled = _result()["candidates"][0]["compiled_strategy_contract"]
    digest = lambda value: sha256_prefixed(value)
    scenario = {
        "scenario_index": 0,
        "scenario_id": "base",
        "scenario_role": "base",
        "compiled_strategy_contract": compiled,
        "compiled_strategy_contract_hash": compiled["compiled_contract_hash"],
        "metrics_hash": digest("m"),
        "behavior_hash": digest("b"),
        "strategy_behavior_hash": digest("sb"),
        "trade_ledger_hash": digest("tl"),
        "equity_curve_hash": digest("e"),
        "composite_behavior_hash": digest("c"),
        "execution_model_hash": digest("x"),
        "portfolio_policy_hash": digest("p"),
    }

    with pytest.raises(ReproductionContractError, match="execution_evidence is required"):
        _scenario_fingerprint(scenario, "candidate")
