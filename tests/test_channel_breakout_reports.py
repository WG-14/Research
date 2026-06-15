from __future__ import annotations

import pytest

from bithumb_bot.research.channel_breakout_reports import build_rootcause_report, classify_acceptance


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


def _acceptance_summary(**candidate_overrides: object) -> dict[str, object]:
    candidate = {
        "variant_role": "candidate",
        "avg_return_pct": 1.0,
        "positive_periods": 2,
        "period_count": 3,
        "sum_reclaim_pnl": 100.0,
        "sum_max_hold_pnl": 50.0,
        "sum_trades": 40,
        "policy_mismatch_sum": 0,
        "first_entry_notional": 99_000.0,
    }
    candidate.update(candidate_overrides)
    return {
        "summary_rows": [
            {
                "variant_role": "control",
                "avg_return_pct": -2.0,
                "sum_reclaim_pnl": -100.0,
                "sum_max_hold_pnl": 50.0,
                "sum_trades": 100,
                "policy_mismatch_sum": 0,
                "first_entry_notional": 99_000.0,
            },
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
