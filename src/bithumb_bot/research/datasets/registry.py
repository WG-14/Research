from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import DatasetAdapter, OrderbookDepthAdapter, TopOfBookAdapter, UnsupportedDatasetAdapterError


@dataclass
class DatasetAdapterRegistry:
    _adapters: dict[str, DatasetAdapter] = field(default_factory=dict)
    _top_of_book_adapters: dict[str, TopOfBookAdapter] = field(default_factory=dict)
    _depth_adapters: dict[str, OrderbookDepthAdapter] = field(default_factory=dict)

    def register(self, adapter: DatasetAdapter) -> None:
        source = str(adapter.source or "").strip()
        if not source:
            raise ValueError("dataset adapter source must be non-empty")
        self._adapters[source] = adapter

    def register_top_of_book(self, adapter: TopOfBookAdapter) -> None:
        source = str(adapter.source or "").strip()
        if not source:
            raise ValueError("top-of-book adapter source must be non-empty")
        self._top_of_book_adapters[source] = adapter

    def register_depth(self, adapter: OrderbookDepthAdapter) -> None:
        source = str(adapter.source or "").strip()
        if not source:
            raise ValueError("orderbook depth adapter source must be non-empty")
        self._depth_adapters[source] = adapter

    def resolve(self, source: str) -> DatasetAdapter:
        normalized = str(source or "").strip()
        adapter = self._adapters.get(normalized)
        if adapter is None:
            raise UnsupportedDatasetAdapterError(f"unsupported_dataset_adapter:{normalized}")
        return adapter

    def resolve_top_of_book(self, source: str) -> TopOfBookAdapter:
        normalized = str(source or "").strip()
        adapter = self._top_of_book_adapters.get(normalized)
        if adapter is not None:
            return adapter
        raise UnsupportedDatasetAdapterError(f"unsupported_top_of_book_adapter:{normalized}")

    def resolve_depth(self, source: str) -> OrderbookDepthAdapter:
        normalized = str(source or "").strip()
        adapter = self._depth_adapters.get(normalized)
        if adapter is not None:
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

    def top_of_book_sources(self) -> tuple[str, ...]:
        return tuple(sorted(self._top_of_book_adapters))

    def depth_sources(self) -> tuple[str, ...]:
        return tuple(sorted(self._depth_adapters))


_DEFAULT_REGISTRY = DatasetAdapterRegistry()


def default_dataset_adapter_registry() -> DatasetAdapterRegistry:
    return _DEFAULT_REGISTRY
