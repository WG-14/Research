from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Mapping
from contextlib import nullcontext
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from research_operations.admission import ExperimentAdmissionStore
from research_operations.backup import verify_signed_recovery_receipt
from research_operations.health import (
    AUDIT_OBSERVATION_KIND,
    expected_migration_hashes,
    expected_platform_migration_digest,
    expected_portal_migrations,
)
from research_operations.migrate import apply_migrations

ROOT = Path(__file__).resolve().parents[3]
OPERATIONS_ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_ENV = "RESEARCH_OPS_TEST_DATABASE_URL"
RELEASE_MANIFEST_ENV = "RESEARCH_OPS_CI_RELEASE_MANIFEST"
_SOURCE_DATABASE = re.compile(
    r"^(?:test_)?research_ops_(?:ci|test)(?:_[a-z0-9]{1,32})?$"
)
_TARGET_DATABASE = re.compile(r"^research_ops_restore_ci_[0-9a-f]{24}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_ARTIFACT_ROOTS = frozenset({"data", "artifact", "report", "cache"})
_DETERMINISTIC_SINGLE_THREAD_ENVIRONMENT = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "BLIS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)


def _database_url_for_name(dsn: str, database_name: str) -> str:
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError("postgresql_test_database_url_required")
    if not database_name or "/" in database_name or "\x00" in database_name:
        raise ValueError("database_name_invalid")
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            "/" + quote(database_name, safe=""),
            parsed.query,
            "",
        )
    )


def _source_database_name(dsn: str) -> str:
    database_name = str(conninfo_to_dict(dsn).get("dbname") or "")
    if not _SOURCE_DATABASE.fullmatch(database_name):
        raise ValueError("blank_restore_source_must_be_an_explicit_test_database")
    return database_name


def _active_django_source(
    configured_dsn: str,
    settings_dict: Mapping[str, Any],
) -> tuple[str, str]:
    """Bind the rehearsal to Django's active, explicitly isolated test DB.

    In a combined pytest process, pytest-django may already have created and
    selected ``test_<configured database>`` before this test starts.  The ORM
    must not write to that database while the backup scripts read the configured
    parent.  Only the configured database itself or pytest-django's exact test
    clone is accepted here; arbitrary database rebinding remains fail-closed.
    """

    configured_name = _source_database_name(configured_dsn)
    engine = str(settings_dict.get("ENGINE") or "")
    if engine != "django.db.backends.postgresql":
        raise ValueError("blank_restore_source_must_use_django_postgresql")
    active_name = str(settings_dict.get("NAME") or "")
    allowed_names = {configured_name, f"test_{configured_name}"}
    if active_name not in allowed_names:
        raise ValueError("blank_restore_django_database_identity_mismatch")
    _source_database_name(_database_url_for_name(configured_dsn, active_name))
    return _database_url_for_name(configured_dsn, active_name), active_name


def _validated_target_database_name(source_name: str, candidate: str) -> str:
    if candidate == source_name or not _TARGET_DATABASE.fullmatch(candidate):
        raise ValueError("blank_restore_target_database_name_invalid")
    return candidate


def _new_target_database_name(source_name: str) -> str:
    return _validated_target_database_name(
        source_name,
        f"research_ops_restore_ci_{uuid.uuid4().hex[:24]}",
    )


def _resolve_restored_ref(namespace: Path, value: str) -> Path:
    root_name, separator, relative = str(value or "").partition(":")
    relative_path = PurePosixPath(relative)
    if (
        not separator
        or root_name not in _ARTIFACT_ROOTS
        or not relative
        or relative_path.is_absolute()
        or any(part in {"", ".", ".."} for part in relative_path.parts)
        or "\\" in relative
    ):
        raise ValueError("restored_artifact_ref_invalid")
    root = (namespace / root_name).resolve(strict=True)
    candidate = root.joinpath(*relative_path.parts).resolve(strict=True)
    if not candidate.is_file() or not candidate.is_relative_to(root):
        raise ValueError("restored_artifact_ref_target_invalid")
    return candidate


