from __future__ import annotations

import argparse
import errno
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

import research_operations.alert_worker as alert_worker_module
import research_operations.alerting as alerting_module
import research_operations.commands as commands_module
from research_operations.alert_worker import (
    AlertWorkerRunResult,
    ServiceAlertWorker,
    ServiceAlertWorkerSettings,
    bounded_alert_retry_delay,
    emergency_database_delivery_claim,
    evaluated_service_conditions,
    stable_health_alert_key,
)
from research_operations.alerting import (
    ALLOWED_SERVICE_CONDITIONS,
    AlertDeliveryClaim,
    ServiceAlert,
)
from research_operations.errors import AlertTransportError
from research_operations.health import (
    CheckResult,
    HealthSnapshot,
    is_database_connectivity_error,
)


def _environment() -> dict[str, str]:
    return {
        "RESEARCH_OPS_RELEASE_ID": "release-2026.07.29",
        "RESEARCH_OPS_ALERT_WORKER_ID": "service-alert:test-1",
        "RESEARCH_OPS_ALERT_PRIMARY_ENDPOINT_ID": "primary-oncall",
        "RESEARCH_OPS_ALERT_ESCALATION_ENDPOINT_ID": "secondary-oncall",
        "RESEARCH_OPS_ALERT_SOURCE_ACTOR_ID": "service-health:evaluator",
        "RESEARCH_OPS_ALERT_ESCALATION_ACTOR_ID": "service-alert:escalator",
        "RESEARCH_OPS_ALERT_POLL_INTERVAL_SECONDS": "0.05",
        "RESEARCH_OPS_ALERT_TRANSPORT_TIMEOUT_SECONDS": "5",
        "RESEARCH_OPS_ALERT_LEASE_SECONDS": "30",
        "RESEARCH_OPS_ALERT_MAX_ATTEMPTS": "8",
        "RESEARCH_OPS_ALERT_RETRY_DELAY_SECONDS": "30",
        "RESEARCH_OPS_ALERT_ACKNOWLEDGMENT_TIMEOUT_SECONDS": "300",
        "RESEARCH_OPS_ALERT_ESCALATION_REPEAT_SECONDS": "300",
        "RESEARCH_OPS_ALERT_MAXIMUM_LEVEL": "3",
        "RESEARCH_OPS_ALERT_MAXIMUM_EVALUATED_PER_CYCLE": "16",
        "RESEARCH_OPS_ALERT_MAXIMUM_DELIVERIES_PER_CYCLE": "8",
        "RESEARCH_OPS_ALERT_MAXIMUM_ESCALATIONS_PER_CYCLE": "8",
    }


def _service_alert(
    observed_at: datetime,
    *,
    condition_code: str = "readiness_failed",
) -> ServiceAlert:
    alert_id = uuid.uuid4()
    return ServiceAlert(
        alert_id=alert_id,
        idempotency_key=f"service-health:v1:release:{condition_code}",
        binding_hash="sha256:" + "a" * 64,
        condition_code=condition_code,
        severity="CRITICAL",
        source_actor_id="service-health:evaluator",
        status="OPEN",
        opened_at=observed_at,
        acknowledgment_deadline_at=observed_at + timedelta(seconds=300),
        acknowledged_by="",
        acknowledgment_reason="",
        acknowledged_at=None,
        resolved_by="",
        resolution_reason="",
        resolved_at=None,
        escalation_level=0,
        last_event_hash="sha256:" + "b" * 64,
        updated_at=observed_at,
    )


def _delivery_claim(
    observed_at: datetime,
    *,
    attempt_count: int,
    sequence: int,
) -> AlertDeliveryClaim:
    alert_id = uuid.uuid4()
    return AlertDeliveryClaim(
        delivery_id=uuid.uuid4(),
        alert_id=alert_id,
        delivery_key=f"service-alert:{alert_id}:level:0:primary-{sequence}",
        endpoint_id="primary-oncall",
        escalation_level=0,
        worker_id="service-alert:test-1",
        lease_token=uuid.uuid4(),
        fencing_token=sequence,
        lease_expires_at=observed_at + timedelta(seconds=30),
        attempt_count=attempt_count,
        condition_code="database_unavailable",
        severity="CRITICAL",
        opened_at=observed_at,
    )


