from __future__ import annotations

from market_research.research.datasets.source_catalog import (
    SourceCatalog,
    build_source_catalog,
)
from market_research.research.datasets.source_provenance import (
    build_dataset_source_provenance,
)


def build_test_source_catalog(
    *,
    provider_id: str = "test-provider",
    source_kinds: tuple[str, ...] = ("file_export",),
) -> SourceCatalog:
    return build_source_catalog(
        catalog_id="test-research-source-catalog",
        version="test-v1",
        approved_at="2025-12-31T00:00:00Z",
        approved_by="test-data-steward",
        entries=(
            {
                "provider_id": provider_id,
                "display_name": "Externally prepared test fixture",
                "data_kinds": ["ohlcv"],
                "frequencies": ["1m"],
                "source_kinds": sorted(source_kinds),
                "point_in_time_policy": ("event_available_received_processed_times"),
                "revision_policy": "append_new_release_preserve_prior",
                "license_id": "test-research-license-v1",
                "research_use_terms": "offline reproducible research only",
                "redistribution_allowed": False,
                "quality_level": "VERIFIED",
                "preparation_boundary": (
                    "externally_prepared_offline_immutable_input_only"
                ),
                "credential_boundary": (
                    "credentials_external_to_research_distribution"
                ),
                "owner": "test-data-steward",
                "expected_delivery_lag_seconds": 1.0,
                "maximum_staleness_seconds": 3600.0,
            },
        ),
    )


TEST_SOURCE_CATALOG = build_test_source_catalog()

TEST_SOURCE_PROVENANCE = build_dataset_source_provenance(
    source_catalog=TEST_SOURCE_CATALOG,
    sources=(
        {
            "provider_id": "test-provider",
            "dataset_id": "test-candles",
            "release_id": "test-release-v1",
            "source_kind": "file_export",
            "request_parameters": {
                "interval": "1m",
                "market": "KRW-BTC",
            },
            "requested_at": "2026-01-01T00:00:00Z",
            "received_at": "2026-01-01T00:00:01Z",
            "response_version": "test-export-v1",
            "acquisition_code_version": "external-fixture-v1",
            "retry_count": 0,
            "acquisition_status": "complete",
            "error_code": "",
            "coverage_start_ts": -(2**63),
            "coverage_end_ts": 2**63 - 1,
            "content_hash": "sha256:" + "1" * 64,
        },
    ),
    source_priority=("test-provider",),
    lineage=(
        {
            "layer": "raw",
            "artifact_id": "test-raw-v1",
            "content_hash": "sha256:" + "2" * 64,
            "schema_version": 1,
            "transformation_id": "external-acquisition-v1",
        },
        {
            "layer": "cleaned",
            "artifact_id": "test-cleaned-v1",
            "content_hash": "sha256:" + "3" * 64,
            "schema_version": 1,
            "transformation_id": "test-cleaner-v1",
        },
        {
            "layer": "standardized",
            "artifact_id": "test-standardized-v1",
            "content_hash": "sha256:" + "4" * 64,
            "schema_version": 1,
            "transformation_id": "test-standardizer-v1",
        },
    ),
)
