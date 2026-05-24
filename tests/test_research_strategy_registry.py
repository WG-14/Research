from __future__ import annotations

import json
import inspect
from dataclasses import replace

import pytest

from bithumb_bot.research.strategy_registry import (
    TEST_TOP_OF_BOOK_REQUIRED_STRATEGY,
    ResearchStrategyRegistryError,
    RuntimeParameterAdapter,
    research_strategy_data_requirements,
    resolve_research_strategy_plugin,
    resolve_research_strategy,
    runtime_strategy_parameter_env_keys,
)
import bithumb_bot.research.strategy_registry as strategy_registry
from bithumb_bot.research.strategy_spec import strategy_spec_for_name


def test_research_strategy_registry_resolves_sma_with_filter() -> None:
    runner = resolve_research_strategy("sma_with_filter")

    assert callable(runner)
    plugin = resolve_research_strategy_plugin("sma_with_filter")
    assert plugin.name == "sma_with_filter"
    assert plugin.runner is runner
    assert plugin.spec is strategy_spec_for_name("sma_with_filter")
    assert plugin.runtime_replay_builder is not None
    assert plugin.contract_payload()["diagnostics_namespace"] == "sma_with_filter"
    assert plugin.contract_payload()["runtime_replay_supported"] is True
    assert plugin.contract_payload()["runner_module"] == "bithumb_bot.research.strategy_registry"
    assert plugin.contract_payload()["runner_qualname"] == "_run_sma_with_filter"
    assert plugin.contract_payload()["runtime_replay_builder_module"] == "bithumb_bot.research.strategy_registry"
    assert plugin.contract_payload()["runtime_replay_builder_qualname"] == "_build_sma_runtime_replay_strategy"
    assert plugin.contract_payload()["runtime_parameter_adapter_supported"] is True
    assert plugin.contract_payload()["runtime_parameter_env_keys"] == list(
        runtime_strategy_parameter_env_keys("sma_with_filter")
    )
    assert "SMA_SHORT" in runtime_strategy_parameter_env_keys("sma_with_filter")
    assert plugin.contract_payload()["runtime_parameter_from_env_module"] == "bithumb_bot.research.strategy_registry"
    assert plugin.contract_payload()["runtime_parameter_from_env_qualname"] == "_sma_runtime_parameters_from_env"
    assert (
        plugin.contract_payload()["runtime_parameter_from_settings_module"]
        == "bithumb_bot.research.strategy_registry"
    )
    assert (
        plugin.contract_payload()["runtime_parameter_from_settings_qualname"]
        == "_sma_runtime_parameters_from_settings"
    )
    assert plugin.contract_hash() == resolve_research_strategy_plugin("sma_with_filter").contract_hash()
    assert plugin.contract_hash() == plugin.contract_hash()
    requirements = research_strategy_data_requirements("sma_with_filter")
    assert requirements.required_data == ("candles",)
    assert requirements.optional_data == ("top_of_book",)


def test_research_strategy_registry_resolves_noop_baseline_as_independent_plugin() -> None:
    runner = resolve_research_strategy("noop_baseline")
    plugin = resolve_research_strategy_plugin("noop_baseline")
    sma_plugin = resolve_research_strategy_plugin("sma_with_filter")

    assert callable(runner)
    assert plugin.name == "noop_baseline"
    assert plugin.runner is runner
    assert plugin.spec is strategy_spec_for_name("noop_baseline")
    assert plugin.spec is not sma_plugin.spec
    assert plugin.contract_hash() != sma_plugin.contract_hash()
    assert plugin.runtime_replay_builder is None
    assert plugin.contract_payload()["runtime_replay_supported"] is False
    assert plugin.contract_payload()["runtime_parameter_adapter_supported"] is False
    assert plugin.contract_payload()["runtime_parameter_from_env_module"] is None
    assert plugin.contract_payload()["runtime_parameter_from_env_qualname"] is None
    assert plugin.contract_payload()["runtime_parameter_from_settings_module"] is None
    assert plugin.contract_payload()["runtime_parameter_from_settings_qualname"] is None
    assert plugin.contract_payload()["diagnostics_namespace"] == "noop_baseline"
    assert plugin.contract_payload()["runner_qualname"] == "_run_noop_baseline"
    requirements = research_strategy_data_requirements("noop_baseline")
    assert requirements.required_data == ("candles",)
    assert requirements.optional_data == ()


