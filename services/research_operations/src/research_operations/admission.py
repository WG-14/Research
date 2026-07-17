"""PostgreSQL authority for active experiment namespaces and fenced results."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .database import assert_mutation_admission_open, connection
from .errors import (
    ActiveExperimentConflict,
    AdmissionClaimLost,
    ExperimentIdentityConflict,
    ExperimentRequestConflict,
)

ACTIVE = "ACTIVE"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
EXPIRED = "EXPIRED"
RELEASED = "RELEASED"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    authority: str
    experiment_id: str
    manifest_hash: str
    request_id: str
    request_hash: str
    owner_id: str
    run_id: uuid.UUID
    status: str
    acquired: bool
    lease_token: uuid.UUID | None = None
    fencing_token: int | None = None
    lease_expires_at: datetime | None = None
    result_ref: str = ""
    result_hash: str = ""
    error_code: str = ""

    @property
    def is_active(self) -> bool:
        return self.status == ACTIVE

    @property
    def is_reused_result(self) -> bool:
        return self.status == SUCCEEDED and not self.acquired


@dataclass(frozen=True, slots=True)
class ResearchJobResultReceipt:
    job_id: uuid.UUID
    authority: str
    experiment_id: str
    request_id: str
    request_hash: str
    admission_run_id: uuid.UUID
    fencing_token: int
    result_ref: str
    result_hash: str
    research_outcome: str
    core_run_id: str
    created_at: datetime
    applied_at: datetime | None


class ExperimentAdmissionStore:
    """Serialize one active run per `(authority, experiment_id)` namespace."""

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn

    def acquire(
        self,
        *,
        authority: str,
        experiment_id: str,
        manifest_hash: str,
        request_id: str,
        request_hash: str,
        owner_id: str,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> AdmissionDecision:
        namespace = _namespace(
            authority=authority,
            experiment_id=experiment_id,
            manifest_hash=manifest_hash,
            request_id=request_id,
            request_hash=request_hash,
            owner_id=owner_id,
        )
        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 3600:
            raise ValueError("admission_lease_seconds_invalid")
        observed_at = now or datetime.now(UTC)
        with connection(self._dsn) as conn:
            assert_mutation_admission_open(conn)
            conn.execute(
                """
                INSERT INTO research_ops.experiment_identity (
                    authority, experiment_id, manifest_hash
                ) VALUES (%s, %s, %s)
                ON CONFLICT (authority, experiment_id) DO NOTHING
                """,
                (
                    namespace["authority"],
                    namespace["experiment_id"],
                    namespace["manifest_hash"],
                ),
            )
            identity = conn.execute(
                """
                SELECT manifest_hash, fencing_counter
                FROM research_ops.experiment_identity
                WHERE authority = %s AND experiment_id = %s
                FOR UPDATE
                """,
                (namespace["authority"], namespace["experiment_id"]),
            ).fetchone()
            if identity is None:
                raise RuntimeError("experiment_identity_insert_failed")
            if identity[0] != namespace["manifest_hash"]:
                raise ExperimentIdentityConflict("experiment_manifest_hash_conflict")

            request = conn.execute(
                """
                SELECT request_hash, owner_id, run_id, status,
                       result_ref, result_hash, error_code
                FROM research_ops.experiment_request
                WHERE authority = %s AND experiment_id = %s AND request_id = %s
                FOR UPDATE
                """,
                (
                    namespace["authority"],
                    namespace["experiment_id"],
                    namespace["request_id"],
                ),
            ).fetchone()
            if request is not None:
                if (
                    request[0] != namespace["request_hash"]
                    or request[1] != namespace["owner_id"]
                ):
                    raise ExperimentRequestConflict(
                        "experiment_request_id_binding_conflict"
                    )
                if request[3] != ACTIVE:
                    return _terminal_decision(namespace, request)

            active = conn.execute(
                """
                SELECT request_id, request_hash, owner_id, run_id,
                       lease_token, fencing_token, lease_expires_at
                FROM research_ops.active_experiment_claim
                WHERE authority = %s AND experiment_id = %s
                FOR UPDATE
                """,
                (namespace["authority"], namespace["experiment_id"]),
            ).fetchone()
            if active is not None and active[6] > observed_at:
                if (
                    active[0] == namespace["request_id"]
                    and active[1] == namespace["request_hash"]
                    and active[2] == namespace["owner_id"]
                ):
                    # Exact retries converge on the active request, but never
                    # inherit the current process's bearer lease capability.
                    return AdmissionDecision(
                        **namespace,
                        run_id=active[3],
                        status=ACTIVE,
                        acquired=False,
                        lease_expires_at=active[6],
                    )
                raise ActiveExperimentConflict("experiment_namespace_already_active")

            prior_run_id: uuid.UUID | None = None
            if active is not None:
                if active[0] == namespace["request_id"]:
                    prior_run_id = active[3]
                else:
                    conn.execute(
                        """
                        UPDATE research_ops.experiment_request
                        SET status = 'EXPIRED', finished_at = %s, updated_at = %s,
                            error_code = 'LEASE_EXPIRED'
                        WHERE authority = %s AND experiment_id = %s
                          AND request_id = %s AND status = 'ACTIVE'
                        """,
                        (
                            observed_at,
                            observed_at,
                            namespace["authority"],
                            namespace["experiment_id"],
                            active[0],
                        ),
                    )
                conn.execute(
                    """
                    DELETE FROM research_ops.active_experiment_claim
                    WHERE authority = %s AND experiment_id = %s
                    """,
                    (namespace["authority"], namespace["experiment_id"]),
                )

            run_id = prior_run_id or (
                request[2] if request is not None else uuid.uuid4()
            )
            if request is None:
                conn.execute(
                    """
                    INSERT INTO research_ops.experiment_request (
                        authority, experiment_id, request_id, request_hash,
                        owner_id, run_id, status, created_at, started_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'ACTIVE', %s, %s, %s)
                    """,
                    (
                        namespace["authority"],
                        namespace["experiment_id"],
                        namespace["request_id"],
                        namespace["request_hash"],
                        namespace["owner_id"],
                        run_id,
                        observed_at,
                        observed_at,
                        observed_at,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE research_ops.experiment_request
                    SET status = 'ACTIVE', finished_at = NULL, error_code = '',
                        updated_at = %s
                    WHERE authority = %s AND experiment_id = %s AND request_id = %s
                    """,
                    (
                        observed_at,
                        namespace["authority"],
                        namespace["experiment_id"],
                        namespace["request_id"],
                    ),
                )

            fencing_token = conn.execute(
                """
                UPDATE research_ops.experiment_identity
                SET fencing_counter = fencing_counter + 1, updated_at = %s
                WHERE authority = %s AND experiment_id = %s
                RETURNING fencing_counter
                """,
                (observed_at, namespace["authority"], namespace["experiment_id"]),
            ).fetchone()[0]
            lease_token = uuid.uuid4()
            lease_expires_at = observed_at + timedelta(seconds=lease_seconds)
            conn.execute(
                """
                INSERT INTO research_ops.active_experiment_claim (
                    authority, experiment_id, request_id, request_hash,
                    owner_id, run_id, lease_token, fencing_token,
                    lease_expires_at, heartbeat_at, started_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    namespace["authority"],
                    namespace["experiment_id"],
                    namespace["request_id"],
                    namespace["request_hash"],
                    namespace["owner_id"],
                    run_id,
                    lease_token,
                    fencing_token,
                    lease_expires_at,
                    observed_at,
                    observed_at,
                    observed_at,
                ),
            )
        return AdmissionDecision(
            **namespace,
            run_id=run_id,
            status=ACTIVE,
            acquired=True,
            lease_token=lease_token,
            fencing_token=int(fencing_token),
            lease_expires_at=lease_expires_at,
        )

    def heartbeat(
        self,
        decision: AdmissionDecision,
        *,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> AdmissionDecision:
        _active_decision(decision)
        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 3600:
            raise ValueError("admission_lease_seconds_invalid")
        observed_at = now or datetime.now(UTC)
        expires_at = observed_at + timedelta(seconds=lease_seconds)
        with connection(self._dsn) as conn:
            row = conn.execute(
                """
                UPDATE research_ops.active_experiment_claim
                SET lease_expires_at = %s, heartbeat_at = %s, updated_at = %s
                WHERE authority = %s AND experiment_id = %s
                  AND request_id = %s AND lease_token = %s
                  AND fencing_token = %s AND lease_expires_at > %s
                RETURNING run_id
                """,
                (
                    expires_at,
                    observed_at,
                    observed_at,
                    decision.authority,
                    decision.experiment_id,
                    decision.request_id,
                    decision.lease_token,
                    decision.fencing_token,
                    observed_at,
                ),
            ).fetchone()
        if row is None:
            raise AdmissionClaimLost("experiment_admission_claim_lost")
        return AdmissionDecision(
            **{
                field: getattr(decision, field)
                for field in (
                    "authority",
                    "experiment_id",
                    "manifest_hash",
                    "request_id",
                    "request_hash",
                    "owner_id",
                    "run_id",
                    "status",
                    "acquired",
                    "lease_token",
                    "fencing_token",
                    "result_ref",
                    "result_hash",
                    "error_code",
                )
            },
            lease_expires_at=expires_at,
        )

    def complete(
        self,
        decision: AdmissionDecision,
        *,
        result_ref: str,
        result_hash: str,
        now: datetime | None = None,
    ) -> AdmissionDecision:
        normalized_ref = _text(result_ref, "result_ref", maximum=1024)
        normalized_hash = _hash(result_hash, "result_hash")
        return self._finish(
            decision,
            status=SUCCEEDED,
            result_ref=normalized_ref,
            result_hash=normalized_hash,
            error_code="",
            now=now,
        )

    def complete_research_job(
        self,
        decision: AdmissionDecision,
        *,
        job_id: uuid.UUID | str,
        result_ref: str,
        result_hash: str,
        research_outcome: str,
        core_run_id: str = "",
        now: datetime | None = None,
    ) -> AdmissionDecision:
        normalized_job_id = _uuid(job_id, "job_id")
        normalized_ref = _text(result_ref, "result_ref", maximum=1024)
        normalized_hash = _hash(result_hash, "result_hash")
        outcome = _text(research_outcome, "research_outcome", maximum=16).upper()
        if outcome not in {"PASS", "FAIL"}:
            raise ValueError("research_outcome_invalid")
        normalized_core_run = str(core_run_id or "").strip()
        if len(normalized_core_run) > 128 or "\x00" in normalized_core_run:
            raise ValueError("core_run_id_invalid")
        return self._finish(
            decision,
            status=SUCCEEDED,
            result_ref=normalized_ref,
            result_hash=normalized_hash,
            error_code="",
            now=now,
            job_receipt={
                "job_id": normalized_job_id,
                "research_outcome": outcome,
                "core_run_id": normalized_core_run,
            },
        )

    def fail(
        self,
        decision: AdmissionDecision,
        *,
        error_code: str,
        now: datetime | None = None,
    ) -> AdmissionDecision:
        code = _text(error_code, "error_code", maximum=128).upper()
        if not code.replace("_", "").isalnum():
            raise ValueError("error_code_invalid")
        return self._finish(
            decision,
            status=FAILED,
            result_ref="",
            result_hash="",
            error_code=code,
            now=now,
        )

    def release(
        self,
        decision: AdmissionDecision,
        *,
        now: datetime | None = None,
    ) -> AdmissionDecision:
        return self._finish(
            decision,
            status=RELEASED,
            result_ref="",
            result_hash="",
            error_code="RELEASED_BY_OWNER",
            now=now,
        )

    def _finish(
        self,
        decision: AdmissionDecision,
        *,
        status: str,
        result_ref: str,
        result_hash: str,
        error_code: str,
        now: datetime | None,
        job_receipt: dict[str, object] | None = None,
    ) -> AdmissionDecision:
        _active_decision(decision)
        observed_at = now or datetime.now(UTC)
        with connection(self._dsn) as conn:
            identity = conn.execute(
                """
                SELECT manifest_hash
                FROM research_ops.experiment_identity
                WHERE authority = %s AND experiment_id = %s
                FOR UPDATE
                """,
                (decision.authority, decision.experiment_id),
            ).fetchone()
            if identity is None or identity[0] != decision.manifest_hash:
                raise AdmissionClaimLost("experiment_admission_claim_lost")
            active = conn.execute(
                """
                SELECT request_id, request_hash, owner_id, run_id,
                       lease_token, fencing_token, lease_expires_at
                FROM research_ops.active_experiment_claim
                WHERE authority = %s AND experiment_id = %s
                FOR UPDATE
                """,
                (decision.authority, decision.experiment_id),
            ).fetchone()
            expected = (
                decision.request_id,
                decision.request_hash,
                decision.owner_id,
                decision.run_id,
                decision.lease_token,
                decision.fencing_token,
            )
            if (
                active is None
                or tuple(active[:6]) != expected
                or active[6] <= observed_at
            ):
                raise AdmissionClaimLost("experiment_admission_claim_lost")
            updated = conn.execute(
                """
                UPDATE research_ops.experiment_request
                SET status = %s, result_ref = %s, result_hash = %s,
                    error_code = %s, finished_at = %s, updated_at = %s
                WHERE authority = %s AND experiment_id = %s
                  AND request_id = %s AND request_hash = %s
                  AND owner_id = %s AND run_id = %s AND status = 'ACTIVE'
                RETURNING run_id
                """,
                (
                    status,
                    result_ref,
                    result_hash,
                    error_code,
                    observed_at,
                    observed_at,
                    decision.authority,
                    decision.experiment_id,
                    decision.request_id,
                    decision.request_hash,
                    decision.owner_id,
                    decision.run_id,
                ),
            ).fetchone()
            if updated is None:
                raise AdmissionClaimLost("experiment_admission_claim_lost")
            if job_receipt is not None:
                conn.execute(
                    """
                    INSERT INTO research_ops.research_job_result_receipt (
                        job_id, authority, experiment_id, request_id,
                        request_hash, admission_run_id, fencing_token,
                        result_ref, result_hash, research_outcome,
                        core_run_id, created_at
                    ) VALUES (
                        %(job_id)s, %(authority)s, %(experiment_id)s,
                        %(request_id)s, %(request_hash)s,
                        %(admission_run_id)s, %(fencing_token)s,
                        %(result_ref)s, %(result_hash)s,
                        %(research_outcome)s, %(core_run_id)s, %(created_at)s
                    )
                    """,
                    {
                        **job_receipt,
                        "authority": decision.authority,
                        "experiment_id": decision.experiment_id,
                        "request_id": decision.request_id,
                        "request_hash": decision.request_hash,
                        "admission_run_id": decision.run_id,
                        "fencing_token": decision.fencing_token,
                        "result_ref": result_ref,
                        "result_hash": result_hash,
                        "created_at": observed_at,
                    },
                )
            conn.execute(
                """
                DELETE FROM research_ops.active_experiment_claim
                WHERE authority = %s AND experiment_id = %s
                  AND request_id = %s AND lease_token = %s AND fencing_token = %s
                """,
                (
                    decision.authority,
                    decision.experiment_id,
                    decision.request_id,
                    decision.lease_token,
                    decision.fencing_token,
                ),
            )
        return AdmissionDecision(
            authority=decision.authority,
            experiment_id=decision.experiment_id,
            manifest_hash=decision.manifest_hash,
            request_id=decision.request_id,
            request_hash=decision.request_hash,
            owner_id=decision.owner_id,
            run_id=decision.run_id,
            status=status,
            acquired=False,
            result_ref=result_ref,
            result_hash=result_hash,
            error_code=error_code,
        )

    def research_job_receipt(
        self,
        job_id: uuid.UUID | str,
    ) -> ResearchJobResultReceipt | None:
        normalized_id = _uuid(job_id, "job_id")
        with connection(self._dsn) as conn:
            row = conn.execute(
                """
                SELECT job_id, authority, experiment_id, request_id,
                       request_hash, admission_run_id, fencing_token,
                       result_ref, result_hash, research_outcome,
                       core_run_id, created_at, applied_at
                FROM research_ops.research_job_result_receipt
                WHERE job_id = %s
                """,
                (normalized_id,),
            ).fetchone()
        if row is None:
            return None
        return ResearchJobResultReceipt(
            job_id=row[0],
            authority=row[1],
            experiment_id=row[2],
            request_id=row[3],
            request_hash=row[4],
            admission_run_id=row[5],
            fencing_token=int(row[6]),
            result_ref=row[7],
            result_hash=row[8],
            research_outcome=row[9],
            core_run_id=row[10],
            created_at=row[11],
            applied_at=row[12],
        )

    def mark_research_job_receipt_applied(
        self,
        *,
        job_id: uuid.UUID | str,
        result_hash: str,
        now: datetime | None = None,
    ) -> None:
        normalized_id = _uuid(job_id, "job_id")
        normalized_hash = _hash(result_hash, "result_hash")
        observed_at = now or datetime.now(UTC)
        with connection(self._dsn) as conn:
            row = conn.execute(
                """
                UPDATE research_ops.research_job_result_receipt
                SET applied_at = COALESCE(applied_at, %s)
                WHERE job_id = %s AND result_hash = %s
                RETURNING job_id
                """,
                (observed_at, normalized_id, normalized_hash),
            ).fetchone()
        if row is None:
            raise AdmissionClaimLost("research_job_receipt_binding_invalid")

    def status(
        self,
        *,
        authority: str,
        experiment_id: str,
        request_id: str,
    ) -> AdmissionDecision | None:
        normalized_authority = _text(authority, "authority", maximum=128)
        normalized_experiment = _text(experiment_id, "experiment_id", maximum=255)
        normalized_request = _text(request_id, "request_id", maximum=255)
        with connection(self._dsn) as conn:
            row = conn.execute(
                """
                SELECT identity.manifest_hash, request.request_hash,
                       request.owner_id, request.run_id, request.status,
                       request.result_ref, request.result_hash, request.error_code,
                       claim.lease_expires_at
                FROM research_ops.experiment_request AS request
                JOIN research_ops.experiment_identity AS identity
                  USING (authority, experiment_id)
                LEFT JOIN research_ops.active_experiment_claim AS claim
                  ON claim.authority = request.authority
                 AND claim.experiment_id = request.experiment_id
                 AND claim.request_id = request.request_id
                WHERE request.authority = %s AND request.experiment_id = %s
                  AND request.request_id = %s
                """,
                (normalized_authority, normalized_experiment, normalized_request),
            ).fetchone()
        if row is None:
            return None
        return AdmissionDecision(
            authority=normalized_authority,
            experiment_id=normalized_experiment,
            manifest_hash=row[0],
            request_id=normalized_request,
            request_hash=row[1],
            owner_id=row[2],
            run_id=row[3],
            status=row[4],
            acquired=False,
            result_ref=row[5],
            result_hash=row[6],
            error_code=row[7],
            lease_expires_at=row[8],
        )


def _namespace(**values: str) -> dict[str, str]:
    return {
        "authority": _text(values["authority"], "authority", maximum=128),
        "experiment_id": _text(values["experiment_id"], "experiment_id", maximum=255),
        "manifest_hash": _hash(values["manifest_hash"], "manifest_hash"),
        "request_id": _text(values["request_id"], "request_id", maximum=255),
        "request_hash": _hash(values["request_hash"], "request_hash"),
        "owner_id": _text(values["owner_id"], "owner_id", maximum=255),
    }


def _terminal_decision(
    namespace: dict[str, str], request: tuple[object, ...]
) -> AdmissionDecision:
    return AdmissionDecision(
        **namespace,
        run_id=request[2],
        status=str(request[3]),
        acquired=False,
        result_ref=str(request[4]),
        result_hash=str(request[5]),
        error_code=str(request[6]),
    )


def _active_decision(decision: AdmissionDecision) -> None:
    if (
        not decision.is_active
        or not decision.acquired
        or decision.lease_token is None
        or decision.fencing_token is None
    ):
        raise AdmissionClaimLost("experiment_admission_active_claim_required")


def _text(value: object, field: str, *, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise ValueError(f"{field}_invalid")
    return normalized


def _hash(value: str, field: str) -> str:
    normalized = str(value or "")
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError(f"{field}_invalid")
    return normalized


def _uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field}_invalid") from exc


__all__ = [
    "ACTIVE",
    "EXPIRED",
    "FAILED",
    "RELEASED",
    "SUCCEEDED",
    "AdmissionDecision",
    "ExperimentAdmissionStore",
    "ResearchJobResultReceipt",
]
