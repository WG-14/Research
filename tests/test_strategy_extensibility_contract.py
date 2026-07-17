from __future__ import annotations

import pytest

from market_research.research.strategy_contract import ResearchStrategyPlugin
from market_research.builtin_strategies.sma_with_filter import SMA_WITH_FILTER_SPEC


def test_custom_strategy_specific_backtest_runner_is_rejected():
    with pytest.raises(ValueError, match="custom_execution_authority_rejected"):
        ResearchStrategyPlugin(
            name="fixture",
            version="1",
            spec=SMA_WITH_FILTER_SPEC,
            required_data=("candles",),
            optional_data=(),
            event_builder=lambda **kwargs: (),
            decision_contract_version="v1",
            diagnostics_namespace="fixture",
            execution_authority="custom_runner",
        )


def test_new_strategy_fixture_requires_no_common_module_branch():
    from pathlib import Path

    source = Path("src/market_research/research/simulation_engine.py").read_text()
    assert '"fixture"' not in source and "'fixture'" not in source
