"""Fail-closed access to authoritative research decision reports.

The database supplies only an ownership-scoped candidate index.  Every public
operation re-reads and verifies the web validation summary and the canonical
decision report from server-derived managed paths before returning metadata or
running a comparison.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Q

from market_research.application.authorization import ensure_capability_authorized
from market_research.application.contracts import (
    ActorContext,
    ReportComparisonRequest,
    ReportComparisonResult,
)
from market_research.application.service import ResearchApplicationService
from market_research.application.platform_contracts import ResearchPathError
from market_research.application.adapter_contracts import (
    content_hash_payload,
    sha256_prefixed,
    validate_research_decision_report,
)

from .authorization import jobs_visible_to
from .models import ImportedDecisionReport, ResearchJob
from .presenters import redact_server_topology
from .report_imports import validate_managed_import_record
from .security import actor_snapshot, reject_symlink_components, validate_sha256
from .storage import SafeArtifactRef, resolve_artifact_ref, verify_result_artifact


MAX_CATALOG_PAGE = 50
MAX_VISIBLE_VALIDATION_JOBS = 500


@dataclass(frozen=True, slots=True)
class VerifiedDecisionReport:
    """Server-side verified report plus path-free catalog metadata."""

    report_id: str
    report_hash: str
    summary_hash: str
    manifest_hash: str
    experiment_id: str
    run_id: str
    validation_result: str
    selected_candidate_id: str | None
    market: str
    interval: str
    strategy_name: str
    strategy_version: str
    catalog_source: str
    payload: dict[str, Any]

    def catalog_item(self) -> dict[str, Any]:
        item = {
            "schema_version": 1,
            "report_id": self.report_id,
            "report_hash": self.report_hash,
            "summary_hash": self.summary_hash,
            "manifest_hash": self.manifest_hash,
            "experiment_id": self.experiment_id,
            "run_id": self.run_id,
            "validation_result": self.validation_result,
            "selected_candidate_id": self.selected_candidate_id,
            "market": self.market,
            "interval": self.interval,
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "catalog_source": self.catalog_source,
            "integrity_status": "VERIFIED",
        }
        safe_item = redact_server_topology(item)
        if not isinstance(safe_item, dict):
            raise RuntimeError("report_catalog_projection_invalid")
        return safe_item


class VisibleDecisionReportResolver:
    """Resolve reports visible to one actor without treating DB rows as evidence."""

    def __init__(self, user: Any) -> None:
        self.user = user
        actor_id, roles, permissions = actor_snapshot(user)
        self.actor = ActorContext(
            actor_id=actor_id,
            roles=tuple(roles),
            permissions=frozenset(permissions),
            source="web",
        )

    def list_reports(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        ensure_capability_authorized("reports.list", self.actor)
        _validate_page(limit=limit, offset=offset)
        verified = self._scan_verified_reports()
        return tuple(item.catalog_item() for item in verified[offset : offset + limit])

    def load_reports(
        self,
        report_ids: tuple[str, ...],
    ) -> dict[str, dict[str, Any]]:
        """Return exact requested reports or one non-enumerating failure."""

        ensure_capability_authorized("research-compare", self.actor)
        normalized = ReportComparisonRequest(report_ids=report_ids).report_ids
        requested = set(normalized)
        resolved: dict[str, dict[str, Any]] = {}
        for item in self._scan_verified_reports():
            if item.report_id in requested:
                resolved[item.report_id] = item.payload
                if len(resolved) == len(requested):
                    break
        if set(resolved) != requested:
            raise ValidationError("report_not_visible_or_invalid")
        return {report_id: resolved[report_id] for report_id in normalized}

    def _scan_verified_reports(self) -> list[VerifiedDecisionReport]:
        jobs = (
            jobs_visible_to(self.user)
            .filter(
                capability_id=ResearchJob.Capability.VALIDATE,
                status=ResearchJob.Status.SUCCEEDED,
            )
            .order_by("-created_at", "-pk")[:MAX_VISIBLE_VALIDATION_JOBS]
        )
        reports: list[VerifiedDecisionReport] = []
        seen: set[str] = set()
        for job in jobs:
            try:
                verified = _verify_validation_job_report(job)
            except ValidationError:
                # An invalid candidate never becomes catalog authority.  Exact
                # lookups still fail closed with a generic not-found response.
                continue
            if verified.report_id not in seen:
                seen.add(verified.report_id)
                reports.append(verified)
        imported = ImportedDecisionReport.objects.select_related("owner")
        if not self.user.has_perm("portal.view_all_research_jobs"):
            imported = imported.filter(
                Q(owner=self.user)
                | Q(visibility=(ImportedDecisionReport.Visibility.ORGANIZATION))
            )
        for record in imported.order_by("-created_at", "-pk")[
            :MAX_VISIBLE_VALIDATION_JOBS
        ]:
            try:
                verified = _verify_imported_report(record)
            except ValidationError:
                continue
            if verified.report_id not in seen:
                seen.add(verified.report_id)
                reports.append(verified)
        return reports


def list_visible_reports(
    user: Any,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[dict[str, Any], ...]:
    return VisibleDecisionReportResolver(user).list_reports(
        limit=limit,
        offset=offset,
    )


def compare_visible_reports(
    user: Any,
    request: ReportComparisonRequest,
    *,
    service: ResearchApplicationService,
) -> dict[str, Any]:
    """Compare visible reports and return a separately hash-bound safe projection."""

    resolver = VisibleDecisionReportResolver(user)
    trusted_request = request.model_copy(update={"actor": resolver.actor})
    result: ReportComparisonResult = service.compare_reports(
        trusted_request,
        report_loader=resolver.load_reports,
    )
    if not result.ok or result.comparison is None or result.content_hash is None:
        raise ValidationError("report_comparison_failed")

    safe_comparison = redact_server_topology(result.comparison)
    if not isinstance(safe_comparison, dict):
        raise ValidationError("report_comparison_projection_invalid")
    embedded_hash = safe_comparison.pop("content_hash", None)
    if embedded_hash != result.content_hash:
        raise ValidationError("report_comparison_source_hash_mismatch")

    document: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "internal_web_report_comparison_projection",
        "source_report_ids": [source.report_id for source in result.sources],
        "source_report_hashes": [source.report_hash for source in result.sources],
        "source_comparison_hash": result.content_hash,
        "comparison": safe_comparison,
    }
    document["content_hash"] = sha256_prefixed(content_hash_payload(document))
    return document


def _verify_validation_job_report(job: ResearchJob) -> VerifiedDecisionReport:
    if (
        job.capability_id != ResearchJob.Capability.VALIDATE
        or job.status != ResearchJob.Status.SUCCEEDED
    ):
        raise ValidationError("report_source_job_not_succeeded_validation")

    expected_summary_ref = SafeArtifactRef(
        "report",
        f"_internal_web/{job.pk}/validation_result.json",
    )
    recorded_summary_ref = SafeArtifactRef.parse(job.result_ref)
    if recorded_summary_ref != expected_summary_ref:
        raise ValidationError("report_summary_ref_binding_mismatch")
    summary_candidate = settings.RESEARCH_PATHS.report_root.joinpath(
        *Path(recorded_summary_ref.relative_path).parts
    )
    reject_symlink_components(summary_candidate)
    summary = verify_result_artifact(
        recorded_summary_ref,
        expected_hash=job.result_hash,
    )
    _validate_summary_binding(job, summary)

    experiment_id = str(summary["experiment_id"])
    try:
        candidate_path = settings.RESEARCH_PATHS.report_path(
            "research",
            experiment_id,
            "research_candidate_report.json",
        )
        candidate_relative = candidate_path.relative_to(
            settings.RESEARCH_PATHS.report_root
        ).as_posix()
    except (ResearchPathError, ValueError) as exc:
        raise ValidationError("candidate_report_server_path_invalid") from exc
    reject_symlink_components(candidate_path)
    candidate_ref = SafeArtifactRef("report", candidate_relative)
    resolved_candidate_path = resolve_artifact_ref(candidate_ref)
    report = _read_bounded_json(resolved_candidate_path)

    reasons = validate_research_decision_report(report)
    if reasons:
        raise ValidationError("candidate_report_schema_or_hash_invalid")
    report_hash = validate_sha256(
        str(report.get("content_hash") or ""),
        field="candidate_report_hash",
    )
    expected_report_hash = validate_sha256(
        str(summary.get("research_candidate_report_hash") or ""),
        field="summary_candidate_report_hash",
    )
    if report_hash != expected_report_hash:
        raise ValidationError("candidate_report_summary_hash_mismatch")
    _validate_candidate_binding(job, summary, report)

    conditions = report["sections"]["hypothesis_and_experiment_conditions"]
    return VerifiedDecisionReport(
        report_id=_opaque_report_id(report_hash),
        report_hash=report_hash,
        summary_hash=job.result_hash,
        manifest_hash=str(report["manifest_hash"]),
        experiment_id=str(report["experiment_id"]),
        run_id=str(report["run_id"]),
        validation_result=str(report["validation_result"]),
        selected_candidate_id=(
            str(report["selected_candidate_id"])
            if report.get("selected_candidate_id") is not None
            else None
        ),
        market=str(conditions["market"]),
        interval=str(conditions["interval"]),
        strategy_name=str(conditions["strategy_name"]),
        strategy_version=str(conditions["strategy_version"]),
        catalog_source="WEB_VALIDATION",
        payload=report,
    )


def _verify_imported_report(
    record: ImportedDecisionReport,
) -> VerifiedDecisionReport:
    try:
        reference = SafeArtifactRef.parse(record.storage_ref)
    except ValidationError as exc:
        raise ValidationError("historical_report_catalog_ref_invalid") from exc
    if reference.root != "report":
        raise ValidationError("historical_report_catalog_ref_invalid")
    resolved = resolve_artifact_ref(reference)
    report = _read_bounded_json(resolved)
    binding = validate_managed_import_record(record, report)
    return VerifiedDecisionReport(
        report_id=record.report_id,
        report_hash=binding["report_hash"],
        summary_hash=record.import_manifest_hash,
        manifest_hash=binding["manifest_hash"],
        experiment_id=binding["experiment_id"],
        run_id=binding["run_id"],
        validation_result=binding["validation_result"],
        selected_candidate_id=(binding["selected_candidate_id"] or None),
        market=binding["market"],
        interval=binding["interval"],
        strategy_name=binding["strategy_name"],
        strategy_version=binding["strategy_version"],
        catalog_source="HISTORICAL_CLI_IMPORT",
        payload=report,
    )


def _validate_summary_binding(job: ResearchJob, summary: dict[str, Any]) -> None:
    if (
        summary.get("schema_version") != 3
        or summary.get("artifact_type") != "validated_research_result"
    ):
        raise ValidationError("validation_summary_contract_invalid")
    validate_sha256(job.result_hash, field="result_hash")
    validate_sha256(job.manifest.manifest_hash, field="manifest_hash")
    bindings = (
        (summary.get("experiment_id"), job.manifest.experiment_id),
        (summary.get("manifest_hash"), job.manifest.manifest_hash),
        (summary.get("run_id"), job.run_id),
        (summary.get("end_to_end_validation_result"), job.research_outcome),
    )
    if (
        job.owner_id != job.manifest.owner_id
        or not job.run_id
        or not job.research_outcome
        or any(actual != expected for actual, expected in bindings)
    ):
        raise ValidationError("validation_summary_job_binding_mismatch")


def _validate_candidate_binding(
    job: ResearchJob,
    summary: dict[str, Any],
    report: dict[str, Any],
) -> None:
    validate_sha256(str(report.get("manifest_hash") or ""), field="manifest_hash")
    summary_selection_hash = validate_sha256(
        str(summary.get("selection_report_hash") or ""),
        field="summary_selection_report_hash",
    )
    report_selection_hash = validate_sha256(
        str(report.get("selection_report_hash") or ""),
        field="candidate_selection_report_hash",
    )
    bindings = (
        (report.get("experiment_id"), summary.get("experiment_id")),
        (report.get("manifest_hash"), summary.get("manifest_hash")),
        (report.get("run_id"), summary.get("run_id")),
        (report.get("selected_candidate_id"), summary.get("selected_candidate_id")),
        (report.get("validation_result"), summary.get("end_to_end_validation_result")),
        (report_selection_hash, summary_selection_hash),
    )
    if any(actual != expected for actual, expected in bindings):
        raise ValidationError("candidate_report_summary_binding_mismatch")

    conditions = report["sections"]["hypothesis_and_experiment_conditions"]
    dimension_bindings = (
        (conditions.get("market"), summary.get("market")),
        (conditions.get("interval"), summary.get("interval")),
        (conditions.get("strategy_name"), summary.get("strategy_name")),
        (conditions.get("strategy_version"), summary.get("strategy_version")),
    )
    if any(
        not isinstance(actual, str) or not actual.strip() or actual != expected
        for actual, expected in dimension_bindings
    ):
        raise ValidationError("candidate_report_dimension_binding_mismatch")
    if report.get("manifest_hash") != job.manifest.manifest_hash:
        raise ValidationError("candidate_report_manifest_binding_mismatch")


def _read_bounded_json(path: Path) -> dict[str, Any]:
    limit = int(settings.INTERNAL_WEB_MAX_RESULT_BYTES)
    if limit <= 0:
        raise ValidationError("candidate_report_read_limit_invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                descriptor = -1
                content = handle.read(limit + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except OSError as exc:
        raise ValidationError("candidate_report_unavailable") from exc
    if len(content) > limit:
        raise ValidationError("candidate_report_too_large_to_verify")
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("candidate_report_json_invalid") from exc
    if not isinstance(payload, dict):
        raise ValidationError("candidate_report_must_be_object")
    return payload


def _opaque_report_id(report_hash: str) -> str:
    validate_sha256(report_hash, field="candidate_report_hash")
    return "report_" + report_hash.removeprefix("sha256:")


def _validate_page(*, limit: int, offset: int) -> None:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_CATALOG_PAGE
    ):
        raise ValidationError("report_catalog_limit_invalid")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValidationError("report_catalog_offset_invalid")


__all__ = [
    "MAX_CATALOG_PAGE",
    "MAX_VISIBLE_VALIDATION_JOBS",
    "VerifiedDecisionReport",
    "VisibleDecisionReportResolver",
    "compare_visible_reports",
    "list_visible_reports",
]
