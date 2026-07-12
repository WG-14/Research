from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import DatasetAdapter, DatasetArtifactAdapter, OrderbookDepthAdapter, TopOfBookAdapter, UnsupportedDatasetAdapterError


@dataclass
class DatasetAdapterRegistry:
    _adapters: dict[str, DatasetAdapter] = field(default_factory=dict)
    _artifact_adapters: dict[str, DatasetArtifactAdapter] = field(default_factory=dict)
    _top_of_book_adapters: dict[str, TopOfBookAdapter] = field(default_factory=dict)
    _depth_adapters: dict[str, OrderbookDepthAdapter] = field(default_factory=dict)

    def register(self, adapter: DatasetAdapter) -> None:
        source = str(adapter.source or "").strip()
        if not source:
            raise ValueError("dataset adapter source must be non-empty")
        required_methods = ("load_range", "quality_report", "provenance", "verify_snapshot")
        required_attributes = ("requires_runtime_db", "requires_artifact_manifest")
        missing = [name for name in required_methods if not callable(getattr(adapter, name, None))]
        missing.extend(name for name in required_attributes if not hasattr(adapter, name))
        if missing:
            raise ValueError(f"dataset_adapter_missing_capability:{source}:{','.join(missing)}")
        self._adapters[source] = adapter
        if bool(getattr(adapter, "requires_artifact_manifest")):
            self.register_artifact(adapter)  # type: ignore[arg-type]

    def register_artifact(self, adapter: DatasetArtifactAdapter) -> None:
        source = str(adapter.source or "").strip()
        required = ("resolve", "verify", "materialize", "requires_artifact_manifest", "requires_runtime_db")
        missing = [name for name in required if not hasattr(adapter, name)]
        if not source or missing or not bool(getattr(adapter, "requires_artifact_manifest", False)):
            suffix = ",".join(missing) if missing else "requires_artifact_manifest"
            raise ValueError(f"dataset_artifact_adapter_incomplete:{source}:{suffix}")
        self._artifact_adapters[source] = adapter

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

    def resolve_artifact(self, source: str) -> DatasetArtifactAdapter:
        adapter = self._artifact_adapters.get(str(source or "").strip())
        if adapter is None:
            raise UnsupportedDatasetAdapterError(f"dataset_artifact_adapter_required:{source}")
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
