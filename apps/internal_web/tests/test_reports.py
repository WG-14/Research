from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from market_research.application.contracts import ReportComparisonRequest
from market_research.application.service import ResearchApplicationService
from market_research.application.platform_contracts import (
    ResearchPathManager,
    ResearchSettings,
    write_json_atomic,
)
from market_research.research.hashing import (
    content_hash_payload,
    report_content_hash_payload,
    sha256_prefixed,
)
from market_research.research.research_decision_report import REPORT_SECTIONS
from portal.models import ManifestUpload, ResearchJob
from portal.reports import (
    VisibleDecisionReportResolver,
    compare_visible_reports,
    list_visible_reports,
)


pytestmark = pytest.mark.django_db


@pytest.fixture
def report_paths(tmp_path: Path, settings) -> ResearchPathManager:
    roots = tmp_path / "report-state"
    manager = ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=roots / "data",
            artifact_root=roots / "artifacts",
            report_root=roots / "reports",
            cache_root=roots / "cache",
            db_path=None,
            max_workers=1,
            random_seed=0,
        ),
        project_root=Path(__file__).resolve().parents[3],
    )
    manager.ensure_roots()
    settings.RESEARCH_PATHS = manager
    settings.INTERNAL_WEB_AUDIT_PATH = manager.artifact_path(
        "_internal_web",
        "audit",
        "web_audit.jsonl",
    )
    return manager


def _manifest(owner: Any, experiment_id: str, manifest_hash: str) -> ManifestUpload:
    digest = uuid.uuid4().hex
    return ManifestUpload.objects.create(
        owner=owner,
        display_name=f"{experiment_id}.json",
        storage_ref=f"data:_internal_web/manifests/{digest}.json",
        content_hash="sha256:" + digest.ljust(64, "0"),
        manifest_hash=manifest_hash,
        size_bytes=128,
        experiment_id=experiment_id,
        strategy_name="noop_baseline",
    )


def _decision_report(
    *,
    experiment_id: str,
    manifest_hash: str,
    run_id: str,
    debug_path: str | None = None,
    padding: str = "",
) -> dict[str, Any]:
    sections: dict[str, Any] = {name: {} for name in REPORT_SECTIONS}
    sections["hypothesis_and_experiment_conditions"] = {
        "market": "KRW-BTC",
        "interval": "1m",
        "strategy_name": "noop_baseline",
        "strategy_version": "1",
    }
    sections["known_limitations"] = {
        "debug_path": debug_path,
        "note": padding,
    }
    sections["research_conclusion"] = {
        "human_research_decision": "NOT_REVIEWED",
        "operational_permission": False,
    }
    material: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "research_decision_report",
        "experiment_id": experiment_id,
        "run_id": run_id,
        "manifest_hash": manifest_hash,
        "selection_report_hash": "sha256:" + "b" * 64,
        "selected_candidate_id": "candidate-1",
        "validation_result": "PASS",
        "sections": sections,
    }
    material["content_hash"] = sha256_prefixed(
        content_hash_payload(material),
        label="research_decision_report",
    )
    return material


