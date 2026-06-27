from __future__ import annotations

import json
from dataclasses import fields

import pytest

from bithumb_bot.config import settings
from bithumb_bot.execution_service import (
    ExecutionReadinessPlanningInput,
    ExecutionTargetPlanningInput,
    TypedExecutionPlanningInput,
    build_typed_execution_decision_summary,
)
from bithumb_bot.h74_live_rehearsal import H74LiveRehearsalConfig, run_h74_live_rehearsal
from bithumb_bot.broker.live_submission_execution import _merge_h74_submit_identity
from bithumb_bot.broker.live_submit_orchestrator import (
    _build_context,
    _plan_submit_attempt,
    _validate_explicit_submit_plan,
    run_standard_submit_pipeline_with_evidence,
)
from bithumb_bot.db_core import ensure_db
from bithumb_bot.execution import record_order_if_missing
from bithumb_bot.h74_cycle_state import upsert_h74_cycle_fill
from bithumb_bot.portfolio_target import PortfolioTarget
from bithumb_bot.strategy_policy_contract import ExitExecutionIntent, PositionSnapshot, StrategyDecisionV2
from bithumb_bot.h74_submit_identity import H74SubmitIdentityError, resolve_h74_sell_identity
from tests.test_h74_live_submit_ownership import _ownership, _request
from tests.test_h74_live_rehearsal import _source_artifact


@pytest.fixture(autouse=True)
def _restore_settings():
    old_values = {field.name: getattr(settings, field.name) for field in fields(type(settings))}
    yield
    for key, value in old_values.items():
        object.__setattr__(settings, key, value)


class _DispatchForbiddenBroker:
    def place_order(self, **_kwargs):
        raise AssertionError("dispatch must not be reached")


def _decision_observability() -> dict[str, object]:
    ownership = _ownership()
    return {
        "h74_fixed_position_contract_active": True,
        "cycle_id": ownership.cycle_id,
        "h74_cycle_id": ownership.h74_cycle_id,
        "strategy_instance_id": ownership.strategy_instance_id,
        "authority_hash": ownership.authority_hash,
        "h74_execution_path_probe_run_id": ownership.probe_run_id,
        "h74_entry_plan_client_order_id": ownership.entry_plan_id,
        "h74_position_ownership_contract_hash": ownership.contract_hash,
        "h74_position_ownership_contract": ownership.as_dict(),
    }


def _h74_sell_decision() -> StrategyDecisionV2:
    return StrategyDecisionV2(
        strategy_name="daily_participation_sma",
        raw_signal="SELL",
        raw_reason="max_holding_time",
        entry_signal="HOLD",
        entry_reason="in_position",
        exit_signal="SELL",
        exit_reason="max_holding_time",
        final_signal="SELL",
        final_reason="max_holding_time",
        blocked_filters=(),
        entry_blocked=True,
        entry_block_reason="in_position",
        exit_rule="max_holding_time",
        exit_evaluations=({"rule": "max_holding_time", "passed": True},),
        protective_exit_overrode_entry=False,
        exit_filter_suppression_prevented=False,
        position_snapshot=PositionSnapshot(
            in_position=True,
            entry_allowed=False,
            exit_allowed=True,
            terminal_state="open_exposure",
            qty_open=0.002,
            raw_qty_open=0.002,
            raw_total_asset_qty=0.002,
            open_lot_count=20,
            sellable_executable_lot_count=20,
            dust_state="none",
            effective_flat=False,
            has_executable_exposure=True,
            has_any_position_residue=True,
        ),
        execution_intent=ExitExecutionIntent(
            side="SELL",
            intent="exit",
            pair="KRW-BTC",
            requires_execution_sizing=True,
        ),
        entry_decision=None,
        trace={"exit_signal_source": "max_holding_time"},
        policy_hash="sha256:h74-policy",
        policy_contract_hash="sha256:h74-contract",
        policy_input_hash="sha256:h74-input",
        policy_decision_hash="sha256:h74-decision",
    )


def _target_for_flat() -> PortfolioTarget:
    return PortfolioTarget(
        pair="KRW-BTC",
        target_exposure_krw=0.0,
        target_qty=0.0,
        allocator_policy_name="unit_allocator",
        allocator_policy_version="1",
        allocator_config_hash="sha256:allocator-config",
        strategy_contribution_hash="sha256:strategy-contribution",
        allocation_input_hash="sha256:allocation-input",
        reason="scheduled_exit",
        conflict_resolution={
            "selected_signal": "SELL",
            "selected_strategy_instance_ids": ["h74-source-observation"],
            "target_exposure_source": "scheduled_exit",
            "allocation_target_source": "scheduled_exit",
            "strict_target_exposure_required": True,
        },
        authoritative=True,
        fail_closed_reason="none",
    )


