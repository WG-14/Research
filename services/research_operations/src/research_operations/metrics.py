"""Low-cardinality Prometheus metrics with no payload or topology labels."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from .database import connection
from .health import AUDIT_OBSERVATION_KIND, HealthPolicy, expected_migration_hashes

_METRIC_HELP = {
    "research_ops_active_experiment_claims": "Current experiment namespace claims.",
    "research_ops_backup_age_seconds": "Age of the latest verified backup set.",
    "research_ops_backup_present": "Whether a verified backup set exists.",
    "research_ops_claim_admission_open": "Whether new worker claims are admitted.",
    "research_ops_database_primary": "Whether PostgreSQL is a writable primary.",
    "research_ops_database_up": "Whether the bounded database snapshot succeeded.",
    "research_ops_experiment_expired_claims": (
        "Expired experiment claims requiring review."
    ),
    "research_ops_integrity_quarantine": "Whether integrity quarantine is active.",
    "research_ops_job_cancel_requested": (
        "Research jobs waiting for cancellation completion."
    ),
    "research_ops_job_expired_leases": "Research jobs with expired execution leases.",
    "research_ops_job_queued": "Queued research jobs.",
    "research_ops_job_running": "Running research jobs.",
    "research_ops_job_succeeded_total": "Terminal successful research jobs.",
    "research_ops_job_failed_total": "Terminal failed research jobs.",
    "research_ops_job_cancelled_total": "Terminal cancelled research jobs.",
    "research_ops_job_timeout_total": "Research jobs failed by execution timeout.",
    "research_ops_job_resource_exhausted_total": (
        "Research jobs failed by a memory or process resource limit."
    ),
    "research_ops_job_contract_violation_total": (
        "Research jobs rejected by capability or result contracts."
    ),
    "research_ops_job_retry_total": "Research job attempts beyond the first claim.",
    "research_ops_job_average_runtime_seconds": (
        "Average terminal research job execution duration."
    ),
    "research_ops_job_average_queue_seconds": (
        "Average research job queue wait before execution."
    ),
    "research_ops_job_receipts_unapplied": (
        "Durable research-job receipts not yet applied to terminal job state."
    ),
    "research_ops_migration_leaves_match": (
        "Whether applied operations migrations match this release."
    ),
    "research_ops_mutation_admission_open": (
        "Whether workflow mutation traffic is admitted."
    ),
    "research_ops_outbox_claimed": "Claimed audit delivery records.",
    "research_ops_outbox_dead_letter": "Dead-letter audit delivery records.",
    "research_ops_outbox_oldest_age_seconds": (
        "Age of the oldest undelivered audit event."
    ),
    "research_ops_outbox_pending": "Pending audit delivery records.",
    "research_ops_restore_drill_age_seconds": (
        "Age of the latest isolated restore drill."
    ),
    "research_ops_restore_drill_last_pass": "Whether the latest restore drill passed.",
    "research_ops_restore_drill_present": "Whether a restore drill result exists.",
    "research_ops_service_alert_delivery_failed": (
        "Terminal failed service-health alert deliveries."
    ),
    "research_ops_service_alert_delivery_pending": (
        "Pending or claimed service-health alert deliveries."
    ),
    "research_ops_service_alert_open": "Open service-health alerts.",
    "research_ops_service_alert_unacknowledged_due": (
        "Open service-health alerts past their acknowledgement deadline."
    ),
    "research_ops_runtime_control_generation": "Monotonic runtime-control generation.",
    "research_ops_snapshot_collection_success": (
        "Whether this metrics snapshot is complete."
    ),
    "research_ops_up": "Whether the metrics process can execute its request loop.",
    "research_ops_validation_age_seconds": (
        "Age of the latest audit validation observation."
    ),
    "research_ops_validation_last_pass": "Whether the latest audit validation passed.",
    "research_ops_validation_present": (
        "Whether an audit validation observation exists."
    ),
    "research_ops_outbox_workers_fresh": (
        "Outbox worker heartbeats within the freshness policy."
    ),
    "research_ops_research_job_workers_fresh": (
        "Research-job worker heartbeats within the freshness policy."
    ),
}


def collect_metrics(
    *,
    dsn: str | None = None,
    environ: Mapping[str, str] | None = None,
    observed_at: datetime | None = None,
) -> dict[str, float]:
    environment = os.environ if environ is None else environ
    now = observed_at or datetime.now(UTC)
    values: dict[str, float] = {
        "research_ops_up": 1.0,
        "research_ops_snapshot_collection_success": 0.0,
    }
    try:
        policy = HealthPolicy.from_environ(environment)
        with connection(dsn, connect_timeout=3) as conn:
            primary = conn.execute(
                "SELECT pg_is_in_recovery(), current_setting('transaction_read_only')"
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
                (now,),
            ).fetchone()
            workers = conn.execute(
                """
                SELECT
                    count(*) FILTER (WHERE worker_id LIKE 'outbox:%%'),
                    count(*) FILTER (WHERE worker_id LIKE 'research-job:%%')
                FROM research_ops.worker_heartbeat
                WHERE state IN ('IDLE', 'WORKING') AND last_seen_at >= %s
                """,
                (now - timedelta(seconds=policy.worker_heartbeat_max_age_seconds),),
            ).fetchone()
            receipts = conn.execute(
                """
                SELECT count(*)
                FROM research_ops.research_job_result_receipt
                WHERE applied_at IS NULL
                """
            ).fetchone()
            alerts = conn.execute(
                """
                SELECT
                    (SELECT count(*)
                     FROM research_ops.service_alert
                     WHERE status = 'OPEN'),
                    (SELECT count(*)
                     FROM research_ops.service_alert
                     WHERE status = 'OPEN'
                       AND acknowledgment_deadline_at <= %s),
                    (SELECT count(*)
                     FROM research_ops.service_alert_delivery
                     WHERE status IN ('PENDING', 'CLAIMED')),
                    (SELECT count(*)
                     FROM research_ops.service_alert_delivery
                     WHERE status = 'FAILED')
                """,
                (now,),
            ).fetchone()
            experiments = conn.execute(
                """
                SELECT count(*), count(*) FILTER (WHERE lease_expires_at <= %s)
                FROM research_ops.active_experiment_claim
                """,
                (now,),
            ).fetchone()
            jobs = conn.execute(
                """
                SELECT
                    count(*) FILTER (WHERE status = 'QUEUED'),
                    count(*) FILTER (WHERE status = 'RUNNING'),
                    count(*) FILTER (WHERE status = 'CANCEL_REQUESTED'),
                    count(*) FILTER (
                        WHERE status IN ('RUNNING', 'CANCEL_REQUESTED')
                          AND lease_expires_at <= %s
                    ),
                    count(*) FILTER (WHERE status = 'SUCCEEDED'),
                    count(*) FILTER (WHERE status = 'FAILED'),
                    count(*) FILTER (WHERE status = 'CANCELLED'),
                    count(*) FILTER (
                        WHERE status = 'FAILED'
                          AND error_code = 'EXECUTION_TIMEOUT'
                    ),
                    count(*) FILTER (
                        WHERE status = 'FAILED'
                          AND error_code = 'RESOURCE_EXHAUSTED'
                    ),
                    count(*) FILTER (
                        WHERE status = 'FAILED'
                          AND error_code IN (
                              'CAPABILITY_CONTRACT_INVALID',
                              'RESULT_CONTRACT_INVALID'
                          )
                    ),
                    COALESCE(sum(GREATEST(attempt_count - 1, 0)), 0),
                    COALESCE(avg(EXTRACT(EPOCH FROM (finished_at - started_at)))
                        FILTER (
                            WHERE finished_at IS NOT NULL
                              AND started_at IS NOT NULL
                        ), 0),
                    COALESCE(avg(EXTRACT(EPOCH FROM (started_at - queued_at)))
                        FILTER (WHERE started_at IS NOT NULL), 0)
                FROM public.portal_researchjob
                """,
                (now,),
            ).fetchone()
            validation = conn.execute(
                """
                SELECT status, observed_at
                FROM research_ops.validation_observation
                WHERE kind = %s
                """,
                (AUDIT_OBSERVATION_KIND,),
            ).fetchone()
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
        values.update(
            _complete_values(
                now=now,
                primary=primary,
                migrations=migrations,
                control=control,
                outbox=outbox,
                workers=workers,
                experiments=experiments,
                jobs=jobs,
                validation=validation,
                backup=backup,
                restore=restore,
                receipts=receipts,
                alerts=alerts,
            )
        )
        values["research_ops_database_up"] = 1.0
        values["research_ops_snapshot_collection_success"] = 1.0
    except Exception:
        values["research_ops_database_up"] = 0.0
    return {name: _finite_nonnegative(value) for name, value in values.items()}


def _complete_values(
    *,
    now: datetime,
    primary: Any,
    migrations: Mapping[str, str],
    control: Any,
    outbox: Any,
    workers: Any,
    experiments: Any,
    jobs: Any,
    validation: Any,
    backup: Any,
    restore: Any,
    receipts: Any,
    alerts: Any,
) -> dict[str, float]:
    if primary is None or control is None or outbox is None or alerts is None:
        raise ValueError("metrics_snapshot_incomplete")
    validation_present = validation is not None
    backup_time = backup[0] if backup is not None else None
    restore_present = restore is not None
    return {
        "research_ops_database_primary": float(
            not bool(primary[0]) and str(primary[1]).lower() == "off"
        ),
        "research_ops_migration_leaves_match": float(
            dict(migrations) == expected_migration_hashes()
        ),
        "research_ops_mutation_admission_open": float(bool(control[0])),
        "research_ops_claim_admission_open": float(bool(control[1])),
        "research_ops_integrity_quarantine": float(bool(control[2])),
        "research_ops_runtime_control_generation": float(control[3]),
        "research_ops_outbox_pending": float(outbox[0]),
        "research_ops_outbox_claimed": float(outbox[1]),
        "research_ops_outbox_dead_letter": float(outbox[2]),
        "research_ops_outbox_oldest_age_seconds": float(outbox[3]),
        "research_ops_active_experiment_claims": float(experiments[0]),
        "research_ops_experiment_expired_claims": float(experiments[1]),
        "research_ops_job_queued": float(jobs[0]),
        "research_ops_job_running": float(jobs[1]),
        "research_ops_job_cancel_requested": float(jobs[2]),
        "research_ops_job_expired_leases": float(jobs[3]),
        "research_ops_job_succeeded_total": float(jobs[4]),
        "research_ops_job_failed_total": float(jobs[5]),
        "research_ops_job_cancelled_total": float(jobs[6]),
        "research_ops_job_timeout_total": float(jobs[7]),
        "research_ops_job_resource_exhausted_total": float(jobs[8]),
        "research_ops_job_contract_violation_total": float(jobs[9]),
        "research_ops_job_retry_total": float(jobs[10]),
        "research_ops_job_average_runtime_seconds": float(jobs[11]),
        "research_ops_job_average_queue_seconds": float(jobs[12]),
        "research_ops_validation_present": float(validation_present),
        "research_ops_validation_last_pass": float(
            validation_present and validation[0] == "PASS"
        ),
        "research_ops_validation_age_seconds": _age(now, validation[1])
        if validation_present
        else 0.0,
        "research_ops_backup_present": float(backup_time is not None),
        "research_ops_backup_age_seconds": _age(now, backup_time)
        if backup_time is not None
        else 0.0,
        "research_ops_restore_drill_present": float(restore_present),
        "research_ops_restore_drill_last_pass": float(
            restore_present and restore[0] == "PASS"
        ),
        "research_ops_restore_drill_age_seconds": _age(now, restore[1])
        if restore_present
        else 0.0,
        "research_ops_outbox_workers_fresh": float(workers[0]),
        "research_ops_research_job_workers_fresh": float(workers[1]),
        "research_ops_job_receipts_unapplied": float(receipts[0]),
        "research_ops_service_alert_open": float(alerts[0]),
        "research_ops_service_alert_unacknowledged_due": float(alerts[1]),
        "research_ops_service_alert_delivery_pending": float(alerts[2]),
        "research_ops_service_alert_delivery_failed": float(alerts[3]),
    }


def render_prometheus(values: Mapping[str, float]) -> str:
    lines: list[str] = []
    for name in sorted(values):
        if name not in _METRIC_HELP:
            continue
        value = _finite_nonnegative(values[name])
        lines.extend(
            (
                f"# HELP {name} {_METRIC_HELP[name]}",
                f"# TYPE {name} gauge",
                f"{name} {_format_number(value)}",
            )
        )
    return "\n".join(lines) + "\n"


def _age(now: datetime, value: datetime) -> float:
    return max(0.0, (now - value).total_seconds())


def _finite_nonnegative(value: object) -> float:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        raise TypeError("metric_value_must_be_numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        return 0.0
    return parsed


def _format_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else format(value, ".6f").rstrip("0")


__all__ = ["collect_metrics", "render_prometheus"]
