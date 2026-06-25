from __future__ import annotations

import argparse
import json

from bithumb_bot.cli.registry import CommandSpec

from ._helpers import make_spec


def _settings_default(name: str):
    from bithumb_bot.config import settings

    return getattr(settings, name)


def _simple(function_name: str):
    def _handler(_args: argparse.Namespace, _context) -> int | None:
        from bithumb_bot import operator_commands

        return getattr(operator_commands, function_name)()

    return _handler


def _with_limit(function_name: str, attr: str = "limit", *, minimum: int | None = None):
    def _handler(args: argparse.Namespace, _context) -> int | None:
        from bithumb_bot import operator_commands

        value = int(getattr(args, attr))
        if minimum is not None:
            value = max(minimum, value)
        return getattr(operator_commands, function_name)(value)

    return _handler


def _signal(args: argparse.Namespace, _context) -> None:
    from bithumb_bot.operator_commands import cmd_signal

    cmd_signal(args.short, args.long)


def _explain(args: argparse.Namespace, _context) -> None:
    from bithumb_bot.operator_commands import cmd_explain

    cmd_explain(args.short, args.long)


def _audit(_args: argparse.Namespace, _context) -> None:
    from bithumb_bot.operator_commands import cmd_audit

    cmd_audit()


def _config_dump(args: argparse.Namespace, _context) -> None:
    from bithumb_bot.operator_commands import cmd_config_dump

    cmd_config_dump(masked=bool(args.masked))


def _notification_diagnose(args: argparse.Namespace, _context) -> int:
    from bithumb_bot.notification_diagnostics import cmd_notification_diagnose

    return int(
        cmd_notification_diagnose(
            as_json=bool(args.json),
            probe=bool(args.probe),
            policy=str(args.notification_policy) if args.notification_policy else None,
        )
    )


def _validate_db(args: argparse.Namespace, _context) -> int:
    from bithumb_bot.operator_commands import cmd_validate_db

    return int(cmd_validate_db(as_json=bool(args.json)))


def _run(args: argparse.Namespace, _context) -> None:
    from bithumb_bot.operator_commands import cmd_run

    del args
    cmd_run()


def _live_dry_run(args: argparse.Namespace, _context) -> None:
    from bithumb_bot.operator_commands import cmd_live_dry_run

    del args
    cmd_live_dry_run()


def _h74_clear_non_authoritative_state(args: argparse.Namespace, context) -> int:
    import time
    from pathlib import Path

    from bithumb_bot.db_core import ensure_db
    from bithumb_bot.h74_state_cleanup import clear_h74_non_authoritative_state
    from bithumb_bot.paths import PathManager
    from bithumb_bot.storage_io import write_json_atomic

    pair = str(args.pair or "").strip().upper()
    manager = PathManager.from_env(Path.cwd())
    backup_path = manager.config.backup_root / context.settings.MODE / "snapshots" / (
        f"h74_non_authoritative_state_cleanup_{pair}_{int(time.time())}.json"
    )
    summary_path = manager.report_path("h74_state_cleanup", ext="json")
    conn = ensure_db()
    conn.row_factory = __import__("sqlite3").Row
    try:
        summary = clear_h74_non_authoritative_state(
            conn,
            pair=pair,
            backup_path=backup_path,
            require_flat=bool(args.require_flat),
            broker_convergence_ok=False,
            allow_broker_unverified=bool(args.allow_broker_unverified),
        )
        conn.commit()
    finally:
        conn.close()
    write_json_atomic(summary_path, summary)
    context.printer(
        "h74_clear_non_authoritative_state_ok "
        f"pair={pair} backup_path={backup_path} summary_path={summary_path}"
    )
    return 0


