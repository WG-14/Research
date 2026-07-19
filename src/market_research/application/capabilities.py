"""Central UI-neutral capability catalog for CLI and internal web adapters."""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field

from .contracts import (
    ApplicationRequest,
    ApplicationResult,
    GenericApplicationRequest,
    GenericApplicationResult,
    HumanReviewRequest,
    HumanReviewResult,
    ReadOnlyQueryRequest,
    ReadOnlyQueryResult,
    ReportComparisonRequest,
    ReportComparisonResult,
    ResearchPreflightRequest,
    ResearchPreflightResult,
    ResearchReadinessResult,
    ResearchValidationRequest,
    ResearchValidationResult,
    ResearchWorkloadResult,
    StrategyApprovalRequest,
    StrategyApprovalResult,
)


class CapabilityExecutionMode(str, Enum):
    SYNCHRONOUS = "synchronous"
    QUEUED = "queued"


class CapabilityRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class GuiPolicy(str, Enum):
    REQUIRED = "required"
    ADMIN_ONLY = "admin_only"
    CLI_ONLY = "cli_only"


class CapabilitySpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    capability_id: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    permission: str = Field(min_length=1, max_length=255)
    execution_mode: CapabilityExecutionMode
    risk: CapabilityRisk
    gui_policy: GuiPolicy
    service_id: str = Field(min_length=1, max_length=255)
    cli_command: str | None = None
    request_model: type[ApplicationRequest]
    result_model: type[ApplicationResult]
    reason: str = Field(min_length=1)


def _cli(
    capability_id: str,
    display_name: str,
    permission: str,
    execution_mode: CapabilityExecutionMode,
    risk: CapabilityRisk,
    gui_policy: GuiPolicy,
    reason: str,
    *,
    service_id: str | None = None,
    request_model: type[ApplicationRequest] = GenericApplicationRequest,
    result_model: type[ApplicationResult] = GenericApplicationResult,
) -> CapabilitySpec:
    return CapabilitySpec(
        capability_id=capability_id,
        display_name=display_name,
        permission=permission,
        execution_mode=execution_mode,
        risk=risk,
        gui_policy=gui_policy,
        service_id=service_id or f"legacy_cli_handler:{capability_id}",
        cli_command=capability_id,
        request_model=request_model,
        result_model=result_model,
        reason=reason,
    )


