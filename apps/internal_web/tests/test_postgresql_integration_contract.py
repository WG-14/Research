from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from threading import Barrier
from typing import Any

import pytest
from django.db import (
    IntegrityError,
    OperationalError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.utils import timezone

from portal.jobs import claim_next_job
from portal.models import LoginThrottle, ManifestUpload, ResearchJob


POSTGRESQL_REQUIRED_REASON = (
    "requires a live PostgreSQL test database; SQLite cannot prove row locking "
    "or cross-connection uniqueness races"
)

pytestmark = [
    # Transactional tests flush tables between cases.  Preserve data-migration
    # rows (notably the RBAC groups seeded by portal.0002) when that happens so
    # every PostgreSQL concurrency case starts from the migrated schema state.
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason=POSTGRESQL_REQUIRED_REASON,
    ),
]


class _RollbackProbe(RuntimeError):
    pass


def _process_conditional_claim(
    database: dict[str, str],
    job_id: str,
    ready: Any,
    results: Any,
) -> None:
    """Contend for one row from a genuinely independent Python process."""

    import psycopg

    try:
        with psycopg.connect(**database) as raw_connection:
            with raw_connection.cursor() as cursor:
                ready.wait(timeout=10)
                cursor.execute(
                    """
                    UPDATE portal_researchjob
                    SET status = 'RUNNING',
                        lease_token = %s,
                        lease_expires_at = CURRENT_TIMESTAMP + INTERVAL '30 seconds',
                        attempt_count = attempt_count + 1,
                        version = version + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND status = 'QUEUED'
                    RETURNING id
                    """,
                    [uuid.uuid4(), job_id],
                )
                results.put(("ok", cursor.fetchone() is not None))
    except BaseException as exc:  # pragma: no cover - surfaced in parent process
        results.put(("error", f"{type(exc).__name__}:{exc}"))


def _postgresql_backend_pid() -> int:
    with connections["default"].cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def _run_in_fresh_connection(operation: Any) -> Any:
    """Run one contender in its thread-local, independently closed DB session."""

    close_old_connections()
    try:
        return operation()
    finally:
        connections.close_all()


def _raw_postgresql_connection_settings() -> dict[str, str]:
    configured = connection.settings_dict
    options = configured.get("OPTIONS", {})
    raw_settings = {
        "dbname": str(configured["NAME"]),
        "user": str(configured["USER"]),
        "password": str(configured["PASSWORD"]),
        "host": str(configured["HOST"]),
        "port": str(configured["PORT"]),
        "sslmode": str(options.get("sslmode", "require")),
        "connect_timeout": str(options.get("connect_timeout", 5)),
    }
    if options.get("sslrootcert"):
        raw_settings["sslrootcert"] = str(options["sslrootcert"])
    return raw_settings


def _queued_job(*, owner_id: int, manifest: ManifestUpload) -> ResearchJob:
    suffix = uuid.uuid4().hex
    return ResearchJob.objects.create(
        owner_id=owner_id,
        manifest=manifest,
        capability_id=ResearchJob.Capability.PREFLIGHT,
        request_payload={"schema_version": 1, "probe": suffix},
        request_hash=f"sha256:{suffix.ljust(64, '0')[:64]}",
        idempotency_key=suffix,
        actor_id=f"postgresql-contract-{suffix}",
        actor_roles=[],
        actor_permissions=[],
    )


def test_postgresql_supports_required_database_primitives(
    runner_user: Any,
    manifest_record: ManifestUpload,
) -> None:
    assert connection.vendor == "postgresql"
    assert connection.features.supports_transactions is True
    assert connection.features.has_select_for_update is True
    assert connection.features.supports_partial_indexes is True

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT current_setting('TimeZone'),
                   current_setting('client_encoding'),
                   current_setting('statement_timeout'),
                   current_setting('lock_timeout'),
                   current_setting('idle_in_transaction_session_timeout'),
                   current_setting('application_name')
            """
        )
        session_settings = cursor.fetchone()
    assert session_settings == (
        "UTC",
        "UTF8",
        "30s",
        "5s",
        "30s",
        "market-research-web",
    )

    job = _queued_job(owner_id=runner_user.pk, manifest=manifest_record)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = %s
              AND indexname IN (%s, %s)
            """,
            [
                ResearchJob._meta.db_table,
                "portal_job_owner_active_request_uniq",
                "portal_job_one_active_uniq",
            ],
        )
        partial_indexes = dict(cursor.fetchall())
    assert set(partial_indexes) == {
        "portal_job_owner_active_request_uniq",
        "portal_job_one_active_uniq",
    }
    assert all(
        " WHERE " in definition.upper()
        for definition in partial_indexes.values()
    )

    with transaction.atomic():
        locked = ResearchJob.objects.select_for_update().get(pk=job.pk)
        assert locked.pk == job.pk

    subject_hash = uuid.uuid4().hex * 2
    with pytest.raises(_RollbackProbe):
        with transaction.atomic():
            LoginThrottle.objects.create(
                subject_hash=subject_hash,
                failure_count=1,
                window_started_at=timezone.now(),
            )
            raise _RollbackProbe
    assert not LoginThrottle.objects.filter(subject_hash=subject_hash).exists()


