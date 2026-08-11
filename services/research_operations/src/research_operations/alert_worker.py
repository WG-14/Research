"""Persistent evaluator, delivery, and escalation worker for service health."""

from __future__ import annotations

import json
import re
import signal
import sys
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import Literal, Protocol

import psycopg

from .alerting import (
    ALLOWED_SERVICE_CONDITIONS,
    AlertDeliveryClaim,
    ServiceAlert,
)
from .errors import (
    AlertDeliveryClaimLost,
    AlertTransportError,
)
from .health import (
    CheckResult,
    HealthSnapshot,
    collect_health_snapshot,
    is_database_connectivity_error,
)
from .outbox import sanitize_error

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_DATABASE_DERIVATIVE_UNAVAILABLE_REASONS = frozenset(
    {
        "audit_validation_unavailable",
        "migration_state_unavailable",
        "outbox_state_unavailable",
        "outbox_worker_state_unavailable",
        "research_job_receipt_state_unavailable",
        "research_job_worker_state_unavailable",
        "runtime_control_unavailable",
        "worker_release_state_unavailable",
        "workflow_admission_unavailable",
    }
)
_CHECK_CONDITIONS = {
    "audit_validation": "audit_validation_failed",
    "database_primary": "database_not_primary",
    "deployment_preflight": "preflight_failed",
    "integrity_quarantine": "quarantine",
    "migration_leaves": "migration_drift",
    "outbox_delivery": "outbox_lag",
    "outbox_workers": "outbox_worker_missing",
    "research_job_receipts": "job_receipt_unapplied",
    "research_job_workers": "research_worker_missing",
    "worker_release": "worker_process_failed",
}
_WARNING_CONDITIONS = frozenset({"outbox_lag"})
_EMERGENCY_ALERT_NAMESPACE = uuid.UUID("f1b0bd72-9314-4bdf-a68c-bfe747c644c9")


def utcnow() -> datetime:
    return datetime.now(UTC)


class AlertStore(Protocol):
    def raise_condition_episode(
        self,
        *,
        condition_key: str,
        condition_code: str,
        severity: str,
        source_actor_id: str,
        endpoint_id: str,
        acknowledgment_timeout_seconds: int,
        now: datetime | None = None,
    ) -> ServiceAlert: ...

    def escalate_due(
        self,
        *,
        actor_id: str,
        endpoint_id: str,
        repeat_after_seconds: int,
        maximum_level: int = 3,
        now: datetime | None = None,
    ) -> ServiceAlert | None: ...

    def claim_delivery(
        self,
        *,
        worker_id: str,
        endpoint_id: str | None = None,
        lease_seconds: int = 30,
        max_attempts: int = 8,
        now: datetime | None = None,
    ) -> AlertDeliveryClaim | None: ...

    def mark_delivered(
        self,
        claim: AlertDeliveryClaim,
        *,
        response_code: int,
        now: datetime | None = None,
    ) -> None: ...

    def record_delivery_failure(
        self,
        claim: AlertDeliveryClaim,
        *,
        reason_code: str,
        max_attempts: int,
        retry_delay_seconds: int,
        now: datetime | None = None,
    ) -> str: ...


class AlertTransport(Protocol):
    def send(self, claim: AlertDeliveryClaim) -> int: ...


class HealthCollector(Protocol):
    def __call__(
        self,
        kind: Literal["web-read", "workflow-mutation"],
        *,
        observed_at: datetime | None = None,
        use_cache: bool = True,
    ) -> HealthSnapshot: ...