class _Store:
    def __init__(
        self,
        observed_at: datetime,
        *,
        escalations: list[ServiceAlert | None] | None = None,
        claims: list[AlertDeliveryClaim | None] | None = None,
    ) -> None:
        self.observed_at = observed_at
        self.raises: list[dict[str, Any]] = []
        self.escalations = list(escalations or [None])
        self.escalation_calls: list[dict[str, Any]] = []
        self.claims = list(claims or [None])
        self.claim_calls: list[dict[str, Any]] = []
        self.delivered: list[tuple[AlertDeliveryClaim, int]] = []
        self.failures: list[tuple[AlertDeliveryClaim, int]] = []

    def raise_condition_episode(self, **kwargs: Any) -> ServiceAlert:
        self.raises.append(kwargs)
        return _service_alert(
            self.observed_at,
            condition_code=str(kwargs["condition_code"]),
        )

    def escalate_due(self, **kwargs: Any) -> ServiceAlert | None:
        self.escalation_calls.append(kwargs)
        return self.escalations.pop(0) if self.escalations else None

    def claim_delivery(self, **kwargs: Any) -> AlertDeliveryClaim | None:
        self.claim_calls.append(kwargs)
        return self.claims.pop(0) if self.claims else None

    def mark_delivered(
        self,
        claim: AlertDeliveryClaim,
        *,
        response_code: int,
        now: datetime | None = None,
    ) -> None:
        del now
        self.delivered.append((claim, response_code))

    def record_delivery_failure(
        self,
        claim: AlertDeliveryClaim,
        *,
        reason_code: str,
        max_attempts: int,
        retry_delay_seconds: int,
        now: datetime | None = None,
    ) -> str:
        del reason_code, now
        self.failures.append((claim, retry_delay_seconds))
        return "FAILED" if claim.attempt_count >= max_attempts else "PENDING"


class _Transport:
    def __init__(
        self,
        *,
        failures: set[uuid.UUID] | None = None,
        on_send: Any | None = None,
    ) -> None:
        self.failures = failures or set()
        self.on_send = on_send
        self.sent: list[AlertDeliveryClaim] = []

    def send(self, claim: AlertDeliveryClaim) -> int:
        self.sent.append(claim)
        if self.on_send is not None:
            self.on_send()
        if claim.delivery_id in self.failures:
            raise AlertTransportError("alert_delivery_http_error")
        return 202


def _collector(snapshot: HealthSnapshot):
    def collect(
        _kind: str,
        *,
        observed_at: datetime | None = None,
        use_cache: bool = True,
    ) -> HealthSnapshot:
        del observed_at
        assert use_cache is False
        return snapshot

    return collect


class _UnavailableStore(_Store):
    def __init__(
        self,
        observed_at: datetime,
        *,
        error: BaseException | None = None,
    ) -> None:
        super().__init__(observed_at)
        self.error = error or psycopg.errors.ConnectionFailure("connection lost")

    def raise_condition_episode(self, **kwargs: Any) -> ServiceAlert:
        self.raises.append(kwargs)
        raise self.error


def test_worker_settings_are_explicit_bounded_and_actor_separated() -> None:
    settings = ServiceAlertWorkerSettings.from_environ(_environment())

    assert settings.maximum_deliveries_per_cycle == 8
    assert settings.poll_interval_seconds == 0.05

    missing = _environment()
    missing.pop("RESEARCH_OPS_ALERT_WORKER_ID")
    with pytest.raises(
        ValueError,
        match="configuration_missing:RESEARCH_OPS_ALERT_WORKER_ID",
    ):
        ServiceAlertWorkerSettings.from_environ(missing)

    unsafe = _environment()
    unsafe["RESEARCH_OPS_ALERT_PRIMARY_ENDPOINT_ID"] = "../receiver"
    with pytest.raises(ValueError, match="alert_primary_endpoint_id_invalid"):
        ServiceAlertWorkerSettings.from_environ(unsafe)

    same_actor = _environment()
    same_actor["RESEARCH_OPS_ALERT_ESCALATION_ACTOR_ID"] = same_actor[
        "RESEARCH_OPS_ALERT_SOURCE_ACTOR_ID"
    ]
    with pytest.raises(ValueError, match="alert_actor_separation_invalid"):
        ServiceAlertWorkerSettings.from_environ(same_actor)

    unsafe_lease = _environment()
    unsafe_lease["RESEARCH_OPS_ALERT_LEASE_SECONDS"] = "3"
    unsafe_lease["RESEARCH_OPS_ALERT_TRANSPORT_TIMEOUT_SECONDS"] = "30"
    with pytest.raises(ValueError, match="alert_lease_transport_window_invalid"):
        ServiceAlertWorkerSettings.from_environ(unsafe_lease)


