from __future__ import annotations

import hashlib
import multiprocessing
import os
import signal
import uuid
from datetime import UTC, datetime, timedelta
from queue import Empty

import psycopg
import pytest
from market_research.application import ReleaseMetadata
from psycopg.types.json import Jsonb

from research_operations.admission import (
    ACTIVE,
    SUCCEEDED,
    ExperimentAdmissionStore,
)
from research_operations.backup import (
    BackupContractError,
    BackupFenceStore,
    VerifiedBackup,
    verify_live_backup_database_state,
)
from research_operations.errors import (
    ActiveExperimentConflict,
    AdmissionClaimLost,
    ClaimLost,
    ExperimentIdentityConflict,
    ExperimentRequestConflict,
    MaintenanceFenceActive,
    OutboxBindingConflict,
    OutboxReplayRejected,
)
from research_operations.migrate import apply_migrations
from research_operations.outbox import DEAD_LETTER, PENDING, PROJECTED, OutboxStore
from research_operations.worker import OutboxWorker, WorkerSettings

pytestmark = pytest.mark.postgresql
TEST_DATABASE_ENV = "RESEARCH_OPS_TEST_DATABASE_URL"
TEST_RELEASE = ReleaseMetadata(
    git_sha="1" * 40,
    release_id="integration-release",
    build_digest="sha256:" + "2" * 64,
)


def _claim_outbox_process(dsn: str, worker_id: str, output: object) -> None:
    try:
        claim = OutboxStore(dsn).claim(worker_id=worker_id, lease_seconds=30)
        output.put(("ok", None if claim is None else str(claim.event_id)))
    except BaseException as exc:
        output.put(("error", type(exc).__name__))


def _claim_experiment_process(
    dsn: str,
    experiment_id: str,
    request_id: str,
    output: object,
) -> None:
    try:
        decision = ExperimentAdmissionStore(dsn).acquire(
            authority="research-core-v2",
            experiment_id=experiment_id,
            manifest_hash="sha256:" + "a" * 64,
            request_id=request_id,
            request_hash="sha256:" + request_id[-1] * 64,
            owner_id=f"process-{request_id}",
            lease_seconds=60,
        )
        output.put(("ok", str(decision.run_id), decision.acquired))
    except BaseException as exc:
        output.put(("error", type(exc).__name__, False))


class _BlockingProjector:
    def __init__(self, started: object, release: object) -> None:
        self.started = started
        self.release = release

    def project(self, _event_id: object) -> None:
        self.started.set()
        if not self.release.wait(timeout=15):
            raise TimeoutError("test projector release timeout")


class _FailingProjector:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def project(self, _event_id: object) -> None:
        raise self.exc


def _run_blocking_worker_process(
    dsn: str,
    started: object,
    release: object,
) -> None:
    OutboxWorker(
        store=OutboxStore(dsn),
        projector=_BlockingProjector(started, release),
        settings=WorkerSettings(
            worker_id="sigterm-worker",
            poll_interval=0.05,
            lease_seconds=6,
        ),
    ).run_forever()


@pytest.fixture(scope="session")
def live_dsn() -> str:
    dsn = os.environ.get(TEST_DATABASE_ENV, "")
    if not dsn:
        pytest.skip(f"{TEST_DATABASE_ENV} is not configured")
    apply_migrations(dsn)
    return dsn


