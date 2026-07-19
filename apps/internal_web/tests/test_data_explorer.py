from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from market_research.paths import ResearchPathManager
from market_research.research.dataset_freeze import freeze_sqlite_candles_dataset
from market_research.research.datasets.source_catalog import build_source_catalog
from market_research.research.datasets.source_provenance import (
    DatasetSourceProvenance,
    build_dataset_source_provenance,
)
from market_research.research.exploration_queries import (
    ExplorationRecord,
    ResearchExplorationQueryError,
)
from market_research.settings import ResearchSettings
from portal.api_contract import (
    ApiErrorEnvelope,
    ResearchListResponse,
    ResearchResource,
    build_openapi_document,
)
from portal.research_explorer import ResearchExplorerService


pytestmark = pytest.mark.django_db


def _source_provenance() -> DatasetSourceProvenance:
    catalog = build_source_catalog(
        catalog_id="web-test-catalog",
        version="v1",
        approved_at="2025-12-31T00:00:00Z",
        approved_by="web-test-steward",
        entries=(
            {
                "provider_id": "web-test-provider",
                "display_name": "Externally prepared web fixture",
                "data_kinds": ["ohlcv"],
                "frequencies": ["1m"],
                "source_kinds": ["file_export"],
                "point_in_time_policy": (
                    "event_available_received_processed_times"
                ),
                "revision_policy": "append_new_release_preserve_prior",
                "license_id": "web-test-license-v1",
                "research_use_terms": "offline reproducible research only",
                "redistribution_allowed": False,
                "quality_level": "VERIFIED",
                "preparation_boundary": (
                    "externally_prepared_offline_immutable_input_only"
                ),
                "credential_boundary": (
                    "credentials_external_to_research_distribution"
                ),
                "owner": "web-test-steward",
                "expected_delivery_lag_seconds": 1.0,
                "maximum_staleness_seconds": 3600.0,
            },
        ),
    )
    return build_dataset_source_provenance(
        source_catalog=catalog,
        sources=(
            {
                "provider_id": "web-test-provider",
                "dataset_id": "web-test-candles",
                "release_id": "release-v1",
                "source_kind": "file_export",
                "request_parameters": {"interval": "1m", "market": "KRW-BTC"},
                "requested_at": "2026-01-01T00:00:00Z",
                "received_at": "2026-01-01T00:00:01Z",
                "response_version": "web-export-v1",
                "acquisition_code_version": "external-web-fixture-v1",
                "retry_count": 0,
                "acquisition_status": "complete",
                "error_code": "",
                "coverage_start_ts": -(2**63),
                "coverage_end_ts": 2**63 - 1,
                "content_hash": "sha256:" + "1" * 64,
            },
        ),
        source_priority=("web-test-provider",),
        lineage=tuple(
            {
                "layer": layer,
                "artifact_id": f"web-{layer}-v1",
                "content_hash": "sha256:" + character * 64,
                "schema_version": 1,
                "transformation_id": f"web-{layer}-transform-v1",
            }
            for layer, character in (
                ("raw", "2"),
                ("cleaned", "3"),
                ("standardized", "4"),
            )
        ),
    )