@pytest.mark.parametrize(
    "exc",
    [
        psycopg.errors.ConnectionFailure("connection lost"),
        psycopg.errors.SqlclientUnableToEstablishSqlconnection("server unreachable"),
        psycopg.OperationalError(
            'connection to server at "127.0.0.1" failed: Connection refused'
        ),
        ConnectionRefusedError(errno.ECONNREFUSED, "connection refused"),
        TimeoutError("database connection timed out"),
    ],
)
def test_database_connectivity_classifier_accepts_only_positive_evidence(
    exc: BaseException,
) -> None:
    assert is_database_connectivity_error(exc)


@pytest.mark.parametrize(
    "exc",
    [
        psycopg.OperationalError("database unavailable"),
        psycopg.InterfaceError("connection already closed"),
        psycopg.errors.InvalidPassword("opaque server rejection"),
        psycopg.errors.InsufficientPrivilege("opaque server rejection"),
        psycopg.errors.UndefinedTable("relation does not exist"),
        psycopg.OperationalError("connection failed: TLS handshake failed"),
        psycopg.OperationalError("root certificate file is invalid"),
        psycopg.OperationalError('connection is bad: invalid sslmode value: "bogus"'),
        psycopg.OperationalError("failed to resolve host 'bad.invalid'"),
        PermissionError(errno.EACCES, "permission denied"),
    ],
)
def test_database_connectivity_classifier_rejects_auth_tls_config_and_code(
    exc: BaseException,
) -> None:
    assert not is_database_connectivity_error(exc)


def test_evaluator_maps_only_allowlisted_conditions_and_reuses_stable_keys() -> None:
    now = datetime(2026, 7, 29, 12, tzinfo=UTC)
    snapshot = HealthSnapshot(
        now,
        (
            CheckResult("release_configuration", "PASS", "valid", now),
            CheckResult(
                "database_primary",
                "FAIL",
                "database_unavailable",
                now,
            ),
            CheckResult(
                "migration_leaves",
                "FAIL",
                "migration_state_unavailable",
                now,
            ),
            CheckResult(
                "deployment_preflight",
                "FAIL",
                "preflight_receipt_stale",
                now,
            ),
            CheckResult(
                "outbox_delivery",
                "FAIL",
                "outbox_dead_letter_present",
                now,
                2,
            ),
            CheckResult(
                "filesystem_roots",
                "FAIL",
                "filesystem_write_policy_invalid",
                now,
            ),
            CheckResult(
                "audit_validation",
                "STALE",
                "audit_validation_stale",
                now,
            ),
        ),
    )
    conditions = evaluated_service_conditions(snapshot)
    store = _Store(now)
    worker = ServiceAlertWorker(
        store=store,
        transport=_Transport(),
        settings=ServiceAlertWorkerSettings.from_environ(_environment()),
        health_collector=_collector(snapshot),  # type: ignore[arg-type]
        clock=lambda: now,
    )

    first = worker.run_once(now=now)
    second = worker.run_once(now=now)

    assert conditions == (
        "audit_validation_failed",
        "database_unavailable",
        "dead_letter_present",
        "preflight_failed",
        "readiness_failed",
    )
    assert set(conditions) <= ALLOWED_SERVICE_CONDITIONS
    prioritized = (
        "database_unavailable",
        "audit_validation_failed",
        "dead_letter_present",
        "preflight_failed",
        "readiness_failed",
    )
    assert first.conditions == second.conditions == prioritized
    first_keys = [item["condition_key"] for item in store.raises[: len(prioritized)]]
    second_keys = [item["condition_key"] for item in store.raises[len(prioritized) :]]
    assert first_keys == second_keys
    assert first_keys == [
        stable_health_alert_key(
            release_id="release-2026.07.29",
            condition_code=condition,
        )
        for condition in prioritized
    ]
    assert all(
        item["endpoint_id"] == "primary-oncall"
        and item["acknowledgment_timeout_seconds"] == 300
        for item in store.raises
    )


