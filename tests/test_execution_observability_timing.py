from market_research.research.execution_model import StressExecutionModel
from tests.test_strategy_partial_fill_feedback import _run


def test_delayed_fill_status_not_visible_before_effective_time():
    observed = []
    run = _run(StressExecutionModel(0.001, 10, partial_fill_rate=1,
                                    partial_fill_fraction=.5, seed=1), observed)
    fill = run.fills[0]
    before = [view for index, view in enumerate(observed) if index < 3]
    assert fill.portfolio_effective_ts == 180_000
    assert all(view.last_execution_status is None for view in before)


def test_delayed_fill_status_visible_after_effective_time():
    observed = []
    _run(StressExecutionModel(0.001, 10, partial_fill_rate=1,
                              partial_fill_fraction=.5, seed=1), observed)
    assert any(view.last_execution_status == "partial" and view.filled_position_qty > 0
               for view in observed)