@dataclass(frozen=True, slots=True)
class ServiceAlertWorkerSettings:
    release_id: str
    worker_id: str
    primary_endpoint_id: str
    escalation_endpoint_id: str
    source_actor_id: str
    escalation_actor_id: str
    poll_interval_seconds: float
    transport_timeout_seconds: float
    lease_seconds: int
    max_attempts: int
    retry_delay_seconds: int
    acknowledgment_timeout_seconds: int
    escalation_repeat_seconds: int
    maximum_escalation_level: int
    maximum_evaluated_per_cycle: int
    maximum_deliveries_per_cycle: int
    maximum_escalations_per_cycle: int

    def __post_init__(self) -> None:
        _validate_identifier(self.release_id, "alert_release_id", maximum=128)
        _validate_identifier(self.worker_id, "alert_worker_id", maximum=255)
        _validate_identifier(
            self.primary_endpoint_id,
            "alert_primary_endpoint_id",
            maximum=128,
        )
        _validate_identifier(
            self.escalation_endpoint_id,
            "alert_escalation_endpoint_id",
            maximum=128,
        )
        _validate_identifier(
            self.source_actor_id,
            "alert_source_actor_id",
            maximum=255,
        )
        _validate_identifier(
            self.escalation_actor_id,
            "alert_escalation_actor_id",
            maximum=255,
        )
        if not 0.05 <= self.poll_interval_seconds <= 60:
            raise ValueError("alert_poll_interval_invalid")
        if not 0.1 <= self.transport_timeout_seconds <= 30:
            raise ValueError("alert_transport_timeout_invalid")
        _validate_int(self.lease_seconds, "alert_lease_seconds", 3, 3600)
        if self.lease_seconds < self.transport_timeout_seconds + 3:
            raise ValueError("alert_lease_transport_window_invalid")
        _validate_int(self.max_attempts, "alert_max_attempts", 1, 100)
        _validate_int(self.retry_delay_seconds, "alert_retry_delay_seconds", 1, 3600)
        _validate_int(
            self.acknowledgment_timeout_seconds,
            "alert_acknowledgment_timeout_invalid",
            1,
            86_400,
        )
        _validate_int(
            self.escalation_repeat_seconds,
            "alert_escalation_repeat_invalid",
            1,
            86_400,
        )
        _validate_int(
            self.maximum_escalation_level,
            "alert_maximum_escalation_level_invalid",
            1,
            32,
        )
        _validate_int(
            self.maximum_evaluated_per_cycle,
            "alert_maximum_evaluated_per_cycle_invalid",
            1,
            len(ALLOWED_SERVICE_CONDITIONS),
        )
        _validate_int(
            self.maximum_deliveries_per_cycle,
            "alert_maximum_deliveries_per_cycle_invalid",
            1,
            100,
        )
        _validate_int(
            self.maximum_escalations_per_cycle,
            "alert_maximum_escalations_per_cycle_invalid",
            1,
            100,
        )
        if self.source_actor_id == self.escalation_actor_id:
            raise ValueError("alert_actor_separation_invalid")

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str],
    ) -> ServiceAlertWorkerSettings:
        return cls(
            release_id=_required(environ, "RESEARCH_OPS_RELEASE_ID"),
            worker_id=_required(environ, "RESEARCH_OPS_ALERT_WORKER_ID"),
            primary_endpoint_id=_required(
                environ,
                "RESEARCH_OPS_ALERT_PRIMARY_ENDPOINT_ID",
            ),
            escalation_endpoint_id=_required(
                environ,
                "RESEARCH_OPS_ALERT_ESCALATION_ENDPOINT_ID",
            ),
            source_actor_id=_required(
                environ,
                "RESEARCH_OPS_ALERT_SOURCE_ACTOR_ID",
            ),
            escalation_actor_id=_required(
                environ,
                "RESEARCH_OPS_ALERT_ESCALATION_ACTOR_ID",
            ),
            poll_interval_seconds=_required_float(
                environ,
                "RESEARCH_OPS_ALERT_POLL_INTERVAL_SECONDS",
            ),
            transport_timeout_seconds=_required_float(
                environ,
                "RESEARCH_OPS_ALERT_TRANSPORT_TIMEOUT_SECONDS",
            ),
            lease_seconds=_required_int(
                environ,
                "RESEARCH_OPS_ALERT_LEASE_SECONDS",
            ),
            max_attempts=_required_int(
                environ,
                "RESEARCH_OPS_ALERT_MAX_ATTEMPTS",
            ),
            retry_delay_seconds=_required_int(
                environ,
                "RESEARCH_OPS_ALERT_RETRY_DELAY_SECONDS",
            ),
            acknowledgment_timeout_seconds=_required_int(
                environ,
                "RESEARCH_OPS_ALERT_ACKNOWLEDGMENT_TIMEOUT_SECONDS",
            ),
            escalation_repeat_seconds=_required_int(
                environ,
                "RESEARCH_OPS_ALERT_ESCALATION_REPEAT_SECONDS",
            ),
            maximum_escalation_level=_required_int(
                environ,
                "RESEARCH_OPS_ALERT_MAXIMUM_LEVEL",
            ),
            maximum_evaluated_per_cycle=_required_int(
                environ,
                "RESEARCH_OPS_ALERT_MAXIMUM_EVALUATED_PER_CYCLE",
            ),
            maximum_deliveries_per_cycle=_required_int(
                environ,
                "RESEARCH_OPS_ALERT_MAXIMUM_DELIVERIES_PER_CYCLE",
            ),
            maximum_escalations_per_cycle=_required_int(
                environ,
                "RESEARCH_OPS_ALERT_MAXIMUM_ESCALATIONS_PER_CYCLE",
            ),
        )