def test_database_emergency_claim_is_deterministic_and_time_bucketed() -> None:
    now = datetime(2026, 7, 29, 12, 1, tzinfo=UTC)
    settings = ServiceAlertWorkerSettings.from_environ(_environment())
    other_worker = replace(settings, worker_id="service-alert:test-2")

    first = emergency_database_delivery_claim(
        settings=settings,
        observed_at=now,
    )
    same_bucket = emergency_database_delivery_claim(
        settings=other_worker,
        observed_at=now + timedelta(seconds=239),
    )
    next_bucket = emergency_database_delivery_claim(
        settings=settings,
        observed_at=now + timedelta(seconds=240),
    )

    assert first.alert_id == same_bucket.alert_id
    assert first.delivery_id == same_bucket.delivery_id
    assert first.delivery_key == same_bucket.delivery_key
    assert first.opened_at == same_bucket.opened_at
    assert first.worker_id != same_bucket.worker_id
    assert next_bucket.delivery_id != first.delivery_id
    assert next_bucket.delivery_key != first.delivery_key
    assert next_bucket.opened_at == first.opened_at + timedelta(seconds=300)


def test_database_failure_uses_db_independent_emergency_delivery_before_cap() -> None:
    now = datetime(2026, 7, 29, 12, tzinfo=UTC)
    snapshot = HealthSnapshot(
        now,
        (
            CheckResult("audit_validation", "FAIL", "audit_validation_stale", now),
            CheckResult(
                "database_primary",
                "FAIL",
                "database_unavailable",
                now,
            ),
        ),
    )
    settings = replace(
        ServiceAlertWorkerSettings.from_environ(_environment()),
        maximum_evaluated_per_cycle=1,
    )
    store = _UnavailableStore(now)
    transport = _Transport()
    worker = ServiceAlertWorker(
        store=store,
        transport=transport,
        settings=settings,
        health_collector=_collector(snapshot),  # type: ignore[arg-type]
        clock=lambda: now,
    )

    result = worker.run_once(now=now)

    assert result.conditions == ("database_unavailable",)
    assert result.deliveries_claimed == 1
    assert result.deliveries_succeeded == 1
    assert not store.escalation_calls
    assert not store.claim_calls
    assert len(transport.sent) == 1
    assert transport.sent[0].condition_code == "database_unavailable"


def test_database_emergency_transport_failure_retries_same_receiver_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime(2026, 7, 29, 12, tzinfo=UTC)
    snapshot = HealthSnapshot(
        now,
        (
            CheckResult(
                "database_primary",
                "FAIL",
                "database_unavailable",
                now,
            ),
        ),
    )
    settings = ServiceAlertWorkerSettings.from_environ(_environment())
    expected = emergency_database_delivery_claim(
        settings=settings,
        observed_at=now,
    )
    transport = _Transport(failures={expected.delivery_id})
    worker = ServiceAlertWorker(
        store=_UnavailableStore(now),
        transport=transport,
        settings=settings,
        health_collector=_collector(snapshot),  # type: ignore[arg-type]
        clock=lambda: now,
    )

    first = worker.run_once(now=now)
    second = worker.run_once(now=now + timedelta(seconds=1))

    assert first.deliveries_failed == second.deliveries_failed == 1
    assert [item.delivery_key for item in transport.sent] == [
        expected.delivery_key,
        expected.delivery_key,
    ]
    logged = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert len(logged) == 2
    assert all(
        item["event_code"] == "database_emergency_delivery_failed"
        and item["error_category"] == "database_emergency_delivery"
        and "database" not in item["error"].casefold()
        for item in logged
    )


def test_non_connectivity_store_error_does_not_use_emergency_delivery() -> None:
    now = datetime(2026, 7, 29, 12, tzinfo=UTC)
    snapshot = HealthSnapshot(
        now,
        (
            CheckResult(
                "database_primary",
                "FAIL",
                "database_unavailable",
                now,
            ),
        ),
    )
    transport = _Transport()
    worker = ServiceAlertWorker(
        store=_UnavailableStore(now, error=ValueError("schema bug")),
        transport=transport,
        settings=ServiceAlertWorkerSettings.from_environ(_environment()),
        health_collector=_collector(snapshot),  # type: ignore[arg-type]
        clock=lambda: now,
    )

    with pytest.raises(ValueError, match="schema bug"):
        worker.run_once(now=now)
    assert not transport.sent


