from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import replace

import pytest

from bithumb_bot.promotion_provenance import validate_promotion_artifact
from bithumb_bot.core.sma_policy import (
    ExecutionConstraintSnapshot,
    MarketWindow,
    SmaPolicyConfig,
    _stable_hash,
)
from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.experiment_manifest import DateRange, ExecutionTimingPolicy
from bithumb_bot.research.hashing import sha256_prefixed
from bithumb_bot.research.promotion_gate import (
    build_candidate_profile,
    validate_backtest_candidate_for_promotion,
)
from bithumb_bot.strategy_plugins import sma_with_filter_events
from bithumb_bot.strategy_plugins.sma_with_filter_assembly import (
    MaterializationMode,
    SmaWithFilterPolicyAssembly,
)
from bithumb_bot.strategy_plugins.sma_with_filter_projector import SmaWithFilterSnapshotProjector
from bithumb_bot.strategy.sma_decision_assembler import evaluate_sma_final_decision
from bithumb_bot.strategy.exit_rules import ExitPolicyConfig


HASH = "sha256:" + "a" * 64


def _dataset() -> DatasetSnapshot:
    candles = tuple(
        Candle(
            ts=1_700_000_000_000 + index * 60_000,
            open=100.0 + index,
            high=101.0 + index,
            low=99.0 + index,
            close=100.0 + index,
            volume=10.0 + index,
        )
        for index in range(8)
    )
    return DatasetSnapshot(
        snapshot_id="snap",
        source="unit",
        market="KRW-BTC",
        interval="1m",
        split_name="validation",
        date_range=DateRange(start="2026-01-01", end="2026-01-02"),
        candles=candles,
    )


def _params() -> dict[str, object]:
    return {
        "SMA_SHORT": 2,
        "SMA_LONG": 4,
        "SMA_FILTER_GAP_MIN_RATIO": 0.0,
        "SMA_FILTER_VOL_WINDOW": 3,
        "SMA_FILTER_VOL_MIN_RANGE_RATIO": 0.0,
        "SMA_FILTER_OVEREXT_LOOKBACK": 2,
        "SMA_FILTER_OVEREXT_MAX_RETURN_RATIO": 0.0,
        "SMA_MARKET_REGIME_ENABLED": False,
        "SMA_COST_EDGE_ENABLED": False,
        "SMA_COST_EDGE_MIN_RATIO": 0.0,
        "ENTRY_EDGE_BUFFER_RATIO": 0.0,
        "STRATEGY_MIN_EXPECTED_EDGE_RATIO": 0.0,
        "STRATEGY_ENTRY_SLIPPAGE_BPS": 0.0,
        "LIVE_FEE_RATE_ESTIMATE": 0.0004,
        "STRATEGY_EXIT_RULES": "stop_loss,opposite_cross,max_holding_time",
        "STRATEGY_EXIT_STOP_LOSS_RATIO": 0.0,
        "STRATEGY_EXIT_MAX_HOLDING_MIN": 0,
        "STRATEGY_EXIT_MIN_TAKE_PROFIT_RATIO": 0.0,
        "STRATEGY_EXIT_SMALL_LOSS_TOLERANCE_RATIO": 0.0,
    }


def test_sma_research_events_are_promotion_seed_only() -> None:
    events = sma_with_filter_events.build_sma_with_filter_research_events(
        dataset=_dataset(),
        parameter_values=_params(),
        fee_rate=0.0004,
        slippage_bps=0.0,
        execution_timing_policy=ExecutionTimingPolicy(),
    )

    assert events
    event = events[0]
    for key in ("prev_s", "prev_l", "curr_s", "curr_l", "prev_above", "overextended_ratio", "regime_snapshot"):
        assert key not in event.extra_payload
    for key in ("gap_ratio", "range_ratio", "short_sma", "long_sma"):
        assert key not in event.feature_snapshot
    assert event.extra_payload["seed_contract"] == "PromotionDecisionSeed.v1"


