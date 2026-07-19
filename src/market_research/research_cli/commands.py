from __future__ import annotations

import argparse
import json
from time import monotonic
from typing import Any

from market_research.application.adapters import (
    preflight_request_from_namespace,
    validation_request_from_namespace,
)
from market_research.application.service import ResearchApplicationService
from market_research.research_composition import builtin_strategy_registry

from .context import ResearchAppContext


def execute_research_command(
    command: str,
    args: argparse.Namespace,
    context: ResearchAppContext,
) -> int:
    """Dispatch an already-parsed research command with explicit dependencies."""

    if command == "research-freeze-dataset":
        from market_research.research.dataset_freeze import cmd_research_freeze_dataset

        return int(
            cmd_research_freeze_dataset(
                db_path=args.db,
                market=args.market,
                interval=args.interval,
                start=args.start,
                end=args.end,
                out_path=args.out,
                provenance_manifest_path=args.provenance_manifest,
            )
        )

    if command in {
        "research-derivative-register",
        "research-derivative-replay",
        "research-derivative-diff",
    }:
        return _execute_derivative_evidence_command(
            command=command, args=args, context=context
        )

    from market_research.research import cli

    lifecycle_commands = {
        "research-backtest",
        "research-walk-forward",
        "research-reproduce-run",
    }
    if command in lifecycle_commands:
        from market_research.research.run_lifecycle import start_run

        handle = start_run(
            manager=context.paths,
            command=command,
            command_args=_namespace_payload(args),
        )
        context.run_id = handle.run_id
        context.run_result_hash = None
        try:
            rc = _dispatch_research_command(command, args, context, cli)
        except BaseException as exc:
            handle.finish(
                status="ABORTED" if isinstance(exc, KeyboardInterrupt) else "FAILED",
                exit_code=130 if isinstance(exc, KeyboardInterrupt) else 1,
                result_content_hash=context.run_result_hash,
                error=exc,
            )
            raise
        handle.finish(
            status="SUCCEEDED" if rc == 0 else "FAILED",
            exit_code=rc,
            result_content_hash=context.run_result_hash,
        )
        return rc

    return _dispatch_research_command(command, args, context, cli)


def _dispatch_research_command(
    command: str,
    args: argparse.Namespace,
    context: ResearchAppContext,
    cli: Any,
) -> int:

    if command == "research-backtest":
        return int(
            cli.cmd_research_backtest(
                context=context,
                manifest_path=args.manifest,
                execution_calibration_path=args.execution_calibration,
                diagnostic_mode=args.diagnostic_mode,
            )
        )
    if command == "research-walk-forward":
        return int(
            cli.cmd_research_walk_forward(
                context=context,
                manifest_path=args.manifest,
                execution_calibration_path=args.execution_calibration,
            )
        )
    if command == "research-validate":
        return _execute_validation_application(args=args, context=context, cli=cli)
    if command == "research-readiness":
        return _execute_readiness_application(args=args, context=context)
    if command == "research-workload-estimate":
        return _execute_workload_application(args=args, context=context)
    if command == "research-forward-diagnostics":
        from market_research.research.forward_diagnostics_cli import (
            cmd_research_forward_diagnostics,
        )

        return int(
            cmd_research_forward_diagnostics(
                context=context,
                manifest_path=args.manifest,
                split_name=args.split,
                features=args.features,
                horizons=args.horizons,
                bucket=args.bucket,
                entry_price=args.entry_price,
                min_bucket_count=args.min_bucket_count,
                out_path=args.out,
                as_json=args.json,
                allow_final_holdout_diagnostics=args.allow_final_holdout_diagnostics,
                allow_degraded_diagnostics=args.allow_degraded_diagnostics,
                strategy_registry=cli.builtin_strategy_registry(),
            )
        )
    if command == "research-batch":
        return int(
            cli.cmd_research_batch(
                context=context,
                manifest_glob=args.manifest_glob,
                max_concurrent_manifests=args.max_concurrent_manifests,
                command=args.command,
                fail_fast=args.fail_fast,
                out_path=args.out,
            )
        )
    if command == "research-verify-audit":
        return int(
            cli.cmd_research_verify_audit(
                context=context, experiment_id=args.experiment_id
            )
        )
    if command == "research-reproduce-run":
        return int(
            cli.cmd_research_reproduce_run(
                context=context,
                manifest_path=args.manifest,
                receipt_path=args.receipt,
                out_path=args.out,
            )
        )
    if command == "research-registry-inspect":
        return int(
            cli.cmd_research_registry_inspect(context=context, row_hash=args.row_hash)
        )
    if command == "research-registry-validate":
        return int(
            cli.cmd_research_registry_validate(
                context=context, experiment_id=args.experiment_id
            )
        )
    if command == "research-mark-attempt-aborted":
        return int(
            cli.cmd_research_mark_attempt_aborted(
                context=context, row_hash=args.row_hash, reason=args.reason
            )
        )
    if command == "research-export-strategy-package":
        return int(
            cli.cmd_research_export_strategy_package(
                context=context,
                result_path=args.result,
                approval_path=args.approval,
                out_path=args.out,
            )
        )
    if command == "research-compare":
        return int(
            cli.cmd_research_compare(
                context=context,
                report_paths=tuple(args.report),
                out_path=args.out,
            )
        )
    if command == "research-render-report":
        return int(
            cli.cmd_research_render_report(
                context=context,
                report_path=args.report,
                out_path=args.out,
            )
        )
    if command == "research-governance-transition":
        return int(
            cli.cmd_research_governance_transition(
                context=context,
                subject_type=args.subject_type,
                subject_id=args.subject_id,
                subject_version=args.subject_version,
                from_state=args.from_state,
                to_state=args.to_state,
                actor_id=args.actor,
                reason=args.reason,
                evidence=tuple(args.evidence),
            )
        )
    if command == "research-record-human-review":
        return int(
            cli.cmd_research_record_human_review(
                context=context,
                subject_type=args.subject_type,
                subject_id=args.subject_id,
                subject_version=args.subject_version,
                decision=args.decision,
                reviewer_id=args.reviewer,
                reviewer_role=args.reviewer_role,
                rationale=args.rationale,
                reviewed_artifact_hash=args.reviewed_artifact_hash,
                requested_changes_path=args.requested_changes,
                resolved_requirement_ids=tuple(args.resolved_requirement),
            )
        )
    if command == "research-approve-strategy-candidate":
        return int(
            cli.cmd_research_approve_strategy_candidate(
                context=context,
                result_path=args.result,
                subject_version=args.subject_version,
                reviewer_id=args.reviewer,
                rationale=args.rationale,
                resolved_requirement_ids=tuple(args.resolved_requirement),
                out_path=args.out,
            )
        )
    raise ValueError(f"unsupported research command: {command}")


