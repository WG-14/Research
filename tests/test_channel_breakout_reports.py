from __future__ import annotations

import pytest

from bithumb_bot.research.channel_breakout_reports import (
    build_rootcause_report,
    classify_acceptance,
    validate_paired_ab_summary,
)


def _trade(
    *,
    net_pnl: float,
    exit_reason: str,
    holding_minutes: float,
    mfe_pct: float = 0.02,
    mae_pct: float = -0.01,
) -> dict[str, object]:
    return {
        "entry_ts": 0,
        "exit_ts": int(holding_minutes * 60_000),
        "net_pnl": net_pnl,
        "exit_reason": exit_reason,
        "holding_minutes": holding_minutes,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
    }


def test_rootcause_report_groups_by_exit_reason_and_holding_bucket() -> None:
    report = build_rootcause_report(
        {
            "rows": [
                {
                    "variant": "control",
                    "period": "2026-01",
                    "closed_trades": [
                        _trade(net_pnl=-100.0, exit_reason="breakout_level_reclaim_failed", holding_minutes=4),
                        _trade(net_pnl=200.0, exit_reason="max_holding_time", holding_minutes=20),
                    ],
                },
                {
                    "variant": "candidate",
                    "period": "2026-01",
                    "closed_trades": [
                        _trade(net_pnl=150.0, exit_reason="breakout_level_reclaim_failed", holding_minutes=8),
                    ],
                },
            ]
        }
    )

    assert "variant_summary" in report
    assert "period_variant_summary" in report
    assert "exit_reason_summary" in report
    assert "holding_bucket_summary" in report
    buckets = {row["holding_bucket"]: row for row in report["holding_bucket_summary"]}  # type: ignore[index]
    assert "00-05m" in buckets
    assert buckets["00-05m"]["reclaim_count"] == 1
    candidate = next(row for row in report["variant_summary"] if row["variant"] == "candidate")  # type: ignore[index]
    assert candidate["win_rate"] == 1.0
    assert candidate["reclaim_pnl"] == 150.0


def test_rootcause_report_fails_without_required_closed_trade_fields() -> None:
    with pytest.raises(ValueError, match="mfe_pct"):
        build_rootcause_report(
            {
                "rows": [
                    {
                        "variant": "candidate",
                        "period": "2026-01",
                        "closed_trades": [{"net_pnl": 1.0, "exit_reason": "max_holding_time", "holding_minutes": 1}],
                    }
                ]
            }
        )


def _paired_row(**overrides: object) -> dict[str, object]:
    row = {
        "variant_role": "candidate",
        "period": "2026-01-clean",
        "market": "KRW-BTC",
        "interval": "1m",
        "execution_scenario": "base",
        "cost_model_hash": "sha256:cost",
        "portfolio_policy_hash": "sha256:portfolio",
        "readiness_status": "PASS",
        "final_holdout_missing_count": 0,
        "final_holdout_interval_mismatch_count": 0,
        "quality_status": "PASS",
        "coverage_pct": 100.0,
        "avg_return_pct": 1.0,
        "positive_periods": 3,
        "period_count": 3,
        "sum_reclaim_pnl": 100.0,
        "sum_max_hold_pnl": 50.0,
        "sum_trades": 40,
        "policy_mismatch_sum": 0,
        "first_entry_notional": 99_000.0,
        "first_entry_notional_approximately_99000": True,
    }
    row.update(overrides)
    return row


def _control_row(**overrides: object) -> dict[str, object]:
    row = _paired_row(
        variant_role="control",
        avg_return_pct=-2.0,
        positive_periods=0,
        sum_reclaim_pnl=-100.0,
        sum_max_hold_pnl=50.0,
        sum_trades=100,
    )
    row.pop("period_count")
    row.update(overrides)
    return row


def _acceptance_summary(**candidate_overrides: object) -> dict[str, object]:
    candidate = {
        **_paired_row(),
    }
    candidate.update(candidate_overrides)
    return {
        "summary_rows": [
            _control_row(),
            candidate,
        ]
    }


def test_channel_breakout_acceptance_success_requires_all_gates() -> None:
    result = classify_acceptance(_acceptance_summary())

    assert result["classification"] == "success"
    assert result["blockers"] == []


def test_channel_breakout_acceptance_fails_when_avg_return_negative() -> None:
    result = classify_acceptance(_acceptance_summary(avg_return_pct=-0.0323, positive_periods=1))

    assert result["classification"] == "loss_reduction_only"
    assert "avg_return_pct_not_positive" in result["blockers"]
    assert "positive_periods_below_two_thirds" in result["blockers"]