def test_buy_and_hold_contract_declares_no_runtime_parameter_adapter() -> None:
    plugin = resolve_research_strategy_plugin("buy_and_hold_baseline")
    payload = plugin.contract_payload()

    assert payload["runtime_parameter_adapter_supported"] is False
    assert payload["runtime_parameter_from_env_module"] is None
    assert payload["runtime_parameter_from_env_qualname"] is None
    assert payload["runtime_parameter_from_settings_module"] is None
    assert payload["runtime_parameter_from_settings_qualname"] is None


def test_runtime_parameter_adapter_identity_is_contract_bound_and_deterministic() -> None:
    def alternate_from_env(_env):
        return {"SMA_SHORT": "2", "SMA_LONG": "4"}

    def alternate_from_settings(_cfg):
        return {"SMA_SHORT": 2, "SMA_LONG": 4}

    plugin = resolve_research_strategy_plugin("sma_with_filter")
    changed = replace(
        plugin,
        runtime_parameter_adapter=RuntimeParameterAdapter(
            from_env=alternate_from_env,
            from_settings=alternate_from_settings,
            env_keys=("ALT_KEY",),
        ),
    )

    assert plugin.contract_payload() != changed.contract_payload()
    assert plugin.contract_hash() != changed.contract_hash()
    assert changed.contract_payload()["runtime_parameter_from_env_module"] == __name__
    assert changed.contract_payload()["runtime_parameter_from_env_qualname"].endswith(
        ".<locals>.alternate_from_env"
    )
    assert changed.contract_payload()["runtime_parameter_from_settings_module"] == __name__
    assert changed.contract_payload()["runtime_parameter_from_settings_qualname"].endswith(
        ".<locals>.alternate_from_settings"
    )
    assert changed.contract_payload()["runtime_parameter_env_keys"] == ["ALT_KEY"]
    encoded = json.dumps(plugin.contract_payload(), sort_keys=True)
    assert "<function" not in encoded
    assert " object at 0x" not in encoded


def test_research_strategy_registry_rejects_unknown_strategy() -> None:
    with pytest.raises(ResearchStrategyRegistryError, match="unsupported research strategy"):
        resolve_research_strategy("profit_hunter")
    with pytest.raises(ResearchStrategyRegistryError, match="unsupported research strategy"):
        resolve_research_strategy_plugin("profit_hunter")


def test_sma_export_normalizer_is_not_imported_from_profile_cli() -> None:
    source = inspect.getsource(strategy_registry)

    assert "from bithumb_bot.profile_cli" not in source
    assert "_sma_promotion_grade_research_export_decisions" not in source
    assert "sma_promotion_grade_research_export_decisions" in source


def test_top_of_book_required_test_hook_is_private_by_name() -> None:
    assert TEST_TOP_OF_BOOK_REQUIRED_STRATEGY.startswith("__test_")
    assert TEST_TOP_OF_BOOK_REQUIRED_STRATEGY.endswith("__")
    requirements = research_strategy_data_requirements(TEST_TOP_OF_BOOK_REQUIRED_STRATEGY)

    assert requirements.required_data == ("candles", "top_of_book")


def test_old_top_of_book_required_test_name_is_not_operator_supported() -> None:
    with pytest.raises(ResearchStrategyRegistryError, match="unsupported research strategy"):
        resolve_research_strategy("top_of_book_required_test")
