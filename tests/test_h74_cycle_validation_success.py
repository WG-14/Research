from __future__ import annotations

from bithumb_bot.h74_cycle_classification import classify_h74_cycle


def _entry(**overrides):
    payload = {
        "client_order_id": "entry",
        "side": "BUY",
        "cycle_id": "cycle-1",
        "authority_source": "daily_participation_entry",
        "fill_ts": 1_000_000,
    }
    payload.update(overrides)
    return payload


def _exit(**overrides):
    payload = {
        "client_order_id": "exit",
        "side": "SELL",
        "cycle_id": "cycle-1",
        "exit_rule_name": "max_holding_time",
        "fill_ts": 1_000_000 + 74 * 60_000,
    }
    payload.update(overrides)
    return payload


def test_buy_only_is_entry_path_sample_not_cycle_success() -> None:
    result = classify_h74_cycle(entry=_entry())

    assert result.h74_entry_path_sample is True
    assert result.h74_cycle_validation_success is False


def test_roundtrip_with_max_holding_exit_and_terminal_flat_is_cycle_success() -> None:
    result = classify_h74_cycle(entry=_entry(), exit=_exit(), terminal={"terminal_executable_qty": 0.0, "broker_local_converged": True})

    assert result.h74_cycle_validation_success is True


def test_roundtrip_with_executable_residual_is_not_cycle_success() -> None:
    result = classify_h74_cycle(entry=_entry(), exit=_exit(), terminal={"terminal_executable_qty": 0.0001, "broker_local_converged": True})

    assert result.h74_cycle_validation_success is False
    assert "terminal_executable_residual" in result.failure_reasons


def test_unauthorized_intermediate_rebalance_blocks_cycle_success() -> None:
    result = classify_h74_cycle(
        entry=_entry(),
        exit=_exit(),
        terminal={"terminal_executable_qty": 0.0, "broker_local_converged": True},
        orders=[{"client_order_id": "rebalance", "cycle_id": "cycle-1", "side": "BUY", "created_ts": 1_000_000 + 60_000}],
    )

    assert result.h74_cycle_validation_success is False
    assert result.unauthorized_intermediate_order_count == 1
