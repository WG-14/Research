from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any

from market_research.storage_io import write_json_atomic
from market_research.paths import ResearchPathManager

from .artifact_store import ArtifactBudgetExceeded
from .experiment_manifest import ManifestValidationError, load_manifest, load_manifest_with_registry
from .experiment_registry import (
    VALIDATION_PERMITTED_STATUSES,
    append_attempt_aborted,
    compute_row_hash,
    experiment_registry_chain_reasons,
    experiment_registry_path,
    load_experiment_registry_rows,
    validate_experiment_registry_binding,
)
from .hashing import content_hash_payload, report_content_hash_payload, sha256_prefixed
from .final_selection import (
    validate_confirmation_artifact,
    validate_final_selection_report,
)
from .governance import (
    GovernanceError,
    GovernanceSubject,
    GovernanceSubjectType,
    HumanReviewDecision,
    append_human_review,
    append_lifecycle_transition,
    approve_strategy_candidate,
)
from .reproduction import (
    ReproductionContractError,
    compare_reproduction_fingerprints,
    load_reproduction_receipt,
)
from .audit_trail import validate_audit_trail_binding, verify_audit_trail
from .return_panel import validate_return_panel_binding
from .execution_calibration import ExecutionCalibrationError, load_calibration_artifact
from .research_classification import requires_candidate_validation
from .run_summary import ResearchRunSummary, build_research_run_summary
from .validation_pipeline import (
    ValidationRunError,
    validate_validated_research_result,
    validation_next_action_payload,
)
from .validation_protocol import ResearchValidationError, run_research_backtest, run_research_walk_forward
from .forward_diagnostics_cli import cmd_research_forward_diagnostics
from .batch_runner import run_research_batch
from .datasets.registry import default_dataset_adapter_registry
from .strategy_package import StrategyPackageError
from .research_reporting import ResearchReportingError
from .application import ResearchApplicationService
from market_research.research_composition import builtin_strategy_registry


def _governance_subject(subject_type: str, subject_id: str, subject_version: str) -> GovernanceSubject:
    return GovernanceSubject(
        GovernanceSubjectType(subject_type),
        subject_id,
        subject_version,
    )


def _parse_evidence_assignments(values: tuple[str, ...]) -> dict[str, str]:
    evidence: dict[str, str] = {}
    for value in values:
        key, separator, digest = value.partition("=")
        if not separator or not key.strip() or not digest.strip() or key.strip() in evidence:
            raise GovernanceError("governance_evidence_assignment_invalid")
        evidence[key.strip()] = digest.strip()
    return evidence


def cmd_research_governance_transition(
    *, context: "ResearchAppContext", subject_type: str, subject_id: str,
    subject_version: str, from_state: str | None, to_state: str,
    actor_id: str, reason: str, evidence: tuple[str, ...],
) -> int:
    try:
        row = append_lifecycle_transition(
            manager=context.paths,
            subject=_governance_subject(subject_type, subject_id, subject_version),
            from_state=from_state,
            to_state=to_state,
            actor_id=actor_id,
            reason=reason,
            evidence_hashes=_parse_evidence_assignments(evidence),
        )
    except (GovernanceError, ValueError) as exc:
        context.printer(f"[RESEARCH-GOVERNANCE-TRANSITION] error={exc}")
        return 1
    context.printer(f"[RESEARCH-GOVERNANCE-TRANSITION] row_hash={row['row_hash']}")
    return 0


def cmd_research_record_human_review(
    *, context: "ResearchAppContext", subject_type: str, subject_id: str,
    subject_version: str, decision: str, reviewer_id: str, reviewer_role: str,
    rationale: str, reviewed_artifact_hash: str, requested_changes_path: str | None,
    resolved_requirement_ids: tuple[str, ...],
) -> int:
    try:
        requested: tuple[dict[str, str], ...] = ()
        if requested_changes_path:
            payload = json.loads(Path(requested_changes_path).read_text(encoding="utf-8"))
            if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
                raise GovernanceError("human_review_requested_changes_file_must_be_array")
            requested = tuple(payload)
        row = append_human_review(
            manager=context.paths,
            subject=_governance_subject(subject_type, subject_id, subject_version),
            decision=HumanReviewDecision(decision),
            reviewer_id=reviewer_id,
            reviewer_role=reviewer_role,
            rationale=rationale,
            reviewed_artifact_hash=reviewed_artifact_hash,
            requested_changes=requested,
            resolved_requirement_ids=resolved_requirement_ids,
        )
    except (OSError, json.JSONDecodeError, GovernanceError, ValueError) as exc:
        context.printer(f"[RESEARCH-HUMAN-REVIEW] error={exc}")
        return 1
    context.printer(f"[RESEARCH-HUMAN-REVIEW] row_hash={row['row_hash']}")
    return 0