def test_channel_breakout_acceptance_fails_on_policy_mismatch() -> None:
    result = classify_acceptance(_acceptance_summary(policy_mismatch_sum=1))

    assert result["classification"] == "fail"
    assert "policy_mismatch" in result["blockers"]


def test_channel_breakout_acceptance_fails_when_trade_count_collapses() -> None:
    result = classify_acceptance(_acceptance_summary(sum_trades=20))

    assert result["classification"] == "fail"
    assert "trade_count_collapse" in result["blockers"]
    assert result["trade_collapse_threshold"] == 25.0


def test_channel_breakout_paired_ab_summary_rejects_candidate_without_matching_control() -> None:
    result = classify_acceptance({"summary_rows": [_paired_row(period="candidate-only")]})

    assert result["classification"] == "fail"
    assert "missing_matching_control_row:candidate-only" in result["blockers"]


def test_channel_breakout_paired_ab_summary_rejects_readiness_fail_window() -> None:
    result = classify_acceptance(_acceptance_summary(readiness_status="FAIL"))

    assert result["classification"] == "fail"
    assert any(blocker.startswith("readiness_status_not_pass:") for blocker in result["blockers"])


def test_channel_breakout_paired_ab_summary_rejects_missing_candles() -> None:
    result = classify_acceptance(_acceptance_summary(final_holdout_missing_count=1))

    assert result["classification"] == "fail"
    assert any(blocker.startswith("final_holdout_missing_count_nonzero:") for blocker in result["blockers"])


def test_channel_breakout_paired_ab_summary_rejects_interval_mismatch() -> None:
    result = classify_acceptance(_acceptance_summary(final_holdout_interval_mismatch_count=1))

    assert result["classification"] == "fail"
    assert any(
        blocker.startswith("final_holdout_interval_mismatch_count_nonzero:")
        for blocker in result["blockers"]
    )


def test_channel_breakout_paired_ab_summary_requires_policy_mismatch_sum() -> None:
    candidate = _paired_row()
    candidate.pop("policy_mismatch_sum")
    result = classify_acceptance({"summary_rows": [_control_row(), candidate]})

    assert result["classification"] == "fail"
    assert "missing_required_acceptance_field:policy_mismatch_sum" in result["blockers"]


def test_channel_breakout_paired_ab_summary_requires_first_entry_notional_verification() -> None:
    candidate = _paired_row()
    candidate.pop("first_entry_notional_approximately_99000")
    result = classify_acceptance({"summary_rows": [_control_row(), candidate]})

    assert result["classification"] == "fail"
    assert any(
        "missing_required_summary_field:" in blocker
        and blocker.endswith(":first_entry_notional_approximately_99000")
        for blocker in result["blockers"]
    )


def test_channel_breakout_paired_ab_summary_validator_normalizes_valid_rows() -> None:
    result = validate_paired_ab_summary(_acceptance_summary())

    assert result["blockers"] == []
    assert len(result["summary_rows"]) == 2


def test_channel_breakout_acceptance_fails_when_policy_mismatch_sum_missing() -> None:
    candidate = _paired_row()
    candidate.pop("policy_mismatch_sum")
    result = classify_acceptance({"summary_rows": [_control_row(), candidate]})

    assert result["classification"] == "fail"
    assert "missing_required_acceptance_field:policy_mismatch_sum" in result["blockers"]


@pytest.mark.parametrize("first_entry_notional", [97_000.0, 101_500.0])
def test_channel_breakout_acceptance_fails_when_first_entry_notional_not_approximately_99000(
    first_entry_notional: float,
) -> None:
    result = classify_acceptance(_acceptance_summary(first_entry_notional=first_entry_notional))

    assert result["classification"] == "fail"
    assert "first_entry_notional_not_approximately_99000" in result["blockers"]


def test_channel_breakout_acceptance_fails_when_reclaim_pnl_not_improved() -> None:
    result = classify_acceptance(_acceptance_summary(sum_reclaim_pnl=-101.0))

    assert result["classification"] == "fail"
    assert "sum_reclaim_pnl_not_improved" in result["blockers"]


def test_channel_breakout_acceptance_fails_when_max_hold_pnl_worse() -> None:
    result = classify_acceptance(_acceptance_summary(sum_max_hold_pnl=49.0))

    assert result["classification"] == "fail"
    assert "sum_max_hold_pnl_worse" in result["blockers"]


def test_channel_breakout_acceptance_reports_missing_required_field_blocker() -> None:
    candidate = _paired_row()
    candidate.pop("avg_return_pct")
    result = classify_acceptance({"summary_rows": [_control_row(), candidate]})

    assert result["classification"] == "fail"
    assert "missing_required_acceptance_field:avg_return_pct" in result["blockers"]