def _planning_settings(db_path) -> None:
    object.__setattr__(settings, "DB_PATH", str(db_path))
    object.__setattr__(settings, "MODE", "live")
    object.__setattr__(settings, "LIVE_DRY_RUN", False)
    object.__setattr__(settings, "LIVE_REAL_ORDER_ARMED", True)
    object.__setattr__(settings, "EXECUTION_ENGINE", "target_delta")
    object.__setattr__(settings, "PAIR", "KRW-BTC")
    object.__setattr__(settings, "H74_LIVE_REHEARSAL_NO_SUBMIT_BOUNDARY", False)
    object.__setattr__(settings, "LIVE_MIN_ORDER_QTY", 0.0001)
    object.__setattr__(settings, "LIVE_ORDER_QTY_STEP", 0.0)
    object.__setattr__(settings, "LIVE_ORDER_MAX_QTY_DECIMALS", 8)


def _record_open_h74_cycle(conn, ownership, *, contract_hash: str | None = None) -> None:
    record_order_if_missing(
        conn,
        client_order_id="h74-entry-order",
        side="BUY",
        qty_req=0.002,
        price=100_000_000.0,
        symbol="KRW-BTC",
        strategy_name="daily_participation_sma",
        strategy_instance_id=ownership.strategy_instance_id,
        cycle_id=ownership.cycle_id,
        authority_hash=ownership.authority_hash,
        h74_entry_plan_client_order_id=ownership.entry_plan_id,
        h74_position_ownership_contract_hash=contract_hash or ownership.contract_hash,
        h74_position_ownership_contract=ownership.as_dict(),
        status="FILLED",
    )
    upsert_h74_cycle_fill(
        conn,
        cycle_id=ownership.cycle_id,
        authority_hash=ownership.authority_hash,
        strategy_instance_id=ownership.strategy_instance_id,
        pair="KRW-BTC",
        side="BUY",
        qty=0.002,
        client_order_id="h74-entry-order",
        fill_ts=1,
        contract_hash=ownership.contract_hash,
        h74_entry_plan_client_order_id=ownership.entry_plan_id,
    )


def _build_h74_sell_summary(db_path, *, payload_override: dict[str, object] | None = None):
    _planning_settings(db_path)
    ownership = _ownership()
    target = _target_for_flat()
    readiness = {
        "cash_available": 1_000_000.0,
        "min_qty": 0.0001,
        "qty_step": 0.0,
        "min_notional_krw": 5_000.0,
        "broker_position_evidence": {
            "broker_qty_known": True,
            "broker_qty": 0.002,
            "broker_asset_qty": 0.002,
            "balance_source_stale": False,
        },
        "total_effective_exposure_notional_krw": 200_000.0,
        "position_mode": "fixed_fill_qty_until_exit",
        "hold_policy": "hold_acquired_fill_qty_until_max_holding_exit",
        "h74_fixed_position_contract_active": True,
        "strategy_instance_id": ownership.strategy_instance_id,
        "authority_hash": ownership.authority_hash,
        "h74_cycle_id": ownership.cycle_id,
        "cycle_id": ownership.cycle_id,
        "remaining_cycle_qty": 0.002,
        "h74_remaining_cycle_qty": 0.002,
        "h74_entry_plan_client_order_id": ownership.entry_plan_id,
        "h74_position_ownership_contract_hash": ownership.contract_hash,
        "h74_position_ownership_contract": ownership.as_dict(),
        "pre_submit_risk_status": "REDUCE_ONLY",
        "pre_submit_risk_reason_code": "POSITION_LOSS_LIMIT",
    }
    readiness.update(payload_override or {})
    return build_typed_execution_decision_summary(
        typed_input=TypedExecutionPlanningInput(
            strategy_decision=_h74_sell_decision(),
            candle_ts=2,
            market_price=100_000_000.0,
            readiness=ExecutionReadinessPlanningInput.from_payload(readiness),
            target=ExecutionTargetPlanningInput(
                previous_target_exposure_krw=200_000.0,
                portfolio_target=target,
                portfolio_target_hash=target.content_hash(),
                allocation_decision_hash="sha256:allocation-decision",
                allocator_config_hash="sha256:allocator-config",
                strategy_contribution_hash="sha256:strategy-contribution",
            ),
            observability_context={"unit": "h74_sell_identity_planning"},
        )
    )


