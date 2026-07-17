"""Coherent backup fencing, signed manifests, and offline recovery checks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from psycopg import sql

from .database import RUNTIME_CONTROL_ADVISORY_LOCK_ID, connection
from .health import (
    AUDIT_OBSERVATION_KIND,
    expected_migration_digest,
    expected_migration_hashes,
    expected_platform_migration_digest,
    expected_portal_migrations,
    iso_utc,
    utcnow,
)

MUTATION_FENCE_ADVISORY_LOCK_ID = RUNTIME_CONTROL_ADVISORY_LOCK_ID
BACKUP_MANIFEST_SCHEMA_VERSION = 2
RECOVERY_RECEIPT_SCHEMA_VERSION = 2
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_BACKUP_FILES = 32
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REQUIRED_BACKUP_ROLES = frozenset(
    {"postgresql", "data", "manifest", "artifact", "report", "identity_registry"}
)


class BackupContractError(RuntimeError):
    """A stable fail-closed backup or recovery contract failure."""


@dataclass(frozen=True, slots=True)
class FenceStatus:
    phase: Literal["OPEN", "DRAINING", "SEALED", "QUARANTINED"]
    generation: int
    fence_token: uuid.UUID | None
    changed_at: datetime
    active_jobs: int
    active_experiment_claims: int
    pending_outbox: int
    claimed_outbox: int
    dead_letter_outbox: int
    unprojected_audit_intents: int
    unapplied_job_receipts: int
    audit_validation_status: str
    audit_validation_reason_count: int
    audit_validation_observed_at: datetime | None
    latest_audit_projection_at: datetime | None
    audit_row_count: int
    audit_terminal_hash: str

    def as_dict(self, *, include_token: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "phase": self.phase,
            "generation": self.generation,
            "changed_at": iso_utc(self.changed_at),
            "counts": {
                "active_jobs": self.active_jobs,
                "active_experiment_claims": self.active_experiment_claims,
                "pending_outbox": self.pending_outbox,
                "claimed_outbox": self.claimed_outbox,
                "dead_letter_outbox": self.dead_letter_outbox,
                "unprojected_audit_intents": self.unprojected_audit_intents,
                "unapplied_job_receipts": self.unapplied_job_receipts,
            },
            "audit_validation": {
                "status": self.audit_validation_status,
                "reason_count": self.audit_validation_reason_count,
                "observed_at": (
                    iso_utc(self.audit_validation_observed_at)
                    if self.audit_validation_observed_at is not None
                    else None
                ),
                "latest_projection_at": (
                    iso_utc(self.latest_audit_projection_at)
                    if self.latest_audit_projection_at is not None
                    else None
                ),
                "row_count": self.audit_row_count,
                "terminal_hash": self.audit_terminal_hash or None,
            },
        }
        if include_token:
            payload["fence_token"] = (
                str(self.fence_token) if self.fence_token is not None else None
            )
        else:
            payload["fence_token_hash"] = (
                _fence_token_hash(self.fence_token)
                if self.fence_token is not None
                else None
            )
        return payload


@dataclass(frozen=True, slots=True)
class VerifiedBackup:
    backup_id: uuid.UUID
    manifest_hash: str
    git_sha: str
    release_id: str
    build_digest: str
    release_bundle_digest: str
    migration_digest: str
    postgresql_major: int
    fence_generation: int
    fence_token_hash: str
    created_at: datetime
    audit_row_count: int
    audit_terminal_hash: str
    files: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": "PASS",
            "backup_id": str(self.backup_id),
            "manifest_hash": self.manifest_hash,
            "git_sha": self.git_sha or None,
            "release_id": self.release_id,
            "build_digest": self.build_digest,
            "release_bundle_digest": self.release_bundle_digest or None,
            "migration_digest": self.migration_digest,
            "created_at": iso_utc(self.created_at),
            "file_count": len(self.files),
            "audit_row_count": self.audit_row_count,
            "audit_terminal_hash": self.audit_terminal_hash or None,
        }


@dataclass(frozen=True, slots=True)
class RecoveryVerification:
    status: Literal["PASS", "FAIL"]
    backup_manifest_hash: str
    started_at: datetime
    finished_at: datetime
    checks: tuple[dict[str, Any], ...]
    git_sha: str = ""
    release_id: str = ""
    build_digest: str = ""
    release_bundle_digest: str = ""

    def __post_init__(self) -> None:
        if self.git_sha:
            _git_sha(self.git_sha, "recovery_git_sha")
            _release_id(self.release_id)
            _sha256(self.build_digest, "recovery_build_digest")
            _sha256(
                self.release_bundle_digest,
                "recovery_release_bundle_digest",
            )
        elif self.release_id or self.build_digest or self.release_bundle_digest:
            raise ValueError("recovery_release_binding_incomplete")

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.finished_at - self.started_at).total_seconds())

    def document(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": RECOVERY_RECEIPT_SCHEMA_VERSION if self.git_sha else 1,
            "status": self.status,
            "backup_manifest_hash": self.backup_manifest_hash,
            "started_at": iso_utc(self.started_at),
            "finished_at": iso_utc(self.finished_at),
            "duration_seconds": round(self.duration_seconds, 6),
            "checks": list(self.checks),
        }
        if self.git_sha:
            payload["release"] = {
                "git_sha": self.git_sha,
                "release_id": self.release_id,
                "build_digest": self.build_digest,
                "release_bundle_digest": self.release_bundle_digest,
            }
        return payload


class BackupFenceStore:
    """Two-phase fence ordered with web mutations and durable worker claims."""

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn

    def begin(
        self,
        *,
        operator_id: str,
        reason: str,
        fence_token: uuid.UUID | str | None = None,
        now: datetime | None = None,
    ) -> FenceStatus:
        """Drain in-flight mutations and block new ones; keep delivery claims open."""

        operator = _bounded_text(operator_id, "operator_id", maximum=255)
        normalized_reason = _bounded_text(reason, "reason", maximum=255)
        observed_at = now or utcnow()
        token = _uuid(fence_token or uuid.uuid4(), "fence_token")
        with connection(self._dsn) as conn:
            # The guarded web WSGI wrapper holds the shared form of this lock for
            # every non-safe request.  Taking it exclusively drains requests that
            # already passed admission before the flag is changed.
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (MUTATION_FENCE_ADVISORY_LOCK_ID,),
            )
            control = conn.execute(
                """
                SELECT mutation_admission_open, claim_admission_open,
                       integrity_quarantine
                FROM research_ops.runtime_control
                WHERE singleton_id = 1
                FOR UPDATE
                """
            ).fetchone()
            if control is None:
                raise BackupContractError("runtime_control_missing")
            if bool(control[2]):
                raise BackupContractError("integrity_quarantine_active")
            if not bool(control[0]) or not bool(control[1]):
                raise BackupContractError("backup_fence_not_open")
            conn.execute(
                """
                UPDATE research_ops.runtime_control
                SET mutation_admission_open = false,
                    claim_admission_open = true,
                    integrity_quarantine = false,
                    generation = generation + 1,
                    fence_token = %s,
                    requested_by = %s,
                    reason = %s,
                    closed_at = %s,
                    reopened_at = NULL,
                    changed_at = %s,
                    last_verified_manifest_hash = ''
                WHERE singleton_id = 1
                """,
                (token, operator, normalized_reason, observed_at, observed_at),
            )
        return self.status()

    def status(self, *, now: datetime | None = None) -> FenceStatus:
        observed_at = now or utcnow()
        with connection(self._dsn) as conn:
            return _load_fence_status(conn, observed_at=observed_at)

    def seal(
        self,
        *,
        fence_token: uuid.UUID | str,
        audit_observation_max_age_seconds: int = 300,
        now: datetime | None = None,
    ) -> FenceStatus:
        """Atomically stop claims only after every authoritative writer is drained."""

        token = _uuid(fence_token, "fence_token")
        if not 10 <= audit_observation_max_age_seconds <= 86_400:
            raise ValueError("audit_observation_max_age_seconds_invalid")
        observed_at = now or utcnow()
        with connection(self._dsn) as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (MUTATION_FENCE_ADVISORY_LOCK_ID,),
            )
            control = conn.execute(
                """
                SELECT mutation_admission_open, claim_admission_open,
                       integrity_quarantine, fence_token
                FROM research_ops.runtime_control
                WHERE singleton_id = 1
                FOR UPDATE
                """
            ).fetchone()
            if control is None:
                raise BackupContractError("runtime_control_missing")
            if control[3] != token:
                raise BackupContractError("backup_fence_token_mismatch")
            if bool(control[2]):
                raise BackupContractError("integrity_quarantine_active")
            if bool(control[0]):
                raise BackupContractError("backup_fence_mutation_admission_open")
            status = _load_fence_status(conn, observed_at=observed_at)
            if not bool(control[1]):
                if status.phase != "SEALED":
                    raise BackupContractError("backup_fence_state_invalid")
                return status
            writer_reasons = _quiescence_reasons(
                status,
                observed_at=observed_at,
                audit_observation_max_age_seconds=audit_observation_max_age_seconds,
                include_audit=False,
            )
            if writer_reasons:
                raise BackupContractError(
                    "backup_not_quiescent:" + ",".join(writer_reasons)
                )
            # The exclusive advisory lock blocks every new claim/acquisition.
            # With web mutations already fenced and all
            # writer counts at zero, this full validation has a stable source.
            from .health import record_audit_validation

            observation = record_audit_validation(dsn=self._dsn)
            if observation["status"] != "PASS":
                raise BackupContractError("backup_audit_validation_failed")
            observed_at = utcnow()
            status = _load_fence_status(conn, observed_at=observed_at)
            reasons = _quiescence_reasons(
                status,
                observed_at=observed_at,
                audit_observation_max_age_seconds=audit_observation_max_age_seconds,
                include_audit=True,
            )
            if reasons:
                raise BackupContractError("backup_not_quiescent:" + ",".join(reasons))
            conn.execute(
                """
                UPDATE research_ops.runtime_control
                SET claim_admission_open = false, changed_at = %s
                WHERE singleton_id = 1 AND fence_token = %s
                  AND NOT mutation_admission_open AND claim_admission_open
                  AND NOT integrity_quarantine
                """,
                (observed_at, token),
            )
        return self.status(now=observed_at)

    def register_verified_backup(
        self,
        *,
        verified: VerifiedBackup,
        fence_token: uuid.UUID | str,
        now: datetime | None = None,
    ) -> None:
        token = _uuid(fence_token, "fence_token")
        observed_at = now or utcnow()
        registered_build_digest = verified.build_digest if verified.git_sha else ""
        registered_bundle_digest = (
            verified.release_bundle_digest if verified.git_sha else ""
        )
        if verified.fence_token_hash != _fence_token_hash(token):
            raise BackupContractError("backup_manifest_fence_token_mismatch")
        with connection(self._dsn) as conn:
            control = conn.execute(
                """
                SELECT mutation_admission_open, claim_admission_open,
                       integrity_quarantine, generation, fence_token
                FROM research_ops.runtime_control
                WHERE singleton_id = 1
                FOR UPDATE
                """
            ).fetchone()
            if (
                control is None
                or bool(control[0])
                or bool(control[1])
                or bool(control[2])
                or control[4] != token
            ):
                raise BackupContractError("backup_fence_not_sealed")
            if int(control[3]) != verified.fence_generation:
                raise BackupContractError("backup_manifest_fence_generation_mismatch")
            conn.execute(
                """
                INSERT INTO research_ops.backup_set (
                    backup_id, manifest_hash, fence_token, fence_generation,
                    git_sha, release_id, build_digest, release_bundle_digest,
                    created_at, verified_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (backup_id) DO NOTHING
                """,
                (
                    verified.backup_id,
                    verified.manifest_hash,
                    token,
                    verified.fence_generation,
                    verified.git_sha,
                    verified.release_id,
                    registered_build_digest,
                    registered_bundle_digest,
                    verified.created_at,
                    observed_at,
                ),
            )
            row = conn.execute(
                """
                SELECT manifest_hash, fence_token, fence_generation,
                       git_sha, release_id, build_digest, release_bundle_digest
                FROM research_ops.backup_set
                WHERE backup_id = %s
                """,
                (verified.backup_id,),
            ).fetchone()
            expected = (
                verified.manifest_hash,
                token,
                verified.fence_generation,
                verified.git_sha,
                verified.release_id,
                registered_build_digest,
                registered_bundle_digest,
            )
            if row is None or tuple(row) != expected:
                raise BackupContractError("backup_registration_conflict")
            conn.execute(
                """
                UPDATE research_ops.runtime_control
                SET last_verified_manifest_hash = %s, changed_at = %s
                WHERE singleton_id = 1 AND fence_token = %s
                """,
                (verified.manifest_hash, observed_at, token),
            )

    def reopen(
        self,
        *,
        fence_token: uuid.UUID | str,
        manifest_hash: str,
        operator_id: str,
        now: datetime | None = None,
    ) -> FenceStatus:
        """Reopen only a sealed generation bound to a verified backup manifest."""

        token = _uuid(fence_token, "fence_token")
        normalized_hash = _sha256(manifest_hash, "manifest_hash")
        operator = _bounded_text(operator_id, "operator_id", maximum=255)
        observed_at = now or utcnow()
        with connection(self._dsn) as conn:
            control = conn.execute(
                """
                SELECT mutation_admission_open, claim_admission_open,
                       integrity_quarantine, generation, fence_token,
                       last_verified_manifest_hash
                FROM research_ops.runtime_control
                WHERE singleton_id = 1
                FOR UPDATE
                """
            ).fetchone()
            if control is None or control[4] != token:
                raise BackupContractError("backup_fence_token_mismatch")
            if bool(control[0]) or bool(control[1]) or bool(control[2]):
                raise BackupContractError("backup_fence_not_reopenable")
            if control[5] != normalized_hash:
                raise BackupContractError("backup_manifest_not_registered")
            backup = conn.execute(
                """
                SELECT 1
                FROM research_ops.backup_set
                WHERE manifest_hash = %s AND fence_token = %s
                  AND fence_generation = %s
                """,
                (normalized_hash, token, int(control[3])),
            ).fetchone()
            if backup is None:
                raise BackupContractError("backup_registration_missing")
            conn.execute(
                """
                UPDATE research_ops.runtime_control
                SET mutation_admission_open = true,
                    claim_admission_open = true,
                    fence_token = NULL,
                    requested_by = %s,
                    reason = 'verified_backup_reopen',
                    reopened_at = %s,
                    changed_at = %s
                WHERE singleton_id = 1 AND fence_token = %s
                """,
                (operator, observed_at, observed_at, token),
            )
        return self.status(now=observed_at)

    def quarantine(
        self,
        *,
        operator_id: str,
        reason: str,
        fence_token: uuid.UUID | str | None = None,
        now: datetime | None = None,
    ) -> FenceStatus:
        """Fail closed after integrity or backup verification failure."""

        operator = _bounded_text(operator_id, "operator_id", maximum=255)
        normalized_reason = _bounded_text(reason, "reason", maximum=255)
        observed_at = now or utcnow()
        expected_token = (
            _uuid(fence_token, "fence_token") if fence_token is not None else None
        )
        with connection(self._dsn) as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (MUTATION_FENCE_ADVISORY_LOCK_ID,),
            )
            control = conn.execute(
                """
                SELECT fence_token, generation
                FROM research_ops.runtime_control
                WHERE singleton_id = 1
                FOR UPDATE
                """
            ).fetchone()
            if control is None:
                raise BackupContractError("runtime_control_missing")
            current_token = control[0]
            if expected_token is not None and current_token != expected_token:
                raise BackupContractError("backup_fence_token_mismatch")
            token = current_token or uuid.uuid4()
            generation_increment = 1 if current_token is None else 0
            conn.execute(
                """
                UPDATE research_ops.runtime_control
                SET mutation_admission_open = false,
                    claim_admission_open = false,
                    integrity_quarantine = true,
                    generation = generation + %s,
                    fence_token = %s,
                    requested_by = %s,
                    reason = %s,
                    closed_at = COALESCE(closed_at, %s),
                    reopened_at = NULL,
                    changed_at = %s
                WHERE singleton_id = 1
                """,
                (
                    generation_increment,
                    token,
                    operator,
                    normalized_reason,
                    observed_at,
                    observed_at,
                ),
            )
        return self.status(now=observed_at)


def write_private_fence_receipt(*, status: FenceStatus, path: Path) -> None:
    """Write the capability token once with owner-only permissions."""

    if status.fence_token is None or status.phase not in {
        "DRAINING",
        "SEALED",
        "QUARANTINED",
    }:
        raise BackupContractError("private_fence_receipt_state_invalid")
    target = Path(path).expanduser()
    if not target.is_absolute() or _has_symlink_component(target.parent):
        raise BackupContractError("private_fence_receipt_path_invalid")
    parent = target.parent.resolve(strict=True)
    if not parent.is_dir() or target.exists():
        raise BackupContractError("private_fence_receipt_path_invalid")
    document = _private_fence_receipt_document(status)
    _write_new_durable(target, _canonical_json(document), mode=0o600)
    _fsync_directory(parent)


def write_private_fence_intent(
    *,
    fence_token: uuid.UUID | str,
    path: Path,
    created_at: datetime | None = None,
) -> None:
    """Durably persist the recovery capability before closing admission."""

    token = _uuid(fence_token, "fence_token")
    observed_at = created_at or utcnow()
    if observed_at.utcoffset() is None:
        raise ValueError("fence_created_at_timezone_required")
    target = Path(path).expanduser()
    if not target.is_absolute() or _has_symlink_component(target.parent):
        raise BackupContractError("private_fence_receipt_path_invalid")
    parent = target.parent.resolve(strict=True)
    if not parent.is_dir() or target.exists():
        raise BackupContractError("private_fence_receipt_path_invalid")
    document = {
        "schema_version": 1,
        "kind": "backup_fence_private_receipt",
        "phase": "DRAINING",
        "generation": 0,
        "fence_token": str(token),
        "fence_token_hash": _fence_token_hash(token),
        "created_at": iso_utc(observed_at),
    }
    _write_new_durable(target, _canonical_json(document), mode=0o600)
    _fsync_directory(parent)


def finalize_private_fence_receipt(*, status: FenceStatus, path: Path) -> None:
    """Atomically bind a prewritten recovery intent to the committed generation."""

    target = _absolute_regular_file(path, "private_fence_receipt")
    token, generation = read_private_fence_receipt(target)
    if status.fence_token != token or generation not in {0, status.generation}:
        raise BackupContractError("private_fence_receipt_binding_invalid")
    document = _private_fence_receipt_document(status)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_new_durable(temporary, _canonical_json(document), mode=0o600)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _private_fence_receipt_document(status: FenceStatus) -> dict[str, Any]:
    if status.fence_token is None or status.phase not in {
        "DRAINING",
        "SEALED",
        "QUARANTINED",
    }:
        raise BackupContractError("private_fence_receipt_state_invalid")
    return {
        "schema_version": 1,
        "kind": "backup_fence_private_receipt",
        "phase": status.phase,
        "generation": status.generation,
        "fence_token": str(status.fence_token),
        "fence_token_hash": _fence_token_hash(status.fence_token),
        "created_at": iso_utc(status.changed_at),
    }


def read_private_fence_receipt(path: Path) -> tuple[uuid.UUID, int]:
    target = _absolute_regular_file(path, "private_fence_receipt")
    if target.stat().st_mode & 0o077:
        raise BackupContractError("private_fence_receipt_permissions_invalid")
    try:
        payload = _read_bounded(target, 4_096)
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupContractError("private_fence_receipt_invalid") from exc
    if (
        not isinstance(document, dict)
        or payload != _canonical_json(document)
        or set(document)
        != {
            "schema_version",
            "kind",
            "phase",
            "generation",
            "fence_token",
            "fence_token_hash",
            "created_at",
        }
        or document.get("schema_version") != 1
        or document.get("kind") != "backup_fence_private_receipt"
        or document.get("phase") not in {"DRAINING", "SEALED", "QUARANTINED"}
    ):
        raise BackupContractError("private_fence_receipt_invalid")
    token = _uuid(document.get("fence_token"), "fence_token")
    generation = document.get("generation")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
        or document.get("fence_token_hash") != _fence_token_hash(token)
    ):
        raise BackupContractError("private_fence_receipt_invalid")
    _parse_utc(document.get("created_at"), "fence_created_at")
    return token, generation


def _load_fence_status(conn: Any, *, observed_at: datetime) -> FenceStatus:
    control = conn.execute(
        """
        SELECT mutation_admission_open, claim_admission_open,
               integrity_quarantine, generation, fence_token, changed_at
        FROM research_ops.runtime_control
        WHERE singleton_id = 1
        """
    ).fetchone()
    if control is None:
        raise BackupContractError("runtime_control_missing")
    jobs = conn.execute(
        """
        SELECT count(*)
        FROM public.portal_researchjob
        WHERE status IN ('RUNNING', 'CANCEL_REQUESTED')
        """
    ).fetchone()
    experiments = conn.execute(
        "SELECT count(*) FROM research_ops.active_experiment_claim"
    ).fetchone()
    outbox = conn.execute(
        """
        SELECT
            count(*) FILTER (WHERE status = 'PENDING'),
            count(*) FILTER (WHERE status = 'CLAIMED'),
            count(*) FILTER (WHERE status = 'DEAD_LETTER')
        FROM research_ops.outbox_delivery
        """
    ).fetchone()
    intents = conn.execute(
        """
        SELECT count(*), max(projected_at)
        FROM public.portal_webauditevent
        WHERE projected_at IS NULL
        """
    ).fetchone()
    latest_projection = conn.execute(
        "SELECT max(projected_at) FROM public.portal_webauditevent"
    ).fetchone()
    receipts = conn.execute(
        """
        SELECT count(*)
        FROM research_ops.research_job_result_receipt
        WHERE applied_at IS NULL
        """
    ).fetchone()
    audit = conn.execute(
        """
        SELECT status, reason_count, observed_at, row_count, terminal_hash
        FROM research_ops.validation_observation
        WHERE kind = %s
        """,
        (AUDIT_OBSERVATION_KIND,),
    ).fetchone()
    mutation_open, claim_open, quarantine = (
        bool(control[0]),
        bool(control[1]),
        bool(control[2]),
    )
    if quarantine:
        phase: Literal["OPEN", "DRAINING", "SEALED", "QUARANTINED"] = "QUARANTINED"
    elif mutation_open and claim_open:
        phase = "OPEN"
    elif not mutation_open and claim_open:
        phase = "DRAINING"
    elif not mutation_open and not claim_open:
        phase = "SEALED"
    else:
        raise BackupContractError("runtime_control_state_invalid")
    return FenceStatus(
        phase=phase,
        generation=int(control[3]),
        fence_token=control[4],
        changed_at=control[5],
        active_jobs=int(jobs[0]),
        active_experiment_claims=int(experiments[0]),
        pending_outbox=int(outbox[0]),
        claimed_outbox=int(outbox[1]),
        dead_letter_outbox=int(outbox[2]),
        unprojected_audit_intents=int(intents[0]),
        unapplied_job_receipts=int(receipts[0]),
        audit_validation_status=str(audit[0]) if audit is not None else "MISSING",
        audit_validation_reason_count=int(audit[1]) if audit is not None else 1,
        audit_validation_observed_at=audit[2] if audit is not None else None,
        latest_audit_projection_at=(
            latest_projection[0] if latest_projection is not None else None
        ),
        audit_row_count=int(audit[3]) if audit is not None else 0,
        audit_terminal_hash=str(audit[4]) if audit is not None else "",
    )


def _quiescence_reasons(
    status: FenceStatus,
    *,
    observed_at: datetime,
    audit_observation_max_age_seconds: int,
    include_audit: bool = True,
) -> list[str]:
    reasons: list[str] = []
    for field in (
        "active_jobs",
        "active_experiment_claims",
        "pending_outbox",
        "claimed_outbox",
        "dead_letter_outbox",
        "unprojected_audit_intents",
        "unapplied_job_receipts",
    ):
        if getattr(status, field) != 0:
            reasons.append(field)
    if not include_audit:
        return sorted(set(reasons))
    if status.audit_validation_status != "PASS" or status.audit_validation_reason_count:
        reasons.append("audit_validation_failed")
    if status.audit_validation_observed_at is None or (
        observed_at - status.audit_validation_observed_at
    ) > timedelta(seconds=audit_observation_max_age_seconds):
        reasons.append("audit_validation_stale")
    if status.latest_audit_projection_at is not None and (
        status.audit_validation_observed_at is None
        or status.audit_validation_observed_at < status.latest_audit_projection_at
    ):
        reasons.append("audit_validation_before_projection_watermark")
    if status.audit_row_count == 0:
        if status.audit_terminal_hash:
            reasons.append("audit_terminal_hash_invalid")
    elif not _HASH_RE.fullmatch(status.audit_terminal_hash):
        reasons.append("audit_terminal_hash_invalid")
    return sorted(set(reasons))


def create_signed_backup_manifest(
    *,
    backup_directory: Path,
    files: Mapping[str, str],
    signing_private_key: Path,
    verification_public_key: Path,
    backup_id: uuid.UUID | str,
    fence_token: uuid.UUID | str,
    fence_generation: int,
    git_sha: str,
    release_id: str,
    build_digest: str,
    release_bundle_digest: str,
    postgresql_major: int,
    audit_row_count: int,
    audit_terminal_hash: str,
    created_at: datetime | None = None,
    openssl_path: Path = Path("/usr/bin/openssl"),
) -> VerifiedBackup:
    """Create a canonical detached-signed manifest without embedding host paths."""

    root = _absolute_existing_directory(backup_directory, "backup_directory")
    private_key = _absolute_regular_file(signing_private_key, "signing_private_key")
    public_key = _absolute_regular_file(
        verification_public_key, "verification_public_key"
    )
    manifest_path = root / "manifest.json"
    signature_path = root / "manifest.sig"
    normalized_id = _uuid(backup_id, "backup_id")
    token = _uuid(fence_token, "fence_token")
    if isinstance(fence_generation, bool) or int(fence_generation) <= 0:
        raise ValueError("fence_generation_invalid")
    normalized_git_sha = _git_sha(git_sha, "git_sha")
    normalized_release = _release_id(release_id)
    normalized_build = _sha256(build_digest, "build_digest")
    normalized_bundle = _sha256(
        release_bundle_digest,
        "release_bundle_digest",
    )
    if not 12 <= int(postgresql_major) <= 30:
        raise ValueError("postgresql_major_invalid")
    if isinstance(audit_row_count, bool) or int(audit_row_count) < 0:
        raise ValueError("audit_row_count_invalid")
    normalized_terminal = _audit_terminal_hash(
        int(audit_row_count), audit_terminal_hash
    )
    records = _backup_file_records(root, files)
    roles = {str(record["role"]) for record in records}
    if not _REQUIRED_BACKUP_ROLES.issubset(roles):
        raise BackupContractError("backup_required_role_missing")
    timestamp = created_at or utcnow()
    if timestamp.utcoffset() is None:
        raise ValueError("backup_created_at_timezone_required")
    manifest: dict[str, Any] = {
        "schema_version": BACKUP_MANIFEST_SCHEMA_VERSION,
        "backup_id": str(normalized_id),
        "created_at": iso_utc(timestamp),
        "git_sha": normalized_git_sha,
        "release_id": normalized_release,
        "build_digest": normalized_build,
        "release_bundle_digest": normalized_bundle,
        # Schema 2 binds the complete Web + Operations migration release, not
        # only the SQL owned by Operations.  The official CLI additionally
        # proves that this exact state is applied to the source database
        # before it calls this publisher.
        "migration_digest": expected_platform_migration_digest(),
        "postgresql_major": int(postgresql_major),
        "fence": {
            "generation": int(fence_generation),
            "token_hash": _fence_token_hash(token),
        },
        "audit": {
            "status": "PASS",
            "row_count": int(audit_row_count),
            "terminal_hash": normalized_terminal or None,
            "segmented_stream_required": True,
        },
        "files": records,
    }
    payload = _canonical_json(manifest)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise BackupContractError("backup_manifest_too_large")
    published_payload = _publish_signed_document(
        payload=payload,
        document_path=manifest_path,
        signature_path=signature_path,
        private_key=private_key,
        public_key=public_key,
        openssl_path=openssl_path,
        maximum_bytes=MAX_MANIFEST_BYTES,
        equivalent=lambda existing, requested: _canonical_documents_equivalent(
            existing,
            requested,
            ignored_fields=frozenset({"created_at"}),
        ),
        conflict_code="backup_manifest_state_conflict",
    )
    _fsync_directory(root)
    verified = verify_backup_set(
        backup_directory=root,
        verification_public_key=public_key,
        expected_git_sha=normalized_git_sha,
        expected_release_id=normalized_release,
        expected_build_digest=normalized_build,
        expected_release_bundle_digest=normalized_bundle,
        expected_postgresql_major=int(postgresql_major),
        openssl_path=openssl_path,
    )
    if (
        verified.backup_id != normalized_id
        or verified.fence_generation != int(fence_generation)
        or verified.fence_token_hash != _fence_token_hash(token)
        or verified.migration_digest != expected_platform_migration_digest()
        or verified.audit_row_count != int(audit_row_count)
        or verified.audit_terminal_hash != normalized_terminal
        or hashlib.sha256(published_payload).hexdigest()
        != verified.manifest_hash.removeprefix("sha256:")
    ):
        raise BackupContractError("backup_manifest_state_conflict")
    return verified


def verify_live_backup_database_state(
    *, expected_postgresql_major: int, dsn: str | None = None
) -> str:
    """Prove the source DB matches the complete signed release schema.

    ``POSTGRES_MAJOR`` is an operator input used by pg_dump tooling, so it is
    not trusted as evidence.  This check reads the server and both migration
    authorities from the same database connection immediately before the
    manifest is signed.
    """

    if not 12 <= int(expected_postgresql_major) <= 30:
        raise ValueError("postgresql_major_invalid")
    try:
        with connection(dsn, connect_timeout=5) as conn:
            row = conn.execute(
                "SELECT current_setting('server_version_num')::integer"
            ).fetchone()
            operations_rows = conn.execute(
                """
                SELECT name, content_hash
                FROM research_ops.migration_history
                ORDER BY name
                """
            ).fetchall()
            portal_rows = conn.execute(
                """
                SELECT name
                FROM django_migrations
                WHERE app = 'portal'
                ORDER BY name
                """
            ).fetchall()
    except Exception as exc:
        raise BackupContractError("backup_database_state_unavailable") from exc
    if row is None:
        raise BackupContractError("backup_postgresql_version_unavailable")
    actual_major = int(row[0]) // 10_000
    if actual_major != int(expected_postgresql_major):
        raise BackupContractError("backup_postgresql_major_mismatch")
    if dict(operations_rows) != expected_migration_hashes():
        raise BackupContractError("backup_operations_migrations_mismatch")
    if tuple(str(item[0]) for item in portal_rows) != expected_portal_migrations():
        raise BackupContractError("backup_portal_migrations_mismatch")
    return expected_platform_migration_digest()


def verify_backup_set(
    *,
    backup_directory: Path,
    verification_public_key: Path,
    expected_git_sha: str | None = None,
    expected_release_id: str | None = None,
    expected_build_digest: str | None = None,
    expected_release_bundle_digest: str | None = None,
    expected_postgresql_major: int | None = None,
    openssl_path: Path = Path("/usr/bin/openssl"),
) -> VerifiedBackup:
    root = _absolute_existing_directory(backup_directory, "backup_directory")
    public_key = _absolute_regular_file(
        verification_public_key, "verification_public_key"
    )
    manifest_path = _regular_file_inside(root, "manifest.json")
    signature_path = _regular_file_inside(root, "manifest.sig")
    payload = _read_bounded(manifest_path, MAX_MANIFEST_BYTES)
    _verify_signature(
        manifest_path,
        signature_path,
        public_key=public_key,
        openssl_path=openssl_path,
    )
    try:
        manifest = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupContractError("backup_manifest_json_invalid") from exc
    if not isinstance(manifest, dict) or payload != _canonical_json(manifest):
        raise BackupContractError("backup_manifest_not_canonical")
    verified = _validate_and_hash_manifest(root, manifest, payload)
    if expected_git_sha is not None and verified.git_sha != _git_sha(
        expected_git_sha, "expected_git_sha"
    ):
        raise BackupContractError("backup_git_sha_mismatch")
    if expected_release_id is not None and verified.release_id != _release_id(
        expected_release_id
    ):
        raise BackupContractError("backup_release_mismatch")
    if expected_build_digest is not None and verified.build_digest != _sha256(
        expected_build_digest, "expected_build_digest"
    ):
        raise BackupContractError("backup_build_digest_mismatch")
    if (
        expected_release_bundle_digest is not None
        and verified.release_bundle_digest
        != _sha256(
            expected_release_bundle_digest,
            "expected_release_bundle_digest",
        )
    ):
        raise BackupContractError("backup_release_bundle_digest_mismatch")
    if expected_postgresql_major is not None and (
        verified.postgresql_major != int(expected_postgresql_major)
    ):
        raise BackupContractError("backup_postgresql_major_mismatch")
    return verified


def verify_restored_application_state(
    *,
    verified_backup: VerifiedBackup,
    restore_namespace: Path,
    maximum_records: int = 100_000,
    started_at: datetime | None = None,
) -> RecoveryVerification:
    """Read-only, path-redacted verification inside an isolated restore namespace."""

    if not 1 <= maximum_records <= 1_000_000:
        raise ValueError("maximum_records_invalid")
    started = started_at or utcnow()
    _assert_offline_restore_context(verified_backup, restore_namespace)
    checks: list[dict[str, Any]] = []

    checks.extend(_restored_database_checks(verified_backup))
    try:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "market_research_web.settings")
        import django

        django.setup()
        from django.conf import settings
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Group
        from django.core.exceptions import ValidationError
        from market_research.application.adapter_contracts import (
            content_hash_payload,
            governance_registry_path,
            load_governance_rows,
            sha256_prefixed,
            validate_experiment_identity_registry,
            validate_governance_registry,
            validate_research_decision_report,
        )
        from market_research_web.operations_contract import (
            GovernanceDecision,
            ImportedDecisionReport,
            ManifestUpload,
            ResearchJob,
            read_verified_manifest_bytes,
            resolve_artifact_ref,
            validate_managed_import_record,
            validate_web_audit_outbox,
            verify_result_artifact,
        )
    except Exception:
        checks.append(
            _recovery_check("runtime_imports", False, "runtime_import_failed")
        )
        return _finish_recovery(verified_backup, started, checks)

    checks.append(_recovery_check("runtime_imports", True, "runtime_imports_valid"))
    model_counts = {
        "users": get_user_model().objects.count(),
        "groups": Group.objects.count(),
        "manifests": ManifestUpload.objects.count(),
        "successful_jobs": ResearchJob.objects.filter(status="SUCCEEDED").count(),
        "imported_reports": ImportedDecisionReport.objects.count(),
        "approvals": GovernanceDecision.objects.filter(action="APPROVAL").count(),
    }
    total_records = sum(model_counts.values())
    within_bound = total_records <= maximum_records
    checks.append(
        _recovery_check(
            "authoritative_record_bound",
            within_bound,
            "authoritative_record_bound_valid"
            if within_bound
            else "authoritative_record_bound_exceeded",
            total_records,
        )
    )
    if not within_bound:
        return _finish_recovery(verified_backup, started, checks)

    invalid_users = get_user_model().objects.filter(username="").count()
    checks.append(
        _recovery_check(
            "authentication_state",
            invalid_users == 0,
            "authentication_state_verified"
            if invalid_users == 0
            else "authentication_state_invalid",
            invalid_users,
        )
    )

    manifest_failures = 0
    for manifest in ManifestUpload.objects.all().iterator(chunk_size=500):
        try:
            read_verified_manifest_bytes(manifest)
        except (OSError, TypeError, ValueError, ValidationError):
            manifest_failures += 1
    checks.append(
        _recovery_check(
            "web_manifests",
            manifest_failures == 0,
            "web_manifests_verified"
            if manifest_failures == 0
            else "web_manifest_verification_failed",
            manifest_failures,
        )
    )

    result_failures = 0
    report_failures = 0
    successful_jobs = ResearchJob.objects.filter(status="SUCCEEDED").iterator(
        chunk_size=250
    )
    for job in successful_jobs:
        try:
            summary = verify_result_artifact(
                job.result_ref, expected_hash=job.result_hash
            )
        except (OSError, TypeError, ValueError, ValidationError):
            result_failures += 1
            continue
        if str(job.capability_id) != "research-validate":
            continue
        try:
            experiment_id = str(summary["experiment_id"])
            expected_report_hash = str(summary["research_candidate_report_hash"])
            candidate_path = settings.RESEARCH_PATHS.report_path(
                "research", experiment_id, "research_candidate_report.json"
            )
            candidate = _read_bounded_json_inside(
                settings.RESEARCH_PATHS.report_root,
                candidate_path,
                maximum=64 * 1024 * 1024,
            )
            reasons = validate_research_decision_report(candidate)
            if reasons or candidate.get("content_hash") != expected_report_hash:
                raise ValueError("candidate_report_binding_invalid")
        except (KeyError, OSError, TypeError, ValueError, ValidationError):
            report_failures += 1
    checks.append(
        _recovery_check(
            "job_result_artifacts",
            result_failures == 0,
            "job_result_artifacts_verified"
            if result_failures == 0
            else "job_result_artifact_verification_failed",
            result_failures,
        )
    )
    checks.append(
        _recovery_check(
            "canonical_reports",
            report_failures == 0,
            "canonical_reports_verified"
            if report_failures == 0
            else "canonical_report_verification_failed",
            report_failures,
        )
    )

    imported_report_failures = 0
    for record in ImportedDecisionReport.objects.all().iterator(chunk_size=250):
        try:
            report_path = resolve_artifact_ref(record.storage_ref)
            report = _read_bounded_json_inside(
                settings.RESEARCH_PATHS.report_root,
                report_path,
                maximum=64 * 1024 * 1024,
            )
            validate_managed_import_record(record, report)
        except (OSError, TypeError, ValueError, ValidationError):
            imported_report_failures += 1
    checks.append(
        _recovery_check(
            "imported_reports",
            imported_report_failures == 0,
            "imported_reports_verified"
            if imported_report_failures == 0
            else "imported_report_verification_failed",
            imported_report_failures,
        )
    )

    try:
        audit = validate_web_audit_outbox()
        audit_ok = (
            audit.get("status") == "PASS"
            and int(audit.get("pending_event_count") or 0) == 0
            and int(audit.get("duplicate_projection_count") or 0) == 0
            and int(audit.get("orphan_projection_count") or 0) == 0
            and int(audit.get("unmarked_projection_count") or 0) == 0
            and int(audit.get("row_count") or 0) == verified_backup.audit_row_count
            and str(audit.get("stream_hash") or "")
            == verified_backup.audit_terminal_hash
        )
        audit_reason_count = len(audit.get("reasons") or ())
    except Exception:
        audit_ok = False
        audit_reason_count = 1
    checks.append(
        _recovery_check(
            "audit_outbox",
            audit_ok,
            "audit_outbox_verified" if audit_ok else "audit_outbox_verification_failed",
            audit_reason_count,
        )
    )

    segment_root = Path(str(settings.INTERNAL_WEB_AUDIT_PATH) + ".segments")
    segment_ok = _segmented_audit_layout_valid(
        segment_root, required=verified_backup.audit_row_count > 0
    )
    checks.append(
        _recovery_check(
            "audit_segments",
            segment_ok,
            "audit_segments_verified"
            if segment_ok
            else "audit_segments_missing_or_invalid",
        )
    )

    try:
        governance = validate_governance_registry(settings.RESEARCH_PATHS)
        governance_ok = governance.get("status") == "PASS"
        governance_reason_count = len(governance.get("reasons") or ())
        governance_rows = load_governance_rows(
            governance_registry_path(settings.RESEARCH_PATHS)
        )
        governance_row_hashes = {
            str(row.get("row_hash") or "") for row in governance_rows
        }
    except Exception:
        governance_ok = False
        governance_reason_count = 1
        governance_row_hashes = set()
    checks.append(
        _recovery_check(
            "governance_registry",
            governance_ok,
            "governance_registry_verified"
            if governance_ok
            else "governance_registry_verification_failed",
            governance_reason_count,
        )
    )

    try:
        identity = validate_experiment_identity_registry(
            manager=settings.RESEARCH_PATHS
        )
        identity_ok = identity.get("status") == "PASS"
        identity_reason_count = len(identity.get("reasons") or ())
    except Exception:
        identity_ok = False
        identity_reason_count = 1
    checks.append(
        _recovery_check(
            "experiment_identity_registry",
            identity_ok,
            "experiment_identity_registry_verified"
            if identity_ok
            else "experiment_identity_registry_verification_failed",
            identity_reason_count,
        )
    )

    approval_failures = 0
    report_root = settings.RESEARCH_PATHS.report_root
    for decision in GovernanceDecision.objects.filter(action="APPROVAL").iterator(
        chunk_size=250
    ):
        try:
            approval = _read_bounded_json_inside(
                report_root,
                Path(str(decision.approval_artifact_ref)),
                maximum=4 * 1024 * 1024,
            )
            material = {
                key: value for key, value in approval.items() if key != "content_hash"
            }
            actual_hash = sha256_prefixed(content_hash_payload(material))
            if (
                approval.get("content_hash") != actual_hash
                or actual_hash != decision.content_hash
                or str(decision.review_row_hash) not in governance_row_hashes
                or str(decision.transition_row_hash) not in governance_row_hashes
            ):
                raise ValueError("approval_binding_invalid")
        except (OSError, TypeError, ValueError, ValidationError):
            approval_failures += 1
    checks.append(
        _recovery_check(
            "approval_artifacts",
            approval_failures == 0,
            "approval_artifacts_verified"
            if approval_failures == 0
            else "approval_artifact_verification_failed",
            approval_failures,
        )
    )
    return _finish_recovery(verified_backup, started, checks)


def create_signed_recovery_receipt(
    *,
    verification: RecoveryVerification,
    receipt_path: Path,
    signing_private_key: Path,
    verification_public_key: Path,
    openssl_path: Path = Path("/usr/bin/openssl"),
) -> tuple[str, Path]:
    """Persist one new signed aggregate; never overwrite a previous drill receipt."""

    path = Path(receipt_path).expanduser()
    if not path.is_absolute() or _has_symlink_component(path.parent):
        raise BackupContractError("recovery_receipt_path_invalid")
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir():
        raise BackupContractError("recovery_receipt_path_invalid")
    signature_path = path.with_suffix(path.suffix + ".sig")
    payload = _canonical_json(verification.document())
    if len(payload) > MAX_RECEIPT_BYTES:
        raise BackupContractError("recovery_receipt_too_large")
    published_payload = _publish_signed_document(
        payload=payload,
        document_path=path,
        signature_path=signature_path,
        private_key=_absolute_regular_file(signing_private_key, "signing_private_key"),
        public_key=_absolute_regular_file(
            verification_public_key,
            "verification_public_key",
        ),
        openssl_path=openssl_path,
        maximum_bytes=MAX_RECEIPT_BYTES,
        equivalent=lambda existing, requested: _canonical_documents_equivalent(
            existing,
            requested,
            ignored_fields=frozenset({"started_at", "finished_at", "duration_seconds"}),
        ),
        conflict_code="recovery_receipt_state_conflict",
    )
    receipt_hash, _recovered, _document = verify_signed_recovery_receipt(
        verification=verification,
        receipt_path=path,
        verification_public_key=verification_public_key,
        openssl_path=openssl_path,
    )
    _fsync_directory(parent)
    if receipt_hash != "sha256:" + hashlib.sha256(published_payload).hexdigest():
        raise BackupContractError("recovery_receipt_state_conflict")
    return receipt_hash, signature_path


def verify_signed_recovery_receipt(
    *,
    verification: RecoveryVerification | None,
    receipt_path: Path,
    verification_public_key: Path,
    openssl_path: Path = Path("/usr/bin/openssl"),
) -> tuple[str, RecoveryVerification, dict[str, Any]]:
    """Verify and reuse an exact signed result after response/control loss."""

    path = _absolute_regular_file(receipt_path, "recovery_receipt")
    signature = _absolute_regular_file(
        path.with_suffix(path.suffix + ".sig"),
        "recovery_receipt_signature",
    )
    public_key = _absolute_regular_file(
        verification_public_key,
        "verification_public_key",
    )
    _verify_signature(
        path,
        signature,
        public_key=public_key,
        openssl_path=openssl_path,
    )
    payload = _read_bounded(path, MAX_RECEIPT_BYTES)
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupContractError("recovery_receipt_json_invalid") from exc
    schema_version = (
        document.get("schema_version") if isinstance(document, dict) else None
    )
    expected_fields = {
        "schema_version",
        "status",
        "backup_manifest_hash",
        "started_at",
        "finished_at",
        "duration_seconds",
        "checks",
    }
    if schema_version == 2:
        expected_fields.add("release")
    if (
        not isinstance(document, dict)
        or set(document) != expected_fields
        or payload != _canonical_json(document)
        or schema_version not in {1, 2}
        or document.get("status") not in {"PASS", "FAIL"}
        or not isinstance(document.get("checks"), list)
    ):
        raise BackupContractError("recovery_receipt_contract_invalid")
    started_at = _parse_utc(document["started_at"], "recovery_started_at")
    finished_at = _parse_utc(document["finished_at"], "recovery_finished_at")
    raw_duration = document["duration_seconds"]
    if (
        isinstance(raw_duration, bool)
        or not isinstance(raw_duration, (int, float))
        or float(raw_duration) < 0
        or abs(
            float(raw_duration) - max(0.0, (finished_at - started_at).total_seconds())
        )
        > 1.000001
    ):
        raise BackupContractError("recovery_receipt_duration_invalid")
    release = document.get("release")
    if schema_version == 2:
        if not isinstance(release, dict) or set(release) != {
            "git_sha",
            "release_id",
            "build_digest",
            "release_bundle_digest",
        }:
            raise BackupContractError("recovery_receipt_release_invalid")
        try:
            recovered_git_sha = _git_sha(
                release.get("git_sha"), "recovery_receipt_git_sha"
            )
            recovered_release_id = _release_id(release.get("release_id"))
            recovered_build_digest = _sha256(
                release.get("build_digest"), "recovery_receipt_build_digest"
            )
            recovered_bundle_digest = _sha256(
                release.get("release_bundle_digest"),
                "recovery_receipt_release_bundle_digest",
            )
        except ValueError as exc:
            raise BackupContractError("recovery_receipt_release_invalid") from exc
    else:
        recovered_git_sha = ""
        recovered_release_id = ""
        recovered_build_digest = ""
        recovered_bundle_digest = ""
    recovered = RecoveryVerification(
        status=document["status"],
        backup_manifest_hash=_sha256(
            document["backup_manifest_hash"],
            "recovery_receipt_backup_manifest_hash",
        ),
        started_at=started_at,
        finished_at=finished_at,
        checks=tuple(document["checks"]),
        git_sha=recovered_git_sha,
        release_id=recovered_release_id,
        build_digest=recovered_build_digest,
        release_bundle_digest=recovered_bundle_digest,
    )
    if verification is not None and (
        recovered.backup_manifest_hash != verification.backup_manifest_hash
        or recovered.status != verification.status
        or recovered.checks != verification.checks
        or recovered.git_sha != verification.git_sha
        or recovered.release_id != verification.release_id
        or recovered.build_digest != verification.build_digest
        or recovered.release_bundle_digest != verification.release_bundle_digest
    ):
        raise BackupContractError("recovery_receipt_state_conflict")
    return (
        "sha256:" + hashlib.sha256(payload).hexdigest(),
        recovered,
        document,
    )


def recovery_activation_state(
    verified_backup: VerifiedBackup,
    *,
    dsn: str | None = None,
) -> Literal["SEALED", "OPEN"]:
    """Classify only the two states valid for activation or exact retry."""

    with connection(dsn) as conn:
        control = conn.execute(
            """
            SELECT mutation_admission_open, claim_admission_open,
                   integrity_quarantine, generation, fence_token,
                   last_verified_manifest_hash
            FROM research_ops.runtime_control
            WHERE singleton_id = 1
            """
        ).fetchone()
    if control is None or bool(control[2]):
        raise BackupContractError("recovery_activation_control_invalid")
    if bool(control[0]) and bool(control[1]) and control[4] is None:
        if str(control[5]) != verified_backup.manifest_hash:
            raise BackupContractError("recovery_activation_manifest_conflict")
        return "OPEN"
    if (
        not bool(control[0])
        and not bool(control[1])
        and control[4] is not None
        and int(control[3]) == verified_backup.fence_generation
        and _fence_token_hash(control[4]) == verified_backup.fence_token_hash
    ):
        return "SEALED"
    raise BackupContractError("recovery_activation_state_invalid")


def record_restore_drill(
    *,
    control_dsn: str,
    verification: RecoveryVerification,
    receipt_hash: str,
    drill_id: uuid.UUID | str | None = None,
) -> uuid.UUID:
    """Record the signed receipt in the control DB, never in the restored target."""

    normalized_receipt_hash = _sha256(receipt_hash, "receipt_hash")
    identifier = _uuid(
        drill_id
        or uuid.uuid5(
            uuid.NAMESPACE_URL,
            "research-operations-restore-drill-v1:"
            f"{verification.backup_manifest_hash}:{normalized_receipt_hash}",
        ),
        "drill_id",
    )
    # The isolated target is forced read-only through PGOPTIONS. Override that
    # inherited session default only for the separately authenticated control
    # database used to register this signed evidence.
    with connection(control_dsn, session_read_only=False) as conn:
        conn.execute(
            """
            INSERT INTO research_ops.restore_drill (
                drill_id, backup_manifest_hash, receipt_hash, status,
                duration_seconds, finished_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (drill_id) DO NOTHING
            """,
            (
                identifier,
                verification.backup_manifest_hash,
                normalized_receipt_hash,
                verification.status,
                verification.duration_seconds,
                verification.finished_at,
            ),
        )
        row = conn.execute(
            """
            SELECT backup_manifest_hash, receipt_hash, status
            FROM research_ops.restore_drill
            WHERE drill_id = %s
            """,
            (identifier,),
        ).fetchone()
        if row is None or tuple(row) != (
            verification.backup_manifest_hash,
            normalized_receipt_hash,
            verification.status,
        ):
            raise BackupContractError("restore_drill_registration_conflict")
    return identifier


def activate_verified_recovery(
    *,
    verified_backup: VerifiedBackup,
    verification: RecoveryVerification,
    receipt_hash: str,
    operator_id: str,
    dsn: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Promote one signed PASS restore from sealed/read-only to serviceable."""

    operator = _bounded_text(operator_id, "operator_id", maximum=255)
    normalized_receipt = _sha256(receipt_hash, "recovery_receipt_hash")
    if (
        verification.status != "PASS"
        or verification.backup_manifest_hash != verified_backup.manifest_hash
        or verification.git_sha != verified_backup.git_sha
        or verification.release_id
        != (verified_backup.release_id if verified_backup.git_sha else "")
        or verification.build_digest
        != (verified_backup.build_digest if verified_backup.git_sha else "")
        or verification.release_bundle_digest
        != (verified_backup.release_bundle_digest if verified_backup.git_sha else "")
    ):
        raise BackupContractError("recovery_activation_verification_invalid")
    observed_at = now or utcnow()

    # Database-level read-only is lifted first. A crash here remains safe:
    # runtime admission is still SEALED and every mutation/claim takes its gate.
    with connection(
        dsn,
        autocommit=True,
        session_read_only=False,
    ) as conn:
        database_name = str(conn.execute("SELECT current_database()").fetchone()[0])
        conn.execute(
            sql.SQL("ALTER DATABASE {} SET default_transaction_read_only = off").format(
                sql.Identifier(database_name)
            )
        )

    with connection(dsn, session_read_only=False) as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (MUTATION_FENCE_ADVISORY_LOCK_ID,),
        )
        control = conn.execute(
            """
            SELECT mutation_admission_open, claim_admission_open,
                   integrity_quarantine, generation, fence_token,
                   last_verified_manifest_hash
            FROM research_ops.runtime_control
            WHERE singleton_id = 1
            FOR UPDATE
            """
        ).fetchone()
        if control is None or bool(control[2]):
            raise BackupContractError("recovery_activation_control_invalid")
        if bool(control[0]) and bool(control[1]) and control[4] is None:
            if str(control[5]) != verified_backup.manifest_hash:
                raise BackupContractError("recovery_activation_manifest_conflict")
            return {
                "schema_version": 1,
                "status": "OPEN",
                "already_activated": True,
                "generation": int(control[3]),
                "backup_manifest_hash": verified_backup.manifest_hash,
                "receipt_hash": normalized_receipt,
            }
        if (
            bool(control[0])
            or bool(control[1])
            or control[4] is None
            or int(control[3]) != verified_backup.fence_generation
            or _fence_token_hash(control[4]) != verified_backup.fence_token_hash
        ):
            raise BackupContractError("recovery_activation_not_sealed")
        counts = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM public.portal_researchjob
               WHERE status IN ('RUNNING', 'CANCEL_REQUESTED')),
              (SELECT count(*) FROM research_ops.active_experiment_claim),
              (SELECT count(*) FROM research_ops.outbox_delivery
               WHERE status IN ('PENDING', 'CLAIMED', 'DEAD_LETTER')),
              (SELECT count(*) FROM public.portal_webauditevent
               WHERE projected_at IS NULL),
              (SELECT count(*) FROM research_ops.research_job_result_receipt
               WHERE applied_at IS NULL)
            """
        ).fetchone()
        if counts is None or any(int(value) != 0 for value in counts):
            raise BackupContractError("recovery_activation_writer_state_invalid")
        conn.execute(
            """
            UPDATE research_ops.runtime_control
            SET mutation_admission_open = true,
                claim_admission_open = true,
                generation = generation + 1,
                fence_token = NULL,
                requested_by = %s,
                reason = 'signed_recovery_activation',
                closed_at = NULL,
                reopened_at = %s,
                changed_at = %s,
                last_verified_manifest_hash = %s
            WHERE singleton_id = 1
            """,
            (
                operator,
                observed_at,
                observed_at,
                verified_backup.manifest_hash,
            ),
        )
        generation = int(control[3]) + 1
    return {
        "schema_version": 1,
        "status": "OPEN",
        "already_activated": False,
        "generation": generation,
        "backup_manifest_hash": verified_backup.manifest_hash,
        "receipt_hash": normalized_receipt,
    }


def _restored_database_checks(verified: VerifiedBackup) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    try:
        with connection() as conn:
            identity = conn.execute(
                """
                SELECT current_database(), pg_is_in_recovery(),
                       current_setting('transaction_read_only'),
                       current_setting('server_version_num')::integer
                """
            ).fetchone()
            control = conn.execute(
                """
                SELECT mutation_admission_open, claim_admission_open,
                       integrity_quarantine
                FROM research_ops.runtime_control
                WHERE singleton_id = 1
                """
            ).fetchone()
            migrations = dict(
                conn.execute(
                    """
                    SELECT name, content_hash
                    FROM research_ops.migration_history
                    ORDER BY name
                    """
                ).fetchall()
            )
            portal_migrations = tuple(
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT name
                    FROM django_migrations
                    WHERE app = 'portal'
                    ORDER BY name
                    """
                ).fetchall()
            )
            writers = conn.execute(
                """
                SELECT
                    (SELECT count(*) FROM public.portal_researchjob
                     WHERE status IN ('RUNNING', 'CANCEL_REQUESTED')),
                    (SELECT count(*) FROM research_ops.active_experiment_claim),
                    (SELECT count(*) FROM research_ops.outbox_delivery
                     WHERE status IN ('PENDING', 'CLAIMED', 'DEAD_LETTER')),
                    (SELECT count(*) FROM public.portal_webauditevent
                     WHERE projected_at IS NULL),
                    (SELECT count(*)
                     FROM research_ops.research_job_result_receipt
                     WHERE applied_at IS NULL),
                    (SELECT count(*)
                     FROM research_ops.research_job_result_receipt AS receipt
                     LEFT JOIN public.portal_researchjob AS job
                       ON job.id = receipt.job_id
                     WHERE receipt.applied_at IS NULL
                        OR job.id IS NULL
                        OR job.status <> 'SUCCEEDED'
                        OR job.request_hash <> receipt.request_hash
                        OR job.result_ref <> receipt.result_ref
                        OR job.result_hash <> receipt.result_hash
                        OR job.research_outcome <> receipt.research_outcome)
                """
            ).fetchone()
    except Exception:
        return [
            _recovery_check("restored_database", False, "restored_database_unavailable")
        ]
    expected_database = os.environ.get("RESEARCH_OPS_RECOVERY_DATABASE_NAME", "")
    database_ok = (
        bool(expected_database)
        and identity is not None
        and str(identity[0]) == expected_database
        and not bool(identity[1])
        and str(identity[2]).lower() == "on"
    )
    checks.append(
        _recovery_check(
            "restored_database",
            database_ok,
            "restored_database_isolated"
            if database_ok
            else "restored_database_identity_invalid",
        )
    )
    fence_ok = (
        control is not None
        and not bool(control[0])
        and not bool(control[1])
        and not bool(control[2])
    )
    checks.append(
        _recovery_check(
            "restored_admission",
            fence_ok,
            "restored_admission_closed"
            if fence_ok
            else "restored_admission_not_closed",
        )
    )
    if verified.git_sha:
        migration_ok = (
            migrations == expected_migration_hashes()
            and portal_migrations == expected_portal_migrations()
            and verified.migration_digest == expected_platform_migration_digest()
        )
        migration_count = len(migrations) + len(portal_migrations)
    else:
        actual_migration_digest = _migration_digest_from_rows(migrations)
        migration_ok = (
            actual_migration_digest == verified.migration_digest
            and verified.migration_digest == expected_migration_digest()
        )
        migration_count = len(migrations)
    checks.append(
        _recovery_check(
            "restored_migrations",
            migration_ok,
            "restored_migrations_match"
            if migration_ok
            else "restored_migrations_mismatch",
            migration_count,
        )
    )
    server_major = int(identity[3]) // 10_000 if identity is not None else 0
    version_ok = server_major == verified.postgresql_major
    checks.append(
        _recovery_check(
            "restored_postgresql_major",
            version_ok,
            "restored_postgresql_major_matches"
            if version_ok
            else "restored_postgresql_major_mismatch",
            server_major,
        )
    )
    active_count = sum(int(value) for value in writers[:5])
    checks.append(
        _recovery_check(
            "restored_writer_state",
            active_count == 0,
            "restored_writer_state_quiescent"
            if active_count == 0
            else "restored_writer_state_active",
            active_count,
        )
    )
    receipt_mismatch_count = int(writers[5])
    checks.append(
        _recovery_check(
            "research_job_receipts",
            receipt_mismatch_count == 0,
            "research_job_receipts_verified"
            if receipt_mismatch_count == 0
            else "research_job_receipt_binding_failed",
            receipt_mismatch_count,
        )
    )
    return checks


