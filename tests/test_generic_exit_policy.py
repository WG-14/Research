from market_research.research.exit_policy import GenericExitPolicyEvaluator
from market_research.research.portfolio_view import ReadOnlyPortfolioView


def test_false_exit_decision_creates_no_intent_authority():
    portfolio = ReadOnlyPortfolioView(0, 1, 100, 100, 0, 0, None, 0, 0)
    decision = GenericExitPolicyEvaluator().evaluate(
        policy={"rules": ["stop_loss"], "stop_loss": {"stop_loss_ratio": 0.1}},
        portfolio=portfolio,
        market_price=95,
        event_ts=1,
    )
    assert decision.triggered is False
    assert decision.rule is None
