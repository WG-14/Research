from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketWarningPolicyDecision:
    normalized_warning: str
    is_warning_state: bool
    should_block: bool


def normalize_market_warning(raw_warning: object) -> str:
    token = str(raw_warning or "").strip().upper()
    return token if token in {"NONE", "CAUTION"} else "UNKNOWN"


def evaluate_market_warning_policy(
    *, raw_warning: object, warning_block_states: set[str]
) -> MarketWarningPolicyDecision:
    normalized_warning = normalize_market_warning(raw_warning)
    return MarketWarningPolicyDecision(
        normalized_warning=normalized_warning,
        is_warning_state=normalized_warning != "NONE",
        should_block=normalized_warning != "NONE" and normalized_warning in warning_block_states,
    )
