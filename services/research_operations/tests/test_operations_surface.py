from __future__ import annotations

import json
import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import research_operations.backup as backup_module
from research_operations import health, runtime
from research_operations.backup import (
    BackupContractError,
    FenceStatus,
    RecoveryVerification,
    VerifiedBackup,
    activate_verified_recovery,
    create_signed_backup_manifest,
    create_signed_recovery_receipt,
    finalize_private_fence_receipt,
    read_private_fence_receipt,
    verify_backup_set,
    verify_restored_application_state,
    verify_signed_recovery_receipt,
    write_private_fence_intent,
    write_private_fence_receipt,
)
from research_operations.health import CheckResult, HealthSnapshot
from research_operations.metrics import render_prometheus


def _rsa_key_pair(directory: Path, name: str) -> tuple[Path, Path]:
    private_key = directory / f"{name}.pem"
    public_key = directory / f"{name}.pub.pem"
    subprocess.run(
        [
            "/usr/bin/openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(private_key),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "/usr/bin/openssl",
            "pkey",
            "-in",
            str(private_key),
            "-pubout",
            "-out",
            str(public_key),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return private_key, public_key


def _request(application, *, method: str, path: str, headers=None):
    captured = {}

    def start_response(status, response_headers):
        captured["status"] = status
        captured["headers"] = dict(response_headers)

    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": ""}
    environ.update(headers or {})
    body = b"".join(application(environ, start_response))
    return captured, body


def test_liveness_is_constant_and_does_not_read_dependencies(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "collect_health_snapshot",
        lambda *_args, **_kwargs: pytest.fail("liveness queried dependencies"),
    )
    response, body = _request(
        runtime.OperationsApplication(), method="GET", path="/__ops/live"
    )
    assert response["status"] == "200 OK"
    assert body == b'{"status":"UP"}'


def test_release_configuration_requires_production_web_security() -> None:
    now = datetime.now(UTC)
    environment = {
        "RESEARCH_OPS_GIT_SHA": "1" * 40,
        "RESEARCH_OPS_RELEASE_ID": "release-1",
        "RESEARCH_OPS_BUILD_DIGEST": "sha256:" + "a" * 64,
        "RESEARCH_OPS_RELEASE_BUNDLE_DIGEST": "sha256:" + "b" * 64,
        "RESEARCH_OPS_EXPECTED_MIGRATION_DIGEST": (
            health.expected_platform_migration_digest()
        ),
        "INTERNAL_WEB_DATABASE_ENGINE": "postgresql",
        "INTERNAL_WEB_ALLOWED_HOSTS": "research.internal",
        "INTERNAL_WEB_TRUST_X_FORWARDED_PROTO": "true",
        "INTERNAL_WEB_SECURE_SSL_REDIRECT": "true",
        "INTERNAL_WEB_SECURE_COOKIES": "true",
        "INTERNAL_WEB_HSTS_SECONDS": "3600",
    }
    assert (
        health.release_configuration_check(environment, observed_at=now).status
        == "PASS"
    )
    assert (
        health.release_configuration_check(
            {**environment, "INTERNAL_WEB_DATABASE_ENGINE": "sqlite"},
            observed_at=now,
        ).status
        == "FAIL"
    )
    assert (
        health.release_configuration_check(
            {**environment, "INTERNAL_WEB_DEBUG": "true"},
            observed_at=now,
        ).status
        == "FAIL"
    )
    assert (
        health.release_configuration_check(
            {
                key: value
                for key, value in environment.items()
                if key != "RESEARCH_OPS_GIT_SHA"
            },
            observed_at=now,
        ).reason_code
        == "release_configuration_invalid"
    )


def _preflight_environment() -> dict[str, str]:
    return {
        "RESEARCH_OPS_GIT_SHA": "1" * 40,
        "RESEARCH_OPS_RELEASE_ID": "release-1",
        "RESEARCH_OPS_BUILD_DIGEST": "sha256:" + "a" * 64,
        "RESEARCH_OPS_RELEASE_BUNDLE_DIGEST": "sha256:" + "b" * 64,
        "RESEARCH_OPS_PREFLIGHT_RECEIPT": (
            "/run/research-operations-preflight/observation.json"
        ),
        "RESEARCH_OPS_PREFLIGHT_MAX_AGE_SECONDS": "90000",
    }


def _preflight_receipt(now: datetime) -> dict[str, object]:
    environment = _preflight_environment()
    return {
        "schema_version": 1,
        "status": "PASS",
        "checked_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "git_sha": environment["RESEARCH_OPS_GIT_SHA"],
        "release_id": environment["RESEARCH_OPS_RELEASE_ID"],
        "build_digest": environment["RESEARCH_OPS_BUILD_DIGEST"],
        "release_bundle_digest": environment["RESEARCH_OPS_RELEASE_BUNDLE_DIGEST"],
        "failure_code": None,
    }


def test_preflight_receipt_check_accepts_fresh_release_bound_pass() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    check = health.preflight_receipt_check(
        _preflight_environment(),
        observed_at=now,
        receipt_loader=lambda _path: _preflight_receipt(now),
    )
    assert check.status == "PASS"
    assert check.reason_code == "preflight_receipt_fresh"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda receipt, _env, _now: receipt.update(
                status="FAIL", failure_code="pki_expired"
            ),
            "preflight_receipt_failed",
        ),
        (
            lambda receipt, _env, now: receipt.update(
                checked_at=(now - timedelta(days=2))
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            ),
            "preflight_receipt_stale",
        ),
        (
            lambda receipt, _env, _now: receipt.update(git_sha="2" * 40),
            "preflight_release_mismatch",
        ),
        (
            lambda receipt, env, _now: env.pop("RESEARCH_OPS_RELEASE_BUNDLE_DIGEST"),
            "preflight_receipt_invalid",
        ),
    ],
)
def test_preflight_receipt_check_fails_closed(mutation, reason: str) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    environment = _preflight_environment()
    receipt = _preflight_receipt(now)
    mutation(receipt, environment, now)

    check = health.preflight_receipt_check(
        environment,
        observed_at=now,
        receipt_loader=lambda _path: receipt,
    )

    assert check.status == "FAIL"
    assert check.reason_code == reason


