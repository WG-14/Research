from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

from .approved_profile import (
    LEGACY_PROFILE_SELECTOR_ENV,
    expected_profile_modes_for_runtime,
    load_profile_or_promotion_regime_policy,
    runtime_contract_from_settings,
    verify_profile_against_runtime,
)
from .config import settings
from .research.strategy_spec import SMA_WITH_FILTER_SPEC


@dataclass(frozen=True)
class SmaStrategyConfig:
    short_n: int
    long_n: int
    pair: str
    interval: str
    exit_rule_names: tuple[str, ...]
    exit_stop_loss_ratio: float
    exit_max_holding_min: int
    exit_min_take_profit_ratio: float
    exit_small_loss_tolerance_ratio: float
    slippage_bps: float
    live_fee_rate_estimate: float
    entry_edge_buffer_ratio: float
    strategy_min_expected_edge_ratio: float
    buy_fraction: float
    max_order_krw: float
    candidate_regime_policy: dict[str, object] | None = None


def normalize_exit_rule_names(raw: str | Iterable[object]) -> tuple[str, ...]:
    if isinstance(raw, str):
        values = raw.split(",")
    else:
        values = raw
    return tuple(str(token).strip().lower() for token in values if str(token).strip())


def _sma_default(name: str) -> object:
    if name == "SMA_SHORT":
        return 7
    if name == "SMA_LONG":
        return 30
    return SMA_WITH_FILTER_SPEC.default_parameters[name]


def _sma_env_value(name: str) -> str | None:
    configured = getattr(settings, name, None)
    if configured is not None:
        return str(configured)
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return None
    return str(raw)


def _sma_int(name: str) -> int:
    raw = _sma_env_value(name)
    if raw is None:
        return int(_sma_default(name))
    return int(raw)


def sma_strategy_config_from_settings(
    *,
    short_n: int | None = None,
    long_n: int | None = None,
) -> SmaStrategyConfig:
    approved_profile_selector = _approved_profile_selector_from_settings()
    profile_or_candidate_path = (
        approved_profile_selector
        or str(settings.STRATEGY_CANDIDATE_PROFILE_PATH or "").strip()
        or str(getattr(settings, "H74_SOURCE_OBSERVATION_AUTHORITY_PATH", "") or "").strip()
    )
    candidate_regime_policy = _candidate_regime_policy_from_configured_profile(
        profile_or_candidate_path,
        approved_profile_path=approved_profile_selector,
    )
    return SmaStrategyConfig(
        short_n=int(_sma_int("SMA_SHORT") if short_n is None else short_n),
        long_n=int(_sma_int("SMA_LONG") if long_n is None else long_n),
        pair=str(settings.PAIR),
        interval=str(settings.INTERVAL),
        exit_rule_names=normalize_exit_rule_names(settings.STRATEGY_EXIT_RULES),
        exit_stop_loss_ratio=float(settings.STRATEGY_EXIT_STOP_LOSS_RATIO),
        exit_max_holding_min=int(settings.STRATEGY_EXIT_MAX_HOLDING_MIN),
        exit_min_take_profit_ratio=float(settings.STRATEGY_EXIT_MIN_TAKE_PROFIT_RATIO),
        exit_small_loss_tolerance_ratio=float(settings.STRATEGY_EXIT_SMALL_LOSS_TOLERANCE_RATIO),
        slippage_bps=float(settings.STRATEGY_ENTRY_SLIPPAGE_BPS),
        live_fee_rate_estimate=float(settings.LIVE_FEE_RATE_ESTIMATE),
        entry_edge_buffer_ratio=float(settings.ENTRY_EDGE_BUFFER_RATIO),
        strategy_min_expected_edge_ratio=float(settings.STRATEGY_MIN_EXPECTED_EDGE_RATIO),
        buy_fraction=float(settings.BUY_FRACTION),
        max_order_krw=float(settings.MAX_ORDER_KRW),
        candidate_regime_policy=candidate_regime_policy,
    )


def _approved_profile_selector_from_settings() -> str:
    return (
        str(settings.APPROVED_STRATEGY_PROFILE_PATH or "").strip()
        or str(settings.STRATEGY_APPROVED_PROFILE_PATH or "").strip()
    )


def _candidate_regime_policy_from_configured_profile(
    path: str,
    *,
    approved_profile_path: str | None = None,
) -> dict[str, object] | None:
    raw_path = str(path or "").strip()
    if not raw_path:
        return None
    approved_profile_path = str(approved_profile_path or "").strip()
    if str(settings.MODE or "").strip().lower() == "live" and not approved_profile_path:
        if str(getattr(settings, "STRATEGY_NAME", "") or "").strip().lower() == "daily_participation_sma":
            from .h74_observation import h74_source_observation_policy_from_settings

            h74_policy = h74_source_observation_policy_from_settings(settings)
            if h74_policy is not None:
                return h74_policy
        return {
            "_policy_load_error": "approved_profile_missing",
            "_policy_source": raw_path,
            "approved_profile_verification_ok": False,
            "approved_profile_block_reason": "approved_profile_missing",
            "approved_profile_loaded": False,
            "approved_profile_schema_hash_valid": False,
            "approved_profile_source_verified": False,
            "approved_profile_evidence_verified": False,
            "approved_profile_runtime_verified": False,
            "approved_profile_contract_scope": "legacy_regime_policy_only",
            "legacy_candidate_profile_path_used": True,
            "legacy_profile_contract_scope": "regime_policy_only",
            "legacy_profile_selector_env": LEGACY_PROFILE_SELECTOR_ENV,
        }
    if raw_path == approved_profile_path:
        from dataclasses import replace
        from .compat.sma_runtime_compat import legacy_default_strategy_name

        runtime_settings = settings
        if not str(getattr(runtime_settings, "STRATEGY_NAME", "") or "").strip():
            runtime_settings = replace(runtime_settings, STRATEGY_NAME=legacy_default_strategy_name())
        runtime = runtime_contract_from_settings(runtime_settings)
        expected_modes, mode_reason = expected_profile_modes_for_runtime(runtime)
        result = verify_profile_against_runtime(
            profile_path=raw_path,
            runtime=runtime,
            require_profile=True,
            expected_profile_modes=expected_modes,
            expected_profile_mode_reason=mode_reason,
            verify_source_promotion=True,
        )
        if not result.ok:
            return {
                "_policy_load_error": result.reason,
                "_policy_source": raw_path,
                **result.audit_fields(),
            }
    policy = load_profile_or_promotion_regime_policy(
        raw_path,
        verify_source=raw_path == approved_profile_path,
        approved_profile_contract_scope=(
            "full_approved_profile" if raw_path == approved_profile_path else "legacy_regime_policy_only"
        ),
    )
    if policy is not None:
        if raw_path == approved_profile_path:
            policy = {
                **policy,
                "legacy_candidate_profile_path_used": False,
                "approved_profile_contract_scope": "full_approved_profile",
            }
        else:
            policy = {
                **policy,
                "legacy_candidate_profile_path_used": True,
                "legacy_profile_contract_scope": "regime_policy_only",
                "approved_profile_contract_scope": "legacy_regime_policy_only",
                "legacy_profile_selector_env": LEGACY_PROFILE_SELECTOR_ENV,
            }
    return policy