@pytest.mark.parametrize(
    "error",
    [
        psycopg.errors.InvalidPassword("password authentication failed"),
        psycopg.errors.InsufficientPrivilege("permission denied"),
        psycopg.errors.UndefinedTable("schema mismatch"),
        psycopg.OperationalError("connection failed: certificate verify failed"),
        psycopg.OperationalError("connection is bad: invalid connection option"),
    ],
)
def test_database_emergency_delivery_fails_loud_for_non_connectivity_errors(
    error: BaseException,
) -> None:
    now = datetime(2026, 7, 29, 12, tzinfo=UTC)
    snapshot = HealthSnapshot(
        now,
        (
            CheckResult(
                "database_primary",
                "FAIL",
                "database_unavailable",
                now,
            ),
        ),
    )
    transport = _Transport()
    worker = ServiceAlertWorker(
        store=_UnavailableStore(now, error=error),
        transport=transport,
        settings=ServiceAlertWorkerSettings.from_environ(_environment()),
        health_collector=_collector(snapshot),  # type: ignore[arg-type]
        clock=lambda: now,
    )

    with pytest.raises(type(error)):
        worker.run_once(now=now)
    assert not transport.sent


def test_persistent_worker_does_not_retry_authentication_failure() -> None:
    now = datetime(2026, 7, 29, 12, tzinfo=UTC)
    error = psycopg.errors.InvalidPassword("opaque server rejection")
    snapshot = HealthSnapshot(
        now,
        (
            CheckResult(
                "database_primary",
                "FAIL",
                "database_unavailable",
                now,
            ),
        ),
    )
    worker = ServiceAlertWorker(
        store=_UnavailableStore(now, error=error),
        transport=_Transport(),
        settings=ServiceAlertWorkerSettings.from_environ(_environment()),
        health_collector=_collector(snapshot),  # type: ignore[arg-type]
        clock=lambda: now,
    )

    with pytest.raises(psycopg.errors.InvalidPassword):
        worker.run_forever(install_signal_handlers=False, maximum_cycles=1)


def test_partial_state_unavailability_is_not_silently_suppressed() -> None:
    now = datetime(2026, 7, 29, 12, tzinfo=UTC)
    snapshot = HealthSnapshot(
        now,
        (
            CheckResult(
                "database_primary",
                "PASS",
                "database_primary_transaction_ok",
                now,
            ),
            CheckResult(
                "migration_leaves",
                "FAIL",
                "migration_state_unavailable",
                now,
            ),
        ),
    )

    assert evaluated_service_conditions(snapshot) == ("migration_drift",)


def test_one_cycle_escalates_and_delivers_with_bounded_retry() -> None:
    now = datetime(2026, 7, 29, 12, tzinfo=UTC)
    retry_claim = _delivery_claim(now, attempt_count=2, sequence=2)
    terminal_claim = _delivery_claim(now, attempt_count=8, sequence=3)
    success_claim = _delivery_claim(now, attempt_count=1, sequence=1)
    store = _Store(
        now,
        escalations=[_service_alert(now), None],
        claims=[success_claim, retry_claim, terminal_claim, None],
    )
    transport = _Transport(
        failures={retry_claim.delivery_id, terminal_claim.delivery_id}
    )
    snapshot = HealthSnapshot(
        now,
        (CheckResult("database_primary", "PASS", "primary", now),),
    )
    worker = ServiceAlertWorker(
        store=store,
        transport=transport,
        settings=ServiceAlertWorkerSettings.from_environ(_environment()),
        health_collector=_collector(snapshot),  # type: ignore[arg-type]
        clock=lambda: now,
    )

    result = worker.run_once(now=now)

    assert result.escalations == 1
    assert result.deliveries_claimed == 3
    assert result.deliveries_succeeded == 1
    assert result.deliveries_retried == 1
    assert result.deliveries_failed == 1
    assert store.delivered == [(success_claim, 202)]
    assert store.failures == [(retry_claim, 60), (terminal_claim, 3600)]
    assert all(call["lease_seconds"] == 30 for call in store.claim_calls)
    assert all(call["max_attempts"] == 8 for call in store.claim_calls)
    assert store.escalation_calls[0]["maximum_level"] == 3


