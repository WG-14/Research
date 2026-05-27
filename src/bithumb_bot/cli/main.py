from __future__ import annotations

from .context import AppContext, build_default_context
from .dispatch import dispatch
from .parser import build_parser
from .registry import command_registry


def main(argv: list[str] | None = None, context: AppContext | None = None) -> int:
    registry = command_registry()
    parser = build_parser(registry)
    args = parser.parse_args(argv)
    app_context = context or build_default_context(argv)
    _validate_mode_and_log_live_contract(args, app_context)
    return dispatch(args, app_context, registry)


def _validate_mode_and_log_live_contract(args: object, context: AppContext) -> None:
    from bithumb_bot.config import ModeValidationError, log_live_execution_contract, validate_mode_or_raise

    settings = context.settings
    try:
        validate_mode_or_raise(settings.MODE)
    except ModeValidationError as exc:
        context.printer(f"[MODE] {exc}")
        raise SystemExit(1) from exc
    if settings.MODE != "live":
        return
    env_summary = context.env_summary
    env_summary_payload = env_summary.as_dict() if hasattr(env_summary, "as_dict") else {}
    log_live_execution_contract(
        settings,
        caller=f"cli.main:{getattr(args, 'cmd', None) or 'ticker'}",
        env_summary=env_summary_payload,
    )


if __name__ == "__main__":
    raise SystemExit(main())
