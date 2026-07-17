from __future__ import annotations

from collections import defaultdict
from typing import Any

from .hashing import sha256_prefixed


def build_walk_forward_selection_evidence(
    *,
    candidates: list[dict[str, Any]],
    acceptance_gate: Any,
    min_windows: int,
) -> dict[str, Any]:
    window_ids = sorted(
        {
            str(window.get("window_id"))
            for candidate in candidates
            for window in _candidate_windows(candidate)
            if window.get("window_id") is not None
        }
    )
    selected_windows: list[dict[str, Any]] = []
    previous_parameters: dict[str, Any] | None = None
    parameter_change_count = 0
    for window_id in window_ids:
        choices: list[dict[str, Any]] = []
        for candidate in candidates:
            window = next(
                (
                    item
                    for item in _candidate_windows(candidate)
                    if str(item.get("window_id")) == window_id
                ),
                None,
            )
            if window is None:
                continue
            choices.append(
                {
                    "candidate_id": str(candidate.get("candidate_id") or ""),
                    "parameter_values": dict(candidate.get("parameter_values") or {}),
                    "train_metrics": dict(window.get("train_metrics") or {}),
                    "test_metrics": dict(window.get("test_metrics") or {}),
                    "train_date_range": dict(window.get("train_date_range") or {}),
                    "test_date_range": dict(window.get("test_date_range") or {}),
                }
            )
        if not choices:
            continue
        selection_inputs = [
            {
                "candidate_id": choice["candidate_id"],
                "parameter_values": choice["parameter_values"],
                "train_metrics_hash": sha256_prefixed(choice["train_metrics"]),
            }
            for choice in sorted(choices, key=lambda item: item["candidate_id"])
        ]
        selected = min(choices, key=lambda item: _train_rank_key(item, acceptance_gate))
        selected_parameters = dict(selected["parameter_values"])
        if (
            previous_parameters is not None
            and selected_parameters != previous_parameters
        ):
            parameter_change_count += 1
        previous_parameters = selected_parameters
        test_fail_reasons = _metrics_fail_reasons(
            selected["test_metrics"], acceptance_gate
        )
        selection_input_hash = sha256_prefixed(
            {"window_id": window_id, "train_candidate_inputs": selection_inputs}
        )
        selection_artifact = {
            "window_id": window_id,
            "selection_method": "train_gate_then_return_mdd_profit_factor_candidate_id",
            "selection_input_hash": selection_input_hash,
            "selected_candidate_id": selected["candidate_id"],
            "selected_parameter_values": selected_parameters,
            "selected_train_metrics_hash": sha256_prefixed(selected["train_metrics"]),
        }
        selected_windows.append(
            {
                **selection_artifact,
                "selection_artifact_hash": sha256_prefixed(selection_artifact),
                "train_date_range": selected["train_date_range"],
                "test_date_range": selected["test_date_range"],
                "train_metrics": selected["train_metrics"],
                "test_metrics": selected["test_metrics"],
                "test_metrics_hash": sha256_prefixed(selected["test_metrics"]),
                "gate_result": "PASS" if not test_fail_reasons else "FAIL",
                "fail_reasons": test_fail_reasons,
            }
        )

    pass_count = sum(window["gate_result"] == "PASS" for window in selected_windows)
    failure_reason = None
    if len(selected_windows) < min_windows:
        failure_reason = "walk_forward_insufficient_windows"
    elif pass_count != len(selected_windows):
        failure_reason = "walk_forward_failed"
    test_returns = [
        float(window["test_metrics"].get("return_pct") or 0.0)
        for window in selected_windows
    ]
    by_year: dict[str, float] = defaultdict(float)
    for window in selected_windows:
        end = str(window.get("test_date_range", {}).get("end") or "unknown")
        by_year[end[:4] if len(end) >= 4 else "unknown"] += float(
            window["test_metrics"].get("return_pct") or 0.0
        )
    positive_total = sum(max(value, 0.0) for value in by_year.values())
    max_year_share = (
        max((max(value, 0.0) for value in by_year.values()), default=0.0)
        / positive_total
        if positive_total > 0.0
        else None
    )
    payload = {
        "selection_mode": "rolling_train_select_then_test",
        "window_count": len(selected_windows),
        "pass_window_count": pass_count,
        "fail_window_count": len(selected_windows) - pass_count,
        "mean_test_return_pct": sum(test_returns) / len(test_returns)
        if test_returns
        else None,
        "worst_test_return_pct": min(test_returns) if test_returns else None,
        "recent_window_test_return_pct": test_returns[-1] if test_returns else None,
        "selected_parameter_change_count": parameter_change_count,
        "test_return_by_year": dict(sorted(by_year.items())),
        "max_positive_test_return_year_share": max_year_share,
        "return_consistency_pass": failure_reason is None,
        "failure_reason": failure_reason,
        "windows": selected_windows,
    }
    return {**payload, "walk_forward_selection_evidence_hash": sha256_prefixed(payload)}


def _candidate_windows(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = candidate.get("walk_forward_metrics")
    windows = metrics.get("windows") if isinstance(metrics, dict) else None
    return [item for item in windows or [] if isinstance(item, dict)]


def _train_rank_key(choice: dict[str, Any], gate: Any) -> tuple[Any, ...]:
    metrics = choice["train_metrics"]
    failed = bool(_metrics_fail_reasons(metrics, gate))
    profit_factor = metrics.get("profit_factor")
    if metrics.get("profit_factor_unbounded") is True:
        profit_factor = float("inf")
    return (
        1 if failed else 0,
        -float(metrics.get("return_pct") or 0.0),
        float(metrics.get("max_drawdown_pct") or 0.0),
        -float(profit_factor or 0.0),
        choice["candidate_id"],
    )


def _metrics_fail_reasons(metrics: dict[str, Any], gate: Any) -> list[str]:
    reasons: list[str] = []
    if int(metrics.get("trade_count") or 0) < int(gate.min_trade_count):
        reasons.append("trade_count_failed")
    if float(metrics.get("max_drawdown_pct") or 0.0) > float(gate.max_mdd_pct):
        reasons.append("max_drawdown_failed")
    profit_factor = metrics.get("profit_factor")
    if metrics.get("profit_factor_unbounded") is not True and (
        profit_factor is None or float(profit_factor) < float(gate.min_profit_factor)
    ):
        reasons.append("profit_factor_failed")
    if (
        gate.oos_return_must_be_positive
        and float(metrics.get("return_pct") or 0.0) <= 0.0
    ):
        reasons.append("return_not_positive")
    return reasons
