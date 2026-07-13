from __future__ import annotations

import argparse
from typing import Any

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

    from market_research.research import cli

    lifecycle_commands = {
        "research-backtest",
        "research-walk-forward",
        "research-validate",
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
        return int(cli.cmd_research_backtest(
            context=context, manifest_path=args.manifest,
            execution_calibration_path=args.execution_calibration,
            diagnostic_mode=args.diagnostic_mode,
        ))
    if command == "research-walk-forward":
        return int(cli.cmd_research_walk_forward(
            context=context, manifest_path=args.manifest,
            execution_calibration_path=args.execution_calibration,
        ))
    if command == "research-validate":
        return int(cli.cmd_research_validate(
            context=context, manifest_path=args.manifest,
            execution_calibration_path=args.execution_calibration,
            candidate_id=args.candidate_id, out_path=args.out, mode=args.mode,
        ))
    if command == "research-readiness":
        from market_research.research.readiness import cmd_research_readiness

        return int(cmd_research_readiness(
            context=context, manifest_path=args.manifest,
            execution_calibration_path=args.execution_calibration, as_json=args.json,
        ))
    if command == "research-workload-estimate":
        return int(cli.cmd_research_workload_estimate(context=context, manifest_path=args.manifest, as_json=args.json))
    if command == "research-forward-diagnostics":
        from market_research.research.forward_diagnostics_cli import cmd_research_forward_diagnostics

        return int(cmd_research_forward_diagnostics(
            context=context, manifest_path=args.manifest, split_name=args.split,
            features=args.features, horizons=args.horizons, bucket=args.bucket,
            entry_price=args.entry_price, min_bucket_count=args.min_bucket_count,
            out_path=args.out, as_json=args.json,
            allow_final_holdout_diagnostics=args.allow_final_holdout_diagnostics,
            allow_degraded_diagnostics=args.allow_degraded_diagnostics,
            strategy_registry=cli.builtin_strategy_registry(),
        ))
    if command == "research-batch":
        return int(cli.cmd_research_batch(
            context=context, manifest_glob=args.manifest_glob,
            max_concurrent_manifests=args.max_concurrent_manifests,
            command=args.command, fail_fast=args.fail_fast, out_path=args.out,
        ))
    if command == "research-verify-audit":
        return int(cli.cmd_research_verify_audit(context=context, experiment_id=args.experiment_id))
    if command == "research-reproduce-run":
        return int(cli.cmd_research_reproduce_run(
            context=context, manifest_path=args.manifest, receipt_path=args.receipt, out_path=args.out,
        ))
    if command == "research-registry-inspect":
        return int(cli.cmd_research_registry_inspect(context=context, row_hash=args.row_hash))
    if command == "research-registry-validate":
        return int(cli.cmd_research_registry_validate(context=context, experiment_id=args.experiment_id))
    if command == "research-mark-attempt-aborted":
        return int(cli.cmd_research_mark_attempt_aborted(context=context, row_hash=args.row_hash, reason=args.reason))
    if command == "research-export-strategy-package":
        return int(cli.cmd_research_export_strategy_package(
            context=context, result_path=args.result, approval_path=args.approval, out_path=args.out,
        ))
    if command == "research-compare":
        return int(cli.cmd_research_compare(
            context=context, report_paths=tuple(args.report), out_path=args.out,
        ))
    if command == "research-render-report":
        return int(cli.cmd_research_render_report(
            context=context, report_path=args.report, out_path=args.out,
        ))
    if command == "research-governance-transition":
        return int(cli.cmd_research_governance_transition(
            context=context, subject_type=args.subject_type, subject_id=args.subject_id,
            subject_version=args.subject_version, from_state=args.from_state,
            to_state=args.to_state, actor_id=args.actor, reason=args.reason,
            evidence=tuple(args.evidence),
        ))
    if command == "research-record-human-review":
        return int(cli.cmd_research_record_human_review(
            context=context, subject_type=args.subject_type, subject_id=args.subject_id,
            subject_version=args.subject_version, decision=args.decision,
            reviewer_id=args.reviewer, reviewer_role=args.reviewer_role,
            rationale=args.rationale, reviewed_artifact_hash=args.reviewed_artifact_hash,
            requested_changes_path=args.requested_changes,
            resolved_requirement_ids=tuple(args.resolved_requirement),
        ))
    if command == "research-approve-strategy-candidate":
        return int(cli.cmd_research_approve_strategy_candidate(
            context=context, result_path=args.result, subject_version=args.subject_version,
            reviewer_id=args.reviewer, rationale=args.rationale,
            resolved_requirement_ids=tuple(args.resolved_requirement), out_path=args.out,
        ))
    raise ValueError(f"unsupported research command: {command}")


def _namespace_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        key: value
        for key, value in vars(args).items()
        if key not in {"func", "handler"}
    }