def test_canonical_projector_features_ignore_poisoned_research_event_payload() -> None:
    dataset = _dataset()
    event = sma_with_filter_events.build_sma_with_filter_research_events(
        dataset=dataset,
        parameter_values=_params(),
        fee_rate=0.0004,
        slippage_bps=0.0,
        execution_timing_policy=ExecutionTimingPolicy(),
    )[0]
    poisoned = replace(
        event,
        feature_snapshot={"gap_ratio": 999.0, "range_ratio": 999.0},
        extra_payload={
            **dict(event.extra_payload),
            "prev_s": 999.0,
            "prev_l": 1.0,
            "curr_s": 999.0,
            "curr_l": 1.0,
            "prev_above": True,
        },
    )
    assembly = SmaWithFilterPolicyAssembly()
    materialized = assembly.materialize_parameters(_params(), MaterializationMode.RESEARCH_PROMOTION)
    projector = SmaWithFilterSnapshotProjector(assembly)
    expected = projector.project_features_from_dataset(
        dataset=dataset,
        candle_index=int(event.extra_payload["index"]),
        materialized=materialized,
        through_ts_ms=int(event.candle_ts),
        allow_initial_cross=False,
    )
    projected = projector.project_from_research_event(
        event=poisoned,
        dataset=dataset,
        candle_index=int(event.extra_payload["index"]),
        position=_flat_position(),
        parameter_values=_params(),
        fee_rate=0.0004,
        slippage_bps=0.0,
        active_exit_policy={"rules": []},
        buy_fraction=0.99,
        materialization_mode=MaterializationMode.RESEARCH_PROMOTION,
        candidate_regime_policy={"source": "unit", "allowed_regimes": ["uptrend_normal_vol_volume_increasing"]},
        candidate_regime_policy_enforced=True,
    )

    assert projected is not None
    assert expected is not None
    assert projected.bundle.market.gap_ratio == expected.gap_ratio
    assert projected.bundle.market.curr_s == expected.curr_s
    assert projected.bundle.market.curr_s != 999.0


def test_market_feature_hash_and_policy_input_hash_are_feature_sensitive() -> None:
    config = _policy_config()
    execution = ExecutionConstraintSnapshot(fee_rate_for_decision=0.0)
    position = _flat_position()
    base = _market_window(
        gap_ratio=0.02,
        volatility_ratio=0.03,
        overextended_ratio=0.01,
        market_regime_snapshot={"version": "v1", "composite_regime": "trend"},
    )
    baseline = evaluate_sma_final_decision(
        market=base,
        position=position,
        config=config,
        execution_context=execution,
        exit_policy_config=_exit_policy(),
    )
    baseline_hash = _stable_hash(base.policy_input_payload())
    for changed in (
        _market_window(gap_ratio=0.021, volatility_ratio=0.03, overextended_ratio=0.01),
        _market_window(gap_ratio=0.02, volatility_ratio=0.031, overextended_ratio=0.01),
        _market_window(gap_ratio=0.02, volatility_ratio=0.03, overextended_ratio=0.011),
        _market_window(
            gap_ratio=0.02,
            volatility_ratio=0.03,
            overextended_ratio=0.01,
            market_regime_snapshot={"version": "v1", "composite_regime": "blocked"},
        ),
    ):
        decision = evaluate_sma_final_decision(
            market=changed,
            position=position,
            config=config,
            execution_context=execution,
            exit_policy_config=_exit_policy(),
        )
        assert _stable_hash(changed.policy_input_payload()) != baseline_hash
        assert decision.policy_input_hash != baseline.policy_input_hash


def test_research_and_runtime_projection_share_canonical_feature_hash() -> None:
    dataset = _dataset()
    assembly = SmaWithFilterPolicyAssembly()
    materialized = assembly.materialize_parameters(_params(), MaterializationMode.RESEARCH_PROMOTION)
    projector = SmaWithFilterSnapshotProjector(assembly)
    from_dataset = projector.project_features_from_dataset(
        dataset=dataset,
        candle_index=7,
        materialized=materialized,
        through_ts_ms=dataset.candles[7].ts,
        allow_initial_cross=True,
    )
    from_arrays = projector.project_features_from_arrays(
        pair=dataset.market,
        interval=dataset.interval,
        ts_list=[candle.ts for candle in dataset.candles],
        closes=[candle.close for candle in dataset.candles],
        highs=[candle.high for candle in dataset.candles],
        lows=[candle.low for candle in dataset.candles],
        volumes=[candle.volume for candle in dataset.candles],
        materialized=materialized,
        candle_index=7,
        through_ts_ms=dataset.candles[7].ts,
        allow_initial_cross=True,
    )

    assert from_dataset is not None
    assert from_arrays is not None
    assert from_dataset.feature_hash == from_arrays.feature_hash


