from __future__ import annotations

import pytest

from bithumb_bot.h74_position_ownership import (
    H74PositionOwnershipContract,
    H74PositionOwnershipError,
)


def _contract(**overrides: object) -> H74PositionOwnershipContract:
    values = {
        "cycle_id": "h74-cycle-1",
        "h74_cycle_id": "h74-cycle-1",
        "strategy_instance_id": "h74-source-observation",
        "authority_hash": "sha256:authority",
        "probe_run_id": "probe-run-1",
        "pair": "KRW-BTC",
        "entry_side": "BUY",
        "entry_plan_id": "h74-entry-plan-1",
        "position_mode": "fixed_fill_qty_until_exit",
        "hold_policy": "hold_acquired_fill_qty_until_max_holding_exit",
    }
    values.update(overrides)
    return H74PositionOwnershipContract(**values)


@pytest.mark.parametrize(
    "field",
    ("cycle_id", "h74_cycle_id", "strategy_instance_id", "authority_hash", "probe_run_id"),
)
def test_h74_position_ownership_contract_requires_cycle_identity_fields(field: str) -> None:
    with pytest.raises(H74PositionOwnershipError, match="h74_cycle_ownership_required_for_entry"):
        _contract(**{field: ""})


def test_h74_position_ownership_contract_hash_is_stable() -> None:
    first = _contract()
    second = _contract()

    assert first.content_hash() == second.content_hash()
    assert first.contract_hash == second.contract_hash


def test_h74_position_ownership_contract_serializes_required_fields() -> None:
    payload = _contract().as_dict()

    for field in (
        "cycle_id",
        "h74_cycle_id",
        "strategy_instance_id",
        "authority_hash",
        "probe_run_id",
        "pair",
        "entry_side",
        "entry_plan_id",
        "position_mode",
        "hold_policy",
        "contract_hash",
    ):
        assert payload[field]
