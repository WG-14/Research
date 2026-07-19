from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.data_exploration_queries import (
    query_dataset_artifact_detail,
    query_dataset_artifacts,
    query_feature_definition_detail,
    query_feature_definitions,
)
from market_research.research.dataset_freeze import freeze_sqlite_candles_dataset
from market_research.research.datasets.source_provenance import (
    DatasetSourceProvenance,
    build_dataset_source_provenance,
)
from market_research.research.exploration_queries import (
    ResearchExplorationQueryError,
)
from market_research.research_composition import builtin_strategy_registry
from market_research.settings import ResearchSettings
from tests.dataset_provenance_fixture import (
    TEST_SOURCE_PROVENANCE,
)


def _manager(tmp_path: Path) -> ResearchPathManager:
    return ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "external-data",
            artifact_root=tmp_path / "artifacts",
            report_root=tmp_path / "reports",
            cache_root=tmp_path / "cache",
            db_path=None,
            max_workers=1,
            random_seed=0,
        ),
        project_root=Path.cwd(),
    )


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "prepared.sqlite"
    path.unlink(missing_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE candles (pair TEXT, interval TEXT, ts INTEGER, "
            "open REAL, high REAL, low REAL, close REAL, volume REAL)"
        )
        db.executemany(
            "INSERT INTO candles VALUES ('KRW-BTC','1m',?,?,?,?,?,?)",
            (
                (0, 1.0, 1.0, 1.0, 1.0, 10.0),
                (60_000, 2.0, 2.0, 2.0, 2.0, 20.0),
            ),
        )
    return path


def _freeze(
    tmp_path: Path,
    manager: ResearchPathManager,
    *,
    provenance: DatasetSourceProvenance = TEST_SOURCE_PROVENANCE,
) -> dict[str, object]:
    return freeze_sqlite_candles_dataset(
        source_provenance=provenance,
        source_db=_source(tmp_path),
        market="KRW-BTC",
        interval="1m",
        start_ts=0,
        end_ts=60_000,
        out_dir=manager.data_root,
    )


def test_dataset_query_is_time_filterable_and_path_free(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    frozen = _freeze(tmp_path, manager)
    staging = manager.data_root / "candles" / "KRW-BTC" / "1m" / ".x.staging-test"
    staging.mkdir(parents=True)
    (staging / "artifact.manifest.json").write_text(
        "not-yet-published", encoding="utf-8"
    )

    records = query_dataset_artifacts(
        manager=manager,
        market="KRW-BTC",
        interval="1m",
        provider_id="test-provider",
        dataset_id="test-candles",
        quality_status="PASS",
        start_ts="0",
        end_ts="60000",
        as_of_ts="30000",
        known_at="2026-01-01T00:00:01Z",
    )

    assert len(records) == 1
    record = records[0]
    assert record.logical_id == frozen["artifact_id"]
    assert record.version == frozen["artifact_manifest_hash"]
    assert record.summary["expected_row_count"] == 2
    assert record.summary["missing_count"] == 0
    assert record.summary["revision_count"] == 1
    assert record.technical is None
    body = str(record.as_dict())
    assert str(tmp_path) not in body
    assert "open REAL" not in body
    assert query_dataset_artifacts(manager=manager, as_of_ts="120000") == ()
    assert (
        query_dataset_artifacts(
            manager=manager, known_at="2026-01-01T00:00:00Z"
        )
        == ()
    )


def test_dataset_detail_verifies_snapshot_and_exposes_metadata_lineage(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    first = _freeze(tmp_path, manager)
    next_source = {
        **TEST_SOURCE_PROVENANCE.sources[0].as_dict(),
        "release_id": "test-release-v2",
        "received_at": "2026-01-02T00:00:01Z",
        "content_hash": "sha256:" + "5" * 64,
    }
    revision = build_dataset_source_provenance(
        source_catalog=TEST_SOURCE_PROVENANCE.source_catalog,
        sources=(next_source,),
        source_priority=TEST_SOURCE_PROVENANCE.source_priority,
        lineage=(stage.as_dict() for stage in TEST_SOURCE_PROVENANCE.lineage),
    )
    _freeze(tmp_path, manager, provenance=revision)

    record = query_dataset_artifact_detail(
        manager=manager,
        artifact_id=str(first["artifact_id"]),
        version=str(first["artifact_manifest_hash"]),
    )

    assert record.technical is not None
    technical = record.technical
    assert technical["snapshot"]["verification"]["overall_status"] == "VERIFIED"
    assert technical["quality"]["status"] == "PASS"
    assert technical["quality"]["verified_dense_grid"] == {
        "status": "PASS",
        "method": "verified_adapter_timestamp_dense_grid_scan",
        "row_count": 2,
        "missing_count": 0,
        "off_grid_count": 0,
        "start_ts": 0,
        "end_ts": 60_000,
    }
    assert len(technical["revision_history"]) == 2
    assert technical["raw_cleaned_comparison"][
        "raw_to_cleaned_content_changed"
    ] is True
    assert [row["layer"] for row in technical["lineage"]] == [
        "raw",
        "cleaned",
        "standardized",
    ]
    assert technical["point_in_time"]["provider_policies"][0][
        "point_in_time_policy"
    ] == "event_available_received_processed_times"
    assert technical["feature_input_contract"]["feature_values_exposed"] is False
    body = str(record.as_dict())
    assert str(tmp_path) not in body
    assert "candles.sqlite" not in body

    as_known_then = query_dataset_artifacts(
        manager=manager,
        artifact_id=str(first["artifact_id"]),
        known_at="2026-01-01T00:00:01Z",
        detail_level="technical",
    )
    assert len(as_known_then) == 1
    assert as_known_then[0].technical is not None
    assert len(as_known_then[0].technical["revision_history"]) == 1


def test_technical_dataset_detail_fails_closed_on_tampered_artifact(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    frozen = _freeze(tmp_path, manager)
    with sqlite3.connect(str(frozen["artifact_path"])) as db:
        db.execute("UPDATE candles SET close=999 WHERE ts=60000")

    with pytest.raises(
        ResearchExplorationQueryError,
        match="dataset_artifact_verification_failed",
    ):
        query_dataset_artifact_detail(
            manager=manager,
            artifact_id=str(frozen["artifact_id"]),
            version=str(frozen["artifact_manifest_hash"]),
        )


def test_feature_authority_query_is_versioned_hash_bound_and_value_free() -> None:
    registry = builtin_strategy_registry()
    records = query_feature_definitions(
        registry=registry,
        strategy="sma_with_filter",
        input_name="candles.close",
    )

    assert records
    close = next(item for item in records if item.logical_id == "sma_with_filter.close")
    assert close.version == "1.0.0"
    assert close.summary["definition_hash"].startswith("sha256:")
    assert close.technical is None
    detail = query_feature_definition_detail(
        registry=registry,
        feature_id=close.logical_id,
        version=close.version,
    )
    assert detail.technical is not None
    assert detail.technical["definition"]["implementation_code_hash"].startswith(
        "sha256:"
    )
    assert "calculator" not in str(detail.as_dict())
    assert "feature_values" not in str(detail.as_dict())
    assert query_feature_definitions(
        registry=registry, strategy="SMA_WITH_FILTER", feature_id=close.logical_id
    )[0].logical_id == close.logical_id


@pytest.mark.parametrize(
    ("filters", "reason"),
    (
        ({"quality_status": "UNKNOWN"}, "dataset_quality_filter_invalid"),
        ({"start_ts": "later"}, "dataset_start_ts_filter_invalid"),
        ({"start_ts": "2", "end_ts": "1"}, "dataset_time_query_invalid"),
        ({"known_at": "2026-01-01"}, "dataset_known_at_filter_invalid"),
    ),
)
def test_dataset_filters_fail_closed(
    tmp_path: Path, filters: dict[str, str], reason: str
) -> None:
    manager = _manager(tmp_path)
    _freeze(tmp_path, manager)
    with pytest.raises(ResearchExplorationQueryError, match=reason):
        query_dataset_artifacts(manager=manager, **filters)
