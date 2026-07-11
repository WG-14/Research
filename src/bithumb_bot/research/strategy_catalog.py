"""Registry used exclusively by research commands.

The legacy ``research.strategy_registry`` remains for the integrated
operational CLI.  Nothing in the research execution path imports it.
"""

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


def _legacy_factory(name: str) -> Callable[[], ResearchStrategyPlugin]:
    """Temporary isolated loader for non-SMA migrated implementations.

    It is never reached by a SMA research command.  The compatibility code is
    intentionally contained here so the research command's selected strategy
    has a narrow import boundary while follow-up extraction proceeds.
    """
    def factory() -> ResearchStrategyPlugin:
        from .strategies.legacy_compat import build_legacy_research_plugin
        return build_legacy_research_plugin(name)
    return factory


from .strategies.sma_with_filter import build_sma_with_filter_plugin

_register_builtin("sma_with_filter", build_sma_with_filter_plugin)
for _name in (
    "daily_participation_sma",
    "noop_baseline",
    "buy_and_hold_baseline",
    "canary_non_sma",
    "replay_threshold",
    "threshold_research_only",
    "channel_breakout_with_regime_filter",
):
    _register_builtin(_name, _legacy_factory(_name))