@dataclass(frozen=True, slots=True)
class AlertWorkerRunResult:
    evaluated_failures: int = 0
    alerts_observed: int = 0
    escalations: int = 0
    deliveries_claimed: int = 0
    deliveries_succeeded: int = 0
    deliveries_retried: int = 0
    deliveries_failed: int = 0
    conditions: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "alerts_observed": self.alerts_observed,
            "conditions": list(self.conditions),
            "deliveries_claimed": self.deliveries_claimed,
            "deliveries_failed": self.deliveries_failed,
            "deliveries_retried": self.deliveries_retried,
            "deliveries_succeeded": self.deliveries_succeeded,
            "escalations": self.escalations,
            "evaluated_failures": self.evaluated_failures,
        }


class ServiceAlertWorker:
    def __init__(
        self,
        *,
        store: AlertStore,
        transport: AlertTransport,
        settings: ServiceAlertWorkerSettings,
        health_collector: HealthCollector = collect_health_snapshot,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.store = store
        self.transport = transport
        self.settings = settings
        self.health_collector = health_collector
        self.clock = clock
        self.stop_requested = threading.Event()

    def request_stop(self) -> None:
        self.stop_requested.set()

    def install_signal_handlers(self) -> None:
        def handle_signal(_signum: int, _frame: FrameType | None) -> None:
            self.request_stop()

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

    def run_once(self, *, now: datetime | None = None) -> AlertWorkerRunResult:
        if self.stop_requested.is_set():
            return AlertWorkerRunResult()

        observed_at = _aware_utc(now or self.clock())
        snapshot = self.health_collector(
            "workflow-mutation",
            observed_at=observed_at,
            use_cache=False,
        )
        all_conditions = evaluated_service_conditions(snapshot)
        # A failed primary database also prevents the normal durable alert
        # path from storing an episode.  Always evaluate that condition first,
        # even when the per-cycle cap is lower than the number of failures.
        prioritized_conditions = (
            (
                "database_unavailable",
                *(item for item in all_conditions if item != "database_unavailable"),
            )
            if "database_unavailable" in all_conditions
            else all_conditions
        )
        conditions = prioritized_conditions[: self.settings.maximum_evaluated_per_cycle]
        alerts_observed = 0
        for condition in conditions:
            if self.stop_requested.is_set():
                break
            try:
                self.store.raise_condition_episode(
                    condition_key=stable_health_alert_key(
                        release_id=self.settings.release_id,
                        condition_code=condition,
                    ),
                    condition_code=condition,
                    severity=service_condition_severity(condition),
                    source_actor_id=self.settings.source_actor_id,
                    endpoint_id=self.settings.primary_endpoint_id,
                    acknowledgment_timeout_seconds=(
                        self.settings.acknowledgment_timeout_seconds
                    ),
                    now=_operation_time(now, self.clock),
                )
            except (OSError, psycopg.Error) as exc:
                if condition != "database_unavailable" or not (
                    is_database_connectivity_error(exc)
                ):
                    raise
                return self._deliver_database_emergency(
                    conditions=conditions,
                    observed_at=observed_at,
                )
            alerts_observed += 1

        escalations = 0
        while (
            not self.stop_requested.is_set()
            and escalations < self.settings.maximum_escalations_per_cycle
        ):
            escalated = self.store.escalate_due(
                actor_id=self.settings.escalation_actor_id,
                endpoint_id=self.settings.escalation_endpoint_id,
                repeat_after_seconds=self.settings.escalation_repeat_seconds,
                maximum_level=self.settings.maximum_escalation_level,
                now=_operation_time(now, self.clock),
            )
            if escalated is None:
                break
            escalations += 1

        deliveries_claimed = 0
        deliveries_succeeded = 0
        deliveries_retried = 0
        deliveries_failed = 0
        while (
            not self.stop_requested.is_set()
            and deliveries_claimed < self.settings.maximum_deliveries_per_cycle
        ):
            claim = self.store.claim_delivery(
                worker_id=self.settings.worker_id,
                lease_seconds=self.settings.lease_seconds,
                max_attempts=self.settings.max_attempts,
                now=_operation_time(now, self.clock),
            )
            if claim is None:
                break
            deliveries_claimed += 1
            try:
                response_code = self.transport.send(claim)
            except AlertTransportError:
                status = self.store.record_delivery_failure(
                    claim,
                    reason_code="delivery_transport_failed",
                    max_attempts=self.settings.max_attempts,
                    retry_delay_seconds=bounded_alert_retry_delay(
                        claim.attempt_count,
                        base_seconds=self.settings.retry_delay_seconds,
                    ),
                    now=_operation_time(now, self.clock),
                )
                if status == "FAILED":
                    deliveries_failed += 1
                else:
                    deliveries_retried += 1
            else:
                self.store.mark_delivered(
                    claim,
                    response_code=response_code,
                    now=_operation_time(now, self.clock),
                )
                deliveries_succeeded += 1

        return AlertWorkerRunResult(
            evaluated_failures=len(conditions),
            alerts_observed=alerts_observed,
            escalations=escalations,
            deliveries_claimed=deliveries_claimed,
            deliveries_succeeded=deliveries_succeeded,
            deliveries_retried=deliveries_retried,
            deliveries_failed=deliveries_failed,
            conditions=conditions,
        )

    def _deliver_database_emergency(
        self,
        *,
        conditions: tuple[str, ...],
        observed_at: datetime,
    ) -> AlertWorkerRunResult:
        """Reach the receiver without the failed database dependency.

        The receiver is the only deduplication authority in this degraded
        mode.  Every worker and restart produces the same envelope inside a
        bounded UTC bucket.  No local acknowledgement, escalation, or durable
        history is claimed until PostgreSQL recovers.
        """

        if self.stop_requested.is_set():
            return AlertWorkerRunResult(
                evaluated_failures=len(conditions),
                alerts_observed=1,
                conditions=conditions,
            )
        claim = emergency_database_delivery_claim(
            settings=self.settings,
            observed_at=observed_at,
        )
        try:
            self.transport.send(claim)
        except AlertTransportError as exc:
            _log_worker_error(
                worker_id=self.settings.worker_id,
                category="database_emergency_delivery",
                event_code="database_emergency_delivery_failed",
                exc=exc,
            )
            return AlertWorkerRunResult(
                evaluated_failures=len(conditions),
                alerts_observed=1,
                deliveries_claimed=1,
                deliveries_failed=1,
                conditions=conditions,
            )
        return AlertWorkerRunResult(
            evaluated_failures=len(conditions),
            alerts_observed=1,
            deliveries_claimed=1,
            deliveries_succeeded=1,
            conditions=conditions,
        )

    def run_forever(
        self,
        *,
        install_signal_handlers: bool = True,
        maximum_cycles: int | None = None,
    ) -> None:
        if maximum_cycles is not None and maximum_cycles < 1:
            raise ValueError("alert_maximum_cycles_invalid")
        if install_signal_handlers:
            self.install_signal_handlers()
        completed_cycles = 0
        while not self.stop_requested.is_set():
            try:
                self.run_once()
            except AlertDeliveryClaimLost as exc:
                _log_worker_error(
                    worker_id=self.settings.worker_id,
                    category="claim_lost",
                    exc=exc,
                )
            except (OSError, psycopg.Error) as exc:
                if not is_database_connectivity_error(exc):
                    raise
                _log_worker_error(
                    worker_id=self.settings.worker_id,
                    category="transient_dependency",
                    exc=exc,
                )
            completed_cycles += 1
            if maximum_cycles is not None and completed_cycles >= maximum_cycles:
                return
            self.stop_requested.wait(self.settings.poll_interval_seconds)


def service_condition_for_check(check: CheckResult) -> str | None:
    if check.status == "PASS":
        return None
    if check.reason_code == "database_unavailable":
        return "database_unavailable"
    if check.reason_code == "outbox_dead_letter_present":
        return "dead_letter_present"
    condition = _CHECK_CONDITIONS.get(check.check_id, "readiness_failed")
    if condition not in ALLOWED_SERVICE_CONDITIONS:
        raise RuntimeError("service_health_condition_mapping_invalid")
    return condition


def evaluated_service_conditions(snapshot: HealthSnapshot) -> tuple[str, ...]:
    database_unavailable = any(
        check.status != "PASS" and check.reason_code == "database_unavailable"
        for check in snapshot.checks
    )
    conditions = {
        condition
        for check in snapshot.checks
        if not (
            database_unavailable
            and check.reason_code in _DATABASE_DERIVATIVE_UNAVAILABLE_REASONS
        )
        for condition in (service_condition_for_check(check),)
        if condition is not None
    }
    return tuple(sorted(conditions))


def stable_health_alert_key(*, release_id: str, condition_code: str) -> str:
    _validate_identifier(release_id, "alert_release_id", maximum=128)
    if condition_code not in ALLOWED_SERVICE_CONDITIONS:
        raise ValueError("service_health_condition_not_allowed")
    key = f"service-health:v1:{release_id}:{condition_code}"
    _validate_identifier(key, "service_health_idempotency_key", maximum=255)
    return key


def emergency_database_delivery_claim(
    *,
    settings: ServiceAlertWorkerSettings,
    observed_at: datetime,
) -> AlertDeliveryClaim:
    """Build one deterministic, receiver-deduplicated degraded-mode claim."""

    observed = _aware_utc(observed_at)
    bucket_seconds = settings.escalation_repeat_seconds
    bucket_number = int(observed.timestamp()) // bucket_seconds
    bucket_start = datetime.fromtimestamp(
        bucket_number * bucket_seconds,
        tz=UTC,
    )
    identity = (
        "service-alert-emergency:v1:"
        f"{settings.release_id}:database_unavailable:"
        f"{settings.primary_endpoint_id}:{int(bucket_start.timestamp())}"
    )
    alert_id = uuid.uuid5(_EMERGENCY_ALERT_NAMESPACE, f"alert:{identity}")
    delivery_id = uuid.uuid5(
        _EMERGENCY_ALERT_NAMESPACE,
        f"delivery:{identity}",
    )
    lease_token = uuid.uuid5(_EMERGENCY_ALERT_NAMESPACE, f"lease:{identity}")
    return AlertDeliveryClaim(
        delivery_id=delivery_id,
        alert_id=alert_id,
        delivery_key=f"service-alert-emergency:v1:{delivery_id}",
        endpoint_id=settings.primary_endpoint_id,
        escalation_level=0,
        worker_id=settings.worker_id,
        lease_token=lease_token,
        fencing_token=bucket_number + 1,
        lease_expires_at=bucket_start + timedelta(seconds=bucket_seconds),
        attempt_count=1,
        condition_code="database_unavailable",
        severity="CRITICAL",
        opened_at=bucket_start,
    )


def service_condition_severity(condition_code: str) -> str:
    if condition_code not in ALLOWED_SERVICE_CONDITIONS:
        raise ValueError("service_health_condition_not_allowed")
    return "WARNING" if condition_code in _WARNING_CONDITIONS else "CRITICAL"


def bounded_alert_retry_delay(attempt_count: int, *, base_seconds: int) -> int:
    _validate_int(attempt_count, "alert_attempt_count_invalid", 1, 100)
    _validate_int(base_seconds, "alert_retry_delay_seconds", 1, 3600)
    exponent = min(attempt_count - 1, 12)
    return min(3600, base_seconds * (1 << exponent))


def _operation_time(
    fixed: datetime | None,
    clock: Callable[[], datetime],
) -> datetime:
    return _aware_utc(fixed if fixed is not None else clock())


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("alert_worker_timestamp_naive")
    return value.astimezone(UTC)


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"configuration_missing:{name}")
    return value


