from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from market_research.research.experiment_manifest import (
    TopOfBookDatasetSpec,
    _simulation_seed_scope_projection,
)
from market_research.research.market_calendar_contract import (
    parse_market_calendar_authority,
)
from market_research.research.universe_contract import parse_point_in_time_universe
from market_research.research_composition import load_builtin_manifest
from tests.research_sma_success_fixture import create_success_fixture


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _universe_payload(source_uri: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "universe_id": "univ_seed_scope_0001",
        "universe_version_id": "univv_seed_scope_0001_v1",
        "version": 1,
        "name": "Seed scope fixture universe",
        "source_uri": source_uri,
        "source_content_hash": _hash("a"),
        "source_schema_hash": _hash("b"),
        "prepared_at": "2024-01-02T00:00:00+00:00",
        "observed_at": "2024-01-02T00:01:00+00:00",
        "memberships": [
            {
                "schema_version": 1,
                "membership_id": "um_seed_scope_0001",
                "membership_version_id": "umv_seed_scope_0001_v1",
                "version": 1,
                "universe_id": "univ_seed_scope_0001",
                "instrument_id": "inst_seed_scope_0001",
                "valid_from": "2020-01-01",
                "valid_to": None,
                "status": "active",
                "published_at": "2024-01-01T00:00:00+00:00",
                "observed_at": "2024-01-01T00:01:00+00:00",
                "source_content_hash": _hash("c"),
                "attributes": [],
                "supersedes_version_id": None,
                "correction_reason": None,
            }
        ],
    }


def _calendar_payload(source_uri: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "calendar_id": "cal_seed_scope_0001",
        "calendar_version_id": "calv_seed_scope_0001_v1",
        "version": 1,
        "market_mode": "continuous_24x7",
        "timezone_name": "UTC",
        "tzdb_version": "2026a",
        "dst_transition_policy": (
            "iana_tzdb_reject_ambiguous_or_nonexistent_local_time"
        ),
        "valid_from": "2020-01-01",
        "valid_to": "2030-12-31",
        "source_uri": source_uri,
        "source_content_hash": _hash("d"),
        "source_schema_hash": _hash("e"),
        "published_at": "2020-01-01T00:00:00+00:00",
        "observed_at": "2020-01-01T00:01:00+00:00",
        "weekly_sessions": [],
        "exceptions": [],
    }


