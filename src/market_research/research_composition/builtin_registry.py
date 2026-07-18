"""Only production composition root importing concrete built-in plugins."""

from __future__ import annotations
from dataclasses import dataclass, replace
from functools import lru_cache
from types import MappingProxyType
from typing import Any

from market_research.builtin_strategies import (
    BuiltinStrategyLoadFailure,
    discover_builtin_strategy_modules,
)
from market_research.research.backtest_types import BacktestRunContext
from market_research.research.strategy_contract import (
    CompiledStrategyContract,
    ResearchStrategyPlugin,
)
from market_research.research.strategy_registry import StrategyRegistry
from market_research.research.strategy_manifest import (
    StrategyManifest,
    load_builtin_strategy_manifest,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.strategy_compiler import StrategyCompiler
from market_research.research.experiment_manifest import (
    ExperimentManifest,
    load_manifest_with_registry,
    parse_manifest_with_registry,
)


@dataclass(frozen=True, slots=True)
class BuiltinStrategyCatalog:
    registry: StrategyRegistry
    manifests: dict[str, StrategyManifest]
    failures: tuple[BuiltinStrategyLoadFailure, ...]
    content_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifests", MappingProxyType(dict(self.manifests)))

    def status_payload(self) -> dict[str, object]:
        available = {
            name: {
                "status": "AVAILABLE",
                "strategy_version": self.manifests[name].strategy_version,
                "manifest_hash": self.manifests[name].content_hash(),
            }
            for name in sorted(self.manifests)
        }
        failures = [
            {
                "module_name": failure.module_name,
                "status": "LOAD_FAILED",
                "reason_code": failure.reason_code,
            }
            for failure in self.failures
        ]
        return {
            "schema_version": 1,
            "catalog_hash": self.content_hash,
            "available": available,
            "failures": failures,
        }


@lru_cache(maxsize=1)
def builtin_strategy_catalog() -> BuiltinStrategyCatalog:
    plugins: list[ResearchStrategyPlugin] = []
    manifests: dict[str, StrategyManifest] = {}
    report = discover_builtin_strategy_modules()
    failures = list(report.failures)
    for factory_index, factory in enumerate(report.factories):
        module_name = (
            report.factory_module_names[factory_index]
            if len(report.factory_module_names) == len(report.factories)
            else str(getattr(factory, "__module__", "<unknown>"))
        )
        try:
            plugin = factory()
            if not isinstance(plugin, ResearchStrategyPlugin):
                raise TypeError("factory_returned_invalid_plugin")
            manifest = load_builtin_strategy_manifest(module_name)
            if plugin.package_manifest_hash is None:
                plugin = replace(
                    plugin,
                    package_manifest_hash=manifest.content_hash(),
                )
            manifest.validate_plugin(plugin)
            if not manifest.selectable:
                failures.append(
                    BuiltinStrategyLoadFailure(
                        module_name,
                        f"strategy_not_selectable:{manifest.status}",
                    )
                )
                continue
            plugins.append(plugin)
            manifests[plugin.name] = manifest
        except Exception as exc:
            failures.append(
                BuiltinStrategyLoadFailure(
                    module_name,
                    f"package_validation_failed:{type(exc).__name__}",
                )
            )
    registry = StrategyRegistry.build(plugins)
    material = {
        "schema_version": 1,
        "registry_hash": registry.content_hash,
        "manifests": {
            name: manifests[name].content_hash() for name in sorted(manifests)
        },
        "failures": [
            {
                "module_name": item.module_name,
                "reason_code": item.reason_code,
            }
            for item in failures
        ],
    }
    return BuiltinStrategyCatalog(
        registry=registry,
        manifests=manifests,
        failures=tuple(failures),
        content_hash=sha256_prefixed(material),
    )


def builtin_strategy_registry() -> StrategyRegistry:
    return builtin_strategy_catalog().registry


def _clear_builtin_strategy_registry_cache() -> None:
    """Preserve the discovery cache reset hook used by extension tooling."""
    builtin_strategy_catalog.cache_clear()


setattr(
    builtin_strategy_registry,
    "cache_clear",
    _clear_builtin_strategy_registry_cache,
)


def compile_builtin_strategy(
    *,
    strategy_name: str,
    raw_parameters: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    context: BacktestRunContext | None = None,
) -> CompiledStrategyContract:
    """Composition-owned convenience for production built-in selection."""
    registry = builtin_strategy_registry()
    return StrategyCompiler(registry).compile(
        strategy_name=strategy_name,
        raw_parameters=raw_parameters,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        context=context,
    )


def load_builtin_manifest(path: str) -> ExperimentManifest:
    """Composition-owned convenience loader for the built-in registry."""
    return load_manifest_with_registry(path, registry=builtin_strategy_registry())


def parse_builtin_manifest(payload: dict[str, object]) -> ExperimentManifest:
    """Composition-owned convenience parser for the built-in registry."""
    return parse_manifest_with_registry(payload, registry=builtin_strategy_registry())


def resolve_builtin_strategy(name: str) -> ResearchStrategyPlugin:
    return builtin_strategy_registry().resolve(name)


def list_builtin_strategies() -> tuple[ResearchStrategyPlugin, ...]:
    registry = builtin_strategy_registry()
    return tuple(registry.plugins[name] for name in sorted(registry.plugins))
