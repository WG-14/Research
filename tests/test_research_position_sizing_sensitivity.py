from __future__ import annotations

from bithumb_bot.research.experiment_manifest import legacy_research_portfolio_policy
from bithumb_bot.research.validation_protocol import _position_sizing_sensitivity_summary


def test_position_sizing_sensitivity_keeps_separate_portfolio_policy_hashes() -> None:
    summary = _position_sizing_sensitivity_summary(
        base_policy=legacy_research_portfolio_policy(),
        candidate={"validation_metrics_v2": {"total_return_pct": 10.0, "max_drawdown_pct": 5.0, "profit_factor": 2.0}},
    )

    by_fraction = summary["by_buy_fraction"]
    assert len(by_fraction) >= 2
    assert by_fraction["0.99"]["portfolio_policy_hash"].startswith("sha256:")
    assert by_fraction["0.10"]["portfolio_policy_hash"].startswith("sha256:")
    assert by_fraction["0.99"]["portfolio_policy_hash"] != by_fraction["0.10"]["portfolio_policy_hash"]


def test_position_sizing_sensitivity_does_not_override_primary_metrics() -> None:
    candidate = {"validation_metrics_v2": {"total_return_pct": 10.0, "max_drawdown_pct": 5.0, "profit_factor": 2.0}}
    original = dict(candidate["validation_metrics_v2"])

    summary = _position_sizing_sensitivity_summary(
        base_policy=legacy_research_portfolio_policy(),
        candidate=candidate,
    )

    assert candidate["validation_metrics_v2"] == original
    assert summary["primary_metrics_overridden"] is False
    assert summary["promotion_authority"] == "diagnostic_only_excluded_from_promotion"
