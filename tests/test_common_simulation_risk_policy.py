from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from market_research.research.dataset_snapshot import Candle, DatasetSnapshot
from market_research.research.decision_event import OrderIntent, ResearchDecisionEvent
from market_research.research.execution_model import FixedBpsExecutionModel
from market_research.research.experiment_manifest import (
    DateRange,
    ExecutionTimingPolicy,
    ManifestValidationError,
    PortfolioPolicy,
    legacy_research_portfolio_policy,
)
from market_research.research.risk_contract import ResearchRiskPolicy
from market_research.research.simulation_engine import (
    run_common_simulation_backtest,
)
from market_research.research.strategy_registry import StrategyRegistry
from market_research.research_composition import (
    parse_builtin_manifest,
    resolve_builtin_strategy as resolve_research_strategy,
)
from tests.test_research_semantics_v2_contract import _manifest_payload


def _scripted_plugin():
    base = resolve_research_strategy("noop_baseline")

    def event_builder(**values):
        candle = values["dataset"].candles[-1]
        side = {1: "BUY", 2: "SELL"}.get(int(candle.volume))
        if side is None:
            return ()
        event = ResearchDecisionEvent(
            candle_ts=int(candle.ts),
            decision_ts=int(candle.ts) + 60_000,
            strategy_name=base.name,
            strategy_version=base.version,
            raw_signal=side,
            final_signal=side,
            entry_signal=side if side == "BUY" else None,
            exit_signal=side if side == "SELL" else None,
            reason="scripted_risk_fixture",
            feature_snapshot={},
            strategy_diagnostics={},
        )
        return (
            replace(
                event,
                order_intent=OrderIntent.from_decision(
                    decision_id=event.decision_id(),
                    side=side,
                    sizing=(
                        "portfolio_policy_fractional_cash"
                        if side == "BUY"
                        else "full_position"
                    ),
                    decision_ts=event.decision_ts,
                    reason="scripted_risk_fixture",
                ),
            ),
        )

    return replace(base, event_builder=event_builder, runtime_factory=None)


def _dataset(
    *,
    volumes: tuple[float, ...],
    prices: tuple[float, ...] | None = None,
    start_ts: int = 0,
) -> DatasetSnapshot:
    resolved_prices = prices or tuple(100.0 for _ in volumes)
    assert len(resolved_prices) == len(volumes)
    return DatasetSnapshot(
        "risk-engine",
        "fixture",
        "KRW-BTC",
        "1m",
        "validation",
        DateRange("2026-01-01", "2026-01-02"),
        tuple(
            Candle(
                start_ts + index * 60_000,
                price,
                price,
                price,
                price,
                volume,
            )
            for index, (price, volume) in enumerate(zip(resolved_prices, volumes))
        ),
    )


def _portfolio_policy(
    *,
    starting_cash: float = 1_000.0,
    min_order_krw: float | None = None,
    max_order_krw: float | None = None,
    rounding_policy: str = "engine_float_no_exchange_lot_rounding",
) -> PortfolioPolicy:
    base = legacy_research_portfolio_policy()
    sizing = replace(
        base.position_sizing,
        buy_fraction=0.5,
        cash_buffer_policy="derived_from_buy_fraction_before_fees",
        min_order_krw=min_order_krw,
        max_order_krw=max_order_krw,
        rounding_policy=rounding_policy,
    )
    return replace(
        base,
        starting_cash_krw=starting_cash,
        position_sizing=sizing,
        source="risk_policy_fixture",
    )


def _run(
    *,
    volumes: tuple[float, ...],
    prices: tuple[float, ...] | None = None,
    start_ts: int = 0,
    risk_policy: ResearchRiskPolicy | None = None,
    portfolio_policy: PortfolioPolicy | None = None,
):
    plugin = _scripted_plugin()
    return run_common_simulation_backtest(
        plugin=plugin,
        registry=StrategyRegistry.build((plugin,)),
        dataset=_dataset(volumes=volumes, prices=prices, start_ts=start_ts),
        parameter_values={},
        fee_rate=0.0,
        slippage_bps=0.0,
        execution_model=FixedBpsExecutionModel(0.0, 0.0),
        execution_timing_policy=ExecutionTimingPolicy(
            fill_reference_policy="next_candle_open",
            allow_same_candle_close_fill=False,
        ),
        portfolio_policy=portfolio_policy or _portfolio_policy(),
        risk_policy=risk_policy or ResearchRiskPolicy(),
    )


