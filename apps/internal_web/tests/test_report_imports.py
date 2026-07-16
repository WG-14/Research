from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import NoReverseMatch, reverse

from market_research.paths import ResearchPathManager
from market_research.research.hashing import content_hash_payload, sha256_prefixed
from market_research.research.research_decision_report import REPORT_SECTIONS
from market_research.settings import ResearchSettings
from portal.models import ImportedDecisionReport, WebAuditEvent
from portal.report_imports import (
    HistoricalReportImportConflict,
    import_historical_decision_report,
)
from portal.reports import list_visible_reports
from portal.storage import resolve_artifact_ref


pytestmark = pytest.mark.django_db

CODE_REVISION = "a" * 40
DATASET_HASH = "sha256:" + "d" * 64
MANIFEST_HASH = "sha256:" + "c" * 64


@pytest.fixture
def import_environment(tmp_path: Path, settings):
    state = tmp_path / "state"
    paths = ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=state / "data",
            artifact_root=state / "artifacts",
            report_root=state / "reports",
            cache_root=state / "cache",
            db_path=None,
            max_workers=1,
            random_seed=0,
        ),
        project_root=Path(__file__).resolve().parents[3],
    )
    paths.ensure_roots()
    import_root = tmp_path / "cli-exports"
    import_root.mkdir()
    settings.RESEARCH_PATHS = paths
    settings.INTERNAL_WEB_REPORT_IMPORT_ROOTS = (import_root,)
    settings.INTERNAL_WEB_AUDIT_PATH = paths.artifact_path(
        "_internal_web",
        "audit",
        "web_audit.jsonl",
    )
    settings.INTERNAL_WEB_MAX_RESULT_BYTES = 2 * 1024 * 1024
    return paths, import_root


@pytest.fixture
def admin_user(db):
    user = get_user_model().objects.create_user(
        username=f"report-import-admin-{uuid.uuid4().hex}",
        password="test-password",
    )
    user.groups.add(Group.objects.get(name="research_admin"))
    return user


def _report(
    *,
    experiment_id: str | None = None,
    run_id: str | None = None,
    code_revision: str = CODE_REVISION,
) -> dict[str, Any]:
    sections: dict[str, Any] = {name: {} for name in REPORT_SECTIONS}
    sections["hypothesis_and_experiment_conditions"] = {
        "market": "KRW-BTC",
        "interval": "1m",
        "strategy_name": "noop_baseline",
        "strategy_version": "1",
        "code_revision": code_revision,
    }
    sections["data_quality"] = {
        "dataset_snapshot_id": "immutable-snapshot-2026-07-16",
        "dataset_content_hash": DATASET_HASH,
    }
    sections["research_conclusion"] = {
        "human_research_decision": "NOT_REVIEWED",
        "operational_permission": False,
        "validation_result": "PASS",
    }
    payload: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "research_decision_report",
        "experiment_id": experiment_id or f"historical-{uuid.uuid4().hex}",
        "run_id": run_id or f"run-{uuid.uuid4().hex}",
        "manifest_hash": MANIFEST_HASH,
        "selection_report_hash": "sha256:" + "b" * 64,
        "selected_candidate_id": "candidate-1",
        "validation_result": "PASS",
        "sections": sections,
    }
    payload["content_hash"] = sha256_prefixed(
        content_hash_payload(payload),
        label="research_decision_report",
    )
    return payload


def _write_report(import_root: Path, payload: dict[str, Any], name: str = "report.json") -> Path:
    path = import_root / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _arguments(
    *,
    actor: Any,
    owner: Any,
    source: Path,
    report: dict[str, Any],
    visibility: str = ImportedDecisionReport.Visibility.OWNER,
) -> dict[str, Any]:
    data_quality = report["sections"]["data_quality"]
    return {
        "actor": actor,
        "owner": owner,
        "source_path": str(source),
        "expected_report_hash": report["content_hash"],
        "expected_manifest_hash": report["manifest_hash"],
        "expected_experiment_id": report["experiment_id"],
        "expected_run_id": report["run_id"],
        "expected_dataset_snapshot_id": data_quality.get(
            "dataset_snapshot_id",
            "immutable-snapshot-2026-07-16",
        ),
        "expected_dataset_content_hash": data_quality.get(
            "dataset_content_hash",
            DATASET_HASH,
        ),
        "code_revision": CODE_REVISION,
        "visibility": visibility,
        "correlation_id": str(uuid.uuid4()),
    }