def test_stop_request_drains_current_delivery_without_claiming_another() -> None:
    now = datetime(2026, 7, 29, 12, tzinfo=UTC)
    first = _delivery_claim(now, attempt_count=1, sequence=1)
    second = _delivery_claim(now, attempt_count=1, sequence=2)
    store = _Store(now, claims=[first, second, None])
    snapshot = HealthSnapshot(
        now,
        (CheckResult("database_primary", "PASS", "primary", now),),
    )
    transport = _Transport()
    worker = ServiceAlertWorker(
        store=store,
        transport=transport,
        settings=ServiceAlertWorkerSettings.from_environ(_environment()),
        health_collector=_collector(snapshot),  # type: ignore[arg-type]
        clock=lambda: now,
    )
    transport.on_send = worker.request_stop

    result = worker.run_once(now=now)

    assert result.deliveries_claimed == 1
    assert store.delivered == [(first, 202)]
    assert len(store.claim_calls) == 1
    assert store.claims == [second, None]
    assert worker.run_once(now=now).deliveries_claimed == 0


def test_poll_loop_has_an_explicit_cycle_bound_for_supervision_tests() -> None:
    now = datetime(2026, 7, 29, 12, tzinfo=UTC)
    calls = 0
    snapshot = HealthSnapshot(
        now,
        (CheckResult("database_primary", "PASS", "primary", now),),
    )

    def collect(
        _kind: str,
        *,
        observed_at: datetime | None = None,
        use_cache: bool = True,
    ) -> HealthSnapshot:
        nonlocal calls
        del observed_at, use_cache
        calls += 1
        return snapshot

    worker = ServiceAlertWorker(
        store=_Store(now),
        transport=_Transport(),
        settings=replace(
            ServiceAlertWorkerSettings.from_environ(_environment()),
            poll_interval_seconds=0.05,
        ),
        health_collector=collect,  # type: ignore[arg-type]
        clock=lambda: now,
    )

    worker.run_forever(install_signal_handlers=False, maximum_cycles=2)

    assert calls == 2


@pytest.mark.parametrize(
    ("result", "expected_exit"),
    [
        (
            AlertWorkerRunResult(
                deliveries_claimed=1,
                deliveries_failed=1,
                conditions=("database_unavailable",),
            ),
            3,
        ),
        (AlertWorkerRunResult(deliveries_retried=1), 0),
        (AlertWorkerRunResult(deliveries_succeeded=1), 0),
    ],
)
def test_alert_worker_once_cli_exit_distinguishes_terminal_and_retryable_work(
    monkeypatch: pytest.MonkeyPatch,
    result: AlertWorkerRunResult,
    expected_exit: int,
) -> None:
    class FakeWorker:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run_once(self) -> AlertWorkerRunResult:
            return result

    settings = type("Settings", (), {"transport_timeout_seconds": 5.0})()
    settings_factory = type(
        "SettingsFactory",
        (),
        {"from_environ": staticmethod(lambda _environ: settings)},
    )
    payloads: list[dict[str, object]] = []
    monkeypatch.setattr(alert_worker_module, "ServiceAlertWorker", FakeWorker)
    monkeypatch.setattr(
        alert_worker_module,
        "ServiceAlertWorkerSettings",
        settings_factory,
    )
    monkeypatch.setattr(alerting_module, "ServiceAlertStore", object)
    monkeypatch.setattr(
        alerting_module,
        "LoopbackOrHttpsAlertTransport",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        commands_module,
        "_required_secret_file",
        lambda _name: "/run/credentials/receiver",
    )
    monkeypatch.setattr(commands_module, "_write", payloads.append)

    exit_code = commands_module.dispatch(
        argparse.Namespace(command="alert-worker", once=True)
    )

    assert exit_code == expected_exit
    assert payloads == [result.as_dict()]


@pytest.mark.parametrize(
    ("attempt_count", "expected"),
    [(1, 30), (2, 60), (5, 480), (8, 3600), (100, 3600)],
)
def test_alert_retry_delay_is_deterministic_and_bounded(
    attempt_count: int,
    expected: int,
) -> None:
    assert bounded_alert_retry_delay(attempt_count, base_seconds=30) == expected