def _risk_reasons(run) -> list[str]:
    return [
        str(item["reason_code"])
        for item in run.execution_event_summary["risk_decision_evidence"]
    ]


def test_max_daily_order_count_blocks_at_limit_and_preserves_evidence():
    run = _run(
        volumes=(1.0, 2.0, 0.0),
        risk_policy=ResearchRiskPolicy(max_daily_order_count=1),
    )

    assert len(run.execution_requests) == 1
    assert _risk_reasons(run) == ["none", "max_daily_order_count_reached"]
    rejected = run.execution_event_summary["risk_decision_evidence"][-1]
    assert rejected["allowed"] is False
    assert rejected["risk_context"]["daily_order_count"] == 1
    assert rejected["evidence_hash"].startswith("sha256:")


def test_daily_order_count_uses_utc_day_rollover():
    # First decision is 23:59 UTC; the second is exactly 00:00 UTC.
    run = _run(
        volumes=(1.0, 2.0, 0.0),
        start_ts=86_280_000,
        risk_policy=ResearchRiskPolicy(max_daily_order_count=1),
    )

    assert len(run.execution_requests) == 2
    contexts = [
        item["risk_context"]
        for item in run.execution_event_summary["risk_decision_evidence"]
    ]
    assert [(item["utc_day"], item["daily_order_count"]) for item in contexts] == [
        (0, 0),
        (1, 0),
    ]
    assert run.execution_event_summary["risk_runtime_state"][
        "order_counts_by_utc_day"
    ] == [{"utc_day": 0, "count": 1}, {"utc_day": 1, "count": 1}]


def test_max_trade_count_per_day_counts_only_portfolio_applied_fills():
    run = _run(
        volumes=(1.0, 2.0, 0.0),
        risk_policy=ResearchRiskPolicy(max_trade_count_per_day=1),
    )

    assert len(run.execution_requests) == 1
    assert _risk_reasons(run) == ["none", "max_trade_count_per_day_reached"]
    rejected = run.execution_event_summary["risk_decision_evidence"][-1]
    assert rejected["risk_context"]["daily_trade_count"] == 1
    assert run.execution_event_summary["risk_runtime_state"][
        "trade_counts_by_utc_day"
    ] == [{"utc_day": 0, "count": 1}]


def test_loss_cooldown_blocks_before_and_allows_at_exact_boundary():
    run = _run(
        volumes=(1.0, 2.0, 1.0, 1.0, 0.0),
        prices=(100.0, 100.0, 90.0, 90.0, 90.0),
        risk_policy=ResearchRiskPolicy(cooldown_after_loss_min=2),
    )

    assert _risk_reasons(run) == ["none", "none", "loss_cooldown_active", "none"]
    cooldown = run.execution_event_summary["risk_decision_evidence"][2]
    boundary = run.execution_event_summary["risk_decision_evidence"][3]
    assert cooldown["risk_context"]["last_realized_loss_ts"] == 120_000
    assert cooldown["cooldown_until_ts"] == 240_000
    assert cooldown["risk_context"]["decision_ts"] == 180_000
    assert boundary["risk_context"]["decision_ts"] == 240_000
    assert boundary["allowed"] is True


@pytest.mark.parametrize(
    ("minimum", "maximum", "allowed", "reason"),
    (
        (500.0, None, True, "none"),
        (500.01, None, False, "min_order_notional_not_met"),
        (None, 500.0, True, "none"),
        (None, 499.99, False, "max_order_notional_exceeded"),
    ),
)
def test_buy_notional_bounds_are_inclusive_and_reject_before_request(
    minimum, maximum, allowed, reason
):
    run = _run(
        volumes=(1.0, 0.0),
        portfolio_policy=_portfolio_policy(
            min_order_krw=minimum,
            max_order_krw=maximum,
        ),
    )

    evidence = run.execution_event_summary["order_policy_decision_evidence"][0]
    assert evidence["allowed"] is allowed
    assert evidence["reason_code"] == reason
    assert evidence["effective_notional_krw"] == 500.0
    assert len(run.execution_requests) == int(allowed)
    if allowed:
        assert run.execution_requests[0].requested_notional == 500.0