def _run_checked(
    arguments: list[str],
    *,
    environment: dict[str, str],
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        arguments,
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    assert completed.returncode == 0, (
        f"command failed ({completed.returncode}): {arguments[0]}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    return completed


def _django_database_access(request: pytest.FixtureRequest):
    if not request.config.pluginmanager.hasplugin("django"):
        return nullcontext()
    return request.getfixturevalue("django_db_blocker").unblock()


def _release_inputs(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    result = {
        "git_sha": str(payload["git_sha"]),
        "release_id": str(payload["release_id"]),
        "build_digest": str(payload["build_digest"]),
        "release_bundle_digest": str(payload["release_bundle_digest"]),
        "migration_digest": str(payload["migration_digest"]),
    }
    assert _GIT_SHA.fullmatch(result["git_sha"])
    assert result["release_id"]
    assert _SHA256.fullmatch(result["build_digest"])
    assert _SHA256.fullmatch(result["release_bundle_digest"])
    assert result["migration_digest"] == expected_platform_migration_digest()
    checkout_sha = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert result["git_sha"] == checkout_sha
    return result


def _database_environment(
    base: dict[str, str],
    *,
    dsn: str,
    database_name: str,
) -> dict[str, str]:
    parameters = conninfo_to_dict(dsn)
    host = str(parameters.get("host") or "")
    user = str(parameters.get("user") or "")
    if not host or not user:
        raise ValueError("postgresql_test_connection_identity_required")
    environment = {
        **base,
        "RESEARCH_OPS_DATABASE_URL": dsn,
        "PGHOST": host,
        "PGPORT": str(parameters.get("port") or "5432"),
        "PGUSER": user,
        "PGPASSWORD": str(parameters.get("password") or ""),
        "PGDATABASE": database_name,
        "PGSSLMODE": str(parameters.get("sslmode") or "disable"),
        "INTERNAL_WEB_DATABASE_ENGINE": "postgresql",
        "INTERNAL_WEB_DATABASE_HOST": host,
        "INTERNAL_WEB_DATABASE_PORT": str(parameters.get("port") or "5432"),
        "INTERNAL_WEB_DATABASE_USER": user,
        "INTERNAL_WEB_DATABASE_PASSWORD": str(parameters.get("password") or ""),
        "INTERNAL_WEB_DATABASE_NAME": database_name,
        "INTERNAL_WEB_DATABASE_SSLMODE": str(parameters.get("sslmode") or "disable"),
    }
    return environment


def _create_blank_database(admin_dsn: str, source_name: str, target_name: str) -> None:
    _validated_target_database_name(source_name, target_name)
    created = False
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            target_exists = connection.execute(
                "SELECT count(*) FROM pg_database WHERE datname = %s",
                (target_name,),
            ).fetchone()[0]
            if target_exists:
                raise RuntimeError("blank_restore_target_already_exists")
            connection.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_name))
            )
            created = True
        target_dsn = _database_url_for_name(admin_dsn, target_name)
        with psycopg.connect(target_dsn) as connection:
            table_count = connection.execute(
                """
                SELECT count(*)
                FROM pg_tables
                WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
                """
            ).fetchone()[0]
        assert table_count == 0
    except Exception:
        if created:
            _drop_target_database(admin_dsn, source_name, target_name)
        raise


def _drop_target_database(admin_dsn: str, source_name: str, target_name: str) -> None:
    _validated_target_database_name(source_name, target_name)
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = %s AND pid <> pg_backend_pid()
            """,
            (target_name,),
        )
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(target_name))
        )


def _assert_pristine_source(dsn: str, source_name: str) -> None:
    with psycopg.connect(dsn) as connection:
        identity = connection.execute("SELECT current_database()").fetchone()[0]
        counts = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM auth_user),
              (SELECT count(*) FROM portal_manifestupload),
              (SELECT count(*) FROM portal_researchjob),
              (SELECT count(*) FROM portal_webauditevent),
              (SELECT count(*) FROM research_ops.experiment_request),
              (SELECT count(*) FROM research_ops.research_job_result_receipt),
              (SELECT count(*) FROM research_ops.backup_set),
              (SELECT count(*) FROM research_ops.restore_drill)
            """
        ).fetchone()
    assert identity == source_name
    assert counts == (0, 0, 0, 0, 0, 0, 0, 0), (
        "blank-restore rehearsal requires a pristine dedicated source database; "
        f"observed counts={counts}"
    )


