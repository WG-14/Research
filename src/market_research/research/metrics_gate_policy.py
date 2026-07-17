from __future__ import annotations

from typing import Any

from .hashing import sha256_prefixed
from .metrics_contract import METRICS_SCHEMA_VERSION


METRICS_GATE_POLICY_FIELDS = (
    "metrics_schema_version",
    "min_cagr_pct",
    "min_expectancy_per_trade_krw",
    "min_expectancy_per_trade_pct",
    "max_exposure_time_pct",
    "max_avg_holding_time_minutes",
    "max_fee_drag_ratio",
    "max_slippage_drag_ratio",
    "max_single_trade_dependency_score",
    "reject_open_position_at_end",
    "metrics_contract_required",
    "min_trade_days_pct",
    "max_zero_filled_days",
    "max_consecutive_zero_filled_days",
    "min_filled_execution_per_kst_day",
    "participation_count_basis",
)


def metrics_gate_policy_from_acceptance_gate(gate: Any) -> dict[str, object]:
    return {
        "metrics_schema_version": METRICS_SCHEMA_VERSION,
        "min_cagr_pct": getattr(gate, "min_cagr_pct", None),
        "min_expectancy_per_trade_krw": getattr(
            gate, "min_expectancy_per_trade_krw", None
        ),
        "min_expectancy_per_trade_pct": getattr(
            gate, "min_expectancy_per_trade_pct", None
        ),
        "max_exposure_time_pct": getattr(gate, "max_exposure_time_pct", None),
        "max_avg_holding_time_minutes": getattr(
            gate, "max_avg_holding_time_minutes", None
        ),
        "max_fee_drag_ratio": getattr(gate, "max_fee_drag_ratio", None),
        "max_slippage_drag_ratio": getattr(gate, "max_slippage_drag_ratio", None),
        "max_single_trade_dependency_score": getattr(
            gate, "max_single_trade_dependency_score", None
        ),
        "reject_open_position_at_end": bool(
            getattr(gate, "reject_open_position_at_end", False)
        ),
        "metrics_contract_required": bool(
            getattr(gate, "metrics_contract_required", False)
        ),
        "min_trade_days_pct": getattr(gate, "min_trade_days_pct", None),
        "max_zero_filled_days": getattr(gate, "max_zero_filled_days", None),
        "max_consecutive_zero_filled_days": getattr(
            gate, "max_consecutive_zero_filled_days", None
        ),
        "min_filled_execution_per_kst_day": getattr(
            gate, "min_filled_execution_per_kst_day", None
        ),
        "participation_count_basis": getattr(gate, "participation_count_basis", None),
    }


def metrics_gate_policy_hash(policy: dict[str, Any]) -> str:
    return sha256_prefixed({key: policy.get(key) for key in METRICS_GATE_POLICY_FIELDS})


def metrics_gate_policy_summary(
    policy: dict[str, Any] | None,
) -> dict[str, object] | None:
    if not isinstance(policy, dict):
        return None
    return {key: policy.get(key) for key in METRICS_GATE_POLICY_FIELDS}
