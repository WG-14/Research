from __future__ import annotations

from types import SimpleNamespace

from bithumb_bot.research.backtest_engine import run_sma_backtest
from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.experiment_manifest import (
    DateRange,
    PortfolioPolicy,
    PositionSizingPolicy,
    legacy_research_portfolio_policy,
    parse_manifest,
)
from bithumb_bot.research.validation_protocol import (
    MISSING_EXECUTED_PORTFOLIO_POLICY_EVIDENCE_REASON,
    PORTFOLIO_POLICY_EXECUTION_MISMATCH_REASON,
    _classified_fail_reasons,
    _failed_candidate_base_result,
    _invoke_strategy_runner,
    _portfolio_policy_execution_gate_reasons,
    _position_sizing_sensitivity_summary,
)


def _candidate() -> dict[str, object]:
    return {
        "validation_metrics_v2": {
            "total_return_pct": 10.0,
            "max_drawdown_pct": 5.0,
            "profit_factor": 2.0,
        },
        "validation_closed_trades": [
            {"entry_ts": 1, "exit_ts": 2, "return_pct": 10.0, "net_pnl": 99_000.0},
            {"entry_ts": 3, "exit_ts": 4, "return_pct": -5.0, "net_pnl": -49_500.0},
            {"entry_ts": 5, "exit_ts": 6, "return_pct": 20.0, "net_pnl": 198_000.0},
        ],
    }


def _manifest_portfolio_policy(*, starting_cash: float = 100_000.0) -> PortfolioPolicy:
    return PortfolioPolicy(
        schema_version=1,
        starting_cash_krw=starting_cash,
        quote_currency="KRW",
        initial_position_qty=0.0,
        cash_interest_policy="zero",
        position_sizing=PositionSizingPolicy(
            type="fractional_cash",
            buy_fraction=0.99,
            sell_policy="sell_all_available_position",
            cash_buffer_policy="retain_1_percent_before_fees",
            min_order_krw=None,
            max_order_krw=None,
            rounding_policy="engine_float_no_exchange_lot_rounding",
        ),
        source="manifest",
    )


def test_100k_tiny_smoke_produces_99k_entry_notional() -> None:
    candles = tuple(
        Candle(index * 60_000, price, price, price, price, 1.0)
        for index, price in enumerate([100.0, 90.0, 110.0, 90.0, 110.0, 90.0, 110.0, 90.0])
    )
    run = run_sma_backtest(
        dataset=DatasetSnapshot(
            snapshot_id="tiny_100k_entry_notional",
            source="unit",
            market="KRW-BTC",
            interval="1m",
            split_name="validation",
            date_range=DateRange(start="2023-01-01", end="2023-01-01"),
            candles=candles,
        ),
        parameter_values={"SMA_SHORT": 1, "SMA_LONG": 2},
        fee_rate=0.0004,
        slippage_bps=0.0,
        portfolio_policy=_manifest_portfolio_policy(starting_cash=100_000.0),
    )

    assert run.closed_trades
    first = run.closed_trades[0].as_dict()
    assert abs(float(first["entry_notional"]) - 99_000.0) < 1_000.0
    assert float(first["fee_total"]) / 0.0008 < 150_000.0
    assert run.resource_usage is not None
    assert run.resource_usage["ledger_starting_cash_krw"] == 100_000.0
    assert run.resource_usage["executed_portfolio_policy_hash"] == _manifest_portfolio_policy(
        starting_cash=100_000.0
    ).policy_hash()


def test_invoke_strategy_runner_keeps_manifest_starting_cash_when_risk_policy_unsupported() -> None:
    received: dict[str, object] = {}

    def fake_runner(
        *,
        dataset,
        parameter_values,
        fee_rate,
        slippage_bps,
        parameter_stability_score=None,
        execution_model=None,
        execution_timing_policy=None,
        portfolio_policy=None,
        context=None,
    ):
        received["starting_cash_krw"] = portfolio_policy.starting_cash_krw
        received["risk_policy_present"] = "risk_policy" in locals()
        return SimpleNamespace(resource_usage={})

    _invoke_strategy_runner(
        runner=fake_runner,
        dataset=DatasetSnapshot(
            snapshot_id="empty",
            source="unit",
            market="KRW-BTC",
            interval="1m",
            split_name="validation",
            date_range=DateRange(start="2023-01-01", end="2023-01-01"),
            candles=(),
        ),
        parameter_values={},
        fee_rate=0.0004,
        slippage_bps=0.0,
        parameter_stability_score=None,
        execution_model=None,
        execution_timing_policy=None,
        portfolio_policy=_manifest_portfolio_policy(starting_cash=100_000.0),
        risk_policy=object(),
        context=None,
    )

    assert received["starting_cash_krw"] == 100_000.0


