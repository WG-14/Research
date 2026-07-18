"""Bounded, redacted health observations for the operations HTTP surface."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from market_research.application import ReleaseMetadata

from .database import connection
from .release import configured_release, configured_release_bundle_digest

CheckStatus = Literal["PASS", "FAIL", "STALE"]
AUDIT_OBSERVATION_KIND = "AUDIT_OUTBOX"
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_DJANGO_MIGRATION_RE = re.compile(r"^[0-9]{4}_[A-Za-z0-9_]+\.py$")
_OPS_MIGRATION_RE = re.compile(r"^[0-9]{4}_[A-Za-z0-9_]+\.sql$")
_UTC_SECONDS_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
_PREFLIGHT_FAILURE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_PREFLIGHT_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "checked_at",
        "git_sha",
        "release_id",
        "build_digest",
        "release_bundle_digest",
        "failure_code",
    }
)
_PREFLIGHT_RECEIPT_MAX_BYTES = 16_384
_SUPPORTED_LOCAL_FILESYSTEMS = frozenset({"ext4", "xfs", "btrfs"})
_ROOT_ROLES = {
    "data": "RESEARCH_DATA_ROOT",
    "artifact": "RESEARCH_ARTIFACT_ROOT",
    "report": "RESEARCH_REPORT_ROOT",
    "cache": "RESEARCH_CACHE_ROOT",
    "identity_registry": "RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime) -> str:
    normalized = value.astimezone(UTC)
    return normalized.isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class CheckResult:
    check_id: str
    status: CheckStatus
    reason_code: str
    observed_at: datetime
    count: int | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.check_id,
            "status": self.status,
            "reason_code": self.reason_code,
            "observed_at": iso_utc(self.observed_at),
        }
        if self.count is not None:
            payload["count"] = max(0, int(self.count))
        return payload


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    observed_at: datetime
    checks: tuple[CheckResult, ...]

    @property
    def ready(self) -> bool:
        return bool(self.checks) and all(item.status == "PASS" for item in self.checks)


@dataclass(frozen=True, slots=True)
class HealthPolicy:
    audit_observation_max_age_seconds: int = 300
    outbox_oldest_max_age_seconds: int = 60
    worker_heartbeat_max_age_seconds: int = 30
    minimum_outbox_workers: int = 1
    minimum_research_job_workers: int = 1
    cache_seconds: int = 5
    deployment_scope: str = "single-host"

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> HealthPolicy:
        scope = environ.get("RESEARCH_OPS_DEPLOYMENT_SCOPE", "single-host").strip()
        if scope not in {"single-host", "multi-host"}:
            raise ValueError("deployment_scope_invalid")
        return cls(
            audit_observation_max_age_seconds=_bounded_int(
                environ,
                "RESEARCH_OPS_AUDIT_OBSERVATION_MAX_AGE_SECONDS",
                default=300,
                minimum=10,
                maximum=86_400,
            ),
            outbox_oldest_max_age_seconds=_bounded_int(
                environ,
                "RESEARCH_OPS_OUTBOX_MAX_LAG_SECONDS",
                default=60,
                minimum=1,
                maximum=86_400,
            ),
            worker_heartbeat_max_age_seconds=_bounded_int(
                environ,
                "RESEARCH_OPS_WORKER_HEARTBEAT_MAX_AGE_SECONDS",
                default=30,
                minimum=5,
                maximum=3_600,
            ),
            minimum_outbox_workers=_bounded_int(
                environ,
                "RESEARCH_OPS_MINIMUM_OUTBOX_WORKERS",
                default=1,
                minimum=1,
                maximum=128,
            ),
            minimum_research_job_workers=_bounded_int(
                environ,
                "RESEARCH_OPS_MINIMUM_RESEARCH_JOB_WORKERS",
                default=1,
                minimum=1,
                maximum=128,
            ),
            cache_seconds=_bounded_int(
                environ,
                "RESEARCH_OPS_HEALTH_CACHE_SECONDS",
                default=5,
                minimum=1,
                maximum=60,
            ),
            deployment_scope=scope,
        )


class SnapshotCache:
    """Small in-process TTL cache; dependency scans never run per load-balancer hit."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, tuple[float, HealthSnapshot]] = {}

    def get(
        self,
        key: str,
        *,
        ttl_seconds: int,
        loader: Callable[[], HealthSnapshot],
    ) -> HealthSnapshot:
        now = time.monotonic()
        with self._lock:
            existing = self._values.get(key)
            if existing is not None and existing[0] > now:
                return existing[1]
        loaded = loader()
        with self._lock:
            self._values[key] = (now + ttl_seconds, loaded)
        return loaded

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


_CACHE = SnapshotCache()


def expected_migration_hashes() -> dict[str, str]:
    root = resources.files("research_operations.migrations")
    result: dict[str, str] = {}
    for item in sorted(root.iterdir(), key=lambda candidate: candidate.name):
        if item.name.endswith(".sql"):
            result[item.name] = hashlib.sha256(item.read_bytes()).hexdigest()
    return result


def expected_migration_digest() -> str:
    material = b"".join(
        f"{name}\0{digest}\n".encode()
        for name, digest in expected_migration_hashes().items()
    )
    return "sha256:" + hashlib.sha256(material).hexdigest()


