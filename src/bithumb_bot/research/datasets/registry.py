from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import DatasetAdapter, UnsupportedDatasetAdapterError


@dataclass
class DatasetAdapterRegistry:
    _adapters: dict[str, DatasetAdapter] = field(default_factory=dict)

    def register(self, adapter: DatasetAdapter) -> None:
        source = str(adapter.source or "").strip()
        if not source:
            raise ValueError("dataset adapter source must be non-empty")
        self._adapters[source] = adapter

    def resolve(self, source: str) -> DatasetAdapter:
        normalized = str(source or "").strip()
        adapter = self._adapters.get(normalized)
        if adapter is None:
            raise UnsupportedDatasetAdapterError(f"unsupported_dataset_adapter:{normalized}")
        return adapter

    def resolve_top_of_book(self, source: str) -> DatasetAdapter:
        normalized = str(source or "").strip()
        for adapter in self._adapters.values():
            if normalized in getattr(adapter, "supported_top_of_book_sources", frozenset()):
                return adapter
        raise UnsupportedDatasetAdapterError(f"unsupported_top_of_book_adapter:{normalized}")

    def resolve_depth(self, source: str) -> DatasetAdapter:
        normalized = str(source or "").strip()
        for adapter in self._adapters.values():
            if normalized in getattr(adapter, "supported_depth_sources", frozenset()):
                return adapter
        raise UnsupportedDatasetAdapterError(f"unsupported_depth_adapter:{normalized}")

    def resolve_capability(self, source: str, capability: str) -> DatasetAdapter:
        adapter = self.resolve(source)
        normalized_capability = str(capability or "").strip()
        if normalized_capability not in getattr(adapter, "supported_capabilities", frozenset()):
            raise UnsupportedDatasetAdapterError(
                f"unsupported_dataset_capability:{source}:{normalized_capability}"
            )
        return adapter

    def sources(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))


_DEFAULT_REGISTRY = DatasetAdapterRegistry()


def default_dataset_adapter_registry() -> DatasetAdapterRegistry:
    return _DEFAULT_REGISTRY
