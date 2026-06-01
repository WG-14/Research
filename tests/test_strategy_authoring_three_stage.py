from __future__ import annotations

import ast
import importlib.util
import sys
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.experiment_manifest import DateRange
from bithumb_bot.research.strategy_registry import (
    list_research_strategy_plugins,
    reload_research_strategy_plugins_for_tests,
    resolve_research_strategy_plugin,
)
from bithumb_bot.strategy_authoring import ReplayCompatibleStrategyPlugin
from bithumb_bot.strategy_plugins.replay_threshold import (
    REPLAY_THRESHOLD_PLUGIN,
    REPLAY_THRESHOLD_SPEC,
    _REPLAY_THRESHOLD_RESEARCH_PLUGIN,
    _build_replay_threshold_strategy,
)
from tests.contracts.strategy_authoring_contracts import (
    assert_live_eligible_contract,
    assert_replay_compatible_contract,
    assert_research_only_contract,
)


def _dataset() -> DatasetSnapshot:
    return DatasetSnapshot(
        snapshot_id="three_stage_unit",
        source="unit",
        market="KRW-BTC",
        interval="1m",
        split_name="validation",
        date_range=DateRange(start="2023-01-01", end="2023-01-01"),
        candles=tuple(
            Candle(
                ts=1_700_000_000_000 + index * 60_000,
                open=100.0 + index,
                high=100.0 + index,
                low=100.0 + index,
                close=100.0 + index,
                volume=1.0,
            )
            for index in range(3)
        ),
    )


@dataclass(frozen=True)
class _FakeEntryPoint:
    name: str
    value: str
    plugin: object

    def load(self) -> object:
        return self.plugin


@pytest.fixture(autouse=True)
def _restore_plugin_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    yield
    monkeypatch.undo()
    from bithumb_bot.strategy_plugins import iter_builtin_strategy_plugins

    reload_research_strategy_plugins_for_tests(providers=(iter_builtin_strategy_plugins,))


def test_level_1_research_only_contract_helper_covers_threshold_example() -> None:
    plugin = resolve_research_strategy_plugin("threshold_research_only")

    assert_research_only_contract(plugin)


def test_level_2_replay_compatible_contract_helper_covers_minimal_example(tmp_path: Path) -> None:
    plugin = resolve_research_strategy_plugin("replay_threshold")

    assert plugin is REPLAY_THRESHOLD_PLUGIN
    assert_replay_compatible_contract(
        plugin,
        dataset=_dataset(),
        params={"REPLAY_THRESHOLD_CLOSE_ABOVE": 100.5},
        tmp_path=tmp_path,
    )


def test_level_2_public_authoring_object_is_discoverable_from_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import bithumb_bot.strategy_plugins as strategy_plugins

    from bithumb_bot.strategy_authoring import ReplayCompatibleStrategyExtension

    provider_plugin = ReplayCompatibleStrategyPlugin(
        research=_REPLAY_THRESHOLD_RESEARCH_PLUGIN,
        extension=ReplayCompatibleStrategyExtension(
            runtime_replay_builder=_build_replay_threshold_strategy,
        ),
    )
    monkeypatch.setattr(
        strategy_plugins.metadata,
        "entry_points",
        lambda: [_FakeEntryPoint("unit_replay_threshold", "tests:plugin", provider_plugin)],
    )

    reload_research_strategy_plugins_for_tests(providers=(strategy_plugins.iter_entry_point_strategy_plugins,))

    plugin = resolve_research_strategy_plugin("replay_threshold")
    assert_replay_compatible_contract(
        plugin,
        dataset=_dataset(),
        params={"REPLAY_THRESHOLD_CLOSE_ABOVE": 100.5},
        tmp_path=tmp_path,
    )


def test_level_2_strict_runtime_rejects_legacy_parameter_fallbacks() -> None:
    from bithumb_bot.config import settings
    from bithumb_bot.runtime_strategy_set import RuntimeDecisionRequestBuilder
    from bithumb_bot.runtime_strategy_set import RuntimeStrategySpec

    builder = RuntimeDecisionRequestBuilder(
        settings_obj=replace(
            settings,
            MODE="live",
            STRATEGY_PARAMETERS_JSON='{"EXAMPLE_REPLAY_CLOSE_ABOVE": 100.5}',
        )
    )

    with pytest.raises(RuntimeError, match="strict_runtime_rejects_strategy_parameters_json_fallback"):
        builder.build_for_spec(
            RuntimeStrategySpec(
                "replay_threshold",
                pair="KRW-BTC",
                interval="1m",
            ),
            through_ts_ms=None,
        )


def test_entry_point_scaffold_documents_and_verifies_level_1_and_level_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import bithumb_bot.strategy_plugins as strategy_plugins

    pyproject = Path("examples/strategy_plugin_package/pyproject.toml").read_text(encoding="utf-8")
    assert '[project.entry-points."bithumb_bot.strategy_plugins"]' in pyproject

    module_path = Path("examples/strategy_plugin_package/example_strategy_plugin.py")
    spec = importlib.util.spec_from_file_location("example_strategy_plugin", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["example_strategy_plugin"] = module
    spec.loader.exec_module(module)

    monkeypatch.setattr(
        strategy_plugins.metadata,
        "entry_points",
        lambda: [
            _FakeEntryPoint(
                "example_research_only",
                "example_strategy_plugin:LEVEL_1_RESEARCH_ONLY_PLUGIN",
                module.LEVEL_1_RESEARCH_ONLY_PLUGIN,
            ),
            _FakeEntryPoint(
                "example_replay_compatible",
                "example_strategy_plugin:LEVEL_2_REPLAY_COMPATIBLE_PLUGIN",
                module.LEVEL_2_REPLAY_COMPATIBLE_PLUGIN,
            ),
        ],
    )

    reload_research_strategy_plugins_for_tests(providers=(strategy_plugins.iter_entry_point_strategy_plugins,))

    assert_research_only_contract(resolve_research_strategy_plugin("example_external_research_only"))
    assert_replay_compatible_contract(
        resolve_research_strategy_plugin("example_external_replay_compatible"),
        dataset=_dataset(),
        params={"EXAMPLE_REPLAY_CLOSE_ABOVE": 100.5},
        tmp_path=tmp_path,
    )


def test_level_3_live_eligible_contract_helper_covers_canary_example(tmp_path: Path) -> None:
    plugin = resolve_research_strategy_plugin("canary_non_sma")

    assert_live_eligible_contract(
        plugin,
        tmp_path=tmp_path,
        params={
            "CANARY_ORDER_START_INDEX": 0,
            "CANARY_ORDER_SIDE": "BUY",
            "CANARY_ORDER_REASON": "contract_helper_canary",
        },
        pair="KRW-BTC",
        interval="1m",
    )


def test_new_strategy_plugins_do_not_directly_construct_internal_research_strategy_plugin() -> None:
    allowlisted_legacy = {
        "src/bithumb_bot/strategy_plugins/baseline_plugins.py",
        "src/bithumb_bot/strategy_plugins/safe_hold_plugin.py",
    }
    violations: list[str] = []
    root = Path("src/bithumb_bot/strategy_plugins")
    for path in root.glob("*.py"):
        rel = path.as_posix()
        if rel in allowlisted_legacy:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "ResearchStrategyPlugin":
                    violations.append(f"{rel}:{node.lineno}")

    assert violations == []
