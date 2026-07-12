"""Direct catalog for the strategies supported by ``market-research``."""

from __future__ import annotations

from collections.abc import Callable

from .strategy_contract import ResearchStrategyDataRequirements, ResearchStrategyPlugin


class ResearchStrategyCatalogError(ValueError):
    pass


_PLUGINS: dict[str, ResearchStrategyPlugin] = {}
_FACTORIES: dict[str, Callable[[], ResearchStrategyPlugin]] = {}


def register_research_strategy(plugin: ResearchStrategyPlugin) -> None:
    key = plugin.name
    if key in _PLUGINS or key in _FACTORIES:
        raise ResearchStrategyCatalogError(f"duplicate_research_strategy:{key}")
    _PLUGINS[key] = plugin


def _register_builtin(name: str, factory: Callable[[], ResearchStrategyPlugin]) -> None:
    key = str(name).strip().lower()
    if not key or key in _PLUGINS or key in _FACTORIES:
        raise ResearchStrategyCatalogError(f"duplicate_research_strategy:{key}")
    _FACTORIES[key] = factory


def resolve_research_strategy(name: str) -> ResearchStrategyPlugin:
    key = str(name or "").strip().lower()
    if key not in _PLUGINS and key in _FACTORIES:
        plugin = _FACTORIES.pop(key)()
        if plugin.name != key:
            raise ResearchStrategyCatalogError(f"research_strategy_name_mismatch:{key}:{plugin.name}")
        _PLUGINS[key] = plugin
    try:
        return _PLUGINS[key]
    except KeyError as exc:
        raise ResearchStrategyCatalogError(f"unsupported_research_strategy:{key}") from exc


def list_research_strategies() -> tuple[ResearchStrategyPlugin, ...]:
    for name in tuple(_FACTORIES):
        resolve_research_strategy(name)
    return tuple(_PLUGINS[name] for name in sorted(_PLUGINS))


def research_strategy_data_requirements(
    strategy_name: str,
    *,
    strategy_spec: object | None = None,
) -> ResearchStrategyDataRequirements:
    return resolve_research_strategy(strategy_name).data_requirements(strategy_spec)


from .strategies.sma_with_filter import build_sma_with_filter_plugin
from .strategies.buy_and_hold_baseline import build_buy_and_hold_baseline_plugin
from .strategies.noop_baseline import build_noop_baseline_plugin
from .strategies.threshold_research_only import build_threshold_research_only_plugin

_register_builtin("sma_with_filter", build_sma_with_filter_plugin)
_register_builtin("buy_and_hold_baseline", build_buy_and_hold_baseline_plugin)
_register_builtin("noop_baseline", build_noop_baseline_plugin)
_register_builtin("threshold_research_only", build_threshold_research_only_plugin)