def _projected_request(conn):
    submit_observability, identity = _merge_h74_submit_identity(
        submit_observability_fields={"h74_fixed_position_contract_active": True},
        decision_observability=_decision_observability(),
    )
    assert identity is not None
    metadata = identity.as_order_metadata()
    base = _request(conn)
    return base.__class__(
        **{
            **base.__dict__,
            "submit_observability_fields": submit_observability,
            "strategy_instance_id": metadata["strategy_instance_id"],
            "cycle_id": metadata["cycle_id"],
            "authority_hash": metadata["authority_hash"],
            "probe_run_id": metadata["probe_run_id"],
            "h74_cycle_id": metadata["h74_cycle_id"],
            "h74_entry_plan_client_order_id": metadata["h74_entry_plan_client_order_id"],
            "h74_position_ownership_contract_hash": metadata["h74_position_ownership_contract_hash"],
            "h74_position_ownership_contract": metadata["h74_position_ownership_contract"],
            "h74_submit_identity": identity,
        }
    )


def test_live_submission_merges_h74_identity_into_submit_observability_fields() -> None:
    submit_observability, identity = _merge_h74_submit_identity(
        submit_observability_fields={"submit_qty_source": "test"},
        decision_observability=_decision_observability(),
    )

    assert identity is not None
    assert submit_observability["cycle_id"] == "cycle-1"
    assert submit_observability["h74_cycle_id"] == "cycle-1"
    assert submit_observability["h74_entry_plan_client_order_id"] == "h74_entry_plan_123"
    assert submit_observability["h74_position_ownership_contract"]["entry_plan_id"] == "h74_entry_plan_123"


def test_live_submission_request_and_observability_use_same_h74_identity(tmp_path) -> None:
    conn = ensure_db(str(tmp_path / "identity.sqlite"))
    request = _projected_request(conn)

    assert request.h74_submit_identity is not None
    assert request.cycle_id == request.submit_observability_fields["cycle_id"]
    assert request.h74_position_ownership_contract_hash == request.submit_observability_fields[
        "h74_position_ownership_contract_hash"
    ]


def test_live_submission_rejects_missing_h74_contract_hash_before_dispatch() -> None:
    decision = _decision_observability()
    decision.pop("h74_position_ownership_contract_hash")

    with pytest.raises(H74SubmitIdentityError, match="contract_hash"):
        _merge_h74_submit_identity(
            submit_observability_fields={"h74_fixed_position_contract_active": True},
            decision_observability=decision,
        )


def test_h74_identity_propagates_from_decision_observability_to_request(tmp_path) -> None:
    conn = ensure_db(str(tmp_path / "identity.sqlite"))
    request = _projected_request(conn)

    assert _validate_explicit_submit_plan(request=request) is request.submit_plan
    assert request.h74_entry_plan_client_order_id == "h74_entry_plan_123"


def test_h74_identity_propagates_to_planning_submit_evidence(tmp_path) -> None:
    conn = ensure_db(str(tmp_path / "identity.sqlite"))
    request = _projected_request(conn)

    context = _build_context(request=request, submit_plan=_validate_explicit_submit_plan(request=request))
    _plan_submit_attempt(context=context)

    event = conn.execute(
        """
        SELECT submit_evidence FROM order_events
        WHERE client_order_id=? AND submit_phase='planning'
        ORDER BY id DESC LIMIT 1
        """,
        (request.client_order_id,),
    ).fetchone()
    evidence = json.loads(event["submit_evidence"])
    row = conn.execute(
        """
        SELECT cycle_id, h74_entry_plan_client_order_id,
               h74_position_ownership_contract_hash, h74_position_ownership_contract
        FROM orders WHERE client_order_id=?
        """,
        (request.client_order_id,),
    ).fetchone()

    assert evidence["cycle_id"] == "cycle-1"
    assert evidence["h74_cycle_id"] == "cycle-1"
    assert evidence["h74_entry_plan_client_order_id"] == "h74_entry_plan_123"
    assert evidence["h74_position_ownership_contract"]["entry_plan_id"] == "h74_entry_plan_123"
    assert row["cycle_id"] == "cycle-1"
    assert row["h74_entry_plan_client_order_id"] == "h74_entry_plan_123"
    assert row["h74_position_ownership_contract_hash"] == request.h74_position_ownership_contract_hash
    assert json.loads(row["h74_position_ownership_contract"])["entry_plan_id"] == "h74_entry_plan_123"