def _assert_offline_restore_context(
    verified: VerifiedBackup, restore_namespace: Path
) -> None:
    if os.environ.get("RESEARCH_OPS_RECOVERY_MODE") != "offline":
        raise BackupContractError("offline_recovery_mode_required")
    if os.environ.get("RESEARCH_OPS_MUTATION_DISABLED") != "true":
        raise BackupContractError("offline_mutation_disable_required")
    namespace = _absolute_existing_directory(restore_namespace, "restore_namespace")
    marker = _regular_file_inside(namespace, ".research-ops-isolated-restore-v1")
    try:
        payload = json.loads(_read_bounded(marker, 4_096))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupContractError("restore_namespace_marker_invalid") from exc
    if payload != {
        "schema_version": 1,
        "purpose": "isolated-recovery-rehearsal",
        "backup_manifest_hash": verified.manifest_hash,
    }:
        raise BackupContractError("restore_namespace_marker_invalid")
    if os.environ.get("RESEARCH_OPS_RELEASE_ID") != verified.release_id:
        raise BackupContractError("recovery_release_mismatch")
    if os.environ.get("RESEARCH_OPS_BUILD_DIGEST") != verified.build_digest:
        raise BackupContractError("recovery_build_mismatch")
    if verified.release_bundle_digest and (
        os.environ.get("RESEARCH_OPS_RELEASE_BUNDLE_DIGEST")
        != verified.release_bundle_digest
    ):
        raise BackupContractError("recovery_release_bundle_mismatch")
    if verified.git_sha and os.environ.get("RESEARCH_OPS_GIT_SHA") != verified.git_sha:
        raise BackupContractError("recovery_git_sha_mismatch")


