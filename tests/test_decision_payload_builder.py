from __future__ import annotations

from bithumb_bot.research.backtest_engine import BacktestRunContext
from tests.test_research_backtest_observability_policy import _run


def test_summary_payload_uses_hash_references_not_full_strategy_contract() -> None:
    result = _run(context=BacktestRunContext(report_detail="summary"), count=1)
    payload = result.decisions[0]

    assert payload["decision_payload_detail_level"] == "summary"
    assert "strategy_spec" not in payload
    assert "strategy_plugin_contract" not in payload
    assert "pure_policy_trace" not in payload
    assert payload["strategy_spec_hash"].startswith("sha256:")
    assert payload["strategy_plugin_contract_hash"].startswith("sha256:")
    assert payload["exit_policy_hash"].startswith("sha256:")


def test_full_payload_keeps_existing_contract_fields_when_requested() -> None:
    result = _run(context=BacktestRunContext(report_detail="full"), count=1)
    payload = result.decisions[0]

    assert payload["decision_payload_detail_level"] == "full_canonical"
    assert isinstance(payload["strategy_spec"], dict)
    assert isinstance(payload["strategy_plugin_contract"], dict)
    assert payload["strategy_spec_hash"].startswith("sha256:")
    assert payload["strategy_plugin_contract_hash"].startswith("sha256:")


def test_payload_builder_requires_detail_level() -> None:
    from bithumb_bot.research.decision_payload import DecisionPayloadBuilder

    try:
        DecisionPayloadBuilder().build()
    except TypeError:
        return
    raise AssertionError("DecisionPayloadBuilder.build accepted an omitted detail level")