def test_h74_identity_propagates_to_failed_order_row_before_dispatch(tmp_path) -> None:
    conn = ensure_db(str(tmp_path / "identity.sqlite"))
    request = _projected_request(conn)
    bad_plan = request.submit_plan.__class__(**{**request.submit_plan.__dict__, "phase_result": "bad"})
    bad_request = request.__class__(**{**request.__dict__, "submit_plan": bad_plan})

    result = run_standard_submit_pipeline_with_evidence(
        broker=_DispatchForbiddenBroker(),
        request=bad_request,
    )

    row = conn.execute(
        """
        SELECT status, cycle_id, h74_entry_plan_client_order_id,
               h74_position_ownership_contract_hash, h74_position_ownership_contract
        FROM orders WHERE client_order_id=?
        """,
        (request.client_order_id,),
    ).fetchone()
    event = conn.execute(
        """
        SELECT submit_evidence FROM order_events
        WHERE client_order_id=? AND submit_phase='planning'
        ORDER BY id DESC LIMIT 1
        """,
        (request.client_order_id,),
    ).fetchone()
    evidence = json.loads(event["submit_evidence"])

    assert result is None
    assert row["status"] == "FAILED"
    assert row["cycle_id"] == "cycle-1"
    assert row["h74_entry_plan_client_order_id"] == "h74_entry_plan_123"
    assert row["h74_position_ownership_contract_hash"] == request.h74_position_ownership_contract_hash
    assert json.loads(row["h74_position_ownership_contract"])["entry_plan_id"] == "h74_entry_plan_123"
    assert evidence["cycle_id"] == "cycle-1"
    assert evidence["h74_cycle_id"] == "cycle-1"
    assert evidence["h74_position_ownership_contract"]["entry_plan_id"] == "h74_entry_plan_123"


def test_h74_sell_plan_carries_entry_plan_id_and_contract_hash(tmp_path) -> None:
    payload = run_h74_live_rehearsal(
        H74LiveRehearsalConfig(
            source_artifact_path=_source_artifact(tmp_path),
            closeout_existing_qty=0.002,
            order_rules={"min_qty": 0.001, "qty_step": 0.0, "max_qty_decimals": 8, "min_notional_krw": 5000.0},
        )
    )
    plan = payload["would_submit_plan"]

    assert plan["side"] == "SELL"
    assert plan["cycle_id"]
    assert plan["h74_cycle_id"] == plan["cycle_id"]
    assert plan["h74_entry_plan_client_order_id"]
    assert plan["h74_position_ownership_contract_hash"]
    assert plan["h74_closeout_contract"]["h74_entry_plan_client_order_id"] == plan["h74_entry_plan_client_order_id"]
    assert plan["h74_closeout_contract"]["h74_position_ownership_contract_hash"] == plan[
        "h74_position_ownership_contract_hash"
    ]


def test_h74_sell_plan_rejects_missing_entry_plan_id() -> None:
    from bithumb_bot.h74_cycle_state import build_h74_cycle_closeout_plan_from_payload

    with pytest.raises(ValueError, match="h74_entry_plan_client_order_id"):
        build_h74_cycle_closeout_plan_from_payload(
            {
                "cycle_id": "h74-cycle",
                "h74_cycle_id": "h74-cycle",
                "authority_hash": "sha256:a",
                "strategy_instance_id": "h74-source-observation",
                "contract_hash": "sha256:b",
                "remaining_cycle_qty": 0.001,
                "broker_available_qty": 0.001,
            },
            target_delta_side="SELL",
            target_qty=0.0,
        )


def test_h74_sell_plan_rejects_contract_hash_mismatch() -> None:
    from bithumb_bot.h74_submit_identity import H74SubmitIdentity

    decision = _decision_observability()
    decision["h74_position_ownership_contract_hash"] = "sha256:" + "0" * 64

    with pytest.raises(H74SubmitIdentityError, match="contract_hash_mismatch"):
        H74SubmitIdentity.from_mapping(decision)


