from market_research.research_composition import builtin_strategy_registry
from market_research.research.strategy_compiler import StrategyCompiler
from market_research.research.strategy_contract import ParameterExtensionResult
import market_research.research.strategy_compiler as compiler_module
import inspect


def test_parameter_source_map_covers_every_materialized_parameter():
    contract = StrategyCompiler(builtin_strategy_registry()).compile(strategy_name="sma_with_filter",
        raw_parameters={"SMA_SHORT": 2, "SMA_LONG": 3}, fee_rate=.001, slippage_bps=10)
    assert set(contract.materialized_parameters) == set(contract.parameter_source_map)
    assert contract.compiled_contract_hash.startswith("sha256:")


def test_same_candidate_scenario_compiles_identically():
    compiler = StrategyCompiler(builtin_strategy_registry())
    values = dict(strategy_name="noop_baseline", raw_parameters={}, fee_rate=.001, slippage_bps=10)
    assert compiler.compile(**values).compiled_contract_hash == compiler.compile(**values).compiled_contract_hash


def test_spec_materialization_occurs_once_per_candidate_scenario(monkeypatch):
    calls = 0
    original = compiler_module.materialize_parameters_from_spec

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(compiler_module, "materialize_parameters_from_spec", counted)
    StrategyCompiler(builtin_strategy_registry()).compile(strategy_name="sma_with_filter",
        raw_parameters={"SMA_SHORT": 2, "SMA_LONG": 3}, fee_rate=.001, slippage_bps=10)
    assert calls == 1


def test_plugin_extension_receives_materialized_parameters():
    plugin = builtin_strategy_registry().resolve("sma_with_filter")
    observed = {}

    def extension(*, materialized, context):
        del context
        observed.update(materialized.values)
        return ParameterExtensionResult(values=materialized.values, source_overrides={})

    from dataclasses import replace
    from market_research.research.strategy_registry import StrategyRegistry
    changed = replace(plugin, parameter_materializer=extension)
    StrategyCompiler(StrategyRegistry.build((changed,))).compile(strategy_name=changed.name,
        raw_parameters={"SMA_SHORT": 2, "SMA_LONG": 3}, fee_rate=.001, slippage_bps=10)
    assert observed["LIVE_FEE_RATE_ESTIMATE"] == .001
    assert observed["STRATEGY_ENTRY_SLIPPAGE_BPS"] == 10


def test_strategy_reads_compiled_cost_evidence_not_runtime_cost_arguments():
    plugin = builtin_strategy_registry().resolve("sma_with_filter")
    source = inspect.getsource(plugin.event_builder)

    assert 'parameter_values.get("STRATEGY_ENTRY_SLIPPAGE_BPS")' in source
    assert "float(slippage_bps)" not in source


def test_parameter_extension_cannot_receive_raw_parameters():
    plugin = builtin_strategy_registry().resolve("sma_with_filter")

    def legacy(*, parameter_values, fee_rate, slippage_bps):
        return parameter_values, fee_rate, slippage_bps

    from dataclasses import replace
    from market_research.research.strategy_registry import StrategyRegistry
    changed = replace(plugin, parameter_materializer=legacy)
    try:
        StrategyCompiler(StrategyRegistry.build((changed,))).compile(
            strategy_name=changed.name,
            raw_parameters={"SMA_SHORT": 2, "SMA_LONG": 3},
            fee_rate=.001,
            slippage_bps=10,
        )
    except TypeError as exc:
        assert "materialized" in str(exc)
    else:
        raise AssertionError("legacy parameter materializer signature was accepted")


def test_parameter_extension_materialized_payload_is_immutable():
    plugin = builtin_strategy_registry().resolve("sma_with_filter")

    def extension(*, materialized, context):
        del context
        materialized.values["SMA_SHORT"] = 99

    from dataclasses import replace
    from market_research.research.strategy_registry import StrategyRegistry
    changed = replace(plugin, parameter_materializer=extension)
    try:
        StrategyCompiler(StrategyRegistry.build((changed,))).compile(
            strategy_name=changed.name,
            raw_parameters={"SMA_SHORT": 2, "SMA_LONG": 3},
            fee_rate=.001,
            slippage_bps=10,
        )
    except TypeError:
        pass
    else:
        raise AssertionError("parameter extension mutated compiler-owned materialization")


def test_parameter_extension_returns_source_overrides_for_every_changed_key():
    plugin = builtin_strategy_registry().resolve("sma_with_filter")

    def extension(*, materialized, context):
        del context
        values = dict(materialized.values)
        values["SMA_SHORT"] = 99
        return ParameterExtensionResult(values=values, source_overrides={})

    from dataclasses import replace
    from market_research.research.strategy_compiler import StrategyCompilationError
    from market_research.research.strategy_registry import StrategyRegistry
    changed = replace(plugin, parameter_materializer=extension)
    try:
        StrategyCompiler(StrategyRegistry.build((changed,))).compile(
            strategy_name=changed.name,
            raw_parameters={"SMA_SHORT": 2, "SMA_LONG": 3},
            fee_rate=.001,
            slippage_bps=10,
        )
    except StrategyCompilationError as exc:
        assert exc.reason_code == "parameter_extension_source_overrides_invalid"
    else:
        raise AssertionError("changed parameter without source override was accepted")
