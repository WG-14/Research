from __future__ import annotations

from bithumb_bot.runtime.operator_event_composer import RuntimeOperatorEventComposer


def test_daily_participation_event_does_not_claim_fill_guarantee() -> None:
    event = RuntimeOperatorEventComposer("KRW-BTC").daily_participation_status_event(
        count_basis="filled",
        days_with_intent=3,
        days_with_filled_execution=1,
        zero_filled_days=2,
        max_consecutive_zero_filled_days=2,
        target_status="FAIL",
    )

    assert event["not_a_fill_guarantee"] is True
    assert "guarantee" in event["operator_compact_summary"]
    assert "fill guarantee" not in event["operator_compact_summary"]

