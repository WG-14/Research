from __future__ import annotations

import json

import pytest

from bithumb_bot.h74_live_rehearsal import H74LiveRehearsalConfig, run_h74_live_rehearsal


pytestmark = pytest.mark.fast_regression


def _source_artifact(tmp_path) -> str:
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            {
                "runtime_base_cost_assumption": {
                    "fee_rate": 0.0004,
                    "fee_source": "research_realistic_bithumb_app_fee",
                    "slippage_bps": 10,
                    "slippage_source": "research_assumption",
                },
                "candle_timing": "closed_candle_kst",
                "behavior_contract": {
                    "position_mode": "fixed_fill_qty_until_exit",
                    "hold_policy": "hold_acquired_fill_qty_until_max_holding_exit",
                    "residual_inventory_mode": "terminal_dust_reported_not_reused_without_authority",
                    "initial_position_policy": "flat_start_required",
                    "partial_fill_policy": "accumulate_cycle_acquired_qty",
                    "fee_application_policy": "repository_observed_fee_fields",
                },
                "entry_submit_semantics": {
                    "schema_version": 1,
                    "entry_order_type": "price",
                    "entry_submit_field": "price",
                    "entry_quote_notional_krw": 100_000,
                    "entry_volume_forbidden": True,
                    "entry_qty_preview_authoritative": False,
                    "entry_fill_qty_authority": "broker_fills",
                },
            }
        ),
        encoding="utf-8",
    )
    return str(source)


def test_h74_rehearsal_blocks_when_quote_notional_floored_to_90000(tmp_path, monkeypatch) -> None:
    from bithumb_bot import h74_live_rehearsal

    original = h74_live_rehearsal.evaluate_submit_authority_policy

    def _mutating_policy(plan, *args, **kwargs):
        plan["notional_krw"] = 90_000.108
        plan["exchange_submit_notional_krw"] = 90_000.108
        return original(plan, *args, **kwargs)

    monkeypatch.setattr(h74_live_rehearsal, "evaluate_submit_authority_policy", _mutating_policy)

    payload = run_h74_live_rehearsal(
        H74LiveRehearsalConfig(source_artifact_path=_source_artifact(tmp_path))
    )

    assert payload["would_submit"] is False
    assert payload["primary_block_gate"] == "submit_semantics"
    assert "notional" in payload["primary_block_reason"]


def test_h74_rehearsal_passes_with_quote_notional_100000_price_payload(tmp_path) -> None:
    payload = run_h74_live_rehearsal(
        H74LiveRehearsalConfig(source_artifact_path=_source_artifact(tmp_path))
    )
    plan = payload["would_submit_plan"]

    assert payload["experiment_equivalence_status"] == "pass"
    assert payload["primary_block_gate"] == "none"
    assert payload["would_submit"] is True
    assert plan["position_mode"] == "fixed_fill_qty_until_exit"
    assert plan["notional_krw"] == pytest.approx(100_000.0)
    assert plan["exchange_submit_notional_krw"] == pytest.approx(100_000.0)
    assert plan["exchange_order_type"] == "price"
    assert plan["exchange_submit_field"] == "price"


def test_h74_rehearsal_exposes_broker_payload_preview(tmp_path) -> None:
    payload = run_h74_live_rehearsal(
        H74LiveRehearsalConfig(source_artifact_path=_source_artifact(tmp_path))
    )

    assert payload["broker_payload_preview"] == {
        "order_type": "price",
        "price": 100_000.0,
        "volume_present": False,
    }
    assert str(payload["broker_payload_preview_hash"]).startswith("sha256:")
