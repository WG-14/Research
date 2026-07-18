"""CLI dispatch kept thin enough for subprocess supervision."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .admission import AdmissionDecision, ExperimentAdmissionStore
from .migrate import apply_migrations
from .outbox import OutboxStore
from .worker import DjangoAuditProjector, OutboxWorker, WorkerSettings

if TYPE_CHECKING:
    from .backup import VerifiedBackup


def dispatch(args: argparse.Namespace) -> int:
    if args.command == "alert-raise":
        from .alerting import ServiceAlertStore

        service_alert = ServiceAlertStore().raise_alert(
            idempotency_key=args.idempotency_key,
            condition_code=args.condition_code,
            severity=args.severity,
            source_actor_id=args.source_actor_id,
            endpoint_id=args.endpoint_id,
            acknowledgment_timeout_seconds=args.acknowledgment_timeout_seconds,
        )
        _write(_alert_payload(service_alert))
        return 0

    if args.command == "alert-deliver-once":
        from .alerting import LoopbackOrHttpsAlertTransport, ServiceAlertStore
        from .errors import AlertTransportError

        service_alert_store = ServiceAlertStore()
        claim = service_alert_store.claim_delivery(
            worker_id=args.worker_id,
            endpoint_id=args.endpoint_id,
            lease_seconds=args.lease_seconds,
            max_attempts=args.max_attempts,
        )
        if claim is None:
            _write({"processed": False})
            return 0
        transport = LoopbackOrHttpsAlertTransport(
            _required_secret_file("RESEARCH_OPS_ALERT_ENDPOINT_URL_FILE")
        )
        try:
            response_code = transport.send(claim)
        except AlertTransportError:
            status = service_alert_store.record_delivery_failure(
                claim,
                reason_code="delivery_transport_failed",
                max_attempts=args.max_attempts,
                retry_delay_seconds=args.retry_delay_seconds,
            )
            _write(
                {
                    "alert_id": str(claim.alert_id),
                    "delivery_id": str(claim.delivery_id),
                    "processed": True,
                    "status": status,
                }
            )
            return 3
        service_alert_store.mark_delivered(claim, response_code=response_code)
        _write(
            {
                "alert_id": str(claim.alert_id),
                "delivery_id": str(claim.delivery_id),
                "processed": True,
                "status": "DELIVERED",
            }
        )
        return 0

    if args.command == "alert-acknowledge":
        from .alerting import ServiceAlertStore

        service_alert = ServiceAlertStore().acknowledge(
            alert_id=args.alert_id,
            actor_id=args.actor_id,
            reason_code=args.reason_code,
        )
        _write(_alert_payload(service_alert))
        return 0

    if args.command == "alert-escalate-once":
        from .alerting import ServiceAlertStore

        escalated_alert = ServiceAlertStore().escalate_due(
            actor_id=args.actor_id,
            endpoint_id=args.endpoint_id,
            repeat_after_seconds=args.repeat_after_seconds,
            maximum_level=args.maximum_level,
        )
        if escalated_alert is None:
            _write({"processed": False})
            return 0
        payload = _alert_payload(escalated_alert)
        payload["processed"] = True
        _write(payload)
        return 0

    if args.command == "alert-resolve":
        from .alerting import ServiceAlertStore

        service_alert = ServiceAlertStore().resolve(
            alert_id=args.alert_id,
            actor_id=args.actor_id,
            reason_code=args.reason_code,
        )
        _write(_alert_payload(service_alert))
        return 0

    if args.command == "audit-validate":
        from .health import record_audit_validation

        audit_result = record_audit_validation()
        _write(audit_result)
        return 0 if audit_result["status"] == "PASS" else 3

    if args.command == "metrics":
        from .metrics import collect_metrics, render_prometheus

        values = collect_metrics()
        print(render_prometheus(values), end="")
        return 0 if values.get("research_ops_snapshot_collection_success") == 1 else 3

    if args.command == "backup-fence":
        return _backup_fence(args)
    if args.command == "backup-manifest-create":
        return _backup_manifest_create(args)
    if args.command == "backup-verify":
        verified = _verify_backup(args)
        _write(verified.as_dict())
        return 0
    if args.command == "recovery-verify":
        from .backup import (
            create_signed_recovery_receipt,
            parse_recovery_started_at,
            record_restore_drill,
            verify_restored_application_state,
            verify_signed_recovery_receipt,
        )

        verified = _verify_backup(args)
        recovery_verification = verify_restored_application_state(
            verified_backup=verified,
            restore_namespace=Path(args.restore_namespace),
            maximum_records=args.maximum_records,
            started_at=(
                parse_recovery_started_at(args.started_at)
                if args.started_at is not None
                else None
            ),
        )
        receipt_path = Path(args.receipt_path)
        verification_key = Path(
            _required_env("RESEARCH_OPS_BACKUP_VERIFICATION_KEY_FILE")
        )
        if receipt_path.exists():
            (
                receipt_hash,
                registered_result,
                registered_document,
            ) = verify_signed_recovery_receipt(
                verification=recovery_verification,
                receipt_path=receipt_path,
                verification_public_key=verification_key,
            )
        else:
            receipt_hash, _signature_path = create_signed_recovery_receipt(
                verification=recovery_verification,
                receipt_path=receipt_path,
                signing_private_key=Path(
                    _required_env("RESEARCH_OPS_BACKUP_SIGNING_KEY_FILE")
                ),
                verification_public_key=verification_key,
            )
            registered_result = recovery_verification
            registered_document = recovery_verification.document()
        drill_id = record_restore_drill(
            control_dsn=_required_secret_file("RESEARCH_OPS_CONTROL_DATABASE_URL_FILE"),
            verification=registered_result,
            receipt_hash=receipt_hash,
        )
        payload = registered_document
        payload["control_drill_id"] = str(drill_id)
        _write(payload)
        return 0 if registered_result.status == "PASS" else 3

    if args.command == "recovery-activate":
        from .backup import (
            activate_verified_recovery,
            record_restore_drill,
            recovery_activation_state,
            verify_restored_application_state,
            verify_signed_recovery_receipt,
        )

        verified = _verify_backup(args)
        receipt_hash, signed_result, _document = verify_signed_recovery_receipt(
            verification=None,
            receipt_path=Path(args.receipt_path),
            verification_public_key=Path(
                _required_env("RESEARCH_OPS_BACKUP_VERIFICATION_KEY_FILE")
            ),
        )
        state = recovery_activation_state(verified)
        if state == "SEALED":
            activation_verification = verify_restored_application_state(
                verified_backup=verified,
                restore_namespace=Path(args.restore_namespace),
                maximum_records=args.maximum_records,
            )
            receipt_hash, signed_result, _document = verify_signed_recovery_receipt(
                verification=activation_verification,
                receipt_path=Path(args.receipt_path),
                verification_public_key=Path(
                    _required_env("RESEARCH_OPS_BACKUP_VERIFICATION_KEY_FILE")
                ),
            )
        drill_id = record_restore_drill(
            control_dsn=_required_secret_file("RESEARCH_OPS_CONTROL_DATABASE_URL_FILE"),
            verification=signed_result,
            receipt_hash=receipt_hash,
        )
        activation = activate_verified_recovery(
            verified_backup=verified,
            verification=signed_result,
            receipt_hash=receipt_hash,
            operator_id=args.operator_id,
        )
        activation["control_drill_id"] = str(drill_id)
        _write(activation)
        return 0

    if args.command == "migrate":
        migration_result = apply_migrations()
        _write(
            {
                "applied": migration_result.applied,
                "already_applied": migration_result.already_applied,
            }
        )
        return 0

    if args.command == "outbox-scan":
        discovered = OutboxStore().scan(batch_size=args.batch_size)
        _write({"discovered": discovered})
        return 0

    if args.command == "outbox-requeue":
        OutboxStore().requeue_dead_letter(
            event_id=args.event_id,
            expected_payload_hash=args.expected_payload_hash,
            operator_id=args.operator_id,
            reason=args.reason,
        )
        _write({"event_id": args.event_id, "status": "PENDING"})
        return 0

    if args.command == "outbox-worker":
        store = OutboxStore()
        outbox_worker = OutboxWorker(
            store=store,
            projector=DjangoAuditProjector(),
            settings=WorkerSettings(
                worker_id=args.worker_id,
                poll_interval=args.poll_interval,
                scan_batch_size=args.batch_size,
                lease_seconds=args.lease_seconds,
                max_attempts=args.max_attempts,
            ),
        )
        if args.once:
            store.worker_heartbeat(worker_id=args.worker_id, state="STARTING")
            try:
                processed = outbox_worker.run_one()
            finally:
                store.worker_heartbeat(worker_id=args.worker_id, state="STOPPED")
            _write({"processed": processed})
        else:
            outbox_worker.run_forever()
        return 0

    if args.command == "research-job-worker":
        from .research_job_worker import (
            ResearchJobWorker,
            ResearchJobWorkerSettings,
        )

        research_worker = ResearchJobWorker(
            admissions=ExperimentAdmissionStore(),
            settings=ResearchJobWorkerSettings(
                worker_id=args.worker_id,
                poll_interval=args.poll_interval,
                admission_lease_seconds=args.admission_lease_seconds,
            ),
        )
        if args.once:
            research_worker.heartbeat_store.worker_heartbeat(
                worker_id=research_worker.worker_heartbeat_id,
                state="STARTING",
            )
            try:
                processed = research_worker.run_one()
            finally:
                research_worker.heartbeat_store.worker_heartbeat(
                    worker_id=research_worker.worker_heartbeat_id,
                    state="STOPPED",
                )
            _write({"processed": processed})
        else:
            research_worker.run_forever()
        return 0

    if args.command == "admitted-run":
        from .admitted import run_admitted_research_command

        admitted_result = run_admitted_research_command(
            command=args.research_command,
            manifest_path=args.manifest,
            request_id=args.request_id,
            owner_id=args.owner_id,
            execution_calibration_path=args.execution_calibration,
            diagnostic_mode=args.diagnostic_mode,
            candidate_id=args.candidate_id,
            out_path=args.out,
            mode=args.mode,
            admission_lease_seconds=args.admission_lease_seconds,
        )
        _write(
            {
                "admission": _decision_payload(admitted_result.admission),
                "executed": admitted_result.executed,
                "residual_publication_window": (
                    admitted_result.residual_publication_window
                ),
            }
        )
        return admitted_result.exit_code

    if args.command == "admission-status":
        admissions = ExperimentAdmissionStore()
        decision = admissions.status(
            authority=args.authority,
            experiment_id=args.experiment_id,
            request_id=args.request_id,
        )
        if decision is None:
            _write({"status": "NOT_FOUND"})
            return 4
    else:
        raise RuntimeError(f"unknown_command:{args.command}")
    _write(_decision_payload(decision))
    return 0


def _backup_fence(args: argparse.Namespace) -> int:
    from .backup import (
        BackupContractError,
        BackupFenceStore,
        finalize_private_fence_receipt,
        read_private_fence_receipt,
        write_private_fence_intent,
    )

    store = BackupFenceStore()
    if args.fence_action == "begin":
        token = uuid.uuid4()
        write_private_fence_intent(fence_token=token, path=Path(args.receipt))
        status = store.begin(
            operator_id=args.operator_id,
            reason=args.reason,
            fence_token=token,
        )
        finalize_private_fence_receipt(status=status, path=Path(args.receipt))
    elif args.fence_action == "status":
        status = store.status()
    elif args.fence_action == "reconcile":
        token, generation = read_private_fence_receipt(Path(args.receipt))
        status = store.status()
        if (
            status.phase not in {"DRAINING", "SEALED", "QUARANTINED"}
            or status.fence_token != token
            or generation not in {0, status.generation}
        ):
            raise BackupContractError("private_fence_intent_not_committed")
        finalize_private_fence_receipt(status=status, path=Path(args.receipt))
    elif args.fence_action == "seal":
        token, _generation = read_private_fence_receipt(Path(args.receipt))
        status = store.seal(
            fence_token=token,
            audit_observation_max_age_seconds=args.audit_max_age_seconds,
        )
    elif args.fence_action == "reopen":
        token, _generation = read_private_fence_receipt(Path(args.receipt))
        status = store.reopen(
            fence_token=token,
            manifest_hash=args.manifest_hash,
            operator_id=args.operator_id,
        )
    elif args.fence_action == "quarantine":
        token = None
        if args.receipt:
            token, _generation = read_private_fence_receipt(Path(args.receipt))
        status = store.quarantine(
            operator_id=args.operator_id,
            reason=args.reason,
            fence_token=token,
        )
    else:
        raise RuntimeError("unknown_backup_fence_action")
    _write(status.as_dict())
    return 0


def _backup_manifest_create(args: argparse.Namespace) -> int:
    from .backup import (
        BackupContractError,
        BackupFenceStore,
        create_signed_backup_manifest,
        read_private_fence_receipt,
        verify_live_backup_database_state,
    )

    files: dict[str, str] = {}
    for value in args.file:
        role, separator, relative = value.partition("=")
        if not separator or not role or role in files or not relative:
            raise BackupContractError("backup_file_argument_invalid")
        files[role] = relative
    token, generation = read_private_fence_receipt(Path(args.fence_receipt))
    store = BackupFenceStore()
    status = store.status()
    if (
        status.phase != "SEALED"
        or status.fence_token != token
        or generation not in {0, status.generation}
    ):
        raise BackupContractError("backup_fence_not_sealed")
    verify_live_backup_database_state(expected_postgresql_major=args.postgresql_major)
    verified = create_signed_backup_manifest(
        backup_directory=Path(args.backup_directory),
        files=files,
        signing_private_key=Path(_required_env("RESEARCH_OPS_BACKUP_SIGNING_KEY_FILE")),
        verification_public_key=Path(
            _required_env("RESEARCH_OPS_BACKUP_VERIFICATION_KEY_FILE")
        ),
        backup_id=args.backup_id or uuid.uuid4(),
        fence_token=token,
        fence_generation=generation,
        git_sha=_required_env("RESEARCH_OPS_GIT_SHA"),
        release_id=_required_env("RESEARCH_OPS_RELEASE_ID"),
        build_digest=_required_env("RESEARCH_OPS_BUILD_DIGEST"),
        release_bundle_digest=_required_env("RESEARCH_OPS_RELEASE_BUNDLE_DIGEST"),
        postgresql_major=args.postgresql_major,
        audit_row_count=status.audit_row_count,
        audit_terminal_hash=status.audit_terminal_hash,
    )
    store.register_verified_backup(verified=verified, fence_token=token)
    _write(verified.as_dict())
    return 0


def _verify_backup(args: argparse.Namespace) -> VerifiedBackup:
    from .backup import verify_backup_set

    return verify_backup_set(
        backup_directory=Path(args.backup_directory),
        verification_public_key=Path(
            _required_env("RESEARCH_OPS_BACKUP_VERIFICATION_KEY_FILE")
        ),
        expected_git_sha=_required_env("RESEARCH_OPS_GIT_SHA"),
        expected_release_id=_required_env("RESEARCH_OPS_RELEASE_ID"),
        expected_build_digest=_required_env("RESEARCH_OPS_BUILD_DIGEST"),
        expected_release_bundle_digest=_required_env(
            "RESEARCH_OPS_RELEASE_BUNDLE_DIGEST"
        ),
        expected_postgresql_major=args.postgresql_major,
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"configuration_missing:{name}")
    return value


def _required_secret_file(name: str) -> str:
    raw = _required_env(name)
    path = Path(raw)
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_mode & 0o077
    ):
        raise RuntimeError(f"configuration_invalid:{name}")
    value = path.read_text(encoding="utf-8").rstrip("\r\n")
    if not value or "\x00" in value:
        raise RuntimeError(f"configuration_invalid:{name}")
    return value


def _decision_payload(decision: AdmissionDecision) -> dict[str, Any]:
    payload = asdict(decision)
    # Lease/fencing values form a bearer capability and must never be emitted
    # through argv-derived operator commands, stdout, logs, or diagnostics.
    payload.pop("lease_token", None)
    payload.pop("fencing_token", None)
    return {str(key): _json_value(value) for key, value in payload.items()}


def _alert_payload(alert: Any) -> dict[str, Any]:
    return {
        "alert_id": str(alert.alert_id),
        "escalation_level": int(alert.escalation_level),
        "last_event_hash": str(alert.last_event_hash),
        "status": str(alert.status),
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (uuid.UUID, datetime)):
        return str(value)
    return value


def _write(payload: dict[str, Any]) -> None:
    print(json.dumps(_json_value(payload), sort_keys=True, separators=(",", ":")))


__all__ = ["dispatch"]
