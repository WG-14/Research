from dataclasses import replace
from market_research.research_composition import builtin_strategy_registry


def test_plugin_contract_binds_every_behavior_hook():
    plugin = builtin_strategy_registry().resolve("sma_with_filter")
    hooks = plugin.contract_payload()["behavior_hooks"]
    assert {
        "parameter_materializer",
        "runtime_factory",
        "event_builder_compatibility",
        "exit_policy_materializer",
        "exit_decision_builder",
        "data_requirements_builder",
    } <= set(hooks)


def test_changing_runtime_callback_changes_plugin_contract_hash():
    plugin = builtin_strategy_registry().resolve("noop_baseline")
    changed = replace(plugin, runtime_factory=lambda: None)
    assert changed.contract_hash() != plugin.contract_hash()