def _required_int(environ: Mapping[str, str], name: str) -> int:
    raw = _required(environ, name)
    if not raw.isascii() or not raw.isdecimal():
        raise ValueError(f"configuration_invalid:{name}")
    return int(raw)


def _required_float(environ: Mapping[str, str], name: str) -> float:
    raw = _required(environ, name)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"configuration_invalid:{name}") from exc
    return value


def _validate_identifier(value: str, name: str, *, maximum: int) -> None:
    if len(value) > maximum or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{name}_invalid")


def _validate_int(value: int, name: str, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(name)


def _log_worker_error(
    *,
    worker_id: str,
    category: str,
    exc: BaseException,
    event_code: str = "alert_worker_iteration_failed",
) -> None:
    payload = {
        "error": sanitize_error(exc),
        "error_category": category,
        "event_code": event_code,
        "schema_version": 1,
        "service_role": "service-alert-worker",
        "severity": "WARNING",
        "worker_id": worker_id,
    }
    print(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        file=sys.stderr,
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    from .cli import main as operations_main

    return operations_main(["alert-worker", *(sys.argv[1:] if argv is None else argv)])


__all__ = [
    "AlertStore",
    "AlertTransport",
    "AlertWorkerRunResult",
    "HealthCollector",
    "ServiceAlertWorker",
    "ServiceAlertWorkerSettings",
    "bounded_alert_retry_delay",
    "emergency_database_delivery_claim",
    "evaluated_service_conditions",
    "main",
    "service_condition_for_check",
    "service_condition_severity",
    "stable_health_alert_key",
]