def test_decision_input_bundle_hash_semantics_are_explicit() -> None:
    dataset = _dataset()
    assembly = SmaWithFilterPolicyAssembly()
    materialized = assembly.materialize_parameters(_params(), MaterializationMode.RESEARCH_PROMOTION)
    projector = SmaWithFilterSnapshotProjector(assembly)
    projected = projector.project_from_research_event(
        event=sma_with_filter_events.build_sma_with_filter_research_events(
            dataset=dataset,
            parameter_values=_params(),
            fee_rate=0.0004,
            slippage_bps=0.0,
            execution_timing_policy=ExecutionTimingPolicy(),
        )[-1],
        dataset=dataset,
        candle_index=7,
        position=_flat_position(),
        parameter_values=_params(),
        fee_rate=0.0004,
        slippage_bps=0.0,
        active_exit_policy={"rules": []},
        buy_fraction=0.99,
        materialization_mode=MaterializationMode.RESEARCH_PROMOTION,
        candidate_regime_policy={"source": "unit", "allowed_regimes": ["trend"]},
        candidate_regime_policy_enforced=True,
    )

    assert projected is not None
    bundle = projected.bundle
    assert bundle.decision_input_bundle_hash == bundle.decision_input_contract_hash
    assert bundle.decision_input_bundle_payload_hash == _stable_hash(bundle.payload())
    assert bundle.decision_input_bundle_payload_hash != bundle.decision_input_bundle_hash
    assert projected.replay_fingerprint["policy_input_payload_hash"] == bundle.decision_input_bundle_payload_hash
    assert projected.replay_fingerprint["decision_input_contract_hash"] == bundle.decision_input_contract_hash
    assert projected.replay_fingerprint["market_feature_hash"] == bundle.market_feature_hash


def test_final_exit_decision_input_hash_drives_policy_input_hash() -> None:
    config = _policy_config()
    execution = ExecutionConstraintSnapshot(fee_rate_for_decision=0.0)
    market = _market_window(previous_cross_state="above", curr_s=9.0, curr_l=10.0)
    base_position = _open_position(unrealized_pnl_ratio=-0.01, holding_time_sec=10.0)
    baseline = evaluate_sma_final_decision(
        market=market,
        position=base_position,
        config=config,
        execution_context=execution,
        exit_policy_config=_exit_policy(stop_loss_ratio=0.02, max_holding_sec=120.0),
        rule_sources={"stop_loss": "common_risk", "max_holding_time": "common_risk", "opposite_cross": "plugin"},
    )
    variants = (
        _open_position(unrealized_pnl_ratio=-0.03, holding_time_sec=10.0),
        _open_position(unrealized_pnl_ratio=-0.01, holding_time_sec=121.0),
    )
    for position in variants:
        changed = evaluate_sma_final_decision(
            market=market,
            position=position,
            config=config,
            execution_context=execution,
            exit_policy_config=_exit_policy(stop_loss_ratio=0.02, max_holding_sec=120.0),
            rule_sources={"stop_loss": "common_risk", "max_holding_time": "common_risk", "opposite_cross": "plugin"},
        )
        assert changed.policy_input_hash != baseline.policy_input_hash
        assert changed.trace["final_exit_decision_input_hash"] != baseline.trace["final_exit_decision_input_hash"]

    changed_cross = evaluate_sma_final_decision(
        market=_market_window(previous_cross_state="below", curr_s=11.0, curr_l=10.0),
        position=base_position,
        config=config,
        execution_context=execution,
        exit_policy_config=_exit_policy(stop_loss_ratio=0.02, max_holding_sec=120.0),
        rule_sources={"stop_loss": "common_risk", "max_holding_time": "common_risk", "opposite_cross": "plugin"},
    )
    assert changed_cross.policy_input_hash != baseline.policy_input_hash
    assert changed_cross.trace["final_exit_decision_input_hash"] != baseline.trace["final_exit_decision_input_hash"]


