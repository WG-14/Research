"""WSGI boundaries for guarded Research traffic and operations-only probes."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from wsgiref.types import StartResponse

from .backup import MUTATION_FENCE_ADVISORY_LOCK_ID
from .database import connection
from .health import (
    CheckResult,
    collect_health_snapshot,
    iso_utc,
    release_diagnostics,
    utcnow,
)
from .metrics import collect_metrics, render_prometheus

WSGIApplication = Callable[[Mapping[str, Any], StartResponse], Iterable[bytes]]
_JSON_HEADERS = [
    ("Content-Type", "application/json"),
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
]
_WEB_APPLICATION: WSGIApplication | None = None
_WEB_APPLICATION_LOCK = threading.Lock()


class OperationsApplication:
    """Small operations-only WSGI app; never mount it on employee ingress."""

    def __call__(
        self, environ: Mapping[str, Any], start_response: StartResponse
    ) -> Iterable[bytes]:
        started = time.monotonic()
        method = str(environ.get("REQUEST_METHOD") or "")
        path = str(environ.get("PATH_INFO") or "")
        endpoint = _endpoint_id(path)
        status_code = 500
        try:
            if method != "GET":
                status_code = 405
                return _json_response(
                    start_response,
                    "405 Method Not Allowed",
                    {"status": "METHOD_NOT_ALLOWED"},
                    extra_headers=[("Allow", "GET")],
                )
            if str(environ.get("QUERY_STRING") or "") or _content_length(environ):
                status_code = 400
                return _json_response(
                    start_response,
                    "400 Bad Request",
                    {"status": "BAD_REQUEST"},
                )
            if path == "/__ops/live":
                status_code = 200
                return _json_response(start_response, "200 OK", {"status": "UP"})
            if path == "/__ops/ready/web-read":
                snapshot = collect_health_snapshot("web-read")
                status_code = 200 if snapshot.ready else 503
                return _json_response(
                    start_response,
                    "200 OK" if snapshot.ready else "503 Service Unavailable",
                    {"status": "READY" if snapshot.ready else "NOT_READY"},
                )
            if path == "/__ops/ready/workflow-mutation":
                snapshot = collect_health_snapshot("workflow-mutation")
                status_code = 200 if snapshot.ready else 503
                return _json_response(
                    start_response,
                    "200 OK" if snapshot.ready else "503 Service Unavailable",
                    {"status": "READY" if snapshot.ready else "NOT_READY"},
                )
            if path == "/__ops/diagnostics":
                if not _operations_identity_authorized(environ):
                    status_code = 404
                    return _json_response(
                        start_response,
                        "404 Not Found",
                        {"status": "NOT_FOUND"},
                    )
                payload = _diagnostics_payload()
                status_code = 200
                return _json_response(
                    start_response, "200 OK", payload, max_bytes=32_768
                )
            if path == "/__ops/metrics":
                if not _operations_identity_authorized(environ):
                    status_code = 404
                    return _json_response(
                        start_response,
                        "404 Not Found",
                        {"status": "NOT_FOUND"},
                    )
                body = render_prometheus(collect_metrics()).encode("ascii")
                status_code = 200
                start_response(
                    "200 OK",
                    [
                        ("Content-Type", "text/plain; version=0.0.4; charset=utf-8"),
                        ("Cache-Control", "no-store"),
                        ("Content-Length", str(len(body))),
                    ],
                )
                return [body]
            if path == "/_internal/mutation-admission":
                if environ.get("HTTP_X_RESEARCH_OPS_INTERNAL") != "mutation-gate-v1":
                    status_code = 404
                    return _json_response(
                        start_response,
                        "404 Not Found",
                        {"status": "NOT_FOUND"},
                    )
                original_method = str(
                    environ.get("HTTP_X_RESEARCH_OPS_ORIGINAL_METHOD") or ""
                ).upper()
                allowed = original_method in {"GET", "HEAD"} or _mutation_is_open()
                status_code = 204 if allowed else 503
                start_response(
                    "204 No Content" if allowed else "503 Service Unavailable",
                    [("Cache-Control", "no-store"), ("Content-Length", "0")],
                )
                return [b""]
            status_code = 404
            return _json_response(
                start_response, "404 Not Found", {"status": "NOT_FOUND"}
            )
        except Exception:
            status_code = 503
            return _json_response(
                start_response,
                "503 Service Unavailable",
                (
                    {"status": "DOWN"}
                    if path == "/__ops/live"
                    else {"status": "NOT_READY"}
                ),
            )
        finally:
            _log_operation_request(
                endpoint=endpoint,
                status_code=status_code,
                duration_ms=(time.monotonic() - started) * 1000,
            )


class MutationGuardedWebApplication:
    """Hold a shared PostgreSQL fence lock for every non-safe web request."""

    def __call__(
        self, environ: Mapping[str, Any], start_response: StartResponse
    ) -> Iterable[bytes]:
        application = _research_web_application()
        method = str(environ.get("REQUEST_METHOD") or "").upper()
        if method in {"GET", "HEAD"}:
            return application(environ, start_response)

        context = connection(connect_timeout=3)
        try:
            conn = context.__enter__()
            conn.execute(
                "SELECT pg_advisory_xact_lock_shared(%s)",
                (MUTATION_FENCE_ADVISORY_LOCK_ID,),
            )
            row = conn.execute(
                """
                SELECT mutation_admission_open, integrity_quarantine
                FROM research_ops.runtime_control
                WHERE singleton_id = 1
                """
            ).fetchone()
            workflow = collect_health_snapshot("workflow-mutation")
            if row is None or not bool(row[0]) or bool(row[1]) or not workflow.ready:
                context.__exit__(None, None, None)
                return _web_unavailable(start_response)
            response = application(environ, start_response)
        except BaseException as exc:
            context.__exit__(type(exc), exc, exc.__traceback__)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return _web_unavailable(start_response)
        return _GuardedIterable(response, context)


class _GuardedIterable:
    def __init__(self, response: Iterable[bytes], context: Any) -> None:
        self._response = response
        self._iterator = iter(response)
        self._context = context
        self._closed = False

    def __iter__(self) -> _GuardedIterable:
        return self

    def __next__(self) -> bytes:
        try:
            return next(self._iterator)
        except StopIteration:
            self.close()
            raise
        except BaseException as exc:
            self._close_with_error(exc)
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self._response, "close", None)
            if close is not None:
                close()
        finally:
            self._context.__exit__(None, None, None)

    def _close_with_error(self, exc: BaseException) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self._response, "close", None)
            if close is not None:
                close()
        finally:
            self._context.__exit__(type(exc), exc, exc.__traceback__)


def _research_web_application() -> WSGIApplication:
    global _WEB_APPLICATION
    if _WEB_APPLICATION is not None:
        return _WEB_APPLICATION
    with _WEB_APPLICATION_LOCK:
        if _WEB_APPLICATION is None:
            os.environ.setdefault(
                "DJANGO_SETTINGS_MODULE", "market_research_web.settings"
            )
            from market_research_web.wsgi import application

            def invoke_django(
                environ: Mapping[str, Any],
                start_response: StartResponse,
            ) -> Iterable[bytes]:
                return application(dict(environ), start_response)

            _WEB_APPLICATION = invoke_django
    configured_application = _WEB_APPLICATION
    if configured_application is None:
        raise RuntimeError("research_web_application_initialization_failed")
    return configured_application


def _web_unavailable(start_response: StartResponse) -> Iterable[bytes]:
    body = b"Service unavailable"
    start_response(
        "503 Service Unavailable",
        [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Cache-Control", "no-store"),
            ("Content-Length", str(len(body))),
            ("Retry-After", "5"),
            ("X-Content-Type-Options", "nosniff"),
        ],
    )
    return [body]


def _mutation_is_open() -> bool:
    try:
        with connection(connect_timeout=3) as conn:
            row = conn.execute(
                """
                SELECT mutation_admission_open, integrity_quarantine
                FROM research_ops.runtime_control
                WHERE singleton_id = 1
                """
            ).fetchone()
    except Exception:
        return False
    return row is not None and bool(row[0]) and not bool(row[1])


def _diagnostics_payload() -> dict[str, Any]:
    observed_at = utcnow()
    web = collect_health_snapshot("web-read")
    mutation = collect_health_snapshot("workflow-mutation")
    merged: dict[str, CheckResult] = {item.check_id: item for item in web.checks}
    merged.update({item.check_id: item for item in mutation.checks})
    for item in _history_checks(observed_at):
        merged[item.check_id] = item
    checks = tuple(merged[key] for key in sorted(merged))[:16]
    status = (
        "PASS" if checks and all(item.status == "PASS" for item in checks) else "FAIL"
    )
    return {
        "schema_version": 1,
        "status": status,
        "observed_at": iso_utc(observed_at),
        "correlation_id": str(uuid.uuid4()),
        "release": release_diagnostics(observed_at=observed_at),
        "checks": [item.as_dict() for item in checks],
    }


def _history_checks(observed_at: datetime) -> tuple[CheckResult, ...]:
    backup_max_age = _bounded_environment_seconds(
        "RESEARCH_OPS_BACKUP_MAX_AGE_SECONDS", default=90_000, maximum=31_536_000
    )
    restore_max_age = _bounded_environment_seconds(
        "RESEARCH_OPS_RESTORE_DRILL_MAX_AGE_SECONDS",
        default=2_678_400,
        maximum=31_536_000,
    )
    try:
        with connection(connect_timeout=3) as conn:
            backup = conn.execute(
                "SELECT max(verified_at) FROM research_ops.backup_set"
            ).fetchone()
            restore = conn.execute(
                """
                SELECT status, finished_at
                FROM research_ops.restore_drill
                ORDER BY finished_at DESC, drill_id DESC
                LIMIT 1
                """
            ).fetchone()
    except Exception:
        return (
            CheckResult(
                "backup_recency", "STALE", "backup_observation_unavailable", observed_at
            ),
            CheckResult(
                "restore_drill", "STALE", "restore_observation_unavailable", observed_at
            ),
        )
    backup_time = backup[0] if backup is not None else None
    if backup_time is None:
        backup_check = CheckResult(
            "backup_recency", "STALE", "verified_backup_missing", observed_at
        )
    elif observed_at - backup_time > timedelta(seconds=backup_max_age):
        backup_check = CheckResult(
            "backup_recency", "STALE", "verified_backup_stale", backup_time
        )
    else:
        backup_check = CheckResult(
            "backup_recency", "PASS", "verified_backup_current", backup_time
        )
    if restore is None:
        restore_check = CheckResult(
            "restore_drill", "STALE", "restore_drill_missing", observed_at
        )
    elif restore[0] != "PASS":
        restore_check = CheckResult(
            "restore_drill", "FAIL", "restore_drill_failed", restore[1]
        )
    elif observed_at - restore[1] > timedelta(seconds=restore_max_age):
        restore_check = CheckResult(
            "restore_drill", "STALE", "restore_drill_stale", restore[1]
        )
    else:
        restore_check = CheckResult(
            "restore_drill", "PASS", "restore_drill_current", restore[1]
        )
    return backup_check, restore_check


def _operations_identity_authorized(environ: Mapping[str, Any]) -> bool:
    return (
        environ.get("HTTP_X_RESEARCH_OPS_CLIENT_VERIFIED") == "SUCCESS"
        and environ.get("HTTP_X_RESEARCH_OPS_AUTHORIZED") == "1"
    )


def _json_response(
    start_response: StartResponse,
    status: str,
    value: Mapping[str, Any],
    *,
    extra_headers: list[tuple[str, str]] | None = None,
    max_bytes: int = 4_096,
) -> Iterable[bytes]:
    body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(body) > max_bytes:
        body = b'{"status":"DOWN"}'
        status = "503 Service Unavailable"
    headers = [*_JSON_HEADERS, ("Content-Length", str(len(body)))]
    if extra_headers:
        headers.extend(extra_headers)
    start_response(status, headers)
    return [body]


def _content_length(environ: Mapping[str, Any]) -> int:
    raw = str(environ.get("CONTENT_LENGTH") or "0")
    if not raw.isascii() or not raw.isdecimal():
        return 1
    return int(raw)


def _endpoint_id(path: str) -> str:
    return {
        "/__ops/live": "live",
        "/__ops/ready/web-read": "ready_web_read",
        "/__ops/ready/workflow-mutation": "ready_workflow_mutation",
        "/__ops/diagnostics": "diagnostics",
        "/__ops/metrics": "metrics",
        "/_internal/mutation-admission": "mutation_admission",
    }.get(path, "unknown")


def _log_operation_request(
    *, endpoint: str, status_code: int, duration_ms: float
) -> None:
    release_id = os.getenv("RESEARCH_OPS_RELEASE_ID", "unconfigured")
    if not release_id or len(release_id) > 128:
        release_id = "unconfigured"
    record = {
        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "severity": "INFO" if status_code < 500 else "ERROR",
        "service_role": "operations-api",
        "release_id": release_id,
        "event_code": "operations_http_request",
        "correlation_id": str(uuid.uuid4()),
        "endpoint_id": endpoint,
        "status_code": int(status_code),
        "duration_ms": round(max(0.0, duration_ms), 3),
    }
    try:
        sys.stderr.write(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        )
        sys.stderr.flush()
    except Exception:
        pass


def _bounded_environment_seconds(name: str, *, default: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    if not raw or not raw.isascii() or not raw.isdecimal():
        return 0
    value = int(raw)
    return value if 1 <= value <= maximum else 0


operations_application = OperationsApplication()
guarded_web_application = MutationGuardedWebApplication()


__all__ = [
    "MutationGuardedWebApplication",
    "OperationsApplication",
    "guarded_web_application",
    "operations_application",
]
