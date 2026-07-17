from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from .research_classification import requires_candidate_validation


@dataclass(frozen=True)
class ResearchRunSummary:
    candidate_gate_counts: dict[str, int]
    base_gate_counts: dict[str, int]
    stress_gate_counts: dict[str, int]
    base_fee_rate: float | None
    stress_fee_rates: tuple[float, ...]
    primary_scenario_role: str | None
    primary_metric_source: str | None
    top_fail_reasons: dict[str, int]
    top_window_fail_reasons: dict[str, int]
    walk_forward_window_count: int | None
    walk_forward_pass_window_count: int | None
    walk_forward_fail_window_count: int | None
    validation_allowed: bool
    nearest_failed_candidate_id: str | None
    nearest_failed_candidate_fail_reasons: tuple[str, ...]
    strategy_diagnostics_summary: dict[str, object]
    top_exit_reasons: dict[str, int]
    validation_raw_sell_filter_blocked_while_in_position_count: int | None
    final_holdout_raw_sell_filter_blocked_while_in_position_count: int | None
    validation_p95_mae_pct: float | None
    final_holdout_p95_mae_pct: float | None
    validation_worst_trade_mae_pct: float | None
    final_holdout_worst_trade_mae_pct: float | None
    next_action: str


def build_research_run_summary(report: dict[str, object]) -> ResearchRunSummary:
    candidates = _candidate_rows(report)
    gate_counts: Counter[str] = Counter()
    base_gate_counts: Counter[str] = Counter()
    stress_gate_counts: Counter[str] = Counter()
    base_fee_rate: float | None = None
    stress_fee_rates: set[float] = set()
    primary_scenario_role: str | None = None
    primary_metric_source: str | None = None
    fail_reasons: Counter[str] = Counter()
    window_fail_reasons: Counter[str] = Counter()
    first_walk_forward_metrics: dict[str, Any] | None = None

    for candidate in candidates:
        gate_counts[
            _safe_label(candidate.get("acceptance_gate_result"), default="UNKNOWN")
        ] += 1
        primary_scenario_role = primary_scenario_role or _safe_optional_label(
            candidate.get("primary_scenario_role")
        )
        primary_metric_source = primary_metric_source or _safe_optional_label(
            candidate.get("primary_metric_source")
        )
        scenario_results = candidate.get("scenario_results")
        if not isinstance(scenario_results, list):
            scenario_results = []
        for scenario in scenario_results:
            if not isinstance(scenario, dict):
                continue
            role = scenario.get("scenario_role")
            gate = _safe_label(
                scenario.get("scenario_acceptance_gate_result"), default="UNKNOWN"
            )
            cost_model = (
                scenario.get("cost_model")
                if isinstance(scenario.get("cost_model"), dict)
                else {}
            )
            fee_rate = _safe_float(cost_model.get("fee_rate"))
            if role == "base":
                base_gate_counts[gate] += 1
                if fee_rate is not None and base_fee_rate is None:
                    base_fee_rate = fee_rate
            elif role == "stress":
                stress_gate_counts[gate] += 1
                if fee_rate is not None:
                    stress_fee_rates.add(fee_rate)
        for reason in _string_items(candidate.get("gate_fail_reasons")):
            fail_reasons[reason] += 1

        walk_forward_metrics = candidate.get("walk_forward_metrics")
        if isinstance(walk_forward_metrics, dict):
            if first_walk_forward_metrics is None:
                first_walk_forward_metrics = walk_forward_metrics
            windows = walk_forward_metrics.get("windows")
            if isinstance(windows, list):
                for window in windows:
                    if not isinstance(window, dict):
                        continue
                    for reason in _string_items(window.get("fail_reasons")):
                        window_fail_reasons[reason] += 1

    statistical_gate_failed = (
        report.get("statistical_validation_required") is True
        and report.get("statistical_gate_result") != "PASS"
    )
    final_selection_gate_value = report.get("final_selection_gate_result")
    final_selection_gate_failed = (
        report.get("final_selection_required") is True
        or final_selection_gate_value is not None
    ) and final_selection_gate_value != "PASS"
    validation_eligibility_failed = (
        report.get("validation_eligibility_gate_result") == "FAIL"
    )
    validation_required = requires_candidate_validation(
        report.get("research_classification")
    )
    registry_gate_value = report.get("registry_gate_result")
    registry_gate_failed = validation_required and (
        registry_gate_value != "PASS"
        or not str(report.get("experiment_registry_row_hash") or "").startswith(
            "sha256:"
        )
        or bool(report.get("registry_gate_fail_reasons"))
    )
    validation_allowed = (
        bool(report.get("best_candidate_id"))
        and report.get("validation_eligibility_gate_result", report.get("gate_result"))
        == "PASS"
        and not statistical_gate_failed
        and not final_selection_gate_failed
        and not registry_gate_failed
        and not bool(report.get("diagnostic_only"))
        and str(report.get("diagnostic_mode") or "candidate_validation")
        != "exploratory"
    )
    has_pass_candidate = any(
        candidate.get("acceptance_gate_result") == "PASS" for candidate in candidates
    )
    nearest_candidate = candidates[0] if candidates and not has_pass_candidate else None
    diagnostic_candidate = _primary_candidate(report, candidates)
    diagnostics_summary = _strategy_diagnostics_summary(diagnostic_candidate or report)
    has_entry_exit_diagnostics = bool(
        diagnostics_summary.get(
            "validation_raw_sell_filter_blocked_while_in_position_count"
        )
        or diagnostics_summary.get(
            "final_holdout_raw_sell_filter_blocked_while_in_position_count"
        )
    )

    return ResearchRunSummary(
        candidate_gate_counts=_ordered_gate_counts(gate_counts) if candidates else {},
        base_gate_counts=_ordered_gate_counts(base_gate_counts)
        if base_gate_counts
        else {},
        stress_gate_counts=_ordered_gate_counts(stress_gate_counts)
        if stress_gate_counts
        else {},
        base_fee_rate=base_fee_rate,
        stress_fee_rates=tuple(sorted(stress_fee_rates)),
        primary_scenario_role=primary_scenario_role,
        primary_metric_source=primary_metric_source,
        top_fail_reasons=_ordered_counts(fail_reasons),
        top_window_fail_reasons=_ordered_counts(window_fail_reasons),
        walk_forward_window_count=_safe_int(
            first_walk_forward_metrics.get("window_count")
        )
        if first_walk_forward_metrics is not None
        else None,
        walk_forward_pass_window_count=_safe_int(
            first_walk_forward_metrics.get("pass_window_count")
        )
        if first_walk_forward_metrics is not None
        else None,
        walk_forward_fail_window_count=_safe_int(
            first_walk_forward_metrics.get("fail_window_count")
        )
        if first_walk_forward_metrics is not None
        else None,
        validation_allowed=validation_allowed,
        nearest_failed_candidate_id=_candidate_id(nearest_candidate),
        nearest_failed_candidate_fail_reasons=tuple(
            _string_items(nearest_candidate.get("gate_fail_reasons"))
        )
        if nearest_candidate is not None
        else (),
        strategy_diagnostics_summary=diagnostics_summary,
        top_exit_reasons=dict(diagnostics_summary.get("top_exit_reasons") or {}),
        validation_raw_sell_filter_blocked_while_in_position_count=_safe_int(
            diagnostics_summary.get(
                "validation_raw_sell_filter_blocked_while_in_position_count"
            )
        ),
        final_holdout_raw_sell_filter_blocked_while_in_position_count=_safe_int(
            diagnostics_summary.get(
                "final_holdout_raw_sell_filter_blocked_while_in_position_count"
            )
        ),
        validation_p95_mae_pct=_safe_float(
            diagnostics_summary.get("validation_p95_mae_pct")
        ),
        final_holdout_p95_mae_pct=_safe_float(
            diagnostics_summary.get("final_holdout_p95_mae_pct")
        ),
        validation_worst_trade_mae_pct=_safe_float(
            diagnostics_summary.get("validation_worst_trade_mae_pct")
        ),
        final_holdout_worst_trade_mae_pct=_safe_float(
            diagnostics_summary.get("final_holdout_worst_trade_mae_pct")
        ),
        next_action=_next_action(
            validation_allowed=validation_allowed,
            has_candidates=bool(candidates),
            top_fail_reasons=fail_reasons,
            gate_result="EXPLORATORY"
            if report.get("diagnostic_only")
            else report.get("gate_result"),
            statistical_gate_failed=statistical_gate_failed,
            final_selection_gate_failed=final_selection_gate_failed,
            validation_eligibility_failed=validation_eligibility_failed,
            registry_gate_failed=registry_gate_failed,
            has_entry_exit_diagnostics=has_entry_exit_diagnostics,
        ),
    )


