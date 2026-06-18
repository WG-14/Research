from __future__ import annotations

from typing import Any, Mapping


DAILY_PARTICIPATION_DIAGNOSTIC_COUNT_DEFAULTS = {
    "fallback_intent_count": 0,
    "fallback_submit_expected_count": 0,
    "fallback_submitted_count": 0,
    "fallback_filled_count": 0,
    "fallback_closed_trade_count": 0,
    "base_sma_buy_count": 0,
}


def daily_participation_diagnostics_count_builder(payload: Mapping[str, Any]) -> dict[str, object]:
    final_signal = str(payload.get("final_signal") or payload.get("signal") or "").upper()
    source = str(payload.get("entry_signal_source") or "").strip()
    submit_expected = bool(payload.get("submit_expected", final_signal == "BUY"))
    counts = dict(DAILY_PARTICIPATION_DIAGNOSTIC_COUNT_DEFAULTS)
    if final_signal == "BUY" and source == "daily_participation_fallback":
        counts["fallback_intent_count"] = 1
        counts["fallback_submit_expected_count"] = 1 if submit_expected else 0
    elif final_signal == "BUY" and source in {"sma_cross", "base_sma"}:
        counts["base_sma_buy_count"] = 1
    return {
        "strategy_diagnostic_count_taxonomy": "daily_participation_entry_signal_source_v1",
        "strategy_diagnostic_count_defaults": DAILY_PARTICIPATION_DIAGNOSTIC_COUNT_DEFAULTS,
        "strategy_diagnostic_counts": counts,
    }


__all__ = [
    "DAILY_PARTICIPATION_DIAGNOSTIC_COUNT_DEFAULTS",
    "daily_participation_diagnostics_count_builder",
]