def _publish_validation_report(
    *,
    owner: Any,
    paths: ResearchPathManager,
    experiment_id: str,
    debug_path: str | None = None,
    padding: str = "",
) -> tuple[ResearchJob, str, Path]:
    manifest_hash = "sha256:" + uuid.uuid4().hex.ljust(64, "a")
    manifest = _manifest(owner, experiment_id, manifest_hash)
    run_id = f"run-{uuid.uuid4().hex}"
    decision = _decision_report(
        experiment_id=experiment_id,
        manifest_hash=manifest_hash,
        run_id=run_id,
        debug_path=debug_path,
        padding=padding,
    )
    candidate_path = paths.report_path(
        "research",
        experiment_id,
        "research_candidate_report.json",
    )
    write_json_atomic(candidate_path, decision)

    job_id = uuid.uuid4()
    summary_path = paths.report_path(
        "_internal_web",
        str(job_id),
        "validation_result.json",
    )
    summary: dict[str, Any] = {
        "schema_version": 3,
        "artifact_type": "validated_research_result",
        "experiment_id": experiment_id,
        "run_id": run_id,
        "manifest_hash": manifest_hash,
        "market": "KRW-BTC",
        "interval": "1m",
        "strategy_name": "noop_baseline",
        "strategy_version": "1",
        "selection_report_hash": "sha256:" + "b" * 64,
        "selected_candidate_id": "candidate-1",
        "end_to_end_validation_result": "PASS",
        "research_candidate_report_hash": decision["content_hash"],
        # Real summaries contain these runtime-only paths.  The resolver must
        # never use or expose either value.
        "validation_run_path": str(summary_path),
        "research_candidate_report_path": "/untrusted/db-controlled/path.json",
    }
    summary["content_hash"] = sha256_prefixed(report_content_hash_payload(summary))
    write_json_atomic(summary_path, summary)

    job = ResearchJob.objects.create(
        id=job_id,
        owner=owner,
        manifest=manifest,
        capability_id=ResearchJob.Capability.VALIDATE,
        status=ResearchJob.Status.SUCCEEDED,
        request_payload={},
        request_hash="sha256:" + uuid.uuid4().hex.ljust(64, "c"),
        idempotency_key=uuid.uuid4().hex,
        actor_id=str(owner.pk),
        actor_roles=["research_runner"],
        actor_permissions=["research.execute", "research.view"],
        run_id=run_id,
        result_ref=f"report:_internal_web/{job_id}/validation_result.json",
        result_hash=str(summary["content_hash"]),
        research_outcome=ResearchJob.ResearchOutcome.PASS,
    )
    report_id = "report_" + str(decision["content_hash"]).removeprefix("sha256:")
    return job, report_id, candidate_path


def test_visible_catalog_and_comparison_reverify_and_return_path_free_projection(
    runner_user,
    report_paths: ResearchPathManager,
) -> None:
    _first_job, first_id, _first_path = _publish_validation_report(
        owner=runner_user,
        paths=report_paths,
        experiment_id=f"report-{uuid.uuid4().hex}",
        debug_path="/srv/private/research.sqlite",
    )
    _second_job, second_id, _second_path = _publish_validation_report(
        owner=runner_user,
        paths=report_paths,
        experiment_id=f"report-{uuid.uuid4().hex}",
    )

    catalog = list_visible_reports(runner_user)
    assert {item["report_id"] for item in catalog} == {first_id, second_id}
    assert all(item["integrity_status"] == "VERIFIED" for item in catalog)
    assert "path" not in json.dumps(catalog).lower()

    service = ResearchApplicationService(report_paths, strategy_registry=object())
    projection = compare_visible_reports(
        runner_user,
        ReportComparisonRequest(report_ids=(second_id, first_id)),
        service=service,
    )
    assert projection["source_report_ids"] == sorted((first_id, second_id))
    assert projection["comparison"]["comparison_compatibility"] == "PASS"
    rendered = json.dumps(projection, sort_keys=True)
    assert "/srv/private" not in rendered
    assert "<server-managed>" in rendered
    recorded_hash = projection.pop("content_hash")
    assert recorded_hash == sha256_prefixed(content_hash_payload(projection))


def test_non_owner_cannot_resolve_owner_reports(
    runner_user,
    report_paths: ResearchPathManager,
) -> None:
    _job, first_id, _path = _publish_validation_report(
        owner=runner_user,
        paths=report_paths,
        experiment_id=f"report-{uuid.uuid4().hex}",
    )
    _job, second_id, _path = _publish_validation_report(
        owner=runner_user,
        paths=report_paths,
        experiment_id=f"report-{uuid.uuid4().hex}",
    )
    other = get_user_model().objects.create_user(
        username=f"other-{uuid.uuid4().hex}",
        password="test-password",
    )
    other.groups.add(Group.objects.get(name="research_runner"))

    assert list_visible_reports(other) == ()
    with pytest.raises(ValidationError, match="report_not_visible_or_invalid"):
        VisibleDecisionReportResolver(other).load_reports((first_id, second_id))


def test_db_redirect_and_candidate_symlink_never_become_report_authority(
    runner_user,
    report_paths: ResearchPathManager,
) -> None:
    first_job, _first_id, first_path = _publish_validation_report(
        owner=runner_user,
        paths=report_paths,
        experiment_id=f"report-{uuid.uuid4().hex}",
    )
    second_job, second_id, second_path = _publish_validation_report(
        owner=runner_user,
        paths=report_paths,
        experiment_id=f"report-{uuid.uuid4().hex}",
    )

    first_job.result_ref = second_job.result_ref
    first_job.save(update_fields=["result_ref"])
    first_path.unlink()
    first_path.symlink_to(second_path)

    catalog = list_visible_reports(runner_user)
    assert [item["report_id"] for item in catalog] == [second_id]