def test_promotion_artifact_rejects_incomplete_or_fallback_canonical_decision() -> None:
    payload = {
        "decision_contract_version": 2,
        "authority_plane": "typed_execution_plan_bundle",
        "decision_authority_source": "DecisionEnvelope.strategy_decision",
        "execution_evidence_source": "typed_execution_plan_bundle",
        "execution_plan_bundle_present": True,
        "execution_plan_bundle_hash": HASH,
        "execution_plan_bundle_evidence": {"bundle": "typed"},
        "typed_execution_summary_present": True,
        "execution_summary_hash": HASH,
        "typed_execution_summary_evidence": {"summary": "typed"},
        "execution_submit_plan_hash": HASH,
        "execution_submit_plan_evidence": {"plan": "typed"},
        "runtime_decision_request_hash": HASH,
        "runtime_strategy_set_manifest_hash": HASH,
        "approved_profile_hash": HASH,
        "artifact_grade": "promotion_candidate",
        "policy_materialization_mode": "research_exploratory",
        "runtime_comparable": False,
        "compatibility_fallback": True,
        "allow_execution_compatibility_fallback": True,
    }

    result = validate_promotion_artifact(payload)

    assert not result.ok
    assert "canonical_promotion_research_exploratory_materialization" in result.reason_codes
    assert "canonical_promotion_runtime_comparable_false" in result.reason_codes
    assert "canonical_promotion_compatibility_fallback" in result.reason_codes
    assert "canonical_promotion_policy_input_hash_missing" in result.reason_codes
    assert "canonical_promotion_decision_input_bundle_hash_missing" in result.reason_codes
    assert "canonical_promotion_market_feature_hash_missing" in result.reason_codes
    assert "canonical_promotion_final_exit_decision_input_hash_missing" in result.reason_codes
    assert "canonical_promotion_strategy_evaluation_provenance_missing" in result.reason_codes


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("strategy_parameters_hash", "strategy_parameters_hash_missing"),
        ("approved_profile_hash", "approved_profile_hash_missing"),
        ("fee_authority_hash", "fee_authority_hash_missing"),
        ("fee_model_hash", "fee_model_hash_missing"),
        ("order_rules_hash", "order_rules_hash_missing"),
        ("slippage_model_hash", "slippage_model_hash_missing"),
        ("market_feature_hash", "market_feature_hash_missing"),
        ("final_exit_decision_input_hash", "final_exit_decision_input_hash_missing"),
    ],
)
def test_production_bound_promotion_requires_decision_evidence(field: str, reason: str) -> None:
    candidate = _promotion_candidate()
    candidate[field] = ""
    candidate["candidate_profile_hash"] = sha256_prefixed(build_candidate_profile(candidate))

    allowed, reasons = validate_backtest_candidate_for_promotion(candidate)

    assert not allowed
    assert reason in reasons


def test_static_promotion_projector_does_not_read_event_signal_or_feature_authority() -> None:
    source = inspect.getsource(SmaWithFilterSnapshotProjector.project_from_research_event)
    tree = ast.parse(textwrap.dedent(source))
    forbidden_attrs = {"raw_signal", "final_signal", "entry_signal", "exit_signal", "feature_snapshot", "extra_payload"}
    observed = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in forbidden_attrs
    }
    assert not observed


def test_static_runtime_snapshot_uses_canonical_feature_projector() -> None:
    source = inspect.getsource(__import__("bithumb_bot.runtime_sma_snapshot_builder", fromlist=["_"]))
    assert "project_features_from_arrays(" in source
    assert "classify_market_regime_from_arrays(" not in source
    assert "curr_s > curr_l" not in source


def test_static_execution_paths_consume_typed_decision_authority() -> None:
    import bithumb_bot.research.execution_planning as research_execution_planning
    import bithumb_bot.run_loop_execution_planner as run_loop_execution_planner

    research_source = inspect.getsource(research_execution_planning._research_execution_plan_bundle)
    runtime_source = inspect.getsource(run_loop_execution_planner.ExecutionPlanner._planning_context_from_envelope_input)

    assert "policy_decision.final_signal" in research_source
    assert "policy_decision.execution_intent" in research_source or "TypedExecutionPlanningInput" in research_source
    assert 'context.get("final_signal"' not in research_source
    assert 'context.get("decision_type"' not in research_source
    assert 'context.get("raw_signal"' not in research_source
    assert "getattr(decision, \"final_signal\"" in runtime_source
    assert "getattr(decision, \"execution_intent\"" in runtime_source


