import pytest
from dataclasses import replace
from market_research.research_composition import builtin_strategy_registry
from market_research.research.strategy_compiler import StrategyCompilationError, StrategyCompiler
from market_research.research.strategy_contract import StrategyCapabilityContract
from market_research.research.strategy_registry import StrategyRegistry


def test_single_asset_long_only_plugin_is_accepted():
    StrategyCompiler(builtin_strategy_registry()).compile(strategy_name="noop_baseline", raw_parameters={}, fee_rate=0, slippage_bps=0)


def test_partial_exit_requirement_is_accepted_by_common_ledger_capability():
    plugin = replace(
        builtin_strategy_registry().resolve("noop_baseline"),
        required_capabilities=StrategyCapabilityContract(partial_exit=True),
    )
    StrategyCompiler(StrategyRegistry.build((plugin,))).compile(
        strategy_name=plugin.name,
        raw_parameters={},
        fee_rate=0,
        slippage_bps=0,
    )


def test_pyramiding_requirement_is_rejected_before_simulation():
    plugin = replace(builtin_strategy_registry().resolve("noop_baseline"),
                     required_capabilities=StrategyCapabilityContract(pyramiding=True))
    with pytest.raises(StrategyCompilationError, match="unsupported_strategy_capability"):
        StrategyCompiler(StrategyRegistry.build((plugin,))).compile(strategy_name=plugin.name,
            raw_parameters={}, fee_rate=0, slippage_bps=0)


@pytest.mark.parametrize("changes", [
    {"direction": "long_short"}, {"instrument_count": 2},
    {"max_intents_per_decision": 2},
])
def test_unsupported_requirements_are_rejected(changes):
    plugin = replace(builtin_strategy_registry().resolve("noop_baseline"),
                     required_capabilities=StrategyCapabilityContract(**changes))
    with pytest.raises(StrategyCompilationError, match="unsupported_strategy_capability"):
        StrategyCompiler(StrategyRegistry.build((plugin,))).compile(strategy_name=plugin.name,
            raw_parameters={}, fee_rate=0, slippage_bps=0)