def _segmented_audit_layout_valid(path: Path, *, required: bool) -> bool:
    if not path.exists():
        return not required
    if not path.is_absolute() or _has_symlink_component(path) or not path.is_dir():
        return False
    checkpoint = path / "checkpoint.json"
    segments = path / "segments"
    metadata = path / "metadata"
    receipts = path / "receipts"
    return (
        checkpoint.is_file()
        and not _has_symlink_component(checkpoint)
        and segments.is_dir()
        and metadata.is_dir()
        and receipts.is_dir()
        and not any(
            _has_symlink_component(candidate)
            for candidate in (segments, metadata, receipts)
        )
    )


def _read_bounded_json_inside(
    root: Path, candidate: Path, *, maximum: int
) -> dict[str, Any]:
    root_resolved = Path(root).resolve(strict=True)
    path = Path(candidate)
    if not path.is_absolute() or _has_symlink_component(path):
        raise ValueError("recovery_artifact_path_invalid")
    resolved = path.resolve(strict=True)
    if not _is_within(resolved, root_resolved) or not resolved.is_file():
        raise ValueError("recovery_artifact_path_invalid")
    try:
        value = json.loads(_read_bounded(resolved, maximum))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("recovery_artifact_json_invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("recovery_artifact_json_invalid")
    return value


def _migration_digest_from_rows(rows: Mapping[str, str]) -> str:
    material = b"".join(
        f"{name}\0{digest}\n".encode() for name, digest in sorted(rows.items())
    )
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _recovery_check(
    check_id: str, passed: bool, reason_code: str, count: int | None = None
) -> dict[str, Any]:
    if not re.fullmatch(r"^[a-z][a-z0-9_]{0,63}$", check_id):
        raise ValueError("recovery_check_id_invalid")
    if not re.fullmatch(r"^[a-z][a-z0-9_]{0,127}$", reason_code):
        raise ValueError("recovery_reason_code_invalid")
    value: dict[str, Any] = {
        "id": check_id,
        "status": "PASS" if passed else "FAIL",
        "reason_code": reason_code,
    }
    if count is not None:
        value["count"] = max(0, int(count))
    return value


def _finish_recovery(
    verified: VerifiedBackup,
    started_at: datetime,
    checks: Sequence[dict[str, Any]],
) -> RecoveryVerification:
    normalized = tuple(sorted(checks, key=lambda item: str(item["id"])))
    status: Literal["PASS", "FAIL"] = (
        "PASS"
        if normalized and all(item.get("status") == "PASS" for item in normalized)
        else "FAIL"
    )
    return RecoveryVerification(
        status=status,
        backup_manifest_hash=verified.manifest_hash,
        started_at=started_at,
        finished_at=utcnow(),
        checks=normalized,
        git_sha=verified.git_sha,
        release_id=verified.release_id if verified.git_sha else "",
        build_digest=verified.build_digest if verified.git_sha else "",
        release_bundle_digest=(
            verified.release_bundle_digest if verified.git_sha else ""
        ),
    )


def _validate_and_hash_manifest(
    root: Path, manifest: Mapping[str, Any], payload: bytes
) -> VerifiedBackup:
    schema_version = manifest.get("schema_version")
    expected_fields = {
        "schema_version",
        "backup_id",
        "created_at",
        "release_id",
        "build_digest",
        "migration_digest",
        "postgresql_major",
        "fence",
        "audit",
        "files",
    }
    if schema_version == 2:
        expected_fields.update({"git_sha", "release_bundle_digest"})
    if set(manifest) != expected_fields or schema_version not in {1, 2}:
        raise BackupContractError("backup_manifest_schema_invalid")
    backup_id = _uuid(manifest.get("backup_id"), "backup_id")
    created_at = _parse_utc(manifest.get("created_at"), "created_at")
    git_sha = (
        _git_sha(manifest.get("git_sha"), "git_sha") if schema_version == 2 else ""
    )
    release_id = _release_id(manifest.get("release_id"))
    build_digest = _sha256(manifest.get("build_digest"), "build_digest")
    release_bundle_digest = (
        _sha256(
            manifest.get("release_bundle_digest"),
            "release_bundle_digest",
        )
        if schema_version == 2
        else ""
    )
    migration_digest = _sha256(manifest.get("migration_digest"), "migration_digest")
    postgresql_major = manifest.get("postgresql_major")
    if isinstance(postgresql_major, bool) or not isinstance(postgresql_major, int):
        raise BackupContractError("backup_postgresql_major_invalid")
    if not 12 <= postgresql_major <= 30:
        raise BackupContractError("backup_postgresql_major_invalid")
    fence = manifest.get("fence")
    if not isinstance(fence, dict) or set(fence) != {"generation", "token_hash"}:
        raise BackupContractError("backup_fence_binding_invalid")
    generation = fence.get("generation")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
    ):
        raise BackupContractError("backup_fence_binding_invalid")
    token_hash = _sha256(fence.get("token_hash"), "fence_token_hash")
    audit = manifest.get("audit")
    if not isinstance(audit, dict) or set(audit) != {
        "status",
        "row_count",
        "terminal_hash",
        "segmented_stream_required",
    }:
        raise BackupContractError("backup_audit_binding_invalid")
    row_count = audit.get("row_count")
    if (
        audit.get("status") != "PASS"
        or audit.get("segmented_stream_required") is not True
        or isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count < 0
    ):
        raise BackupContractError("backup_audit_binding_invalid")
    terminal_hash = _audit_terminal_hash(row_count, audit.get("terminal_hash") or "")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not 1 <= len(raw_files) <= MAX_BACKUP_FILES:
        raise BackupContractError("backup_file_list_invalid")
    normalized_files: list[dict[str, Any]] = []
    roles: set[str] = set()
    paths: set[str] = set()
    for raw_record in raw_files:
        record = _validate_file_record(root, raw_record)
        role = str(record["role"])
        relative_path = str(record["relative_path"])
        if role in roles or relative_path in paths:
            raise BackupContractError("backup_file_binding_duplicate")
        roles.add(role)
        paths.add(relative_path)
        normalized_files.append(record)
    if normalized_files != sorted(
        normalized_files, key=lambda value: (value["role"], value["relative_path"])
    ):
        raise BackupContractError("backup_file_list_not_sorted")
    if not _REQUIRED_BACKUP_ROLES.issubset(roles):
        raise BackupContractError("backup_required_role_missing")
    return VerifiedBackup(
        backup_id=backup_id,
        manifest_hash="sha256:" + hashlib.sha256(payload).hexdigest(),
        git_sha=git_sha,
        release_id=release_id,
        build_digest=build_digest,
        release_bundle_digest=release_bundle_digest,
        migration_digest=migration_digest,
        postgresql_major=postgresql_major,
        fence_generation=generation,
        fence_token_hash=token_hash,
        created_at=created_at,
        audit_row_count=row_count,
        audit_terminal_hash=terminal_hash,
        files=tuple(normalized_files),
    )