def _published_dataset(tmp_path: Path) -> tuple[ResearchPathManager, dict[str, object]]:
    source = tmp_path / "prepared-web.sqlite"
    with sqlite3.connect(source) as db:
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
    manager = ResearchPathManager.from_settings(
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
    frozen = freeze_sqlite_candles_dataset(
        source_provenance=_source_provenance(),
        source_db=source,
        market="KRW-BTC",
        interval="1m",
        start_ts=0,
        end_ts=60_000,
        out_dir=manager.data_root,
    )
    return manager, frozen


class FakeDataExplorer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str], str]] = []

    @staticmethod
    def _dataset(logical_id: str, *, technical: bool) -> dict[str, Any]:
        record = ExplorationRecord(
            kind="dataset_artifact",
            logical_id=logical_id,
            version="sha256:" + "a" * 64,
            status="PASS",
            summary={
                "market": "KRW-BTC",
                "interval": "1m",
                "row_count": 2,
                "missing_count": 0,
                "revision_count": 2,
            },
            technical=(
                {
                    "snapshot": {
                        "artifact_content_hash": "sha256:" + "b" * 64,
                        "verification": {"overall_status": "VERIFIED"},
                    },
                    "point_in_time": {
                        "observation_time_basis": "candle_event_timestamp",
                        "knowledge_time_basis": (
                            "externally_recorded_source_received_at"
                        )
                    },
                    "quality": {
                        "verified_dense_grid": {
                            "status": "PASS",
                            "method": "verified_adapter_timestamp_dense_grid_scan",
                            "missing_count": 0,
                            "off_grid_count": 0,
                        }
                    },
                    "revision_history": [{"release_id": "release-v1"}],
                    "lineage": [
                        {
                            "layer": "raw",
                            "artifact_id": "raw-v1",
                            "content_hash": "sha256:" + "e" * 64,
                            "transformation_id": "external-v1",
                        },
                        {
                            "layer": "cleaned",
                            "artifact_id": "cleaned-v1",
                            "content_hash": "sha256:" + "f" * 64,
                            "transformation_id": "clean-v1",
                        },
                    ],
                    "raw_cleaned_comparison": {
                        "comparison_scope": "metadata_and_content_hash_only",
                        "raw_values_exposed": False,
                    },
                    "artifact_path": "/private/datasets/candles.sqlite",
                    "api_secret": "do-not-expose",
                }
                if technical
                else None
            ),
        )
        return ResearchExplorerService._project_record(record)

    @staticmethod
    def _feature(logical_id: str, *, technical: bool) -> dict[str, Any]:
        record = ExplorationRecord(
            kind="feature_definition",
            logical_id=logical_id,
            version="1.0.0",
            status="ACTIVE",
            summary={
                "strategy": "sma_with_filter",
                "description": "Completed close authority",
                "inputs": ["candles.close"],
                "definition_hash": "sha256:" + "c" * 64,
            },
            technical=(
                {"definition": {"implementation_code_hash": "sha256:" + "d" * 64}}
                if technical
                else None
            ),
        )
        return ResearchExplorerService._project_record(record)

    def list_records(
        self,
        *,
        section: str,
        filters: dict[str, str],
        detail_level: str = "summary",
    ) -> tuple[dict[str, Any], ...]:
        self.calls.append((section, filters, detail_level))
        if filters.get("quality_status") == "INVALID":
            raise ResearchExplorationQueryError("dataset_quality_filter_invalid")
        if section == "datasets":
            return tuple(
                self._dataset(name, technical=detail_level == "technical")
                for name in ("immutable-candle:one", "immutable-candle:two")
            )
        if section == "features":
            return (
                self._feature(
                    "sma_with_filter.close", technical=detail_level == "technical"
                ),
            )
        raise ResearchExplorationQueryError("research_section_invalid")

    def get_record(
        self,
        *,
        section: str,
        logical_id: str,
        version: str,
        detail_level: str = "technical",
        record_type: str | None = None,
    ) -> dict[str, Any]:
        del version, record_type
        if section == "datasets":
            return self._dataset(logical_id, technical=detail_level == "technical")
        if section == "features":
            return self._feature(logical_id, technical=detail_level == "technical")
        raise ResearchExplorationQueryError("research_section_invalid")


@pytest.fixture
def fake_data_explorer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: Any
) -> FakeDataExplorer:
    from portal import api_views, views

    fake = FakeDataExplorer()
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "data-explorer-audit.jsonl"
    monkeypatch.setattr(api_views, "_research_service", lambda: fake)
    monkeypatch.setattr(views, "_research_explorer_service", lambda: fake)
    return fake


