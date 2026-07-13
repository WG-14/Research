"""Immutable, content-hashed strategy registry."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

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
