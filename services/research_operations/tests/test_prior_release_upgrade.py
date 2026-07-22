from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from research_operations.admission import ExperimentAdmissionStore
from research_operations.migrate import apply_migrations
from research_operations.outbox import PROJECTED, OutboxStore

ROOT = Path(__file__).resolve().parents[3]
TEST_DATABASE_ENV = "RESEARCH_OPS_TEST_DATABASE_URL"
_SOURCE_DATABASE = re.compile(r"^research_ops_(?:ci|test)(?:_[a-z0-9]{1,32})?$")
_UPGRADE_DATABASE = re.compile(r"^research_ops_upgrade_ci_[0-9a-f]{24}$")
_PRIOR_PORTAL_MIGRATION = "0007_governance_authority"
_PRIOR_OPERATIONS_MIGRATIONS = (
    "0001_initial.sql",
    "0002_runtime_control.sql",
    "0003_research_job_receipt.sql",
    "0004_worker_release_provenance.sql",
)


def _database_name(dsn: str) -> str:
    name = str(conninfo_to_dict(dsn).get("dbname") or "")
    if not _SOURCE_DATABASE.fullmatch(name):
        raise ValueError("prior_upgrade_source_must_be_an_explicit_test_database")
    return name


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


def _validated_upgrade_database_name(source_name: str, candidate: str) -> str:
    if candidate == source_name or not _UPGRADE_DATABASE.fullmatch(candidate):
        raise ValueError("prior_upgrade_target_database_name_invalid")
    return candidate


def _new_upgrade_database_name(source_name: str) -> str:
    return _validated_upgrade_database_name(
        source_name,
        f"research_ops_upgrade_ci_{uuid.uuid4().hex[:24]}",
    )


def _create_pristine_database(
    admin_dsn: str,
    source_name: str,
    target_name: str,
) -> None:
    _validated_upgrade_database_name(source_name, target_name)
    created = False
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            exists = connection.execute(
                "SELECT count(*) FROM pg_database WHERE datname = %s",
                (target_name,),
            ).fetchone()[0]
            if exists:
                raise RuntimeError("prior_upgrade_target_already_exists")
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
            _drop_upgrade_database(admin_dsn, source_name, target_name)
        raise


def _drop_upgrade_database(
    admin_dsn: str,
    source_name: str,
    target_name: str,
) -> None:
    _validated_upgrade_database_name(source_name, target_name)
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


def _web_environment(
    *,
    dsn: str,
    database_name: str,
    state_root: Path,
) -> dict[str, str]:
    parameters = conninfo_to_dict(dsn)
    host = str(parameters.get("host") or "")
    user = str(parameters.get("user") or "")
    if not host or not user:
        raise ValueError("postgresql_test_connection_identity_required")
    roots = {
        "RESEARCH_DATA_ROOT": state_root / "datasets",
        "RESEARCH_ARTIFACT_ROOT": state_root / "artifacts",
        "RESEARCH_REPORT_ROOT": state_root / "reports",
        "RESEARCH_CACHE_ROOT": state_root / "cache",
    }
    return {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "market_research_web.settings_test",
        "INTERNAL_WEB_SECRET_KEY": "prior-upgrade-test-secret-0123456789abcdef",
        "INTERNAL_WEB_SECURE_SSL_REDIRECT": "false",
        "INTERNAL_WEB_DATABASE_ENGINE": "postgresql",
        "INTERNAL_WEB_DATABASE_HOST": host,
        "INTERNAL_WEB_DATABASE_PORT": str(parameters.get("port") or "5432"),
        "INTERNAL_WEB_DATABASE_USER": user,
        "INTERNAL_WEB_DATABASE_PASSWORD": str(parameters.get("password") or ""),
        "INTERNAL_WEB_DATABASE_NAME": database_name,
        "INTERNAL_WEB_DATABASE_SSLMODE": str(parameters.get("sslmode") or "disable"),
        "INTERNAL_WEB_DATABASE_CONN_MAX_AGE_SECONDS": "0",
        "RESEARCH_OPS_SOURCE_ROOT": str(ROOT),
        **{name: str(path) for name, path in roots.items()},
    }