def _backup_file_records(root: Path, files: Mapping[str, str]) -> list[dict[str, Any]]:
    if not 1 <= len(files) <= MAX_BACKUP_FILES:
        raise BackupContractError("backup_file_list_invalid")
    records: list[dict[str, Any]] = []
    for role, relative_path in files.items():
        normalized_role = str(role)
        if not _ROLE_RE.fullmatch(normalized_role):
            raise ValueError("backup_file_role_invalid")
        normalized_path = _safe_relative_path(relative_path)
        path = _regular_file_inside(root, normalized_path)
        digest, size = _hash_file(path)
        records.append(
            {
                "role": normalized_role,
                "relative_path": normalized_path,
                "size_bytes": size,
                "sha256": digest,
                "snapshot_id": digest,
            }
        )
    return sorted(records, key=lambda value: (value["role"], value["relative_path"]))


def _validate_file_record(root: Path, value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "role",
        "relative_path",
        "size_bytes",
        "sha256",
        "snapshot_id",
    }:
        raise BackupContractError("backup_file_record_invalid")
    role = str(value.get("role") or "")
    if not _ROLE_RE.fullmatch(role):
        raise BackupContractError("backup_file_role_invalid")
    relative_path = _safe_relative_path(value.get("relative_path"))
    size_bytes = value.get("size_bytes")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
    ):
        raise BackupContractError("backup_file_size_invalid")
    expected_hash = _sha256(value.get("sha256"), "backup_file_hash")
    snapshot_id = _sha256(value.get("snapshot_id"), "backup_snapshot_id")
    if snapshot_id != expected_hash:
        raise BackupContractError("backup_snapshot_binding_invalid")
    path = _regular_file_inside(root, relative_path)
    actual_hash, actual_size = _hash_file(path)
    if actual_hash != expected_hash or actual_size != size_bytes:
        raise BackupContractError("backup_file_checksum_mismatch")
    return {
        "role": role,
        "relative_path": relative_path,
        "size_bytes": size_bytes,
        "sha256": expected_hash,
        "snapshot_id": snapshot_id,
    }