def _candidate_rows(report: dict[str, object]) -> list[dict[str, Any]]:
    candidates = report.get("candidates")
    if not isinstance(candidates, list):
        return []
    return [candidate for candidate in candidates if isinstance(candidate, dict)]


def _string_items(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if item is not None and str(item))


def _safe_label(value: object, *, default: str) -> str:
    if value is None:
        return default
    label = str(value)
    return label if label else default


def _safe_optional_label(value: object) -> str | None:
    if value is None:
        return None
    label = str(value)
    return label if label else None


def _safe_int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return None


def _safe_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _candidate_id(candidate: dict[str, Any] | None) -> str | None:
    if candidate is None:
        return None
    value = candidate.get("parameter_candidate_id") or candidate.get("candidate_id")
    if value is None:
        return None
    candidate_id = str(value)
    return candidate_id if candidate_id else None


def _ordered_counts(counts: Counter[str]) -> dict[str, int]:
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _ordered_gate_counts(counts: Counter[str]) -> dict[str, int]:
    ordered = {"PASS": counts.get("PASS", 0), "FAIL": counts.get("FAIL", 0)}
    for key, value in sorted(counts.items()):
        if key not in ordered:
            ordered[key] = value
    return ordered


def _primary_candidate(
    report: dict[str, object], candidates: list[dict[str, Any]]
) -> dict[str, Any] | None:
    preferred_ids = [
        str(report.get("selected_candidate_id") or "").strip(),
        str(report.get("best_candidate_id") or "").strip(),
    ]
    for candidate_id in preferred_ids:
        if not candidate_id:
            continue
        for candidate in candidates:
            if _candidate_id(candidate) == candidate_id:
                return candidate
    return candidates[0] if candidates else None


