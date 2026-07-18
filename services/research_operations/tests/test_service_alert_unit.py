from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from research_operations.alerting import (
    AlertDeliveryClaim,
    LoopbackOrHttpsAlertTransport,
    ServiceAlertStore,
)
from research_operations.errors import AlertTransportError


class _Receiver(BaseHTTPRequestHandler):
    received: list[tuple[dict[str, object], str]] = []

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://example.invalid/alerts")
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.received.append((payload, self.headers.get("Idempotency-Key", "")))
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"accepted":true}')

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def receiver_url() -> str:
    _Receiver.received.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _claim() -> AlertDeliveryClaim:
    observed_at = datetime.now(UTC)
    alert_id = uuid.uuid4()
    delivery_id = uuid.uuid4()
    return AlertDeliveryClaim(
        delivery_id=delivery_id,
        alert_id=alert_id,
        delivery_key=f"service-alert:{alert_id}:level:0:primary",
        endpoint_id="primary",
        escalation_level=0,
        worker_id="alert-worker:test",
        lease_token=uuid.uuid4(),
        fencing_token=1,
        lease_expires_at=observed_at + timedelta(seconds=30),
        attempt_count=1,
        condition_code="database_unavailable",
        severity="CRITICAL",
        opened_at=observed_at,
    )


def test_transport_posts_bounded_idempotent_envelope_to_real_loopback_receiver(
    receiver_url: str,
) -> None:
    claim = _claim()

    response_code = LoopbackOrHttpsAlertTransport(receiver_url + "/alerts").send(claim)

    assert response_code == 202
    assert len(_Receiver.received) == 1
    payload, idempotency_header = _Receiver.received[0]
    assert payload == {
        "alert_id": str(claim.alert_id),
        "condition_code": "database_unavailable",
        "delivery_id": str(claim.delivery_id),
        "escalation_level": 0,
        "idempotency_key": claim.delivery_key,
        "opened_at": claim.opened_at.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
        "schema_version": 1,
        "severity": "CRITICAL",
    }
    assert idempotency_header == claim.delivery_key
    assert "actor" not in json.dumps(payload)
    assert "path" not in json.dumps(payload)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/alerts",
        "http://127.0.0.1/alerts?token=secret",
        "https://user:password@example.com/alerts",
        "ftp://127.0.0.1/alerts",
        " https://example.com/alerts",
    ],
)
def test_transport_requires_https_or_literal_loopback_and_secret_free_url(
    url: str,
) -> None:
    with pytest.raises(ValueError, match="alert_endpoint_url"):
        LoopbackOrHttpsAlertTransport(url)


def test_transport_does_not_follow_redirects(receiver_url: str) -> None:
    with pytest.raises(AlertTransportError, match="alert_delivery_http_error"):
        LoopbackOrHttpsAlertTransport(receiver_url + "/redirect").send(_claim())
    assert _Receiver.received == []


def test_alert_store_rejects_non_service_health_condition_before_database_use() -> None:
    with pytest.raises(ValueError, match="condition_not_allowed"):
        ServiceAlertStore("postgresql://not-used").raise_alert(
            idempotency_key="forbidden-domain-probe",
            condition_code="account_state_changed",
            severity="CRITICAL",
            source_actor_id="health-probe:test",
            endpoint_id="primary-oncall",
            acknowledgment_timeout_seconds=60,
        )