def test_h74_sell_plan_identity_matches_cycle_state_and_entry_order(tmp_path) -> None:
    conn = ensure_db(str(tmp_path / "identity.sqlite"))
    ownership = _ownership()
    record_order_if_missing(
        conn,
        client_order_id="h74-entry-order",
        side="BUY",
        qty_req=0.002,
        price=100_000_000.0,
        symbol="KRW-BTC",
        strategy_name="daily_participation_sma",
        strategy_instance_id=ownership.strategy_instance_id,
        cycle_id=ownership.cycle_id,
        authority_hash=ownership.authority_hash,
        h74_entry_plan_client_order_id=ownership.entry_plan_id,
        h74_position_ownership_contract_hash=ownership.contract_hash,
        h74_position_ownership_contract=ownership.as_dict(),
        status="FILLED",
    )
    upsert_h74_cycle_fill(
        conn,
        cycle_id=ownership.cycle_id,
        authority_hash=ownership.authority_hash,
        strategy_instance_id=ownership.strategy_instance_id,
        pair="KRW-BTC",
        side="BUY",
        qty=0.002,
        client_order_id="h74-entry-order",
        fill_ts=1,
        contract_hash=ownership.contract_hash,
        h74_entry_plan_client_order_id=ownership.entry_plan_id,
    )

    identity = resolve_h74_sell_identity(
        conn,
        {
            "cycle_id": ownership.cycle_id,
            "h74_cycle_id": ownership.cycle_id,
            "strategy_instance_id": ownership.strategy_instance_id,
            "authority_hash": ownership.authority_hash,
        },
        pair="KRW-BTC",
    )
    order_row = conn.execute(
        """
        SELECT cycle_id, h74_entry_plan_client_order_id, h74_position_ownership_contract_hash
        FROM orders
        WHERE client_order_id='h74-entry-order'
        """
    ).fetchone()
    cycle_row = conn.execute(
        """
        SELECT cycle_id, h74_entry_plan_client_order_id, contract_hash
        FROM h74_cycle_state
        WHERE cycle_id=?
        """,
        (ownership.cycle_id,),
    ).fetchone()

    assert identity.cycle_id == cycle_row["cycle_id"] == order_row["cycle_id"]
    assert identity.h74_entry_plan_client_order_id == cycle_row["h74_entry_plan_client_order_id"]
    assert identity.h74_entry_plan_client_order_id == order_row["h74_entry_plan_client_order_id"]
    assert identity.h74_position_ownership_contract_hash == cycle_row["contract_hash"]
    assert identity.h74_position_ownership_contract_hash == order_row["h74_position_ownership_contract_hash"]


def test_h74_sell_plan_rejects_payload_identity_override(tmp_path) -> None:
    conn = ensure_db(str(tmp_path / "identity.sqlite"))
    ownership = _ownership()
    record_order_if_missing(
        conn,
        client_order_id="h74-entry-order",
        side="BUY",
        qty_req=0.002,
        price=100_000_000.0,
        symbol="KRW-BTC",
        strategy_name="daily_participation_sma",
        strategy_instance_id=ownership.strategy_instance_id,
        cycle_id=ownership.cycle_id,
        authority_hash=ownership.authority_hash,
        h74_entry_plan_client_order_id=ownership.entry_plan_id,
        h74_position_ownership_contract_hash=ownership.contract_hash,
        h74_position_ownership_contract=ownership.as_dict(),
        status="FILLED",
    )
    upsert_h74_cycle_fill(
        conn,
        cycle_id=ownership.cycle_id,
        authority_hash=ownership.authority_hash,
        strategy_instance_id=ownership.strategy_instance_id,
        pair="KRW-BTC",
        side="BUY",
        qty=0.002,
        client_order_id="h74-entry-order",
        fill_ts=1,
        contract_hash=ownership.contract_hash,
        h74_entry_plan_client_order_id=ownership.entry_plan_id,
    )

    with pytest.raises(H74SubmitIdentityError, match="payload_mismatch:h74_position_ownership_contract_hash"):
        resolve_h74_sell_identity(
            conn,
            {
                "cycle_id": ownership.cycle_id,
                "h74_cycle_id": ownership.cycle_id,
                "strategy_instance_id": ownership.strategy_instance_id,
                "authority_hash": ownership.authority_hash,
                "h74_position_ownership_contract_hash": "sha256:" + "0" * 64,
            },
            pair="KRW-BTC",
        )