def test_sell_notional_bound_is_checked_without_changing_full_exit_quantity():
    run = _run(
        volumes=(1.0, 0.0, 2.0, 0.0),
        prices=(100.0, 100.0, 200.0, 200.0),
        portfolio_policy=_portfolio_policy(max_order_krw=999.0),
    )

    evidence = run.execution_event_summary["order_policy_decision_evidence"][-1]
    assert evidence["notional_source"] == (
        "sellable_quantity_times_decision_candle_close"
    )
    assert evidence["effective_notional_krw"] == 1_000.0
    assert evidence["reason_code"] == "max_order_notional_exceeded"
    assert len(run.execution_requests) == 1
    assert run.ledger_entries[0].side == "BUY"
    assert run.resource_usage["final_asset_qty"] == 5.0


def test_unsupported_rounding_and_open_position_semantics_fail_before_events():
    with pytest.raises(ValueError, match="unsupported_position_sizing_rounding_policy"):
        _run(
            volumes=(1.0, 0.0),
            portfolio_policy=_portfolio_policy(rounding_policy="nearest_krw"),
        )
    with pytest.raises(
        ValueError, match="unsupported_max_open_positions_requires_exactly_one"
    ):
        _run(
            volumes=(1.0, 0.0),
            risk_policy=ResearchRiskPolicy(max_open_positions=2),
        )


def test_manifest_accepts_executable_notional_bounds_and_rejects_unsupported_shape():
    payload = _manifest_payload()
    payload["portfolio_policy"] = _portfolio_policy(
        min_order_krw=100.0,
        max_order_krw=500.0,
    ).as_dict()
    payload["portfolio_policy"]["source"] = "manifest"

    manifest = parse_builtin_manifest(payload)
    sizing = manifest.portfolio_policy.position_sizing
    assert sizing.min_order_krw == 100.0
    assert sizing.max_order_krw == 500.0

    bad_rounding = _manifest_payload()
    bad_rounding["portfolio_policy"] = _portfolio_policy().as_dict()
    bad_rounding["portfolio_policy"]["source"] = "manifest"
    bad_rounding["portfolio_policy"]["position_sizing"]["rounding_policy"] = (
        "nearest_krw"
    )
    with pytest.raises(ManifestValidationError, match="rounding_policy"):
        parse_builtin_manifest(bad_rounding)

    bad_risk = _manifest_payload()
    bad_risk["risk_policy"] = {"max_open_positions": 2}
    with pytest.raises(ManifestValidationError, match="supports exactly 1"):
        parse_builtin_manifest(bad_risk)


def test_risk_and_order_evidence_is_parallel_serial_deterministic():
    def execute():
        run = _run(
            volumes=(1.0, 2.0, 0.0),
            risk_policy=ResearchRiskPolicy(max_daily_order_count=1),
            portfolio_policy=_portfolio_policy(max_order_krw=500.0),
        )
        summary = run.execution_event_summary
        return {
            "risk_decision_stream_hash": summary["risk_decision_stream_hash"],
            "order_policy_decision_stream_hash": summary[
                "order_policy_decision_stream_hash"
            ],
            "risk_runtime_state_hash": summary["risk_runtime_state_hash"],
            "execution_request_stream_hash": summary["execution_request_stream_hash"],
            "risk_decision_evidence": summary["risk_decision_evidence"],
            "order_policy_decision_evidence": summary["order_policy_decision_evidence"],
        }

    serial = [execute(), execute()]
    with ThreadPoolExecutor(max_workers=2) as pool:
        parallel = list(pool.map(lambda _: execute(), range(2)))
    assert serial[0] == serial[1] == parallel[0] == parallel[1]


def test_neutral_rounding_policy_is_explicit_effective_evidence():
    run = _run(volumes=(1.0, 0.0))

    effective = run.execution_event_summary["effective_position_sizing_policy"]
    assert effective["rounding_operation"] == (
        "identity_float_no_exchange_lot_rounding"
    )
    assert effective["out_of_bounds_action"] == ("reject_before_execution_request")
    assert (
        run.execution_event_summary["declared_risk_policy_hash"]
        == (run.execution_event_summary["executed_risk_policy_hash"])
    )
