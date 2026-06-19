from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bithumb_bot.approved_profile import ApprovedProfileError, validate_approved_profile
from bithumb_bot.operator_smoke_authority import (
    OperatorSmokeAuthorityError,
    build_operator_smoke_authority_payload,
    verify_operator_smoke_authority,
)


def test_smoke_authority_declares_not_promotion_evidence() -> None:
    payload = build_operator_smoke_authority_payload(
        expires_at=datetime.now(timezone.utc) + timedelta(days=1)
    )

    assert payload["promotion_evidence"] is False
    assert payload["approved_profile_evidence"] is False
    assert payload["strategy_performance_evidence"] is False
    assert payload["promotion_grade"] is False


def test_operator_smoke_authority_not_accepted_as_approved_profile() -> None:
    payload = build_operator_smoke_authority_payload(
        expires_at=datetime.now(timezone.utc) + timedelta(days=1)
    )

    with pytest.raises(ApprovedProfileError):
        validate_approved_profile(payload)


def test_operator_smoke_authority_not_accepted_by_profile_generate() -> None:
    payload = build_operator_smoke_authority_payload(
        expires_at=datetime.now(timezone.utc) + timedelta(days=1)
    )

    with pytest.raises(ApprovedProfileError):
        validate_approved_profile(payload)


def test_smoke_authority_expired_blocks_smoke_buy() -> None:
    payload = build_operator_smoke_authority_payload(
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
    )

    with pytest.raises(OperatorSmokeAuthorityError, match="operator_smoke_authority_expired"):
        verify_operator_smoke_authority(payload, now=datetime.now(timezone.utc), side="BUY", notional_krw=50_000)