def test_h74_sell_plan_rejects_entry_order_contract_hash_mismatch(tmp_path) -> None:
    conn = ensure_db(str(tmp_path / "identity.sqlite"))
    ownership = _ownership()
    record_order_if_missing(
        conn,
        client_order_id="h74-entry-order",
        side="BUY",
        qty_req=0.002,
        price=100_000_000.0,
        symbol="KRW-BTC",
        strategy_name="daily_participation_sma",
        strategy_instance_id=ownership.strategy_instance_id,
        cycle_id=ownership.cycle_id,
        authority_hash=ownership.authority_hash,
        h74_entry_plan_client_order_id=ownership.entry_plan_id,
        h74_position_ownership_contract_hash=ownership.contract_hash,
        h74_position_ownership_contract=ownership.as_dict(),
        status="FILLED",
    )
    upsert_h74_cycle_fill(
        conn,
        cycle_id=ownership.cycle_id,
        authority_hash=ownership.authority_hash,
        strategy_instance_id=ownership.strategy_instance_id,
        pair="KRW-BTC",
        side="BUY",
        qty=0.002,
        client_order_id="h74-entry-order",
        fill_ts=1,
        contract_hash=ownership.contract_hash,
        h74_entry_plan_client_order_id=ownership.entry_plan_id,
    )
    conn.execute(
        """
        UPDATE orders
        SET h74_position_ownership_contract_hash=?
        WHERE client_order_id='h74-entry-order'
        """,
        ("sha256:" + "0" * 64,),
    )

    with pytest.raises(H74SubmitIdentityError, match="entry_buy_mismatch:h74_position_ownership_contract_hash"):
        resolve_h74_sell_identity(
            conn,
            {
                "cycle_id": ownership.cycle_id,
                "h74_cycle_id": ownership.cycle_id,
                "strategy_instance_id": ownership.strategy_instance_id,
                "authority_hash": ownership.authority_hash,
            },
            pair="KRW-BTC",
        )


def test_h74_sell_submit_plan_uses_resolved_cycle_and_entry_order_identity(tmp_path) -> None:
    db_path = tmp_path / "identity-planning.sqlite"
    conn = ensure_db(str(db_path))
    ownership = _ownership()
    try:
        _record_open_h74_cycle(conn, ownership)
        conn.commit()
    finally:
        conn.close()
    summary = _build_h74_sell_summary(db_path)

    plan = summary.typed_target_submit_plan()
    assert plan is not None
    payload = plan.as_dict()

    assert payload["submit_expected"] is True
    assert payload["side"] == "SELL"
    assert payload["cycle_id"] == ownership.cycle_id
    assert payload["h74_cycle_id"] == ownership.cycle_id
    assert payload["h74_entry_plan_client_order_id"] == ownership.entry_plan_id
    assert payload["h74_position_ownership_contract_hash"] == ownership.contract_hash
    assert payload["authority_hash"] == ownership.authority_hash
    assert payload["strategy_instance_id"] == ownership.strategy_instance_id
    assert payload["h74_closeout_contract"]["h74_entry_plan_client_order_id"] == ownership.entry_plan_id
    assert payload["h74_closeout_contract"]["h74_position_ownership_contract_hash"] == ownership.contract_hash


def test_h74_sell_submit_plan_rejects_payload_identity_override_in_execution_service(tmp_path) -> None:
    db_path = tmp_path / "identity-planning.sqlite"
    conn = ensure_db(str(db_path))
    ownership = _ownership()
    try:
        _record_open_h74_cycle(conn, ownership)
        conn.commit()
    finally:
        conn.close()
    summary = _build_h74_sell_summary(
        db_path,
        payload_override={"h74_position_ownership_contract_hash": "sha256:" + "0" * 64},
    )

    plan = summary.typed_target_submit_plan()
    assert plan is not None
    payload = plan.as_dict()

    assert payload["submit_expected"] is False
    assert payload["block_reason"] == "h74_sell_identity_payload_mismatch:h74_position_ownership_contract_hash"
    assert payload.get("h74_position_ownership_contract_hash") != "sha256:" + "0" * 64


def test_h74_sell_submit_plan_generation_rejects_entry_order_contract_hash_mismatch(tmp_path) -> None:
    db_path = tmp_path / "identity-planning.sqlite"
    conn = ensure_db(str(db_path))
    ownership = _ownership()
    try:
        _record_open_h74_cycle(conn, ownership)
        conn.execute(
            """
            UPDATE orders
            SET h74_position_ownership_contract_hash=?
            WHERE client_order_id='h74-entry-order'
            """,
            ("sha256:" + "0" * 64,),
        )
        conn.commit()
    finally:
        conn.close()
    summary = _build_h74_sell_summary(db_path)

    plan = summary.typed_target_submit_plan()
    assert plan is not None
    payload = plan.as_dict()

    assert payload["submit_expected"] is False
    assert payload["block_reason"] == "h74_sell_identity_entry_buy_mismatch:h74_position_ownership_contract_hash"
    assert payload.get("h74_position_ownership_contract_hash") != "sha256:" + "0" * 64
