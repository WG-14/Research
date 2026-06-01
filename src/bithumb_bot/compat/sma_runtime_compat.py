from __future__ import annotations

import os

from bithumb_bot.research.strategy_registry import runtime_strategy_parameter_env_keys
from bithumb_bot.research.strategy_spec import SMA_WITH_FILTER_SPEC


LEGACY_DEFAULT_RUNTIME_STRATEGY = SMA_WITH_FILTER_SPEC.strategy_name
LEGACY_DEFAULT_STRATEGY_COMPAT_ENV = "LEGACY_DEFAULT_STRATEGY_COMPAT"


def legacy_default_strategy_compat_enabled_from_env() -> bool:
    raw = os.getenv(LEGACY_DEFAULT_STRATEGY_COMPAT_ENV, "")
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def legacy_default_strategy_name() -> str:
    return LEGACY_DEFAULT_RUNTIME_STRATEGY


def legacy_sma_strategy_parameter_env_keys() -> tuple[str, ...]:
    return runtime_strategy_parameter_env_keys(LEGACY_DEFAULT_RUNTIME_STRATEGY)


def legacy_default_strategy_allowed_for_contract(
    *,
    mode: object,
    live_dry_run: object,
    live_real_order_armed: object,
) -> bool:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode == "live":
        return False
    return True


def strategy_name_from_legacy_compat_env() -> str:
    if legacy_default_strategy_compat_enabled_from_env():
        return legacy_default_strategy_name()
    return ""


def explicit_legacy_contract_source_label() -> str:
    return "explicit_legacy_sma_compat"