def test_preflight_receipt_reader_rejects_symlink_and_untrusted_write(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(_preflight_receipt(datetime.now(UTC))))
    receipt.chmod(0o640)
    assert (
        health._read_preflight_receipt(receipt, required_uid=os.getuid())["status"]
        == "PASS"
    )

    receipt.chmod(0o660)
    with pytest.raises(ValueError, match="preflight_receipt_invalid"):
        health._read_preflight_receipt(receipt, required_uid=os.getuid())

    receipt.chmod(0o640)
    link = tmp_path / "receipt-link.json"
    link.symlink_to(receipt)
    with pytest.raises(OSError):
        health._read_preflight_receipt(link, required_uid=os.getuid())


def test_diagnostics_requires_both_proxy_identity_headers(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "_diagnostics_payload",
        lambda: pytest.fail("unauthorized diagnostics executed"),
    )
    response, body = _request(
        runtime.OperationsApplication(),
        method="GET",
        path="/__ops/diagnostics",
        headers={"HTTP_X_RESEARCH_OPS_CLIENT_VERIFIED": "SUCCESS"},
    )
    assert response["status"] == "404 Not Found"
    assert body == b'{"status":"NOT_FOUND"}'


def test_runtime_diagnostics_includes_release_evidence(monkeypatch) -> None:
    now = datetime.now(UTC)
    snapshot = HealthSnapshot(
        now,
        (CheckResult("release_configuration", "PASS", "release_valid", now),),
    )
    release = {
        "schema_version": 1,
        "configured": True,
        "components": {
            "internal_web": {
                "git_sha": "1" * 40,
                "release_id": "release-1",
                "build_digest": "sha256:" + "a" * 64,
            },
            "research_operations": {
                "git_sha": "1" * 40,
                "release_id": "release-1",
                "build_digest": "sha256:" + "a" * 64,
            },
        },
        "workers": [],
        "workers_truncated": False,
        "worker_state_available": True,
    }
    monkeypatch.setattr(runtime, "collect_health_snapshot", lambda *_args: snapshot)
    monkeypatch.setattr(runtime, "_history_checks", lambda _now: ())
    monkeypatch.setattr(runtime, "release_diagnostics", lambda **_kwargs: release)

    payload = runtime._diagnostics_payload()

    assert payload["release"] == release


def _database_snapshot(now: datetime) -> dict:
    return {
        "primary": (False, "off", 1),
        "migrations": health.expected_migration_hashes(),
        "expected_migrations": health.expected_migration_hashes(),
        "portal_migrations": health.expected_portal_migrations(),
        "expected_portal_migrations": health.expected_portal_migrations(),
        "control": (True, True, False, 0),
        "outbox": (0, 0, 0, 0.0),
        "fresh_outbox_workers": 1,
        "fresh_research_job_workers": 1,
        "worker_release_mismatch_count": 0,
        "expected_release_configured": True,
        "unapplied_job_receipts": 0,
        "audit": ("PASS", "audit_validation_passed", 0, now, ""),
    }


