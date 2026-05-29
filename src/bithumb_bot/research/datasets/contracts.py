from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from bithumb_bot.research.dataset_snapshot import DatasetQualityReport, DatasetSnapshot
    from bithumb_bot.research.experiment_manifest import DateRange, ExperimentManifest


class UnsupportedDatasetAdapterError(ValueError):
    pass


@dataclass(frozen=True)
class DatasetLoadContext:
    db_path: str | Path | None = None
    manager: Any | None = None


class DatasetAdapter(Protocol):
    source: str
    adapter_name: str
    adapter_version: str
    supported_capabilities: frozenset[str]
    supported_top_of_book_sources: frozenset[str]
    supported_depth_sources: frozenset[str]
    supports_sqlite_streaming_quality_scan: bool

    def load_range(
        self,
        *,
        manifest: ExperimentManifest,
        split_name: str,
        date_range: DateRange,
        context: DatasetLoadContext,
    ) -> DatasetSnapshot:
        ...

    def quality_report(
        self,
        *,
        snapshot: DatasetSnapshot,
        context: DatasetLoadContext,
    ) -> DatasetQualityReport:
        ...

    def provenance(
        self,
        *,
        manifest: ExperimentManifest,
        context: DatasetLoadContext,
    ) -> dict[str, Any]:
        ...
