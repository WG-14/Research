"""Single authority for causal decision and execution timeline invariants."""

from __future__ import annotations

from typing import Any


CAUSAL_TIMELINE_VALIDATOR = "execution_invariants.v1"
MARKET_KNOWLEDGE_TIME_POLICY = (
    "event_time_lte_observed_availability_lte_portfolio_effective_time"
)


def decision_timeline_violations(
    event: Any,
    *,
    current_candle_ts: int,
    candle_available_at_ts: int,
    strategy_view_boundary_ts: int,
    expected_decision_ts: int,
) -> tuple[str, ...]:
    violations: list[str] = []
    if int(getattr(event, "candle_ts", -1)) != int(current_candle_ts):
        violations.append("strategy_decision_candle_mismatch")
    decision_ts = int(getattr(event, "decision_ts", -1))
    if int(candle_available_at_ts) > int(strategy_view_boundary_ts):
        violations.append("strategy_view_precedes_candle_availability")
    if int(strategy_view_boundary_ts) > decision_ts:
        violations.append("strategy_decision_precedes_knowledge_boundary")
    if decision_ts != int(expected_decision_ts):
        violations.append("strategy_decision_time_policy_mismatch")
    return tuple(violations)


def fill_timeline_violations(fill: Any) -> tuple[str, ...]:
    """Validate persisted fill chronology, including market knowledge times."""

    is_positive_fill = (
        getattr(fill, "fill_status", "") in {"filled", "partial"}
        and float(getattr(fill, "filled_qty", 0.0)) > 0.0
    )
    fill_reference_ts = getattr(fill, "fill_reference_ts", None)
    resolution_ts = getattr(fill, "execution_resolution_ts", None)
    reference_resolution_ts = getattr(fill, "execution_reference_resolution_ts", None)
    effective = getattr(fill, "portfolio_effective_ts", None)
    target_ts = getattr(fill, "execution_reference_target_ts", None)
    deadline_ts = getattr(fill, "execution_reference_deadline_ts", None)
    if (
        resolution_ts is None
        or reference_resolution_ts is None
        or effective is None
        or target_ts is None
        or deadline_ts is None
        or not (
            int(fill.decision_ts)
            <= int(fill.order_intent_ts)
            <= int(fill.submit_ts_assumption)
            <= int(reference_resolution_ts)
            <= int(resolution_ts)
            <= int(effective)
        )
        or int(target_ts) > int(deadline_ts)
        or not int(target_ts) <= int(reference_resolution_ts) <= int(deadline_ts)
    ):
        return ("execution_timeline_causality_violation",)
    if fill_reference_ts is not None and (
        not int(fill.submit_ts_assumption) <= int(fill_reference_ts) <= int(effective)
    ):
        return ("execution_timeline_causality_violation",)
    if is_positive_fill and fill_reference_ts is None:
        return ("execution_timeline_causality_violation",)

    violations: list[str] = []
    if (
        fill.fill_reference_policy == "next_candle_open"
        and fill.fill_reference_source != "next_candle_open"
    ):
        violations.append("next_open_fill_source_invalid")
    if fill.fill_reference_policy in {
        "first_orderbook_after_decision",
        "latency_adjusted_orderbook",
    }:
        target = (
            fill.submit_ts_assumption
            if fill.fill_reference_policy == "latency_adjusted_orderbook"
            else fill.decision_ts
        )
        if is_positive_fill and (
            fill.quote_ts is None or fill.quote_available_at_ts is None
        ):
            violations.append("orderbook_quote_knowledge_time_missing")
        elif fill.quote_ts is not None and fill.quote_available_at_ts is not None:
            if int(fill.quote_ts) < int(target):
                violations.append("orderbook_quote_event_precedes_target")
            if int(fill.quote_ts) > int(fill.quote_available_at_ts):
                violations.append("orderbook_quote_event_after_knowledge_time")
            if int(fill.quote_available_at_ts) < int(target):
                violations.append("orderbook_quote_precedes_target")
            if int(fill.quote_available_at_ts) > int(deadline_ts):
                violations.append("orderbook_quote_after_deadline")
            if fill_reference_ts is None or int(fill_reference_ts) != int(
                fill.quote_available_at_ts
            ):
                violations.append("orderbook_quote_reference_time_mismatch")

    depth_available_at = getattr(fill, "depth_snapshot_available_at_ts", None)
    depth_event_ts = getattr(fill, "depth_snapshot_ts", None)
    depth_target_ts = getattr(fill, "depth_reference_target_ts", None)
    depth_deadline_ts = getattr(fill, "depth_reference_deadline_ts", None)
    depth_resolution_ts = getattr(fill, "depth_resolution_ts", None)
    depth_consumed = int(getattr(fill, "depth_levels_consumed", 0) or 0) > 0
    if depth_available_at is not None and (
        depth_event_ts is None or int(depth_event_ts) > int(depth_available_at)
    ):
        violations.append("depth_event_after_knowledge_time")
    if depth_event_ts is not None:
        if depth_target_ts is None or int(depth_event_ts) < int(depth_target_ts):
            violations.append("depth_event_precedes_target")
        if (
            depth_available_at is None
            or depth_deadline_ts is None
            or int(depth_available_at) > int(depth_deadline_ts)
        ):
            violations.append("depth_knowledge_time_after_deadline")
        if depth_resolution_ts != depth_available_at:
            violations.append("depth_resolution_time_mismatch")
        if (
            depth_resolution_ts is None
            or int(depth_resolution_ts) > int(resolution_ts)
            or int(depth_resolution_ts) > int(effective)
            or depth_available_at is None
            or int(depth_available_at) > int(effective)
        ):
            violations.append("depth_knowledge_time_after_portfolio_effective")
    elif depth_target_ts is not None:
        if depth_deadline_ts is None or depth_resolution_ts != depth_deadline_ts:
            violations.append("depth_missing_resolution_deadline_mismatch")
        elif (
            depth_resolution_ts is None
            or int(depth_resolution_ts) > int(resolution_ts)
            or int(depth_resolution_ts) > int(effective)
        ):
            violations.append("depth_knowledge_time_after_portfolio_effective")
    if depth_consumed and depth_event_ts is None:
        violations.append("depth_consumed_without_snapshot")
    return tuple(violations)