def test_dataset_api_filters_paginates_and_audits_actor(
    client: Client,
    runner_user: Any,
    fake_data_explorer: FakeDataExplorer,
    settings: Any,
) -> None:
    client.force_login(runner_user)
    response = client.get(
        reverse("portal:api-dataset-artifact-list"),
        {
            "market": "KRW-BTC",
            "quality_status": "PASS",
            "as_of_ts": "30000",
            "known_at": "2026-01-01T00:00:01Z",
            "limit": "1",
            "offset": "1",
        },
    )
    page = ResearchListResponse.model_validate(response.json())

    assert response.status_code == 200
    assert page.page.count == 2
    assert page.page.limit == 1
    assert page.page.offset == 1
    assert page.items[0].kind == "dataset_artifact"
    assert page.items[0].logical_id == "immutable-candle:two"
    assert page.items[0].technical is None
    assert page.items[0].links.web.startswith("/research/datasets/")
    assert fake_data_explorer.calls[-1] == (
        "datasets",
        {
            "market": "KRW-BTC",
            "quality_status": "PASS",
            "as_of_ts": "30000",
            "known_at": "2026-01-01T00:00:01Z",
        },
        "summary",
    )
    audit = json.loads(
        Path(settings.INTERNAL_WEB_AUDIT_PATH)
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    assert audit["action"] == "research_exploration_read"
    assert audit["actor_id"] == str(runner_user.pk)
    assert audit["object_type"] == "dataset_artifact_collection"
    assert audit["details"]["application_permission"] == "research.view"


def test_dataset_detail_is_verified_path_free_and_read_only(
    client: Client,
    runner_user: Any,
    fake_data_explorer: FakeDataExplorer,
) -> None:
    del fake_data_explorer
    client.force_login(runner_user)
    version = "sha256:" + "a" * 64
    response = client.get(
        reverse(
            "portal:api-dataset-artifact-detail",
            args=("immutable-candle:one", version),
        ),
        {"detail": "technical"},
    )
    resource = ResearchResource.model_validate(response.json())
    body = response.content.decode("utf-8")
    screen = client.get(
        reverse(
            "portal:research-explorer-detail",
            args=("datasets", "immutable-candle:one", version),
        )
    )
    screen_body = screen.content.decode("utf-8")

    assert response.status_code == screen.status_code == 200
    assert resource.technical is not None
    assert resource.technical["snapshot"]["verification"]["overall_status"] == (
        "VERIFIED"
    )
    assert resource.technical["raw_cleaned_comparison"][
        "raw_values_exposed"
    ] is False
    assert "artifact_path" not in resource.technical
    assert resource.technical["api_secret"] == "<redacted>"
    assert "/private/datasets" not in body
    assert "do-not-expose" not in body
    assert "Snapshot·품질" in screen_body
    assert "Point-in-time·수정 이력" in screen_body
    assert "Raw→정제→표준화 계보" in screen_body
    assert "/private/datasets" not in screen_body
    assert "do-not-expose" not in screen_body
    assert client.post(response.request["PATH_INFO"]).status_code == 405


def test_feature_api_and_html_data_tabs_use_stable_authorities(
    client: Client,
    runner_user: Any,
    fake_data_explorer: FakeDataExplorer,
) -> None:
    client.force_login(runner_user)
    feature = client.get(
        reverse("portal:api-feature-definition-list"),
        {"strategy": "sma_with_filter", "input_name": "candles.close"},
    )
    page = ResearchListResponse.model_validate(feature.json())
    data_screen = client.get(
        reverse("portal:research-explorer"),
        {"section": "datasets", "quality_status": "PASS"},
    )
    feature_screen = client.get(
        reverse("portal:research-explorer"), {"section": "features"}
    )

    assert feature.status_code == 200
    assert page.items[0].kind == "feature_definition"
    assert page.items[0].logical_id == "sma_with_filter.close"
    assert page.items[0].links.technical.startswith("/api/v1/research/features/")
    assert data_screen.status_code == feature_screen.status_code == 200
    assert "데이터/PIT" in data_screen.content.decode("utf-8")
    assert "immutable-candle:one" in data_screen.content.decode("utf-8")
    assert "Feature 정의" in feature_screen.content.decode("utf-8")
    assert "sma_with_filter.close" in feature_screen.content.decode("utf-8")
    assert fake_data_explorer.calls[-1][0] == "features"


def test_dataset_filter_error_and_openapi_are_stable_read_only_contracts(
    client: Client,
    runner_user: Any,
    fake_data_explorer: FakeDataExplorer,
) -> None:
    del fake_data_explorer
    client.force_login(runner_user)
    invalid = client.get(
        reverse("portal:api-dataset-artifact-list"),
        {"quality_status": "INVALID"},
    )
    error = ApiErrorEnvelope.model_validate(invalid.json()).error

    assert invalid.status_code == 400
    assert error.code == "RESEARCH_QUERY_INVALID"
    paths = build_openapi_document()["paths"]
    for path in (
        "/api/v1/research/datasets/",
        "/api/v1/research/datasets/{logical_id}/{version}/",
        "/api/v1/research/features/",
        "/api/v1/research/features/{logical_id}/{version}/",
    ):
        assert set(paths[path]) == {"get"}


def test_authenticated_api_executes_real_core_dataset_authority(
    client: Client,
    runner_user: Any,
    tmp_path: Path,
    settings: Any,
) -> None:
    manager, frozen = _published_dataset(tmp_path)
    settings.RESEARCH_PATHS = manager
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "real-data-explorer-audit.jsonl"
    client.force_login(runner_user)

    listing = client.get(
        reverse("portal:api-dataset-artifact-list"),
        {
            "market": "KRW-BTC",
            "quality_status": "PASS",
            "as_of_ts": "30000",
        },
    )
    page = ResearchListResponse.model_validate(listing.json())
    detail = client.get(
        reverse(
            "portal:api-dataset-artifact-detail",
            args=(frozen["artifact_id"], frozen["artifact_manifest_hash"]),
        ),
        {"detail": "technical"},
    )
    resource = ResearchResource.model_validate(detail.json())

    assert listing.status_code == detail.status_code == 200
    assert page.page.count == 1
    assert page.items[0].logical_id == frozen["artifact_id"]
    assert resource.technical is not None
    assert resource.technical["snapshot"]["verification"]["overall_status"] == (
        "VERIFIED"
    )
    assert str(tmp_path) not in listing.content.decode("utf-8")
    assert str(tmp_path) not in detail.content.decode("utf-8")
    assert Path(settings.INTERNAL_WEB_AUDIT_PATH).is_file()