CAPABILITIES: tuple[CapabilitySpec, ...] = (
    _cli(
        "research-backtest",
        "Backtest",
        "research.execute",
        CapabilityExecutionMode.QUEUED,
        CapabilityRisk.MEDIUM,
        GuiPolicy.CLI_ONLY,
        "Standalone backtest remains an expert CLI workflow; the web validation workflow invokes the same backtest engine after mandatory preflight.",
    ),
    _cli(
        "research-walk-forward",
        "Walk-forward study",
        "research.execute",
        CapabilityExecutionMode.QUEUED,
        CapabilityRisk.HIGH,
        GuiPolicy.CLI_ONLY,
        "Standalone walk-forward remains an expert CLI workflow; manifest-required folds run inside the guarded web validation workflow.",
    ),
    _cli(
        "research-validate",
        "End-to-end validation",
        "research.execute",
        CapabilityExecutionMode.QUEUED,
        CapabilityRisk.HIGH,
        GuiPolicy.REQUIRED,
        "Primary fail-closed validation workflow.",
        service_id="ResearchApplicationService.validate",
        request_model=ResearchValidationRequest,
        result_model=ResearchValidationResult,
    ),
    _cli(
        "research-readiness",
        "Readiness check",
        "research.view",
        CapabilityExecutionMode.SYNCHRONOUS,
        CapabilityRisk.LOW,
        GuiPolicy.REQUIRED,
        "Required pre-execution data check.",
        service_id="ResearchApplicationService.readiness",
        request_model=ResearchPreflightRequest,
        result_model=ResearchReadinessResult,
    ),
    _cli(
        "research-freeze-dataset",
        "Freeze dataset",
        "research.dataset.manage",
        CapabilityExecutionMode.QUEUED,
        CapabilityRisk.HIGH,
        GuiPolicy.ADMIN_ONLY,
        "Publishes an immutable dataset from a trusted source.",
    ),
    _cli(
        "research-workload-estimate",
        "Workload estimate",
        "research.view",
        CapabilityExecutionMode.SYNCHRONOUS,
        CapabilityRisk.LOW,
        GuiPolicy.REQUIRED,
        "Required resource estimate before execution.",
        service_id="ResearchApplicationService.workload_estimate",
        request_model=ResearchPreflightRequest,
        result_model=ResearchWorkloadResult,
    ),
    _cli(
        "research-batch",
        "Batch research",
        "research.batch.execute",
        CapabilityExecutionMode.QUEUED,
        CapabilityRisk.HIGH,
        GuiPolicy.CLI_ONLY,
        "Existing batch path is a low-level subprocess orchestration surface.",
    ),
    _cli(
        "research-forward-diagnostics",
        "Forward diagnostics",
        "research.diagnostics.execute",
        CapabilityExecutionMode.QUEUED,
        CapabilityRisk.HIGH,
        GuiPolicy.CLI_ONLY,
        "Advanced diagnostic overrides require a guarded future workflow.",
    ),
    _cli(
        "research-verify-audit",
        "Verify audit evidence",
        "research.audit.verify",
        CapabilityExecutionMode.SYNCHRONOUS,
        CapabilityRisk.MEDIUM,
        GuiPolicy.ADMIN_ONLY,
        "Integrity verification is restricted to reviewers and administrators.",
    ),
    _cli(
        "research-reproduce-run",
        "Reproduce run",
        "research.reproduce",
        CapabilityExecutionMode.QUEUED,
        CapabilityRisk.HIGH,
        GuiPolicy.ADMIN_ONLY,
        "Long-running evidence reproduction requires elevated permission.",
    ),
    _cli(
        "research-registry-inspect",
        "Inspect registry row",
        "research.registry.inspect",
        CapabilityExecutionMode.SYNCHRONOUS,
        CapabilityRisk.MEDIUM,
        GuiPolicy.CLI_ONLY,
        "Low-level hash-row inspection is retained as a CLI diagnostic.",
    ),
    _cli(
        "research-registry-validate",
        "Validate registry",
        "research.registry.validate",
        CapabilityExecutionMode.SYNCHRONOUS,
        CapabilityRisk.MEDIUM,
        GuiPolicy.ADMIN_ONLY,
        "Canonical registry validation is an administrative audit action.",
    ),
    _cli(
        "research-mark-attempt-aborted",
        "Mark attempt aborted",
        "research.registry.recover",
        CapabilityExecutionMode.SYNCHRONOUS,
        CapabilityRisk.CRITICAL,
        GuiPolicy.CLI_ONLY,
        "Break-glass lifecycle repair must remain explicit and CLI-only.",
    ),
    _cli(
        "research-export-strategy-package",
        "Export strategy package",
        "research.package.export",
        CapabilityExecutionMode.SYNCHRONOUS,
        CapabilityRisk.HIGH,
        GuiPolicy.ADMIN_ONLY,
        "Exports approved authoritative research evidence.",
    ),
    _cli(
        "research-compare",
        "Compare reports",
        "research.view",
        CapabilityExecutionMode.SYNCHRONOUS,
        CapabilityRisk.LOW,
        GuiPolicy.REQUIRED,
        "Compare two to ten verified reports selected only by opaque report identity.",
        service_id="ResearchApplicationService.compare_reports",
        request_model=ReportComparisonRequest,
        result_model=ReportComparisonResult,
    ),
    _cli(
        "research-render-report",
        "Render report",
        "research.view",
        CapabilityExecutionMode.SYNCHRONOUS,
        CapabilityRisk.LOW,
        GuiPolicy.CLI_ONLY,
        "The web adapter renders a safe summary, not the CLI report-rendering contract.",
    ),
    _cli(
        "research-governance-transition",
        "Governance transition",
        "research.governance.transition",
        CapabilityExecutionMode.SYNCHRONOUS,
        CapabilityRisk.CRITICAL,
        GuiPolicy.ADMIN_ONLY,
        "Changes authoritative research lifecycle state.",
    ),
    _cli(
        "research-record-human-review",
        "Record human review",
        "research.review.record",
        CapabilityExecutionMode.SYNCHRONOUS,
        CapabilityRisk.HIGH,
        GuiPolicy.ADMIN_ONLY,
        "Creates an authoritative human-review event.",
        service_id="ResearchGovernanceApplicationService.record_review",
        request_model=HumanReviewRequest,
        result_model=HumanReviewResult,
    ),
    _cli(
        "research-approve-strategy-candidate",
        "Approve strategy candidate",
        "research.approve",
        CapabilityExecutionMode.SYNCHRONOUS,
        CapabilityRisk.CRITICAL,
        GuiPolicy.ADMIN_ONLY,
        "Creates a hash-bound research approval.",
        service_id="ResearchGovernanceApplicationService.approve_candidate",
        request_model=StrategyApprovalRequest,
        result_model=StrategyApprovalResult,
    ),
    CapabilitySpec(
        capability_id="research-preflight",
        display_name="Research preflight",
        permission="research.execute",
        execution_mode=CapabilityExecutionMode.QUEUED,
        risk=CapabilityRisk.LOW,
        gui_policy=GuiPolicy.REQUIRED,
        service_id="ResearchApplicationService.preflight",
        cli_command=None,
        request_model=ResearchPreflightRequest,
        result_model=ResearchPreflightResult,
        reason="GUI orchestration of readiness and workload estimation through the shared services.",
    ),
    CapabilitySpec(
        capability_id="research.explore",
        display_name="Explore research evidence",
        permission="research.view",
        execution_mode=CapabilityExecutionMode.SYNCHRONOUS,
        risk=CapabilityRisk.LOW,
        gui_policy=GuiPolicy.REQUIRED,
        service_id="research_exploration.query",
        cli_command=None,
        request_model=ReadOnlyQueryRequest,
        result_model=ReadOnlyQueryResult,
        reason="Bounded, path-free queries over immutable research evidence.",
    ),
    CapabilitySpec(
        capability_id="jobs.list",
        display_name="List jobs",
        permission="research.view",
        execution_mode=CapabilityExecutionMode.SYNCHRONOUS,
        risk=CapabilityRisk.LOW,
        gui_policy=GuiPolicy.REQUIRED,
        service_id="job_query.list",
        cli_command=None,
        request_model=ReadOnlyQueryRequest,
        result_model=ReadOnlyQueryResult,
        reason="Read-only job history query.",
    ),
    CapabilitySpec(
        capability_id="jobs.detail",
        display_name="View job",
        permission="research.view",
        execution_mode=CapabilityExecutionMode.SYNCHRONOUS,
        risk=CapabilityRisk.LOW,
        gui_policy=GuiPolicy.REQUIRED,
        service_id="job_query.detail",
        cli_command=None,
        request_model=ReadOnlyQueryRequest,
        result_model=ReadOnlyQueryResult,
        reason="Read-only job detail query.",
    ),
    CapabilitySpec(
        capability_id="reports.list",
        display_name="List reports",
        permission="research.view",
        execution_mode=CapabilityExecutionMode.SYNCHRONOUS,
        risk=CapabilityRisk.LOW,
        gui_policy=GuiPolicy.REQUIRED,
        service_id="report_query.list",
        cli_command=None,
        request_model=ReadOnlyQueryRequest,
        result_model=ReadOnlyQueryResult,
        reason="Read-only authoritative report query.",
    ),
    CapabilitySpec(
        capability_id="reports.detail",
        display_name="View report",
        permission="research.view",
        execution_mode=CapabilityExecutionMode.SYNCHRONOUS,
        risk=CapabilityRisk.LOW,
        gui_policy=GuiPolicy.REQUIRED,
        service_id="report_query.detail",
        cli_command=None,
        request_model=ReadOnlyQueryRequest,
        result_model=ReadOnlyQueryResult,
        reason="Read-only authoritative report detail query.",
    ),
    CapabilitySpec(
        capability_id="reports.download",
        display_name="Download report",
        permission="research.view",
        execution_mode=CapabilityExecutionMode.SYNCHRONOUS,
        risk=CapabilityRisk.LOW,
        gui_policy=GuiPolicy.REQUIRED,
        service_id="report_query.download",
        cli_command=None,
        request_model=ReadOnlyQueryRequest,
        result_model=ReadOnlyQueryResult,
        reason="Read-only hash-bound report download.",
    ),
)


_CAPABILITY_REGISTRY: Mapping[str, CapabilitySpec] = MappingProxyType(
    {spec.capability_id: spec for spec in CAPABILITIES}
)

if len(_CAPABILITY_REGISTRY) != len(CAPABILITIES):
    raise RuntimeError("duplicate_application_capability_id")


def capability_registry() -> Mapping[str, CapabilitySpec]:
    return _CAPABILITY_REGISTRY


def get_capability(capability_id: str) -> CapabilitySpec:
    try:
        return _CAPABILITY_REGISTRY[capability_id]
    except KeyError as exc:
        raise KeyError(f"unknown_application_capability:{capability_id}") from exc