@pytest.mark.parametrize("kind", ["web-read", "workflow-mutation"])
def test_both_readiness_surfaces_require_preflight_receipt(
    monkeypatch, kind: str
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    failed_receipt = _preflight_receipt(now)
    failed_receipt.update(status="FAIL", failure_code="preflight_in_progress")
    monkeypatch.setattr(health, "_read_preflight_receipt", lambda _path: failed_receipt)
    monkeypatch.setattr(
        health, "_database_snapshot", lambda **_kwargs: _database_snapshot(now)
    )
    monkeypatch.setattr(
        health,
        "release_configuration_check",
        lambda *_args, **_kwargs: CheckResult(
            "release_configuration", "PASS", "release_configuration_valid", now
        ),
    )
    monkeypatch.setattr(
        health,
        "_filesystem_check",
        lambda *_args, **_kwargs: CheckResult(
            "filesystem_roots", "PASS", "filesystem_write_policy_qualified", now
        ),
    )

    snapshot = health.collect_health_snapshot(
        kind,
        environ=_preflight_environment(),
        observed_at=now,
        use_cache=False,
    )

    check = next(
        item for item in snapshot.checks if item.check_id == "deployment_preflight"
    )
    assert check.status == "FAIL"
    assert check.reason_code == "preflight_receipt_failed"
    assert not snapshot.ready


def test_expected_portal_migrations_come_from_installed_web_package() -> None:
    assert health.expected_portal_migrations() == (
        "0001_initial",
        "0002_seed_rbac",
        "0003_validation_preflight_binding",
        "0004_researchjob_one_active_per_owner",
        "0005_manifest_experiment_id_and_login_throttle",
        "0006_webauditevent",
        "0007_governance_authority",
        "0008_imported_decision_report",
    )


@pytest.mark.parametrize("key", ["migrations", "portal_migrations"])
def test_migration_readiness_requires_ops_and_portal_exactly(key: str) -> None:
    now = datetime.now(UTC)
    database = _database_snapshot(now)
    if key == "migrations":
        database[key] = {**database[key], "9999_unexpected.sql": "bad"}
    else:
        database[key] = (*database[key], "9999_unexpected")

    check = health._migration_check(database, now)

    assert check.status == "FAIL"
    assert check.reason_code == "migration_leaves_mismatch"
    assert check.count == 1


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        (
            lambda value: value.update(outbox=(0, 0, 1, 0.0)),
            "outbox_dead_letter_present",
        ),
        (
            lambda value: value.update(fresh_outbox_workers=0),
            "outbox_worker_pool_unavailable",
        ),
        (
            lambda value: value.update(fresh_research_job_workers=0),
            "research_job_worker_pool_unavailable",
        ),
        (
            lambda value: value.update(unapplied_job_receipts=1),
            "research_job_receipt_unapplied",
        ),
    ],
)
def test_workflow_readiness_fails_for_backlog_and_worker_risks(
    monkeypatch, mutation, expected_reason
):
    now = datetime.now(UTC)
    database = _database_snapshot(now)
    mutation(database)
    monkeypatch.setattr(health, "_database_snapshot", lambda **_kwargs: database)
    monkeypatch.setattr(
        health,
        "release_configuration_check",
        lambda *_args, **_kwargs: CheckResult(
            "release_configuration", "PASS", "release_configuration_valid", now
        ),
    )
    monkeypatch.setattr(
        health,
        "_filesystem_check",
        lambda *_args, **_kwargs: CheckResult(
            "filesystem_roots", "PASS", "filesystem_write_policy_qualified", now
        ),
    )
    snapshot = health.collect_health_snapshot(
        "workflow-mutation",
        environ={},
        observed_at=now,
        use_cache=False,
    )
    assert not snapshot.ready
    assert expected_reason in {check.reason_code for check in snapshot.checks}


def test_workflow_readiness_fails_closed_when_database_is_down(monkeypatch):
    now = datetime.now(UTC)
    monkeypatch.setattr(
        health,
        "_database_snapshot",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("unavailable")),
    )
    monkeypatch.setattr(
        health,
        "release_configuration_check",
        lambda *_args, **_kwargs: CheckResult(
            "release_configuration", "PASS", "release_configuration_valid", now
        ),
    )
    monkeypatch.setattr(
        health,
        "_filesystem_check",
        lambda *_args, **_kwargs: CheckResult(
            "filesystem_roots", "PASS", "filesystem_write_policy_qualified", now
        ),
    )
    snapshot = health.collect_health_snapshot(
        "workflow-mutation", environ={}, observed_at=now, use_cache=False
    )
    assert not snapshot.ready
    assert "database_unavailable" in {check.reason_code for check in snapshot.checks}


