from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bithumb_bot.h74_observation import (
    H74_OBSERVATION_PARAMETERS,
    H74ObservationAuthorityError,
    build_h74_observation_authority_payload,
    verify_h74_observation_authority,
)
from bithumb_bot.config import LiveModeValidationError, settings, validate_live_strategy_selection
from dataclasses import replace
import json


def test_h74_observation_authority_hash_binds_50k_parameters() -> None:
    payload = build_h74_observation_authority_payload()

    bound = payload["hash_bound_parameters"]
    assert bound["DAILY_PARTICIPATION_MAX_ORDER_KRW"] == 50_000
    assert payload["authority_parameter_hash"].startswith("sha256:")
    verify_h74_observation_authority(payload, runtime_values=H74_OBSERVATION_PARAMETERS)


def test_h74_observation_authority_rejects_100k_runtime_mismatch() -> None:
    payload = build_h74_observation_authority_payload()
    runtime = dict(H74_OBSERVATION_PARAMETERS)
    runtime["DAILY_PARTICIPATION_MAX_ORDER_KRW"] = 100_000

    with pytest.raises(H74ObservationAuthorityError, match="DAILY_PARTICIPATION_MAX_ORDER_KRW"):
        verify_h74_observation_authority(payload, runtime_values=runtime)


def test_h74_observation_authority_expires_after_7_days() -> None:
    payload = build_h74_observation_authority_payload(
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
    )

    with pytest.raises(H74ObservationAuthorityError, match="expired"):
        verify_h74_observation_authority(payload, runtime_values=H74_OBSERVATION_PARAMETERS)


def test_h74_observation_authority_not_accepted_as_promotion_profile() -> None:
    payload = build_h74_observation_authority_payload()

    assert payload["promotion_grade"] is False
    assert payload["research_promotion_evidence"] is False
    assert payload["approved_profile_evidence"] is False


def test_h74_observation_authority_requires_daily_window_09_11() -> None:
    payload = build_h74_observation_authority_payload()
    runtime = dict(H74_OBSERVATION_PARAMETERS)
    runtime["DAILY_PARTICIPATION_WINDOW_START_HOUR_KST"] = 0

    with pytest.raises(H74ObservationAuthorityError, match="DAILY_PARTICIPATION_WINDOW_START_HOUR_KST"):
        verify_h74_observation_authority(payload, runtime_values=runtime)


def test_h74_observation_authority_requires_holding_74() -> None:
    payload = build_h74_observation_authority_payload()
    runtime = dict(H74_OBSERVATION_PARAMETERS)
    runtime["STRATEGY_EXIT_MAX_HOLDING_MIN"] = 75

    with pytest.raises(H74ObservationAuthorityError, match="STRATEGY_EXIT_MAX_HOLDING_MIN"):
        verify_h74_observation_authority(payload, runtime_values=runtime)


def test_live_observation_authority_runtime_hook_rejects_env_mismatch(tmp_path, monkeypatch) -> None:
    authority = build_h74_observation_authority_payload()
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(authority), encoding="utf-8")
    monkeypatch.setenv("LIVE_OBSERVATION_AUTHORITY_PATH", str(path))
    cfg = replace(
        settings,
        MODE="live",
        STRATEGY_NAME="daily_participation_sma",
        PAIR="KRW-BTC",
        INTERVAL="1m",
        STRATEGY_EXIT_MAX_HOLDING_MIN=75,
        APPROVED_STRATEGY_PROFILE_PATH="",
    )

    with pytest.raises(LiveModeValidationError, match="live_observation_authority_validation_failed"):
        validate_live_strategy_selection(cfg)
