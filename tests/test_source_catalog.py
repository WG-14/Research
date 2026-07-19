from __future__ import annotations

from copy import deepcopy

import pytest

from market_research.research.datasets.source_catalog import (
    SourceCatalogError,
    build_source_catalog,
    parse_source_catalog,
    source_catalog_hash,
)


def _entry(provider_id: str = "prepared-provider") -> dict[str, object]:
    return {
        "provider_id": provider_id,
        "display_name": "Reviewed offline provider export",
        "data_kinds": ["ohlcv"],
        "frequencies": ["1d", "1m"],
        "source_kinds": ["file_export", "vendor_archive"],
        "point_in_time_policy": "event_available_received_processed_times",
        "revision_policy": "append_new_release_preserve_prior",
        "license_id": "license-research-001",
        "research_use_terms": "offline reproducible research only",
        "redistribution_allowed": False,
        "quality_level": "VERIFIED",
        "preparation_boundary": "externally_prepared_offline_immutable_input_only",
        "credential_boundary": "credentials_external_to_research_distribution",
        "owner": "research-data-steward",
        "expected_delivery_lag_seconds": 60.0,
        "maximum_staleness_seconds": 3600.0,
    }


def _catalog():
    return build_source_catalog(
        catalog_id="research-source-catalog",
        version="1",
        approved_at="2026-01-01T00:00:00+00:00",
        approved_by="data-governance-reviewer",
        entries=(_entry(),),
    )


def test_catalog_is_hash_bound_and_resolves_reviewed_provider() -> None:
    catalog = _catalog()

    assert catalog.catalog_hash == source_catalog_hash(catalog.identity_payload())
    entry = catalog.resolve("prepared-provider")
    assert entry.quality_level == "VERIFIED"
    assert entry.revision_policy == "append_new_release_preserve_prior"
    assert entry.preparation_boundary.endswith("immutable_input_only")


def test_tamper_and_unknown_secret_field_fail_closed() -> None:
    payload = _catalog().as_dict()
    payload["entries"][0]["quality_level"] = "PROVISIONAL"
    with pytest.raises(SourceCatalogError, match="hash_mismatch"):
        parse_source_catalog(payload)

    payload = deepcopy(_catalog().as_dict())
    payload["entries"][0]["api_token"] = "must-not-enter-research"
    payload["catalog_hash"] = source_catalog_hash(payload)
    with pytest.raises(SourceCatalogError, match="unknown_field"):
        parse_source_catalog(payload)


def test_catalog_entries_are_deterministic_and_duplicate_free() -> None:
    catalog = build_source_catalog(
        catalog_id="research-source-catalog",
        version="1",
        approved_at="2026-01-01T00:00:00+00:00",
        approved_by="data-governance-reviewer",
        entries=(_entry("z-provider"), _entry("a-provider")),
    )
    assert [item.provider_id for item in catalog.entries] == [
        "a-provider",
        "z-provider",
    ]

    with pytest.raises(SourceCatalogError, match="provider_duplicate"):
        build_source_catalog(
            catalog_id="research-source-catalog",
            version="1",
            approved_at="2026-01-01T00:00:00+00:00",
            approved_by="data-governance-reviewer",
            entries=(_entry(), _entry()),
        )


def test_non_external_preparation_or_credentials_boundary_is_rejected() -> None:
    entry = _entry()
    entry["preparation_boundary"] = "runtime_network_collection"
    with pytest.raises(SourceCatalogError, match="preparation_boundary_invalid"):
        build_source_catalog(
            catalog_id="research-source-catalog",
            version="1",
            approved_at="2026-01-01T00:00:00+00:00",
            approved_by="data-governance-reviewer",
            entries=(entry,),
        )
