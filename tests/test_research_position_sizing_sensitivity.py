from __future__ import annotations

from bithumb_bot.research.experiment_manifest import legacy_research_portfolio_policy
from bithumb_bot.research.validation_protocol import _position_sizing_sensitivity_summary


def _candidate() -> dict[str, object]:
    return {
        "validation_metrics_v2": {
            "total_return_pct": 10.0,
            "max_drawdown_pct": 5.0,
            "profit_factor": 2.0,
        },
        "validation_closed_trades": [
            {"entry_ts": 1, "exit_ts": 2, "return_pct": 10.0, "net_pnl": 99_000.0},
            {"entry_ts": 3, "exit_ts": 4, "return_pct": -5.0, "net_pnl": -49_500.0},
            {"entry_ts": 5, "exit_ts": 6, "return_pct": 20.0, "net_pnl": 198_000.0},
        ],
    }


def test_position_sizing_sensitivity_keeps_separate_portfolio_policy_hashes() -> None:
    summary = _position_sizing_sensitivity_summary(
        base_policy=legacy_research_portfolio_policy(),
        candidate=_candidate(),
    )

    by_fraction = summary["by_buy_fraction"]
    assert len(by_fraction) >= 2
    assert by_fraction["0.99"]["portfolio_policy_hash"].startswith("sha256:")
    assert by_fraction["0.10"]["portfolio_policy_hash"].startswith("sha256:")
    assert by_fraction["0.99"]["portfolio_policy_hash"] != by_fraction["0.10"]["portfolio_policy_hash"]


def test_position_sizing_sensitivity_does_not_override_primary_metrics() -> None:
    candidate = _candidate()
    original = dict(candidate["validation_metrics_v2"])

    summary = _position_sizing_sensitivity_summary(
        base_policy=legacy_research_portfolio_policy(),
        candidate=candidate,
    )

    assert candidate["validation_metrics_v2"] == original
    assert summary["primary_metrics_overridden"] is False
    assert summary["promotion_authority"] == "diagnostic_only_excluded_from_promotion"


def test_position_sizing_sensitivity_uses_independent_portfolio_simulation() -> None:
    summary = _position_sizing_sensitivity_summary(
        base_policy=legacy_research_portfolio_policy(),
        candidate=_candidate(),
    )

    assert summary["status"] == "available"
    assert summary["direct_linear_scaling_used"] is False
    assert "missing_reason" not in summary
    assert summary["by_buy_fraction"]["0.50"]["simulation_method"] == "independent_closed_trade_portfolio_replay"
    assert summary["by_buy_fraction"]["0.50"]["validation_trade_count"] == 3


def test_position_sizing_sensitivity_does_not_linearly_scale_primary_metrics() -> None:
    summary = _position_sizing_sensitivity_summary(
        base_policy=legacy_research_portfolio_policy(),
        candidate=_candidate(),
    )

    assert summary["by_buy_fraction"]["0.50"]["validation_return_pct"] != 5.0
    assert summary["by_buy_fraction"]["0.10"]["validation_return_pct"] != 10.0 * (0.10 / 0.99)
    assert summary["by_buy_fraction"]["0.50"]["validation_max_drawdown_pct"] is not None


def test_position_sizing_sensitivity_persists_non_null_metrics_for_each_fraction() -> None:
    summary = _position_sizing_sensitivity_summary(
        base_policy=legacy_research_portfolio_policy(),
        candidate=_candidate(),
    )

    for fraction in ("0.99", "0.50", "0.25", "0.10"):
        result = summary["by_buy_fraction"][fraction]
        assert result["validation_return_pct"] is not None
        assert result["validation_max_drawdown_pct"] is not None
        assert result["validation_profit_factor"] is not None
        assert result["portfolio_policy_hash"].startswith("sha256:")
