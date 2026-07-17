from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from market_research.research.walk_forward_selection import (
    build_walk_forward_selection_evidence,
)


GATE = SimpleNamespace(
    min_trade_count=1,
    max_mdd_pct=20.0,
    min_profit_factor=1.0,
    oos_return_must_be_positive=True,
)


def _candidate(
    candidate_id: str,
    parameter: int,
    train_returns: list[float],
    test_returns: list[float],
):
    return {
        "candidate_id": candidate_id,
        "parameter_values": {"lookback": parameter},
        "walk_forward_metrics": {
            "windows": [
                {
                    "window_id": f"window_{index:03d}",
                    "train_date_range": {
                        "start": f"202{index}-01-01",
                        "end": f"202{index}-06-30",
                    },
                    "test_date_range": {
                        "start": f"202{index}-07-01",
                        "end": f"202{index}-12-31",
                    },
                    "train_metrics": {
                        "return_pct": train_return,
                        "max_drawdown_pct": 5.0,
                        "profit_factor": 2.0,
                        "trade_count": 10,
                    },
                    "test_metrics": {
                        "return_pct": test_return,
                        "max_drawdown_pct": 5.0,
                        "profit_factor": 2.0,
                        "trade_count": 10,
                    },
                }
                for index, (train_return, test_return) in enumerate(
                    zip(train_returns, test_returns), start=1
                )
            ]
        },
    }


def test_walk_forward_selects_on_train_only_and_test_mutation_cannot_change_selection() -> (
    None
):
    candidates = [
        _candidate("slow", 20, [10.0, 1.0], [2.0, 100.0]),
        _candidate("fast", 5, [1.0, 10.0], [100.0, 2.0]),
    ]
    first = build_walk_forward_selection_evidence(
        candidates=candidates,
        acceptance_gate=GATE,
        min_windows=2,
    )
    mutated = deepcopy(candidates)
    for candidate in mutated:
        for window in candidate["walk_forward_metrics"]["windows"]:
            window["test_metrics"]["return_pct"] *= -100.0
    second = build_walk_forward_selection_evidence(
        candidates=mutated,
        acceptance_gate=GATE,
        min_windows=2,
    )

    assert [window["selected_candidate_id"] for window in first["windows"]] == [
        "slow",
        "fast",
    ]
    assert [window["selected_candidate_id"] for window in second["windows"]] == [
        "slow",
        "fast",
    ]
    assert [window["selection_artifact_hash"] for window in first["windows"]] == [
        window["selection_artifact_hash"] for window in second["windows"]
    ]
    assert first["selected_parameter_change_count"] == 1
    assert first["recent_window_test_return_pct"] == 2.0
