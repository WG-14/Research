from __future__ import annotations

from dataclasses import replace

import pytest

from market_research.orderbook_depth_store import build_orderbook_depth_snapshot
from market_research.research.dataset_snapshot import TopOfBookQuote
from market_research.research.execution_evidence import (
    ExecutionEvidenceError,
    validate_execution_evidence,
)
from market_research.research.execution_invariants import fill_timeline_violations
from market_research.research.execution_model import (
    DepthWalkExecutionModel,
    FixedBpsExecutionModel,
    StressExecutionModel,
)
from market_research.research.experiment_manifest import (
    ExecutionTimingPolicy,
    legacy_research_portfolio_policy,
)
from market_research.research.hashing import canonical_payload_hash
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import resolve_builtin_strategy
from tests.test_common_simulation_engine import _dataset, _run


_NEXT_OPEN_TIMING = ExecutionTimingPolicy(
    fill_reference_policy="next_candle_open",
    allow_same_candle_close_fill=False,
)


def _refresh_stream_evidence(run):
    """Keep stream metadata honest so tests isolate invariant failures."""

    evidence = dict(run.execution_event_summary or {})
    evidence.update(
        {
            "execution_request_count": len(run.execution_requests),
            "fill_count": len(run.fills),
            "execution_filled_count": sum(
                fill.fill_status in {"filled", "partial"}
                and float(fill.filled_qty) > 0.0
                for fill in run.fills
            ),
            "filled_execution_count": sum(
                fill.fill_status in {"filled", "partial"}
                and float(fill.filled_qty) > 0.0
                for fill in run.fills
            ),
            "portfolio_applied_trade_count": len(run.ledger_entries),
            "execution_request_stream_hash": canonical_payload_hash(
                [item.as_dict() for item in run.execution_requests]
            ),
            "execution_fill_stream_hash": canonical_payload_hash(
                [item.as_dict() for item in run.fills]
            ),
            "ledger_stream_hash": canonical_payload_hash(
                [item.as_dict() for item in run.ledger_entries]
            ),
            "portfolio_ledger_hash": canonical_payload_hash(
                [item.as_dict() for item in run.ledger_entries]
            ),
            "execution_timing_stream_hash": canonical_payload_hash(
                [
                    {
                        "request_id": request.request_id,
                        "decision_ts": request.decision_ts,
                        "order_intent_ts": request.order_intent_ts,
                        "submit_ts_assumption": request.submit_ts_assumption,
                        "fill_reference_ts": request.fill_reference_ts,
                    }
                    for request in run.execution_requests
                ]
            ),
        }
    )
    return replace(run, execution_event_summary=evidence)


def test_duplicate_fills_for_one_request_are_rejected_by_both_gates() -> None:
    model = FixedBpsExecutionModel(fee_rate=0.001, slippage_bps=10.0)
    run = _run(model)
    duplicate = replace(run.fills[0], latency_ms=1, fill_id="")
    corrupted = _refresh_stream_evidence(replace(run, fills=(run.fills[0], duplicate)))

    with pytest.raises(ValueError, match="multiple_fills_for_execution_request"):
        corrupted.validate_execution_lineage()
    with pytest.raises(
        ExecutionEvidenceError, match="multiple_fills_for_execution_request"
    ):
        validate_execution_evidence(
            run=corrupted,
            timing=_NEXT_OPEN_TIMING,
            model=model,
        )


def test_request_without_fill_is_rejected_by_both_gates() -> None:
    model = FixedBpsExecutionModel(fee_rate=0.001, slippage_bps=10.0)
    run = _run(model)
    corrupted = _refresh_stream_evidence(
        replace(run, fills=(), ledger_entries=(), trades=())
    )

    with pytest.raises(ValueError, match="execution_request_fill_bijection_mismatch"):
        corrupted.validate_execution_lineage()
    with pytest.raises(
        ExecutionEvidenceError, match="execution_request_fill_bijection_mismatch"
    ):
        validate_execution_evidence(
            run=corrupted,
            timing=_NEXT_OPEN_TIMING,
            model=model,
        )


def test_duplicate_request_id_is_rejected_by_direct_evidence_gate() -> None:
    model = FixedBpsExecutionModel(fee_rate=0.001, slippage_bps=10.0)
    run = _run(model)
    corrupted = _refresh_stream_evidence(
        replace(run, execution_requests=(run.execution_requests[0],) * 2)
    )

    with pytest.raises(ValueError, match="duplicate_request_id"):
        corrupted.validate_execution_lineage()
    with pytest.raises(ExecutionEvidenceError, match="duplicate_execution_request_id"):
        validate_execution_evidence(
            run=corrupted,
            timing=_NEXT_OPEN_TIMING,
            model=model,
        )