def test_worker_freshness_uses_past_cutoff_not_observation_time(monkeypatch):
    observed = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    captured = []

    class Result:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = rows

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class Connection:
        def execute(self, statement, parameters=None):
            if "pg_is_in_recovery" in statement:
                return Result((False, "off", 1))
            if "migration_history" in statement:
                return Result(rows=[])
            if "django_migrations" in statement:
                return Result(
                    rows=[(name,) for name in health.expected_portal_migrations()]
                )
            if "runtime_control" in statement:
                return Result((True, True, False, 0))
            if "outbox_delivery" in statement:
                return Result((0, 0, 0, 0.0))
            if "worker_heartbeat" in statement:
                captured.append(parameters[4])
                return Result((1, 1, 0))
            if "research_job_result_receipt" in statement:
                return Result((0,))
            return Result(None)

    class Context:
        def __enter__(self):
            return Connection()

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(health, "connection", lambda *_args, **_kwargs: Context())
    health._database_snapshot(
        dsn=None,
        observed_at=observed,
        worker_heartbeat_max_age_seconds=30,
    )
    assert captured == [observed.replace(second=30, minute=59, hour=11)]


def test_readiness_rejects_fresh_worker_release_mismatch(monkeypatch) -> None:
    now = datetime.now(UTC)
    database = _database_snapshot(now)
    database["worker_release_mismatch_count"] = 1
    monkeypatch.setattr(health, "_database_snapshot", lambda **_kwargs: database)
    monkeypatch.setattr(
        health,
        "release_configuration_check",
        lambda *_args, **_kwargs: CheckResult(
            "release_configuration", "PASS", "release_configuration_valid", now
        ),
    )
    monkeypatch.setattr(
        health,
        "_filesystem_check",
        lambda *_args, **_kwargs: CheckResult(
            "filesystem_roots", "PASS", "filesystem_write_policy_qualified", now
        ),
    )

    snapshot = health.collect_health_snapshot(
        "web-read",
        environ={
            "RESEARCH_OPS_GIT_SHA": "1" * 40,
            "RESEARCH_OPS_RELEASE_ID": "release-1",
            "RESEARCH_OPS_BUILD_DIGEST": "sha256:" + "a" * 64,
            "RESEARCH_OPS_RELEASE_BUNDLE_DIGEST": "sha256:" + "b" * 64,
        },
        observed_at=now,
        use_cache=False,
    )

    assert not snapshot.ready
    assert "worker_release_mismatch" in {check.reason_code for check in snapshot.checks}


def test_release_diagnostics_names_web_ops_and_worker_release(monkeypatch) -> None:
    class Result:
        def fetchall(self):
            return [
                (
                    "outbox-worker",
                    "1" * 40,
                    "release-1",
                    "sha256:" + "a" * 64,
                    "sha256:" + "b" * 64,
                    True,
                    2,
                )
            ]

    class Connection:
        def execute(self, _statement, _parameters=None):
            return Result()

    class Context:
        def __enter__(self):
            return Connection()

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(health, "connection", lambda *_args, **_kwargs: Context())
    metadata = health.release_diagnostics(
        environ={
            "RESEARCH_OPS_GIT_SHA": "1" * 40,
            "RESEARCH_OPS_RELEASE_ID": "release-1",
            "RESEARCH_OPS_BUILD_DIGEST": "sha256:" + "a" * 64,
            "RESEARCH_OPS_RELEASE_BUNDLE_DIGEST": "sha256:" + "b" * 64,
        },
        observed_at=datetime.now(UTC),
    )

    assert metadata["components"]["internal_web"]["git_sha"] == "1" * 40
    assert metadata["components"]["internal_web"]["release_bundle_digest"] == (
        "sha256:" + "b" * 64
    )
    assert (
        metadata["components"]["research_operations"]
        == metadata["components"]["internal_web"]
    )
    assert metadata["workers"] == [
        {
            "service_role": "outbox-worker",
            "git_sha": "1" * 40,
            "release_id": "release-1",
            "build_digest": "sha256:" + "a" * 64,
            "release_bundle_digest": "sha256:" + "b" * 64,
            "count": 2,
            "matches_runtime": True,
        }
    ]


class _FakeConnection:
    def execute(self, statement, _parameters=None):
        self._statement = statement
        return self

    def fetchone(self):
        return (True, False)


class _FakeContext:
    def __init__(self, *, fail=False):
        self.fail = fail

    def __enter__(self):
        if self.fail:
            raise OSError("db unavailable")
        return _FakeConnection()

    def __exit__(self, *_exc):
        return False


