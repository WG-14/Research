from __future__ import annotations

import copy

import pytest

from market_research.builtin_strategies.sma_exit_rules import evaluate_sma_exit_policy
from market_research.research.experiment_manifest import ManifestValidationError
from market_research.research.position_model import ResearchPosition
from market_research.research.strategy_compiler import StrategyCompiler
from market_research.research_composition import builtin_strategy_registry, parse_builtin_manifest
from tests.test_research_semantics_v2_contract import _manifest_payload


def _sma_payload(rules: str, **parameters):
    payload = copy.deepcopy(_manifest_payload())
    payload["strategy_name"] = "sma_with_filter"
    payload["strategy_version"] = "sma_with_filter.research_runtime_contract.v2"
    payload["parameter_space"] = {
        "SMA_SHORT": [2], "SMA_LONG": [3], "STRATEGY_EXIT_RULES": [rules],
        **{key: [value] for key, value in parameters.items()},
    }
    return payload


@pytest.mark.parametrize(
    ("rules", "parameter", "value"),
    (("take_profit", "STRATEGY_EXIT_TAKE_PROFIT_RATIO", 0.02),
     ("edge_invalidation", "STRATEGY_EXIT_MIN_EDGE_RATIO", 0.01),
     ("max_holding_time", "STRATEGY_EXIT_MAX_HOLDING_MIN", 5),
     ("stop_loss", "STRATEGY_EXIT_STOP_LOSS_RATIO", 0.03)),
)
def test_each_sma_exit_rule_parses_and_compiles_from_manifest(rules, parameter, value):
    manifest = parse_builtin_manifest(_sma_payload(rules, **{parameter: value}))
    contract = StrategyCompiler(builtin_strategy_registry()).compile(
        strategy_name=manifest.strategy_name,
        raw_parameters={key: values[0] for key, values in manifest.parameter_space.items()},
        fee_rate=0.0,
        slippage_bps=0.0,
    )
    assert contract.exit_policy["rules"] == (rules,)


def test_exit_threshold_without_matching_rule_fails_closed():
    with pytest.raises(ManifestValidationError, match="does not include edge_invalidation"):
        parse_builtin_manifest(_sma_payload("stop_loss", STRATEGY_EXIT_MIN_EDGE_RATIO=0.01))


def test_take_profit_and_edge_invalidation_trigger_independently():
    position = ResearchPosition(cash=0, asset_qty=1, entry_price=100, entry_ts=0, sellable_qty=1)
    take = evaluate_sma_exit_policy(policy={"rules": ["take_profit"],
        "take_profit": {"take_profit_ratio": 0.05}}, position=position, candle_ts=60_000,
        market_price=106, exit_signal="HOLD")
    edge = evaluate_sma_exit_policy(policy={"rules": ["edge_invalidation"],
        "edge_invalidation": {"min_edge_ratio": 0.02}}, position=position, candle_ts=60_000,
        market_price=100, exit_signal="HOLD", feature_state={"gap_ratio": 0.01})
    assert take.triggered and take.rule == "take_profit"
    assert edge.triggered and edge.rule == "edge_invalidation"
