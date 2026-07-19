from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from market_research.research.exploration_queries import (
    ExplorationRecord,
    ResearchExplorationQueryError,
)
from portal.api_contract import (
    ApiErrorEnvelope,
    ResearchListResponse,
    ResearchProjectionResponse,
    ResearchResource,
    build_openapi_document,
)
from portal.research_explorer import ResearchExplorerService


pytestmark = pytest.mark.django_db


class FakeResearchExplorer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str], str]] = []

    @staticmethod
    def _record(logical_id: str, *, detail_level: str) -> dict[str, Any]:
        record = ExplorationRecord(
            kind="research_package",
            logical_id=logical_id,
            version="1",
            status="CONFIRMED",
            summary={
                "market": "KRW-BTC",
                "instrument": "BTC",
                "dataset_id": "dataset-one",
                "content_hash": "sha256:" + "a" * 64,
            },
            technical=(
                {
                    "evidence_refs": {
                        "dataset_snapshot": {
                            "logical_id": "dataset-one",
                            "version": "1",
                            "content_hash": "sha256:" + "b" * 64,
                        }
                    },
                    "artifact_path": "/private/research/holdout.json",
                    "api_secret": "never-expose-this",
                    "final_holdout_metrics": {"return_pct": 999.0},
                    "final_holdout_hash": "sha256:" + "c" * 64,
                }
                if detail_level == "technical"
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
        return tuple(
            self._record(name, detail_level=detail_level)
            for name in ("package-a", "package-b")
        )

    def get_record(
        self,
        *,
        section: str,
        logical_id: str,
        version: str,
        detail_level: str = "technical",
        record_type: str | None = None,
    ) -> dict[str, Any]:
        del section, version, record_type
        return self._record(logical_id, detail_level=detail_level)

    def package_lineage(self, *, package_id: str, version: str) -> dict[str, Any]:
        return {
            "package_ref": {"logical_id": package_id, "version": version},
            "evidence_refs": {"hypothesis": {"logical_id": "hypothesis-a"}},
        }

    def package_diff(self, **values: str) -> dict[str, Any]:
        return {
            "left_package_ref": {
                "logical_id": values["left_package_id"],
                "version": values["left_version"],
            },
            "right_package_ref": {
                "logical_id": values["right_package_id"],
                "version": values["right_version"],
            },
            "changes": {"validated_rule_set": {"changed": True}},
        }


@pytest.fixture
def fake_explorer(monkeypatch, tmp_path: Path, settings) -> FakeResearchExplorer:
    from portal import api_views, views

    fake = FakeResearchExplorer()
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "research-explorer-audit.jsonl"
    monkeypatch.setattr(api_views, "_research_service", lambda: fake)
    monkeypatch.setattr(views, "_research_explorer_service", lambda: fake)
    return fake


def test_research_api_requires_authentication_and_rbac(
    client: Client,
    fake_explorer: FakeResearchExplorer,
) -> None:
    del fake_explorer
    endpoint = reverse("portal:api-research-package-list")

    unauthenticated = client.get(endpoint)
    assert unauthenticated.status_code == 401
    assert (
        ApiErrorEnvelope.model_validate(unauthenticated.json()).error.code
        == "AUTHENTICATION_REQUIRED"
    )

    outsider = get_user_model().objects.create_user(
        username=f"no-research-role-{uuid.uuid4().hex}",
        password="test-password",
    )
    client.force_login(outsider)
    forbidden = client.get(endpoint)
    assert forbidden.status_code == 403
    assert (
        ApiErrorEnvelope.model_validate(forbidden.json()).error.code
        == "PERMISSION_DENIED"
    )


def test_package_api_has_filters_pagination_safe_defaults_and_actor_audit(
    client: Client,
    runner_user,
    fake_explorer: FakeResearchExplorer,
    settings,
) -> None:
    client.force_login(runner_user)
    response = client.get(
        reverse("portal:api-research-package-list"),
        {"market": "KRW-BTC", "limit": 1, "offset": 1},
    )
    page = ResearchListResponse.model_validate(response.json())

    assert response.status_code == 200
    assert page.page.count == 2
    assert page.page.limit == 1
    assert page.page.offset == 1
    assert page.page.previous is not None
    assert page.page.filters == {"market": "KRW-BTC"}
    assert page.items[0].logical_id == "package-b"
    assert page.items[0].technical is None
    assert page.items[0].links.web == "/research/packages/package-b/1/"
    assert fake_explorer.calls[-1] == (
        "packages",
        {"market": "KRW-BTC"},
        "summary",
    )

    audit = json.loads(
        Path(settings.INTERNAL_WEB_AUDIT_PATH)
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    assert audit["action"] == "research_exploration_read"
    assert audit["actor_id"] == str(runner_user.pk)
    assert audit["details"]["django_permission"] == "portal.view_researchjob"
    assert audit["details"]["application_permission"] == "research.view"
    assert "research_runner" in audit["details"]["roles"]


def test_technical_detail_redacts_path_secret_and_raw_holdout(
    client: Client,
    runner_user,
    fake_explorer: FakeResearchExplorer,
) -> None:
    del fake_explorer
    client.force_login(runner_user)
    response = client.get(
        reverse(
            "portal:api-research-package-detail",
            args=("package-a", "1"),
        ),
        {"detail": "technical"},
    )
    resource = ResearchResource.model_validate(response.json())
    body = response.content.decode("utf-8")

    assert response.status_code == 200
    assert resource.technical is not None
    assert "artifact_path" not in resource.technical
    assert resource.technical["api_secret"] == "<redacted>"
    assert resource.technical["final_holdout_metrics"] == (
        "<redacted-holdout-evidence>"
    )
    assert resource.technical["final_holdout_hash"].startswith("sha256:")
    assert "/private/research" not in body
    assert "never-expose-this" not in body
    assert "999.0" not in body


def test_package_lineage_diff_and_openapi_routes_are_read_only(
    client: Client,
    runner_user,
    fake_explorer: FakeResearchExplorer,
) -> None:
    del fake_explorer
    client.force_login(runner_user)
    lineage = client.get(
        reverse("portal:api-research-package-lineage", args=("package-a", "1"))
    )
    difference = client.get(
        reverse("portal:api-research-package-diff"),
        {
            "left_package_id": "package-a",
            "left_version": "1",
            "right_package_id": "package-b",
            "right_version": "1",
        },
    )

    assert lineage.status_code == difference.status_code == 200
    assert (
        ResearchProjectionResponse.model_validate(lineage.json()).kind
        == "research_package_lineage"
    )
    assert ResearchProjectionResponse.model_validate(difference.json()).payload[
        "changes"
    ]["validated_rule_set"]["changed"]
    paths = build_openapi_document()["paths"]
    assert "/api/v1/research/lineage/" in paths
    assert "/api/v1/research/validation-decisions/" in paths
    assert "/api/v1/research/prospective/" in paths
    assert "/api/v1/research/packages/diff/" in paths
    assert "post" not in paths["/api/v1/research/packages/diff/"]


def test_invalid_identity_and_mandatory_audit_failure_are_actionable(
    client: Client,
    runner_user,
    fake_explorer: FakeResearchExplorer,
    monkeypatch,
) -> None:
    del fake_explorer
    from portal import api_views

    client.force_login(runner_user)
    invalid = client.get(
        reverse(
            "portal:api-research-package-detail",
            args=("package-a", "bad id"),
        )
    )
    assert invalid.status_code == 400
    assert (
        ApiErrorEnvelope.model_validate(invalid.json()).error.code
        == "RESEARCH_ID_INVALID"
    )

    monkeypatch.setattr(
        api_views,
        "audit_research_exploration_read",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("audit offline")),
    )
    unavailable = client.get(reverse("portal:api-research-package-list"))
    assert unavailable.status_code == 503
    error = ApiErrorEnvelope.model_validate(unavailable.json()).error
    assert error.code == "AUDIT_UNAVAILABLE"
    assert error.retryable is True


def test_html_explorer_defaults_to_summary_and_folds_technical_evidence(
    client: Client,
    runner_user,
    fake_explorer: FakeResearchExplorer,
) -> None:
    del fake_explorer
    client.force_login(runner_user)
    listing = client.get(reverse("portal:research-explorer"))
    detail = client.get(
        reverse(
            "portal:research-explorer-detail",
            args=("packages", "package-a", "1"),
        )
    )

    listing_body = listing.content.decode("utf-8")
    detail_body = detail.content.decode("utf-8")
    assert listing.status_code == detail.status_code == 200
    assert "연구 증거 탐색" in listing_body
    assert "package-a" in listing_body
    assert "never-expose-this" not in listing_body
    assert "기술 증거 펼치기" in detail_body
    assert "패키지 lineage" in detail_body
    assert "/private/research" not in detail_body
    assert "never-expose-this" not in detail_body


def test_registry_query_error_has_stable_actionable_envelope(
    client: Client,
    runner_user,
    fake_explorer: FakeResearchExplorer,
    monkeypatch,
) -> None:
    del fake_explorer
    from portal import api_views

    class UnavailableExplorer(FakeResearchExplorer):
        def list_records(self, **kwargs: Any) -> tuple[dict[str, Any], ...]:
            raise ResearchExplorationQueryError("research_package_registry_invalid")

    monkeypatch.setattr(api_views, "_research_service", UnavailableExplorer)
    client.force_login(runner_user)
    response = client.get(reverse("portal:api-research-package-list"))

    assert response.status_code == 503
    error = ApiErrorEnvelope.model_validate(response.json()).error
    assert error.code == "RESEARCH_REGISTRY_UNAVAILABLE"
    assert error.correlation_id