@pytest.mark.parametrize(
    ("field_name", "tampered_value"),
    (
        ("decision_ts", 119_999),
        ("fill_reference_policy", "first_orderbook_after_decision"),
    ),
)
def test_request_fill_causal_and_policy_tampering_is_rejected_by_both_gates(
    field_name: str,
    tampered_value: object,
) -> None:
    model = FixedBpsExecutionModel(fee_rate=0.001, slippage_bps=10.0)
    run = _run(model)
    tampered_fill = replace(
        run.fills[0],
        **{field_name: tampered_value, "fill_id": ""},
    )
    corrupted = _refresh_stream_evidence(replace(run, fills=(tampered_fill,)))
    expected = f"fill_request_field_mismatch:{field_name}"

    with pytest.raises(ValueError, match=expected):
        corrupted.validate_execution_lineage()
    with pytest.raises(ExecutionEvidenceError, match=expected):
        validate_execution_evidence(
            run=corrupted,
            timing=_NEXT_OPEN_TIMING,
            model=model,
        )


def test_consumed_depth_without_snapshot_is_rejected_by_both_gates() -> None:
    model = FixedBpsExecutionModel(fee_rate=0.001, slippage_bps=10.0)
    run = _run(model)
    tampered_fill = replace(run.fills[0], depth_levels_consumed=1, fill_id="")
    corrupted = _refresh_stream_evidence(replace(run, fills=(tampered_fill,)))

    with pytest.raises(ValueError, match="depth_consumed_without_snapshot"):
        corrupted.validate_execution_lineage()
    with pytest.raises(ExecutionEvidenceError, match="depth_consumed_without_snapshot"):
        validate_execution_evidence(
            run=corrupted,
            timing=_NEXT_OPEN_TIMING,
            model=model,
        )


def test_absent_depth_target_resolves_at_deadline_before_portfolio_effective() -> None:
    fill = _run(FixedBpsExecutionModel(0.001, 10.0)).fills[0]
    valid = replace(
        fill,
        depth_reference_target_ts=120_000,
        depth_reference_deadline_ts=120_000,
        depth_resolution_ts=120_000,
        fill_id="",
    )

    assert valid.depth_snapshot_ts is None
    assert fill_timeline_violations(valid) == ()

    early_resolution = replace(
        valid,
        depth_reference_deadline_ts=120_001,
        fill_id="",
    )
    assert "depth_missing_resolution_deadline_mismatch" in fill_timeline_violations(
        early_resolution
    )

    after_effective = replace(
        valid,
        depth_reference_deadline_ts=120_001,
        depth_resolution_ts=120_001,
        fill_id="",
    )
    assert "depth_knowledge_time_after_portfolio_effective" in (
        fill_timeline_violations(after_effective)
    )


@pytest.mark.parametrize(
    "model",
    (
        FixedBpsExecutionModel(fee_rate=0.001, slippage_bps=10.0),
        StressExecutionModel(
            fee_rate=0.001,
            slippage_bps=10.0,
            partial_fill_rate=1.0,
            partial_fill_fraction=0.5,
            seed=1,
        ),
    ),
)
def test_fixed_bps_and_stress_outputs_pass_both_gates(model) -> None:
    run = _run(model)

    run.validate_execution_lineage()
    assert (
        validate_execution_evidence(
            run=run,
            timing=_NEXT_OPEN_TIMING,
            model=model,
        )["status"]
        == "PASS"
    )


def test_depth_walk_output_passes_both_gates() -> None:
    quote = TopOfBookQuote(
        ts=60_500,
        pair="KRW-BTC",
        bid_price=99.0,
        ask_price=101.0,
        spread_bps=200.0,
        source="fixture",
        observed_at_epoch_sec=61.0,
    )
    depth = build_orderbook_depth_snapshot(
        ts=61_500,
        pair="KRW-BTC",
        bid_levels=((99.0, 10_000.0),),
        ask_levels=((101.0, 10_000.0),),
        source="fixture",
        observed_at_epoch_sec=62.0,
    )
    dataset = replace(
        _dataset(),
        top_of_book_event_quotes=(quote,),
        orderbook_depth_snapshots=(depth,),
    )
    timing = ExecutionTimingPolicy(
        fill_reference_policy="first_orderbook_after_decision",
        max_quote_wait_ms=5_000,
        allow_same_candle_close_fill=False,
    )
    model = DepthWalkExecutionModel(fee_rate=0.01)
    run = run_common_simulation_backtest(
        plugin=resolve_builtin_strategy("buy_and_hold_baseline"),
        dataset=dataset,
        parameter_values={"BUY_HOLD_BUY_INDEX": 0},
        fee_rate=0.01,
        slippage_bps=0.0,
        execution_model=model,
        execution_timing_policy=timing,
        portfolio_policy=legacy_research_portfolio_policy(),
    )

    request = run.execution_requests[0]
    fill = run.fills[0]
    entry = run.ledger_entries[0]
    assert fill.depth_levels_consumed == 1
    assert fill.model_version == "research_depth_walk_v2"
    assert fill.filled_notional + fill.fee == pytest.approx(request.requested_notional)
    assert entry.cash_delta == pytest.approx(-(fill.filled_notional + fill.fee))
    assert entry.cash_after >= -1e-8
    run.validate_execution_lineage()
    assert (
        validate_execution_evidence(run=run, timing=timing, model=model)["status"]
        == "PASS"
    )
