from __future__ import annotations

from types import SimpleNamespace

from bithumb_bot.h74_authority_alignment import (
    H74_AUTHORITY_ENV_BEHAVIOR_MISMATCH,
    validate_h74_authority_env_alignment,
)
import bithumb_bot.h74_authority_alignment as authority_alignment
from bithumb_bot.h74_observation import H74_SOURCE_OBSERVATION_PARAMETERS
from tests.test_h74_source_variant_authority import _source, _variant


def _settings(start: int, end: int) -> object:
    values = dict(H74_SOURCE_OBSERVATION_PARAMETERS)
    values.update(
        {
            "MODE": "live",
            "LIVE_DRY_RUN": True,
            "LIVE_REAL_ORDER_ARMED": False,
            "STRATEGY_NAME": "daily_participation_sma",
            "PAIR": "KRW-BTC",
            "INTERVAL": "1m",
            "MAX_DAILY_ORDER_COUNT": 2,
            "MAX_ORDER_KRW": 100_000,
            "DAILY_PARTICIPATION_WINDOW_START_HOUR_KST": start,
            "DAILY_PARTICIPATION_WINDOW_END_HOUR_KST": end,
        }
    )
    return SimpleNamespace(**values)


def test_source_authority_rejects_no_window_env() -> None:
    result = validate_h74_authority_env_alignment(_source(), settings_obj=_settings(0, 24), raise_on_mismatch=False)
    assert not result.ok
    assert result.reason_code == H74_AUTHORITY_ENV_BEHAVIOR_MISMATCH
    assert "DAILY_PARTICIPATION_WINDOW_START_HOUR_KST" in result.mismatched_keys


def test_no_window_variant_authority_accepts_no_window_env() -> None:
    result = validate_h74_authority_env_alignment(_variant(), settings_obj=_settings(0, 24), raise_on_mismatch=False)
    assert result.ok


def test_no_window_variant_authority_rejects_source_env() -> None:
    result = validate_h74_authority_env_alignment(_variant(), settings_obj=_settings(9, 11), raise_on_mismatch=False)
    assert not result.ok
    assert "DAILY_PARTICIPATION_WINDOW_END_HOUR_KST" in result.mismatched_keys


def test_lint_reports_h74_authority_env_behavior_mismatch_reason_code() -> None:
    result = validate_h74_authority_env_alignment(_source(), settings_obj=_settings(0, 24), raise_on_mismatch=False)
    payload = result.as_dict()
    assert payload["reason_code"] == H74_AUTHORITY_ENV_BEHAVIOR_MISMATCH
    assert "DAILY_PARTICIPATION_WINDOW_START_HOUR_KST" in payload["mismatched_keys"]


def test_alignment_uses_runtime_adapter_materialized_behavior(monkeypatch) -> None:
    cfg = _settings(9, 11)
    materialized = dict(H74_SOURCE_OBSERVATION_PARAMETERS)
    materialized["DAILY_PARTICIPATION_WINDOW_START_HOUR_KST"] = 0
    materialized["DAILY_PARTICIPATION_WINDOW_END_HOUR_KST"] = 24
    monkeypatch.setattr(
        authority_alignment,
        "h74_runtime_adapter_materialized_values_from_settings",
        lambda _settings_obj: materialized,
    )

    result = validate_h74_authority_env_alignment(_source(), settings_obj=cfg, raise_on_mismatch=False)

    assert not result.ok
    assert result.reason_code == H74_AUTHORITY_ENV_BEHAVIOR_MISMATCH
    assert result.raw_settings_parameters["DAILY_PARTICIPATION_WINDOW_START_HOUR_KST"] == 9
    assert result.effective_behavior_parameters["DAILY_PARTICIPATION_WINDOW_START_HOUR_KST"] == 0


def test_lint_reports_adapter_materialized_mismatched_keys(monkeypatch) -> None:
    materialized = dict(H74_SOURCE_OBSERVATION_PARAMETERS)
    materialized["DAILY_PARTICIPATION_WINDOW_START_HOUR_KST"] = 0
    materialized["DAILY_PARTICIPATION_WINDOW_END_HOUR_KST"] = 24
    monkeypatch.setattr(
        authority_alignment,
        "h74_runtime_adapter_materialized_values_from_settings",
        lambda _settings_obj: materialized,
    )

    result = validate_h74_authority_env_alignment(_source(), settings_obj=_settings(9, 11), raise_on_mismatch=False)

    assert result.reason_code == H74_AUTHORITY_ENV_BEHAVIOR_MISMATCH
    assert "DAILY_PARTICIPATION_WINDOW_START_HOUR_KST" in result.mismatched_keys
    assert "DAILY_PARTICIPATION_WINDOW_END_HOUR_KST" in result.mismatched_keys
