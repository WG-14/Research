from __future__ import annotations

from types import SimpleNamespace

from bithumb_bot.broker import live_submit_planning


def test_build_live_submit_plan_uses_explicit_market(monkeypatch) -> None:
    captured = {}

    def _plan_place_order(_broker, *, intent, rules, skip_qty_revalidation):
        captured["intent"] = intent
        captured["rules"] = rules
        captured["skip_qty_revalidation"] = skip_qty_revalidation
        return SimpleNamespace(intent=intent)

    monkeypatch.setattr(live_submit_planning, "plan_place_order", _plan_place_order)

    result = live_submit_planning.build_live_submit_plan(
        broker=object(),
        client_order_id="cid",
        side="BUY",
        qty=0.001,
        ts=123,
        effective_rules=SimpleNamespace(),
        reference_price=50_000_000.0,
        market="KRW-ETH",
    )

    assert result.intent.market == "KRW-ETH"
    assert captured["intent"].market == "KRW-ETH"
    assert captured["rules"] is not None