def test_seed_scope_is_stable_when_an_immutable_bundle_is_relocated(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first-mount"
    second_root = tmp_path / "second-mount"
    first_root.mkdir()
    second_root.mkdir()
    _, first_path = create_success_fixture(first_root)
    _, second_path = create_success_fixture(second_root)

    first = load_builtin_manifest(str(first_path))
    second = load_builtin_manifest(str(second_path))
    first_ref = first.dataset.artifact_ref
    second_ref = second.dataset.artifact_ref
    assert first_ref is not None
    assert second_ref is not None
    assert first_ref.artifact_manifest_uri != second_ref.artifact_manifest_uri
    assert first_ref.artifact_manifest_hash != second_ref.artifact_manifest_hash

    first_seed_payload = first.simulation_seed_scope_payload()
    second_seed_payload = second.simulation_seed_scope_payload()
    first_identity = first_seed_payload["dataset"]["artifact_manifest_identity"]
    second_identity = second_seed_payload["dataset"]["artifact_manifest_identity"]
    assert first_identity == second_identity
    assert first_identity["artifact_content_hash"].startswith("sha256:")
    assert first_identity["artifact_schema_hash"].startswith("sha256:")
    assert first_identity["artifact_identity_hash"].startswith("sha256:")

    # Full evidence identity remains location-bound; only random seed derivation
    # is projected onto the immutable artifact's logical identity.
    assert first.manifest_hash() != second.manifest_hash()
    assert first_seed_payload == second_seed_payload
    assert first.simulation_seed_scope_hash() == second.simulation_seed_scope_hash()


def test_seed_scope_projects_dataset_and_authority_locations_only(
    tmp_path: Path,
) -> None:
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    _, manifest_path = create_success_fixture(fixture_root)
    base = load_builtin_manifest(str(manifest_path))

    first_universe_payload = _universe_payload("/first-mount/universe-v1.json")
    second_universe_payload = deepcopy(first_universe_payload)
    second_universe_payload["source_uri"] = "/second-mount/universe-v1.json"
    first_calendar_payload = _calendar_payload("/first-mount/calendar-2026.json")
    second_calendar_payload = deepcopy(first_calendar_payload)
    second_calendar_payload["source_uri"] = "/second-mount/calendar-2026.json"

    first_dataset = replace(
        base.dataset,
        artifact_ref=None,
        source_uri="/first-mount/candles.sqlite",
        source_content_hash=_hash("3"),
        source_schema_hash=_hash("4"),
        locator={
            "type": "content_addressed_local",
            "path": "/first-mount/candles.sqlite",
            "artifact_content_hash": _hash("3"),
        },
        top_of_book=TopOfBookDatasetSpec(
            required=True,
            source_uri="/first-mount/top-of-book.sqlite",
            source_content_hash=_hash("5"),
            source_schema_hash=_hash("6"),
            locator={
                "type": "content_addressed_local",
                "path": "/first-mount/top-of-book.sqlite",
                "artifact_content_hash": _hash("5"),
            },
        ),
        options={"cache_path": "/first-mount/cache", "semantic_mode": "strict"},
    )
    first_top_of_book = first_dataset.top_of_book
    assert first_top_of_book is not None
    second_dataset = replace(
        first_dataset,
        source_uri="/second-mount/candles.sqlite",
        locator={
            "type": "content_addressed_local",
            "path": "/second-mount/candles.sqlite",
            "artifact_content_hash": _hash("3"),
        },
        top_of_book=replace(
            first_top_of_book,
            source_uri="/second-mount/top-of-book.sqlite",
            locator={
                "type": "content_addressed_local",
                "path": "/second-mount/top-of-book.sqlite",
                "artifact_content_hash": _hash("5"),
            },
        ),
        options={"cache_path": "/second-mount/cache", "semantic_mode": "strict"},
    )
    first_universe = parse_point_in_time_universe(first_universe_payload)
    second_universe = parse_point_in_time_universe(second_universe_payload)
    first_calendar = parse_market_calendar_authority(first_calendar_payload)
    second_calendar = parse_market_calendar_authority(second_calendar_payload)
    first = replace(
        base,
        dataset=first_dataset,
        universe=first_universe,
        market_calendar=first_calendar,
    )
    second = replace(
        base,
        dataset=second_dataset,
        universe=second_universe,
        market_calendar=second_calendar,
    )

    assert first.manifest_hash() != second.manifest_hash()
    assert first_universe.contract_hash() != second_universe.contract_hash()
    assert first_calendar.contract_hash() != second_calendar.contract_hash()
    assert (
        first.simulation_seed_scope_payload() == second.simulation_seed_scope_payload()
    )
    assert first.simulation_seed_scope_hash() == second.simulation_seed_scope_hash()

    changed_dataset = replace(second_dataset, source_content_hash=_hash("7"))
    changed = replace(second, dataset=changed_dataset)
    assert changed.simulation_seed_scope_hash() != second.simulation_seed_scope_hash()


def test_seed_scope_projection_retains_logical_authority_and_hashes() -> None:
    projected = _simulation_seed_scope_projection(
        {
            "source_uri": "/external/calendar.json",
            "backup_path": "/external/calendar.backup.json",
            "calendar_id": "cal_reviewed_0001",
            "calendar_version_id": "calv_reviewed_0001_v1",
            "source_content_hash": _hash("8"),
            "source_schema_hash": _hash("9"),
            "locator": {
                "type": "content_addressed_local",
                "path": "/external/calendar.json",
                "artifact_content_hash": _hash("8"),
            },
        }
    )

    assert projected == {
        "calendar_id": "cal_reviewed_0001",
        "calendar_version_id": "calv_reviewed_0001_v1",
        "locator": {
            "artifact_content_hash": _hash("8"),
            "type": "content_addressed_local",
        },
        "source_content_hash": _hash("8"),
        "source_schema_hash": _hash("9"),
    }
