from __future__ import annotations

from market_research.research.datasets.source_provenance import (
    build_dataset_source_provenance,
)


TEST_SOURCE_PROVENANCE = build_dataset_source_provenance(
    sources=(
        {
            "provider_id": "test-provider",
            "dataset_id": "test-candles",
            "release_id": "test-release-v1",
            "acquired_at": "2026-01-01T00:00:00Z",
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