def test_portfolio_policy_mismatch_fails_with_fixed_reason() -> None:
    assert _portfolio_policy_execution_gate_reasons(
        {
            "work_unit_portfolio_policy_hash": "sha256:manifest",
            "executed_portfolio_policy_hash": "sha256:legacy",
        }
    ) == [PORTFOLIO_POLICY_EXECUTION_MISMATCH_REASON]


def test_portfolio_policy_gate_distinguishes_missing_evidence_from_mismatch() -> None:
    assert _portfolio_policy_execution_gate_reasons(
        {
            "work_unit_portfolio_policy_hash": "sha256:manifest",
            "executed_portfolio_policy_hash": None,
        }
    ) == [MISSING_EXECUTED_PORTFOLIO_POLICY_EVIDENCE_REASON]
    assert _portfolio_policy_execution_gate_reasons(
        {
            "work_unit_portfolio_policy_hash": "sha256:manifest",
            "executed_portfolio_policy_hash": "sha256:legacy",
        }
    ) == [PORTFOLIO_POLICY_EXECUTION_MISMATCH_REASON]


def test_resource_limited_candidate_with_matching_policy_hash_does_not_emit_mismatch() -> None:
    assert PORTFOLIO_POLICY_EXECUTION_MISMATCH_REASON not in _portfolio_policy_execution_gate_reasons(
        {
            "candidate_failed": True,
            "failure_reason": "candidate_resource_limit_exceeded",
            "resource_guard": {"status": "TRIPPED", "reasons": ["max_runtime_exceeded"]},
            "work_unit_portfolio_policy_hash": "sha256:same",
            "executed_portfolio_policy_hash": "sha256:same",
            "train_executed_portfolio_policy_hash": "sha256:same",
            "validation_executed_portfolio_policy_hash": "sha256:same",
        }
    )


def test_failed_candidate_base_result_preserves_policy_evidence_when_failure_has_starting_cash() -> None:
    manifest = parse_manifest(
        {
            "experiment_id": "unit_failure_evidence",
            "hypothesis": "Unit failure evidence preserves portfolio policy.",
            "strategy_name": "sma_with_filter",
            "market": "KRW-BTC",
            "interval": "1m",
            "dataset": {
                "source": "sqlite_candles",
                "snapshot_id": "unit",
                "source_uri": "managed-db:unit",
                "source_content_hash": "sha256:unit-candles-content",
                "source_schema_hash": "sha256:66a0dab69243f592c1dae02908aed5d1bf11194ec0ec692337a85a5636f711d3",
                "locator": {"snapshot_id": "unit", "immutable": True},
                "train": {"start": "2023-01-01", "end": "2023-01-02"},
                "validation": {"start": "2023-01-03", "end": "2023-01-04"},
            },
            "parameter_space": {"SMA_SHORT": [1], "SMA_LONG": [2]},
            "cost_model": {"fee_rate": 0.0004, "slippage_bps": [0]},
            "acceptance_gate": {
                "min_trade_count": 1,
                "max_mdd_pct": 90,
                "min_profit_factor": 0.1,
                "oos_return_must_be_positive": False,
                "parameter_stability_required": False,
            },
            "portfolio_policy": _manifest_portfolio_policy(starting_cash=100_000.0).as_dict(),
        }
    )
    resource_guard = {
        "status": "TRIPPED",
        "split": "train",
        "reasons": ["max_runtime_exceeded"],
        "executed_portfolio_policy": manifest.portfolio_policy.as_dict(),
        "executed_portfolio_policy_hash": manifest.portfolio_policy_hash(),
        "ledger_starting_cash_krw": 100_000.0,
        "ledger_initial_position_qty": 0.0,
        "position_sizing_policy": manifest.portfolio_policy.position_sizing.as_dict(),
    }

    base = _failed_candidate_base_result(
        manifest=manifest,
        candidate_index=0,
        candidate_id="candidate_0",
        params={"SMA_SHORT": 1, "SMA_LONG": 2},
        scenario=manifest.execution_model.scenarios[0],
        scenario_index=0,
        scenario_id="scenario_1",
        reason="candidate_resource_limit_exceeded",
        resource_guard=resource_guard,
    )

    assert base["metrics_v2_source"] == "failure_fallback"
    assert base["ledger_starting_cash_krw"] == 100_000.0
    assert base["executed_portfolio_policy_hash"] == manifest.portfolio_policy_hash()