def cmd_research_approve_strategy_candidate(
    *, context: "ResearchAppContext", result_path: str, subject_version: str,
    reviewer_id: str, rationale: str, resolved_requirement_ids: tuple[str, ...],
    out_path: str,
) -> int:
    try:
        report = json.loads(Path(result_path).read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise GovernanceError("strategy_approval_report_must_be_object")
        recorded_hash = str(report.get("content_hash") or "")
        actual_hash = sha256_prefixed(report_content_hash_payload(report))
        if recorded_hash != actual_hash:
            raise GovernanceError("strategy_approval_source_report_content_hash_mismatch")
        result_reasons = validate_validated_research_result(report)
        if result_reasons:
            raise GovernanceError(
                "strategy_approval_validated_result_invalid:"
                + ",".join(result_reasons)
            )
        selection_reasons = validate_final_selection_report(report)
        if selection_reasons:
            raise GovernanceError(
                "strategy_approval_final_selection_invalid:"
                + ",".join(selection_reasons)
            )
        candidate_id = str(report.get("selected_candidate_id") or "").strip()
        if not candidate_id:
            raise GovernanceError("strategy_approval_selected_candidate_missing")
        confirmation = report.get("final_holdout_confirmation")
        confirmation_hash = (
            str(confirmation.get("content_hash") or "")
            if isinstance(confirmation, dict) else ""
        )
        if not confirmation_hash:
            raise GovernanceError("strategy_approval_final_holdout_confirmation_missing")
        selection_artifact = report.get("selection_artifact")
        if not isinstance(selection_artifact, dict):
            raise GovernanceError("strategy_approval_selection_artifact_missing")
        confirmation_reasons = validate_confirmation_artifact(
            confirmation,
            selection_artifact=selection_artifact,
        )
        confirmation_reasons.extend(
            validate_experiment_registry_binding(
                report=confirmation,
                require_complete=True,
                expected_registry_path=experiment_registry_path(
                    manager=context.paths
                ),
            )
        )
        if confirmation.get("confirmation_gate_result") != "PASS":
            confirmation_reasons.append(
                "final_holdout_confirmation_not_passed"
            )
        if confirmation_reasons:
            raise GovernanceError(
                "strategy_approval_final_holdout_invalid:"
                + ",".join(sorted(set(confirmation_reasons)))
            )
        hypothesis_id = str(report.get("hypothesis_id") or "").strip()
        hypothesis_version = str(report.get("hypothesis_version") or "").strip()
        hypothesis_contract_hash = str(report.get("hypothesis_contract_hash") or "").strip()
        if not hypothesis_id or not hypothesis_version or not hypothesis_contract_hash:
            raise GovernanceError("strategy_approval_hypothesis_identity_missing")
        candidates = [item for item in report.get("candidates") or [] if isinstance(item, dict)]
        selected = next(
            (item for item in candidates if str(item.get("parameter_candidate_id") or item.get("candidate_id") or "") == candidate_id),
            None,
        )
        compiled = selected.get("compiled_strategy_contract") if isinstance(selected, dict) else None
        if not isinstance(selected, dict) or not isinstance(compiled, dict):
            raise GovernanceError("strategy_approval_selected_candidate_contract_missing")
        target = Path(out_path).expanduser().resolve()
        if ResearchPathManager.is_within(target, context.paths.project_root):
            raise GovernanceError("strategy_approval_output_must_be_repository_external")
        approval = approve_strategy_candidate(
            manager=context.paths,
            subject=_governance_subject("strategy_candidate", candidate_id, subject_version),
            hypothesis_subject=_governance_subject("hypothesis", hypothesis_id, hypothesis_version),
            hypothesis_contract_hash=hypothesis_contract_hash,
            strategy_name=str(compiled.get("strategy_name") or ""),
            strategy_version=str(compiled.get("strategy_version") or ""),
            strategy_plugin_contract_hash=str(selected.get("strategy_plugin_contract_hash") or ""),
            effective_strategy_parameters_hash=str(selected.get("effective_strategy_parameters_hash") or ""),
            source_report_hash=recorded_hash,
            final_holdout_confirmation_hash=confirmation_hash,
            reviewer_id=reviewer_id,
            rationale=rationale,
            resolved_requirement_ids=resolved_requirement_ids,
        )
        write_json_atomic(target, approval)
    except (OSError, json.JSONDecodeError, GovernanceError, ValueError) as exc:
        context.printer(f"[RESEARCH-APPROVE-STRATEGY-CANDIDATE] error={exc}")
        return 1
    context.printer(
        f"[RESEARCH-APPROVE-STRATEGY-CANDIDATE] content_hash={approval['content_hash']}"
    )
    return 0


def _required_runtime_db_path(context: "ResearchAppContext", manifest: Any, *, registry: Any | None = None) -> Path | None:
    """Resolve runtime database capability from registered selected adapters.

    The error names the source and evidence role so a manifest author can
    distinguish candle, top-of-book, and implicit depth dependencies.
    """
    registry = registry or default_dataset_adapter_registry()
    adapters: list[tuple[Any, str, str, object | None]] = [
        (registry.resolve(manifest.dataset.source), manifest.dataset.source, "candles", None)
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
        or getattr(timing, "min_execution_reality_level_for_validation", None) == "l2_depth_walk_no_queue"
        or any(getattr(item, "type", None) == "depth_walk" for item in getattr(execution_model, "scenarios", ()))
    )
    if depth_needed:
        source = manifest.dataset.depth.source if manifest.dataset.depth else "orderbook_depth_levels"
        adapters.append(
            (
                registry.resolve_depth(source),
                source,
                "depth",
                getattr(manifest.dataset.depth, "locator", None) if manifest.dataset.depth else None,
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
    if required is not None:
        try:
            return context.paths.require_database_path()
        except (OSError, ValueError) as exc:
            source, role = required
            raise ValueError(
                "runtime_context_missing:"
                f"source={source}:capability=runtime_db:role={role}"
            ) from exc
    return None

if TYPE_CHECKING:
    from market_research.research_cli.context import ResearchAppContext


def _print_research_command_finished(
    context: "ResearchAppContext",
    command: str,
    started_at: float,
    rc: int,
    **fields: object,
) -> None:
    status = "success" if rc == 0 else "failure"
    payload: dict[str, object] = {
        "command": command,
        "status": status,
        "exit_code": rc,
        "elapsed_sec": round(monotonic() - started_at, 3),
        **fields,
    }
    context.printer(f"[RESEARCH-COMMAND-FINISHED] {json.dumps(payload, sort_keys=True, default=str)}")


def cmd_research_export_strategy_package(
    *, context: "ResearchAppContext", result_path: str, approval_path: str, out_path: str
) -> int:
    """Export an offline immutable review package from a selected result JSON."""
    try:
        result = json.loads(Path(result_path).read_text(encoding="utf-8"))
        approval = json.loads(Path(approval_path).read_text(encoding="utf-8"))
        package = ResearchApplicationService(
            context.paths, builtin_strategy_registry()
        ).export_strategy_package(report=result, approval=approval, out_path=out_path)
    except (OSError, ValueError, StrategyPackageError) as exc:
        context.printer(f"[RESEARCH-EXPORT-STRATEGY-PACKAGE] error={exc}")
        return 1
    context.printer(f"[RESEARCH-EXPORT-STRATEGY-PACKAGE] content_hash={package['content_hash']}")
    return 0


def cmd_research_compare(
    *, context: "ResearchAppContext", report_paths: tuple[str, ...], out_path: str,
) -> int:
    try:
        reports = [json.loads(Path(path).read_text(encoding="utf-8")) for path in report_paths]
        comparison = ResearchApplicationService(
            context.paths, builtin_strategy_registry()
        ).compare_reports(reports=reports, out_path=out_path)
    except (OSError, json.JSONDecodeError, ResearchReportingError, ValueError) as exc:
        context.printer(f"[RESEARCH-COMPARE] error={exc}")
        return 1
    context.printer(f"[RESEARCH-COMPARE] content_hash={comparison['content_hash']}")
    return 0


def cmd_research_render_report(
    *, context: "ResearchAppContext", report_path: str, out_path: str,
) -> int:
    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        rendered = ResearchApplicationService(
            context.paths, builtin_strategy_registry()
        ).render_report(report=report, out_path=out_path)
    except (OSError, json.JSONDecodeError, ResearchReportingError, ValueError) as exc:
        context.printer(f"[RESEARCH-RENDER-REPORT] error={exc}")
        return 1
    context.printer(f"[RESEARCH-RENDER-REPORT] source_hash={report['content_hash']}")
    return 0


def cmd_research_backtest(
    *,
    context: "ResearchAppContext",
    manifest_path: str,
    execution_calibration_path: str | None = None,
    diagnostic_mode: str | None = None,
) -> int:
    started_at = monotonic()
    rc = 1
    try:
        try:
            strategy_registry = builtin_strategy_registry()
            manifest = load_manifest_with_registry(manifest_path, registry=strategy_registry)
            if diagnostic_mode is not None:
                manifest = replace(
                    manifest,
                    research_run=replace(manifest.research_run, diagnostic_mode=diagnostic_mode),
                )
            calibration = load_calibration_artifact(execution_calibration_path) if execution_calibration_path else None
            report = run_research_backtest(
                manifest=manifest,
                db_path=_required_runtime_db_path(context, manifest),
                manager=context.paths,
                execution_calibration=calibration,
                manifest_path=manifest_path,
                command_args={
                    "manifest": manifest_path,
                    "execution_calibration": execution_calibration_path,
                    "diagnostic_mode": diagnostic_mode,
                    "run_id": context.run_id,
                },
                progress_callback=_print_research_backtest_progress,
                strategy_registry=strategy_registry,
            )
        except ArtifactBudgetExceeded as exc:
            payload = _write_artifact_budget_failure_payload(
                manager=context.paths,
                manifest_path=manifest_path,
                exc=exc,
                run_id=context.run_id,
            )
            context.printer(f"[RESEARCH-BACKTEST] artifact_budget_failure={json.dumps(payload, sort_keys=True)}")
            rc = 1
        except (ManifestValidationError, ExecutionCalibrationError, ResearchValidationError, OSError, ValueError) as exc:
            context.printer(f"[RESEARCH-BACKTEST] error={exc}")
            rc = 1
        else:
            context.run_result_hash = str(report.get("content_hash") or "") or None
            _print_report_summary("RESEARCH-BACKTEST", report)
            if _standalone_report_is_non_validation_eligible_validation_diagnostic(report):
                rc = 1
            else:
                rc = 0
    finally:
        _print_research_command_finished(
            context,
            "research-backtest",
            started_at,
            rc,
            manifest=manifest_path,
            execution_calibration=execution_calibration_path,
            diagnostic_mode=diagnostic_mode,
        )
    return rc


def cmd_research_walk_forward(
    *,
    context: "ResearchAppContext",
    manifest_path: str,
    execution_calibration_path: str | None = None,
) -> int:
    started_at = monotonic()
    rc = 1
    try:
        try:
            strategy_registry = builtin_strategy_registry()
            manifest = load_manifest_with_registry(manifest_path, registry=strategy_registry)
            calibration = load_calibration_artifact(execution_calibration_path) if execution_calibration_path else None
            report = run_research_walk_forward(
                manifest=manifest,
                db_path=_required_runtime_db_path(context, manifest),
                manager=context.paths,
                execution_calibration=calibration,
                manifest_path=manifest_path,
                command_args={
                    "manifest": manifest_path,
                    "execution_calibration": execution_calibration_path,
                    "run_id": context.run_id,
                },
                progress_callback=_print_research_walk_forward_progress,
                strategy_registry=strategy_registry,
            )
        except ArtifactBudgetExceeded as exc:
            payload = _write_artifact_budget_failure_payload(
                manager=context.paths,
                manifest_path=manifest_path,
                exc=exc,
                run_id=context.run_id,
            )
            context.printer(f"[RESEARCH-WALK-FORWARD] artifact_budget_failure={json.dumps(payload, sort_keys=True)}")
            rc = 1
        except (ManifestValidationError, ExecutionCalibrationError, ResearchValidationError, OSError, ValueError) as exc:
            context.printer(f"[RESEARCH-WALK-FORWARD] error={exc}")
            rc = 1
        else:
            context.run_result_hash = str(report.get("content_hash") or "") or None
            _print_report_summary("RESEARCH-WALK-FORWARD", report)
            if _standalone_report_is_non_validation_eligible_validation_diagnostic(report):
                rc = 1
            else:
                rc = 0
    finally:
        _print_research_command_finished(
            context,
            "research-walk-forward",
            started_at,
            rc,
            manifest=manifest_path,
            execution_calibration=execution_calibration_path,
        )
    return rc


def cmd_research_workload_estimate(*, context: "ResearchAppContext", manifest_path: str, as_json: bool = False) -> int:
    try:
        from .workload_estimate import build_manifest_workload_estimate_from_path

        strategy_registry = builtin_strategy_registry()
        payload = build_manifest_workload_estimate_from_path(
            manifest_path, strategy_registry=strategy_registry
        )
    except (ManifestValidationError, OSError, ValueError) as exc:
        context.printer(f"[RESEARCH-WORKLOAD-ESTIMATE] error={exc}")
        return 1
    if as_json:
        context.printer(json.dumps(payload, sort_keys=True, indent=2))
        return 0
    context.printer(
        "[RESEARCH-WORKLOAD-ESTIMATE] "
        f"experiment_id={payload['experiment_id']} "
        f"candidate_count={payload['candidate_count']} "
        f"scenario_count={payload['scenario_count']} "
        f"split_count={payload['split_count']} "
        f"work_unit_count={payload['work_unit_count']} "
        f"available_parallel_work_tasks={payload.get('available_parallel_work_tasks')} "
        f"pre_parallel_dataset_hash_call_count={payload['pre_parallel_dataset_hash_call_count']}"
    )
    return 0


def cmd_research_batch(
    *,
    context: "ResearchAppContext",
    manifest_glob: str,
    max_concurrent_manifests: int,
    command: str = "research-backtest",
    fail_fast: bool = False,
    out_path: str | None = None,
) -> int:
    try:
        result = run_research_batch(
            manifest_glob=manifest_glob,
            max_concurrent_manifests=max_concurrent_manifests,
            command=command,
            fail_fast=fail_fast,
            out_path=out_path,
            manager=context.paths,
            project_root=context.paths.project_root,
            strategy_registry=builtin_strategy_registry(),
        )
    except (OSError, ValueError) as exc:
        context.printer(f"[RESEARCH-BATCH] error={exc}")
        return 1
    context.printer(f"[RESEARCH-BATCH] summary={result.summary_path} status={result.payload['status']}")
    return 0 if result.payload["status"] == "succeeded" or not fail_fast else 1


def _write_artifact_budget_failure_payload(
    *,
    manager: Any,
    manifest_path: str,
    exc: ArtifactBudgetExceeded,
    run_id: str | None = None,
) -> dict[str, object]:
    try:
        strategy_registry = builtin_strategy_registry()
        manifest = load_manifest_with_registry(manifest_path, registry=strategy_registry)
        experiment_id = manifest.experiment_id
    except Exception:
        experiment_id = "unknown"
    payload: dict[str, object] = {
        "schema_version": 1,
        "status": "ARTIFACT_BUDGET_EXCEEDED",
        "run_id": run_id,
        **exc.as_dict(),
    }
    path = manager.report_path("research", experiment_id, "artifact_budget_failure.json")
    payload["failure_artifact_path"] = str(path.resolve())
    payload["failure_artifact_ref"] = path.resolve().relative_to(manager.data_dir().resolve()).as_posix()
    payload["content_hash"] = sha256_prefixed(
        content_hash_payload(payload), label="artifact_budget_failure"
    )
    write_json_atomic(path, payload)
    return payload


def cmd_research_validate(
    *,
    context: "ResearchAppContext",
    manifest_path: str,
    execution_calibration_path: str | None = None,
    candidate_id: str | None = None,
    out_path: str | None = None,
    mode: str = "strict",
) -> int:
    started_at = monotonic()
    rc = 1
    try:
        try:
            strategy_registry = builtin_strategy_registry()
            manifest = load_manifest_with_registry(manifest_path, registry=strategy_registry)
            calibration = load_calibration_artifact(execution_calibration_path) if execution_calibration_path else None
            validation_run = ResearchApplicationService(
                context.paths, strategy_registry
            ).validate(
                manifest=manifest,
                db_path=_required_runtime_db_path(context, manifest),
                manifest_path=manifest_path,
                mode=mode,
                execution_calibration=calibration,
                execution_calibration_path=execution_calibration_path,
                candidate_id=candidate_id,
                out_path=out_path,
                progress_callback=_print_research_backtest_progress,
                run_id=context.run_id,
                record_lifecycle=False,
            )
        except (
            ManifestValidationError,
            ExecutionCalibrationError,
            ResearchValidationError,
            ValidationRunError,
            OSError,
            ValueError,
        ) as exc:
            context.printer(f"[RESEARCH-VALIDATE] error={exc}")
            rc = 1
        else:
            context.run_result_hash = str(validation_run.get("content_hash") or "") or None
            _print_validation_run_summary(validation_run)
            rc = 0 if validation_run.get("end_to_end_validation_result") == "PASS" else 1
    finally:
        _print_research_command_finished(
            context,
            "research-validate",
            started_at,
            rc,
            manifest=manifest_path,
            execution_calibration=execution_calibration_path,
            candidate_id=candidate_id,
            out=out_path,
            mode=mode,
        )
    return rc


def cmd_research_reproduce_run(
    *,
    context: "ResearchAppContext",
    manifest_path: str,
    receipt_path: str,
    out_path: str | None = None,
) -> int:
    """Reproduce a run in three explicit, fail-closed phases."""

    started_at = monotonic()
    result_path: Path | None = None
    status = "REPRODUCTION_FAILED"
    payload: dict[str, object]
    manifest = None
    receipt: dict[str, object]
    manifest_display_path = str(Path(manifest_path).expanduser())
    baseline_display_path = str(Path(receipt_path).expanduser())
    try:
        # A. Baseline preflight.  No dataset access or backtest work is allowed
        # until the supplied receipt is itself a valid baseline.
        try:
            strategy_registry = builtin_strategy_registry()
            manifest = load_manifest_with_registry(manifest_path, registry=strategy_registry)
            receipt = load_reproduction_receipt(receipt_path)
            if receipt["manifest_hash"] != manifest.manifest_hash():
                raise ReproductionContractError("receipt manifest_hash does not match manifest")
            if receipt["experiment_id"] != manifest.experiment_id:
                raise ReproductionContractError("receipt experiment_id does not match manifest")
        except (ManifestValidationError, ReproductionContractError, OSError, ValueError) as exc:
            status = "INVALID_BASELINE"
            payload = _reproduction_error_payload(
                status=status,
                phase="baseline_preflight",
                error_code=_baseline_preflight_error_code(exc),
                error=exc,
                manifest_path=manifest_display_path,
                baseline_receipt_path=baseline_display_path,
                experiment_id=manifest.experiment_id if manifest is not None else None,
            )
            result_path = _write_reproduction_result(
                context=context, out_path=out_path, payload=payload, experiment_id="invalid", prefix="baseline"
            )
            context.printer(f"[RESEARCH-REPRODUCE-RUN] error={exc}")
            return 1

        # B. Isolated reproduction execution.  Contract failures here belong
        # to the newly generated output, never to the already checked baseline.
        receipt_hash = str(receipt["receipt_content_hash"])
        prefix = receipt_hash.removeprefix("sha256:")[:12]
        try:
            db_path = _required_runtime_db_path(context, manifest)
            if db_path is not None and not db_path.is_file():
                raise OSError(f"dataset locator is not accessible: {db_path}")
            isolated_settings = replace(
                context.settings,
                artifact_root=context.settings.artifact_root / "reproductions" / manifest.experiment_id / prefix,
                report_root=context.settings.report_root / "reproductions" / manifest.experiment_id / prefix,
                cache_root=context.settings.cache_root / "reproductions" / manifest.experiment_id / prefix,
            )
            isolated_paths = type(context.paths).from_settings(
                isolated_settings, project_root=context.paths.project_root
            )
            stable_fingerprint = receipt["stable_fingerprint"]
            if not isinstance(stable_fingerprint, dict):
                raise ReproductionContractError("receipt.stable_fingerprint is required")
            source_report_kind = str(stable_fingerprint["report_kind"])
            runner = (
                run_research_walk_forward
                if source_report_kind == "walk_forward"
                else run_research_backtest
            )
            progress_callback = (
                _print_research_walk_forward_progress
                if source_report_kind == "walk_forward"
                else _print_research_backtest_progress
            )
            reproduced_report = runner(
                manifest=manifest,
                db_path=db_path,
                manager=isolated_paths,
                manifest_path=manifest_path,
                command_args={
                    "manifest": manifest_path,
                    "receipt": receipt_path,
                    "reproduction": True,
                    "source_report_kind": source_report_kind,
                },
                progress_callback=progress_callback,
                strategy_registry=strategy_registry,
                governance_authority_manager=context.paths,
            )
            reproduced_receipt_path = Path(str(reproduced_report["reproduction_receipt_path"]))
            try:
                reproduced_receipt = load_reproduction_receipt(reproduced_receipt_path)
            except ReproductionContractError as exc:
                raise _ReproductionExecutionError("reproduced_receipt_invalid", exc) from exc
        except _ReproductionExecutionError as wrapped:
            status = "REPRODUCTION_FAILED"
            payload = _reproduction_error_payload(
                status=status, phase="reproduction_execution", error_code=wrapped.error_code, error=wrapped.__cause__ or wrapped,
                manifest_path=manifest_display_path, baseline_receipt_path=baseline_display_path,
                experiment_id=manifest.experiment_id,
            )
            result_path = _write_reproduction_result(
                context=context, out_path=out_path, payload=payload, experiment_id=manifest.experiment_id, prefix=prefix
            )
            context.printer(f"[RESEARCH-REPRODUCE-RUN] error={wrapped.__cause__ or wrapped}")
            return 1
        except (ResearchValidationError, ReproductionContractError, OSError, ValueError, KeyError) as exc:
            status = "REPRODUCTION_FAILED"
            payload = _reproduction_error_payload(
                status=status, phase="reproduction_execution", error_code=_reproduction_execution_error_code(exc), error=exc,
                manifest_path=manifest_display_path, baseline_receipt_path=baseline_display_path,
                experiment_id=manifest.experiment_id,
            )
            result_path = _write_reproduction_result(
                context=context, out_path=out_path, payload=payload, experiment_id=manifest.experiment_id, prefix=prefix
            )
            context.printer(f"[RESEARCH-REPRODUCE-RUN] error={exc}")
            return 1

        # C. Fingerprint comparison is the only phase that can report DRIFT.
        comparison = compare_reproduction_fingerprints(
            receipt["stable_fingerprint"], reproduced_receipt["stable_fingerprint"]
        )
        status = comparison.status
        report_path = Path(str((reproduced_report.get("artifact_paths") or {}).get("report_path") or ""))
        payload = {
            "schema_version": 1,
            "status": status,
            "experiment_id": manifest.experiment_id,
            "manifest_path": manifest_display_path,
            "manifest_hash": manifest.manifest_hash(),
            "baseline_receipt_path": baseline_display_path,
            "phase": "fingerprint_comparison",
            "error_code": None,
            "error": None,
            **comparison.as_dict(),
            "reproduced_report_path": str(report_path.resolve()),
            "reproduced_receipt_path": str(reproduced_receipt_path.resolve()),
        }
        result_path = _write_reproduction_result(
            context=context, out_path=out_path, payload=payload, experiment_id=manifest.experiment_id, prefix=prefix
        )
        if result_path is None:
            status = "REPRODUCTION_FAILED"
            payload = _reproduction_error_payload(
                status=status,
                phase="reproduction_execution",
                error_code="isolated_artifact_write_failed",
                error=OSError("unable to write reproduction report"),
                manifest_path=manifest_display_path,
                baseline_receipt_path=baseline_display_path,
                experiment_id=manifest.experiment_id,
            )
            return 1
        return 0 if status == "PASS" else 1
    finally:
        if result_path is not None:
            context.printer(json.dumps(payload, sort_keys=True))
        _print_research_command_finished(
            context, "research-reproduce-run", started_at, 0 if status == "PASS" else 1,
            manifest=manifest_path, receipt=receipt_path, out=str(result_path) if result_path else out_path, status=status,
        )


class _ReproductionExecutionError(Exception):
    def __init__(self, error_code: str, cause: Exception) -> None:
        super().__init__(str(cause))
        self.error_code = error_code


def _reproduction_error_payload(
    *, status: str, phase: str, error_code: str, error: Exception,
    manifest_path: str, baseline_receipt_path: str, experiment_id: str | None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": status,
        "experiment_id": experiment_id,
        "manifest_path": manifest_path,
        "baseline_receipt_path": baseline_receipt_path,
        "phase": phase,
        "error_code": error_code,
        "error": str(error),
        "mismatches": [],
    }


def _baseline_preflight_error_code(exc: Exception) -> str:
    message = str(exc)
    if "manifest_hash does not match" in message:
        return "baseline_manifest_mismatch"
    if "experiment_id does not match" in message:
        return "baseline_experiment_mismatch"
    if isinstance(exc, ManifestValidationError):
        return "manifest_invalid"
    return "baseline_receipt_invalid"


def _reproduction_execution_error_code(exc: Exception) -> str:
    if isinstance(exc, OSError) and "dataset locator" in str(exc):
        return "dataset_access_failed"
    if isinstance(exc, OSError):
        return "isolated_artifact_write_failed"
    return "backtest_failed"


def _write_reproduction_result(
    *, context: "ResearchAppContext", out_path: str | None, payload: dict[str, object], experiment_id: str, prefix: str,
) -> Path | None:
    try:
        path = (
            _require_external_absolute_output_path(context, out_path)
            if out_path is not None
            else context.paths.report_path("reproductions", experiment_id, prefix, "reproduction_report.json")
        )
        write_json_atomic(path, payload)
        return path
    except (OSError, ValueError):
        return None


def _require_external_absolute_output_path(context: "ResearchAppContext", out_path: str) -> Path:
    path = Path(out_path).expanduser()
    if not path.is_absolute():
        raise ValueError("--out must be an absolute repository-external path")
    resolved = path.resolve()
    if context.paths.is_within(resolved, context.paths.project_root):
        raise ValueError("--out must be outside the repository")
    return resolved


def cmd_research_registry_inspect(*, context: "ResearchAppContext", row_hash: str) -> int:
    path = experiment_registry_path(manager=context.paths)
    rows = load_experiment_registry_rows(path)
    row = next((item for item in rows if item.get("row_hash") == row_hash), None)
    if not isinstance(row, dict):
        context.printer(json.dumps({"ok": False, "reason": "experiment_registry_row_hash_mismatch", "row_hash": row_hash}, sort_keys=True, indent=2))
        return 1
    completion = next(
        (
            item
            for item in reversed(rows)
            if item.get("event_type") in {"research_attempt_completed", "research_attempt_aborted"}
            and item.get("reservation_row_hash") == row_hash
        ),
        None,
    )
    summary = {
        "ok": True,
        "registry_path": str(path.resolve()),
        "row": row,
        "completion_or_abort": completion,
        "attempt_status": completion.get("result_status") if isinstance(completion, dict) else row.get("result_status"),
        "incomplete": completion is None and row.get("event_type") == "research_attempt_reserved",
    }
    context.printer(json.dumps(summary, sort_keys=True, indent=2))
    return 0


def cmd_research_registry_validate(*, context: "ResearchAppContext", experiment_id: str) -> int:
    path = experiment_registry_path(manager=context.paths)
    rows = load_experiment_registry_rows(path)
    registry_chain_reasons = experiment_registry_chain_reasons(rows)
    reservations = [
        item
        for item in rows
        if item.get("event_type") == "research_attempt_reserved" and item.get("experiment_id") == experiment_id
    ]
    if not reservations:
        context.printer(json.dumps({
            "ok": False,
            "validation_scope": "registry_only",
            "reason": "experiment_registry_row_hash_mismatch",
            "experiment_id": experiment_id,
            "artifact_binding_valid": "unknown",
            "report_loaded": False,
            "evidence_loaded": False,
            "return_panel_loaded": False,
            "warning": "artifact_binding_not_checked",
        }, sort_keys=True, indent=2))
        return 1
    ok = not registry_chain_reasons
    report_candidates = [
        (
            report_kind,
            context.paths.report_path(
                "research", experiment_id, f"{report_kind}_report.json"
            ),
        )
        for report_kind in ("backtest", "walk_forward")
    ]
    report_loads = [
        (
            report_kind,
            candidate_path,
            *_load_json_artifact(
                candidate_path,
                label=f"{report_kind}_report",
            ),
        )
        for report_kind, candidate_path in report_candidates
    ]
    loaded_reports = [
        (report_kind, candidate_path, payload)
        for report_kind, candidate_path, payload, _error in report_loads
        if isinstance(payload, dict)
    ]
    artifact_load_errors = [
        error
        for _report_kind, _candidate_path, _payload, error in report_loads
        if error is not None
    ]
    report_resolution_error = len(loaded_reports) > 1
    if len(loaded_reports) == 1:
        report_kind, report_path, report = loaded_reports[0]
    else:
        report_kind = None
        report_path = report_candidates[0][1]
        report = None
    confirmation_path = context.paths.report_path(
        "research", experiment_id, "final_holdout_confirmation.json"
    )
    confirmation, confirmation_load_error = _load_json_artifact(
        confirmation_path,
        label="final_holdout_confirmation",
    )
    evidence_path = context.paths.research_artifact_path(experiment_id, "statistical_selection_evidence.json")
    panel_path = context.paths.research_artifact_path(experiment_id, "candidate_return_panel.json")
    evidence, evidence_load_error = _load_json_artifact(
        evidence_path,
        label="statistical_evidence",
    )
    panel, panel_load_error = _load_json_artifact(
        panel_path,
        label="return_panel",
    )
    artifact_load_errors.extend(
        error
        for error in (
            confirmation_load_error,
            evidence_load_error,
            panel_load_error,
        )
        if error is not None
    )
    artifact_reasons: list[str] = [
        *artifact_load_errors,
        *registry_chain_reasons,
    ]
    if report_resolution_error:
        artifact_reasons.append("multiple_research_reports_found")
        ok = False
    validation_scope = (
        "registry_and_artifacts"
        if (
            isinstance(report, dict)
            or isinstance(confirmation, dict)
            or report_resolution_error
            or artifact_load_errors
        )
        else "registry_only"
    )
    report_loaded = isinstance(report, dict)
    confirmation_loaded = isinstance(confirmation, dict)
    evidence_loaded = isinstance(evidence, dict)
    return_panel_loaded = isinstance(panel, dict)
    artifact_bound_row_hashes: set[str] = set()
    selection_bound_row_hash: str | None = None
    confirmation_bound_row_hash: str | None = None
    artifact_bound_row_hash: str | None = None
    artifact_binding_valid: bool | str = "unknown"
    canonical_registry_path = path.resolve()
    for payload in (report, evidence, confirmation):
        if not isinstance(payload, dict):
            continue
        recorded_registry_path = str(
            payload.get("experiment_registry_path") or ""
        ).strip()
        if recorded_registry_path and Path(recorded_registry_path).expanduser().resolve() != canonical_registry_path:
            artifact_reasons.append("experiment_registry_path_mismatch")
    if (
        report_loaded
        and requires_candidate_validation(report.get("research_classification"))
        and not confirmation_loaded
    ):
        artifact_reasons.append("final_holdout_confirmation_missing")
    if report_loaded:
        evidence_row_hash = str(evidence.get("experiment_registry_row_hash") or "").strip() if isinstance(evidence, dict) else ""
        report_row_hash = str(report.get("experiment_registry_row_hash") or "").strip()
        if evidence_row_hash and report_row_hash and evidence_row_hash != report_row_hash:
            artifact_reasons.append("experiment_registry_report_evidence_row_hash_mismatch")
            artifact_reasons.append("experiment_registry_artifact_bound_row_hash_mismatch")
        selection_bound_row_hash = evidence_row_hash or report_row_hash or None
        if selection_bound_row_hash:
            artifact_bound_row_hashes.add(selection_bound_row_hash)
        if not selection_bound_row_hash and not confirmation_loaded:
            artifact_reasons.append("experiment_registry_row_hash_missing")
        elif selection_bound_row_hash and not any(
            row.get("row_hash") == selection_bound_row_hash for row in reservations
        ):
            artifact_reasons.append("experiment_registry_artifact_bound_row_missing")
        artifact_reasons.extend(
            _content_hash_reasons(
                report,
                report_hash=True,
                label=f"{report_kind}_report",
            )
        )
        artifact_reasons.extend(validate_final_selection_report(report))
        evidence_required = bool(report.get("statistical_validation_required")) or bool(report.get("statistical_evidence_hash"))
        if evidence_required and not evidence_loaded:
            artifact_reasons.append("statistical_evidence_missing")
        if evidence_loaded:
            artifact_reasons.extend(_content_hash_reasons(evidence, report_hash=False, label="statistical_evidence"))
            artifact_reasons.extend(validate_return_panel_binding(report=report, evidence=evidence, panel=panel))
        artifact_reasons.extend(validate_audit_trail_binding(report=report, manager=context.paths))
    elif not confirmation_loaded:
        artifact_reasons.append("artifact_binding_not_checked")
    if confirmation_loaded:
        confirmation_bound_row_hash = str(
            confirmation.get("experiment_registry_row_hash") or ""
        ).strip() or None
        authorization_row_hash = str(
            confirmation.get("authorization_row_hash") or ""
        ).strip() or None
        completion_row_hash = str(
            confirmation.get("experiment_registry_completion_row_hash") or ""
        ).strip() or None
        legacy_completion_row_hash = str(
            confirmation.get("completion_row_hash") or ""
        ).strip() or None
        if (
            confirmation_bound_row_hash
            and authorization_row_hash
            and confirmation_bound_row_hash != authorization_row_hash
        ):
            artifact_reasons.append("final_holdout_confirmation_authorization_row_hash_mismatch")
        if (
            completion_row_hash
            and legacy_completion_row_hash
            and completion_row_hash != legacy_completion_row_hash
        ):
            artifact_reasons.append("final_holdout_confirmation_completion_row_hash_mismatch")
        if not confirmation_bound_row_hash:
            artifact_reasons.append("final_holdout_confirmation_registry_row_hash_missing")
        elif not any(
            row.get("row_hash") == confirmation_bound_row_hash
            for row in reservations
        ):
            artifact_reasons.append("final_holdout_confirmation_registry_row_missing")
        else:
            artifact_bound_row_hashes.add(confirmation_bound_row_hash)
        canonical_completion = (
            _completion_for_row(rows, confirmation_bound_row_hash)
            if confirmation_bound_row_hash
            else None
        )
        if (
            not isinstance(canonical_completion, dict)
            or canonical_completion.get("event_type") != "research_attempt_completed"
            or not completion_row_hash
            or canonical_completion.get("row_hash") != completion_row_hash
        ):
            artifact_reasons.append(
                "final_holdout_confirmation_canonical_completion_row_mismatch"
            )
        selection_artifact = report.get("selection_artifact") if report_loaded else None
        if not isinstance(selection_artifact, dict):
            artifact_reasons.append("final_holdout_confirmation_selection_artifact_missing")
        else:
            if report.get("selection_artifact_hash") != selection_artifact.get(
                "content_hash"
            ):
                artifact_reasons.append("report_selection_artifact_hash_mismatch")
            artifact_reasons.extend(
                validate_confirmation_artifact(
                    confirmation,
                    selection_artifact=selection_artifact,
                )
            )
    artifact_bound_row_hash = confirmation_bound_row_hash or selection_bound_row_hash
    if (
        validation_scope == "registry_and_artifacts"
        and selection_bound_row_hash
        and any(row.get("row_hash") == selection_bound_row_hash for row in reservations)
    ):
        bound_report = dict(report) if isinstance(report, dict) else {}
        bound_completion = _completion_for_row(rows, selection_bound_row_hash)
        if isinstance(bound_completion, dict) and bound_report.get("experiment_registry_completion_row_hash") is None:
            bound_report["experiment_registry_completion_row_hash"] = bound_completion.get("row_hash")
        binding_reasons = validate_experiment_registry_binding(
            report=bound_report,
            evidence=evidence if isinstance(evidence, dict) else None,
            require_complete=True,
        )
        artifact_reasons.extend(binding_reasons)
    if (
        validation_scope == "registry_and_artifacts"
        and confirmation_bound_row_hash
        and any(row.get("row_hash") == confirmation_bound_row_hash for row in reservations)
    ):
        artifact_reasons.extend(
            validate_experiment_registry_binding(
                report=confirmation,
                require_complete=True,
            )
        )
    if validation_scope == "registry_and_artifacts":
        artifact_binding_valid = bool(artifact_bound_row_hashes) and not artifact_reasons
    lifecycle_summary = []
    for row in reservations:
        reservation_hash = str(row.get("row_hash") or "")
        terminal_rows = [
            item
            for item in rows
            if item.get("event_type")
            in {"research_attempt_completed", "research_attempt_aborted"}
            and item.get("reservation_row_hash") == reservation_hash
        ]
        completion = terminal_rows[0] if len(terminal_rows) == 1 else None
        lifecycle = _registry_lifecycle_row(
            row=row,
            completion=completion,
            artifact_bound=reservation_hash in artifact_bound_row_hashes,
        )
        lifecycle["terminal_event_count"] = len(terminal_rows)
        if len(terminal_rows) > 1:
            lifecycle["ok"] = False
            lifecycle["lifecycle_complete"] = False
            lifecycle["validation_permitted"] = False
            lifecycle["reasons"] = sorted(
                set(
                    [str(item) for item in lifecycle["reasons"]]
                    + ["experiment_registry_multiple_terminal_events"]
                )
            )
        lifecycle["report_loaded"] = report_loaded
        lifecycle["final_holdout_confirmation_loaded"] = confirmation_loaded
        lifecycle["evidence_loaded"] = evidence_loaded
        lifecycle["return_panel_loaded"] = return_panel_loaded
        if lifecycle["artifact_bound"]:
            lifecycle["artifact_binding_valid"] = artifact_binding_valid
            lifecycle["reasons"] = sorted(set([str(item) for item in lifecycle["reasons"]] + artifact_reasons))
        lifecycle_summary.append(lifecycle)
        ok = ok and bool(lifecycle["ok"])
    if validation_scope == "registry_and_artifacts":
        ok = ok and artifact_binding_valid is True
    payload = {
        "ok": ok,
        "validation_scope": validation_scope,
        "experiment_id": experiment_id,
        "registry_path": str(path.resolve()),
        "artifact_bound_row_hash": artifact_bound_row_hash,
        "artifact_bound_row_hashes": sorted(artifact_bound_row_hashes),
        "artifact_reasons": sorted(set(artifact_reasons)),
        "report_kind": report_kind,
        "report_path": str(report_path.resolve()),
        "final_holdout_confirmation_path": str(confirmation_path.resolve()),
        "evidence_path": str(evidence_path.resolve()),
        "return_panel_path": str(panel_path.resolve()),
        "report_loaded": report_loaded,
        "final_holdout_confirmation_loaded": confirmation_loaded,
        "evidence_loaded": evidence_loaded,
        "return_panel_loaded": return_panel_loaded,
        "artifact_binding_valid": artifact_binding_valid,
        "warning": "artifact_binding_not_checked" if validation_scope == "registry_only" else None,
        "registry_lifecycle_summary": lifecycle_summary,
        "results": lifecycle_summary,
    }
    context.printer(json.dumps(payload, sort_keys=True, indent=2))
    return 0 if ok else 1


def cmd_research_verify_audit(*, context: "ResearchAppContext", experiment_id: str) -> int:
    result = verify_audit_trail(manager=context.paths, experiment_id=experiment_id)
    context.printer(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result.get("ok") is True else 1


def _completion_for_row(rows: list[dict[str, object]], row_hash: str) -> dict[str, object] | None:
    return next(
        (
            item
            for item in reversed(rows)
            if item.get("event_type") in {"research_attempt_completed", "research_attempt_aborted"}
            and item.get("reservation_row_hash") == row_hash
        ),
        None,
    )


def _registry_lifecycle_row(
    *,
    row: dict[str, object],
    completion: dict[str, object] | None,
    artifact_bound: bool,
) -> dict[str, object]:
    reasons: list[str] = []
    registry_row_valid = compute_row_hash(row) == row.get("row_hash")
    if not registry_row_valid:
        reasons.append("experiment_registry_row_hash_mismatch")
    completion_status = str(completion.get("result_status") or "") if isinstance(completion, dict) else str(row.get("result_status") or "")
    lifecycle_complete = completion_status in VALIDATION_PERMITTED_STATUSES
    incomplete = not lifecycle_complete
    if not lifecycle_complete:
        reasons.append("experiment_registry_incomplete_attempt")
    completion_row_valid = True
    if isinstance(completion, dict):
        completion_row_valid = compute_row_hash(completion) == completion.get("row_hash")
        if not completion_row_valid:
            reasons.append("experiment_registry_row_hash_mismatch")
    row_valid_only = registry_row_valid and not lifecycle_complete
    return {
        "row_hash": row.get("row_hash"),
        "artifact_bound": artifact_bound,
        "event_type": row.get("event_type"),
        "result_status": row.get("result_status"),
        "registry_row_valid": registry_row_valid,
        "completion_row_valid": completion_row_valid,
        "completion_row_hash": completion.get("row_hash") if isinstance(completion, dict) else None,
        "completion_status": completion_status,
        "incomplete": incomplete,
        "lifecycle_complete": lifecycle_complete,
        "validation_permitted": lifecycle_complete,
        "row_valid_only": row_valid_only,
        "artifact_binding_valid": "unknown",
        "report_loaded": False,
        "evidence_loaded": False,
        "return_panel_loaded": False,
        "ok": registry_row_valid and completion_row_valid and lifecycle_complete,
        "reasons": sorted(set(reasons)),
    }


def _load_json_artifact(
    path: Path,
    *,
    label: str,
) -> tuple[dict[str, object] | None, str | None]:
    if not path.exists():
        return None, None
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError:
        return None, f"{label}_json_invalid"
    except OSError:
        return None, f"{label}_read_failed"
    if not isinstance(payload, dict):
        return None, f"{label}_must_be_object"
    return payload, None


def _content_hash_reasons(payload: dict[str, object], *, report_hash: bool, label: str) -> list[str]:
    expected = str(payload.get("content_hash") or "").strip()
    if not expected.startswith("sha256:"):
        return [f"{label}_content_hash_missing"]
    actual = sha256_prefixed(
        report_content_hash_payload(payload) if report_hash else content_hash_payload({k: v for k, v in payload.items() if k != "content_hash"})
    )
    return [] if actual == expected else [f"{label}_content_hash_mismatch"]


def cmd_research_mark_attempt_aborted(*, context: "ResearchAppContext", row_hash: str, reason: str) -> int:
    result = append_attempt_aborted(manager=context.paths, reservation_row_hash=row_hash, reason=reason)
    if result is None:
        context.printer(json.dumps({"ok": False, "reason": "experiment_registry_row_hash_mismatch", "row_hash": row_hash}, sort_keys=True, indent=2))
        return 1
    context.printer(json.dumps({"ok": True, **result}, sort_keys=True, indent=2))
    return 0


def _print_validation_run_summary(payload: dict[str, object]) -> None:
    print("[RESEARCH-VALIDATE]")
    print(f"  validation_run_path={payload.get('validation_run_path')}")
    print(f"  validation_run_hash={payload.get('content_hash')}")
    print(f"  validation_run_binding_hash={payload.get('validation_run_binding_hash')}")
    print(f"  validation_policy_source={payload.get('validation_policy_source') or 'none'}")
    print(
        "  validation_policy_required_stage_names="
        f"{_format_items(tuple(str(item) for item in payload.get('validation_policy_required_stage_names') or []))}"
    )
    print(f"  end_to_end_validation_result={payload.get('end_to_end_validation_result')}")
    print(f"  selected_candidate_id={payload.get('selected_candidate_id') or 'none'}")
    print(f"  backtest_report_hash={payload.get('backtest_report_hash') or 'none'}")
    print(f"  walk_forward_report_hash={payload.get('walk_forward_report_hash') or 'none'}")
    print(f"  validation_artifact_hash={payload.get('validation_artifact_hash') or 'none'}")
    print(f"  reproduce_ok={1 if payload.get('reproduce_ok') else 0}")
    reasons = payload.get("fail_closed_reasons") or []
    print(f"  fail_closed_reasons={_format_items(tuple(str(item) for item in reasons))}")
    print(f"  next_required_action={_validation_next_action(payload)}")
    print(f"  recommended_command={payload.get('recommended_command') or validation_next_action_payload(reasons).get('recommended_command') or 'none'}")


def _standalone_report_is_non_validation_eligible_validation_diagnostic(report: dict[str, object]) -> bool:
    if not requires_candidate_validation(report.get("research_classification")):
        return False
    if report.get("validation_run_complete") is True and report.get("validation_eligibility_gate_result") == "PASS":
        return False
    if report.get("diagnostic_only") is True or report.get("standalone_backtest_not_full_validation") is True:
        return True
    return report.get("validation_eligibility_gate_result") != "PASS"


def _validation_next_action(payload: dict[str, object]) -> str:
    if payload.get("next_required_action"):
        return str(payload.get("next_required_action"))
    if payload.get("end_to_end_validation_result") == "PASS":
        return "review_validation_run_and_validation_artifact"
    reasons = [str(item) for item in payload.get("fail_closed_reasons") or []]
    mapped = validation_next_action_payload(reasons)
    if mapped.get("next_required_action") != "inspect_validation_run_failure_reasons":
        return str(mapped["next_required_action"])
    if any("readiness" in reason for reason in reasons):
        return "fix_data_readiness_then_rerun_research-validate"
    if any("walk_forward" in reason for reason in reasons):
        return "fix_walk_forward_evidence_then_rerun_research-validate"
    if any("reproduce" in reason for reason in reasons):
        return "inspect_validation_reproducibility_then_rerun_research-validate"
    return "inspect_validation_run_failure_reasons"


def _first_participation_summary(report: dict[str, object]) -> dict[str, object]:
    candidates = report.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            participation = candidate.get("participation_summary")
            if isinstance(participation, dict):
                return participation
            for metrics_key in ("validation_metrics_v2", "final_holdout_metrics_v2"):
                metrics = candidate.get(metrics_key)
                if isinstance(metrics, dict) and isinstance(metrics.get("participation"), dict):
                    return metrics["participation"]
    participation = report.get("participation_summary")
    return participation if isinstance(participation, dict) else {}


def _print_report_summary(label: str, report: dict[str, object]) -> None:
    artifact_paths = report.get("artifact_paths") if isinstance(report.get("artifact_paths"), dict) else {}
    summary = build_research_run_summary(report)
    print(f"[{label}]")
    print(f"  experiment_id={report.get('experiment_id')}")
    print(f"  manifest_hash={report.get('manifest_hash')}")
    print(f"  dataset_snapshot_id={report.get('dataset_snapshot_id')}")
    print(f"  dataset_content_hash={report.get('dataset_content_hash')}")
    print(f"  candidates_evaluated={report.get('candidate_count')}")
    print(f"  best_candidate_id={report.get('best_candidate_id') or 'none'}")
    print(f"  final_selection_required={1 if report.get('final_selection_required') else 0}")
    print(f"  final_selection_gate_result={report.get('final_selection_gate_result') or 'none'}")
    print(
        "  final_selection_fail_reasons="
        f"{_format_items(tuple(str(item) for item in report.get('final_selection_fail_reasons') or []))}"
    )
    print(f"  selected_candidate_id={report.get('selected_candidate_id') or 'none'}")
    print(f"  selected_candidate_score_hash={report.get('selected_candidate_score_hash') or 'none'}")
    print(f"  final_selection_contract_hash={report.get('final_selection_contract_hash') or 'none'}")
    print(f"  candidate_final_scores_hash={report.get('candidate_final_scores_hash') or 'none'}")
    _print_final_selection_components(report)
    print(f"  gate_result={report.get('gate_result')}")
    print(f"  validation_eligibility_gate_result={report.get('validation_eligibility_gate_result') or report.get('gate_result')}")
    print(
        "  validation_blocking_reasons="
        f"{_format_items(tuple(str(item) for item in report.get('validation_blocking_reasons') or []))}"
    )
    print(f"  candidate_gate_counts={_format_counts(summary.candidate_gate_counts)}")
    print(f"  candidate_aggregate_gate_counts={_format_counts(summary.candidate_gate_counts)}")
    print(f"  base_gate_counts={_format_counts(summary.base_gate_counts)}")
    print(f"  stress_gate_counts={_format_counts(summary.stress_gate_counts)}")
    print(f"  base_fee_rate={_format_optional(summary.base_fee_rate)}")
    print(
        "  stress_fee_rates="
        f"{','.join(_format_optional(value) for value in summary.stress_fee_rates) if summary.stress_fee_rates else 'none'}"
    )
    print(f"  primary_scenario_role={summary.primary_scenario_role or report.get('primary_metric_scenario_role') or 'none'}")
    print(
        "  primary_metric_source="
        f"{summary.primary_metric_source or report.get('primary_metric_source_semantics') or report.get('primary_metric_source') or 'none'}"
    )
    print(f"  top_fail_reasons={_format_counts(summary.top_fail_reasons)}")
    print(f"  strategy_diagnostics_summary={_format_strategy_diagnostics_summary(summary)}")
    participation_summary = _first_participation_summary(report)
    if participation_summary:
        print(
            "  daily_participation_fallback_counts="
            f"intent:{int(participation_summary.get('fallback_entry_count') or 0)},"
            f"submit_expected:{int(participation_summary.get('fallback_submit_expected_count') or 0)},"
            f"submitted:{int(participation_summary.get('fallback_submitted_count') or 0)},"
            f"filled:{int(participation_summary.get('fallback_filled_count') or 0)},"
            f"closed:{int(participation_summary.get('fallback_closed_trade_count') or 0)},"
            f"base_sma_buy:{int(participation_summary.get('base_sma_buy_count') or 0)}"
        )
    print(f"  top_exit_reasons={_format_counts(summary.top_exit_reasons)}")
    print(
        "  validation_raw_sell_filter_blocked_while_in_position_count="
        f"{_format_optional(summary.validation_raw_sell_filter_blocked_while_in_position_count)}"
    )
    print(
        "  final_holdout_raw_sell_filter_blocked_while_in_position_count="
        f"{_format_optional(summary.final_holdout_raw_sell_filter_blocked_while_in_position_count)}"
    )
    print(f"  validation_p95_mae_pct={_format_optional(summary.validation_p95_mae_pct)}")
    print(f"  final_holdout_p95_mae_pct={_format_optional(summary.final_holdout_p95_mae_pct)}")
    print(f"  validation_worst_trade_mae_pct={_format_optional(summary.validation_worst_trade_mae_pct)}")
    print(f"  final_holdout_worst_trade_mae_pct={_format_optional(summary.final_holdout_worst_trade_mae_pct)}")
    print(f"  validation_allowed={1 if summary.validation_allowed else 0}")
    print(f"  validation_run_complete={1 if report.get('validation_run_complete') else 0}")
    print(f"  diagnostic_only={1 if report.get('diagnostic_only') else 0}")
    print(f"  diagnostic_mode={report.get('diagnostic_mode') or _nested(report, 'research_run', 'diagnostic_mode') or 'candidate_validation'}")
    strategy_contract = _report_strategy_contract(report)
    print(f"  research_strategy_contract={json.dumps(strategy_contract, sort_keys=True)}")
    print(f"  next_required_stage={report.get('next_required_stage') or 'none'}")
    if report.get("standalone_backtest_not_full_validation"):
        print("  reason=standalone_backtest_not_full_validation")
    print(f"  statistical_validation_required={1 if report.get('statistical_validation_required') else 0}")
    print(f"  statistical_candidate_count={report.get('candidate_count')}")
    print(f"  statistical_parameter_grid_size={report.get('parameter_grid_size')}")
    print(f"  statistical_search_budget={report.get('search_budget')}")
    print(f"  statistical_attempt_index={report.get('attempt_index')}")
    print(f"  statistical_holdout_reuse_count={report.get('holdout_reuse_count')}")
    print(f"  selection_universe_hash={report.get('selection_universe_hash') or 'none'}")
    print(f"  candidate_metric_values_hash={report.get('candidate_metric_values_hash') or 'none'}")
    print(f"  statistical_metric_value_count={report.get('metric_value_count')}")
    print(f"  statistical_missing_metric_count={report.get('missing_metric_count')}")
    print(f"  statistical_evidence_hash={report.get('statistical_evidence_hash') or 'none'}")
    print(f"  evidence_grade={report.get('evidence_grade') or 'none'}")
    print(f"  statistical_method={report.get('statistical_method') or 'none'}")
    print(
        "  official_statistical_evidence_wrc_generation_available="
        f"{1 if report.get('official_statistical_evidence_wrc_generation_available') else 0}"
    )
    print(
        "  statistical_evidence_limitations="
        f"{_format_items(tuple(str(item) for item in report.get('statistical_evidence_limitations') or []))}"
    )
    print(f"  return_panel_hash={report.get('return_panel_hash') or 'none'}")
    print(f"  return_unit={report.get('return_unit') or 'none'}")
    print(f"  return_panel_observation_count={report.get('return_panel_observation_count')}")
    print(f"  audit_mode={_nested(report, 'audit_trail_policy', 'mode') or 'none'}")
    print(f"  audit_status={report.get('audit_trail_status') or 'none'}")
    print(f"  audit_trace_manifest_ref={report.get('audit_trail_trace_manifest_ref') or 'none'}")
    print(f"  audit_trace_manifest_hash={report.get('audit_trail_trace_manifest_hash') or 'none'}")
    print(
        "  audit_fail_reasons="
        f"{_format_items(tuple(str(item) for item in report.get('audit_trail_fail_reasons') or []))}"
    )
    execution_observability = report.get("execution_observability")
    if isinstance(execution_observability, dict):
        print(f"  requested_max_workers={execution_observability.get('requested_max_workers')}")
        print(f"  research_max_workers_effective={execution_observability.get('research_max_workers_effective')}")
        print(f"  effective_process_start_method={execution_observability.get('effective_process_start_method') or 'none'}")
        print(f"  observed_worker_count={execution_observability.get('observed_worker_count')}")
        print(
            "  worker_budget_warning_reasons="
            f"{_format_items(tuple(str(item) for item in execution_observability.get('worker_budget_warning_reasons') or []))}"
        )
        print(
            "  worker_observation_warning_reasons="
            f"{_format_items(tuple(str(item) for item in execution_observability.get('worker_observation_warning_reasons') or []))}"
        )
    print(f"  family_trial_registry_path={report.get('family_trial_registry_path') or 'none'}")
    print(f"  family_trial_registry_prior_hash={report.get('family_trial_registry_prior_hash') or 'none'}")
    print(f"  family_trial_registry_row_hash={report.get('family_trial_registry_row_hash') or 'none'}")
    _print_experiment_registry_summary(report)
    print(f"  summary_metric_max_bootstrap_p_value={report.get('summary_metric_max_bootstrap_p_value')}")
    print(f"  white_reality_check_p_value={report.get('white_reality_check_p_value')}")
    print(f"  white_reality_check_method={report.get('white_reality_check_method') or 'none'}")
    print(f"  bootstrap_sampling_contract_hash={report.get('bootstrap_sampling_contract_hash') or 'none'}")
    print(f"  statistical_gate_result={report.get('statistical_gate_result') or 'none'}")
    print(
        "  statistical_gate_fail_reasons="
        f"{_format_items(tuple(str(item) for item in report.get('statistical_gate_fail_reasons') or []))}"
    )
    _print_stress_suite_summary(report)
    print(f"  nearest_failed_candidate_id={summary.nearest_failed_candidate_id or 'none'}")
    print(
        "  nearest_failed_candidate_fail_reasons="
        f"{_format_items(summary.nearest_failed_candidate_fail_reasons)}"
    )
    print(f"  walk_forward_window_summary={_format_walk_forward_window_summary(summary)}")
    print(f"  top_window_fail_reasons={_format_counts(summary.top_window_fail_reasons)}")
    print(f"  execution_reference_policy={_nested(report, 'execution_timing_policy', 'fill_reference_policy') or 'unknown'}")
    print(f"  execution_reality_level={report.get('execution_reality_level') or 'unknown'}")
    _print_execution_capability_summary(report)
    print(f"  execution_reality_gate_status={report.get('execution_reality_gate_status') or 'unknown'}")
    print(
        "  execution_reality_gate_reasons="
        f"{_format_items(tuple(str(item) for item in report.get('execution_reality_gate_reasons') or []))}"
    )
    signal_coverage = report.get("signal_quote_coverage_summary")
    if isinstance(signal_coverage, dict):
        print(
            "  signal_quote_coverage="
            f"signal_event_count={signal_coverage.get('signal_event_count')} "
            f"fillable_signal_event_count={signal_coverage.get('fillable_signal_event_count')} "
            f"missing_quote_on_signal_count={signal_coverage.get('missing_quote_on_signal_count')} "
            f"skipped_execution_signal_count={signal_coverage.get('skipped_execution_signal_count')} "
            f"missing_quote_warning_count={signal_coverage.get('missing_quote_warning_count')} "
            f"quote_after_decision_coverage_pct={signal_coverage.get('quote_after_decision_coverage_pct')} "
            f"median_quote_age_ms={signal_coverage.get('median_quote_age_ms_on_signal')} "
            f"p95_quote_age_ms={signal_coverage.get('p95_quote_age_ms_on_signal')} "
            f"latency_submit_count={signal_coverage.get('latency_applied_to_submit_ts_count')} "
            f"latency_reference_count={signal_coverage.get('latency_applied_to_fill_reference_count')} "
            f"execution_attempt_count={signal_coverage.get('execution_attempt_count')} "
            f"execution_filled_count={signal_coverage.get('execution_filled_count')} "
            f"filled_execution_count={signal_coverage.get('filled_execution_count')} "
            f"portfolio_applied_trade_count={signal_coverage.get('portfolio_applied_trade_count')} "
            f"pending_execution_count={signal_coverage.get('pending_execution_count')} "
            f"skipped_execution_count={signal_coverage.get('skipped_execution_count')} "
            f"failed_execution_count={signal_coverage.get('failed_execution_count')} "
            f"closed_trade_count={signal_coverage.get('closed_trade_count')} "
            f"execution_event_timeline_incomplete={signal_coverage.get('execution_event_timeline_incomplete')}"
        )
    execution_events = report.get("execution_event_summary")
    if isinstance(execution_events, dict):
        print(
            "  execution_event_summary="
            f"execution_attempt_count={execution_events.get('execution_attempt_count')} "
            f"execution_filled_count={execution_events.get('execution_filled_count')} "
            f"portfolio_applied_trade_count={execution_events.get('portfolio_applied_trade_count')} "
            f"pending_execution_count={execution_events.get('pending_execution_count')} "
            f"skipped_execution_count={execution_events.get('skipped_execution_count')} "
            f"failed_execution_count={execution_events.get('failed_execution_count')} "
            f"closed_trade_count={execution_events.get('closed_trade_count')} "
            f"execution_event_timeline_incomplete={execution_events.get('execution_event_timeline_incomplete')}"
        )
    _print_metrics_v2_summary(report)
    print(f"  next_action={summary.next_action}")
    print(f"  report_path={artifact_paths.get('report_path')}")
    print(f"  derived_path={artifact_paths.get('derived_path')}")
    print(f"  content_hash={report.get('content_hash')}")
    warnings = report.get("warnings") or []
    print(f"  warnings={','.join(str(item) for item in warnings) if warnings else 'none'}")
    _print_top_of_book_summary(report)


def _print_research_backtest_progress(event: dict[str, object]) -> None:
    _print_progress_event("RESEARCH-BACKTEST", event)


def _print_research_walk_forward_progress(event: dict[str, object]) -> None:
    _print_progress_event("RESEARCH-WALK-FORWARD", event)


def _print_progress_event(label: str, event: dict[str, object]) -> None:
    parts = [f"stage={event.get('stage', 'unknown')}"]
    for key in sorted(key for key in event if key != "stage"):
        value = event[key]
        if isinstance(value, bool):
            rendered = "1" if value else "0"
        else:
            rendered = str(value)
        parts.append(f"{key}={rendered}")
    print(f"[{label}] " + " ".join(parts), flush=True)


def _format_optional(value: object) -> str:
    return "None" if value is None else str(value)


def _format_strategy_diagnostics_summary(summary: ResearchRunSummary) -> str:
    return (
        "top_exit_reasons="
        f"{_format_counts(summary.top_exit_reasons)} "
        "validation_raw_sell_filter_blocked_while_in_position_count="
        f"{_format_optional(summary.validation_raw_sell_filter_blocked_while_in_position_count)} "
        "final_holdout_raw_sell_filter_blocked_while_in_position_count="
        f"{_format_optional(summary.final_holdout_raw_sell_filter_blocked_while_in_position_count)} "
        f"validation_p95_mae_pct={_format_optional(summary.validation_p95_mae_pct)} "
        f"final_holdout_p95_mae_pct={_format_optional(summary.final_holdout_p95_mae_pct)} "
        f"validation_worst_trade_mae_pct={_format_optional(summary.validation_worst_trade_mae_pct)} "
        f"final_holdout_worst_trade_mae_pct={_format_optional(summary.final_holdout_worst_trade_mae_pct)}"
    )


def _report_strategy_contract(report: dict[str, object]) -> dict[str, object]:
    candidates = report.get("candidates")
    candidate: dict[str, object] | None = None
    selected_id = str(report.get("selected_candidate_id") or report.get("best_candidate_id") or "")
    if isinstance(candidates, list):
        for item in candidates:
            if not isinstance(item, dict):
                continue
            if selected_id and str(item.get("parameter_candidate_id") or item.get("candidate_id") or "") == selected_id:
                candidate = item
                break
            if candidate is None:
                candidate = item
    capabilities = (
        candidate.get("research_strategy_contract")
        if isinstance(candidate, dict)
        else report.get("research_strategy_contract")
    )
    capabilities = dict(capabilities) if isinstance(capabilities, dict) else {}
    return capabilities


def _print_metrics_v2_summary(report: dict[str, object]) -> None:
    metrics = report.get("best_validation_metrics_v2")
    if isinstance(metrics, dict) and (
        metrics.get("metrics_status") == "unavailable" or metrics.get("metrics_v2_source") == "failure_fallback"
    ):
        print(
            "  metrics_v2_summary="
            f"status={metrics.get('metrics_status')} source={metrics.get('metrics_v2_source')}"
        )
        metrics = None
    if not isinstance(metrics, dict):
        candidates = report.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if isinstance(candidate, dict) and candidate.get("acceptance_gate_result") == "PASS":
                    metrics = candidate.get("validation_metrics_v2")
                    if isinstance(metrics, dict) and (
                        metrics.get("metrics_status") == "unavailable"
                        or metrics.get("metrics_v2_source") == "failure_fallback"
                    ):
                        print(
                            "  metrics_v2_summary="
                            f"status={metrics.get('metrics_status')} source={metrics.get('metrics_v2_source')}"
                        )
                        metrics = None
                        continue
                    break
    if not isinstance(metrics, dict):
        return
    return_risk = metrics.get("return_risk") if isinstance(metrics.get("return_risk"), dict) else {}
    trade_quality = metrics.get("trade_quality") if isinstance(metrics.get("trade_quality"), dict) else {}
    time_exposure = metrics.get("time_exposure") if isinstance(metrics.get("time_exposure"), dict) else {}
    cost_execution = metrics.get("cost_execution") if isinstance(metrics.get("cost_execution"), dict) else {}
    print(
        "  metrics_v2_summary="
        f"schema={metrics.get('metrics_schema_version')} "
        f"cagr_pct={return_risk.get('cagr_pct')} "
        f"expectancy_per_trade_krw={trade_quality.get('expectancy_per_trade_krw')} "
        f"exposure_time_pct={time_exposure.get('exposure_time_pct')} "
        f"avg_holding_time_ms={time_exposure.get('avg_holding_time_ms')} "
        f"open_position_at_end={return_risk.get('open_position_at_end')} "
        f"fee_drag_ratio={cost_execution.get('fee_drag_ratio')} "
        f"fee_drag_ratio_basis={cost_execution.get('fee_drag_ratio_basis')} "
        f"slippage_drag_ratio={cost_execution.get('slippage_drag_ratio')} "
        f"slippage_drag_ratio_basis={cost_execution.get('slippage_drag_ratio_basis')}"
    )


def _print_final_selection_components(report: dict[str, object]) -> None:
    selected = str(report.get("selected_candidate_id") or "")
    scores = report.get("candidate_final_scores")
    if not selected or not isinstance(scores, list):
        return
    row = next(
        (
            item for item in scores
            if isinstance(item, dict) and str(item.get("candidate_id") or "") == selected
        ),
        None,
    )
    if not isinstance(row, dict):
        return
    components = row.get("rank_components")
    if not isinstance(components, list):
        return
    for index, component in enumerate(components[:2], start=1):
        if not isinstance(component, dict):
            continue
        print(
            f"  top_final_selection_component_{index}="
            f"{component.get('metric')}:{component.get('value')}:{component.get('order')}"
        )


def _print_execution_event_summary(summary: object) -> None:
    if not isinstance(summary, dict):
        return
    print(
        "  execution_event_summary="
        f"execution_attempt_count={summary.get('execution_attempt_count')} "
        f"execution_filled_count={summary.get('execution_filled_count')} "
        f"portfolio_applied_trade_count={summary.get('portfolio_applied_trade_count')} "
        f"pending_execution_count={summary.get('pending_execution_count')} "
        f"pending_execution_after_dataset_end_count={summary.get('pending_execution_after_dataset_end_count')} "
        f"skipped_execution_count={summary.get('skipped_execution_count')} "
        f"failed_execution_count={summary.get('failed_execution_count')} "
        f"closed_trade_count={summary.get('closed_trade_count')} "
        f"execution_event_timeline_incomplete={summary.get('execution_event_timeline_incomplete')}"
    )


def _print_stress_suite_summary(payload: dict[str, object]) -> None:
    required = bool(payload.get("stress_suite_required"))
    evidence = payload.get("validation_stress_suite")
    if not isinstance(evidence, dict):
        evidence = payload.get("best_validation_stress_suite")
    trade_removal = evidence.get("trade_removal") if isinstance(evidence, dict) and isinstance(evidence.get("trade_removal"), dict) else {}
    monte_carlo = (
        evidence.get("trade_order_monte_carlo")
        if isinstance(evidence, dict) and isinstance(evidence.get("trade_order_monte_carlo"), dict)
        else {}
    )
    period_ablation = (
        evidence.get("period_ablation")
        if isinstance(evidence, dict) and isinstance(evidence.get("period_ablation"), dict)
        else {}
    )
    parameter_perturbation = (
        evidence.get("parameter_perturbation")
        if isinstance(evidence, dict) and isinstance(evidence.get("parameter_perturbation"), dict)
        else {}
    )
    print(f"  stress_suite_required={1 if required else 0}")
    print(f"  stress_suite_gate_result={payload.get('stress_suite_gate_result') or 'none'}")
    print(
        "  stress_suite_fail_reasons="
        f"{_format_items(tuple(str(item) for item in payload.get('stress_suite_fail_reasons') or []))}"
    )
    print(f"  stress_trade_removal_status={trade_removal.get('status') or 'none'}")
    print(f"  stress_period_ablation_status={period_ablation.get('status') or 'none'}")
    print(f"  stress_period_ablation_pass_ratio={period_ablation.get('pass_ratio')}")
    print(f"  stress_parameter_perturbation_status={parameter_perturbation.get('status') or 'none'}")
    print(f"  stress_parameter_perturbation_pass_ratio={parameter_perturbation.get('pass_ratio')}")
    print(f"  stress_monte_carlo_survival_probability={monte_carlo.get('survival_probability')}")
    print(f"  stress_monte_carlo_max_drawdown_pct_p95={monte_carlo.get('max_drawdown_pct_p95')}")


def _print_experiment_registry_summary(payload: dict[str, object]) -> None:
    print(f"  experiment_registry_path={payload.get('experiment_registry_path') or 'none'}")
    print(f"  experiment_registry_prior_hash={payload.get('experiment_registry_prior_hash') or 'none'}")
    print(f"  experiment_registry_row_hash={payload.get('experiment_registry_row_hash') or 'none'}")
    print(f"  experiment_registry_completion_row_hash={payload.get('experiment_registry_completion_row_hash') or 'none'}")
    print(f"  experiment_registry_bound_evidence_hash={payload.get('experiment_registry_bound_evidence_hash') or 'none'}")
    print(f"  experiment_registry_evidence_hash_phase={payload.get('experiment_registry_evidence_hash_phase') or 'none'}")
    print(f"  final_holdout_fingerprint={payload.get('final_holdout_fingerprint') or 'none'}")
    print(f"  final_holdout_identity_hash={payload.get('final_holdout_identity_hash') or 'none'}")
    print(f"  final_holdout_content_hash={payload.get('final_holdout_content_hash') or 'none'}")
    print(f"  final_holdout_reuse_key_hash={payload.get('final_holdout_reuse_key_hash') or 'none'}")
    print(f"  final_holdout_split_hash={payload.get('final_holdout_split_hash') or 'none'}")
    print(f"  computed_attempt_index={payload.get('computed_attempt_index')}")
    print(f"  computed_holdout_reuse_count={payload.get('computed_holdout_reuse_count')}")
    print(f"  declared_attempt_index={payload.get('declared_attempt_index')}")
    print(f"  declared_holdout_reuse_count={payload.get('declared_holdout_reuse_count')}")
    print(f"  registry_gate_result={payload.get('registry_gate_result') or 'none'}")
    print(
        "  registry_gate_fail_reasons="
        f"{_format_items(tuple(str(item) for item in payload.get('registry_gate_fail_reasons') or []))}"
    )
    print(f"  research_freedom_hash={payload.get('research_freedom_hash') or 'none'}")


def _format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    return ",".join(f"{key}:{value}" for key, value in counts.items())


def _format_items(items: tuple[str, ...]) -> str:
    if not items:
        return "none"
    return ",".join(items)


def _format_walk_forward_window_summary(summary: ResearchRunSummary) -> str:
    if summary.walk_forward_window_count is None:
        return "none"
    return (
        f"window_count:{summary.walk_forward_window_count},"
        f"pass:{summary.walk_forward_pass_window_count if summary.walk_forward_pass_window_count is not None else 'unknown'},"
        f"fail:{summary.walk_forward_fail_window_count if summary.walk_forward_fail_window_count is not None else 'unknown'}"
    )


def _print_top_of_book_summary(report: dict[str, object]) -> None:
    summary = report.get("top_of_book_quality_summary")
    if not isinstance(summary, dict) or not bool(summary.get("requested")):
        return
    affected = summary.get("affected_splits")
    affected_names = []
    if isinstance(affected, list):
        affected_names = [
            str(item.get("split_name"))
            for item in affected
            if isinstance(item, dict) and item.get("split_name")
        ]
    print(
        "  top_of_book_quote_coverage="
        f"requested=1 required={1 if summary.get('required') else 0} "
        f"gate_status={summary.get('gate_status')} "
        f"coverage_pct={summary.get('coverage_pct')} "
        f"joined_count={summary.get('joined_quote_count')} "
        f"missing_count={summary.get('missing_quote_count')} "
        f"join_tolerance_ms={summary.get('join_tolerance_ms')} "
        f"affected_splits={','.join(affected_names) if affected_names else 'none'}"
    )
    print(
        "  top_of_book_limitations="
        "best_bid_ask_only_not_full_depth,intra_candle_path_unavailable"
    )
    if summary.get("next_action"):
        print(f"  top_of_book_next_action={summary.get('next_action')}")


def _print_execution_capability_summary(report: dict[str, object]) -> None:
    capability = report.get("execution_capability_contract")
    unavailable: object = report.get("unavailable_required_capabilities")
    market_impact_available: object = report.get("market_impact_model_available")
    top_of_book_is_full_depth: object = report.get("top_of_book_is_full_depth")
    if isinstance(capability, dict):
        available = capability.get("available_capabilities") if isinstance(capability.get("available_capabilities"), dict) else {}
        unavailable = capability.get("unavailable_required_capabilities", unavailable)
        market_impact_available = available.get("market_impact_model", market_impact_available)
        top_of_book_is_full_depth = available.get("top_of_book_is_full_depth", top_of_book_is_full_depth)
    unavailable_items = tuple(str(item) for item in (unavailable or [])) if isinstance(unavailable, list) else ()
    print(f"  execution_capability_contract_hash={report.get('execution_capability_contract_hash') or 'none'}")
    print(f"  evidence_tier={report.get('evidence_tier') or (capability.get('evidence_tier') if isinstance(capability, dict) else 'unknown')}")
    print(f"  unavailable_required_capabilities={_format_items(unavailable_items)}")
    print(f"  market_impact_required={report.get('market_impact_required')}")
    print(f"  market_impact_model_available={market_impact_available}")
    print(f"  top_of_book_is_full_depth={top_of_book_is_full_depth}")
    if unavailable_items:
        print("  execution_capability_next_action=remove unsupported requirements or add implemented evidence/model support")


def _nested(payload: dict[str, object], *keys: str) -> object | None:
    current: object = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current
