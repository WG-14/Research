from __future__ import annotations

import pytest

from market_research.research.portfolio_ledger import PortfolioLedger
from tests.test_common_simulation_engine import SpyModel, _run


def test_each_stream_has_complete_decision_to_ledger_lineage():
    run = _run(SpyModel())
    decisions = {item["decision_id"] for item in run.decisions}
    intents = {item.intent_id: item for item in run.order_intents}
    requests = {item.request_id: item for item in run.execution_requests}
    assert all(intent.decision_id in decisions for intent in intents.values())
    assert all(request.intent_id in intents for request in requests.values())
    assert all(fill.request_id in requests and fill.fill_id for fill in run.fills)
    assert all(entry.fill_id in {fill.fill_id for fill in run.fills} for entry in run.ledger_entries)


def test_duplicate_execution_ids_are_rejected():
    run = _run(SpyModel())
    ledger = PortfolioLedger(starting_cash=1_000_000)
    ledger.apply(run.fills[0])
    with pytest.raises(ValueError, match="duplicate_fill_id"):
        ledger.apply(run.fills[0])
