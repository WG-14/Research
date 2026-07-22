"""Persistent web-job worker guarded by PostgreSQL experiment admission."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from errno import EDQUOT, ENOSPC
from pathlib import Path
from types import FrameType
from typing import Any

import psycopg
from django.db import OperationalError as DjangoOperationalError
from market_research.application.platform_contracts import write_json_atomic
from market_research.application.process_sandbox import (
    IsolatedProcessError,
    IsolatedProcessPolicy,
    run_isolated_command,
)

from .admission import (
    ACTIVE,
    SUCCEEDED,
    AdmissionDecision,
    ExperimentAdmissionStore,
    ResearchJobResultReceipt,
)
from .database import RUNTIME_CONTROL_ADVISORY_LOCK_ID
from .errors import (
    ActiveExperimentConflict,
    AdmissionClaimLost,
    ExperimentIdentityConflict,
    ExperimentRequestConflict,
    MaintenanceFenceActive,
)
from .execution_capability import research_job_execution_context
from .outbox import OutboxStore, sanitize_error
from .runtime_guard import require_operated_preflight_receipt

RESEARCH_NAMESPACE_AUTHORITY = "market-research:experiment:v1"


def configure_django(
    settings_module: str = "market_research_web.settings",
) -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", settings_module)
    import django

    django.setup()


@dataclass(frozen=True, slots=True)
class ResearchJobWorkerSettings:
    worker_id: str
    poll_interval: float = 1.0
    admission_lease_seconds: int = 60

    def __post_init__(self) -> None:
        if not self.worker_id.strip() or len(self.worker_id) > 255:
            raise ValueError("worker_id_invalid")
        if not 0.05 <= self.poll_interval <= 60:
            raise ValueError("worker_poll_interval_invalid")
        if not 6 <= self.admission_lease_seconds <= 3600:
            raise ValueError("admission_lease_seconds_invalid")


class IsolatedJobProcessError(RuntimeError):
    """The supervised production dispatcher child failed without safe detail."""


class SandboxUnavailableError(IsolatedJobProcessError):
    """The mandatory operated execution sandbox could not be initialized."""


class FencedJobProgressReporter:
    def __init__(
        self,
        *,
        job_id: uuid.UUID,
        job_lease_token: uuid.UUID,
        admission: AdmissionDecision,
        admissions: ExperimentAdmissionStore,
        admission_lease_seconds: int,
        heartbeat_store: OutboxStore,
        worker_heartbeat_id: str,
    ) -> None:
        self.job_id = job_id
        self.job_lease_token = job_lease_token
        self.admission = admission
        self.admissions = admissions
        self.admission_lease_seconds = admission_lease_seconds
        self.heartbeat_store = heartbeat_store
        self.worker_heartbeat_id = worker_heartbeat_id

    def __call__(self, event: dict[str, Any]) -> None:
        stage = str(event.get("stage") or "working")
        details = {key: value for key, value in event.items() if key != "stage"}
        _heartbeat_research_job(
            job_id=self.job_id,
            lease_token=self.job_lease_token,
            stage=stage,
            details=details,
        )
        self.admissions.heartbeat(
            self.admission,
            lease_seconds=self.admission_lease_seconds,
        )
        self.heartbeat_store.worker_heartbeat(
            worker_id=self.worker_heartbeat_id,
            state="WORKING",
            event_id=self.job_id,
        )


class _CombinedHeartbeat:
    def __init__(
        self,
        reporter: FencedJobProgressReporter,
        interval_seconds: float,
    ) -> None:
        self.reporter = reporter
        self.interval_seconds = interval_seconds
        self.stop = threading.Event()
        self.errors: list[BaseException] = []
        self.thread = threading.Thread(
            target=self._run,
            name=f"research-job-heartbeat-{reporter.job_id}",
            daemon=True,
        )

    def __enter__(self) -> _CombinedHeartbeat:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop.set()
        self.thread.join(timeout=max(1.0, self.interval_seconds * 2))

    def raise_if_lost(self) -> None:
        if self.errors:
            raise AdmissionClaimLost("combined_job_admission_heartbeat_lost") from (
                self.errors[0]
            )

    def _run(self) -> None:
        from django.db import close_old_connections

        close_old_connections()
        try:
            while not self.stop.wait(self.interval_seconds):
                try:
                    self.reporter({"stage": "working"})
                except BaseException as exc:
                    self.errors.append(exc)
                    return
        finally:
            close_old_connections()


class ResearchJobWorker:
    def __init__(
        self,
        *,
        admissions: ExperimentAdmissionStore,
        settings: ResearchJobWorkerSettings,
        dispatcher: Any | None = None,
        heartbeat_store: OutboxStore | None = None,
    ) -> None:
        configure_django()
        if dispatcher is None:
            from market_research_web.operations_contract import ResearchJobDispatcher

            dispatcher = ResearchJobDispatcher()
            isolate_dispatcher = True
        else:
            isolate_dispatcher = False
        self.admissions = admissions
        self.settings = settings
        if settings.admission_lease_seconds > _job_lease_seconds():
            raise ValueError("admission_lease_must_not_exceed_job_lease")
        self.dispatcher = dispatcher
        self.isolate_dispatcher = isolate_dispatcher
        self.heartbeat_store = heartbeat_store or OutboxStore()
        self.worker_heartbeat_id = (
            settings.worker_id
            if settings.worker_id.startswith("research-job:")
            else f"research-job:{settings.worker_id}"
        )
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
        job = _claim_research_job(worker_id=self.settings.worker_id)
        if job is None:
            self._heartbeat_state("IDLE")
            return False
        self._heartbeat_state("WORKING", event_id=job.pk)
        if job.lease_token is None:
            raise RuntimeError("claimed_job_missing_lease_token")

        receipt = self.admissions.research_job_receipt(job.pk)
        # A receipt means the fenced PostgreSQL authority already committed a
        # successful result.  A cancellation request arriving in the narrow
        # receipt-to-ResearchJob window cannot reverse that terminal truth.
        if receipt is not None:
            self._apply_receipt(job, receipt)
            return True
        if job.status == "CANCEL_REQUESTED":
            from market_research_web.operations_contract import finalize_cancelled

            finalize_cancelled(job_id=job.pk, lease_token=job.lease_token)
            return True

        try:
            decision = self._acquire_when_available(job)
            if decision is not None:
                decision = self._wait_for_exclusive_admission(job, decision)
        except Exception as exc:
            from market_research_web.operations_contract import (
                JobCancellationRequested,
                finalize_cancelled,
            )

            if isinstance(exc, JobCancellationRequested):
                finalize_cancelled(job_id=job.pk, lease_token=job.lease_token)
                return True
            if isinstance(
                exc,
                (ExperimentIdentityConflict, ExperimentRequestConflict),
            ):
                _fail_owned_job(job, "ADMISSION_BINDING_CONFLICT")
                return True
            raise
        if decision is None:
            return True
        if decision.status == SUCCEEDED:
            receipt = self.admissions.research_job_receipt(job.pk)
            if receipt is None:
                _fail_owned_job(job, "ADMISSION_RECEIPT_MISSING")
            else:
                self._apply_receipt(job, receipt)
            return True
        if decision.status != ACTIVE or not decision.acquired:
            _fail_owned_job(job, "ADMISSION_STATE_INVALID")
            return True

        progress = FencedJobProgressReporter(
            job_id=job.pk,
            job_lease_token=job.lease_token,
            admission=decision,
            admissions=self.admissions,
            admission_lease_seconds=self.settings.admission_lease_seconds,
            heartbeat_store=self.heartbeat_store,
            worker_heartbeat_id=self.worker_heartbeat_id,
        )
        lease_seconds = _job_lease_seconds()
        interval = max(
            1.0, min(lease_seconds, self.settings.admission_lease_seconds) / 3
        )
        try:
            with _CombinedHeartbeat(progress, interval) as heartbeat:
                result = self._execute_dispatcher(job, progress, decision)
                heartbeat.raise_if_lost()
                progress({"stage": "finalizing"})
                from django.core.exceptions import ValidationError
                from market_research_web.operations_contract import (
                    verify_result_artifact,
                )

                verify_result_artifact(
                    result.result_ref,
                    expected_hash=result.result_hash,
                )
                if result.research_outcome not in {"PASS", "FAIL"}:
                    raise ValidationError("research_job_outcome_invalid")
        except Exception as exc:
            self._handle_execution_failure(job, decision, exc)
            return True

        # Admission completion and the recovery receipt commit atomically.
        # ResearchJob completion is a second transaction; a crash in between
        # is recovered from this immutable receipt without re-running research.
        self.admissions.complete_research_job(
            decision,
            job_id=job.pk,
            result_ref=str(result.result_ref),
            result_hash=result.result_hash,
            research_outcome=result.research_outcome,
            core_run_id=result.run_id,
        )
        from market_research_web.operations_contract import complete_job_success

        complete_job_success(
            job_id=job.pk,
            lease_token=job.lease_token,
            result=result,
            authoritative_result_committed=True,
        )
        self.admissions.mark_research_job_receipt_applied(
            job_id=job.pk,
            result_hash=result.result_hash,
        )
        return True

    def _execute_dispatcher(
        self,
        job: Any,
        progress: Any,
        decision: AdmissionDecision,
    ) -> Any:
        """Dispatch under an Operations capability and production process fence."""

        if getattr(self, "isolate_dispatcher", False):
            return _run_isolated_dispatcher_child(
                job_id=job.pk,
                decision=decision,
                progress=progress,
            )

        with research_job_execution_context(decision):
            return self.dispatcher.execute(job, progress)

    def run_forever(self, *, install_signal_handlers: bool = True) -> None:
        if install_signal_handlers:
            self.install_signal_handlers()
        self._heartbeat_state("STARTING")
        try:
            while not self.stop_requested.is_set():
                try:
                    processed = self.run_one()
                except (
                    AdmissionClaimLost,
                    MaintenanceFenceActive,
                    OSError,
                    TimeoutError,
                    psycopg.OperationalError,
                    psycopg.InterfaceError,
                    DjangoOperationalError,
                ) as exc:
                    processed = False
                    _log_job_worker_error(self.worker_heartbeat_id, exc)
                except Exception as exc:
                    from market_research_web.operations_contract import JobLeaseLost

                    if not isinstance(exc, JobLeaseLost):
                        raise
                    processed = False
                    _log_job_worker_error(self.worker_heartbeat_id, exc)
                if not processed:
                    self.stop_requested.wait(self.settings.poll_interval)
        finally:
            with suppress(Exception):
                self._heartbeat_state("STOPPED")

    def _acquire(self, job: Any) -> AdmissionDecision:
        return self.admissions.acquire(
            authority=RESEARCH_NAMESPACE_AUTHORITY,
            experiment_id=job.manifest.experiment_id,
            manifest_hash=job.manifest.manifest_hash,
            request_id=f"web-job:{job.pk}",
            request_hash=job.request_hash,
            owner_id=f"web-owner:{job.owner_id}",
            lease_seconds=self.settings.admission_lease_seconds,
        )

    def _acquire_when_available(self, job: Any) -> AdmissionDecision | None:
        """Wait through legitimate cross-process namespace contention.

        The ResearchJob lease is renewed while another request owns the
        experiment namespace.  Contention is therefore flow control, not a
        process-fatal worker error.
        """

        while True:
            try:
                return self._acquire(job)
            except ActiveExperimentConflict:
                self._heartbeat_state("WORKING", event_id=job.pk)
                if self.stop_requested.wait(self._admission_wait_interval()):
                    self._heartbeat_state("DRAINING", event_id=job.pk)
                    return None
                _heartbeat_research_job(
                    job_id=job.pk,
                    lease_token=job.lease_token,
                    stage="waiting_for_experiment_namespace",
                    details={},
                )

    def _wait_for_exclusive_admission(
        self,
        job: Any,
        decision: AdmissionDecision,
    ) -> AdmissionDecision | None:
        while decision.status == ACTIVE and not decision.acquired:
            receipt = self.admissions.research_job_receipt(job.pk)
            if receipt is not None:
                self._apply_receipt(job, receipt)
                return None
            if self.stop_requested.wait(self._admission_wait_interval()):
                self._heartbeat_state("DRAINING", event_id=job.pk)
                return None
            _heartbeat_research_job(
                job_id=job.pk,
                lease_token=job.lease_token,
                stage="waiting_for_admission",
                details={},
            )
            refreshed_decision = self._acquire_when_available(job)
            if refreshed_decision is None:
                return None
            decision = refreshed_decision
            self._heartbeat_state("WORKING", event_id=job.pk)
        return decision

    def _admission_wait_interval(self) -> float:
        return max(
            0.05,
            min(
                self.settings.poll_interval,
                _job_lease_seconds() / 3,
                self.settings.admission_lease_seconds / 3,
            ),
        )

    def _heartbeat_state(
        self,
        state: str,
        *,
        event_id: uuid.UUID | None = None,
    ) -> None:
        self.heartbeat_store.worker_heartbeat(
            worker_id=self.worker_heartbeat_id,
            state=state,
            event_id=event_id,
        )

    def _apply_receipt(
        self,
        job: Any,
        receipt: ResearchJobResultReceipt,
    ) -> None:
        if (
            receipt.authority != RESEARCH_NAMESPACE_AUTHORITY
            or receipt.experiment_id != job.manifest.experiment_id
            or receipt.request_id != f"web-job:{job.pk}"
            or receipt.request_hash != job.request_hash
        ):
            raise AdmissionClaimLost("research_job_receipt_binding_invalid")
        from market_research_web.operations_contract import (
            JobExecutionResult,
            SafeArtifactRef,
            complete_job_success,
        )

        result = JobExecutionResult(
            result_ref=SafeArtifactRef.parse(receipt.result_ref),
            result_hash=receipt.result_hash,
            run_id=receipt.core_run_id,
            research_outcome=receipt.research_outcome,
        )
        _heartbeat_research_job(
            job_id=job.pk,
            lease_token=job.lease_token,
            stage="recovering_fenced_result",
            details={},
        )
        complete_job_success(
            job_id=job.pk,
            lease_token=job.lease_token,
            result=result,
            authoritative_result_committed=True,
        )
        self.admissions.mark_research_job_receipt_applied(
            job_id=job.pk,
            result_hash=receipt.result_hash,
        )

    def _handle_execution_failure(
        self,
        job: Any,
        decision: AdmissionDecision,
        exc: Exception,
    ) -> None:
        from django.core.exceptions import ValidationError
        from market_research_web.operations_contract import (
            JobCancellationRequested,
            JobLeaseLost,
            PublicJobError,
            fail_job,
            finalize_cancelled,
        )

        if isinstance(exc, JobCancellationRequested):
            with suppress(AdmissionClaimLost):
                self.admissions.release(decision)
            finalize_cancelled(job_id=job.pk, lease_token=job.lease_token)
            return
        if isinstance(exc, PublicJobError):
            error_code = exc.error_code
        elif isinstance(exc, ValidationError):
            error_code = "RESULT_CONTRACT_INVALID"
        elif isinstance(exc, (AdmissionClaimLost, JobLeaseLost)):
            error_code = "ADMISSION_OR_JOB_LEASE_LOST"
        else:
            error_code = (
                classify_research_job_runtime_error(exc) or "UNEXPECTED_WORKER_ERROR"
            )
        with suppress(AdmissionClaimLost):
            self.admissions.fail(decision, error_code=error_code)
        with suppress(JobLeaseLost, JobCancellationRequested):
            fail_job(
                job_id=job.pk,
                lease_token=job.lease_token,
                error_code=error_code,
            )


def classify_research_job_runtime_error(exc: BaseException) -> str | None:
    """Map host/runtime failures to stable, aggregateable job error codes.

    Domain and result-contract exceptions are classified by the caller.  This
    helper covers failures that would otherwise collapse into the generic
    worker bucket, while never persisting exception text or host paths.
    """

    if isinstance(exc, SandboxUnavailableError):
        return "SANDBOX_UNAVAILABLE"
    if isinstance(exc, IsolatedJobProcessError):
        return "WORKER_CRASH"
    if isinstance(exc, MemoryError):
        return "RESOURCE_EXHAUSTED"
    if isinstance(exc, TimeoutError):
        return "EXECUTION_TIMEOUT"
    if isinstance(exc, PermissionError):
        return "STORAGE_PERMISSION_DENIED"
    if isinstance(exc, FileNotFoundError):
        return "RESEARCH_INPUT_UNAVAILABLE"
    if isinstance(exc, OSError) and exc.errno in {EDQUOT, ENOSPC}:
        return "STORAGE_EXHAUSTED"
    if isinstance(exc, OSError):
        return "RESEARCH_INPUT_UNAVAILABLE"
    return None


def _run_isolated_dispatcher_child(
    *,
    job_id: uuid.UUID,
    decision: AdmissionDecision,
    progress: Any,
) -> Any:
    """Run admitted application work inside the mandatory OS sandbox."""

    configure_django()
    from django.conf import settings as django_settings
    from market_research_web.operations_contract import (
        JobCancellationRequested,
        ResearchJob,
        ResearchJobDispatcher,
    )

    job = ResearchJob.objects.select_related(
        "owner", "manifest", "source_preflight_job"
    ).get(pk=job_id)
    dispatcher = ResearchJobDispatcher()
    sandbox_root = django_settings.RESEARCH_PATHS.artifact_path(
        "_operations_sandbox", str(job_id)
    ).resolve()
    control_root = sandbox_root / "control"
    control_root.mkdir(parents=True, exist_ok=True)
    request_path = control_root / "request.json"
    result_path = control_root / "result.json"
    log_path = control_root / "sandbox.log"
    with research_job_execution_context(decision):
        request = dispatcher.build_sandbox_request(job, sandbox_root=sandbox_root)
    write_json_atomic(request_path, request)
    settings_value = request["settings"]
    if not isinstance(settings_value, dict):
        raise IsolatedJobProcessError("isolated_research_job_settings_invalid")
    runtime_project_root = Path(str(request["runtime_project_root"])).resolve()
    package_source_root = Path(__file__).resolve().parents[4] / "src"
    readable_roots = {
        Path(sys.prefix).resolve(),
        runtime_project_root,
        package_source_root.resolve(),
        Path(str(request["manifest_path"])).resolve(),
        Path(str(settings_value["data_root"])).resolve(),
    }
    db_path = settings_value.get("db_path")
    if isinstance(db_path, str) and db_path:
        readable_roots.add(Path(db_path).resolve())
    progress({"stage": "preparing_sandbox"})
    last_cancellation_check = 0.0
    cancellation_cache = False

    def cancellation_requested() -> bool:
        nonlocal last_cancellation_check, cancellation_cache
        now = time.monotonic()
        if now - last_cancellation_check < 1.0:
            return cancellation_cache
        last_cancellation_check = now
        cancellation_cache = ResearchJob.objects.filter(
            pk=job_id,
            status=ResearchJob.Status.CANCEL_REQUESTED,
        ).exists()
        return cancellation_cache

    child_env = {
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), "/usr/bin", "/bin")),
        "VIRTUAL_ENV": str(Path(sys.prefix).resolve()),
        "PYTHONPATH": str(package_source_root.resolve()),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONHASHSEED": "0",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "BLIS_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
        "TMPDIR": "/tmp",
    }
    try:
        completed = run_isolated_command(
            (
                sys.executable,
                "-m",
                "market_research.application.sandbox_job",
                "--request",
                str(request_path),
                "--result",
                str(result_path),
            ),
            cwd=sandbox_root,
            env=child_env,
            readable_roots=tuple(sorted(readable_roots, key=str)),
            writable_roots=(sandbox_root,),
            policy=IsolatedProcessPolicy(
                wall_timeout_seconds=float(_job_execution_timeout_seconds()),
                memory_limit_mb=float(_job_child_memory_limit_mb()),
                output_limit_bytes=_job_child_output_limit_bytes(),
                process_limit=_job_child_process_limit(),
                file_descriptor_limit=1024,
                network_access=False,
            ),
            output_path=log_path,
            cancellation_requested=cancellation_requested,
        )
    except IsolatedProcessError as exc:
        raise SandboxUnavailableError("research_job_sandbox_unavailable") from exc
    if completed.status == "sandbox_unavailable":
        raise SandboxUnavailableError("research_job_sandbox_unavailable")
    if completed.status == "cancelled":
        raise JobCancellationRequested("research_job_cancellation_requested")
    if completed.status == "timed_out":
        raise TimeoutError("isolated_research_job_execution_timeout")
    if completed.status == "resource_exhausted":
        raise MemoryError("isolated_research_job_resource_exhausted")
    if completed.status != "succeeded":
        raise IsolatedJobProcessError("isolated_research_job_child_failed")
    try:
        if result_path.stat().st_size > 16 * 1024 * 1024:
            raise IsolatedJobProcessError("isolated_research_job_result_too_large")
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IsolatedJobProcessError("isolated_research_job_result_invalid") from exc
    progress({"stage": "validating_output"})
    return dispatcher.accept_sandbox_result(
        job,
        payload,
        sandbox_root=sandbox_root,
    )


def _job_execution_timeout_seconds() -> int:
    raw = os.environ.get("RESEARCH_OPS_JOB_EXECUTION_TIMEOUT_SECONDS", "21600")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("research_job_execution_timeout_invalid") from exc
    if not 30 <= value <= 86400:
        raise RuntimeError("research_job_execution_timeout_invalid")
    return value


def _job_child_memory_limit_mb() -> int:
    raw = os.environ.get("RESEARCH_OPS_JOB_CHILD_MEMORY_LIMIT_MB", "2048")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("research_job_child_memory_limit_invalid") from exc
    if not 256 <= value <= 32768:
        raise RuntimeError("research_job_child_memory_limit_invalid")
    return value


def _job_child_output_limit_bytes() -> int:
    raw = os.environ.get("RESEARCH_OPS_JOB_CHILD_OUTPUT_LIMIT_BYTES", "67108864")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("research_job_child_output_limit_invalid") from exc
    if not 1_048_576 <= value <= 1_073_741_824:
        raise RuntimeError("research_job_child_output_limit_invalid")
    return value


def _job_child_process_limit() -> int:
    raw = os.environ.get("RESEARCH_OPS_JOB_CHILD_PROCESS_LIMIT", "128")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("research_job_child_process_limit_invalid") from exc
    if not 16 <= value <= 1024:
        raise RuntimeError("research_job_child_process_limit_invalid")
    return value


def _claim_research_job(*, worker_id: str) -> Any | None:
    from django.db import connection as django_connection
    from django.db import transaction
    from django.db.models import F, Q
    from django.utils import timezone
    from market_research_web.operations_contract import (
        ResearchJob,
        record_web_audit_event,
    )

    now = timezone.now()
    lease_expires_at = now + timedelta(seconds=_job_lease_seconds())
    with transaction.atomic():
        with django_connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock_shared(%s)",
                (RUNTIME_CONTROL_ADVISORY_LOCK_ID,),
            )
            cursor.execute(
                """
                SELECT mutation_admission_open, integrity_quarantine
                FROM research_ops.runtime_control
                WHERE singleton_id = 1
                """
            )
            runtime_control = cursor.fetchone()
        if (
            runtime_control is None
            or runtime_control[0] is not True
            or runtime_control[1] is True
        ):
            return None
        job = (
            ResearchJob.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("owner", "manifest", "source_preflight_job")
            .filter(
                Q(status=ResearchJob.Status.QUEUED)
                | Q(
                    status__in=(
                        ResearchJob.Status.RUNNING,
                        ResearchJob.Status.CANCEL_REQUESTED,
                    ),
                    lease_expires_at__lte=now,
                )
            )
            .order_by("queued_at", "pk")
            .first()
        )
        if job is None:
            return None
        recovered = job.status != ResearchJob.Status.QUEUED
        token = uuid.uuid4()
        update: dict[str, Any] = {
            "heartbeat_at": now,
            "lease_token": token,
            "lease_expires_at": lease_expires_at,
            "attempt_count": F("attempt_count") + 1,
            "version": F("version") + 1,
            "progress_stage": "recovering" if recovered else "starting",
            "updated_at": now,
        }
        if job.status == ResearchJob.Status.QUEUED:
            update.update(status=ResearchJob.Status.RUNNING, started_at=now)
        ResearchJob.objects.filter(pk=job.pk).update(**update)
        job.refresh_from_db()
        record_web_audit_event(
            action=("research_job_recovered" if recovered else "research_job_claimed"),
            actor_id=str(worker_id)[:255],
            object_type="research_job",
            object_id=str(job.pk),
            correlation_id=str(job.correlation_id),
            details={
                "attempt_count": job.attempt_count,
                "external_admission": True,
            },
        )
    return job


def _heartbeat_research_job(
    *,
    job_id: uuid.UUID,
    lease_token: uuid.UUID,
    stage: str,
    details: dict[str, Any],
) -> Any:
    from django.db.models import F
    from django.utils import timezone
    from market_research_web.operations_contract import (
        JobCancellationRequested,
        JobLeaseLost,
        ResearchJob,
        reject_paths_in_job_payload,
    )

    normalized_stage = str(stage or "").strip()
    if not normalized_stage or len(normalized_stage) > 128:
        raise ValueError("job_progress_stage_invalid")
    safe_details = dict(details)
    reject_paths_in_job_payload(safe_details)
    now = timezone.now()
    updated = ResearchJob.objects.filter(
        pk=job_id,
        lease_token=lease_token,
        lease_expires_at__gt=now,
        status__in=(
            ResearchJob.Status.RUNNING,
            ResearchJob.Status.CANCEL_REQUESTED,
        ),
    ).update(
        progress_stage=normalized_stage,
        progress_details=safe_details,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=_job_lease_seconds()),
        version=F("version") + 1,
        updated_at=now,
    )
    if updated != 1:
        raise JobLeaseLost("research_job_lease_lost")
    job = ResearchJob.objects.get(pk=job_id)
    if job.status == ResearchJob.Status.CANCEL_REQUESTED:
        raise JobCancellationRequested("research_job_cancellation_requested")
    return job


def _fail_owned_job(job: Any, error_code: str) -> None:
    from market_research_web.operations_contract import (
        JobCancellationRequested,
        JobLeaseLost,
        fail_job,
    )

    with suppress(JobLeaseLost, JobCancellationRequested):
        fail_job(
            job_id=job.pk,
            lease_token=job.lease_token,
            error_code=error_code,
        )


def _job_lease_seconds() -> int:
    from django.conf import settings

    value = int(settings.INTERNAL_WEB_JOB_LEASE_SECONDS)
    if not 3 <= value <= 3600:
        raise RuntimeError("internal_web_job_lease_seconds_invalid")
    return value


def _log_job_worker_error(worker_id: str, exc: BaseException) -> None:
    payload = {
        "schema_version": 1,
        "severity": "WARNING",
        "service_role": "research-job-worker",
        "event_code": "worker_iteration_failed",
        "worker_id": worker_id,
        "error_category": "transient_dependency",
        "error": sanitize_error(exc),
    }
    print(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        file=sys.stderr,
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    from .cli import main as operations_main

    return operations_main(
        ["research-job-worker", *(sys.argv[1:] if argv is None else argv)]
    )


__all__ = [
    "RESEARCH_NAMESPACE_AUTHORITY",
    "FencedJobProgressReporter",
    "ResearchJobWorker",
    "ResearchJobWorkerSettings",
    "IsolatedJobProcessError",
    "SandboxUnavailableError",
    "classify_research_job_runtime_error",
    "configure_django",
    "main",
]