def _h74_no_window_probe(args: argparse.Namespace, context) -> int:
    from pathlib import Path

    from bithumb_bot.db_core import ensure_db
    from bithumb_bot.h74_authority_alignment import load_h74_authority_payload
    from bithumb_bot.h74_execution_path_probe import generate_h74_execution_path_probe_report
    from bithumb_bot.h74_probe_acceptance import evaluate_h74_execution_path_probe_acceptance
    from bithumb_bot.h74_pre_submit_evidence import require_pre_submit_bundle_hash
    from bithumb_bot.h74_restore_check import verify_h74_restore_original_window
    from bithumb_bot.paths import PathManager
    from bithumb_bot.research.hashing import sha256_prefixed
    from bithumb_bot.storage_io import write_json_atomic

    if not args.pre_submit_evidence:
        context.printer("h74_no_window_probe_failed reason=h74_no_window_probe_pre_submit_evidence_hash_required")
        return 1
    probe_run_id = str(args.probe_run_id or "").strip()
    if not probe_run_id:
        context.printer("h74_no_window_probe_failed reason=probe_run_id_required")
        return 1
    with Path(args.pre_submit_evidence).expanduser().open("r", encoding="utf-8") as handle:
        bundle = json.load(handle)
    require_pre_submit_bundle_hash(bundle)
    manager = PathManager.from_env(Path.cwd())
    startup_path = manager.report_path("h74_no_window_probe_startup", ext="json")
    startup_artifact = {
        "artifact_type": "h74_no_window_probe_startup",
        "probe_run_id": probe_run_id,
        "pre_submit_evidence_hash": str(bundle["pre_submit_evidence_hash"]),
    }
    startup_artifact["startup_artifact_hash"] = sha256_prefixed(startup_artifact)
    write_json_atomic(startup_path, startup_artifact)

    conn = ensure_db(str(args.db) if args.db else None, ensure_schema_ready=False)
    try:
        report = generate_h74_execution_path_probe_report(
            conn,
            probe_run_id=probe_run_id,
            pair=str(args.pair or getattr(context.settings, "PAIR", "KRW-BTC") or "KRW-BTC"),
            min_executable_qty=float(args.min_executable_qty),
        )
    finally:
        conn.close()
    report_path = manager.report_path("h74_execution_path_probe_report", ext="json")
    write_json_atomic(report_path, report)
    acceptance = evaluate_h74_execution_path_probe_acceptance(report)
    acceptance["acceptance_artifact_hash"] = sha256_prefixed(acceptance)
    acceptance_path = manager.report_path("h74_execution_path_probe_acceptance", ext="json")
    write_json_atomic(acceptance_path, acceptance)

    restore_path_text = ""
    if report["execution_path_probe_status"] == "PASS":
        authority_path = str(args.restore_authority or getattr(context.settings, "H74_SOURCE_OBSERVATION_AUTHORITY_PATH", "") or "").strip()
        if not authority_path:
            context.printer("h74_no_window_probe_failed reason=restore_source_authority_required")
            return 1
        restore = verify_h74_restore_original_window(
            authority_payload=load_h74_authority_payload(authority_path),
            settings_obj=context.settings,
            env_hash=str(bundle.get("env_hash") or ""),
        )
        restore_path = manager.report_path("h74_restore_original_window_check", ext="json")
        write_json_atomic(restore_path, restore)
        restore_path_text = f" restore_artifact={restore_path}"
    context.printer(
        "h74_no_window_probe_complete "
        f"pre_submit_evidence_hash={bundle['pre_submit_evidence_hash']} "
        f"probe_run_id={probe_run_id} "
        f"execution_path_probe_status={report['execution_path_probe_status']} "
        f"acceptance_status={acceptance['execution_path_probe_status']} "
        f"startup_artifact={startup_path} "
        f"report_artifact={report_path} "
        f"acceptance_artifact={acceptance_path}"
        f"{restore_path_text}"
    )
    return 0 if acceptance["execution_path_probe_status"] == "PASS" else 1


def _runtime_strategy_set_lint(_args: argparse.Namespace, context) -> int:
    from bithumb_bot.config import validate_runtime_strategy_set_selection
    from bithumb_bot.h74_authority_alignment import validate_h74_authority_file_env_alignment
    from bithumb_bot.runtime_strategy_set import normalized_runtime_strategy_set_manifest

    try:
        validate_runtime_strategy_set_selection(context.settings)
        authority_path = str(
            getattr(context.settings, "H74_SOURCE_OBSERVATION_AUTHORITY_PATH", "") or ""
        ).strip()
        alignment = None
        if authority_path:
            alignment = validate_h74_authority_file_env_alignment(
                authority_path,
                settings_obj=context.settings,
                raise_on_mismatch=False,
            )
            if not alignment.ok:
                context.printer(
                    "runtime_strategy_set_lint_failed "
                    f"reason_code={alignment.reason_code} "
                    f"authority_type={alignment.authority_type} "
                    f"mismatched_keys={','.join(alignment.mismatched_keys)}"
                )
                return 1
        manifest = normalized_runtime_strategy_set_manifest(settings_obj=context.settings)
    except Exception as exc:
        context.printer(f"runtime_strategy_set_lint_failed reason={type(exc).__name__}:{exc}")
        return 1
    context.printer(
        "runtime_strategy_set_lint_ok "
        f"runtime_scope={manifest['runtime_scope']!r} "
        f"manifest_hash={manifest['runtime_strategy_set_manifest_hash']} "
        f"active_strategy_count={manifest['active_strategy_count']} "
        f"source={manifest['source']} "
        f"authority_env_alignment={(alignment.reason_code if alignment is not None else 'not_configured')}"
    )
    return 0


def _runtime_strategy_set_dump(args: argparse.Namespace, context) -> int:
    from bithumb_bot.config import validate_runtime_strategy_set_selection
    from bithumb_bot.runtime_strategy_set import normalized_runtime_strategy_set_manifest

    validate_runtime_strategy_set_selection(context.settings)
    manifest = normalized_runtime_strategy_set_manifest(settings_obj=context.settings)
    context.printer(json.dumps(manifest, indent=2 if args.pretty else None, sort_keys=True))
    return 0