def _diagnostics_dict(container: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = container.get(key)
    return dict(value) if isinstance(value, dict) else None


def _strategy_diagnostics_summary(container: dict[str, Any]) -> dict[str, object]:
    validation = (
        _diagnostics_dict(container, "validation_strategy_diagnostics")
        or _diagnostics_dict(container, "strategy_diagnostics")
        or {}
    )
    final_holdout = _diagnostics_dict(container, "final_holdout_strategy_diagnostics")
    top_exit_reasons = Counter()
    for diagnostics in (validation, final_holdout or {}):
        distribution = (
            diagnostics.get("exit_reason_distribution")
            if isinstance(diagnostics, dict)
            else None
        )
        if not isinstance(distribution, dict):
            continue
        for reason, count in distribution.items():
            top_exit_reasons[str(reason)] += int(count) if isinstance(count, int) else 0
    return {
        "top_exit_reasons": _ordered_counts(top_exit_reasons),
        "validation_raw_sell_filter_blocked_while_in_position_count": _safe_int(
            validation.get("raw_sell_filter_blocked_while_in_position_count")
        ),
        "final_holdout_raw_sell_filter_blocked_while_in_position_count": (
            _safe_int(
                final_holdout.get("raw_sell_filter_blocked_while_in_position_count")
            )
            if final_holdout is not None
            else None
        ),
        "validation_p95_mae_pct": _safe_float(validation.get("p95_mae_pct")),
        "final_holdout_p95_mae_pct": (
            _safe_float(final_holdout.get("p95_mae_pct"))
            if final_holdout is not None
            else None
        ),
        "validation_worst_trade_mae_pct": _safe_float(
            validation.get("worst_trade_mae_pct")
        ),
        "final_holdout_worst_trade_mae_pct": (
            _safe_float(final_holdout.get("worst_trade_mae_pct"))
            if final_holdout is not None
            else None
        ),
    }


def _next_action(
    *,
    validation_allowed: bool,
    has_candidates: bool,
    top_fail_reasons: Counter[str],
    gate_result: object,
    statistical_gate_failed: bool = False,
    final_selection_gate_failed: bool = False,
    validation_eligibility_failed: bool = False,
    registry_gate_failed: bool = False,
    has_entry_exit_diagnostics: bool = False,
) -> str:
    if validation_allowed:
        return "review_candidate_validation"
    if gate_result == "EXPLORATORY" or gate_result == "DIAGNOSTIC_ONLY":
        return "revise_hypothesis_from_exploratory_diagnostics"
    if final_selection_gate_failed:
        return "candidate_not_selected_review_final_selection_contract"
    if statistical_gate_failed:
        return "candidate_not_selected_review_statistical_selection"
    if registry_gate_failed:
        return "candidate_not_selected_review_experiment_registry"
    if validation_eligibility_failed:
        return "candidate_ineligible_review_blocking_reasons"
    if not has_candidates:
        return "inspect_dataset_or_manifest"
    if "walk_forward_missing" in top_fail_reasons:
        return "run_walk_forward_before_validation"
    if "walk_forward_failed" in top_fail_reasons:
        return "candidate_not_selected_review_walk_forward_windows"
    if (
        "profit_factor_failed" in top_fail_reasons
        or "min_trade_count_failed" in top_fail_reasons
    ):
        return "candidate_not_selected_revise_strategy_hypothesis"
    if has_entry_exit_diagnostics:
        return "review_entry_exit_channel_diagnostics"
    if gate_result == "FAIL":
        return "inspect_report_or_adjust_hypothesis"
    return "inspect_report_or_adjust_hypothesis"
