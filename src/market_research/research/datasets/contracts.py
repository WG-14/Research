from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from market_research.research.dataset_snapshot import DatasetQualityReport, DatasetSnapshot
    from market_research.research.experiment_manifest import DateRange, ExperimentManifest


class UnsupportedDatasetAdapterError(ValueError):
    pass


@dataclass(frozen=True)
class DatasetLoadContext:
    db_path: str | Path | None = None
    manager: Any | None = None


@dataclass(frozen=True)
class DatasetResolutionContext:
    """Runtime capabilities only; adapters never read process environment."""
    db_path: str | Path | None = None


@dataclass(frozen=True)
class DatasetArtifactRef:
    artifact_manifest_uri: str
    artifact_manifest_hash: str


@dataclass(frozen=True)
class DatasetArtifactHandle:
    reference: DatasetArtifactRef
    manifest: Any


@dataclass(frozen=True)
class VerifiedDatasetArtifact:
    handle: DatasetArtifactHandle
    verification: Any


@dataclass(frozen=True)
class DatasetSliceQuery:
    market: str
    interval: str
    start_ts: int
    end_ts: int
    split_role: str
    snapshot_id: str
    dataset_options: dict[str, object]


class DatasetArtifactAdapter(Protocol):
    source: str
    adapter_name: str
    adapter_version: str
    requires_runtime_db: bool
    requires_artifact_manifest: bool

    def resolve(self, reference: DatasetArtifactRef, context: DatasetResolutionContext) -> DatasetArtifactHandle:
        ...

    def verify(self, handle: DatasetArtifactHandle) -> VerifiedDatasetArtifact:
        ...

    def materialize(self, artifact: VerifiedDatasetArtifact, query: DatasetSliceQuery) -> DatasetSnapshot:
        ...


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


class TopOfBookAdapter(Protocol):
    source: str
    adapter_name: str
    adapter_version: str

    def load_candle_quotes(
        self,
        *,
        manifest: ExperimentManifest,
        candles: tuple[Any, ...],
        context: DatasetLoadContext,
    ) -> tuple[Any | None, ...]:
        ...

    def load_event_quotes(
        self,
        *,
        manifest: ExperimentManifest,
        candles: tuple[Any, ...],
        execution_quote_lookahead_ms: int,
        context: DatasetLoadContext,
    ) -> tuple[Any, ...]:
        ...

    def provenance(
        self,
        *,
        manifest: ExperimentManifest,
        context: DatasetLoadContext,
    ) -> dict[str, Any]:
        ...


class OrderbookDepthAdapter(Protocol):
    source: str
    adapter_name: str
    adapter_version: str

    def load_event_snapshots(
        self,
        *,
        manifest: ExperimentManifest,
        candles: tuple[Any, ...],
        execution_depth_lookahead_ms: int,
        context: DatasetLoadContext,
    ) -> tuple[Any, ...]:
        ...

    def quality_summary(
        self,
        *,
        snapshot: DatasetSnapshot,
        context: DatasetLoadContext,
    ) -> dict[str, Any]:
        ...

    def provenance(
        self,
        *,
        manifest: ExperimentManifest,
        context: DatasetLoadContext,
    ) -> dict[str, Any]:
        ...