def test_admin_import_publishes_managed_copy_and_owner_catalog_survives_source_removal(
    admin_user,
    runner_user,
    import_environment,
) -> None:
    _paths, import_root = import_environment
    report = _report()
    source = _write_report(import_root, report)

    result = import_historical_decision_report(
        **_arguments(
            actor=admin_user,
            owner=runner_user,
            source=source,
            report=report,
        )
    )

    assert result.created is True
    record = result.record
    managed = resolve_artifact_ref(record.storage_ref)
    assert managed != source
    assert managed.is_file()
    assert json.loads(managed.read_text(encoding="utf-8")) == report
    assert str(source) not in {
        str(getattr(record, field.name))
        for field in record._meta.concrete_fields
    }
    assert record.imported_by == admin_user
    assert record.owner == runner_user
    assert record.code_revision == CODE_REVISION
    assert record.dataset_content_hash == DATASET_HASH
    assert WebAuditEvent.objects.count() == 1
    assert str(source) not in json.dumps(WebAuditEvent.objects.get().payload)

    source.unlink()
    catalog = list_visible_reports(runner_user)
    assert [item["report_id"] for item in catalog] == [record.report_id]
    assert catalog[0]["catalog_source"] == "HISTORICAL_CLI_IMPORT"
    assert catalog[0]["integrity_status"] == "VERIFIED"


def test_owner_visibility_and_organization_visibility_are_enforced(
    admin_user,
    runner_user,
    import_environment,
) -> None:
    _paths, import_root = import_environment
    other = get_user_model().objects.create_user(
        username=f"other-report-user-{uuid.uuid4().hex}",
        password="test-password",
    )
    other.groups.add(Group.objects.get(name="research_runner"))
    owner_report = _report()
    owner_source = _write_report(import_root, owner_report, "owner.json")
    import_historical_decision_report(
        **_arguments(
            actor=admin_user,
            owner=runner_user,
            source=owner_source,
            report=owner_report,
        )
    )
    assert list_visible_reports(other) == ()

    organization_report = _report()
    organization_source = _write_report(
        import_root,
        organization_report,
        "organization.json",
    )
    imported = import_historical_decision_report(
        **_arguments(
            actor=admin_user,
            owner=runner_user,
            source=organization_source,
            report=organization_report,
            visibility=ImportedDecisionReport.Visibility.ORGANIZATION,
        )
    ).record

    assert [item["report_id"] for item in list_visible_reports(other)] == [
        imported.report_id
    ]


def test_import_permission_is_checked_before_read_or_copy(
    runner_user,
    import_environment,
) -> None:
    paths, import_root = import_environment
    report = _report()
    missing_source = import_root / "missing.json"

    with pytest.raises(
        PermissionDenied,
        match="historical_report_import_permission_required",
    ):
        import_historical_decision_report(
            **_arguments(
                actor=runner_user,
                owner=runner_user,
                source=missing_source,
                report=report,
            )
        )

    assert not ImportedDecisionReport.objects.exists()
    assert not WebAuditEvent.objects.exists()
    assert not paths.report_path("_internal_web").exists()


def test_traversal_and_symlink_sources_fail_closed(
    admin_user,
    runner_user,
    import_environment,
    tmp_path: Path,
) -> None:
    _paths, import_root = import_environment
    report = _report()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(report), encoding="utf-8")
    traversal = import_root / ".." / outside.name
    symlink = import_root / "linked.json"
    symlink.symlink_to(outside)

    for source in (traversal, symlink):
        with pytest.raises(ValidationError):
            import_historical_decision_report(
                **_arguments(
                    actor=admin_user,
                    owner=runner_user,
                    source=source,
                    report=report,
                )
            )

    assert not ImportedDecisionReport.objects.exists()
    assert not WebAuditEvent.objects.exists()


def test_source_read_is_bounded(
    admin_user,
    runner_user,
    import_environment,
    settings,
) -> None:
    _paths, import_root = import_environment
    report = _report()
    source = _write_report(import_root, report)
    settings.INTERNAL_WEB_MAX_RESULT_BYTES = 64

    with pytest.raises(
        ValidationError,
        match="historical_report_too_large_to_verify",
    ):
        import_historical_decision_report(
            **_arguments(
                actor=admin_user,
                owner=runner_user,
                source=source,
                report=report,
            )
        )

    assert not ImportedDecisionReport.objects.exists()