def test_oversized_candidate_report_is_not_exposed(
    runner_user,
    report_paths: ResearchPathManager,
    settings,
) -> None:
    _job, _report_id, _path = _publish_validation_report(
        owner=runner_user,
        paths=report_paths,
        experiment_id=f"report-{uuid.uuid4().hex}",
        padding="x" * 20_000,
    )
    settings.INTERNAL_WEB_MAX_RESULT_BYTES = 8_000

    assert list_visible_reports(runner_user) == ()


def test_report_views_list_and_compare_only_verified_visible_reports(
    client,
    runner_user,
    report_paths: ResearchPathManager,
) -> None:
    _job, first_id, _path = _publish_validation_report(
        owner=runner_user,
        paths=report_paths,
        experiment_id="visible-first",
        debug_path="/srv/private/research.sqlite",
    )
    _job, second_id, _path = _publish_validation_report(
        owner=runner_user,
        paths=report_paths,
        experiment_id="visible-second",
    )
    client.force_login(runner_user)

    catalog = client.get(reverse("portal:report-list"))
    assert catalog.status_code == 200
    assert {item["report_id"] for item in catalog.context["reports"]} == {
        first_id,
        second_id,
    }
    catalog_body = catalog.content.decode("utf-8")
    assert "visible-first" in catalog_body
    assert "visible-second" in catalog_body
    assert "/untrusted/db-controlled" not in catalog_body

    compared = client.post(
        reverse("portal:report-compare"),
        {"report_ids": [second_id, first_id]},
    )
    assert compared.status_code == 200
    comparison = compared.context["comparison"]
    assert comparison["source_report_ids"] == sorted((first_id, second_id))
    assert comparison["comparison"]["comparison_compatibility"] == "PASS"
    compared_body = compared.content.decode("utf-8")
    assert "비교 호환성: PASS" in compared_body
    assert "/srv/private" not in compared_body
    assert Path(settings.INTERNAL_WEB_AUDIT_PATH).is_file()


def test_report_compare_bad_or_invisible_selection_fails_closed(
    client,
    runner_user,
    report_paths: ResearchPathManager,
) -> None:
    _job, first_id, _path = _publish_validation_report(
        owner=runner_user,
        paths=report_paths,
        experiment_id=f"report-{uuid.uuid4().hex}",
    )
    _job, second_id, _path = _publish_validation_report(
        owner=runner_user,
        paths=report_paths,
        experiment_id=f"report-{uuid.uuid4().hex}",
    )
    other = get_user_model().objects.create_user(
        username=f"other-view-{uuid.uuid4().hex}",
        password="test-password",
    )
    other.groups.add(Group.objects.get(name="research_runner"))
    client.force_login(other)

    assert client.get(reverse("portal:report-list")).context["reports"] == ()
    response = client.post(
        reverse("portal:report-compare"),
        {"report_ids": [first_id, second_id]},
        follow=True,
    )
    assert response.redirect_chain == [(reverse("portal:report-list"), 302)]
    body = response.content.decode("utf-8")
    assert "비교할 수 있는 검증 완료 보고서를 2개 이상 선택해 주세요" in body
    assert "비교 호환성:" not in body
    assert not Path(settings.INTERNAL_WEB_AUDIT_PATH).exists()

    client.force_login(runner_user)
    malformed = client.post(
        reverse("portal:report-compare"),
        {"report_ids": [first_id, "/tmp/report.json"]},
    )
    assert malformed.status_code == 302
    assert malformed.url == reverse("portal:report-list")
    assert not Path(settings.INTERNAL_WEB_AUDIT_PATH).exists()


def test_report_views_require_permission_and_post_requires_csrf(
    client,
    runner_user,
    report_paths: ResearchPathManager,
) -> None:
    list_url = reverse("portal:report-list")
    compare_url = reverse("portal:report-compare")
    assert client.get(list_url).status_code == 302

    unauthorized = get_user_model().objects.create_user(
        username=f"unauthorized-{uuid.uuid4().hex}",
        password="test-password",
    )
    client.force_login(unauthorized)
    assert client.get(list_url).status_code == 403
    assert client.post(compare_url, {"report_ids": []}).status_code == 403

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(runner_user)
    assert (
        csrf_client.post(
            compare_url,
            {"report_ids": ["report_" + "1" * 64, "report_" + "2" * 64]},
        ).status_code
        == 403
    )