def fill_request_binding_violations(request: Any, fill: Any) -> tuple[str, ...]:
    """Require a fill to preserve its request's causal and policy contract."""

    fields = (
        "request_id",
        "decision_id",
        "intent_id",
        "signal_ts",
        "decision_ts",
        "order_intent_ts",
        "submit_ts_assumption",
        "side",
        "order_type",
        "reference_price",
        "fill_reference_ts",
        "fill_reference_price",
        "fill_reference_source",
        "signal_candle_start_ts",
        "signal_candle_close_ts",
        "signal_reference_price",
        "signal_reference_source",
        "quote_ts",
        "quote_available_at_ts",
        "quote_availability_basis",
        "quote_source",
        "quote_age_ms",
        "depth_snapshot_ts",
        "depth_snapshot_available_at_ts",
        "depth_snapshot_availability_basis",
        "orderbook_depth_ref",
        "depth_snapshot_age_ms",
        "allow_same_candle_close_fill",
        "quote_selection",
        "fill_reference_policy",
        "top_of_book_is_full_depth",
        "latency_applied_to_reference",
        "latency_applied_to_submit_ts",
        "latency_applied_to_fill_reference",
        "execution_reference_target_ts",
        "execution_reference_deadline_ts",
        "execution_reference_resolution_ts",
        "execution_resolution_ts",
        "depth_reference_target_ts",
        "depth_reference_deadline_ts",
        "depth_resolution_ts",
        "feature_snapshot",
        "regime_snapshot",
        "entry_signal_source",
        "entry_sizing_source",
    )
    mismatches = [
        field
        for field in fields
        if getattr(request, field, None) != getattr(fill, field, None)
    ]
    return tuple(f"fill_request_field_mismatch:{field}" for field in mismatches)


__all__ = [
    "CAUSAL_TIMELINE_VALIDATOR",
    "MARKET_KNOWLEDGE_TIME_POLICY",
    "decision_timeline_violations",
    "fill_request_binding_violations",
    "fill_timeline_violations",
]
