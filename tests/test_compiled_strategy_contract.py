from __future__ import annotations

import copy

import pytest

from market_research.research_composition import builtin_strategy_registry
from market_research.research.hashing import sha256_prefixed
from market_research.research.strategy_compiler import (
    StrategyCompilationError,
    StrategyCompiler,
    compiled_contract_from_payload,
    validate_compiled_strategy_contract,
)
from market_research.research.strategy_contract import CompiledStrategyContract


def _contract(*, long: int = 30):
    return StrategyCompiler(builtin_strategy_registry()).compile(
        strategy_name="sma_with_filter",
        raw_parameters={"SMA_SHORT": 5, "SMA_LONG": long},
        fee_rate=0,
        slippage_bps=0,
    )


def _rehash(payload: dict[str, object]) -> None:
    payload["compiled_contract_hash"] = sha256_prefixed(
        {key: value for key, value in payload.items() if key != "compiled_contract_hash"}
    )


def test_plugin_override_source_replaces_spec_default_source():
    contract = _contract()
    assert contract.parameter_source_map["SMA_FILTER_GAP_MIN_RATIO"] == "plugin_parameter_materializer"


def test_data_requirements_use_materialized_sma_long():
    short = _contract(long=30)
    long = _contract(long=200)
    short_capability = short.data_requirements["capabilities"][0]
    long_capability = long.data_requirements["capabilities"][0]
    assert short_capability["lookback_rows"] == 32
    assert long_capability["lookback_rows"] == 202
    assert short.data_requirements != long.data_requirements


def test_compiled_contract_nested_values_are_immutable():
    contract = _contract()
    with pytest.raises(TypeError):
        contract.materialized_parameters["SMA_LONG"] = 1
    with pytest.raises(TypeError):
        contract.data_requirements["capabilities"][0]["lookback_rows"] = 1
    with pytest.raises(TypeError):
        contract.exit_policy["rules"] += ("take_profit",)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        (lambda value: value.__setitem__("schema_version", 99), "schema_version_unsupported"),
        (lambda value: value.pop("strategy_name"), "required_field_missing"),
        (lambda value: value.__setitem__("strategy_plugin_contract_hash", "not-a-hash"), "hash_format_invalid"),
        (lambda value: value.__setitem__("strategy_registry_hash", "sha256:UPPER"), "hash_format_invalid"),
    ),
)
def test_hydration_rejects_invalid_complete_contract(mutation, reason):
    payload = _contract().as_dict()
    mutation(payload)
    _rehash(payload)
    with pytest.raises(StrategyCompilationError, match=reason):
        compiled_contract_from_payload(payload)


def test_hydration_rejects_materialized_parameter_hash_mismatch():
    payload = copy.deepcopy(_contract().as_dict())
    payload["materialized_parameters"]["SMA_LONG"] = 31
    _rehash(payload)
    with pytest.raises(StrategyCompilationError, match="materialized_parameters_hash_mismatch"):
        compiled_contract_from_payload(payload)


def test_hydration_rejects_capability_hash_mismatch():
    payload = copy.deepcopy(_contract().as_dict())
    payload["capability_contract"]["pyramiding"] = True
    _rehash(payload)
    with pytest.raises(StrategyCompilationError, match="capability_contract_hash_mismatch"):
        compiled_contract_from_payload(payload)


def test_hydrated_contract_remains_recursively_immutable():
    hydrated = compiled_contract_from_payload(_contract().as_dict())
    with pytest.raises(TypeError):
        hydrated.capability_contract["nested"] = {}


@pytest.mark.parametrize("mutation", (
    lambda p: p["capability_contract"].__setitem__("pyramiding", "false"),
    lambda p: p["capability_contract"].__setitem__("direction", "short"),
    lambda p: p["capability_contract"].__setitem__("portfolio_mode", "unknown"),
    lambda p: p["capability_contract"].__setitem__("instrument_count", 0),
    lambda p: p["capability_contract"].__setitem__("max_concurrent_positions", True),
    lambda p: p["capability_contract"].__setitem__("max_intents_per_decision", -1),
    lambda p: p["data_requirements"]["required_data"].append(1),
    lambda p: p["data_requirements"]["required_data"].append("candles"),
    lambda p: p["data_requirements"]["optional_data"].append("candles"),
    lambda p: p["data_requirements"]["capabilities"].append(copy.deepcopy(p["data_requirements"]["capabilities"][0])),
    lambda p: p["data_requirements"]["capabilities"][0].__setitem__("lookback_rows", "200"),
    lambda p: p["data_requirements"]["capabilities"][0].__setitem__("min_rows", -1),
    lambda p: p["data_requirements"]["capabilities"][0].__setitem__("min_coverage_pct", 101),
    lambda p: p["parameter_source_map"].__setitem__("SMA_LONG", "unknown_source"),
    lambda p: p["exit_policy"].__setitem__("rules", "stop_loss"),
))
def test_hydration_rejects_semantically_invalid_self_hashed_payload(mutation):
    payload = _contract().as_dict(); mutation(payload)
    payload["capability_contract_hash"] = sha256_prefixed(payload["capability_contract"])
    _rehash(payload)
    with pytest.raises(StrategyCompilationError):
        compiled_contract_from_payload(payload)


def test_direct_contract_object_uses_same_semantic_validator():
    payload = _contract().as_dict()
    payload["capability_contract"]["pyramiding"] = "false"
    payload["capability_contract_hash"] = sha256_prefixed(payload["capability_contract"])
    _rehash(payload)
    direct = CompiledStrategyContract(**payload)
    with pytest.raises(StrategyCompilationError, match="capability_payload_invalid"):
        validate_compiled_strategy_contract(direct)