def test_resource_limited_and_mismatch_classification_keeps_resource_separate() -> None:
    matching = _classified_fail_reasons(["candidate_resource_limit_exceeded", "max_runtime_exceeded"])
    assert matching["resource_integrity_fail_reasons"] == [
        "candidate_resource_limit_exceeded",
        "max_runtime_exceeded",
    ]
    assert PORTFOLIO_POLICY_EXECUTION_MISMATCH_REASON not in matching["simulation_integrity_fail_reasons"]

    missing = _classified_fail_reasons(
        ["candidate_resource_limit_exceeded", MISSING_EXECUTED_PORTFOLIO_POLICY_EVIDENCE_REASON]
    )
    assert missing["simulation_integrity_fail_reasons"] == [
        MISSING_EXECUTED_PORTFOLIO_POLICY_EVIDENCE_REASON
    ]

    different = _classified_fail_reasons(
        ["candidate_resource_limit_exceeded", PORTFOLIO_POLICY_EXECUTION_MISMATCH_REASON]
    )
    assert different["simulation_integrity_fail_reasons"] == [PORTFOLIO_POLICY_EXECUTION_MISMATCH_REASON]


def test_position_sizing_sensitivity_keeps_separate_portfolio_policy_hashes() -> None:
    summary = _position_sizing_sensitivity_summary(
        base_policy=legacy_research_portfolio_policy(),
        candidate=_candidate(),
    )

    by_fraction = summary["by_buy_fraction"]
    assert len(by_fraction) >= 2
    assert by_fraction["0.99"]["portfolio_policy_hash"].startswith("sha256:")
    assert by_fraction["0.10"]["portfolio_policy_hash"].startswith("sha256:")
    assert by_fraction["0.99"]["portfolio_policy_hash"] != by_fraction["0.10"]["portfolio_policy_hash"]


def test_position_sizing_sensitivity_does_not_override_primary_metrics() -> None:
    candidate = _candidate()
    original = dict(candidate["validation_metrics_v2"])

    summary = _position_sizing_sensitivity_summary(
        base_policy=legacy_research_portfolio_policy(),
        candidate=candidate,
    )

    assert candidate["validation_metrics_v2"] == original
    assert summary["primary_metrics_overridden"] is False
    assert summary["promotion_authority"] == "diagnostic_only_excluded_from_promotion"


def test_position_sizing_sensitivity_uses_independent_portfolio_simulation() -> None:
    summary = _position_sizing_sensitivity_summary(
        base_policy=legacy_research_portfolio_policy(),
        candidate=_candidate(),
    )

    assert summary["status"] == "available"
    assert summary["direct_linear_scaling_used"] is False
    assert "missing_reason" not in summary
    assert summary["by_buy_fraction"]["0.50"]["simulation_method"] == "independent_closed_trade_portfolio_replay"
    assert summary["by_buy_fraction"]["0.50"]["validation_trade_count"] == 3


def test_position_sizing_sensitivity_does_not_linearly_scale_primary_metrics() -> None:
    summary = _position_sizing_sensitivity_summary(
        base_policy=legacy_research_portfolio_policy(),
        candidate=_candidate(),
    )

    assert summary["by_buy_fraction"]["0.50"]["validation_return_pct"] != 5.0
    assert summary["by_buy_fraction"]["0.10"]["validation_return_pct"] != 10.0 * (0.10 / 0.99)
    assert summary["by_buy_fraction"]["0.50"]["validation_max_drawdown_pct"] is not None


def test_position_sizing_sensitivity_persists_non_null_metrics_for_each_fraction() -> None:
    summary = _position_sizing_sensitivity_summary(
        base_policy=legacy_research_portfolio_policy(),
        candidate=_candidate(),
    )

    for fraction in ("0.99", "0.50", "0.25", "0.10"):
        result = summary["by_buy_fraction"][fraction]
        assert result["validation_return_pct"] is not None
        assert result["validation_max_drawdown_pct"] is not None
        assert result["validation_profit_factor"] is not None
        assert result["portfolio_policy_hash"].startswith("sha256:")
