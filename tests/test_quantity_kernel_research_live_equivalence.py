from __future__ import annotations

from bithumb_bot.quantity_kernel import OrderRuleSnapshot, plan_buy_notional, plan_sell_qty


def _rules(**overrides) -> OrderRuleSnapshot:
    payload = {"min_qty": 0.0001, "qty_step": 0.0001, "max_qty_decimals": 8, "min_notional_krw": 5000.0}
    payload.update(overrides)
    return OrderRuleSnapshot.from_mapping(payload)


def test_buy_100000_krw_same_quantity_contract_in_research_and_live() -> None:
    research = plan_buy_notional(requested_notional_krw=100_000.0, reference_price=123_456_789.0, rules=_rules())
    live = plan_buy_notional(requested_notional_krw=100_000.0, reference_price=123_456_789.0, rules=_rules())

    assert research.submitted_qty == live.submitted_qty
    assert research.quantity_contract_hash == live.quantity_contract_hash


def test_sell_remaining_cycle_qty_same_submitted_volume_in_research_and_live() -> None:
    research = plan_sell_qty(requested_qty=0.00087, reference_price=123_456_789.0, rules=_rules())
    live = plan_sell_qty(requested_qty=0.00087, reference_price=123_456_789.0, rules=_rules())

    assert research.submitted_qty == live.submitted_qty == 0.0008
    assert research.exchange_submit_field == live.exchange_submit_field == "volume"


def test_qty_step_mutation_changes_quantity_contract_hash() -> None:
    baseline = plan_sell_qty(requested_qty=0.00087, reference_price=123_456_789.0, rules=_rules())
    mutated = plan_sell_qty(requested_qty=0.00087, reference_price=123_456_789.0, rules=_rules(qty_step=0.00001))

    assert baseline.quantity_contract_hash != mutated.quantity_contract_hash


def test_max_qty_decimals_mutation_changes_quantity_contract_hash() -> None:
    baseline = plan_buy_notional(requested_notional_krw=100_000.0, reference_price=123_456_789.0, rules=_rules())
    mutated = plan_buy_notional(requested_notional_krw=100_000.0, reference_price=123_456_789.0, rules=_rules(max_qty_decimals=4))

    assert baseline.quantity_contract_hash != mutated.quantity_contract_hash
