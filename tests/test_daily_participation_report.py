from __future__ import annotations

from bithumb_bot.research.report_writer import summarize_report_candidate


def _candidate() -> dict[str, object]:
    return {
        "candidate_id": "candidate_001",
        "validation_metrics_v2": {
            "metrics_schema_version": 2,
            "participation": {
                "timezone": "Asia/Seoul",
                "count_basis": "filled",
                "calendar_day_count": 3,
                "days_with_intent": 3,
                "days_with_submit_expected": 3,
                "days_with_submitted": 2,
                "days_with_filled_execution": 1,
                "days_with_closed_trade": 1,
                "zero_filled_days": 2,
                "max_consecutive_zero_filled_days": 2,
                "daily_counts_hash": "sha256:" + "3" * 64,
            },
        },
    }


def test_report_exposes_count_basis_breakdown() -> None:
    summary = summarize_report_candidate(_candidate())

    participation = summary["participation_summary"]
    assert participation["days_with_intent"] == 3
    assert participation["days_with_submitted"] == 2
    assert participation["days_with_filled_execution"] == 1
    assert participation["days_with_closed_trade"] == 1


def test_report_marks_daily_target_not_fill_guarantee() -> None:
    summary = summarize_report_candidate(_candidate())

    assert summary["daily_participation_target"]["not_a_fill_guarantee"] is True
    assert summary["participation_summary"]["not_a_fill_guarantee"] is True


def test_zero_filled_days_visible_in_candidate_summary() -> None:
    summary = summarize_report_candidate(_candidate())

    assert summary["participation_summary"]["zero_filled_days"] == 2
    assert summary["participation_metric_hash"].startswith("sha256:")