@pytest.mark.parametrize("failure_kind", ("incomplete", "tampered", "code_revision"))
def test_incomplete_or_tampered_evidence_is_not_imported(
    admin_user,
    runner_user,
    import_environment,
    failure_kind: str,
) -> None:
    _paths, import_root = import_environment
    report = _report()
    if failure_kind == "incomplete":
        del report["sections"]["data_quality"]["dataset_snapshot_id"]
        report["content_hash"] = sha256_prefixed(
            content_hash_payload(
                {key: value for key, value in report.items() if key != "content_hash"}
            ),
            label="research_decision_report",
        )
    elif failure_kind == "tampered":
        report["sections"]["known_limitations"] = {"tampered": True}
    else:
        report["sections"]["hypothesis_and_experiment_conditions"][
            "code_revision"
        ] = "b" * 40
        report["content_hash"] = sha256_prefixed(
            content_hash_payload(
                {key: value for key, value in report.items() if key != "content_hash"}
            ),
            label="research_decision_report",
        )
    source = _write_report(import_root, report)
    arguments = _arguments(
        actor=admin_user,
        owner=runner_user,
        source=source,
        report=report,
    )
    with pytest.raises(ValidationError):
        import_historical_decision_report(**arguments)

    assert not ImportedDecisionReport.objects.exists()
    assert not WebAuditEvent.objects.exists()


def test_exact_duplicate_converges_but_different_owner_binding_conflicts(
    admin_user,
    runner_user,
    import_environment,
) -> None:
    _paths, import_root = import_environment
    other = get_user_model().objects.create_user(
        username=f"different-owner-{uuid.uuid4().hex}",
        password="test-password",
    )
    report = _report()
    source = _write_report(import_root, report)
    arguments = _arguments(
        actor=admin_user,
        owner=runner_user,
        source=source,
        report=report,
    )

    first = import_historical_decision_report(**arguments)
    second = import_historical_decision_report(
        **{**arguments, "correlation_id": str(uuid.uuid4())}
    )

    assert first.created is True
    assert second.created is False
    assert first.record.pk == second.record.pk
    assert ImportedDecisionReport.objects.count() == 1
    assert WebAuditEvent.objects.count() == 2

    with pytest.raises(HistoricalReportImportConflict):
        import_historical_decision_report(
            **{
                **arguments,
                "owner": other,
                "correlation_id": str(uuid.uuid4()),
            }
        )
    assert ImportedDecisionReport.objects.count() == 1


def test_audit_outbox_failure_rolls_back_catalog_manifest(
    admin_user,
    runner_user,
    import_environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _paths, import_root = import_environment
    report = _report()
    source = _write_report(import_root, report)
    monkeypatch.setattr(
        "portal.report_imports.record_web_audit_event",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )

    with pytest.raises(RuntimeError, match="audit unavailable"):
        import_historical_decision_report(
            **_arguments(
                actor=admin_user,
                owner=runner_user,
                source=source,
                report=report,
            )
        )

    assert not ImportedDecisionReport.objects.exists()
    assert not WebAuditEvent.objects.exists()


def test_managed_copy_tampering_removes_report_from_visible_catalog(
    admin_user,
    runner_user,
    import_environment,
) -> None:
    _paths, import_root = import_environment
    report = _report()
    source = _write_report(import_root, report)
    record = import_historical_decision_report(
        **_arguments(
            actor=admin_user,
            owner=runner_user,
            source=source,
            report=report,
        )
    ).record
    managed = resolve_artifact_ref(record.storage_ref)
    managed.write_text("{}", encoding="utf-8")

    assert list_visible_reports(runner_user) == ()


def test_report_import_view_is_admin_only_and_reproduction_route_stays_absent(
    client,
    admin_user,
    runner_user,
    import_environment,
) -> None:
    _paths, import_root = import_environment
    report = _report()
    source = _write_report(import_root, report)
    payload = _arguments(
        actor=admin_user,
        owner=runner_user,
        source=source,
        report=report,
    )
    form_payload = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "actor",
            "owner",
            "correlation_id",
        }
    }
    form_payload["owner"] = runner_user.pk

    client.force_login(runner_user)
    assert client.post(reverse("portal:report-import"), form_payload).status_code == 403

    client.force_login(admin_user)
    response = client.post(reverse("portal:report-import"), form_payload)
    assert response.status_code == 302
    assert response.url == reverse("portal:report-list")
    assert ImportedDecisionReport.objects.count() == 1
    catalog = client.get(reverse("portal:report-list"))
    assert catalog.status_code == 200
    assert "과거 CLI 보고서 가져오기" in catalog.content.decode("utf-8")

    with pytest.raises(NoReverseMatch):
        reverse("portal:report-reproduce")