@pytest.fixture(autouse=True)
def clean_operational_state(live_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_OPS_GIT_SHA", TEST_RELEASE.git_sha)
    monkeypatch.setenv("RESEARCH_OPS_RELEASE_ID", TEST_RELEASE.release_id)
    monkeypatch.setenv("RESEARCH_OPS_BUILD_DIGEST", TEST_RELEASE.build_digest)
    monkeypatch.setenv(
        "RESEARCH_OPS_RELEASE_BUNDLE_DIGEST",
        "sha256:" + "3" * 64,
    )
    _clean(live_dsn)
    yield
    _clean(live_dsn)


def _clean(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute(
            """
            TRUNCATE research_ops.outbox_operator_action,
                     research_ops.backup_set,
                     research_ops.worker_heartbeat,
                     research_ops.outbox_delivery,
                     research_ops.research_job_result_receipt,
                     research_ops.active_experiment_claim,
                     research_ops.experiment_request,
                     research_ops.experiment_identity
            """
        )
        conn.execute(
            """
            DELETE FROM public.portal_webauditevent
            WHERE payload ->> 'actor_id' = 'research-ops-test'
            """
        )
        conn.execute(
            """
            UPDATE research_ops.runtime_control
            SET mutation_admission_open = TRUE,
                claim_admission_open = TRUE,
                integrity_quarantine = FALSE,
                fence_token = NULL,
                requested_by = '', reason = '', closed_at = NULL,
                changed_at = CURRENT_TIMESTAMP
            WHERE singleton_id = 1
            """
        )


def _audit_event(dsn: str, *, projected: bool = False) -> tuple[uuid.UUID, str]:
    event_id = uuid.uuid4()
    payload_hash = "sha256:" + uuid.uuid4().hex * 2
    payload = {
        "event_id": str(event_id),
        "actor_id": "research-ops-test",
        "action": "integration_probe",
    }
    projected_at = datetime.now(UTC) if projected else None
    projection_hash = "sha256:" + "f" * 64 if projected else ""
    with psycopg.connect(dsn) as conn:
        conn.execute(
            """
            INSERT INTO public.portal_webauditevent (
                id, payload, payload_hash, projection_row_hash,
                projected_at, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                event_id,
                Jsonb(payload),
                payload_hash,
                projection_hash,
                projected_at,
                datetime.now(UTC),
            ),
        )
    return event_id, payload_hash


def _decision_args(
    experiment_id: str,
    *,
    request_id: str = "request-a",
    manifest_character: str = "a",
    request_character: str = "b",
) -> dict[str, object]:
    return {
        "authority": "research-core-v2",
        "experiment_id": experiment_id,
        "manifest_hash": "sha256:" + manifest_character * 64,
        "request_id": request_id,
        "request_hash": "sha256:" + request_character * 64,
        "owner_id": "integration-owner",
        "lease_seconds": 30,
    }


def test_migrations_are_idempotent_and_checksummed(live_dsn: str) -> None:
    result = apply_migrations(live_dsn)
    assert result.applied == ()
    assert "0001_initial.sql" in result.already_applied
    assert "0002_runtime_control.sql" in result.already_applied
    assert "0003_research_job_receipt.sql" in result.already_applied
    assert "0004_worker_release_provenance.sql" in result.already_applied


def test_backup_source_state_binds_real_server_and_both_migration_sets(
    live_dsn: str,
) -> None:
    from research_operations.health import expected_platform_migration_digest

    assert (
        verify_live_backup_database_state(
            expected_postgresql_major=16,
            dsn=live_dsn,
        )
        == expected_platform_migration_digest()
    )
    with pytest.raises(BackupContractError, match="backup_postgresql_major_mismatch"):
        verify_live_backup_database_state(
            expected_postgresql_major=15,
            dsn=live_dsn,
        )


def test_backup_registration_persists_release_bundle_digest(live_dsn: str) -> None:
    backup_id = uuid.uuid4()
    fence_token = uuid.uuid4()
    observed_at = datetime.now(UTC)
    bundle_digest = "sha256:" + "3" * 64
    with psycopg.connect(live_dsn) as conn:
        generation = int(
            conn.execute(
                """
                UPDATE research_ops.runtime_control
                SET mutation_admission_open = FALSE,
                    claim_admission_open = FALSE,
                    integrity_quarantine = FALSE,
                    generation = generation + 1,
                    fence_token = %s,
                    requested_by = 'release-bundle-test',
                    reason = 'verify backup provenance registration',
                    closed_at = %s,
                    changed_at = %s
                WHERE singleton_id = 1
                RETURNING generation
                """,
                (fence_token, observed_at, observed_at),
            ).fetchone()[0]
        )
    verified = VerifiedBackup(
        backup_id=backup_id,
        manifest_hash="sha256:" + "4" * 64,
        git_sha=TEST_RELEASE.git_sha,
        release_id=TEST_RELEASE.release_id,
        build_digest=TEST_RELEASE.build_digest,
        release_bundle_digest=bundle_digest,
        migration_digest="sha256:" + "5" * 64,
        postgresql_major=16,
        fence_generation=generation,
        fence_token_hash=(
            "sha256:"
            + hashlib.sha256(
                b"research-operations-fence-token-v1\0" + fence_token.bytes
            ).hexdigest()
        ),
        created_at=observed_at,
        audit_row_count=0,
        audit_terminal_hash="",
        files=(),
    )

    BackupFenceStore(live_dsn).register_verified_backup(
        verified=verified,
        fence_token=fence_token,
        now=observed_at,
    )

    with psycopg.connect(live_dsn) as conn:
        row = conn.execute(
            """
            SELECT git_sha, release_id, build_digest, release_bundle_digest
            FROM research_ops.backup_set
            WHERE backup_id = %s
            """,
            (backup_id,),
        ).fetchone()
    assert row == (
        TEST_RELEASE.git_sha,
        TEST_RELEASE.release_id,
        TEST_RELEASE.build_digest,
        bundle_digest,
    )


def test_live_health_snapshot_uses_bounded_worker_freshness(live_dsn: str) -> None:
    from research_operations.health import _database_snapshot

    now = datetime.now(UTC)
    store = OutboxStore(live_dsn)
    store.worker_heartbeat(worker_id="outbox:health-test", state="IDLE", now=now)
    store.worker_heartbeat(worker_id="research-job:health-test", state="IDLE", now=now)
    snapshot = _database_snapshot(
        dsn=live_dsn,
        observed_at=now + timedelta(seconds=5),
        worker_heartbeat_max_age_seconds=30,
        expected_release=TEST_RELEASE,
        expected_release_bundle_digest="sha256:" + "3" * 64,
    )
    assert snapshot["fresh_outbox_workers"] >= 1
    assert snapshot["fresh_research_job_workers"] >= 1
    assert snapshot["worker_release_mismatch_count"] == 0
    assert snapshot["portal_migrations"] == snapshot["expected_portal_migrations"]
    with psycopg.connect(live_dsn) as conn:
        releases = conn.execute(
            """
            SELECT DISTINCT git_sha, release_id, build_digest,
                            release_bundle_digest,
                            release_seen_at = last_seen_at
            FROM research_ops.worker_heartbeat
            """
        ).fetchall()
    assert releases == [
        (
            TEST_RELEASE.git_sha,
            TEST_RELEASE.release_id,
            TEST_RELEASE.build_digest,
            "sha256:" + "3" * 64,
            True,
        )
    ]


def test_old_worker_cannot_reuse_a_new_worker_release_binding(live_dsn: str) -> None:
    from research_operations.health import _database_snapshot

    now = datetime.now(UTC)
    store = OutboxStore(live_dsn)
    store.worker_heartbeat(worker_id="outbox:rollout-test", state="IDLE", now=now)
    # Simulate the pre-provenance binary updating only its historical columns
    # while reusing the same stable worker_id during a rolling restart.
    with psycopg.connect(live_dsn) as conn:
        conn.execute(
            """
            UPDATE research_ops.worker_heartbeat
            SET last_seen_at = %s
            WHERE worker_id = 'outbox:rollout-test'
            """,
            (now + timedelta(seconds=1),),
        )

    snapshot = _database_snapshot(
        dsn=live_dsn,
        observed_at=now + timedelta(seconds=2),
        worker_heartbeat_max_age_seconds=30,
        expected_release=TEST_RELEASE,
        expected_release_bundle_digest="sha256:" + "3" * 64,
    )
    assert snapshot["worker_release_mismatch_count"] == 1


def test_worker_release_bundle_mismatch_blocks_readiness(live_dsn: str) -> None:
    from research_operations.health import _database_snapshot

    now = datetime.now(UTC)
    OutboxStore(live_dsn).worker_heartbeat(
        worker_id="research-job:bundle-rollout-test",
        state="IDLE",
        now=now,
    )
    snapshot = _database_snapshot(
        dsn=live_dsn,
        observed_at=now + timedelta(seconds=1),
        worker_heartbeat_max_age_seconds=30,
        expected_release=TEST_RELEASE,
        expected_release_bundle_digest="sha256:" + "4" * 64,
    )
    assert snapshot["worker_release_mismatch_count"] == 1


def test_scanner_is_idempotent_and_preserves_projected_marker(live_dsn: str) -> None:
    pending_id, _ = _audit_event(live_dsn)
    projected_id, _ = _audit_event(live_dsn, projected=True)
    store = OutboxStore(live_dsn)
    assert store.scan() == 2
    assert store.scan() == 0
    with psycopg.connect(live_dsn) as conn:
        rows = dict(
            conn.execute(
                """
                SELECT event_id, status FROM research_ops.outbox_delivery
                WHERE event_id IN (%s, %s)
                """,
                (pending_id, projected_id),
            ).fetchall()
        )
    assert rows[pending_id] == PENDING
    assert rows[projected_id] == PROJECTED


def test_scanner_rejects_same_event_with_changed_binding(live_dsn: str) -> None:
    event_id, _ = _audit_event(live_dsn)
    store = OutboxStore(live_dsn)
    store.scan()
    with psycopg.connect(live_dsn) as conn:
        conn.execute(
            """
            UPDATE research_ops.outbox_delivery
            SET payload_hash = %s WHERE event_id = %s
            """,
            ("sha256:" + "e" * 64, event_id),
        )
    with pytest.raises(OutboxBindingConflict):
        store.scan()


def test_two_os_processes_have_one_outbox_claim_winner(live_dsn: str) -> None:
    event_id, _ = _audit_event(live_dsn)
    OutboxStore(live_dsn).scan()
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    processes = [
        context.Process(
            target=_claim_outbox_process,
            args=(live_dsn, f"worker-{index}", output),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    results = [output.get(timeout=2) for _ in processes]
    assert sorted(result[1] is not None for result in results) == [False, True]
    assert {result[1] for result in results if result[1]} == {str(event_id)}
    recovered = OutboxStore(live_dsn).claim(
        worker_id="recovery-worker",
        lease_seconds=30,
        now=datetime.now(UTC) + timedelta(seconds=31),
    )
    assert recovered is not None
    assert recovered.event_id == event_id
    assert recovered.fencing_token == 2


def test_expired_outbox_claim_is_fenced_and_recovered(live_dsn: str) -> None:
    event_id, _ = _audit_event(live_dsn)
    store = OutboxStore(live_dsn)
    store.scan()
    start = datetime.now(UTC)
    first = store.claim(worker_id="first", lease_seconds=10, now=start)
    assert first is not None and first.event_id == event_id
    second = store.claim(
        worker_id="second",
        lease_seconds=10,
        now=start + timedelta(seconds=11),
    )
    assert second is not None
    assert second.fencing_token == first.fencing_token + 1
    with pytest.raises(ClaimLost):
        store.mark_projected(first, now=start + timedelta(seconds=12))
    store.mark_projected(second, now=start + timedelta(seconds=12))
    assert store.metrics(now=start + timedelta(seconds=12)).pending_count == 0


def test_permanent_failure_dead_letters_and_bound_requeue(live_dsn: str) -> None:
    event_id, payload_hash = _audit_event(live_dsn)
    store = OutboxStore(live_dsn)
    store.scan()
    claim = store.claim(worker_id="worker")
    assert claim is not None
    disposition = store.record_failure(
        claim,
        category="permanent_contract",
        error="malformed payload",
        permanent=True,
        max_attempts=8,
        retry_delay_seconds=1,
    )
    assert disposition.status == DEAD_LETTER
    with pytest.raises(OutboxReplayRejected):
        store.requeue_dead_letter(
            event_id=event_id,
            expected_payload_hash="sha256:" + "0" * 64,
            operator_id="operator",
            reason="wrong binding",
        )
    store.requeue_dead_letter(
        event_id=event_id,
        expected_payload_hash=payload_hash,
        operator_id="operator",
        reason="source corrected and independently reviewed",
    )
    assert store.metrics().pending_count == 1


def test_worker_classifies_transient_and_permanent_projection_errors(
    live_dsn: str,
) -> None:
    transient_id, _ = _audit_event(live_dsn)
    transient_worker = OutboxWorker(
        store=OutboxStore(live_dsn),
        projector=_FailingProjector(OSError("temporary file busy")),
        settings=WorkerSettings(worker_id="transient-worker", lease_seconds=6),
    )
    assert transient_worker.run_one() is True
    with psycopg.connect(live_dsn) as conn:
        transient = conn.execute(
            """
            SELECT status, attempt_count, last_error_category
            FROM research_ops.outbox_delivery WHERE event_id = %s
            """,
            (transient_id,),
        ).fetchone()
    assert transient == (PENDING, 1, "transient_dependency")

    permanent_id, _ = _audit_event(live_dsn)
    permanent_worker = OutboxWorker(
        store=OutboxStore(live_dsn),
        projector=_FailingProjector(ValueError("malformed event")),
        settings=WorkerSettings(worker_id="permanent-worker", lease_seconds=6),
    )
    assert permanent_worker.run_one() is True
    with psycopg.connect(live_dsn) as conn:
        permanent = conn.execute(
            """
            SELECT status, last_error_category
            FROM research_ops.outbox_delivery WHERE event_id = %s
            """,
            (permanent_id,),
        ).fetchone()
    assert permanent == (DEAD_LETTER, "permanent_contract")


def test_worker_sigterm_drains_current_event_then_stops(live_dsn: str) -> None:
    event_id, _ = _audit_event(live_dsn)
    OutboxStore(live_dsn).scan()
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    release = context.Event()
    process = context.Process(
        target=_run_blocking_worker_process,
        args=(live_dsn, started, release),
    )
    process.start()
    assert started.wait(timeout=10)
    os.kill(process.pid, signal.SIGTERM)
    assert process.is_alive()
    release.set()
    process.join(timeout=15)
    assert process.exitcode == 0
    with psycopg.connect(live_dsn) as conn:
        delivery_status = conn.execute(
            """
            SELECT status FROM research_ops.outbox_delivery WHERE event_id = %s
            """,
            (event_id,),
        ).fetchone()[0]
        worker_status = conn.execute(
            """
            SELECT state FROM research_ops.worker_heartbeat
            WHERE worker_id = 'sigterm-worker'
            """
        ).fetchone()[0]
    assert delivery_status == PROJECTED
    assert worker_status == "STOPPED"


def test_backup_fence_blocks_scanner_claim_and_admission(live_dsn: str) -> None:
    with psycopg.connect(live_dsn) as conn:
        conn.execute(
            """
            UPDATE research_ops.runtime_control
            SET mutation_admission_open = FALSE,
                claim_admission_open = FALSE,
                fence_token = %s,
                requested_by = 'integration-test',
                reason = 'prove claim fence',
                closed_at = CURRENT_TIMESTAMP
            WHERE singleton_id = 1
            """,
            (uuid.uuid4(),),
        )
    with pytest.raises(MaintenanceFenceActive):
        OutboxStore(live_dsn).scan()
    with pytest.raises(MaintenanceFenceActive):
        ExperimentAdmissionStore(live_dsn).acquire(
            **_decision_args(f"fenced-{uuid.uuid4().hex}")
        )


def test_draining_fence_allows_outbox_drain_but_blocks_new_experiment(
    live_dsn: str,
) -> None:
    _audit_event(live_dsn)
    with psycopg.connect(live_dsn) as conn:
        conn.execute(
            """
            UPDATE research_ops.runtime_control
            SET mutation_admission_open = FALSE,
                claim_admission_open = TRUE,
                fence_token = %s,
                requested_by = 'integration-test',
                reason = 'drain committed outbox',
                closed_at = CURRENT_TIMESTAMP
            WHERE singleton_id = 1
            """,
            (uuid.uuid4(),),
        )
    outbox = OutboxStore(live_dsn)
    assert outbox.scan() == 1
    assert outbox.claim(worker_id="draining-worker") is not None
    with pytest.raises(MaintenanceFenceActive):
        outbox.requeue_dead_letter(
            event_id=uuid.uuid4(),
            expected_payload_hash="sha256:" + "a" * 64,
            operator_id="backup-test",
            reason="must remain sealed",
        )
    with pytest.raises(MaintenanceFenceActive):
        ExperimentAdmissionStore(live_dsn).acquire(
            **_decision_args(f"draining-{uuid.uuid4().hex}")
        )


def test_admission_idempotency_conflicts_and_result_reuse(live_dsn: str) -> None:
    store = ExperimentAdmissionStore(live_dsn)
    experiment_id = f"experiment-{uuid.uuid4().hex}"
    args = _decision_args(experiment_id)
    first = store.acquire(**args)
    repeated = store.acquire(**args)
    assert first.status == ACTIVE and first.acquired is True
    assert repeated.acquired is False
    assert repeated.lease_token is None
    assert repeated.fencing_token is None
    observed = store.status(
        authority=args["authority"],
        experiment_id=args["experiment_id"],
        request_id=args["request_id"],
    )
    assert observed is not None
    assert observed.lease_token is None
    assert observed.fencing_token is None
    with pytest.raises(ExperimentRequestConflict):
        store.acquire(**{**args, "request_hash": "sha256:" + "c" * 64})
    with pytest.raises(ExperimentIdentityConflict):
        store.acquire(
            **{
                **args,
                "request_id": "request-other-manifest",
                "manifest_hash": "sha256:" + "d" * 64,
            }
        )
    with pytest.raises(ActiveExperimentConflict):
        store.acquire(
            **{
                **args,
                "request_id": "request-b",
                "request_hash": "sha256:" + "e" * 64,
            }
        )
    completed = store.complete(
        first,
        result_ref="reports:immutable/result.json",
        result_hash="sha256:" + "f" * 64,
    )
    assert completed.status == SUCCEEDED
    reused = store.acquire(**args)
    assert reused.is_reused_result
    assert reused.run_id == first.run_id
    assert reused.result_hash == "sha256:" + "f" * 64


def test_web_and_cli_requests_share_one_active_namespace(live_dsn: str) -> None:
    store = ExperimentAdmissionStore(live_dsn)
    experiment_id = f"cross-adapter-{uuid.uuid4().hex}"
    web = store.acquire(
        authority="market-research:experiment:v1",
        experiment_id=experiment_id,
        manifest_hash="sha256:" + "a" * 64,
        request_id="web-job:00000000-0000-0000-0000-000000000001",
        request_hash="sha256:" + "b" * 64,
        owner_id="web-owner:1",
    )
    assert web.acquired
    with pytest.raises(ActiveExperimentConflict):
        store.acquire(
            authority="market-research:experiment:v1",
            experiment_id=experiment_id,
            manifest_hash="sha256:" + "a" * 64,
            request_id="cli:request-1",
            request_hash="sha256:" + "c" * 64,
            owner_id="cli:operator-1",
        )


def test_fenced_job_result_receipt_is_atomic_and_recoverable(live_dsn: str) -> None:
    store = ExperimentAdmissionStore(live_dsn)
    experiment_id = f"receipt-{uuid.uuid4().hex}"
    decision = store.acquire(**_decision_args(experiment_id))
    job_id = uuid.uuid4()
    completed = store.complete_research_job(
        decision,
        job_id=job_id,
        result_ref="report:_internal_web/job/result.json",
        result_hash="sha256:" + "d" * 64,
        research_outcome="PASS",
        core_run_id="core-run-1",
    )
    assert completed.status == SUCCEEDED
    receipt = store.research_job_receipt(job_id)
    assert receipt is not None
    assert receipt.fencing_token == decision.fencing_token
    assert receipt.result_hash == "sha256:" + "d" * 64
    assert receipt.applied_at is None
    store.mark_research_job_receipt_applied(
        job_id=job_id,
        result_hash=receipt.result_hash,
    )
    assert store.research_job_receipt(job_id).applied_at is not None


def test_expired_admission_rejects_stale_result_publish(live_dsn: str) -> None:
    store = ExperimentAdmissionStore(live_dsn)
    experiment_id = f"expiry-{uuid.uuid4().hex}"
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first = store.acquire(**_decision_args(experiment_id), now=start)
    takeover = store.acquire(
        **_decision_args(
            experiment_id,
            request_id="request-b",
            request_character="c",
        ),
        now=start + timedelta(seconds=31),
    )
    assert takeover.fencing_token == first.fencing_token + 1
    with pytest.raises(AdmissionClaimLost):
        store.complete(
            first,
            result_ref="reports:stale.json",
            result_hash="sha256:" + "d" * 64,
            now=start + timedelta(seconds=32),
        )
    store.complete(
        takeover,
        result_ref="reports:winner.json",
        result_hash="sha256:" + "e" * 64,
        now=start + timedelta(seconds=32),
    )


def test_two_os_processes_cannot_enter_same_experiment_namespace(
    live_dsn: str,
) -> None:
    experiment_id = f"multiprocess-{uuid.uuid4().hex}"
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    processes = [
        context.Process(
            target=_claim_experiment_process,
            args=(live_dsn, experiment_id, f"request-{suffix}", output),
        )
        for suffix in ("a", "b")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    try:
        results = [output.get(timeout=2) for _ in processes]
    except Empty as exc:
        raise AssertionError("child process did not report admission result") from exc
    assert sorted(result[0] for result in results) == ["error", "ok"]
    assert {result[1] for result in results if result[0] == "error"} == {
        "ActiveExperimentConflict"
    }
