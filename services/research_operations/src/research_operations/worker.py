"""Persistent, draining outbox worker around Research's single-event projector."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from dataclasses import dataclass
from types import FrameType
from typing import Any, Protocol

import psycopg
from django.core.exceptions import ValidationError
from django.db import OperationalError as DjangoOperationalError

from .errors import ClaimLost, MaintenanceFenceActive
from .outbox import OutboxClaim, OutboxStore, bounded_retry_delay, sanitize_error
from .runtime_guard import require_operated_preflight_receipt


class Projector(Protocol):
    def project(self, event_id: Any) -> Any: ...


class DjangoAuditProjector:
    """Lazy adapter to the immutable Research audit projection primitive."""

    def __init__(self, settings_module: str = "market_research_web.settings") -> None:
        self._settings_module = settings_module
        self._ready = False

    def project(self, event_id: Any) -> Any:
        if not self._ready:
            os.environ.setdefault("DJANGO_SETTINGS_MODULE", self._settings_module)
            import django

            django.setup()
            self._ready = True
        from market_research_web.operations_contract import project_web_audit_event

        return project_web_audit_event(event_id)


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    worker_id: str
    poll_interval: float = 1.0
    scan_batch_size: int = 100
    lease_seconds: int = 30
    max_attempts: int = 8

    def __post_init__(self) -> None:
        if not self.worker_id.strip() or len(self.worker_id) > 255:
            raise ValueError("worker_id_invalid")
        if not 0.05 <= self.poll_interval <= 60:
            raise ValueError("worker_poll_interval_invalid")
        if not 1 <= self.scan_batch_size <= 10_000:
            raise ValueError("worker_scan_batch_size_invalid")
        if not 3 <= self.lease_seconds <= 3600:
            raise ValueError("worker_lease_seconds_invalid")
        if not 1 <= self.max_attempts <= 100:
            raise ValueError("worker_max_attempts_invalid")


class _Heartbeat:
    def __init__(
        self,
        store: OutboxStore,
        claim: OutboxClaim,
        lease_seconds: int,
    ) -> None:
        self._store = store
        self._claim = claim
        self._lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._errors: list[BaseException] = []
        self._thread = threading.Thread(
            target=self._run,
            name=f"outbox-heartbeat-{claim.event_id}",
            daemon=True,
        )

    def __enter__(self) -> _Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self._lease_seconds / 2))

    def raise_if_lost(self) -> None:
        if self._errors:
            raise ClaimLost("outbox_heartbeat_lost") from self._errors[0]

    def _run(self) -> None:
        interval = max(1.0, self._lease_seconds / 3)
        while not self._stop.wait(interval):
            try:
                self._store.heartbeat(
                    self._claim,
                    lease_seconds=self._lease_seconds,
                )
            except BaseException as exc:  # thread boundary; propagated in main thread
                self._errors.append(exc)
                return


class OutboxWorker:
    def __init__(
        self,
        *,
        store: OutboxStore,
        projector: Projector,
        settings: WorkerSettings,
    ) -> None:
        self.store = store
        self.projector = projector
        self.settings = settings
        self.stop_requested = threading.Event()

    def request_stop(self) -> None:
        self.stop_requested.set()

    def install_signal_handlers(self) -> None:
        def handle_signal(_signum: int, _frame: FrameType | None) -> None:
            self.request_stop()

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

    def run_one(self) -> bool:
        require_operated_preflight_receipt()
        self.store.scan(batch_size=self.settings.scan_batch_size)
        claim = self.store.claim(
            worker_id=self.settings.worker_id,
            lease_seconds=self.settings.lease_seconds,
            max_attempts=self.settings.max_attempts,
        )
        if claim is None:
            self.store.worker_heartbeat(
                worker_id=self.settings.worker_id,
                state="IDLE",
            )
            return False
        self.store.worker_heartbeat(
            worker_id=self.settings.worker_id,
            state="WORKING",
            event_id=claim.event_id,
        )
        try:
            if claim.event_type != "internal_web_audit":
                raise ValueError("outbox_event_type_unsupported")
            with _Heartbeat(
                self.store, claim, self.settings.lease_seconds
            ) as heartbeat:
                self.projector.project(claim.event_id)
                heartbeat.raise_if_lost()
                self.store.mark_projected(claim)
        except ClaimLost:
            raise
        except Exception as exc:
            category, permanent = classify_projection_error(exc)
            self.store.record_failure(
                claim,
                category=category,
                error=sanitize_error(exc),
                permanent=permanent,
                max_attempts=self.settings.max_attempts,
                retry_delay_seconds=bounded_retry_delay(claim.attempt_count),
            )
        finally:
            self.store.worker_heartbeat(
                worker_id=self.settings.worker_id,
                state="DRAINING" if self.stop_requested.is_set() else "IDLE",
            )
        return True

    def run_forever(self, *, install_signal_handlers: bool = True) -> None:
        if install_signal_handlers:
            self.install_signal_handlers()
        self.store.worker_heartbeat(
            worker_id=self.settings.worker_id,
            state="STARTING",
        )
        try:
            while not self.stop_requested.is_set():
                try:
                    processed = self.run_one()
                except MaintenanceFenceActive:
                    processed = False
                    self.store.worker_heartbeat(
                        worker_id=self.settings.worker_id,
                        state="IDLE",
                    )
                except ClaimLost as exc:
                    processed = False
                    _log_worker_error(
                        worker_id=self.settings.worker_id,
                        category="claim_lost",
                        exc=exc,
                    )
                except (
                    OSError,
                    TimeoutError,
                    psycopg.OperationalError,
                    psycopg.InterfaceError,
                    DjangoOperationalError,
                ) as exc:
                    processed = False
                    _log_worker_error(
                        worker_id=self.settings.worker_id,
                        category="transient_dependency",
                        exc=exc,
                    )
                if not processed:
                    self.stop_requested.wait(self.settings.poll_interval)
        finally:
            self.store.worker_heartbeat(
                worker_id=self.settings.worker_id,
                state="STOPPED",
            )


def _log_worker_error(*, worker_id: str, category: str, exc: BaseException) -> None:
    payload = {
        "schema_version": 1,
        "severity": "WARNING",
        "service_role": "outbox-worker",
        "event_code": "worker_iteration_failed",
        "worker_id": worker_id,
        "error_category": category,
        "error": sanitize_error(exc),
    }
    print(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        file=sys.stderr,
        flush=True,
    )


def classify_projection_error(exc: BaseException) -> tuple[str, bool]:
    if isinstance(exc, (ValueError, TypeError, ValidationError)):
        return "permanent_contract", True
    if isinstance(
        exc,
        (
            OSError,
            TimeoutError,
            psycopg.OperationalError,
            psycopg.InterfaceError,
            DjangoOperationalError,
        ),
    ):
        return "transient_dependency", False
    return "transient_unexpected", False


def main(argv: list[str] | None = None) -> int:
    from .cli import main as operations_main

    return operations_main(["outbox-worker", *(sys.argv[1:] if argv is None else argv)])


__all__ = [
    "DjangoAuditProjector",
    "OutboxWorker",
    "Projector",
    "WorkerSettings",
    "classify_projection_error",
    "main",
]