def _canonical_documents_equivalent(
    existing: bytes,
    requested: bytes,
    *,
    ignored_fields: frozenset[str],
) -> bool:
    """Compare canonical documents while allowing retry-only timestamps to differ."""

    try:
        existing_document = json.loads(existing)
        requested_document = json.loads(requested)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if (
        not isinstance(existing_document, dict)
        or not isinstance(requested_document, dict)
        or set(existing_document) != set(requested_document)
        or existing != _canonical_json(existing_document)
        or requested != _canonical_json(requested_document)
    ):
        return False
    try:
        for field in ignored_fields & {"created_at", "started_at", "finished_at"}:
            _parse_utc(existing_document[field], f"resume_{field}")
        if "duration_seconds" in ignored_fields:
            duration = existing_document["duration_seconds"]
            started = _parse_utc(existing_document["started_at"], "resume_started_at")
            finished = _parse_utc(
                existing_document["finished_at"], "resume_finished_at"
            )
            if (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or duration < 0
                or abs(float(duration) - max(0.0, (finished - started).total_seconds()))
                > 1.000001
            ):
                return False
    except (BackupContractError, TypeError, ValueError):
        return False
    return {
        key: value
        for key, value in existing_document.items()
        if key not in ignored_fields
    } == {
        key: value
        for key, value in requested_document.items()
        if key not in ignored_fields
    }


