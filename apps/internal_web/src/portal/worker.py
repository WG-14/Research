from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from django.core.exceptions import ValidationError

from .jobs import (
    JobCancellationRequested,
    JobExecutionResult,
    claim_next_job,
    complete_job_success,
    fail_job,
    finalize_cancelled,
    update_job_progress,
)
from .models import ResearchJob


class JobDispatcher(Protocol):
    def execute(
        self,
        job: ResearchJob,
        progress: "JobProgressReporter",
    ) -> JobExecutionResult:
        ...


class PublicJobError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(frozen=True, slots=True)
class JobProgressReporter:
    job_id: Any
    lease_token: Any

    def __call__(self, event: dict[str, Any]) -> None:
        stage = str(event.get("stage") or "working")
        details = {key: value for key, value in event.items() if key != "stage"}
        update_job_progress(
            job_id=self.job_id,
            lease_token=self.lease_token,
            stage=stage,
            details=details,
        )


def run_worker_once(
    dispatcher: JobDispatcher,
    *,
    worker_id: str = "internal-web-worker",
) -> ResearchJob | None:
    """Claim and execute at most one job through a direct Python dispatcher."""

    job = claim_next_job(worker_id=worker_id)
    if job is None:
        return None
    if job.lease_token is None:
        raise RuntimeError("claimed_job_missing_lease_token")
    progress = JobProgressReporter(job.pk, job.lease_token)
    try:
        result = dispatcher.execute(job, progress)
    except JobCancellationRequested:
        return finalize_cancelled(job_id=job.pk, lease_token=job.lease_token)
    except PublicJobError as exc:
        return fail_job(
            job_id=job.pk,
            lease_token=job.lease_token,
            error_code=exc.error_code,
        )
    except ValidationError:
        return fail_job(
            job_id=job.pk,
            lease_token=job.lease_token,
            error_code="RESULT_CONTRACT_INVALID",
        )
    except Exception:
        return fail_job(
            job_id=job.pk,
            lease_token=job.lease_token,
            error_code="UNEXPECTED_WORKER_ERROR",
        )

    # Keep the terminal transition outside the dispatcher catch-all.  If its
    # append-only audit write fails after the database commit, the job is
    # already SUCCEEDED and must never be reclassified as a worker failure.
    try:
        return complete_job_success(
            job_id=job.pk,
            lease_token=job.lease_token,
            result=result,
        )
    except JobCancellationRequested:
        return finalize_cancelled(job_id=job.pk, lease_token=job.lease_token)
    except ValidationError:
        return fail_job(
            job_id=job.pk,
            lease_token=job.lease_token,
            error_code="RESULT_CONTRACT_INVALID",
        )
