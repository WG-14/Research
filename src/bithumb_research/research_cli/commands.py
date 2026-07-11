from __future__ import annotations

import argparse

from .context import ResearchAppContext


def execute_research_command(
    command: str,
    args: argparse.Namespace,
    context: ResearchAppContext,
) -> int:
    """Dispatch an already-parsed research command with explicit dependencies."""

    if command == "research-freeze-dataset":
        from bithumb_research.research.dataset_freeze import cmd_research_freeze_dataset

        return int(
            cmd_research_freeze_dataset(
                db_path=args.db,
                market=args.market,
                interval=args.interval,
                start=args.start,
                end=args.end,
                out_path=args.out,
            )
        )

    from bithumb_research.research import cli

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
        from bithumb_research.research.readiness import cmd_research_readiness

        return int(cmd_research_readiness(
            context=context, manifest_path=args.manifest,
            execution_calibration_path=args.execution_calibration,
            missing_classification_path=args.missing_classification, as_json=args.json,
        ))
    if command == "research-workload-estimate":
        return int(cli.cmd_research_workload_estimate(context=context, manifest_path=args.manifest, as_json=args.json))
    if command == "research-forward-diagnostics":
        from bithumb_research.research.forward_diagnostics_cli import cmd_research_forward_diagnostics

        return int(cmd_research_forward_diagnostics(
            context=context, manifest_path=args.manifest, split_name=args.split,
            features=args.features, horizons=args.horizons, bucket=args.bucket,
            entry_price=args.entry_price, min_bucket_count=args.min_bucket_count,
            out_path=args.out, as_json=args.json,
            allow_final_holdout_diagnostics=args.allow_final_holdout_diagnostics,
            allow_degraded_diagnostics=args.allow_degraded_diagnostics,
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
        return int(cli.cmd_research_reproduce_run(context=context, manifest_path=args.manifest))
    if command == "research-registry-inspect":
        return int(cli.cmd_research_registry_inspect(context=context, row_hash=args.row_hash))
    if command == "research-registry-validate":
        return int(cli.cmd_research_registry_validate(context=context, experiment_id=args.experiment_id))
    if command == "research-mark-attempt-aborted":
        return int(cli.cmd_research_mark_attempt_aborted(context=context, row_hash=args.row_hash, reason=args.reason))
    raise ValueError(f"unsupported research command: {command}")