def _publish_signed_document(
    *,
    payload: bytes,
    document_path: Path,
    signature_path: Path,
    private_key: Path,
    public_key: Path,
    openssl_path: Path,
    maximum_bytes: int,
    equivalent: Callable[[bytes, bytes], bool],
    conflict_code: str,
) -> bytes:
    """Verify a temporary pair before publication and resume an exact partial pair.

    The document is the commit marker for a logical pair.  A signer or verifier
    failure happens while both outputs still have unique temporary names.  A
    process loss after publishing the document but before publishing its
    signature leaves a deterministic, resumable partial state; a retry signs
    that exact canonical document only when its non-temporal fields still match.
    """

    parent = document_path.parent
    if signature_path.parent != parent:
        raise BackupContractError(conflict_code)
    token = uuid.uuid4().hex
    temporary_document = parent / f".{document_path.name}.{token}.payload.tmp"
    temporary_signature = parent / f".{signature_path.name}.{token}.signature.tmp"
    try:
        document_exists = document_path.exists() or document_path.is_symlink()
        signature_exists = signature_path.exists() or signature_path.is_symlink()
        if signature_exists and not document_exists:
            raise BackupContractError(conflict_code)
        if document_exists:
            existing_path = _absolute_regular_file(
                document_path,
                "signed_document_partial",
            )
            existing_payload = _read_bounded(existing_path, maximum_bytes)
            if not equivalent(existing_payload, payload):
                raise BackupContractError(conflict_code)
            if signature_exists:
                existing_signature = _absolute_regular_file(
                    signature_path,
                    "signed_document_signature",
                )
                _verify_signature(
                    existing_path,
                    existing_signature,
                    public_key=public_key,
                    openssl_path=openssl_path,
                )
                return existing_payload

            _sign_file(
                existing_path,
                temporary_signature,
                private_key=private_key,
                openssl_path=openssl_path,
            )
            _verify_signature(
                existing_path,
                temporary_signature,
                public_key=public_key,
                openssl_path=openssl_path,
            )
            _publish_new_file(temporary_signature, signature_path)
            _fsync_directory(parent)
            return existing_payload

        _write_new_durable(temporary_document, payload, mode=0o640)
        _sign_file(
            temporary_document,
            temporary_signature,
            private_key=private_key,
            openssl_path=openssl_path,
        )
        _verify_signature(
            temporary_document,
            temporary_signature,
            public_key=public_key,
            openssl_path=openssl_path,
        )
        _publish_new_file(temporary_document, document_path)
        _fsync_directory(parent)
        try:
            _publish_new_file(temporary_signature, signature_path)
        except OSError as exc:
            # The already durable document is an intentional recovery marker.
            # A retry will validate its canonical state and publish a newly
            # verified signature; no caller may replace or reinterpret it.
            raise BackupContractError("signed_document_publish_interrupted") from exc
        _fsync_directory(parent)
        return payload
    except BackupContractError:
        raise
    except OSError as exc:
        raise BackupContractError("signed_document_publish_failed") from exc
    finally:
        for temporary in (temporary_signature, temporary_document):
            with suppress(OSError):
                temporary.unlink()


