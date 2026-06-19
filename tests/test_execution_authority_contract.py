from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bithumb_bot.execution_authority import (
    APPROVED_PROFILE_AUTHORITY_TYPE,
    LIVE_OBSERVATION_AUTHORITY_TYPE,
    OPERATOR_SMOKE_AUTHORITY_TYPE,
    execution_authority_from_payload,
    validate_live_observation_authority_complete_for_runtime,
)
from bithumb_bot.h74_observation import build_h74_observation_authority_payload
from bithumb_bot.operator_smoke_authority import build_operator_smoke_authority_payload


def test_operator_smoke_authority_allows_only_operator_smoke_operations() -> None:
    payload = build_operator_smoke_authority_payload(
        expires_at=datetime.now(timezone.utc) + timedelta(days=1)
    )

    authority = execution_authority_from_payload(payload)

    assert authority.authority_type == OPERATOR_SMOKE_AUTHORITY_TYPE
    assert authority.allows("operator_smoke_buy")
    assert not authority.allows("strategy_run")
    assert authority.parameter_authority is False
    assert authority.risk_authority is False


def test_h74_observation_authority_cannot_be_used_as_approved_profile() -> None:
    authority = execution_authority_from_payload(build_h74_observation_authority_payload())

    assert authority.authority_type == LIVE_OBSERVATION_AUTHORITY_TYPE
    assert authority.allows("h74_live_observation_50k")
    assert not authority.allows("strategy_run")
    assert authority.evidence_classification == "live_observation_non_substitutive"


def test_approved_profile_authority_cannot_be_used_as_operator_smoke_without_operator_confirmation() -> None:
    authority = execution_authority_from_payload(
        {
            "artifact_type": "approved_profile",
            "profile_content_hash": "sha256:" + "a" * 64,
            "market": "KRW-BTC",
        }
    )

    assert authority.authority_type == APPROVED_PROFILE_AUTHORITY_TYPE
    assert authority.allows("strategy_run")
    assert not authority.allows("operator_smoke_buy")


def test_live_observation_authority_requires_parameter_exit_and_risk_authority() -> None:
    authority = execution_authority_from_payload(build_h74_observation_authority_payload())

    with pytest.raises(ValueError, match="requires_parameter_exit_and_risk_authority"):
        validate_live_observation_authority_complete_for_runtime(authority)
