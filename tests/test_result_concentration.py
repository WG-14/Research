from __future__ import annotations

from datetime import datetime, timezone

import pytest

from market_research.research.result_concentration import (
    ResultConcentrationError,
    analyze_trade_concentration,
)
from market_research.research.validation_protocol import (
    _closed_trade_diagnostics_summary,
)


def _ts(value: str) -> int:
    return int(
        datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000
    )


def test_concentration_explains_trade_period_regime_instrument_and_calendar_contribution() -> (
    None
):
    result = analyze_trade_concentration(
        (
            {
                "net_pnl": 12.0,
                "exit_ts": _ts("2024-01-01T12:00:00"),
                "entry_regime": "uptrend",
                "instrument_id": "instrument-a",
            },
            {
                "net_pnl": -5.0,
                "exit_ts": _ts("2025-02-04T12:00:00"),
                "entry_regime": "downtrend",
                "instrument_id": "instrument-b",
            },
            {
                "net_pnl": 1.0,
                "exit_ts": _ts("2025-02-05T12:00:00"),
                "entry_regime": "downtrend",
                "instrument_id": "instrument-b",
            },
        )
    )

    assert result["trade_count"] == 3
    assert result["total_net_pnl"] == 8.0
    assert [row["bucket"] for row in result["contribution_by_exit_year"]] == [
        "2024",
        "2025",
    ]
    assert {row["bucket"] for row in result["contribution_by_entry_regime"]} == {
        "downtrend",
        "uptrend",
    }
    assert {row["bucket"] for row in result["contribution_by_instrument"]} == {
        "instrument-a",
        "instrument-b",
    }
    assert {row["bucket"] for row in result["contribution_by_exit_weekday"]} == {
        "monday",
        "tuesday",
        "wednesday",
    }
    assert {row["bucket"] for row in result["contribution_by_exit_month"]} == {
        "2024-01",
        "2025-02",
    }
    assert result["top_positive_trade_contribution"][0] == {
        "requested_trade_count": 1,
        "removed_trade_count": 1,
        "removed_net_pnl": 12.0,
        "share_of_gross_profit_pct": pytest.approx(12.0 / 13.0 * 100.0),
        "remaining_net_pnl": -4.0,
        "remaining_result_positive": False,
    }
    assert "net_result_depends_on_top_1_positive_trades" in result["warnings"]


def test_concentration_retains_missing_time_as_explicit_unavailable_bucket() -> None:
    result = analyze_trade_concentration(
        ({"net_pnl": 0.0, "exit_ts": None, "entry_regime": None},)
    )

    assert result["missing_exit_timestamp_count"] == 1
    assert result["contribution_by_exit_year"][0]["bucket"] == "unavailable"
    assert (
        result["contribution_by_instrument"][0]["bucket"] == "single_instrument_scope"
    )


def test_validation_report_embeds_concentration_evidence() -> None:
    diagnostics = _closed_trade_diagnostics_summary(
        {
            "market": "KRW-BTC",
            "validation_closed_trades": [
                {"net_pnl": 2.0, "exit_ts": _ts("2025-01-01T00:00:00")},
                {"net_pnl": -1.0, "exit_ts": _ts("2025-01-02T00:00:00")},
            ],
        }
    )

    concentration = diagnostics["concentration_analysis"]
    assert concentration["trade_count"] == 2
    assert concentration["contribution_by_instrument"] == [
        {
            "bucket": "KRW-BTC",
            "trade_count": 2,
            "net_pnl": 1.0,
            "share_of_total_net_pnl_pct": 100.0,
        }
    ]


@pytest.mark.parametrize(
    "trade",
    (
        {"net_pnl": float("nan"), "exit_ts": 0},
        {"net_pnl": "1", "exit_ts": 0},
        {"net_pnl": 1.0, "exit_ts": -1},
    ),
)
def test_concentration_rejects_invalid_trade_evidence(trade: dict[str, object]) -> None:
    with pytest.raises(ResultConcentrationError):
        analyze_trade_concentration((trade,))