def _publish_new_file(temporary: Path, destination: Path) -> None:
    """Publish without replacement, then remove only our unique temporary link."""

    os.link(temporary, destination, follow_symlinks=False)
    temporary.unlink()


def _sign_file(
    source: Path,
    destination: Path,
    *,
    private_key: Path,
    openssl_path: Path,
) -> None:
    executable = _absolute_regular_file(openssl_path, "openssl_path")
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        try:
            completed = subprocess.run(
                [
                    str(executable),
                    "dgst",
                    "-sha256",
                    "-sign",
                    str(private_key),
                    "-out",
                    str(temporary),
                    str(source),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BackupContractError("backup_manifest_signing_failed") from exc
        if completed.returncode != 0:
            raise BackupContractError("backup_manifest_signing_failed")
        _absolute_regular_file(temporary, "signature_temporary")
        os.chmod(temporary, 0o640)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        _publish_new_file(temporary, destination)
    finally:
        with suppress(OSError):
            temporary.unlink()


def _verify_signature(
    manifest: Path,
    signature: Path,
    *,
    public_key: Path,
    openssl_path: Path,
) -> None:
    executable = _absolute_regular_file(openssl_path, "openssl_path")
    try:
        completed = subprocess.run(
            [
                str(executable),
                "dgst",
                "-sha256",
                "-verify",
                str(public_key),
                "-signature",
                str(signature),
                str(manifest),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupContractError("backup_manifest_signature_invalid") from exc
    if completed.returncode != 0:
        raise BackupContractError("backup_manifest_signature_invalid")


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode()


def _write_new_durable(path: Path, payload: bytes, *, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_bounded(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            payload = handle.read(maximum + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > maximum:
        raise BackupContractError("bounded_document_too_large")
    return payload


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            while block := handle.read(1024 * 1024):
                digest.update(block)
                size += len(block)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return "sha256:" + digest.hexdigest(), size


def _absolute_existing_directory(value: Path, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or _has_symlink_component(path):
        raise BackupContractError(f"{field}_invalid")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise BackupContractError(f"{field}_invalid")
    return resolved


def _absolute_regular_file(value: Path, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or _has_symlink_component(path):
        raise BackupContractError(f"{field}_invalid")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise BackupContractError(f"{field}_invalid")
    return resolved


def _regular_file_inside(root: Path, relative_path: str) -> Path:
    normalized = _safe_relative_path(relative_path)
    candidate = root.joinpath(*PurePosixPath(normalized).parts)
    if _has_symlink_component(candidate):
        raise BackupContractError("backup_file_symlink_rejected")
    resolved = candidate.resolve(strict=True)
    if not _is_within(resolved, root) or not resolved.is_file():
        raise BackupContractError("backup_file_invalid")
    return resolved


def _safe_relative_path(value: object) -> str:
    raw = str(value or "")
    candidate = PurePosixPath(raw)
    if (
        not raw
        or raw in {".", ".."}
        or not candidate.parts
        or raw.startswith("/")
        or "\\" in raw
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or candidate.as_posix() != raw
    ):
        raise BackupContractError("backup_relative_path_invalid")
    return raw


def _has_symlink_component(path: Path) -> bool:
    if not path.is_absolute():
        return True
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parse_utc(value: object, field: str) -> datetime:
    raw = str(value or "")
    if not raw.endswith("Z"):
        raise BackupContractError(f"backup_{field}_invalid")
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError as exc:
        raise BackupContractError(f"backup_{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise BackupContractError(f"backup_{field}_invalid")
    if iso_utc(parsed) != raw:
        raise BackupContractError(f"backup_{field}_invalid")
    return parsed


def _audit_terminal_hash(row_count: int, value: object) -> str:
    raw = str(value or "")
    if row_count == 0:
        if raw:
            raise BackupContractError("backup_audit_terminal_hash_invalid")
        return ""
    if not _HASH_RE.fullmatch(raw):
        raise BackupContractError("backup_audit_terminal_hash_invalid")
    return raw


def _fence_token_hash(value: uuid.UUID) -> str:
    material = b"research-operations-fence-token-v1\0" + value.bytes
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _sha256(value: object, field: str) -> str:
    normalized = str(value or "")
    if not _HASH_RE.fullmatch(normalized):
        raise ValueError(f"{field}_invalid")
    return normalized


def _git_sha(value: object, field: str) -> str:
    normalized = str(value or "")
    if not _GIT_SHA_RE.fullmatch(normalized):
        raise ValueError(f"{field}_invalid")
    return normalized


def _release_id(value: object) -> str:
    normalized = str(value or "")
    if not _RELEASE_RE.fullmatch(normalized):
        raise ValueError("release_id_invalid")
    return normalized


def _bounded_text(value: object, field: str, *, maximum: int) -> str:
    normalized = " ".join(str(value or "").split())
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise ValueError(f"{field}_invalid")
    return normalized


def _uuid(value: object, field: str) -> uuid.UUID:
    try:
        normalized = uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field}_invalid") from exc
    if str(normalized) != str(value):
        raise ValueError(f"{field}_invalid")
    return normalized


__all__ = [
    "BACKUP_MANIFEST_SCHEMA_VERSION",
    "MUTATION_FENCE_ADVISORY_LOCK_ID",
    "BackupContractError",
    "BackupFenceStore",
    "FenceStatus",
    "RecoveryVerification",
    "VerifiedBackup",
    "create_signed_backup_manifest",
    "create_signed_recovery_receipt",
    "finalize_private_fence_receipt",
    "read_private_fence_receipt",
    "record_restore_drill",
    "verify_backup_set",
    "verify_restored_application_state",
    "write_private_fence_intent",
    "write_private_fence_receipt",
]