def _run_web_migrations(
    environment: dict[str, str],
    *,
    target: str | None = None,
) -> None:
    arguments = [
        sys.executable,
        str(ROOT / "apps" / "internal_web" / "manage.py"),
        "migrate",
    ]
    if target is not None:
        arguments.extend(("portal", target))
    arguments.extend(
        ("--noinput", "--verbosity=0", "--settings=market_research_web.settings_test")
    )
    completed = subprocess.run(
        arguments,
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, (
        f"Web migration command failed ({completed.returncode})\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )


def _operations_migration_files() -> tuple[Traversable, ...]:
    root = resources.files("research_operations.migrations")
    return tuple(sorted(item for item in root.iterdir() if item.name.endswith(".sql")))


def _install_prior_operations_release(dsn: str) -> dict[str, str]:
    migration_files = _operations_migration_files()
    names = tuple(item.name for item in migration_files)
    assert names[: len(_PRIOR_OPERATIONS_MIGRATIONS)] == _PRIOR_OPERATIONS_MIGRATIONS
    prior_files = migration_files[: len(_PRIOR_OPERATIONS_MIGRATIONS)]
    installed: dict[str, str] = {}
    with psycopg.connect(dsn) as connection:
        connection.execute("CREATE SCHEMA research_ops")
        connection.execute(
            """
            CREATE TABLE research_ops.migration_history (
                name varchar(255) PRIMARY KEY,
                content_hash varchar(64) NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.commit()
        for migration_file in prior_files:
            payload = migration_file.read_bytes()
            content_hash = hashlib.sha256(payload).hexdigest()
            connection.execute(payload.decode("utf-8"))
            connection.execute(
                """
                INSERT INTO research_ops.migration_history(name, content_hash)
                VALUES (%s, %s)
                """,
                (migration_file.name, content_hash),
            )
            connection.commit()
            installed[migration_file.name] = content_hash
    return installed


def _seed_prior_release_state(dsn: str) -> dict[str, object]:
    observed_at = datetime.now(UTC) - timedelta(minutes=5)
    event_id = uuid.uuid4()
    run_id = uuid.uuid4()
    backup_id = uuid.uuid4()
    fence_token = uuid.uuid4()
    payload = {
        "event_id": str(event_id),
        "actor_id": "prior-release-fixture",
        "action": "prior_release_audit_preserved",
    }
    payload_hash = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    manifest_hash = "sha256:" + "a" * 64
    request_hash = "sha256:" + "b" * 64
    result_hash = "sha256:" + "c" * 64
    backup_manifest_hash = "sha256:" + "d" * 64
    git_sha = "1" * 40
    build_digest = "sha256:" + "2" * 64
    bundle_digest = "sha256:" + "3" * 64
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO public.portal_webauditevent (
                id, payload, payload_hash, projection_row_hash,
                projected_at, created_at
            ) VALUES (%s, %s, %s, '', NULL, %s)
            """,
            (event_id, Jsonb(payload), payload_hash, observed_at),
        )
        connection.execute(
            """
            INSERT INTO research_ops.outbox_delivery (
                event_id, event_type, payload_hash, idempotency_key,
                created_at, status, available_at
            ) VALUES (%s, 'internal_web_audit', %s, %s, %s, 'PENDING', %s)
            """,
            (
                event_id,
                payload_hash,
                f"internal_web_audit:{event_id}",
                observed_at,
                observed_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO research_ops.experiment_identity (
                authority, experiment_id, manifest_hash, fencing_counter,
                created_at, updated_at
            ) VALUES (
                'market-research:experiment:v1', 'prior-release-study',
                %s, 1, %s, %s
            )
            """,
            (manifest_hash, observed_at, observed_at),
        )
        connection.execute(
            """
            INSERT INTO research_ops.experiment_request (
                authority, experiment_id, request_id, request_hash,
                owner_id, run_id, status, result_ref, result_hash,
                created_at, started_at, finished_at, updated_at
            ) VALUES (
                'market-research:experiment:v1', 'prior-release-study',
                'prior-release-request', %s, 'prior-release-owner', %s,
                'SUCCEEDED', 'report:prior-release/result.json', %s,
                %s, %s, %s, %s
            )
            """,
            (
                request_hash,
                run_id,
                result_hash,
                observed_at,
                observed_at,
                observed_at,
                observed_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO research_ops.worker_heartbeat (
                worker_id, process_id, state, started_at, last_seen_at, stopped_at,
                git_sha, release_id, build_digest, release_bundle_digest,
                release_seen_at
            ) VALUES (
                'prior-release-worker', 1, 'STOPPED', %s, %s, %s,
                %s, 'prior-release', %s, %s, %s
            )
            """,
            (
                observed_at,
                observed_at,
                observed_at,
                git_sha,
                build_digest,
                bundle_digest,
                observed_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO research_ops.backup_set (
                backup_id, manifest_hash, fence_token, fence_generation,
                release_id, created_at, verified_at, git_sha, build_digest,
                release_bundle_digest
            ) VALUES (
                %s, %s, %s, 1, 'prior-release', %s, %s, %s, %s, %s
            )
            """,
            (
                backup_id,
                backup_manifest_hash,
                fence_token,
                observed_at,
                observed_at,
                git_sha,
                build_digest,
                bundle_digest,
            ),
        )
    return {
        "event_id": event_id,
        "payload_hash": payload_hash,
        "run_id": run_id,
        "manifest_hash": manifest_hash,
        "request_hash": request_hash,
        "result_hash": result_hash,
        "backup_id": backup_id,
        "backup_manifest_hash": backup_manifest_hash,
        "git_sha": git_sha,
        "build_digest": build_digest,
        "bundle_digest": bundle_digest,
    }


def _prior_state_snapshot(dsn: str) -> tuple[object, ...]:
    with psycopg.connect(dsn) as connection:
        return (
            connection.execute(
                """
                SELECT payload_hash, projection_row_hash, projected_at
                FROM public.portal_webauditevent
                WHERE payload ->> 'actor_id' = 'prior-release-fixture'
                """
            ).fetchone(),
            connection.execute(
                """
                SELECT event_id, payload_hash, status, attempt_count
                FROM research_ops.outbox_delivery
                WHERE idempotency_key LIKE 'internal_web_audit:%'
                """
            ).fetchone(),
            connection.execute(
                """
                SELECT manifest_hash, fencing_counter
                FROM research_ops.experiment_identity
                WHERE authority = 'market-research:experiment:v1'
                  AND experiment_id = 'prior-release-study'
                """
            ).fetchone(),
            connection.execute(
                """
                SELECT request_hash, run_id, status, result_ref, result_hash
                FROM research_ops.experiment_request
                WHERE authority = 'market-research:experiment:v1'
                  AND experiment_id = 'prior-release-study'
                  AND request_id = 'prior-release-request'
                """
            ).fetchone(),
            connection.execute(
                """
                SELECT state, git_sha, release_id, build_digest,
                       release_bundle_digest
                FROM research_ops.worker_heartbeat
                WHERE worker_id = 'prior-release-worker'
                """
            ).fetchone(),
            connection.execute(
                """
                SELECT backup_id, manifest_hash, git_sha, build_digest,
                       release_bundle_digest
                FROM research_ops.backup_set
                WHERE release_id = 'prior-release'
                """
            ).fetchone(),
        )


def test_prior_upgrade_database_name_guards_reject_broad_targets() -> None:
    source = "research_ops_ci"
    with pytest.raises(ValueError, match="prior_upgrade_target_database_name_invalid"):
        _validated_upgrade_database_name(source, source)
    for unsafe in ("postgres", "research_ops_ci_other", "research_ops_upgrade_ci_123"):
        with pytest.raises(
            ValueError,
            match="prior_upgrade_target_database_name_invalid",
        ):
            _validated_upgrade_database_name(source, unsafe)
    target = "research_ops_upgrade_ci_0123456789abcdef01234567"
    assert _validated_upgrade_database_name(source, target) == target


@pytest.mark.postgresql
def test_prior_release_schema_and_data_upgrade_to_current_platform(
    tmp_path: Path,
) -> None:
    source_dsn = os.environ.get(TEST_DATABASE_ENV, "").strip()
    if not source_dsn:
        pytest.skip(f"{TEST_DATABASE_ENV} is not configured")
    source_name = _database_name(source_dsn)
    admin_dsn = _database_url_for_name(source_dsn, "postgres")
    target_name = _new_upgrade_database_name(source_name)
    target_dsn = _database_url_for_name(source_dsn, target_name)
    target_created = False
    try:
        _create_pristine_database(admin_dsn, source_name, target_name)
        target_created = True
        web_environment = _web_environment(
            dsn=target_dsn,
            database_name=target_name,
            state_root=tmp_path / "prior-release-web-state",
        )
        _run_web_migrations(web_environment, target=_PRIOR_PORTAL_MIGRATION)
        prior_migration_hashes = _install_prior_operations_release(target_dsn)
        fixture = _seed_prior_release_state(target_dsn)
        prior_snapshot = _prior_state_snapshot(target_dsn)

        with psycopg.connect(target_dsn) as connection:
            prior_portal_head = connection.execute(
                """
                SELECT name FROM django_migrations
                WHERE app = 'portal' ORDER BY applied DESC LIMIT 1
                """
            ).fetchone()[0]
            imported_report_table = connection.execute(
                "SELECT to_regclass('public.portal_importeddecisionreport')"
            ).fetchone()[0]
            import_permission_count = connection.execute(
                """
                SELECT count(*)
                FROM auth_group AS role
                JOIN auth_group_permissions AS membership
                  ON membership.group_id = role.id
                JOIN auth_permission AS permission
                  ON permission.id = membership.permission_id
                WHERE role.name = 'research_admin'
                  AND permission.codename = 'import_research_report'
                """
            ).fetchone()[0]
        assert prior_portal_head == _PRIOR_PORTAL_MIGRATION
        assert imported_report_table is None
        assert import_permission_count == 0

        _run_web_migrations(web_environment)
        current_operations = _operations_migration_files()
        pending_names = tuple(
            item.name
            for item in current_operations[len(_PRIOR_OPERATIONS_MIGRATIONS) :]
        )
        assert pending_names == (
            "0005_recovery_activation_event.sql",
            "0006_service_alert_workflow.sql",
        )
        first_upgrade = apply_migrations(target_dsn)
        assert first_upgrade.applied == pending_names
        assert first_upgrade.already_applied == _PRIOR_OPERATIONS_MIGRATIONS
        assert _prior_state_snapshot(target_dsn) == prior_snapshot

        with psycopg.connect(target_dsn) as connection:
            current_portal_head = connection.execute(
                """
                SELECT name FROM django_migrations
                WHERE app = 'portal' ORDER BY applied DESC LIMIT 1
                """
            ).fetchone()[0]
            operations_history = dict(
                connection.execute(
                    """
                    SELECT name, content_hash
                    FROM research_ops.migration_history ORDER BY name
                    """
                ).fetchall()
            )
            import_permission_count = connection.execute(
                """
                SELECT count(*)
                FROM auth_group AS role
                JOIN auth_group_permissions AS membership
                  ON membership.group_id = role.id
                JOIN auth_permission AS permission
                  ON permission.id = membership.permission_id
                WHERE role.name = 'research_admin'
                  AND permission.codename = 'import_research_report'
                """
            ).fetchone()[0]
            preserved_admin_permission_count = connection.execute(
                """
                SELECT count(*)
                FROM auth_group AS role
                JOIN auth_group_permissions AS membership
                  ON membership.group_id = role.id
                JOIN auth_permission AS permission
                  ON permission.id = membership.permission_id
                WHERE role.name = 'research_admin'
                  AND permission.codename = 'manage_research_web'
                """
            ).fetchone()[0]
            recovery_table = connection.execute(
                "SELECT to_regclass('research_ops.recovery_activation_event')"
            ).fetchone()[0]
            append_only_trigger = connection.execute(
                """
                SELECT count(*)
                FROM pg_trigger
                WHERE tgrelid = 'research_ops.recovery_activation_event'::regclass
                  AND tgname = 'research_ops_recovery_activation_event_append_only'
                  AND NOT tgisinternal
                """
            ).fetchone()[0]
            alert_table = connection.execute(
                "SELECT to_regclass('research_ops.service_alert')"
            ).fetchone()[0]
            alert_append_only_trigger = connection.execute(
                """
                SELECT count(*)
                FROM pg_trigger
                WHERE tgrelid = 'research_ops.service_alert_event'::regclass
                  AND tgname = 'research_ops_service_alert_event_append_only'
                  AND NOT tgisinternal
                """
            ).fetchone()[0]
            django_migration_count = connection.execute(
                "SELECT count(*) FROM django_migrations"
            ).fetchone()[0]
        assert current_portal_head == "0010_dataset_resource_access"
        assert import_permission_count == 1
        assert preserved_admin_permission_count == 1
        assert recovery_table == "research_ops.recovery_activation_event"
        assert append_only_trigger == 1
        assert alert_table == "research_ops.service_alert"
        assert alert_append_only_trigger == 1
        assert {
            name: operations_history[name] for name in _PRIOR_OPERATIONS_MIGRATIONS
        } == prior_migration_hashes
        assert tuple(operations_history) == tuple(
            item.name for item in current_operations
        )

        _run_web_migrations(web_environment)
        second_upgrade = apply_migrations(target_dsn)
        assert second_upgrade.applied == ()
        assert second_upgrade.already_applied == tuple(
            item.name for item in current_operations
        )
        with psycopg.connect(target_dsn) as connection:
            assert (
                connection.execute("SELECT count(*) FROM django_migrations").fetchone()[
                    0
                ]
                == django_migration_count
            )

        outbox = OutboxStore(target_dsn)
        assert outbox.scan() == 0
        claim = outbox.claim(worker_id="post-upgrade-worker")
        assert claim is not None
        assert claim.event_id == fixture["event_id"]
        assert claim.payload_hash == fixture["payload_hash"]
        outbox.mark_projected(claim)

        store = ExperimentAdmissionStore(target_dsn)
        admission_args = {
            "authority": "market-research:experiment:v1",
            "experiment_id": "post-upgrade-study",
            "manifest_hash": "sha256:" + "e" * 64,
            "request_id": "post-upgrade-request",
            "request_hash": "sha256:" + "f" * 64,
            "owner_id": "post-upgrade-owner",
        }
        decision = store.acquire(**admission_args)
        job_id = uuid.uuid4()
        completed = store.complete_research_job(
            decision,
            job_id=job_id,
            result_ref="report:post-upgrade/result.json",
            result_hash="sha256:" + "9" * 64,
            research_outcome="PASS",
            core_run_id="post-upgrade-core-run",
        )
        replay = store.acquire(**admission_args)
        receipt = store.research_job_receipt(job_id)
        assert completed.status == "SUCCEEDED"
        assert replay.is_reused_result
        assert replay.run_id == decision.run_id
        assert receipt is not None
        assert receipt.admission_run_id == decision.run_id
        assert receipt.result_hash == completed.result_hash
        store.mark_research_job_receipt_applied(
            job_id=job_id,
            result_hash=receipt.result_hash,
        )

        with psycopg.connect(target_dsn) as connection:
            prior_delivery = connection.execute(
                """
                SELECT status, attempt_count, payload_hash
                FROM research_ops.outbox_delivery WHERE event_id = %s
                """,
                (fixture["event_id"],),
            ).fetchone()
            prior_request = connection.execute(
                """
                SELECT run_id, status, result_hash
                FROM research_ops.experiment_request
                WHERE authority = 'market-research:experiment:v1'
                  AND experiment_id = 'prior-release-study'
                  AND request_id = 'prior-release-request'
                """
            ).fetchone()
        assert prior_delivery == (
            PROJECTED,
            1,
            fixture["payload_hash"],
        )
        assert prior_request == (
            fixture["run_id"],
            "SUCCEEDED",
            fixture["result_hash"],
        )
    finally:
        if target_created:
            _drop_upgrade_database(admin_dsn, source_name, target_name)