@pytest.mark.parametrize("db_fails", [False, True])
def test_guarded_web_blocks_readiness_and_database_failures(monkeypatch, db_fails):
    called = []

    def web(_environ, start_response):
        called.append(True)
        start_response("200 OK", [])
        return [b"ok"]

    monkeypatch.setattr(runtime, "_research_web_application", lambda: web)
    monkeypatch.setattr(
        runtime, "connection", lambda **_kwargs: _FakeContext(fail=db_fails)
    )
    monkeypatch.setattr(
        runtime,
        "collect_health_snapshot",
        lambda *_args, **_kwargs: HealthSnapshot(
            datetime.now(UTC),
            (
                CheckResult(
                    "outbox_workers",
                    "FAIL",
                    "outbox_worker_pool_unavailable",
                    datetime.now(UTC),
                ),
            ),
        ),
    )
    response, body = _request(
        runtime.MutationGuardedWebApplication(), method="POST", path="/jobs/new/"
    )
    assert response["status"] == "503 Service Unavailable"
    assert body == b"Service unavailable"
    assert not called


def test_private_fence_receipt_is_owner_only_and_hash_bound(tmp_path):
    token = uuid.uuid4()
    status = FenceStatus(
        phase="DRAINING",
        generation=3,
        fence_token=token,
        changed_at=datetime.now(UTC),
        active_jobs=0,
        active_experiment_claims=0,
        pending_outbox=0,
        claimed_outbox=0,
        dead_letter_outbox=0,
        unprojected_audit_intents=0,
        unapplied_job_receipts=0,
        audit_validation_status="PASS",
        audit_validation_reason_count=0,
        audit_validation_observed_at=datetime.now(UTC),
        latest_audit_projection_at=None,
        audit_row_count=0,
        audit_terminal_hash="",
    )
    receipt = tmp_path / "fence.json"
    write_private_fence_receipt(status=status, path=receipt)
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert read_private_fence_receipt(receipt) == (token, 3)
    receipt.chmod(0o640)
    with pytest.raises(BackupContractError, match="permissions"):
        read_private_fence_receipt(receipt)


def test_private_fence_intent_survives_commit_to_receipt_window(tmp_path):
    token = uuid.uuid4()
    receipt = tmp_path / "fence-intent.json"
    write_private_fence_intent(fence_token=token, path=receipt)
    assert read_private_fence_receipt(receipt) == (token, 0)

    status = FenceStatus(
        phase="DRAINING",
        generation=4,
        fence_token=token,
        changed_at=datetime.now(UTC),
        active_jobs=0,
        active_experiment_claims=0,
        pending_outbox=0,
        claimed_outbox=0,
        dead_letter_outbox=0,
        unprojected_audit_intents=0,
        unapplied_job_receipts=0,
        audit_validation_status="PASS",
        audit_validation_reason_count=0,
        audit_validation_observed_at=datetime.now(UTC),
        latest_audit_projection_at=None,
        audit_row_count=0,
        audit_terminal_hash="",
    )
    finalize_private_fence_receipt(status=status, path=receipt)
    assert read_private_fence_receipt(receipt) == (token, 4)


