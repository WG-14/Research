from __future__ import annotations

import argparse
from types import SimpleNamespace

from .context import ResearchAppContext


def _legacy_research_dependencies(context: ResearchAppContext) -> tuple[object, object]:
    """Adapt the existing research functions without loading them for help.

    The legacy functions currently accept the operational path/config globals.
    During this transition they receive an injected research-path/settings view
    for the duration of the selected command only. This avoids a change to the
    existing ``bithumb-bot`` command path.
    """

    settings = SimpleNamespace(
        DB_PATH=str(context.paths.require_database_path()) if context.settings.db_path else None,
        MODE="research",
    )
    return context.paths, settings


def execute_existing_research_command(
    command: str, args: argparse.Namespace, context: ResearchAppContext
) -> int:
    # Importing this compatibility module is deliberately deferred until after
    # parsing. It is never part of bootstrap or --help execution.
    if command == "research-freeze-dataset":
        from bithumb_bot.research.dataset_freeze import cmd_research_freeze_dataset

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
    if command == "research-readiness":
        from bithumb_bot.research.readiness import cmd_research_readiness

        return int(
            cmd_research_readiness(
                manifest_path=args.manifest,
                execution_calibration_path=args.execution_calibration,
                missing_classification_path=args.missing_classification,
                as_json=args.json,
                db_path=context.paths.require_database_path(),
                mode="research",
            )
        )
    if command == "research-forward-diagnostics":
        from bithumb_bot.research.forward_diagnostics_cli import cmd_research_forward_diagnostics

        return int(
            cmd_research_forward_diagnostics(
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
                db_path=context.paths.require_database_path(),
                manager=context.paths,
            )
        )

    from bithumb_bot.research import cli as legacy

    paths, settings = _legacy_research_dependencies(context)
    old_paths, old_settings = legacy.PATH_MANAGER, legacy.settings
    legacy.PATH_MANAGER, legacy.settings = paths, settings
    try:
        return _dispatch(command, args, legacy, default_notification_policy=context.settings.notification_policy)
    finally:
        legacy.PATH_MANAGER, legacy.settings = old_paths, old_settings


def _dispatch(
    command: str,
    args: argparse.Namespace,
    legacy: object,
    *,
    default_notification_policy: str,
) -> int:
    if command == "research-backtest":
        return int(legacy.cmd_research_backtest(manifest_path=args.manifest, execution_calibration_path=args.execution_calibration, diagnostic_mode=args.diagnostic_mode, notification_policy=args.notification_policy or default_notification_policy))
    if command == "research-walk-forward":
        return int(legacy.cmd_research_walk_forward(manifest_path=args.manifest, execution_calibration_path=args.execution_calibration, notification_policy=args.notification_policy or default_notification_policy))
    if command == "research-validate":
        return int(legacy.cmd_research_validate(manifest_path=args.manifest, execution_calibration_path=args.execution_calibration, candidate_id=args.candidate_id, out_path=args.out, mode=args.mode, notification_policy=args.notification_policy or default_notification_policy))
    if command == "research-workload-estimate":
        return int(legacy.cmd_research_workload_estimate(manifest_path=args.manifest, as_json=args.json))
    if command == "research-batch":
        return int(legacy.cmd_research_batch(manifest_glob=args.manifest_glob, max_concurrent_manifests=args.max_concurrent_manifests, command=args.command, fail_fast=args.fail_fast, out_path=args.out))
    if command == "research-verify-audit":
        return int(legacy.cmd_research_verify_audit(experiment_id=args.experiment_id))
    if command == "research-reproduce":
        return int(legacy.cmd_research_reproduce(promotion_path=args.promotion))
    if command == "research-registry-inspect":
        return int(legacy.cmd_research_registry_inspect(row_hash=args.row_hash))
    if command == "research-registry-validate":
        return int(legacy.cmd_research_registry_validate(experiment_id=args.experiment_id))
    if command == "research-mark-attempt-aborted":
        return int(legacy.cmd_research_mark_attempt_aborted(row_hash=args.row_hash, reason=args.reason))
    raise ValueError(f"unsupported research command: {command}")
