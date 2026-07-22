"""Read-only discovery of immutable datasets and feature authorities.

The explorer intentionally projects evidence, not market observations.  It
discovers published immutable bundles below the configured external data root,
validates their manifest/provenance/catalog contracts, and exposes only stable
identities, hashes, quality summaries, revision history, and point-in-time
metadata.  Technical detail additionally performs the owning dataset
adapter's complete artifact verification without returning a locator or raw
row value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from market_research.paths import ResearchPathManager

from .dataset_snapshot import FrozenSQLiteCandleAdapter
from .datasets.artifact_manifest import (
    ArtifactManifest,
    ArtifactManifestError,
    load_artifact_manifest,
)
from .datasets.contracts import (
    DatasetArtifactRef,
    DatasetResolutionContext,
    DatasetSliceQuery,
)
from .exploration_queries import (
    ExplorationRecord,
    ResearchExplorationQueryError,
    safe_research_projection,
)
from .intervals import interval_to_milliseconds
from .strategy_registry import StrategyRegistry, StrategyRegistryError


DATA_EXPLORATION_SCHEMA_VERSION = 1
_DETAIL_LEVELS = frozenset({"summary", "technical"})
_QUALITY_STATUSES = frozenset({"PASS", "WARN", "FAIL"})
_MAX_DISCOVERED_MANIFESTS = 1_000
_MAX_TECHNICAL_DATASETS = 25


@dataclass(frozen=True, slots=True)
class _PublishedDataset:
    manifest_path: Path
    manifest: ArtifactManifest


def query_dataset_artifacts(
    *,
    manager: ResearchPathManager,
    artifact_id: str | None = None,
    market: str | None = None,
    interval: str | None = None,
    provider_id: str | None = None,
    dataset_id: str | None = None,
    quality_status: str | None = None,
    start_ts: str | int | None = None,
    end_ts: str | int | None = None,
    as_of_ts: str | int | None = None,
    known_at: str | None = None,
    detail_level: str = "summary",
) -> tuple[ExplorationRecord, ...]:
    """Query immutable dataset snapshots without returning observation rows."""

    _require_detail_level(detail_level)
    if quality_status is not None and quality_status not in _QUALITY_STATUSES:
        raise ResearchExplorationQueryError("dataset_quality_filter_invalid")
    query_start = _optional_timestamp(start_ts, "start_ts")
    query_end = _optional_timestamp(end_ts, "end_ts")
    query_as_of = _optional_timestamp(as_of_ts, "as_of_ts")
    if query_start is not None and query_end is not None and query_start > query_end:
        raise ResearchExplorationQueryError("dataset_time_query_invalid")
    known_at_value = _optional_datetime(known_at, "known_at")

    discovered = _discover_published_datasets(manager)
    datasets = tuple(
        published
        for published in discovered
        if known_at_value is None
        or _latest_received_at(published.manifest) <= known_at_value
    )
    selected: list[_PublishedDataset] = []
    for published in datasets:
        manifest = published.manifest
        sources = manifest.source_provenance.sources
        if artifact_id is not None and manifest.artifact_id != artifact_id:
            continue
        if market is not None and manifest.market != market:
            continue
        if interval is not None and manifest.interval != interval:
            continue
        if provider_id is not None and all(
            source.provider_id != provider_id for source in sources
        ):
            continue
        if dataset_id is not None and all(
            source.dataset_id != dataset_id for source in sources
        ):
            continue
        if query_start is not None and manifest.coverage_end_ts < query_start:
            continue
        if query_end is not None and manifest.start_ts > query_end:
            continue
        if query_as_of is not None and not (
            manifest.start_ts <= query_as_of <= manifest.coverage_end_ts
        ):
            continue
        if (
            quality_status is not None
            and _quality_summary(manifest)["status"] != quality_status
        ):
            continue
        selected.append(published)
    if detail_level == "technical" and len(selected) > _MAX_TECHNICAL_DATASETS:
        raise ResearchExplorationQueryError("dataset_technical_query_invalid")
    records = [
        _dataset_record(
            published,
            all_datasets=datasets,
            technical=(detail_level == "technical"),
        )
        for published in selected
    ]
    return tuple(
        sorted(
            records,
            key=lambda item: (
                str(item.summary.get("market") or ""),
                str(item.summary.get("interval") or ""),
                int(item.summary.get("start_ts") or 0),
                item.logical_id,
                item.version,
            ),
        )
    )


def query_dataset_artifact_detail(
    *,
    manager: ResearchPathManager,
    artifact_id: str,
    version: str,
    detail_level: str = "technical",
) -> ExplorationRecord:
    """Return one stable dataset version, verifying bytes for technical detail."""

    matches = [
        item
        for item in query_dataset_artifacts(
            manager=manager,
            artifact_id=artifact_id,
            detail_level=detail_level,
        )
        if item.version == version
    ]
    if len(matches) != 1:
        raise ResearchExplorationQueryError("research_resource_not_found")
    return matches[0]


def query_feature_definitions(
    *,
    registry: StrategyRegistry,
    feature_id: str | None = None,
    strategy: str | None = None,
    input_name: str | None = None,
    detail_level: str = "summary",
) -> tuple[ExplorationRecord, ...]:
    """Query versioned feature authorities from an explicit strategy registry."""

    _require_detail_level(detail_level)
    records: list[ExplorationRecord] = []
    try:
        strategy_names = tuple(sorted(registry.plugins))
        if strategy is not None:
            selected_plugin = registry.resolve(strategy)
            strategy_names = (selected_plugin.name,)
        for strategy_name in strategy_names:
            plugin = registry.resolve(strategy_name)
            for definition in plugin.spec.feature_definitions:
                if feature_id is not None and definition.feature_id != feature_id:
                    continue
                if input_name is not None and input_name not in definition.inputs:
                    continue
                summary = {
                    "strategy": strategy_name,
                    "description": definition.description,
                    "inputs": list(definition.inputs),
                    "value_type": definition.value_type,
                    "unit": definition.unit,
                    "warm_up_bars": definition.warm_up_bars,
                    "current_bar_rule": definition.current_bar_rule,
                    "availability_lag_ms": definition.availability_lag_ms,
                    "missing_policy": definition.missing_policy,
                    "definition_hash": definition.definition_hash,
                }
                technical = None
                if detail_level == "technical":
                    technical = {
                        "definition": definition.as_dict(),
                        "strategy_spec_hash": plugin.spec.spec_hash(),
                        "strategy_plugin_contract_hash": registry.plugin_contract_hashes[
                            strategy_name
                        ],
                        "strategy_registry_hash": registry.content_hash,
                    }
                records.append(
                    ExplorationRecord(
                        kind="feature_definition",
                        logical_id=definition.feature_id,
                        version=definition.version,
                        status="ACTIVE",
                        summary=safe_research_projection(summary),
                        technical=safe_research_projection(technical),
                    )
                )
    except StrategyRegistryError as exc:
        if "unsupported_research_strategy" in str(exc):
            raise ResearchExplorationQueryError(
                "feature_strategy_filter_invalid"
            ) from exc
        raise ResearchExplorationQueryError("feature_registry_invalid") from exc
    return tuple(
        sorted(
            records,
            key=lambda item: (
                str(item.summary.get("strategy") or ""),
                item.logical_id,
                item.version,
            ),
        )
    )


def query_feature_definition_detail(
    *,
    registry: StrategyRegistry,
    feature_id: str,
    version: str,
    detail_level: str = "technical",
) -> ExplorationRecord:
    matches = [
        item
        for item in query_feature_definitions(
            registry=registry,
            feature_id=feature_id,
            detail_level=detail_level,
        )
        if item.version == version
    ]
    if len(matches) != 1:
        raise ResearchExplorationQueryError("research_resource_not_found")
    return matches[0]


def _discover_published_datasets(
    manager: ResearchPathManager,
) -> tuple[_PublishedDataset, ...]:
    root = manager.data_root.resolve()
    if not root.exists():
        return ()
    if not root.is_dir():
        raise ResearchExplorationQueryError("dataset_registry_invalid")
    paths = sorted(
        path
        for path in root.rglob("artifact.manifest.json")
        if not any(part.startswith(".") and ".staging-" in part for part in path.parts)
    )
    if len(paths) > _MAX_DISCOVERED_MANIFESTS:
        raise ResearchExplorationQueryError("dataset_registry_limit_exceeded")
    datasets: list[_PublishedDataset] = []
    identities: set[tuple[str, str]] = set()
    for path in paths:
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ResearchExplorationQueryError(
                "dataset_registry_outside_root"
            ) from exc
        try:
            manifest = load_artifact_manifest(resolved)
        except (ArtifactManifestError, OSError, ValueError) as exc:
            raise ResearchExplorationQueryError("dataset_registry_invalid") from exc
        identity = (manifest.artifact_id, manifest.artifact_manifest_hash)
        if identity in identities:
            raise ResearchExplorationQueryError("dataset_registry_identity_duplicate")
        identities.add(identity)
        datasets.append(_PublishedDataset(resolved, manifest))
    return tuple(datasets)


def _dataset_record(
    published: _PublishedDataset,
    *,
    all_datasets: tuple[_PublishedDataset, ...],
    technical: bool,
) -> ExplorationRecord:
    manifest = published.manifest
    quality = _quality_summary(manifest)
    revisions = _revision_history(manifest, all_datasets)
    providers = tuple(
        manifest.source_provenance.source_catalog.resolve(source.provider_id)
        for source in manifest.source_provenance.sources
    )
    summary = {
        "market": manifest.market,
        "interval": manifest.interval,
        "start_ts": manifest.start_ts,
        "end_ts": manifest.end_ts,
        "coverage_end_ts": manifest.coverage_end_ts,
        "row_count": manifest.row_count,
        "artifact_manifest_hash": manifest.artifact_manifest_hash,
        "artifact_content_hash": manifest.content_hash,
        "source_provenance_hash": (manifest.source_provenance.provenance_manifest_hash),
        "provider_ids": [item.provider_id for item in providers],
        "dataset_ids": [
            source.dataset_id for source in manifest.source_provenance.sources
        ],
        "quality_status": quality["status"],
        "missing_count": quality["missing_count"],
        "expected_row_count": quality["expected_row_count"],
        "revision_count": len(revisions),
        "latest_received_at": _latest_received_at(manifest).isoformat(),
        "point_in_time_policies": [item.point_in_time_policy for item in providers],
    }
    technical_payload: dict[str, Any] | None = None
    if technical:
        verification, verified_grid_quality = _verify_dataset_artifact(published)
        lineage = [stage.as_dict() for stage in manifest.source_provenance.lineage]
        technical_payload = {
            "snapshot": {
                "artifact_id": manifest.artifact_id,
                "artifact_manifest_hash": manifest.artifact_manifest_hash,
                "artifact_content_hash": manifest.content_hash,
                "artifact_schema_hash": manifest.schema_hash,
                "source_provenance_hash": (
                    manifest.source_provenance.provenance_manifest_hash
                ),
                "row_count": manifest.row_count,
                "scope": {
                    "market": manifest.market,
                    "interval": manifest.interval,
                    "start_ts": manifest.start_ts,
                    "end_ts": manifest.end_ts,
                    "coverage_start_ts": manifest.coverage_start_ts,
                    "coverage_end_ts": manifest.coverage_end_ts,
                },
                "canonicalization": {
                    "name": manifest.canonicalization_name,
                    "version": manifest.canonicalization_version,
                },
                "verification": verification,
            },
            "quality": {
                **quality,
                "verified_dense_grid": verified_grid_quality,
            },
            "point_in_time": {
                "observation_time_basis": "candle_event_timestamp",
                "knowledge_time_basis": "externally_recorded_source_received_at",
                "sources": [
                    {
                        "provider_id": source.provider_id,
                        "dataset_id": source.dataset_id,
                        "release_id": source.release_id,
                        "requested_at": source.requested_at,
                        "received_at": source.received_at,
                        "coverage_start_ts": source.coverage_start_ts,
                        "coverage_end_ts": source.coverage_end_ts,
                        "content_hash": source.content_hash,
                    }
                    for source in manifest.source_provenance.sources
                ],
                "provider_policies": [
                    {
                        "provider_id": provider.provider_id,
                        "point_in_time_policy": provider.point_in_time_policy,
                        "expected_delivery_lag_seconds": (
                            provider.expected_delivery_lag_seconds
                        ),
                        "maximum_staleness_seconds": (
                            provider.maximum_staleness_seconds
                        ),
                    }
                    for provider in providers
                ],
            },
            "revision_history": revisions,
            "raw_cleaned_comparison": _raw_cleaned_comparison(lineage),
            "lineage": lineage,
            "source_catalog": {
                "catalog_id": manifest.source_provenance.source_catalog.catalog_id,
                "version": manifest.source_provenance.source_catalog.version,
                "catalog_hash": manifest.source_provenance.source_catalog.catalog_hash,
                "providers": [
                    {
                        "provider_id": provider.provider_id,
                        "display_name": provider.display_name,
                        "data_kinds": list(provider.data_kinds),
                        "frequencies": list(provider.frequencies),
                        "source_kinds": list(provider.source_kinds),
                        "quality_level": provider.quality_level,
                        "revision_policy": provider.revision_policy,
                        "license_id": provider.license_id,
                        "research_use_terms": provider.research_use_terms,
                        "redistribution_allowed": provider.redistribution_allowed,
                        "preparation_boundary": provider.preparation_boundary,
                        "credential_boundary": provider.credential_boundary,
                        "owner": provider.owner,
                    }
                    for provider in providers
                ],
            },
            "feature_input_contract": {
                "available_inputs": [
                    "candles.open",
                    "candles.high",
                    "candles.low",
                    "candles.close",
                    "candles.volume",
                ],
                "feature_values_exposed": False,
                "feature_authority": "feature_definition_registry",
            },
        }
    return ExplorationRecord(
        kind="dataset_artifact",
        logical_id=manifest.artifact_id,
        version=manifest.artifact_manifest_hash,
        status=str(quality["status"]),
        summary=safe_research_projection(summary),
        technical=safe_research_projection(technical_payload),
    )


def _verify_dataset_artifact(
    published: _PublishedDataset,
) -> tuple[dict[str, Any], dict[str, Any]]:
    adapter = FrozenSQLiteCandleAdapter()
    try:
        handle = adapter.resolve(
            DatasetArtifactRef(
                artifact_manifest_uri=str(published.manifest_path),
                artifact_manifest_hash=published.manifest.artifact_manifest_hash,
            ),
            DatasetResolutionContext(),
        )
        verified = adapter.verify(handle)
        manifest = published.manifest
        snapshot = adapter.materialize(
            verified,
            DatasetSliceQuery(
                market=manifest.market,
                interval=manifest.interval,
                start_ts=manifest.start_ts,
                end_ts=manifest.end_ts,
                split_role="data_explorer",
                snapshot_id=(
                    f"data-explorer:{manifest.artifact_manifest_hash.removeprefix('sha256:')}"
                ),
                dataset_options={},
            ),
        )
    except (ArtifactManifestError, OSError, RuntimeError, ValueError) as exc:
        raise ResearchExplorationQueryError(
            "dataset_artifact_verification_failed"
        ) from exc
    interval_ms = interval_to_milliseconds(published.manifest.interval)
    timestamps = tuple(int(candle.ts) for candle in snapshot.candles)
    missing_count = sum(
        max(((current - previous) // interval_ms) - 1, 0)
        for previous, current in zip(timestamps, timestamps[1:])
    )
    off_grid_count = sum(
        (timestamp - published.manifest.start_ts) % interval_ms != 0
        for timestamp in timestamps
    )
    grid_status = "PASS" if missing_count == 0 and off_grid_count == 0 else "FAIL"
    return verified.verification.as_dict(), {
        "status": grid_status,
        "method": "verified_adapter_timestamp_dense_grid_scan",
        "row_count": len(timestamps),
        "missing_count": missing_count,
        "off_grid_count": off_grid_count,
        "start_ts": timestamps[0] if timestamps else None,
        "end_ts": timestamps[-1] if timestamps else None,
    }


def _quality_summary(manifest: ArtifactManifest) -> dict[str, Any]:
    interval_ms = interval_to_milliseconds(manifest.interval)
    expected_count = ((manifest.end_ts - manifest.start_ts) // interval_ms) + 1
    missing_count = max(expected_count - manifest.row_count, 0)
    excess_count = max(manifest.row_count - expected_count, 0)
    source_statuses = [
        source.acquisition_status for source in manifest.source_provenance.sources
    ]
    catalog_levels = [
        manifest.source_provenance.source_catalog.resolve(
            source.provider_id
        ).quality_level
        for source in manifest.source_provenance.sources
    ]
    reasons: list[str] = []
    status = "PASS"
    if any(value != "complete" for value in source_statuses):
        status = "FAIL"
        reasons.append("source_acquisition_not_complete")
    if missing_count or excess_count:
        status = "WARN" if status == "PASS" else status
        reasons.append("declared_row_count_differs_from_dense_interval_grid")
    if any(value != "VERIFIED" for value in catalog_levels):
        status = "WARN" if status == "PASS" else status
        reasons.append("source_catalog_quality_not_verified")
    return {
        "status": status,
        "method": "manifest_dense_continuous_24x7_interval_grid",
        "expected_row_count": expected_count,
        "declared_row_count": manifest.row_count,
        "missing_count": missing_count,
        "excess_count": excess_count,
        "source_acquisition_statuses": source_statuses,
        "source_catalog_quality_levels": catalog_levels,
        "reasons": reasons,
    }


def _revision_history(
    manifest: ArtifactManifest,
    all_datasets: tuple[_PublishedDataset, ...],
) -> list[dict[str, Any]]:
    identities = {
        (source.provider_id, source.dataset_id)
        for source in manifest.source_provenance.sources
    }
    revisions: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for published in all_datasets:
        other = published.manifest
        for source in other.source_provenance.sources:
            if (source.provider_id, source.dataset_id) not in identities:
                continue
            key = (
                source.provider_id,
                source.dataset_id,
                source.release_id,
                other.artifact_manifest_hash,
            )
            revisions[key] = {
                "provider_id": source.provider_id,
                "dataset_id": source.dataset_id,
                "release_id": source.release_id,
                "received_at": source.received_at,
                "source_content_hash": source.content_hash,
                "artifact_id": other.artifact_id,
                "artifact_version": other.artifact_manifest_hash,
                "artifact_content_hash": other.content_hash,
            }
    return sorted(
        revisions.values(),
        key=lambda item: (
            str(item["received_at"]),
            str(item["provider_id"]),
            str(item["dataset_id"]),
            str(item["release_id"]),
        ),
    )


def _raw_cleaned_comparison(lineage: list[dict[str, Any]]) -> dict[str, Any]:
    by_layer = {str(item.get("layer")): item for item in lineage}
    raw = by_layer.get("raw", {})
    cleaned = by_layer.get("cleaned", {})
    standardized = by_layer.get("standardized", {})
    return {
        "comparison_scope": "metadata_and_content_hash_only",
        "raw": raw,
        "cleaned": cleaned,
        "standardized": standardized,
        "raw_to_cleaned_content_changed": raw.get("content_hash")
        != cleaned.get("content_hash"),
        "cleaned_to_standardized_content_changed": cleaned.get("content_hash")
        != standardized.get("content_hash"),
        "raw_values_exposed": False,
    }


def _latest_received_at(manifest: ArtifactManifest) -> datetime:
    return max(
        _parse_datetime(source.received_at)
        for source in manifest.source_provenance.sources
    )


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchExplorationQueryError("dataset_registry_invalid")
    return parsed


def _optional_datetime(value: str | None, label: str) -> datetime | None:
    if value is None:
        return None
    try:
        return _parse_datetime(value)
    except (ValueError, TypeError) as exc:
        raise ResearchExplorationQueryError(f"dataset_{label}_filter_invalid") from exc


def _optional_timestamp(value: str | int | None, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ResearchExplorationQueryError(f"dataset_{label}_filter_invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ResearchExplorationQueryError(f"dataset_{label}_filter_invalid") from exc
    if str(value).strip() != str(parsed):
        raise ResearchExplorationQueryError(f"dataset_{label}_filter_invalid")
    return parsed


def _require_detail_level(value: str) -> None:
    if value not in _DETAIL_LEVELS:
        raise ResearchExplorationQueryError("research_detail_level_invalid")


__all__ = [
    "DATA_EXPLORATION_SCHEMA_VERSION",
    "query_dataset_artifact_detail",
    "query_dataset_artifacts",
    "query_feature_definition_detail",
    "query_feature_definitions",
]
