"""Supported Web adapter surface for the Operations distribution.

This module is intentionally imported only after ``django.setup()``.  It keeps
the Operations package from coupling itself to arbitrary ``portal`` modules;
changes to this facade are reviewed as cross-distribution API changes.
"""

from collections.abc import Callable, Iterable, Mapping
from typing import Any, cast
from wsgiref.types import StartResponse

from portal.audit import (
    project_web_audit_event,
    record_web_audit_event,
    validate_web_audit_outbox,
)
from portal.execution import ResearchJobDispatcher
from portal.jobs import (
    JobCancellationRequested,
    JobExecutionResult,
    JobLeaseLost,
    complete_job_success,
    fail_job,
    finalize_cancelled,
)
from portal.models import (
    GovernanceDecision,
    ImportedDecisionReport,
    ManifestUpload,
    ResearchJob,
)
from portal.report_imports import validate_managed_import_record
from portal.security import reject_paths_in_job_payload
from portal.storage import (
    SafeArtifactRef,
    read_verified_manifest_bytes,
    resolve_artifact_ref,
    verify_result_artifact,
)
from portal.worker import PublicJobError


WebWSGIApplication = Callable[
    [Mapping[str, Any], StartResponse],
    Iterable[bytes],
]


def get_web_wsgi_application() -> WebWSGIApplication:
    """Return the configured Web WSGI entrypoint through the public facade."""

    from market_research_web.wsgi import application

    return cast(WebWSGIApplication, application)


__all__ = [
    "GovernanceDecision",
    "ImportedDecisionReport",
    "JobCancellationRequested",
    "JobExecutionResult",
    "JobLeaseLost",
    "ManifestUpload",
    "PublicJobError",
    "ResearchJob",
    "ResearchJobDispatcher",
    "SafeArtifactRef",
    "WebWSGIApplication",
    "complete_job_success",
    "fail_job",
    "finalize_cancelled",
    "get_web_wsgi_application",
    "project_web_audit_event",
    "read_verified_manifest_bytes",
    "record_web_audit_event",
    "reject_paths_in_job_payload",
    "resolve_artifact_ref",
    "validate_managed_import_record",
    "validate_web_audit_outbox",
    "verify_result_artifact",
]