def _force_source_open_and_remove_fixture(
    dsn: str,
    *,
    user_id: int | None,
    manifest_id: uuid.UUID | None,
    job_id: uuid.UUID | None,
    audit_id: uuid.UUID | None,
    experiment_id: str,
    request_id: str,
    backup_id: uuid.UUID | None,
    receipt_hash: str | None,
) -> None:
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            UPDATE research_ops.runtime_control
            SET mutation_admission_open = TRUE,
                claim_admission_open = TRUE,
                integrity_quarantine = FALSE,
                fence_token = NULL,
                requested_by = '', reason = '', closed_at = NULL,
                last_verified_manifest_hash = '',
                changed_at = CURRENT_TIMESTAMP
            WHERE singleton_id = 1
            """
        )
        if receipt_hash is not None:
            connection.execute(
                "DELETE FROM research_ops.restore_drill WHERE receipt_hash = %s",
                (receipt_hash,),
            )
        if backup_id is not None:
            connection.execute(
                "DELETE FROM research_ops.backup_set WHERE backup_id = %s",
                (backup_id,),
            )
        if job_id is not None:
            connection.execute(
                """
                DELETE FROM research_ops.research_job_result_receipt
                WHERE job_id = %s
                """,
                (job_id,),
            )
        connection.execute(
            """
            DELETE FROM research_ops.active_experiment_claim
            WHERE authority = 'market-research:experiment:v1'
              AND experiment_id = %s AND request_id = %s
            """,
            (experiment_id, request_id),
        )
        connection.execute(
            """
            DELETE FROM research_ops.experiment_request
            WHERE authority = 'market-research:experiment:v1'
              AND experiment_id = %s AND request_id = %s
            """,
            (experiment_id, request_id),
        )
        connection.execute(
            """
            DELETE FROM research_ops.experiment_identity
            WHERE authority = 'market-research:experiment:v1'
              AND experiment_id = %s
            """,
            (experiment_id,),
        )
        connection.execute(
            "DELETE FROM research_ops.validation_observation WHERE kind = %s",
            (AUDIT_OBSERVATION_KIND,),
        )
        if audit_id is not None:
            connection.execute(
                "DELETE FROM portal_webauditevent WHERE id = %s", (audit_id,)
            )
        if job_id is not None:
            connection.execute(
                "DELETE FROM portal_researchjob WHERE id = %s", (job_id,)
            )
        if manifest_id is not None:
            connection.execute(
                "DELETE FROM portal_manifestupload WHERE id = %s", (manifest_id,)
            )
        if user_id is not None:
            connection.execute(
                "DELETE FROM auth_user_groups WHERE user_id = %s", (user_id,)
            )
            connection.execute(
                "DELETE FROM auth_user_user_permissions WHERE user_id = %s", (user_id,)
            )
            connection.execute("DELETE FROM auth_user WHERE id = %s", (user_id,))


def _assert_report_hash(report: dict[str, object]) -> None:
    from market_research.application.adapter_contracts import (
        content_hash_payload,
        report_content_hash_payload,
        sha256_prefixed,
    )

    expected = str(report.get("content_hash") or "")
    without_hash = {
        key: value for key, value in report.items() if key != "content_hash"
    }
    assert expected in {
        sha256_prefixed(content_hash_payload(without_hash)),
        sha256_prefixed(report_content_hash_payload(report)),
    }


def _assert_restored_dataset(
    *,
    restored_manifest_payload: dict[str, object],
    source_data_root: Path,
    restore_namespace: Path,
) -> None:
    from market_research.research.dataset_freeze import sqlite_candles_schema_hash
    from market_research.research.datasets.artifact_manifest import (
        parse_artifact_manifest,
    )
    from market_research.research.datasets.hashing_contract import (
        artifact_content_hash,
    )

    dataset = restored_manifest_payload["dataset"]
    assert isinstance(dataset, dict)
    source_sidecar = Path(str(dataset["artifact_manifest_uri"])).resolve()
    relative_sidecar = source_sidecar.relative_to(source_data_root.resolve())
    restored_sidecar = (restore_namespace / "data" / relative_sidecar).resolve()
    assert restored_sidecar != source_sidecar
    assert restored_sidecar.is_file()
    sidecar_payload = json.loads(restored_sidecar.read_text(encoding="utf-8"))
    artifact = parse_artifact_manifest(sidecar_payload)
    assert artifact.artifact_manifest_hash == dataset["artifact_manifest_hash"]

    source_database = Path(artifact.locator.path).resolve()
    relative_database = source_database.relative_to(source_data_root.resolve())
    restored_database = (restore_namespace / "data" / relative_database).resolve()
    assert restored_database != source_database
    assert restored_database.is_file()
    with sqlite3.connect(f"file:{restored_database}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            """
            SELECT pair, interval, ts, open, high, low, close, volume
            FROM candles ORDER BY pair, interval, ts
            """
        ).fetchall()
    assert len(rows) == artifact.row_count
    assert artifact_content_hash(rows) == artifact.content_hash
    assert sqlite_candles_schema_hash(restored_database) == artifact.schema_hash


def test_restore_ref_resolution_is_confined_to_the_new_namespace(
    tmp_path: Path,
) -> None:
    namespace = tmp_path / "restore"
    report = namespace / "report" / "research" / "report.json"
    report.parent.mkdir(parents=True)
    report.write_text("{}", encoding="utf-8")
    assert (
        _resolve_restored_ref(namespace, "report:research/report.json")
        == report.resolve()
    )
    for invalid in (
        "report:../source/report.json",
        "report:/absolute/report.json",
        "unknown:report.json",
        "report:research\\report.json",
    ):
        with pytest.raises(ValueError, match="restored_artifact_ref"):
            _resolve_restored_ref(namespace, invalid)


def test_restore_database_name_guard_rejects_source_and_broad_targets() -> None:
    source = "research_ops_ci"
    valid = "research_ops_restore_ci_0123456789abcdef01234567"
    assert _validated_target_database_name(source, valid) == valid
    invalid_names = (
        source,
        "postgres",
        "research_ops_restore_ci_",
        "research_ops_ci_2",
    )
    for invalid in invalid_names:
        with pytest.raises(ValueError, match="target_database_name_invalid"):
            _validated_target_database_name(source, invalid)


def test_database_url_rebind_preserves_connection_authority_and_policy() -> None:
    source = (
        "postgresql://postgres:test-password@127.0.0.1:5432/research_ops_ci"
        "?sslmode=disable&connect_timeout=5"
    )
    target = "research_ops_restore_ci_0123456789abcdef01234567"
    rebound = _database_url_for_name(source, target)
    parsed = urlsplit(rebound)
    assert parsed.netloc == "postgres:test-password@127.0.0.1:5432"
    assert parsed.path == f"/{target}"
    assert parsed.query == "sslmode=disable&connect_timeout=5"
    assert _source_database_name(source) == "research_ops_ci"


def test_operations_ci_builds_exact_release_before_the_zero_skip_restore() -> None:
    workflow = (ROOT / ".github" / "workflows" / "research-ci.yml").read_text(
        encoding="utf-8"
    )
    assert "RESEARCH_OPS_CI_RELEASE_MANIFEST:" in workflow
    assert "Build exact release inputs for the blank-restore rehearsal" in workflow
    assert "scripts/platform build" in workflow
    assert "tools/release_manifest.py" in workflow
    assert 'PYTHONHASHSEED: "0"' in workflow
    for name in _DETERMINISTIC_SINGLE_THREAD_ENVIRONMENT:
        assert f'{name}: "1"' in workflow
    assert "Run the complete Operations suite with zero skips" in workflow


@pytest.mark.postgresql
def test_ci_performs_signed_blank_restore_with_research_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    django_db_setup: None,
) -> None:
    configured_source_dsn = os.environ.get(TEST_DATABASE_ENV, "").strip()
    if not configured_source_dsn:
        pytest.skip(f"{TEST_DATABASE_ENV} is not configured")
    release_path_raw = os.environ.get(RELEASE_MANIFEST_ENV, "").strip()
    assert release_path_raw, f"{RELEASE_MANIFEST_ENV} is required with a live test DB"
    release_path = Path(release_path_raw)
    assert release_path.is_absolute() and release_path.is_file()
    release = _release_inputs(release_path)
    assert os.environ.get("PYTHONHASHSEED") == "0"
    for name in _DETERMINISTIC_SINGLE_THREAD_ENVIRONMENT:
        assert os.environ.get(name) == "1"

    configured_source_name = _source_database_name(configured_source_dsn)
    target_created = False
    backup_id: uuid.UUID | None = None
    receipt_hash: str | None = None
    user_id: int | None = None
    manifest_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    audit_id: uuid.UUID | None = None
    experiment_id = "sma_success_import_boundary"
    request_id = ""

    parameters = conninfo_to_dict(configured_source_dsn)
    for key, value in {
        "DJANGO_SETTINGS_MODULE": "market_research_web.settings_test",
        "INTERNAL_WEB_SECRET_KEY": "ci-blank-restore-secret-0123456789abcdef",
        "INTERNAL_WEB_DATABASE_ENGINE": "postgresql",
        "INTERNAL_WEB_DATABASE_HOST": str(parameters.get("host") or ""),
        "INTERNAL_WEB_DATABASE_PORT": str(parameters.get("port") or "5432"),
        "INTERNAL_WEB_DATABASE_USER": str(parameters.get("user") or ""),
        "INTERNAL_WEB_DATABASE_PASSWORD": str(parameters.get("password") or ""),
        "INTERNAL_WEB_DATABASE_NAME": configured_source_name,
        "INTERNAL_WEB_DATABASE_SSLMODE": str(parameters.get("sslmode") or "disable"),
    }.items():
        monkeypatch.setenv(key, value)

    import django

    django.setup()
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group
    from django.core.management import call_command
    from django.db import connection as django_connection
    from django.db import transaction
    from django.test import override_settings
    from django.utils import timezone as django_timezone
    from market_research.application.adapter_contracts import (
        content_hash_payload,
        sha256_prefixed,
    )
    from market_research.paths import ResearchPathManager
    from market_research.research.experiment_identity import (
        bind_research_validation_experiment,
    )
    from market_research.research.experiment_manifest import load_manifest
    from market_research.research.reproduction import load_reproduction_receipt
    from market_research.research.validation_protocol import run_research_backtest
    from market_research.research_composition import builtin_strategy_registry
    from market_research.settings import ResearchSettings
    from portal.audit import record_web_audit_event, validate_web_audit_outbox
    from portal.models import ManifestUpload, ResearchJob
    from portal.storage import (
        make_artifact_ref,
        publish_manifest_bytes,
        verify_result_artifact,
    )
    from tests.clean_provenance_fixture import install_committed_checkout_provenance
    from tests.research_sma_success_fixture import create_success_fixture

    source_dsn, source_name = _active_django_source(
        configured_source_dsn,
        django_connection.settings_dict,
    )
    monkeypatch.setenv("INTERNAL_WEB_DATABASE_NAME", source_name)
    admin_dsn = _database_url_for_name(source_dsn, "postgres")
    target_name = _new_target_database_name(source_name)
    target_dsn = _database_url_for_name(source_dsn, target_name)
    assert django_connection.settings_dict["NAME"] == source_name
    with _django_database_access(request):
        call_command("migrate", interactive=False, verbosity=0)
    apply_migrations(source_dsn)
    _assert_pristine_source(source_dsn, source_name)

    source_namespace = tmp_path / "source-state"
    source_settings = ResearchSettings(
        data_root=source_namespace / "data",
        artifact_root=source_namespace / "artifact",
        report_root=source_namespace / "report",
        cache_root=source_namespace / "cache",
        db_path=None,
        max_workers=1,
        random_seed=0,
        experiment_identity_registry_path=(
            source_namespace / "identity_registry" / "experiment_identity.jsonl"
        ),
    )
    manager = ResearchPathManager.from_settings(source_settings, project_root=ROOT)
    manager.ensure_roots()
    source_settings.experiment_identity_registry_path.parent.mkdir(
        parents=True, mode=0o700
    )
    fixture_root = manager.dataset_path("prepared_sma_fixture")
    fixture_root.mkdir(parents=True)
    database_path, manifest_path = create_success_fixture(fixture_root)
    registry = builtin_strategy_registry()
    manifest = load_manifest(manifest_path, registry=registry)

    # The CI release is built from the exact committed checkout even when this
    # focused rehearsal is executed from a developer worktree containing the
    # patch under test. Dirty-source rejection has separate contract tests.
    install_committed_checkout_provenance(monkeypatch)
    report = run_research_backtest(
        manifest=manifest,
        db_path=database_path,
        manager=manager,
        manifest_path=str(manifest_path),
        strategy_registry=registry,
    )
    report_path = manager.report_path("research", experiment_id, "backtest_report.json")
    assert report_path.is_file()
    source_reproduction = load_reproduction_receipt(
        str(report["reproduction_receipt_path"])
    )

    manifest_content = manifest_path.read_bytes()
    manifest_content_hash = "sha256:" + hashlib.sha256(manifest_content).hexdigest()
    audit_path = manager.artifact_path("_internal_web", "audit", "web_audit.jsonl")
    manifest_root = manager.dataset_path("_internal_web", "manifests")

    try:
        with (
            _django_database_access(request),
            override_settings(
                RESEARCH_PATHS=manager,
                INTERNAL_WEB_MANIFEST_ROOT=manifest_root,
                INTERNAL_WEB_AUDIT_PATH=audit_path,
                INTERNAL_WEB_AUDIT_SEGMENT_ROWS=4,
            ),
        ):
            user = get_user_model().objects.create_user(
                username=f"ci-restore-{uuid.uuid4().hex}",
                password=None,
            )
            user_id = int(user.pk)
            user.groups.add(Group.objects.get(name="research_runner"))
            published_manifest = publish_manifest_bytes(
                content=manifest_content,
                content_hash=manifest_content_hash,
            )
            manifest_record = ManifestUpload.objects.create(
                owner=user,
                display_name="ci-blank-restore-manifest.json",
                storage_ref=str(published_manifest),
                content_hash=manifest_content_hash,
                manifest_hash=manifest.manifest_hash(),
                size_bytes=len(manifest_content),
                experiment_id=experiment_id,
                strategy_name=manifest.strategy_name,
            )
            manifest_id = manifest_record.pk
            report_ref = str(make_artifact_ref("report", report_path))
            verified_report = verify_result_artifact(
                report_ref,
                expected_hash=str(report["content_hash"]),
            )
            request_payload = {
                "schema_version": 1,
                "capability_id": ResearchJob.Capability.PREFLIGHT,
                "manifest_id": str(manifest_record.pk),
                "manifest_hash": manifest_record.manifest_hash,
            }
            request_hash = sha256_prefixed(content_hash_payload(request_payload))
            # This is deliberately non-promotional evidence. Recovery verifies
            # the real research-only backtest as a preflight artifact without
            # interpreting it as a reviewed research-validate decision report.
            job = ResearchJob.objects.create(
                owner=user,
                manifest=manifest_record,
                capability_id=ResearchJob.Capability.PREFLIGHT,
                status=ResearchJob.Status.SUCCEEDED,
                request_payload=request_payload,
                request_hash=request_hash,
                idempotency_key=uuid.uuid4().hex,
                actor_id=user.username,
                actor_roles=["research_runner"],
                actor_permissions=["portal.submit_research_job"],
                run_id=str(report["run_id"]),
                result_ref=report_ref,
                result_hash=str(report["content_hash"]),
                research_outcome=ResearchJob.ResearchOutcome.PASS,
                progress_stage="complete",
                finished_at=django_timezone.now(),
            )
            job_id = job.pk
            request_id = f"web-job:{job.pk}"

            admission_store = ExperimentAdmissionStore(source_dsn)
            admission = admission_store.acquire(
                authority="market-research:experiment:v1",
                experiment_id=experiment_id,
                manifest_hash=manifest_record.manifest_hash,
                request_id=request_id,
                request_hash=request_hash,
                owner_id=str(user.pk),
                lease_seconds=60,
            )
            assert admission.acquired
            admission_store.complete_research_job(
                admission,
                job_id=job.pk,
                result_ref=report_ref,
                result_hash=str(report["content_hash"]),
                research_outcome="PASS",
                core_run_id=str(report["run_id"]),
            )
            admission_store.mark_research_job_receipt_applied(
                job_id=job.pk,
                result_hash=str(report["content_hash"]),
            )
            bind_research_validation_experiment(
                manager=manager,
                experiment_id=experiment_id,
                manifest_hash=manifest_record.manifest_hash,
            )
            with transaction.atomic():
                audit_event = record_web_audit_event(
                    action="ci_blank_restore_fixture_created",
                    actor_id=user.username,
                    object_type="research_job",
                    object_id=str(job.pk),
                    correlation_id=str(job.correlation_id),
                    details={
                        "capability_id": job.capability_id,
                        "research_report_hash": str(report["content_hash"]),
                        "reproduction_receipt_hash": str(
                            source_reproduction["receipt_content_hash"]
                        ),
                    },
                )
                audit_id = audit_event.pk
            audit = validate_web_audit_outbox()
            assert audit["status"] == "PASS", audit
            assert audit["row_count"] == 1
            assert audit["projected_event_count"] == 1
            assert verified_report["manifest_hash"] == manifest_record.manifest_hash

        django_connection.close()
        key_root = tmp_path / "keys"
        key_root.mkdir(mode=0o700)
        private_key = key_root / "backup-signing.pem"
        public_key = key_root / "backup-verification.pem"
        _run_checked(
            [
                shutil.which("openssl") or "/usr/bin/openssl",
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
                "-out",
                str(private_key),
            ],
            environment=os.environ.copy(),
        )
        private_key.chmod(0o600)
        _run_checked(
            [
                shutil.which("openssl") or "/usr/bin/openssl",
                "pkey",
                "-in",
                str(private_key),
                "-pubout",
                "-out",
                str(public_key),
            ],
            environment=os.environ.copy(),
        )
        public_key.chmod(0o644)

        backup_root = tmp_path / "backup-sets"
        runtime_root = tmp_path / "backup-runtime"
        receipt_root = tmp_path / "recovery-receipts"
        for path in (backup_root, runtime_root, receipt_root):
            path.mkdir(mode=0o700)
        control_secret = key_root / "control-database-url"
        control_secret.write_text(source_dsn + "\n", encoding="utf-8")
        control_secret.chmod(0o600)

        executable_root = str(Path(sys.executable).parent)
        assert (Path(executable_root) / "research-ops").is_file()
        base_environment = {
            **os.environ,
            "PATH": executable_root + os.pathsep + os.environ.get("PATH", ""),
            "DJANGO_SETTINGS_MODULE": "market_research_web.settings",
            "INTERNAL_WEB_SECRET_KEY": "ci-blank-restore-secret-0123456789abcdef",
            "INTERNAL_WEB_AUDIT_SEGMENT_ROWS": "4",
            "INTERNAL_WEB_SECURE_COOKIES": "false",
            "INTERNAL_WEB_SECURE_SSL_REDIRECT": "false",
            "RESEARCH_OPS_SOURCE_ROOT": str(ROOT),
            "RESEARCH_DATA_ROOT": str(manager.data_root),
            "RESEARCH_ARTIFACT_ROOT": str(manager.artifact_root),
            "RESEARCH_REPORT_ROOT": str(manager.report_root),
            "RESEARCH_CACHE_ROOT": str(manager.cache_root),
            "RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH": str(
                source_settings.experiment_identity_registry_path
            ),
            "RESEARCH_OPS_BACKUP_SIGNING_KEY_FILE": str(private_key),
            "RESEARCH_OPS_BACKUP_VERIFICATION_KEY_FILE": str(public_key),
            "RESEARCH_OPS_CONTROL_DATABASE_URL_FILE": str(control_secret),
            "RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY": str(runtime_root),
            "BACKUP_ROOT": str(backup_root),
            "BACKUP_OPERATOR_ID": "ci-blank-restore",
            "POSTGRES_MAJOR": "16",
            "RESEARCH_OPS_GIT_SHA": release["git_sha"],
            "RESEARCH_OPS_RELEASE_ID": release["release_id"],
            "RESEARCH_OPS_BUILD_DIGEST": release["build_digest"],
            "RESEARCH_OPS_RELEASE_BUNDLE_DIGEST": release["release_bundle_digest"],
        }
        source_environment = _database_environment(
            base_environment,
            dsn=source_dsn,
            database_name=source_name,
        )
        created = _run_checked(
            ["/bin/sh", str(OPERATIONS_ROOT / "scripts" / "create-backup.sh")],
            environment=source_environment,
        )
        output_lines = [line for line in created.stdout.splitlines() if line.strip()]
        assert output_lines
        backup_path = Path(output_lines[-1]).resolve(strict=True)
        assert backup_path.parent == backup_root.resolve()
        backup_id = uuid.UUID(backup_path.name)

        backup_manifest = json.loads(
            (backup_path / "manifest.json").read_text(encoding="utf-8")
        )
        for required_file in (
            "postgresql.dump",
            "data.tar",
            "manifest.tar",
            "artifact.tar",
            "report.tar",
            "identity_registry.tar",
            "manifest.json",
            "manifest.sig",
        ):
            assert (backup_path / required_file).is_file()
        assert {
            key: backup_manifest[key]
            for key in (
                "git_sha",
                "release_id",
                "build_digest",
                "release_bundle_digest",
            )
        } == {
            "git_sha": release["git_sha"],
            "release_id": release["release_id"],
            "build_digest": release["build_digest"],
            "release_bundle_digest": release["release_bundle_digest"],
        }
        assert backup_manifest["migration_digest"] == release["migration_digest"]
        assert backup_manifest["audit"]["row_count"] == 1
        assert backup_manifest["audit"]["segmented_stream_required"] is True

        _create_blank_database(admin_dsn, source_name, target_name)
        target_created = True
        restore_namespace = tmp_path / "isolated-restore"
        receipt_path = receipt_root / "blank-restore-receipt.json"
        target_environment = _database_environment(
            base_environment,
            dsn=target_dsn,
            database_name=target_name,
        )
        target_environment["RESEARCH_OPS_RECOVERY_DATABASE_NAME"] = target_name
        restored = _run_checked(
            [
                "/bin/sh",
                str(OPERATIONS_ROOT / "scripts" / "restore-rehearsal.sh"),
                str(backup_path),
                str(restore_namespace),
                str(receipt_path),
            ],
            environment=target_environment,
        )
        assert restored.stdout.strip()
        assert restore_namespace.resolve() != source_namespace.resolve()
        receipt_hash, recovery, receipt_document = verify_signed_recovery_receipt(
            verification=None,
            receipt_path=receipt_path,
            verification_public_key=public_key,
        )
        assert recovery.status == "PASS"
        assert recovery.duration_seconds >= 0
        assert receipt_path.with_suffix(receipt_path.suffix + ".sig").is_file()
        assert receipt_document["release"] == {
            "git_sha": release["git_sha"],
            "release_id": release["release_id"],
            "build_digest": release["build_digest"],
            "release_bundle_digest": release["release_bundle_digest"],
        }
        assert receipt_document["checks"]
        assert {item["status"] for item in receipt_document["checks"]} == {"PASS"}

        with psycopg.connect(source_dsn) as control_connection:
            control_drill = control_connection.execute(
                """
                SELECT status, duration_seconds, backup_manifest_hash, receipt_hash
                FROM research_ops.restore_drill WHERE receipt_hash = %s
                """,
                (receipt_hash,),
            ).fetchone()
        assert control_drill is not None
        assert control_drill[0] == "PASS"
        assert float(control_drill[1]) == pytest.approx(recovery.duration_seconds)
        assert control_drill[2:] == (
            recovery.backup_manifest_hash,
            receipt_hash,
        )

        with psycopg.connect(target_dsn) as connection:
            restored_database_identity = connection.execute(
                """
                SELECT current_database(),
                       current_setting('default_transaction_read_only')
                """
            ).fetchone()
            restored_control = connection.execute(
                """
                SELECT mutation_admission_open, claim_admission_open,
                       integrity_quarantine, fence_token
                FROM research_ops.runtime_control WHERE singleton_id = 1
                """
            ).fetchone()
            restored_runner_membership = connection.execute(
                """
                SELECT count(*)
                FROM auth_user_groups AS membership
                JOIN auth_group AS role ON role.id = membership.group_id
                WHERE membership.user_id = %s AND role.name = 'research_runner'
                """,
                (user_id,),
            ).fetchone()[0]
            operations_migrations = dict(
                connection.execute(
                    """
                    SELECT name, content_hash
                    FROM research_ops.migration_history ORDER BY name
                    """
                ).fetchall()
            )
            portal_migrations = tuple(
                row[0]
                for row in connection.execute(
                    """
                    SELECT name FROM django_migrations
                    WHERE app = 'portal' ORDER BY name
                    """
                ).fetchall()
            )
            restored_record = connection.execute(
                """
                SELECT manifest.storage_ref, manifest.content_hash,
                       manifest.manifest_hash, job.result_ref, job.result_hash,
                       job.request_hash, receipt.request_hash,
                       receipt.result_ref, receipt.result_hash,
                       receipt.research_outcome, receipt.applied_at,
                       manifest.experiment_id, job.status, job.capability_id
                FROM portal_manifestupload AS manifest
                JOIN portal_researchjob AS job ON job.manifest_id = manifest.id
                JOIN research_ops.research_job_result_receipt AS receipt
                  ON receipt.job_id = job.id
                WHERE job.id = %s
                """,
                (job_id,),
            ).fetchone()
            restored_audit = connection.execute(
                """
                SELECT payload ->> 'action', projection_row_hash, projected_at,
                       payload #>> '{details,research_report_hash}',
                       payload #>> '{details,reproduction_receipt_hash}'
                FROM portal_webauditevent WHERE id = %s
                """,
                (audit_id,),
            ).fetchone()
        assert restored_database_identity == (target_name, "on")
        assert target_name != source_name
        assert restored_control is not None
        assert restored_control[:3] == (False, False, False)
        assert restored_control[3] is not None
        assert restored_runner_membership == 1
        assert operations_migrations == expected_migration_hashes()
        assert portal_migrations == expected_portal_migrations()
        assert restored_record is not None
        assert restored_record[5] == restored_record[6]
        assert restored_record[3:5] == restored_record[7:9]
        assert restored_record[9] == "PASS"
        assert restored_record[10] is not None
        assert restored_record[11] == experiment_id
        assert restored_record[12:] == ("SUCCEEDED", "research-preflight")
        assert restored_audit is not None
        assert restored_audit[0] == "ci_blank_restore_fixture_created"
        assert restored_audit[1].startswith("sha256:")
        assert restored_audit[2] is not None
        assert restored_audit[3] == restored_record[4]
        assert restored_audit[4] == source_reproduction["receipt_content_hash"]

        restored_manifest_path = _resolve_restored_ref(
            restore_namespace, str(restored_record[0])
        )
        restored_report_path = _resolve_restored_ref(
            restore_namespace, str(restored_record[3])
        )
        restored_manifest_content = restored_manifest_path.read_bytes()
        assert (
            "sha256:" + hashlib.sha256(restored_manifest_content).hexdigest()
            == restored_record[1]
        )
        restored_manifest_payload = json.loads(
            restored_manifest_content.decode("utf-8")
        )
        restored_manifest = load_manifest(restored_manifest_path, registry=registry)
        assert restored_manifest.manifest_hash() == restored_record[2]
        restored_report = json.loads(restored_report_path.read_text(encoding="utf-8"))
        _assert_report_hash(restored_report)
        assert restored_report["content_hash"] == restored_record[4]

        # The immutable report cannot embed its receipt path/hash without a
        # circular content-hash dependency. The authoritative receipt is the
        # canonical sibling produced after the report is published.
        source_receipt_path = Path(str(report["reproduction_receipt_path"])).resolve()
        receipt_relative_path = source_receipt_path.relative_to(
            manager.report_root.resolve()
        )
        restored_receipt_path = (
            restore_namespace / "report" / receipt_relative_path
        ).resolve()
        assert restored_receipt_path != source_receipt_path
        assert restored_receipt_path.is_file()
        restored_reproduction = load_reproduction_receipt(restored_receipt_path)
        assert (
            restored_reproduction["receipt_content_hash"]
            == source_reproduction["receipt_content_hash"]
        )
        assert restored_reproduction["source_report_hash"] == restored_record[4]
        assert restored_reproduction["manifest_hash"] == restored_record[2]
        assert (
            restored_reproduction["stable_fingerprint_hash"]
            == source_reproduction["stable_fingerprint_hash"]
        )
        _assert_restored_dataset(
            restored_manifest_payload=restored_manifest_payload,
            source_data_root=manager.data_root,
            restore_namespace=restore_namespace,
        )
        restored_identity = (
            restore_namespace / "identity_registry" / "experiment_identity.jsonl"
        )
        assert restored_identity.is_file()
        assert manifest.manifest_hash() in restored_identity.read_text(encoding="utf-8")
    finally:
        if (
            receipt_hash is None
            and "receipt_path" in locals()
            and receipt_path.is_file()
        ):
            receipt_hash = (
                "sha256:" + hashlib.sha256(receipt_path.read_bytes()).hexdigest()
            )
        try:
            if target_created:
                _drop_target_database(admin_dsn, source_name, target_name)
        finally:
            _force_source_open_and_remove_fixture(
                source_dsn,
                user_id=user_id,
                manifest_id=manifest_id,
                job_id=job_id,
                audit_id=audit_id,
                experiment_id=experiment_id,
                request_id=request_id,
                backup_id=backup_id,
                receipt_hash=receipt_hash,
            )
