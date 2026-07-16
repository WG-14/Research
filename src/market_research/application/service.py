"""Shared application services over the existing research engine.

This module may be called by CLI, web, or worker adapters.  It does not import
any of those adapters and it never launches the CLI or a subprocess.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from market_research.paths import ResearchPathManager

from market_research.research.datasets.registry import default_dataset_adapter_registry
from market_research.research.execution_calibration import (
    ExecutionCalibrationError,
    load_calibration_artifact,
)
from market_research.research.experiment_identity import (
    bind_research_validation_experiment,
)
from market_research.research.experiment_manifest import (
    ManifestValidationError,
    load_manifest_with_registry,
)
from market_research.research.run_lifecycle import start_run
from market_research.research.validation_pipeline import (
    ValidationRunError,
    run_research_validation,
)
from market_research.research.validation_protocol import ResearchValidationError
from market_research.research.readiness import build_research_readiness_report
from market_research.research.research_reporting import (
    ResearchReportingError,
    compare_research_decision_reports,
)
from market_research.research.workload_estimate import (
    build_manifest_workload_estimate_from_path,
)

from .authorization import ensure_capability_authorized
from .contracts import (
    ApplicationError,
    ArtifactReference,
    ReportComparisonRequest,
    ReportComparisonResult,
    ReportComparisonSource,
    ResearchPreflightRequest,
    ResearchPreflightResult,
    ResearchReadinessResult,
    ResearchValidationRequest,
    ResearchValidationResult,
    ResearchWorkloadResult,
    ResultStatus,
)
from .errors import ApplicationCancellation


ProgressCallback = Callable[[dict[str, Any]], None]
CancellationCheck = Callable[[], bool]
ReportLoader = Callable[[tuple[str, ...]], Mapping[str, dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class ResearchApplicationService:
    paths: ResearchPathManager
    strategy_registry: Any
    environment_summary: dict[str, object] | None = None

    def preflight(
        self,
        request: ResearchPreflightRequest,
        *,
        progress_callback: ProgressCallback | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> ResearchPreflightResult:
        """Run the two preflight projections through their canonical services."""

        ensure_capability_authorized("research-preflight", request.actor)
        readiness = self.readiness(
            request,
            progress_callback=progress_callback,
            cancellation_check=cancellation_check,
        )
        workload = self.workload_estimate(
            request,
            cancellation_check=cancellation_check,
        )
        cancelled = (
            readiness.status is ResultStatus.CANCELLED
            or workload.status is ResultStatus.CANCELLED
        )
        errors = (*readiness.errors, *workload.errors)
        execution_failed = bool(errors) and not cancelled
        return ResearchPreflightResult(
            capability_id="research-preflight",
            request_id=request.request_id,
            status=(
                ResultStatus.CANCELLED
                if cancelled
                else ResultStatus.FAILED
                if execution_failed
                else ResultStatus.SUCCEEDED
            ),
            exit_code=(
                130
                if cancelled
                else 1
                if execution_failed
                else max(readiness.exit_code, workload.exit_code)
            ),
            content_hash=None,
            artifacts=(*readiness.artifacts, *workload.artifacts),
            warnings=(*readiness.warnings, *workload.warnings),
            errors=errors,
            readiness=readiness,
            workload=workload,
        )

    def readiness(
        self,
        request: ResearchPreflightRequest,
        *,
        progress_callback: ProgressCallback | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> ResearchReadinessResult:
        ensure_capability_authorized("research-readiness", request.actor)
        try:
            self._check_cancelled(cancellation_check)

            def progress(split_name: str, method: str) -> None:
                self._check_cancelled(cancellation_check)
                if progress_callback is not None:
                    progress_callback(
                        {
                            "stage": "readiness_scan",
                            "split": split_name,
                            "method": method,
                        }
                    )

            report = build_research_readiness_report(
                manifest_path=request.manifest_path,
                db_path=self.paths.db_path,
                execution_calibration_path=request.execution_calibration_path,
                progress_callback=progress,
                environment_summary=self.environment_summary,
                strategy_registry=self.strategy_registry,
            )
        except ApplicationCancellation as exc:
            return ResearchReadinessResult(
                capability_id="research-readiness",
                request_id=request.request_id,
                status=ResultStatus.CANCELLED,
                exit_code=130,
                errors=(_error_from_exception(exc),),
            )
        except Exception as exc:
            return ResearchReadinessResult(
                capability_id="research-readiness",
                request_id=request.request_id,
                status=ResultStatus.FAILED,
                exit_code=1,
                errors=(_error_from_exception(exc),),
            )
        passed = report.get("status") == "PASS"
        return ResearchReadinessResult(
            capability_id="research-readiness",
            request_id=request.request_id,
            # A completed readiness gate is an execution success even when the
            # research outcome is FAIL.  ``exit_code`` preserves CLI semantics.
            status=ResultStatus.SUCCEEDED,
            exit_code=0 if passed else 1,
            content_hash=_optional_string(report.get("content_hash")),
            readiness_outcome=_optional_string(report.get("status")),
            report=report,
        )

    def workload_estimate(
        self,
        request: ResearchPreflightRequest,
        *,
        cancellation_check: CancellationCheck | None = None,
    ) -> ResearchWorkloadResult:
        ensure_capability_authorized("research-workload-estimate", request.actor)
        try:
            self._check_cancelled(cancellation_check)
            estimate = build_manifest_workload_estimate_from_path(
                request.manifest_path,
                strategy_registry=self.strategy_registry,
            )
        except ApplicationCancellation as exc:
            return ResearchWorkloadResult(
                capability_id="research-workload-estimate",
                request_id=request.request_id,
                status=ResultStatus.CANCELLED,
                exit_code=130,
                errors=(_error_from_exception(exc),),
            )
        except (ManifestValidationError, OSError, ValueError) as exc:
            return ResearchWorkloadResult(
                capability_id="research-workload-estimate",
                request_id=request.request_id,
                status=ResultStatus.FAILED,
                exit_code=1,
                errors=(_error_from_exception(exc),),
            )
        return ResearchWorkloadResult(
            capability_id="research-workload-estimate",
            request_id=request.request_id,
            status=ResultStatus.SUCCEEDED,
            exit_code=0,
            content_hash=_optional_string(estimate.get("content_hash")),
            estimate=estimate,
        )

    def compare_reports(
        self,
        request: ReportComparisonRequest,
        *,
        report_loader: ReportLoader,
    ) -> ReportComparisonResult:
        """Compare authoritative reports loaded only after authorization.

        The request carries opaque report identities.  A trusted adapter owns
        resolution and supplies the loader, which prevents filesystem paths or
        report payloads from becoming user-controlled application inputs.
        """

        ensure_capability_authorized("research-compare", request.actor)
        try:
            reports_by_id = report_loader(request.report_ids)
            if set(reports_by_id) != set(request.report_ids):
                raise ResearchReportingError(
                    "research_comparison_report_resolution_mismatch"
                )
            reports = [reports_by_id[report_id] for report_id in request.report_ids]
            if any(not isinstance(report, dict) for report in reports):
                raise ResearchReportingError(
                    "research_comparison_report_payload_must_be_object"
                )
            for report_id, report in zip(request.report_ids, reports, strict=True):
                report_hash = str(report.get("content_hash") or "")
                if report_id != "report_" + report_hash.removeprefix("sha256:"):
                    raise ResearchReportingError(
                        "research_comparison_report_identity_mismatch"
                    )
            comparison = compare_research_decision_reports(reports)
            sources = tuple(
                ReportComparisonSource(
                    report_id=report_id,
                    report_hash=str(reports_by_id[report_id]["content_hash"]),
                )
                for report_id in request.report_ids
            )
        except (KeyError, TypeError, ResearchReportingError) as exc:
            return ReportComparisonResult(
                request_id=request.request_id,
                status=ResultStatus.FAILED,
                exit_code=1,
                errors=(_error_from_exception(exc),),
            )
        return ReportComparisonResult(
            request_id=request.request_id,
            status=ResultStatus.SUCCEEDED,
            exit_code=0,
            content_hash=str(comparison["content_hash"]),
            sources=sources,
            comparison=comparison,
        )

    def validate(
        self,
        request: ResearchValidationRequest,
        *,
        progress_callback: ProgressCallback | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> ResearchValidationResult:
        ensure_capability_authorized("research-validate", request.actor)
        command_args = {
            "cmd": "research-validate",
            "manifest": request.manifest_path,
            "execution_calibration": request.execution_calibration_path,
            "candidate_id": request.candidate_id,
            "out": request.out_path,
            "mode": request.mode,
        }
        handle = start_run(
            manager=self.paths,
            command="research-validate",
            command_args=command_args,
        )

        def progress(event: dict[str, Any]) -> None:
            self._check_cancelled(cancellation_check)
            if progress_callback is not None:
                progress_callback(event)

        try:
            self._check_cancelled(cancellation_check)
            manifest = load_manifest_with_registry(
                request.manifest_path,
                registry=self.strategy_registry,
            )
            bind_research_validation_experiment(
                manager=self.paths,
                experiment_id=manifest.experiment_id,
                manifest_hash=manifest.manifest_hash(),
            )
            calibration = (
                load_calibration_artifact(request.execution_calibration_path)
                if request.execution_calibration_path
                else None
            )
            report = run_research_validation(
                manifest=manifest,
                db_path=_required_runtime_db_path(
                    self.paths,
                    manifest,
                ),
                manager=self.paths,
                manifest_path=request.manifest_path,
                mode=request.mode,
                execution_calibration=calibration,
                execution_calibration_path=request.execution_calibration_path,
                candidate_id=request.candidate_id,
                out_path=request.out_path,
                progress_callback=progress,
                strategy_registry=self.strategy_registry,
                run_id=handle.run_id,
            )
        except ApplicationCancellation as exc:
            handle.finish(
                status="ABORTED",
                exit_code=130,
                result_content_hash=None,
                error=exc,
            )
            return ResearchValidationResult(
                capability_id="research-validate",
                request_id=request.request_id,
                status=ResultStatus.CANCELLED,
                exit_code=130,
                run_id=handle.run_id,
                errors=(_error_from_exception(exc),),
            )
        except (
            ManifestValidationError,
            ExecutionCalibrationError,
            ResearchValidationError,
            ValidationRunError,
            OSError,
            ValueError,
        ) as exc:
            handle.finish(
                status="FAILED",
                exit_code=1,
                result_content_hash=None,
                error=exc,
            )
            return ResearchValidationResult(
                capability_id="research-validate",
                request_id=request.request_id,
                status=ResultStatus.FAILED,
                exit_code=1,
                run_id=handle.run_id,
                errors=(_error_from_exception(exc),),
            )
        except KeyboardInterrupt as exc:
            handle.finish(
                status="ABORTED",
                exit_code=130,
                result_content_hash=None,
                error=exc,
            )
            raise
        except BaseException as exc:
            handle.finish(
                status="FAILED",
                exit_code=1,
                result_content_hash=None,
                error=exc,
            )
            raise

        content_hash = _optional_string(report.get("content_hash"))
        outcome = _optional_string(report.get("end_to_end_validation_result"))
        passed = outcome == "PASS"
        handle.finish(
            status="SUCCEEDED" if passed else "FAILED",
            exit_code=0 if passed else 1,
            result_content_hash=content_hash,
        )
        artifacts = _validation_artifact_references(report)
        return ResearchValidationResult(
            capability_id="research-validate",
            request_id=request.request_id,
            # The validation pipeline completed and produced authoritative
            # evidence.  A non-PASS research gate remains distinct via
            # ``research_outcome`` and exit_code, not an execution error.
            status=ResultStatus.SUCCEEDED,
            exit_code=0 if passed else 1,
            run_id=handle.run_id,
            content_hash=content_hash,
            artifacts=artifacts,
            research_outcome=outcome,
            report=report,
        )

    @staticmethod
    def _check_cancelled(cancellation_check: CancellationCheck | None) -> None:
        if cancellation_check is not None and cancellation_check():
            raise ApplicationCancellation()


def _required_runtime_db_path(
    paths: ResearchPathManager,
    manifest: Any,
) -> Path | None:
    registry = default_dataset_adapter_registry()
    adapters: list[tuple[Any, str, str, object | None]] = [
        (
            registry.resolve(manifest.dataset.source),
            manifest.dataset.source,
            "candles",
            None,
        )
    ]
    if manifest.dataset.top_of_book is not None:
        source = manifest.dataset.top_of_book.source
        adapters.append(
            (
                registry.resolve_top_of_book(source),
                source,
                "top_of_book",
                getattr(manifest.dataset.top_of_book, "locator", None),
            )
        )
    timing = getattr(manifest, "execution_timing", None)
    execution_model = getattr(manifest, "execution_model", None)
    depth_needed = (
        manifest.dataset.depth is not None
        or bool(getattr(timing, "depth_required", False))
        or getattr(timing, "min_execution_reality_level_for_validation", None)
        == "l2_depth_walk_no_queue"
        or any(
            getattr(item, "type", None) == "depth_walk"
            for item in getattr(execution_model, "scenarios", ())
        )
    )
    if depth_needed:
        source = (
            manifest.dataset.depth.source
            if manifest.dataset.depth
            else "orderbook_depth_levels"
        )
        adapters.append(
            (
                registry.resolve_depth(source),
                source,
                "depth",
                getattr(manifest.dataset.depth, "locator", None)
                if manifest.dataset.depth
                else None,
            )
        )
    required = next(
        (
            (source, role)
            for adapter, source, role, immutable_locator in adapters
            if bool(getattr(adapter, "requires_runtime_db", False))
            and immutable_locator is None
        ),
        None,
    )
    if required is None:
        return None
    try:
        return paths.require_database_path()
    except (OSError, ValueError) as exc:
        source, role = required
        raise ValueError(
            "runtime_context_missing:"
            f"source={source}:capability=runtime_db:role={role}"
        ) from exc


def _validation_artifact_references(
    report: dict[str, Any],
) -> tuple[ArtifactReference, ...]:
    references: list[ArtifactReference] = []
    for kind, field in (
        ("validation_summary", "validation_run_path"),
        ("research_candidate_report", "research_candidate_report_path"),
        ("selected_candidate", "selected_candidate_path"),
    ):
        uri = _optional_string(report.get(field))
        if uri is not None:
            references.append(
                ArtifactReference(
                    kind=kind,
                    uri=uri,
                    content_hash=(
                        _optional_string(report.get("content_hash"))
                        if kind == "validation_summary"
                        else None
                    ),
                )
            )
    return tuple(references)


def _error_from_exception(exc: BaseException) -> ApplicationError:
    if isinstance(exc, ApplicationCancellation):
        code = exc.code
    elif isinstance(exc, ManifestValidationError):
        code = "manifest_invalid"
    elif isinstance(exc, ExecutionCalibrationError):
        code = "execution_calibration_invalid"
    elif isinstance(exc, ValidationRunError):
        code = "validation_run_failed"
    elif isinstance(exc, ResearchValidationError):
        code = "research_validation_failed"
    elif isinstance(exc, OSError):
        code = "research_io_error"
    elif isinstance(exc, ValueError):
        code = "invalid_research_request"
    else:
        code = "application_execution_failed"
    return ApplicationError(
        code=code,
        message=str(exc) or type(exc).__name__,
        details={"exception_type": type(exc).__name__},
        retryable=isinstance(exc, OSError),
    )


def _optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None