def expected_platform_migration_digest() -> str:
    """Return the release-manifest digest for both shipped migration sets."""

    migrations = {
        "web": _resource_migration_metadata("portal.migrations", _DJANGO_MIGRATION_RE),
        "operations": _resource_migration_metadata(
            "research_operations.migrations", _OPS_MIGRATION_RE
        ),
    }
    return _canonical_digest(migrations)


def _resource_migration_metadata(
    package: str, pattern: re.Pattern[str]
) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for item in sorted(
        resources.files(package).iterdir(), key=lambda candidate: candidate.name
    ):
        if not pattern.fullmatch(item.name):
            continue
        payload = item.read_bytes()
        records.append(
            {
                "name": item.name.rsplit(".", 1)[0],
                "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        )
    if not records:
        raise RuntimeError(f"migration_set_missing_from_installed_package:{package}")
    return {
        "count": len(records),
        "latest": records[-1]["name"],
        "digest": _canonical_digest(records),
    }


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def expected_portal_migrations() -> tuple[str, ...]:
    """Return the exact Portal migration leaves shipped in the Web package."""

    root = resources.files("portal.migrations")
    names = tuple(
        item.name.removesuffix(".py")
        for item in sorted(root.iterdir(), key=lambda candidate: candidate.name)
        if _DJANGO_MIGRATION_RE.fullmatch(item.name)
    )
    if not names:
        raise RuntimeError("portal_migrations_missing_from_installed_package")
    return names


def release_configuration_check(
    environ: Mapping[str, str], *, observed_at: datetime
) -> CheckResult:
    try:
        configured_release(environ)
        configured_release_bundle_digest(environ)
    except ValueError:
        release_valid = False
    else:
        release_valid = True
    migration_digest = environ.get("RESEARCH_OPS_EXPECTED_MIGRATION_DIGEST", "").strip()
    allowed_hosts = {
        item.strip()
        for item in environ.get("INTERNAL_WEB_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    }
    secret = environ.get("INTERNAL_WEB_SECRET_KEY")
    hsts_raw = environ.get("INTERNAL_WEB_HSTS_SECONDS", "")
    web_security_valid = (
        environ.get("INTERNAL_WEB_DATABASE_ENGINE") == "postgresql"
        and environ.get("INTERNAL_WEB_DEBUG", "false").lower()
        not in {"1", "true", "yes"}
        and environ.get("INTERNAL_WEB_TRUST_X_FORWARDED_PROTO") == "true"
        and environ.get("INTERNAL_WEB_SECURE_SSL_REDIRECT") == "true"
        and environ.get("INTERNAL_WEB_SECURE_COOKIES") == "true"
        and bool(allowed_hosts)
        and "*" not in allowed_hosts
        and hsts_raw.isascii()
        and hsts_raw.isdecimal()
        and int(hsts_raw) > 0
        and (secret is None or len(secret) >= 32)
    )
    if (
        not release_valid
        or migration_digest != expected_platform_migration_digest()
        or not web_security_valid
    ):
        return CheckResult(
            "release_configuration",
            "FAIL",
            "release_configuration_invalid",
            observed_at,
        )
    return CheckResult(
        "release_configuration",
        "PASS",
        "release_configuration_valid",
        observed_at,
    )


def preflight_receipt_check(
    environ: Mapping[str, str],
    *,
    observed_at: datetime,
    receipt_loader: Callable[[Path], Mapping[str, object]] | None = None,
) -> CheckResult:
    """Require a fresh, release-bound PASS from the privileged preflight."""

    try:
        path_raw = environ.get("RESEARCH_OPS_PREFLIGHT_RECEIPT", "")
        path = Path(path_raw)
        if not path_raw or not path.is_absolute() or "\x00" in path_raw:
            raise _PreflightReceiptError("preflight_receipt_invalid")
        max_age = _required_bounded_int(
            environ,
            "RESEARCH_OPS_PREFLIGHT_MAX_AGE_SECONDS",
            minimum=300,
            maximum=172_800,
        )
        expected_release = configured_release(environ)
        bundle_digest = environ.get("RESEARCH_OPS_RELEASE_BUNDLE_DIGEST", "").strip()
        if not _HASH_RE.fullmatch(bundle_digest):
            raise _PreflightReceiptError("preflight_receipt_invalid")
        loader = _read_preflight_receipt if receipt_loader is None else receipt_loader
        receipt = loader(path)
        if set(receipt) != _PREFLIGHT_RECEIPT_FIELDS:
            raise _PreflightReceiptError("preflight_receipt_invalid")
        if receipt.get("schema_version") != 1 or isinstance(
            receipt.get("schema_version"), bool
        ):
            raise _PreflightReceiptError("preflight_receipt_invalid")
        status = receipt.get("status")
        failure_code = receipt.get("failure_code")
        if status == "FAIL":
            if not isinstance(failure_code, str) or not _PREFLIGHT_FAILURE_RE.fullmatch(
                failure_code
            ):
                raise _PreflightReceiptError("preflight_receipt_invalid")
            raise _PreflightReceiptError("preflight_receipt_failed")
        if status != "PASS" or failure_code is not None:
            raise _PreflightReceiptError("preflight_receipt_invalid")
        checked_at = _parse_utc_seconds(receipt.get("checked_at"))
        age = (observed_at.astimezone(UTC) - checked_at).total_seconds()
        if age < -60 or age > max_age:
            raise _PreflightReceiptError("preflight_receipt_stale")
        if (
            receipt.get("git_sha") != expected_release.git_sha
            or receipt.get("release_id") != expected_release.release_id
            or receipt.get("build_digest") != expected_release.build_digest
            or receipt.get("release_bundle_digest") != bundle_digest
        ):
            raise _PreflightReceiptError("preflight_release_mismatch")
    except _PreflightReceiptError as error:
        return CheckResult(
            "deployment_preflight",
            "FAIL",
            error.reason_code,
            observed_at,
        )
    except (OSError, TypeError, ValueError):
        return CheckResult(
            "deployment_preflight",
            "FAIL",
            "preflight_receipt_invalid",
            observed_at,
        )
    return CheckResult(
        "deployment_preflight",
        "PASS",
        "preflight_receipt_fresh",
        observed_at,
    )


class _PreflightReceiptError(ValueError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _read_preflight_receipt(path: Path, *, required_uid: int = 0) -> dict[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        status = os.fstat(descriptor)
        mode = stat.S_IMODE(status.st_mode)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != required_uid
            or mode & 0o022
            or status.st_size < 2
            or status.st_size > _PREFLIGHT_RECEIPT_MAX_BYTES
        ):
            raise _PreflightReceiptError("preflight_receipt_invalid")
        payload = os.read(descriptor, _PREFLIGHT_RECEIPT_MAX_BYTES + 1)
        if len(payload) != status.st_size:
            raise _PreflightReceiptError("preflight_receipt_invalid")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _PreflightReceiptError("preflight_receipt_invalid") from error
    if not isinstance(value, dict):
        raise _PreflightReceiptError("preflight_receipt_invalid")
    return value


def _parse_utc_seconds(value: object) -> datetime:
    if not isinstance(value, str) or not _UTC_SECONDS_RE.fullmatch(value):
        raise _PreflightReceiptError("preflight_receipt_invalid")
    try:
        return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise _PreflightReceiptError("preflight_receipt_invalid") from error


def _required_bounded_int(
    environ: Mapping[str, str], key: str, *, minimum: int, maximum: int
) -> int:
    raw = environ.get(key)
    if raw is None or not raw or not raw.isascii() or not raw.isdecimal():
        raise _PreflightReceiptError("preflight_receipt_invalid")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise _PreflightReceiptError("preflight_receipt_invalid")
    return value


def release_diagnostics(
    *,
    dsn: str | None = None,
    environ: Mapping[str, str] | None = None,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Return bounded release identity evidence for Web, Ops, and workers."""

    environment = os.environ if environ is None else environ
    now = observed_at or utcnow()
    try:
        release: ReleaseMetadata | None = configured_release(environment)
        configured_bundle_digest = configured_release_bundle_digest(environment)
    except ValueError:
        release = None
        configured_bundle_digest = ""
    try:
        max_age = HealthPolicy.from_environ(
            environment
        ).worker_heartbeat_max_age_seconds
    except (TypeError, ValueError):
        max_age = HealthPolicy().worker_heartbeat_max_age_seconds
    rows: list[tuple[Any, ...]] = []
    database_available = True
    try:
        with connection(dsn, connect_timeout=3) as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT
                        CASE
                            WHEN worker_id LIKE 'outbox:%%' THEN 'outbox-worker'
                            ELSE 'research-job-worker'
                        END AS service_role,
                        git_sha, release_id, build_digest, release_bundle_digest,
                        bool_and(release_seen_at = last_seen_at), count(*)
                    FROM research_ops.worker_heartbeat
                    WHERE (worker_id LIKE 'outbox:%%'
                           OR worker_id LIKE 'research-job:%%')
                      AND state IN ('STARTING', 'IDLE', 'WORKING', 'DRAINING')
                      AND last_seen_at >= %s
                    GROUP BY service_role, git_sha, release_id, build_digest,
                             release_bundle_digest
                    ORDER BY service_role, git_sha, release_id, build_digest,
                             release_bundle_digest
                    LIMIT 17
                    """,
                    (now - timedelta(seconds=max_age),),
                ).fetchall()
            )
    except Exception:
        database_available = False
    bundle_digest = configured_bundle_digest
    expected = (
        {**release.as_dict(), "release_bundle_digest": bundle_digest or None}
        if release is not None
        else None
    )
    workers = [
        {
            "service_role": str(row[0]),
            "git_sha": str(row[1]) or None,
            "release_id": str(row[2]) or None,
            "build_digest": str(row[3]) or None,
            "release_bundle_digest": str(row[4]) or None,
            "count": int(row[6]),
            "matches_runtime": bool(
                release is not None
                and row[1] == release.git_sha
                and row[2] == release.release_id
                and row[3] == release.build_digest
                and row[4] == bundle_digest
                and row[5] is True
            ),
        }
        for row in rows[:16]
    ]
    return {
        "schema_version": 1,
        "configured": release is not None and bool(bundle_digest),
        "components": {
            "internal_web": expected,
            "research_operations": expected,
        },
        "workers": workers,
        "workers_truncated": len(rows) > 16,
        "worker_state_available": database_available,
    }


def collect_health_snapshot(
    kind: Literal["web-read", "workflow-mutation"],
    *,
    dsn: str | None = None,
    environ: Mapping[str, str] | None = None,
    observed_at: datetime | None = None,
    use_cache: bool = True,
) -> HealthSnapshot:
    environment = os.environ if environ is None else environ
    try:
        policy = HealthPolicy.from_environ(environment)
    except (TypeError, ValueError):
        now = observed_at or utcnow()
        return HealthSnapshot(
            now,
            (CheckResult("health_policy", "FAIL", "health_policy_invalid", now),),
        )
    if kind not in {"web-read", "workflow-mutation"}:
        raise ValueError("health_snapshot_kind_invalid")

    def load() -> HealthSnapshot:
        return _collect_health_snapshot(
            kind,
            dsn=dsn,
            environ=environment,
            policy=policy,
            observed_at=observed_at or utcnow(),
        )

    if use_cache and observed_at is None:
        return _CACHE.get(kind, ttl_seconds=policy.cache_seconds, loader=load)
    return load()


def _collect_health_snapshot(
    kind: Literal["web-read", "workflow-mutation"],
    *,
    dsn: str | None,
    environ: Mapping[str, str],
    policy: HealthPolicy,
    observed_at: datetime,
) -> HealthSnapshot:
    try:
        expected_release: ReleaseMetadata | None = configured_release(environ)
        expected_release_bundle_digest: str | None = configured_release_bundle_digest(
            environ
        )
    except ValueError:
        expected_release = None
        expected_release_bundle_digest = None
    checks: list[CheckResult] = [
        release_configuration_check(environ, observed_at=observed_at),
        preflight_receipt_check(environ, observed_at=observed_at),
        _filesystem_check(
            environ,
            policy=policy,
            require_write=kind == "workflow-mutation",
            observed_at=observed_at,
        ),
    ]
    try:
        database = _database_snapshot(
            dsn=dsn,
            observed_at=observed_at,
            worker_heartbeat_max_age_seconds=(policy.worker_heartbeat_max_age_seconds),
            expected_release=expected_release,
            expected_release_bundle_digest=expected_release_bundle_digest,
        )
    except Exception:
        checks.extend(_database_unavailable_checks(kind, observed_at))
        return HealthSnapshot(observed_at, tuple(checks))

    checks.extend(
        (
            _database_primary_check(database, observed_at),
            _migration_check(database, observed_at),
            _quarantine_check(database, observed_at),
            _worker_release_check(database, observed_at),
        )
    )
    if kind == "workflow-mutation":
        checks.extend(
            (
                _admission_check(database, observed_at),
                _audit_observation_check(database, policy, observed_at),
                _outbox_backlog_check(database, policy, observed_at),
                _outbox_worker_check(database, policy, observed_at),
                _research_job_worker_check(database, policy, observed_at),
                _job_receipt_check(database, observed_at),
            )
        )
    return HealthSnapshot(observed_at, tuple(checks))


def _database_snapshot(
    *,
    dsn: str | None,
    observed_at: datetime,
    worker_heartbeat_max_age_seconds: int,
    expected_release: ReleaseMetadata | None = None,
    expected_release_bundle_digest: str | None = None,
) -> dict[str, Any]:
    expected = expected_migration_hashes()
    expected_portal = expected_portal_migrations()
    with connection(dsn, connect_timeout=3) as conn:
        # Prove the service role can execute a rollback-safe write without
        # touching durable application state. The temporary relation is scoped
        # to this connection and dropped at commit/close.
        conn.execute(
            """
            CREATE TEMPORARY TABLE research_ops_readiness_write_probe (
                value integer NOT NULL
            ) ON COMMIT DROP
            """
        )
        conn.execute("INSERT INTO research_ops_readiness_write_probe(value) VALUES (1)")
        primary = conn.execute(
            """
            SELECT pg_is_in_recovery(), current_setting('transaction_read_only'), 1
            """
        ).fetchone()
        migrations = conn.execute(
            """
            SELECT name, content_hash
            FROM research_ops.migration_history
            ORDER BY name
            """
        ).fetchall()
        portal_migrations = conn.execute(
            """
            SELECT name
            FROM django_migrations
            WHERE app = 'portal'
            ORDER BY name
            """
        ).fetchall()
        control = conn.execute(
            """
            SELECT mutation_admission_open, claim_admission_open,
                   integrity_quarantine, generation
            FROM research_ops.runtime_control
            WHERE singleton_id = 1
            """
        ).fetchone()
        outbox = conn.execute(
            """
            SELECT
                count(*) FILTER (WHERE status = 'PENDING'),
                count(*) FILTER (WHERE status = 'CLAIMED'),
                count(*) FILTER (WHERE status = 'DEAD_LETTER'),
                COALESCE(EXTRACT(EPOCH FROM
                    (%s - min(created_at) FILTER (
                        WHERE status IN ('PENDING', 'CLAIMED')
                    ))), 0)
            FROM research_ops.outbox_delivery
            """,
            (observed_at,),
        ).fetchone()
        heartbeat = conn.execute(
            """
            SELECT
                count(*) FILTER (
                    WHERE worker_id LIKE 'outbox:%%'
                      AND state IN ('IDLE', 'WORKING')
                ),
                count(*) FILTER (
                    WHERE worker_id LIKE 'research-job:%%'
                      AND state IN ('IDLE', 'WORKING')
                ),
                count(*) FILTER (
                    WHERE git_sha <> %s
                       OR release_id <> %s
                       OR build_digest <> %s
                       OR release_bundle_digest <> %s
                       OR release_seen_at IS DISTINCT FROM last_seen_at
                )
            FROM research_ops.worker_heartbeat
            WHERE (worker_id LIKE 'outbox:%%'
                   OR worker_id LIKE 'research-job:%%')
              AND state IN ('STARTING', 'IDLE', 'WORKING', 'DRAINING')
              AND last_seen_at >= %s
            """,
            (
                expected_release.git_sha if expected_release is not None else "",
                expected_release.release_id if expected_release is not None else "",
                (expected_release.build_digest if expected_release is not None else ""),
                expected_release_bundle_digest or "",
                observed_at - timedelta(seconds=worker_heartbeat_max_age_seconds),
            ),
        ).fetchone()
        unapplied_receipts = conn.execute(
            """
            SELECT count(*)
            FROM research_ops.research_job_result_receipt
            WHERE applied_at IS NULL
            """
        ).fetchone()
        audit = conn.execute(
            """
            SELECT status, reason_code, reason_count, observed_at, evidence_hash
            FROM research_ops.validation_observation
            WHERE kind = %s
            """,
            (AUDIT_OBSERVATION_KIND,),
        ).fetchone()
    if heartbeat is None:
        raise RuntimeError("worker_heartbeat_snapshot_missing")
    if unapplied_receipts is None:
        raise RuntimeError("research_job_receipt_snapshot_missing")
    return {
        "primary": primary,
        "migrations": dict(migrations),
        "expected_migrations": expected,
        "portal_migrations": tuple(str(row[0]) for row in portal_migrations),
        "expected_portal_migrations": expected_portal,
        "control": control,
        "outbox": outbox,
        "fresh_outbox_workers": int(heartbeat[0]),
        "fresh_research_job_workers": int(heartbeat[1]),
        "worker_release_mismatch_count": int(heartbeat[2]),
        "expected_release_configured": (
            expected_release is not None and expected_release_bundle_digest is not None
        ),
        "unapplied_job_receipts": int(unapplied_receipts[0]),
        "audit": audit,
    }


def _database_primary_check(
    database: Mapping[str, Any], observed_at: datetime
) -> CheckResult:
    row = database.get("primary")
    if row is None or bool(row[0]) or str(row[1]).lower() != "off" or row[2] != 1:
        return CheckResult(
            "database_primary", "FAIL", "database_not_writable_primary", observed_at
        )
    return CheckResult(
        "database_primary", "PASS", "database_primary_transaction_ok", observed_at
    )


def _migration_check(database: Mapping[str, Any], observed_at: datetime) -> CheckResult:
    actual = database.get("migrations")
    expected = database.get("expected_migrations")
    actual_portal = database.get("portal_migrations")
    expected_portal = database.get("expected_portal_migrations")
    ops_mismatch_count = _mapping_mismatch_count(actual, expected)
    portal_mismatch_count = _sequence_mismatch_count(actual_portal, expected_portal)
    if ops_mismatch_count or portal_mismatch_count:
        return CheckResult(
            "migration_leaves",
            "FAIL",
            "migration_leaves_mismatch",
            observed_at,
            ops_mismatch_count + portal_mismatch_count,
        )
    migration_count = (len(actual) if isinstance(actual, dict) else 0) + (
        len(actual_portal) if isinstance(actual_portal, (list, tuple)) else 0
    )
    return CheckResult(
        "migration_leaves",
        "PASS",
        "migration_leaves_match",
        observed_at,
        migration_count,
    )


def _mapping_mismatch_count(actual: object, expected: object) -> int:
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        return 1
    names = set(actual) | set(expected)
    return sum(1 for name in names if actual.get(name) != expected.get(name))


def _sequence_mismatch_count(actual: object, expected: object) -> int:
    if not isinstance(actual, (list, tuple)) or not isinstance(expected, (list, tuple)):
        return 1
    actual_names = tuple(actual)
    expected_names = tuple(expected)
    if actual_names == expected_names:
        return 0
    return max(1, len(set(actual_names) ^ set(expected_names)))


def _quarantine_check(
    database: Mapping[str, Any], observed_at: datetime
) -> CheckResult:
    control = database.get("control")
    if control is None or bool(control[2]):
        return CheckResult(
            "integrity_quarantine",
            "FAIL",
            "integrity_quarantine_active",
            observed_at,
        )
    return CheckResult(
        "integrity_quarantine",
        "PASS",
        "integrity_quarantine_clear",
        observed_at,
    )


def _worker_release_check(
    database: Mapping[str, Any], observed_at: datetime
) -> CheckResult:
    if not bool(database.get("expected_release_configured")):
        return CheckResult(
            "worker_release",
            "FAIL",
            "worker_release_configuration_missing",
            observed_at,
        )
    mismatch_count = int(database.get("worker_release_mismatch_count", 0))
    if mismatch_count:
        return CheckResult(
            "worker_release",
            "FAIL",
            "worker_release_mismatch",
            observed_at,
            mismatch_count,
        )
    return CheckResult("worker_release", "PASS", "worker_release_match", observed_at, 0)


def _admission_check(database: Mapping[str, Any], observed_at: datetime) -> CheckResult:
    control = database.get("control")
    if (
        control is None
        or not bool(control[0])
        or not bool(control[1])
        or bool(control[2])
    ):
        return CheckResult(
            "workflow_admission",
            "FAIL",
            "workflow_admission_closed",
            observed_at,
        )
    return CheckResult(
        "workflow_admission",
        "PASS",
        "workflow_admission_open",
        observed_at,
    )


def _audit_observation_check(
    database: Mapping[str, Any],
    policy: HealthPolicy,
    observed_at: datetime,
) -> CheckResult:
    row = database.get("audit")
    if row is None:
        return CheckResult(
            "audit_validation",
            "STALE",
            "audit_validation_missing",
            observed_at,
        )
    status, reason_code, reason_count, observation_time, _evidence_hash = row
    age = max(0.0, (observed_at - observation_time).total_seconds())
    if age > policy.audit_observation_max_age_seconds:
        return CheckResult(
            "audit_validation",
            "STALE",
            "audit_validation_stale",
            observation_time,
            int(reason_count),
        )
    if status != "PASS":
        safe_reason = str(reason_code)
        if not _REASON_RE.fullmatch(safe_reason):
            safe_reason = "audit_validation_failed"
        return CheckResult(
            "audit_validation",
            "FAIL",
            safe_reason,
            observation_time,
            int(reason_count),
        )
    return CheckResult(
        "audit_validation",
        "PASS",
        "audit_validation_passed",
        observation_time,
        0,
    )


def _outbox_backlog_check(
    database: Mapping[str, Any],
    policy: HealthPolicy,
    observed_at: datetime,
) -> CheckResult:
    row = database.get("outbox")
    if row is None:
        return CheckResult(
            "outbox_delivery", "FAIL", "outbox_state_unavailable", observed_at
        )
    pending, claimed, dead_letter, oldest_age = (
        int(row[0]),
        int(row[1]),
        int(row[2]),
        max(0.0, float(row[3])),
    )
    if dead_letter:
        return CheckResult(
            "outbox_delivery",
            "FAIL",
            "outbox_dead_letter_present",
            observed_at,
            dead_letter,
        )
    if oldest_age > policy.outbox_oldest_max_age_seconds:
        return CheckResult(
            "outbox_delivery",
            "FAIL",
            "outbox_delivery_lag_exceeded",
            observed_at,
            pending + claimed,
        )
    return CheckResult(
        "outbox_delivery",
        "PASS",
        "outbox_delivery_within_slo",
        observed_at,
        pending + claimed,
    )


def _outbox_worker_check(
    database: Mapping[str, Any],
    policy: HealthPolicy,
    observed_at: datetime,
) -> CheckResult:
    count = int(database.get("fresh_outbox_workers", 0))
    if count < policy.minimum_outbox_workers:
        return CheckResult(
            "outbox_workers",
            "FAIL",
            "outbox_worker_pool_unavailable",
            observed_at,
            count,
        )
    return CheckResult(
        "outbox_workers",
        "PASS",
        "outbox_worker_pool_fresh",
        observed_at,
        count,
    )


def _research_job_worker_check(
    database: Mapping[str, Any],
    policy: HealthPolicy,
    observed_at: datetime,
) -> CheckResult:
    count = int(database.get("fresh_research_job_workers", 0))
    if count < policy.minimum_research_job_workers:
        return CheckResult(
            "research_job_workers",
            "FAIL",
            "research_job_worker_pool_unavailable",
            observed_at,
            count,
        )
    return CheckResult(
        "research_job_workers",
        "PASS",
        "research_job_worker_pool_fresh",
        observed_at,
        count,
    )


def _job_receipt_check(
    database: Mapping[str, Any], observed_at: datetime
) -> CheckResult:
    count = int(database.get("unapplied_job_receipts", 0))
    if count:
        return CheckResult(
            "research_job_receipts",
            "FAIL",
            "research_job_receipt_unapplied",
            observed_at,
            count,
        )
    return CheckResult(
        "research_job_receipts",
        "PASS",
        "research_job_receipts_applied",
        observed_at,
        0,
    )


def _database_unavailable_checks(
    kind: Literal["web-read", "workflow-mutation"], observed_at: datetime
) -> tuple[CheckResult, ...]:
    checks = [
        CheckResult("database_primary", "FAIL", "database_unavailable", observed_at),
        CheckResult(
            "migration_leaves", "FAIL", "migration_state_unavailable", observed_at
        ),
        CheckResult(
            "integrity_quarantine", "FAIL", "runtime_control_unavailable", observed_at
        ),
        CheckResult(
            "worker_release", "FAIL", "worker_release_state_unavailable", observed_at
        ),
    ]
    if kind == "workflow-mutation":
        checks.extend(
            (
                CheckResult(
                    "workflow_admission",
                    "FAIL",
                    "workflow_admission_unavailable",
                    observed_at,
                ),
                CheckResult(
                    "audit_validation",
                    "STALE",
                    "audit_validation_unavailable",
                    observed_at,
                ),
                CheckResult(
                    "outbox_delivery",
                    "FAIL",
                    "outbox_state_unavailable",
                    observed_at,
                ),
                CheckResult(
                    "outbox_workers",
                    "FAIL",
                    "outbox_worker_state_unavailable",
                    observed_at,
                ),
                CheckResult(
                    "research_job_workers",
                    "FAIL",
                    "research_job_worker_state_unavailable",
                    observed_at,
                ),
                CheckResult(
                    "research_job_receipts",
                    "FAIL",
                    "research_job_receipt_state_unavailable",
                    observed_at,
                ),
            )
        )
    return tuple(checks)


def _filesystem_check(
    environ: Mapping[str, str],
    *,
    policy: HealthPolicy,
    require_write: bool,
    observed_at: datetime,
) -> CheckResult:
    try:
        paths = _configured_root_paths(environ)
        receipt = _load_qualification_receipt(environ)
        _verify_root_qualification(paths, receipt, policy)
        source_root_raw = environ.get("RESEARCH_OPS_SOURCE_ROOT", "").strip()
        source_root = (
            Path(source_root_raw).resolve(strict=True) if source_root_raw else None
        )
        for role, path in paths.items():
            candidate = (
                path.parent
                if role == "identity_registry" and not path.exists()
                else path
            )
            if not candidate.exists() or _has_symlink_component(candidate):
                raise ValueError("root_missing_or_symlink")
            resolved = candidate.resolve(strict=True)
            if source_root is not None and _is_within(resolved, source_root):
                raise ValueError("root_inside_source")
            if not os.access(resolved, os.R_OK):
                raise ValueError("root_not_readable")
            if require_write and role != "data":
                writable = resolved if resolved.is_dir() else resolved.parent
                if not os.access(writable, os.W_OK | os.X_OK):
                    raise ValueError("root_not_writable")
        manifest_root = paths["data"] / "_internal_web" / "manifests"
        if require_write and (
            not manifest_root.is_dir()
            or _has_symlink_component(manifest_root)
            or not os.access(manifest_root, os.W_OK | os.X_OK)
        ):
            raise ValueError("manifest_root_not_writable")
        db_raw = environ.get("RESEARCH_DB_PATH", "").strip()
        if db_raw:
            db_path = Path(db_raw)
            if (
                not db_path.is_absolute()
                or not db_path.is_file()
                or _has_symlink_component(db_path)
                or not os.access(db_path, os.R_OK)
            ):
                raise ValueError("research_db_invalid")
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return CheckResult(
            "filesystem_roots",
            "FAIL",
            (
                "filesystem_write_policy_invalid"
                if require_write
                else "filesystem_read_policy_invalid"
            ),
            observed_at,
        )
    return CheckResult(
        "filesystem_roots",
        "PASS",
        (
            "filesystem_write_policy_qualified"
            if require_write
            else "filesystem_read_policy_qualified"
        ),
        observed_at,
        len(paths),
    )


def _configured_root_paths(environ: Mapping[str, str]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for role, key in _ROOT_ROLES.items():
        raw = environ.get(key, "").strip()
        path = Path(raw)
        if not raw or not path.is_absolute():
            raise ValueError("root_configuration_invalid")
        paths[role] = path
    return paths


def root_fingerprint(role: str, path: Path) -> str:
    resolved = str(path.expanduser().resolve(strict=False))
    material = f"research-operations-root-v1\0{role}\0{resolved}".encode()
    return "sha256:" + hashlib.sha256(material).hexdigest()


def filesystem_identity(role: str, path: Path) -> str:
    target = path if path.is_dir() else path.parent
    stat = target.stat()
    filesystem = os.statvfs(target)
    material = (
        f"research-operations-filesystem-v1\0{role}\0{stat.st_dev}\0"
        f"{getattr(filesystem, 'f_fsid', 0)}\0{filesystem.f_bsize}"
    ).encode()
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _load_qualification_receipt(environ: Mapping[str, str]) -> dict[str, Any]:
    raw = environ.get("RESEARCH_OPS_FILESYSTEM_QUALIFICATION_RECEIPT", "").strip()
    path = Path(raw)
    if not raw or not path.is_absolute() or _has_symlink_component(path):
        raise ValueError("qualification_receipt_invalid")
    stat = path.stat()
    if not path.is_file() or stat.st_size <= 0 or stat.st_size > 65_536:
        raise ValueError("qualification_receipt_invalid")
    with path.open("rb") as handle:
        payload = handle.read(65_537)
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("qualification_receipt_invalid")
    return value


def _verify_root_qualification(
    paths: Mapping[str, Path],
    receipt: Mapping[str, Any],
    policy: HealthPolicy,
) -> None:
    if receipt.get("schema_version") != 1 or receipt.get("status") != "PASS":
        raise ValueError("qualification_receipt_failed")
    if receipt.get("boot_id_hash") != _boot_id_hash():
        raise ValueError("qualification_host_boot_changed")
    qualified_at = receipt.get("qualified_at")
    if not isinstance(qualified_at, str) or not qualified_at.endswith("Z"):
        raise ValueError("qualification_receipt_invalid")
    qualifications = receipt.get("roots")
    if not isinstance(qualifications, list) or len(qualifications) > 32:
        raise ValueError("qualification_receipt_invalid")
    observed: dict[str, Mapping[str, Any]] = {}
    for item in qualifications:
        if not isinstance(item, dict) or not isinstance(item.get("role"), str):
            raise ValueError("qualification_receipt_invalid")
        observed[str(item["role"])] = item
    for role, path in paths.items():
        item = observed.get(role)
        target = path if path.is_dir() else path.parent
        mount_id, device_id, filesystem_type = _mount_identity(target)
        if (
            item is None
            or item.get("status") != "PASS"
            or item.get("root_fingerprint") != root_fingerprint(role, path)
            or item.get("filesystem_identity") != filesystem_identity(role, path)
            or item.get("atomic_replace") != "PASS"
            or item.get("durable_fsync") != "PASS"
            or item.get("process_lock") != "PASS"
            or item.get("concurrent_append") != "PASS"
            or item.get("mount_id") != mount_id
            or item.get("device_id") != device_id
            or item.get("filesystem_type") != filesystem_type
            or filesystem_type not in _SUPPORTED_LOCAL_FILESYSTEMS
        ):
            raise ValueError("root_not_qualified")
    if (
        policy.deployment_scope == "multi-host"
        and receipt.get("cross_host_status") != "PASS"
    ):
        raise ValueError("cross_host_not_qualified")


def _mount_identity(path: Path) -> tuple[int, str, str]:
    resolved = path.resolve(strict=True)
    matches: list[tuple[int, int, str, str]] = []
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        before, separator, after = line.partition(" - ")
        fields = before.split()
        after_fields = after.split()
        if not separator or len(fields) < 6 or not after_fields:
            continue
        mount_point = Path(
            fields[4]
            .replace("\\040", " ")
            .replace("\\011", "\t")
            .replace("\\134", "\\")
        )
        try:
            resolved.relative_to(mount_point)
        except ValueError:
            continue
        matches.append(
            (len(mount_point.parts), int(fields[0]), fields[2], after_fields[0])
        )
    if not matches:
        raise ValueError("qualification_mount_unavailable")
    _depth, mount_id, device_id, filesystem_type = max(matches)
    return mount_id, device_id, filesystem_type


def _boot_id_hash() -> str:
    boot_id = (
        Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    )
    return "sha256:" + hashlib.sha256(boot_id.encode("ascii")).hexdigest()


def _has_symlink_component(path: Path) -> bool:
    absolute = path.expanduser()
    if not absolute.is_absolute():
        return True
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def record_audit_validation(
    *,
    dsn: str | None = None,
    validator: Callable[[], Mapping[str, Any]] | None = None,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Run the full validator off the probe path and persist only a safe aggregate."""

    now = observed_at
    try:
        result = dict((validator or _default_audit_validator)())
        passed = result.get("status") == "PASS"
        reasons = result.get("reasons")
        reason_count = (
            len(reasons) if isinstance(reasons, list) else (0 if passed else 1)
        )
        counts = {
            key: _nonnegative_int(result.get(key))
            for key in (
                "row_count",
                "outbox_event_count",
                "projected_event_count",
                "pending_event_count",
                "duplicate_projection_count",
                "orphan_projection_count",
                "unmarked_projection_count",
            )
        }
        stream_hash = result.get("stream_hash")
        if stream_hash is not None and not _HASH_RE.fullmatch(str(stream_hash)):
            passed = False
            reason_count += 1
            stream_hash = None
        safe_counts = counts
        safe_stream_hash = str(stream_hash) if stream_hash is not None else None
        safe_evidence: dict[str, object] = {
            "status": "PASS" if passed else "FAIL",
            "counts": safe_counts,
            "stream_hash": safe_stream_hash,
        }
        evidence_hash = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    safe_evidence, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
        )
        status = "PASS" if passed else "FAIL"
        reason_code = (
            "audit_validation_passed" if passed else "audit_integrity_validation_failed"
        )
    except Exception:
        status = "FAIL"
        reason_code = "audit_validator_unavailable"
        reason_count = 1
        evidence_hash = ""
        safe_counts = {}
        safe_stream_hash = None
        safe_evidence = {
            "status": "FAIL",
            "counts": safe_counts,
            "stream_hash": safe_stream_hash,
        }
    now = now or utcnow()
    with connection(dsn) as conn:
        conn.execute(
            """
            INSERT INTO research_ops.validation_observation (
                kind, status, reason_code, reason_count, observed_at, evidence_hash,
                row_count, terminal_hash
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (kind) DO UPDATE SET
                status = EXCLUDED.status,
                reason_code = EXCLUDED.reason_code,
                reason_count = EXCLUDED.reason_count,
                observed_at = EXCLUDED.observed_at,
                evidence_hash = EXCLUDED.evidence_hash,
                row_count = EXCLUDED.row_count,
                terminal_hash = EXCLUDED.terminal_hash
            """,
            (
                AUDIT_OBSERVATION_KIND,
                status,
                reason_code,
                reason_count,
                now,
                evidence_hash,
                safe_counts.get("row_count", 0),
                safe_stream_hash or "",
            ),
        )
    _CACHE.clear()
    return {
        "schema_version": 1,
        "status": status,
        "reason_code": reason_code,
        "reason_count": reason_count,
        "observed_at": iso_utc(now),
        "evidence_hash": evidence_hash or None,
        "evidence": safe_evidence,
    }


def _default_audit_validator() -> Mapping[str, Any]:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "market_research_web.settings")
    import django

    django.setup()
    from market_research_web.operations_contract import validate_web_audit_outbox

    return validate_web_audit_outbox()


def _bounded_int(
    environ: Mapping[str, str],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = environ.get(key)
    if raw is None:
        return default
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise ValueError(f"{key}_invalid")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{key}_invalid")
    return value


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("count_invalid")
    if value is None:
        return 0
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        raise ValueError("count_invalid")
    parsed = int(value)
    if parsed < 0:
        raise ValueError("count_invalid")
    return parsed


__all__ = [
    "AUDIT_OBSERVATION_KIND",
    "CheckResult",
    "HealthPolicy",
    "HealthSnapshot",
    "SnapshotCache",
    "collect_health_snapshot",
    "expected_migration_digest",
    "expected_migration_hashes",
    "expected_platform_migration_digest",
    "expected_portal_migrations",
    "filesystem_identity",
    "iso_utc",
    "preflight_receipt_check",
    "record_audit_validation",
    "release_configuration_check",
    "release_diagnostics",
    "root_fingerprint",
    "utcnow",
]