def _namespace_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        key: value
        for key, value in vars(args).items()
        if key not in {"func", "handler"}
    }


def _execute_derivative_evidence_command(
    *,
    command: str,
    args: argparse.Namespace,
    context: ResearchAppContext,
) -> int:
    from market_research.research.derivatives.workflow import (
        diff_derivative_evidence_packages,
        register_derivative_evidence_bundle,
        replay_derivative_evidence_bundle,
    )

    if command == "research-derivative-register":
        package_ref = register_derivative_evidence_bundle(context.paths, args.bundle)
        context.run_result_hash = package_ref.content_hash
        context.printer(
            json.dumps(
                {
                    "status": "REGISTERED",
                    "package_ref": package_ref.as_dict(),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if command == "research-derivative-replay":
        receipt = replay_derivative_evidence_bundle(
            context.paths,
            args.bundle,
            verified_at=args.verified_at,
        )
        context.run_result_hash = receipt.content_hash
        context.printer(
            json.dumps(receipt.as_dict(), ensure_ascii=False, sort_keys=True)
        )
        return 0
    if command == "research-derivative-diff":
        difference = diff_derivative_evidence_packages(
            context.paths,
            left_package_id=args.left_package_id,
            left_version=args.left_version,
            right_package_id=args.right_package_id,
            right_version=args.right_version,
        )
        context.printer(json.dumps(difference, ensure_ascii=False, sort_keys=True))
        return 0
    raise ValueError(f"unsupported derivative evidence command: {command}")


def _application_service(context: ResearchAppContext) -> ResearchApplicationService:
    return ResearchApplicationService(
        paths=context.paths,
        strategy_registry=builtin_strategy_registry(),
        environment_summary=(
            context.environment.as_dict() if context.environment is not None else None
        ),
    )


def _execute_readiness_application(
    *,
    args: argparse.Namespace,
    context: ResearchAppContext,
) -> int:
    from market_research.research.readiness import _print_readiness

    request = preflight_request_from_namespace(args)
    result = _application_service(context).readiness(
        request,
        progress_callback=(
            None
            if args.json
            else lambda event: context.printer(
                "[RESEARCH-READINESS] "
                f"scanning split={event.get('split')} method={event.get('method')}"
            )
        ),
    )
    if result.errors:
        context.printer(f"[RESEARCH-READINESS] error={result.errors[0].message}")
        return int(result.exit_code)
    report = result.report or {}
    if args.json:
        context.printer(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
        )
    else:
        _print_readiness(report, printer=context.printer)
    return int(result.exit_code)


def _execute_workload_application(
    *,
    args: argparse.Namespace,
    context: ResearchAppContext,
) -> int:
    request = preflight_request_from_namespace(args)
    result = _application_service(context).workload_estimate(request)
    if result.errors:
        context.printer(
            f"[RESEARCH-WORKLOAD-ESTIMATE] error={result.errors[0].message}"
        )
        return int(result.exit_code)
    payload = result.estimate or {}
    if args.json:
        context.printer(json.dumps(payload, sort_keys=True, indent=2))
        return int(result.exit_code)
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
    return int(result.exit_code)


def _execute_validation_application(
    *,
    args: argparse.Namespace,
    context: ResearchAppContext,
    cli: Any,
) -> int:
    started_at = monotonic()
    request = validation_request_from_namespace(args)
    rc = 1
    try:
        result = _application_service(context).validate(
            request,
            progress_callback=cli._print_research_backtest_progress,
        )
        context.run_id = result.run_id
        context.run_result_hash = result.content_hash
        rc = int(result.exit_code)
        if result.errors:
            context.printer(f"[RESEARCH-VALIDATE] error={result.errors[0].message}")
        elif result.report is not None:
            cli._print_validation_run_summary(result.report)
    finally:
        cli._print_research_command_finished(
            context,
            "research-validate",
            started_at,
            rc,
            manifest=request.manifest_path,
            execution_calibration=request.execution_calibration_path,
            candidate_id=request.candidate_id,
            out=request.out_path,
            mode=request.mode,
        )
    return rc