def test_signed_backup_manifest_detects_archive_tamper(tmp_path):
    private_key = tmp_path / "signing.pem"
    public_key = tmp_path / "signing.pub.pem"
    subprocess.run(
        [
            "/usr/bin/openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(private_key),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "/usr/bin/openssl",
            "pkey",
            "-in",
            str(private_key),
            "-pubout",
            "-out",
            str(public_key),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    backup = tmp_path / "backup"
    backup.mkdir()
    files = {
        "postgresql": "postgresql.dump",
        "data": "data.tar",
        "manifest": "manifest.tar",
        "artifact": "artifact.tar",
        "report": "report.tar",
        "identity_registry": "identity.tar",
    }
    for relative in files.values():
        (backup / relative).write_bytes(relative.encode())
    verified = create_signed_backup_manifest(
        backup_directory=backup,
        files=files,
        signing_private_key=private_key,
        verification_public_key=public_key,
        backup_id=uuid.uuid4(),
        fence_token=uuid.uuid4(),
        fence_generation=1,
        git_sha="1" * 40,
        release_id="release-1",
        build_digest="sha256:" + "a" * 64,
        release_bundle_digest="sha256:" + "b" * 64,
        postgresql_major=16,
        audit_row_count=0,
        audit_terminal_hash="",
    )
    assert verified.git_sha == "1" * 40
    assert verified.release_id == "release-1"
    assert verified.release_bundle_digest == "sha256:" + "b" * 64
    assert verified.migration_digest == health.expected_platform_migration_digest()
    with pytest.raises(BackupContractError, match="backup_git_sha_mismatch"):
        verify_backup_set(
            backup_directory=backup,
            verification_public_key=public_key,
            expected_git_sha="2" * 40,
        )
    with pytest.raises(
        BackupContractError,
        match="backup_release_bundle_digest_mismatch",
    ):
        verify_backup_set(
            backup_directory=backup,
            verification_public_key=public_key,
            expected_release_bundle_digest="sha256:" + "c" * 64,
        )
    (backup / "data.tar").write_bytes(b"tampered")
    with pytest.raises(BackupContractError, match="checksum"):
        verify_backup_set(
            backup_directory=backup,
            verification_public_key=public_key,
        )


def test_signed_recovery_receipt_converges_after_control_response_loss(tmp_path):
    private_key = tmp_path / "recovery-signing.pem"
    public_key = tmp_path / "recovery-signing.pub.pem"
    subprocess.run(
        [
            "/usr/bin/openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(private_key),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "/usr/bin/openssl",
            "pkey",
            "-in",
            str(private_key),
            "-pubout",
            "-out",
            str(public_key),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    now = datetime.now(UTC)
    check = {"id": "audit_outbox", "status": "PASS", "reason_code": "verified"}
    original = RecoveryVerification(
        status="PASS",
        backup_manifest_hash="sha256:" + "a" * 64,
        started_at=now,
        finished_at=now + timedelta(seconds=1),
        checks=(check,),
        git_sha="1" * 40,
        release_id="release-1",
        build_digest="sha256:" + "b" * 64,
        release_bundle_digest="sha256:" + "c" * 64,
    )
    receipt = tmp_path / "recovery.json"
    expected_hash, _signature = create_signed_recovery_receipt(
        verification=original,
        receipt_path=receipt,
        signing_private_key=private_key,
        verification_public_key=public_key,
    )
    retried = RecoveryVerification(
        status="PASS",
        backup_manifest_hash=original.backup_manifest_hash,
        started_at=now + timedelta(seconds=2),
        finished_at=now + timedelta(seconds=3),
        checks=(check,),
        git_sha=original.git_sha,
        release_id=original.release_id,
        build_digest=original.build_digest,
        release_bundle_digest=original.release_bundle_digest,
    )
    actual_hash, recovered, document = verify_signed_recovery_receipt(
        verification=retried,
        receipt_path=receipt,
        verification_public_key=public_key,
    )
    assert actual_hash == expected_hash
    assert document == original.document()
    assert recovered.status == original.status
    assert recovered.backup_manifest_hash == original.backup_manifest_hash
    assert recovered.checks == original.checks
    assert document["release"] == {
        "git_sha": original.git_sha,
        "release_id": original.release_id,
        "build_digest": original.build_digest,
        "release_bundle_digest": original.release_bundle_digest,
    }
    legacy = RecoveryVerification(
        status="PASS",
        backup_manifest_hash=original.backup_manifest_hash,
        started_at=now,
        finished_at=now + timedelta(seconds=1),
        checks=(check,),
    )
    legacy_receipt = tmp_path / "legacy-recovery.json"
    create_signed_recovery_receipt(
        verification=legacy,
        receipt_path=legacy_receipt,
        signing_private_key=private_key,
        verification_public_key=public_key,
    )
    _legacy_hash, recovered_legacy, legacy_document = verify_signed_recovery_receipt(
        verification=legacy,
        receipt_path=legacy_receipt,
        verification_public_key=public_key,
    )
    assert legacy_document["schema_version"] == 1
    assert recovered_legacy.release_bundle_digest == ""


def test_signed_publish_failure_leaves_no_final_or_temporary_orphan(tmp_path):
    private_key, public_key = _rsa_key_pair(tmp_path, "trusted")
    invalid_private_key = tmp_path / "invalid-signing.pem"
    invalid_private_key.write_text("not a private key\n")
    backup = tmp_path / "backup-signing-retry"
    backup.mkdir()
    files = {
        "postgresql": "postgresql.dump",
        "data": "data.tar",
        "manifest": "manifest.tar",
        "artifact": "artifact.tar",
        "report": "report.tar",
        "identity_registry": "identity.tar",
    }
    for relative in files.values():
        (backup / relative).write_bytes(relative.encode())
    arguments = {
        "backup_directory": backup,
        "files": files,
        "verification_public_key": public_key,
        "backup_id": uuid.uuid4(),
        "fence_token": uuid.uuid4(),
        "fence_generation": 1,
        "git_sha": "1" * 40,
        "release_id": "retry-release",
        "build_digest": "sha256:" + "a" * 64,
        "release_bundle_digest": "sha256:" + "b" * 64,
        "postgresql_major": 16,
        "audit_row_count": 0,
        "audit_terminal_hash": "",
    }
    with pytest.raises(BackupContractError, match="signing_failed"):
        create_signed_backup_manifest(
            **arguments,
            signing_private_key=invalid_private_key,
        )
    assert not (backup / "manifest.json").exists()
    assert not (backup / "manifest.sig").exists()
    assert not tuple(path for path in backup.iterdir() if path.name.endswith(".tmp"))
    create_signed_backup_manifest(
        **arguments,
        signing_private_key=private_key,
    )

    now = datetime.now(UTC)
    verification = RecoveryVerification(
        status="PASS",
        backup_manifest_hash="sha256:" + "c" * 64,
        started_at=now,
        finished_at=now + timedelta(seconds=1),
        checks=({"id": "retry", "status": "PASS", "reason_code": "verified"},),
        git_sha="1" * 40,
        release_id="retry-release",
        build_digest="sha256:" + "a" * 64,
        release_bundle_digest="sha256:" + "b" * 64,
    )
    receipt = tmp_path / "recovery-signing-retry.json"
    with pytest.raises(BackupContractError, match="signing_failed"):
        create_signed_recovery_receipt(
            verification=verification,
            receipt_path=receipt,
            signing_private_key=invalid_private_key,
            verification_public_key=public_key,
        )
    assert not receipt.exists()
    assert not receipt.with_suffix(".json.sig").exists()
    assert not tuple(path for path in tmp_path.iterdir() if path.name.endswith(".tmp"))
    create_signed_recovery_receipt(
        verification=verification,
        receipt_path=receipt,
        signing_private_key=private_key,
        verification_public_key=public_key,
    )


def test_signed_receipt_resumes_exact_document_after_publish_interruption(
    tmp_path,
    monkeypatch,
):
    private_key, public_key = _rsa_key_pair(tmp_path, "publish")
    now = datetime.now(UTC)
    check = {"id": "audit", "status": "PASS", "reason_code": "verified"}
    original = RecoveryVerification(
        status="PASS",
        backup_manifest_hash="sha256:" + "a" * 64,
        started_at=now,
        finished_at=now + timedelta(seconds=1),
        checks=(check,),
        git_sha="1" * 40,
        release_id="publish-retry",
        build_digest="sha256:" + "b" * 64,
        release_bundle_digest="sha256:" + "c" * 64,
    )
    receipt = tmp_path / "interrupted.json"
    signature = receipt.with_suffix(".json.sig")
    original_publish = backup_module._publish_new_file
    interrupted = False

    def interrupt_signature_publish(temporary, destination):
        nonlocal interrupted
        if destination == signature and not interrupted:
            interrupted = True
            raise OSError("simulated publication interruption")
        original_publish(temporary, destination)

    monkeypatch.setattr(
        backup_module,
        "_publish_new_file",
        interrupt_signature_publish,
    )
    with pytest.raises(BackupContractError, match="publish_interrupted"):
        create_signed_recovery_receipt(
            verification=original,
            receipt_path=receipt,
            signing_private_key=private_key,
            verification_public_key=public_key,
        )
    assert receipt.is_file()
    assert not signature.exists()
    assert not tuple(path for path in tmp_path.iterdir() if path.name.endswith(".tmp"))

    monkeypatch.setattr(backup_module, "_publish_new_file", original_publish)
    retried = RecoveryVerification(
        status=original.status,
        backup_manifest_hash=original.backup_manifest_hash,
        started_at=now + timedelta(seconds=5),
        finished_at=now + timedelta(seconds=6),
        checks=original.checks,
        git_sha=original.git_sha,
        release_id=original.release_id,
        build_digest=original.build_digest,
        release_bundle_digest=original.release_bundle_digest,
    )
    receipt_hash, _signature = create_signed_recovery_receipt(
        verification=retried,
        receipt_path=receipt,
        signing_private_key=private_key,
        verification_public_key=public_key,
    )
    observed_hash, recovered, document = verify_signed_recovery_receipt(
        verification=retried,
        receipt_path=receipt,
        verification_public_key=public_key,
    )
    assert observed_hash == receipt_hash
    assert recovered.started_at == datetime.fromisoformat(
        original.document()["started_at"].replace("Z", "+00:00")
    )
    assert document == original.document()


def test_recovery_requires_exact_release_bundle_environment(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    verified = VerifiedBackup(
        backup_id=uuid.uuid4(),
        manifest_hash="sha256:" + "a" * 64,
        git_sha="1" * 40,
        release_id="release-1",
        build_digest="sha256:" + "b" * 64,
        release_bundle_digest="sha256:" + "c" * 64,
        migration_digest="sha256:" + "d" * 64,
        postgresql_major=16,
        fence_generation=1,
        fence_token_hash="sha256:" + "e" * 64,
        created_at=now,
        audit_row_count=0,
        audit_terminal_hash="",
        files=(),
    )
    namespace = tmp_path / "isolated-restore"
    namespace.mkdir()
    (namespace / ".research-ops-isolated-restore-v1").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "purpose": "isolated-recovery-rehearsal",
                "backup_manifest_hash": verified.manifest_hash,
            }
        )
    )
    monkeypatch.setenv("RESEARCH_OPS_RECOVERY_MODE", "offline")
    monkeypatch.setenv("RESEARCH_OPS_MUTATION_DISABLED", "true")
    monkeypatch.setenv("RESEARCH_OPS_GIT_SHA", verified.git_sha)
    monkeypatch.setenv("RESEARCH_OPS_RELEASE_ID", verified.release_id)
    monkeypatch.setenv("RESEARCH_OPS_BUILD_DIGEST", verified.build_digest)
    monkeypatch.setenv(
        "RESEARCH_OPS_RELEASE_BUNDLE_DIGEST",
        "sha256:" + "f" * 64,
    )

    with pytest.raises(BackupContractError, match="recovery_release_bundle_mismatch"):
        verify_restored_application_state(
            verified_backup=verified,
            restore_namespace=namespace,
        )

    verification = RecoveryVerification(
        status="PASS",
        backup_manifest_hash=verified.manifest_hash,
        started_at=now,
        finished_at=now,
        checks=({"id": "test", "status": "PASS", "reason_code": "verified"},),
        git_sha=verified.git_sha,
        release_id=verified.release_id,
        build_digest=verified.build_digest,
        release_bundle_digest="sha256:" + "f" * 64,
    )
    with pytest.raises(
        BackupContractError,
        match="recovery_activation_verification_invalid",
    ):
        activate_verified_recovery(
            verified_backup=verified,
            verification=verification,
            receipt_hash="sha256:" + "0" * 64,
            operator_id="recovery-test",
        )


def test_signed_backup_schema_one_remains_readable(tmp_path):
    private_key = tmp_path / "legacy-signing.pem"
    public_key = tmp_path / "legacy-signing.pub.pem"
    subprocess.run(
        [
            "/usr/bin/openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(private_key),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "/usr/bin/openssl",
            "pkey",
            "-in",
            str(private_key),
            "-pubout",
            "-out",
            str(public_key),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    backup = tmp_path / "legacy-backup"
    backup.mkdir()
    files = {
        "postgresql": "postgresql.dump",
        "data": "data.tar",
        "manifest": "manifest.tar",
        "artifact": "artifact.tar",
        "report": "report.tar",
        "identity_registry": "identity.tar",
    }
    for relative in files.values():
        (backup / relative).write_bytes(relative.encode())
    create_signed_backup_manifest(
        backup_directory=backup,
        files=files,
        signing_private_key=private_key,
        verification_public_key=public_key,
        backup_id=uuid.uuid4(),
        fence_token=uuid.uuid4(),
        fence_generation=1,
        git_sha="1" * 40,
        release_id="legacy-release",
        build_digest="sha256:" + "a" * 64,
        release_bundle_digest="sha256:" + "b" * 64,
        postgresql_major=16,
        audit_row_count=0,
        audit_terminal_hash="",
    )
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["schema_version"] = 1
    manifest.pop("git_sha")
    manifest.pop("release_bundle_digest")
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="ascii",
    )
    subprocess.run(
        [
            "/usr/bin/openssl",
            "dgst",
            "-sha256",
            "-sign",
            str(private_key),
            "-out",
            str(backup / "manifest.sig"),
            str(manifest_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    verified = verify_backup_set(
        backup_directory=backup,
        verification_public_key=public_key,
    )

    assert verified.git_sha == ""
    assert verified.release_id == "legacy-release"
    assert verified.build_digest == "sha256:" + "a" * 64
    assert verified.release_bundle_digest == ""


def test_prometheus_output_has_only_allowlisted_label_free_metrics():
    rendered = render_prometheus(
        {
            "research_ops_up": 1.0,
            "research_ops_outbox_pending": 2.0,
            "untrusted_metric": 99.0,
        }
    )
    assert "research_ops_up 1" in rendered
    assert "research_ops_outbox_pending 2" in rendered
    assert "untrusted_metric" not in rendered
    assert "{" not in rendered


def test_deployment_scripts_preserve_backup_and_restore_contracts() -> None:
    root = Path(__file__).resolve().parents[1]
    grants = (root / "scripts" / "apply-migrations.sh").read_text()
    create = (root / "scripts" / "create-backup.sh").read_text()
    restore = (root / "scripts" / "restore-rehearsal.sh").read_text()

    assert "GRANT SELECT ON ALL SEQUENCES IN SCHEMA public, research_ops" in grants
    assert "--exclude='./_internal_web/manifests'" in create
    assert "BACKUP_RESUME_ID" in create
    assert "DRAINING) ;;" in create
    assert "SEALED) sealed=1 ;;" in create
    assert "/opt/research-ops/scripts/safe-extract.py" not in restore
    assert "script_dir/safe-extract.py" in restore
    assert "SELECT current_database()" in restore
    assert "RESEARCH_OPS_RECOVERY_RESUME" in restore
    assert "ALTER DATABASE %I SET default_transaction_read_only = on" in restore
    assert "identity_registry/$identity_basename" in restore