def test_postgresql_conditional_claim_has_exactly_one_winner(
    runner_user: Any,
    manifest_record: ManifestUpload,
    settings: Any,
    tmp_path: Any,
) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "claim-audit.jsonl"
    queued = _queued_job(owner_id=runner_user.pk, manifest=manifest_record)
    ready = Barrier(2)

    def contend(worker_id: str) -> tuple[int, uuid.UUID | None]:
        def operation() -> tuple[int, uuid.UUID | None]:
            backend_pid = _postgresql_backend_pid()
            ready.wait(timeout=10)
            claimed = claim_next_job(worker_id=worker_id)
            return backend_pid, None if claimed is None else claimed.pk

        return _run_in_fresh_connection(operation)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(contend, ("pg-worker-a", "pg-worker-b")))

    backend_pids = {backend_pid for backend_pid, _claimed_id in results}
    claimed_ids = [claimed_id for _pid, claimed_id in results if claimed_id is not None]
    assert len(backend_pids) == 2
    assert claimed_ids == [queued.pk]

    queued.refresh_from_db()
    assert queued.status == ResearchJob.Status.RUNNING
    assert queued.attempt_count == 1
    assert queued.lease_token is not None


def test_postgresql_global_experiment_id_race_has_exactly_one_winner(
    runner_user: Any,
) -> None:
    experiment_id = f"postgresql-race-{uuid.uuid4().hex}"
    ready = Barrier(2)

    def contend(suffix: str) -> tuple[int, bool]:
        def operation() -> tuple[int, bool]:
            backend_pid = _postgresql_backend_pid()
            ready.wait(timeout=10)
            try:
                ManifestUpload.objects.create(
                    owner_id=runner_user.pk,
                    display_name=f"race-{suffix}.json",
                    storage_ref=f"data:_internal_web/manifests/race-{suffix}.json",
                    content_hash=f"sha256:{suffix * 64}",
                    manifest_hash=f"sha256:{suffix.swapcase() * 64}",
                    size_bytes=64,
                    experiment_id=experiment_id,
                    strategy_name="sma_with_filter",
                )
            except IntegrityError:
                return backend_pid, False
            return backend_pid, True

        return _run_in_fresh_connection(operation)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(contend, ("a", "b")))

    assert len({backend_pid for backend_pid, _won in results}) == 2
    assert sorted(won for _backend_pid, won in results) == [False, True]
    assert ManifestUpload.objects.filter(experiment_id=experiment_id).count() == 1


def test_postgresql_os_process_conditional_claim_has_one_winner(
    runner_user: Any,
    manifest_record: ManifestUpload,
) -> None:
    queued = _queued_job(owner_id=runner_user.pk, manifest=manifest_record)
    # The supported runtime is Linux/WSL POSIX.  ``fork`` keeps Django's test
    # bootstrap out of the child while the child opens a brand-new raw psycopg
    # connection; inherited Django connections are never used there.
    context = get_context("fork")
    ready = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_process_conditional_claim,
            args=(
                _raw_postgresql_connection_settings(),
                str(queued.pk),
                ready,
                results,
            ),
        )
        for _index in range(2)
    ]
    for process in processes:
        process.start()
    try:
        outcomes = [results.get(timeout=15) for _process in processes]
    finally:
        for process in processes:
            process.join(timeout=15)
            if process.is_alive():  # pragma: no cover - defensive hang cleanup
                process.kill()
                process.join(timeout=5)

    assert [process.exitcode for process in processes] == [0, 0]
    assert all(kind == "ok" for kind, _value in outcomes), outcomes
    assert sorted(bool(value) for _kind, value in outcomes) == [False, True]
    queued.refresh_from_db()
    assert queued.status == ResearchJob.Status.RUNNING
    assert queued.attempt_count == 1


def test_postgresql_lock_timeout_fails_closed(
    runner_user: Any,
    manifest_record: ManifestUpload,
) -> None:
    queued = _queued_job(owner_id=runner_user.pk, manifest=manifest_record)
    locked = Barrier(2)
    release = Barrier(2)

    def hold_lock() -> None:
        def operation() -> None:
            with transaction.atomic():
                ResearchJob.objects.select_for_update().get(pk=queued.pk)
                locked.wait(timeout=10)
                release.wait(timeout=10)

        _run_in_fresh_connection(operation)

    def hit_timeout() -> str:
        def operation() -> str:
            locked.wait(timeout=10)
            try:
                with transaction.atomic():
                    with connections["default"].cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '100ms'")
                    ResearchJob.objects.select_for_update().get(pk=queued.pk)
            except OperationalError as exc:
                cause = getattr(exc, "__cause__", None)
                return str(getattr(cause, "sqlstate", ""))
            return "unexpected-success"

        try:
            return _run_in_fresh_connection(operation)
        finally:
            release.wait(timeout=10)

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_lock)
        contender = executor.submit(hit_timeout)
        sqlstate = contender.result(timeout=15)
        holder.result(timeout=15)

    assert sqlstate == "55P03"


def test_postgresql_deadlock_is_detected_and_one_transaction_rolls_back() -> None:
    first = LoginThrottle.objects.create(
        subject_hash=uuid.uuid4().hex * 2,
        failure_count=1,
        window_started_at=timezone.now(),
    )
    second = LoginThrottle.objects.create(
        subject_hash=uuid.uuid4().hex * 2,
        failure_count=1,
        window_started_at=timezone.now(),
    )
    ready = Barrier(2)

    def contend(first_id: int, second_id: int) -> str:
        def operation() -> str:
            try:
                with transaction.atomic():
                    LoginThrottle.objects.select_for_update().get(pk=first_id)
                    ready.wait(timeout=10)
                    LoginThrottle.objects.select_for_update().get(pk=second_id)
            except OperationalError as exc:
                cause = getattr(exc, "__cause__", None)
                return str(getattr(cause, "sqlstate", ""))
            return "committed"

        return _run_in_fresh_connection(operation)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda ids: contend(*ids),
                ((first.pk, second.pk), (second.pk, first.pk)),
            )
        )

    assert sorted(outcomes) == ["40P01", "committed"]
