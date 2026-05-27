from __future__ import annotations

import argparse

from bithumb_bot.cli.registry import CommandSpec

from ._helpers import call_app_impl, make_spec


def _settings_default(name: str):
    from bithumb_bot.config import settings

    return getattr(settings, name)


def _simple(function_name: str):
    def _handler(_args: argparse.Namespace, _context) -> int | None:
        return call_app_impl(function_name)

    return _handler


def _with_limit(function_name: str, attr: str = "limit", *, minimum: int | None = None):
    def _handler(args: argparse.Namespace, _context) -> int | None:
        value = int(getattr(args, attr))
        if minimum is not None:
            value = max(minimum, value)
        return call_app_impl(function_name, value)

    return _handler


def _signal(args: argparse.Namespace, _context) -> None:
    call_app_impl("cmd_signal", args.short, args.long)


def _explain(args: argparse.Namespace, _context) -> None:
    call_app_impl("cmd_explain", args.short, args.long)


def _audit(_args: argparse.Namespace, _context) -> None:
    call_app_impl("cmd_audit")


def _config_dump(args: argparse.Namespace, _context) -> None:
    call_app_impl("cmd_config_dump", masked=bool(args.masked))


def _validate_db(args: argparse.Namespace, _context) -> int:
    return int(call_app_impl("cmd_validate_db", as_json=bool(args.json)))


def _run(args: argparse.Namespace, _context) -> None:
    call_app_impl("cmd_run", args.short, args.long)


def _live_dry_run(args: argparse.Namespace, _context) -> None:
    call_app_impl("cmd_live_dry_run", args.short, args.long)


def _build_window_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--short", type=int, default=_settings_default("SMA_SHORT"))
    parser.add_argument("--long", type=int, default=_settings_default("SMA_LONG"))


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
        make_spec("orders", domain="runtime", handler=_with_limit("cmd_orders"), build=_limit(50)),
        make_spec("fills", domain="runtime", handler=_with_limit("cmd_fills"), build=_limit(50)),
        make_spec("trades", domain="runtime", handler=_with_limit("cmd_trades"), build=_limit(20)),
        make_spec(
            "run",
            domain="runtime",
            handler=_run,
            build=_build_window_parser,
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
            build=_build_window_parser,
            read_only=False,
            mutating=True,
            guard_policy="live_dry_run_loop",
            writes_db=True,
            uses_broker=True,
        ),
    ]
