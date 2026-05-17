from __future__ import annotations

from bithumb_bot.research.lot_native_simulation import (
    LOT_NATIVE_RESEARCH_POSITION_MODEL,
    LotNativeResearchPositionModel,
    ResearchLotRules,
)


ORDER_RULES_HASH = "sha256:order_rules"
FEE_AUTHORITY_HASH = "sha256:fee_authority"


def _snapshot(model: LotNativeResearchPositionModel):
    return model.authority_snapshot(
        order_rules_hash=ORDER_RULES_HASH,
        fee_authority_hash=FEE_AUTHORITY_HASH,
    )


def test_flat_no_dust_snapshot_matches_canonical_flat_authority() -> None:
    snapshot = _snapshot(LotNativeResearchPositionModel.flat())

    assert snapshot.state_class == "flat_no_dust_no_position"
    assert snapshot.unsupported_reason == ""
    assert snapshot.research_position_model == LOT_NATIVE_RESEARCH_POSITION_MODEL
    assert snapshot.open_lot_count == 0
    assert snapshot.dust_tracking_lot_count == 0
    assert snapshot.reserved_exit_lot_count == 0
    assert snapshot.sellable_executable_lot_count == 0
    assert snapshot.entry_allowed is True
    assert snapshot.exit_allowed is False
    assert snapshot.terminal_state == "flat"


def test_buy_fill_creates_open_exposure_lot_authority() -> None:
    model = LotNativeResearchPositionModel.flat().apply_buy_fill(qty=0.0003)
    snapshot = _snapshot(model)

    assert snapshot.state_class == "open_exposure"
    assert snapshot.unsupported_reason == ""
    assert snapshot.research_position_model == LOT_NATIVE_RESEARCH_POSITION_MODEL
    assert snapshot.open_lot_count == 3
    assert snapshot.reserved_exit_lot_count == 0
    assert snapshot.sellable_executable_lot_count == 3
    assert snapshot.open_exposure_qty == 0.0003
    assert snapshot.sellable_executable_qty == 0.0003
    assert snapshot.entry_allowed is False
    assert snapshot.exit_allowed is True


def test_sell_submit_reserves_sellable_lots_as_reserved_exit_pending() -> None:
    model = LotNativeResearchPositionModel.flat().apply_buy_fill(qty=0.0002).submit_sell()
    snapshot = _snapshot(model)

    assert snapshot.state_class == "reserved_exit_pending"
    assert snapshot.unsupported_reason == "research_model_lacks_lot_native_authority"
    assert snapshot.research_position_model == "lot_native_simulation_v1_partial"
    assert snapshot.open_lot_count == 2
    assert snapshot.reserved_exit_lot_count == 2
    assert snapshot.sellable_executable_lot_count == 0
    assert snapshot.reserved_exit_qty == 0.0002
    assert snapshot.exit_allowed is False
    assert snapshot.terminal_state == "reserved_exit_pending"


def test_partial_sell_fill_reduces_reserved_and_open_exposure_consistently() -> None:
    model = (
        LotNativeResearchPositionModel.flat()
        .apply_buy_fill(qty=0.0003)
        .submit_sell()
        .apply_sell_fill(qty=0.0001)
    )
    snapshot = _snapshot(model)

    assert snapshot.state_class == "reserved_exit_pending"
    assert snapshot.unsupported_reason == "research_model_lacks_lot_native_authority"
    assert snapshot.research_position_model == "lot_native_simulation_v1_partial"
    assert snapshot.open_lot_count == 2
    assert snapshot.reserved_exit_lot_count == 2
    assert snapshot.sellable_executable_lot_count == 0
    assert snapshot.open_exposure_qty == 0.0002
    assert snapshot.reserved_exit_qty == 0.0002


def test_unmodeled_dust_residue_remains_fail_closed() -> None:
    rules = ResearchLotRules(internal_lot_size=0.0001, min_qty=0.0001, qty_step=0.0001)
    model = LotNativeResearchPositionModel.flat(rules).apply_buy_fill(qty=0.00015)
    snapshot = _snapshot(model)

    assert snapshot.unsupported_reason == "research_model_lacks_dust_state"
    assert snapshot.research_position_model == "lot_native_simulation_v1_partial"


def test_recovery_blocked_remains_fail_closed() -> None:
    model = LotNativeResearchPositionModel.flat().apply_buy_fill(qty=0.0001).mark_recovery_blocked(
        "recovery_required_present"
    )
    snapshot = _snapshot(model)

    assert snapshot.state_class == "recovery_blocked"
    assert snapshot.unsupported_reason == "research_model_lacks_lot_native_authority"
    assert snapshot.recovery_blocked is True
    assert snapshot.recovery_block_reason == "recovery_required_present"