def test_runtime_diagnostic_decision_type_is_marked_non_authoritative() -> None:
    source = inspect.getsource(__import__("bithumb_bot.runtime_sma_snapshot_builder", fromlist=["_"]))

    assert '"decision_type_authority": "diagnostic_non_authoritative"' in source
    assert '"signal_observability_authority": "StrategyDecisionV2_non_authoritative_projection"' in source
    assert '"strategy_diagnostics"' in source


def _flat_position():
    from bithumb_bot.strategy_policy_contract import PositionSnapshot

    return PositionSnapshot(
        in_position=False,
        entry_allowed=True,
        exit_allowed=False,
        terminal_state="flat",
        effective_flat=True,
    )


def _open_position(**overrides: object):
    from bithumb_bot.strategy_policy_contract import PositionSnapshot

    values = {
        "in_position": True,
        "entry_allowed": False,
        "exit_allowed": True,
        "terminal_state": "open_exposure",
        "entry_ts": 1_700_000_000_000,
        "entry_price": 100.0,
        "qty_open": 1.0,
        "holding_time_sec": 10.0,
        "unrealized_pnl": -1.0,
        "unrealized_pnl_ratio": -0.01,
        "raw_qty_open": 1.0,
        "raw_total_asset_qty": 1.0,
        "open_lot_count": 1,
        "dust_tracking_lot_count": 0,
        "reserved_exit_lot_count": 0,
        "sellable_executable_lot_count": 1,
        "effective_flat": False,
        "has_executable_exposure": True,
        "has_any_position_residue": True,
    }
    values.update(overrides)
    return PositionSnapshot(**values)


def _market_window(**overrides: object) -> MarketWindow:
    values = {
        "pair": "KRW-BTC",
        "interval": "1m",
        "candle_ts": 1_700_000_420_000,
        "closes": (97.0, 98.0, 99.0, 100.0),
        "prev_s": 98.0,
        "prev_l": 99.0,
        "curr_s": 101.0,
        "curr_l": 100.0,
        "through_ts_ms": 1_700_000_420_000,
        "gap_ratio": 0.02,
        "volatility_ratio": 0.03,
        "overextended_ratio": 0.01,
        "market_regime_snapshot": {"version": "v1", "composite_regime": "trend"},
        "previous_cross_state": "below",
        "allow_initial_cross": False,
    }
    values.update(overrides)
    return MarketWindow(**values)


def _policy_config() -> SmaPolicyConfig:
    return SmaPolicyConfig(
        strategy_name="sma_with_filter",
        short_n=2,
        long_n=4,
        min_gap_ratio=0.0,
        volatility_window=3,
        min_volatility_ratio=0.0,
        overextended_lookback=2,
        overextended_max_return_ratio=1.0,
        slippage_bps=0.0,
        live_fee_rate_estimate=0.0,
        entry_edge_buffer_ratio=0.0,
        cost_edge_enabled=False,
        cost_edge_min_ratio=0.0,
        market_regime_enabled=False,
        buy_fraction=0.99,
        max_order_krw=10000.0,
        strategy_min_expected_edge_ratio=0.0,
        runtime_comparable=True,
        materialization_mode="research_promotion",
    )


def _exit_policy(
    *,
    stop_loss_ratio: float = 0.0,
    max_holding_sec: float = 0.0,
) -> ExitPolicyConfig:
    return ExitPolicyConfig(
        rule_names=("stop_loss", "opposite_cross", "max_holding_time"),
        stop_loss_ratio=stop_loss_ratio,
        max_holding_sec=max_holding_sec,
        min_take_profit_ratio=0.0,
        small_loss_tolerance_ratio=0.0,
        live_fee_rate_estimate=0.0,
    )


def _promotion_candidate() -> dict[str, object]:
    from tests.test_research_promotion_gate import _production_candidate

    return _production_candidate()
