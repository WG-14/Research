from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import replace

import pytest

from bithumb_bot.promotion_provenance import validate_promotion_artifact
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


def _flat_position():
    from bithumb_bot.strategy_policy_contract import PositionSnapshot

    return PositionSnapshot(
        in_position=False,
        entry_allowed=True,
        exit_allowed=False,
        terminal_state="flat",
        effective_flat=True,
    )


def _promotion_candidate() -> dict[str, object]:
    from tests.test_research_promotion_gate import _production_candidate

    return _production_candidate()
