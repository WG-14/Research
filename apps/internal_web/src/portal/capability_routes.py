"""Explicit web workflow coverage for required application capabilities."""

from __future__ import annotations


# Values are (Django URL name, Django permission).  Multiple shared service
# capabilities intentionally converge on the guarded composite preflight route.
WEB_CAPABILITY_WORKFLOWS: dict[str, tuple[str, str]] = {
    "research-preflight": ("manifest-preflight", "portal.submit_research_job"),
    "research-readiness": ("manifest-preflight", "portal.submit_research_job"),
    "research-workload-estimate": (
        "manifest-preflight",
        "portal.submit_research_job",
    ),
    "research-validate": ("job-submit-validation", "portal.submit_research_job"),
    "research-compare": ("report-compare", "portal.view_researchjob"),
    "research-record-human-review": (
        "review-record",
        "portal.record_research_review",
    ),
    "research-approve-strategy-candidate": (
        "review-approve",
        "portal.approve_research_candidate",
    ),
    "jobs.list": ("job-list", "portal.view_researchjob"),
    "jobs.detail": ("job-detail", "portal.view_researchjob"),
    "reports.list": ("report-list", "portal.view_researchjob"),
    "reports.detail": ("job-detail", "portal.view_researchjob"),
    "reports.download": ("job-download", "portal.view_researchjob"),
    "research.explore": ("research-explorer", "portal.view_researchjob"),
}
