from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
import pytest

from research_operations.alerting import (
    ACKNOWLEDGED,
    DELIVERED,
    RESOLVED,
    LoopbackOrHttpsAlertTransport,
    ServiceAlertStore,
)
from research_operations.errors import (
    AlertBindingConflict,
    AlertDeliveryClaimLost,
    AlertStateConflict,
)
from research_operations.metrics import collect_metrics
from research_operations.migrate import apply_migrations

pytestmark = pytest.mark.postgresql
TEST_DATABASE_ENV = "RESEARCH_OPS_TEST_DATABASE_URL"


class _AlertReceiver(BaseHTTPRequestHandler):
    received: list[tuple[dict[str, object], str]] = []

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.received.append((payload, self.headers.get("Idempotency-Key", "")))
        self.send_response(202)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture(scope="session")
def alert_live_dsn() -> str:
    dsn = os.environ.get(TEST_DATABASE_ENV, "")
    if not dsn:
        pytest.skip(f"{TEST_DATABASE_ENV} is not configured")
    apply_migrations(dsn)
    return dsn


@pytest.fixture(autouse=True)
def clean_alert_state(alert_live_dsn: str) -> None:
    with psycopg.connect(alert_live_dsn) as conn:
        conn.execute(
            """
            TRUNCATE research_ops.service_alert_event,
                     research_ops.service_alert_delivery,
                     research_ops.service_alert
            """
        )
    yield
    with psycopg.connect(alert_live_dsn) as conn:
        conn.execute(
            """
            TRUNCATE research_ops.service_alert_event,
                     research_ops.service_alert_delivery,
                     research_ops.service_alert
            """
        )


@pytest.fixture
def actual_alert_receiver() -> str:
    _AlertReceiver.received.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AlertReceiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/alerts"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _raise(
    store: ServiceAlertStore,
    *,
    key: str,
    now: datetime,
    severity: str = "CRITICAL",
):
    return store.raise_alert(
        idempotency_key=key,
        condition_code="database_unavailable",
        severity=severity,
        source_actor_id="health-probe:database",
        endpoint_id="primary-oncall",
        acknowledgment_timeout_seconds=5,
        now=now,
    )


def _deliver_one(
    store: ServiceAlertStore,
    *,
    receiver_url: str,
    now: datetime,
):
    claim = store.claim_delivery(
        worker_id="service-alert-worker:integration",
        lease_seconds=30,
        now=now,
    )
    assert claim is not None
    response_code = LoopbackOrHttpsAlertTransport(receiver_url).send(claim)
    store.mark_delivered(
        claim,
        response_code=response_code,
        now=now + timedelta(seconds=1),
    )
    return claim


