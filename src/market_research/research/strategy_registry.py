"""Immutable, content-hashed strategy registry."""
from __future__ import annotations

from dataclasses import dataclass
import importlib
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .hashing import sha256_prefixed
from .strategy_contract import ResearchStrategyPlugin


class StrategyRegistryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StrategyRegistry:
    schema_version: int
    plugins: Mapping[str, ResearchStrategyPlugin]
    plugin_contract_hashes: Mapping[str, str]
    content_hash: str

    @classmethod
    def build(cls, plugins: Iterable[ResearchStrategyPlugin]) -> "StrategyRegistry":
        values: dict[str, ResearchStrategyPlugin] = {}
        for plugin in plugins:
            if plugin.name in values:
                raise StrategyRegistryError(f"duplicate_research_strategy:{plugin.name}")
            values[plugin.name] = plugin
        payload = {"schema_version": 1, "plugins": {name: values[name].contract_hash() for name in sorted(values)}}
        hashes = {name: values[name].contract_hash() for name in sorted(values)}
        return cls(1, MappingProxyType(values), MappingProxyType(hashes), sha256_prefixed(payload))

    def resolve(self, name: str) -> ResearchStrategyPlugin:
        key = str(name or "").strip().lower()
        try:
            plugin = self.plugins[key]
        except KeyError as exc:
            raise StrategyRegistryError(f"unsupported_research_strategy:{key}") from exc
        if plugin.contract_hash() != self.plugin_contract_hashes[key]:
            raise StrategyRegistryError(f"stale_research_strategy_contract:{key}")
        return plugin

    def descriptor(self) -> dict[str, object]:
        """Return the deterministic, process-safe reconstruction contract."""
        plugins: list[dict[str, object]] = []
        for name in sorted(self.plugins):
            plugin = self.resolve(name)
            module = plugin.reconstruction_module
            qualname = plugin.reconstruction_qualname
            if not module or not qualname or "<locals>" in qualname:
                raise StrategyRegistryError(f"strategy_plugin_not_reconstructable:{name}")
            plugins.append({
                "schema_version": 1,
                "strategy_name": plugin.name,
                "strategy_version": plugin.version,
                "factory_module": module,
                "factory_qualname": qualname,
                "plugin_contract_hash": self.plugin_contract_hashes[name],
                "strategy_spec_hash": plugin.spec.spec_hash(),
            })
        material = {
            "schema_version": 1,
            "registry_content_hash": self.content_hash,
            "plugins": plugins,
        }
        return {**material, "descriptor_hash": sha256_prefixed(material)}


_REGISTRY_DESCRIPTOR_FIELDS = frozenset({
    "schema_version", "registry_content_hash", "plugins", "descriptor_hash",
})
_PLUGIN_DESCRIPTOR_FIELDS = frozenset({
    "schema_version", "strategy_name", "strategy_version", "factory_module", "factory_qualname",
    "plugin_contract_hash", "strategy_spec_hash",
})


def reconstruct_strategy_registry(descriptor: Mapping[str, Any]) -> StrategyRegistry:
    """Rebuild and verify a registry before a worker may execute candidates."""
    if not isinstance(descriptor, Mapping) or set(descriptor) != _REGISTRY_DESCRIPTOR_FIELDS:
        raise StrategyRegistryError("strategy_registry_descriptor_invalid")
    material = {key: descriptor[key] for key in ("schema_version", "registry_content_hash", "plugins")}
    if descriptor.get("schema_version") != 1 or descriptor.get("descriptor_hash") != sha256_prefixed(material):
        raise StrategyRegistryError("strategy_registry_descriptor_hash_mismatch")
    raw_plugins = descriptor.get("plugins")
    if not isinstance(raw_plugins, (list, tuple)):
        raise StrategyRegistryError("strategy_registry_descriptor_plugins_invalid")
    names = [item.get("strategy_name") for item in raw_plugins if isinstance(item, Mapping)]
    if len(names) != len(raw_plugins) or names != sorted(names) or len(set(names)) != len(names):
        raise StrategyRegistryError("strategy_registry_descriptor_order_invalid")
    plugins: list[ResearchStrategyPlugin] = []
    for item in raw_plugins:
        if set(item) != _PLUGIN_DESCRIPTOR_FIELDS or item.get("schema_version") != 1:
            raise StrategyRegistryError("strategy_plugin_descriptor_invalid")
        name = str(item["strategy_name"])
        try:
            module = importlib.import_module(str(item["factory_module"]))
            factory: Any = module
            for component in str(item["factory_qualname"]).split("."):
                factory = getattr(factory, component)
            plugin = factory()
        except (ImportError, AttributeError, TypeError) as exc:
            raise StrategyRegistryError(f"strategy_plugin_reconstruction_failed:{name}") from exc
        if not isinstance(plugin, ResearchStrategyPlugin) or plugin.name != name or plugin.version != item["strategy_version"]:
            raise StrategyRegistryError(f"strategy_plugin_reconstruction_identity_mismatch:{name}")
        if plugin.spec.spec_hash() != item["strategy_spec_hash"]:
            raise StrategyRegistryError(f"strategy_plugin_spec_hash_mismatch:{name}")
        if plugin.contract_hash() != item["plugin_contract_hash"]:
            raise StrategyRegistryError(f"strategy_plugin_contract_hash_mismatch:{name}")
        plugins.append(plugin)
    registry = StrategyRegistry.build(plugins)
    if registry.content_hash != descriptor.get("registry_content_hash"):
        raise StrategyRegistryError("strategy_registry_content_hash_mismatch")
    return registry