def _build_window_parser(parser: argparse.ArgumentParser) -> None:
    from bithumb_bot.strategy_config import _sma_int

    parser.add_argument("--short", type=int, default=_sma_int("SMA_SHORT"))
    parser.add_argument("--long", type=int, default=_sma_int("SMA_LONG"))


def _limit(default: int):
    def _build(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--limit", type=int, default=default)

    return _build


def command_specs() -> list[CommandSpec]:
    return [
        make_spec("signal", domain="runtime", handler=_signal, build=_build_window_parser),
        make_spec("explain", domain="runtime", handler=_explain, build=_build_window_parser),
        make_spec("status", domain="runtime", handler=_simple("cmd_status")),
        make_spec(
            "health",
            domain="runtime",
            handler=_simple("cmd_health"),
            help="show health summary (staleness/errors/trading state/recovery)",
            description="Show health summary for limited unattended operation checks.",
        ),
        make_spec("audit", domain="runtime", handler=_audit),
        make_spec("check", domain="runtime", handler=_audit),
        make_spec("audit-ledger", domain="runtime", handler=_simple("cmd_audit_ledger")),
        make_spec(
            "validate-db",
            domain="runtime",
            handler=_validate_db,
            help="validate operational DB schema without applying repair",
            build=lambda p: p.add_argument("--json", action="store_true"),
            json_output_supported=True,
        ),
        make_spec(
            "config-dump",
            domain="runtime",
            handler=_config_dump,
            help="show bootstrap-loaded effective config for operator validation",
            description="Print selected effective settings; use --masked for normal operator use.",
            build=lambda p: p.add_argument("--masked", action="store_true"),
        ),
        make_spec(
            "notification-diagnose",
            domain="runtime",
            handler=_notification_diagnose,
            help="inspect notification configuration or explicitly send a probe",
            description="Print masked notification configuration; --probe explicitly attempts delivery.",
            build=_build_notification_diagnose,
            json_output_supported=True,
        ),
        make_spec("orders", domain="runtime", handler=_with_limit("cmd_orders"), build=_limit(50)),
        make_spec("fills", domain="runtime", handler=_with_limit("cmd_fills"), build=_limit(50)),
        make_spec("trades", domain="runtime", handler=_with_limit("cmd_trades"), build=_limit(20)),
        make_spec(
            "run",
            domain="runtime",
            handler=_run,
            read_only=False,
            mutating=True,
            guard_policy="live_run_loop",
            writes_db=True,
            uses_broker=True,
        ),
        make_spec(
            "live-dry-run",
            domain="runtime",
            handler=_live_dry_run,
            help="run one live no-submit decision cycle",
            description="Validate live decision flow, target_delta plan, and performance gate without broker submission.",
            read_only=False,
            mutating=True,
            guard_policy="live_dry_run_loop",
            writes_db=True,
            uses_broker=True,
        ),
        make_spec(
            "h74-clear-non-authoritative-state",
            domain="runtime",
            handler=_h74_clear_non_authoritative_state,
            help="clear stale H74 target/virtual state after flat preconditions and backup",
            description=(
                "Delete only pair-scoped target_position_state and H74 strategy_virtual_target_state. "
                "Refuses non-flat, risky-order, or broker-unverified cleanup unless explicitly allowed."
            ),
            build=_build_h74_clear_non_authoritative_state,
            read_only=False,
            mutating=True,
            writes_db=True,
            produces_artifact=True,
        ),
        make_spec(
            "h74-no-window-probe",
            domain="runtime",
            handler=_h74_no_window_probe,
            help="validate no-window H74 probe pre-submit evidence before execution",
            build=_build_h74_no_window_probe,
            read_only=True,
            produces_artifact=True,
        ),
        make_spec(
            "runtime-strategy-set-lint",
            domain="runtime",
            handler=_runtime_strategy_set_lint,
            help="validate the active runtime strategy set without placing orders",
            description="Validate and materialize the active runtime strategy set using startup validation.",
        ),
        make_spec(
            "runtime-strategy-set-dump",
            domain="runtime",
            handler=_runtime_strategy_set_dump,
            help="print the normalized active runtime strategy-set manifest",
            description="Validate and print the materialized active runtime strategy set without placing orders.",
            build=lambda p: p.add_argument("--pretty", action="store_true"),
            json_output_supported=True,
        ),
    ]


def _build_notification_diagnose(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument(
        "--notification-policy",
        choices=("best_effort", "require_delivery", "disabled"),
        help="policy label to include in diagnostic output",
    )


def _build_h74_clear_non_authoritative_state(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pair", required=True)
    parser.add_argument("--require-flat", action="store_true")
    parser.add_argument("--backup", action="store_true", help="required; cleanup always writes a backup artifact")
    parser.add_argument("--allow-broker-unverified", action="store_true")


def _build_h74_no_window_probe(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pre-submit-evidence", required=True)
    parser.add_argument("--probe-run-id", required=True)
    parser.add_argument("--db")
    parser.add_argument("--pair", default="KRW-BTC")
    parser.add_argument("--min-executable-qty", type=float, default=0.0)
    parser.add_argument("--restore-authority")