def test_actual_delivery_acknowledgment_and_due_escalation_are_durable(
    alert_live_dsn: str,
    actual_alert_receiver: str,
) -> None:
    store = ServiceAlertStore(alert_live_dsn)
    base = datetime.now(UTC).replace(microsecond=100_000)

    acknowledged = _raise(store, key="database-primary-incident", now=base)
    first_claim = _deliver_one(
        store,
        receiver_url=actual_alert_receiver,
        now=base + timedelta(seconds=1),
    )
    acknowledged = store.acknowledge(
        alert_id=acknowledged.alert_id,
        actor_id="operator:alice",
        reason_code="incident_owned",
        now=base + timedelta(seconds=3),
    )
    assert acknowledged.status == ACKNOWLEDGED
    assert (
        store.escalate_due(
            actor_id="service-alert-escalator",
            endpoint_id="secondary-oncall",
            repeat_after_seconds=30,
            now=base + timedelta(seconds=6),
        )
        is None
    )

    unacknowledged = _raise(
        store,
        key="database-secondary-incident",
        now=base + timedelta(seconds=10),
    )
    _deliver_one(
        store,
        receiver_url=actual_alert_receiver,
        now=base + timedelta(seconds=11),
    )
    escalated = store.escalate_due(
        actor_id="service-alert-escalator",
        endpoint_id="secondary-oncall",
        repeat_after_seconds=30,
        now=base + timedelta(seconds=16),
    )
    assert escalated is not None
    assert escalated.alert_id == unacknowledged.alert_id
    assert escalated.escalation_level == 1
    escalation_claim = _deliver_one(
        store,
        receiver_url=actual_alert_receiver,
        now=base + timedelta(seconds=17),
    )
    assert escalation_claim.escalation_level == 1
    acknowledged_after_escalation = store.acknowledge(
        alert_id=unacknowledged.alert_id,
        actor_id="operator:bob",
        reason_code="incident_owned",
        now=base + timedelta(seconds=19),
    )
    resolved = store.resolve(
        alert_id=unacknowledged.alert_id,
        actor_id="operator:bob",
        reason_code="service_recovered",
        now=base + timedelta(seconds=20),
    )

    assert acknowledged_after_escalation.status == ACKNOWLEDGED
    assert resolved.status == RESOLVED
    assert len(_AlertReceiver.received) == 3
    assert _AlertReceiver.received[0][0]["alert_id"] == str(acknowledged.alert_id)
    assert _AlertReceiver.received[2][0]["escalation_level"] == 1
    assert _AlertReceiver.received[2][1] == escalation_claim.delivery_key
    assert store.verify_event_chain(acknowledged.alert_id).startswith("sha256:")
    assert store.verify_event_chain(resolved.alert_id) == resolved.last_event_hash
    assert [event.event_type for event in store.events(resolved.alert_id)] == [
        "OPENED",
        "DELIVERY_CLAIMED",
        "DELIVERED",
        "ESCALATED",
        "DELIVERY_CLAIMED",
        "DELIVERED",
        "ACKNOWLEDGED",
        "RESOLVED",
    ]
    with psycopg.connect(alert_live_dsn) as conn:
        delivered_count = conn.execute(
            """
            SELECT count(*) FROM research_ops.service_alert_delivery
            WHERE status = %s
            """,
            (DELIVERED,),
        ).fetchone()[0]
    assert delivered_count == 3
    assert first_claim.attempt_count == 1


def test_raise_and_acknowledge_retries_converge_but_conflicts_fail_closed(
    alert_live_dsn: str,
) -> None:
    store = ServiceAlertStore(alert_live_dsn)
    base = datetime.now(UTC)
    first = _raise(store, key="idempotent-service-alert", now=base)
    replay = _raise(
        store,
        key="idempotent-service-alert",
        now=base + timedelta(minutes=1),
    )
    assert replay.alert_id == first.alert_id
    assert replay.opened_at == first.opened_at
    with pytest.raises(AlertBindingConflict, match="binding_conflict"):
        _raise(
            store,
            key="idempotent-service-alert",
            now=base + timedelta(minutes=2),
            severity="WARNING",
        )

    acknowledged = store.acknowledge(
        alert_id=first.alert_id,
        actor_id="operator:alice",
        reason_code="incident_owned",
        now=base + timedelta(seconds=1),
    )
    acknowledgment_replay = store.acknowledge(
        alert_id=first.alert_id,
        actor_id="operator:alice",
        reason_code="incident_owned",
        now=base + timedelta(seconds=2),
    )
    assert acknowledgment_replay.last_event_hash == acknowledged.last_event_hash
    with pytest.raises(AlertStateConflict, match="acknowledgment_conflict"):
        store.acknowledge(
            alert_id=first.alert_id,
            actor_id="operator:bob",
            reason_code="incident_owned",
            now=base + timedelta(seconds=3),
        )
    source_owned = store.raise_alert(
        idempotency_key="actor-separation",
        condition_code="database_unavailable",
        severity="CRITICAL",
        source_actor_id="operator:source",
        endpoint_id="primary-oncall",
        acknowledgment_timeout_seconds=5,
        now=base + timedelta(minutes=4),
    )
    # The source health probe cannot approve its own incident.
    with pytest.raises(AlertStateConflict, match="actor_separation"):
        store.acknowledge(
            alert_id=source_owned.alert_id,
            actor_id="operator:source",
            reason_code="incident_owned",
            now=base + timedelta(minutes=4, seconds=1),
        )


def test_stale_delivery_claim_cannot_publish_and_events_are_append_only(
    alert_live_dsn: str,
) -> None:
    store = ServiceAlertStore(alert_live_dsn)
    base = datetime.now(UTC)
    alert = _raise(store, key="stale-service-alert", now=base)
    claim = store.claim_delivery(
        worker_id="service-alert-worker:stale",
        lease_seconds=3,
        now=base + timedelta(seconds=1),
    )
    assert claim is not None
    with pytest.raises(AlertDeliveryClaimLost, match="claim_lost"):
        store.mark_delivered(
            claim,
            response_code=202,
            now=base + timedelta(seconds=5),
        )

    with (
        psycopg.connect(alert_live_dsn) as conn,
        pytest.raises(
            psycopg.errors.ObjectNotInPrerequisiteState,
            match="service_alert_event_append_only",
        ),
    ):
        conn.execute(
            """
            UPDATE research_ops.service_alert_event
            SET reason_code = 'tampered'
            WHERE alert_id = %s
            """,
            (alert.alert_id,),
        )


def test_alert_metrics_are_label_free_bounded_counts(alert_live_dsn: str) -> None:
    store = ServiceAlertStore(alert_live_dsn)
    base = datetime.now(UTC)
    _raise(store, key="metrics-service-alert", now=base)

    values = collect_metrics(
        dsn=alert_live_dsn,
        observed_at=base + timedelta(seconds=6),
    )

    assert values["research_ops_snapshot_collection_success"] == 1
    assert values["research_ops_service_alert_open"] == 1
    assert values["research_ops_service_alert_unacknowledged_due"] == 1
    assert values["research_ops_service_alert_delivery_pending"] == 1
    assert values["research_ops_service_alert_delivery_failed"] == 0


def test_concurrent_exact_raise_and_due_escalation_converge(
    alert_live_dsn: str,
) -> None:
    store = ServiceAlertStore(alert_live_dsn)
    base = datetime.now(UTC)
    raise_barrier = threading.Barrier(2)

    def raise_concurrently():
        raise_barrier.wait(timeout=5)
        return _raise(store, key="concurrent-service-alert", now=base)

    with ThreadPoolExecutor(max_workers=2) as pool:
        raised = tuple(pool.map(lambda _index: raise_concurrently(), range(2)))

    assert raised[0].alert_id == raised[1].alert_id
    with psycopg.connect(alert_live_dsn) as conn:
        counts = conn.execute(
            """
            SELECT
                (SELECT count(*) FROM research_ops.service_alert),
                (SELECT count(*) FROM research_ops.service_alert_delivery),
                (SELECT count(*) FROM research_ops.service_alert_event)
            """
        ).fetchone()
    assert counts == (1, 1, 1)

    escalation_barrier = threading.Barrier(2)

    def escalate_concurrently():
        escalation_barrier.wait(timeout=5)
        return store.escalate_due(
            actor_id="service-alert-escalator",
            endpoint_id="secondary-oncall",
            repeat_after_seconds=30,
            now=base + timedelta(seconds=6),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        escalations = tuple(pool.map(lambda _index: escalate_concurrently(), range(2)))

    assert sum(item is not None for item in escalations) == 1
    with psycopg.connect(alert_live_dsn) as conn:
        alert_state = conn.execute(
            """
            SELECT escalation_level,
                   (SELECT count(*)
                    FROM research_ops.service_alert_delivery),
                   (SELECT count(*)
                    FROM research_ops.service_alert_event
                    WHERE event_type = 'ESCALATED')
            FROM research_ops.service_alert
            """
        ).fetchone()
    assert alert_state == (1, 2, 1)
