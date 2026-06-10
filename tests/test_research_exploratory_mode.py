from __future__ import annotations

import argparse
from types import SimpleNamespace

from bithumb_bot.cli.commands.research import command_specs
from bithumb_bot.research.experiment_manifest import legacy_research_portfolio_policy
from bithumb_bot.research.run_summary import build_research_run_summary
from bithumb_bot.research.validation_protocol import _attach_candidate_diagnostic_blocks
from bithumb_bot.strategy_plugins.channel_breakout_research import CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN


def _manifest() -> SimpleNamespace:
    return SimpleNamespace(
        research_run=SimpleNamespace(diagnostic_mode="exploratory"),
        portfolio_policy=legacy_research_portfolio_policy(),
    )


def _candidate() -> dict[str, object]:
    return {
        "parameter_candidate_id": "candidate_001",
        "acceptance_gate_result": "PASS",
        "gate_fail_reasons": [],
        "validation_metrics_v2": {"total_return_pct": 10.0, "max_drawdown_pct": 4.0, "profit_factor": 2.0},
        "validation_strategy_diagnostics": {
            "raw_signal_count": 2,
            "final_signal_count": 1,
            "blocked_filter_distribution": {"volume_ratio_below_min": 1},
        },
        "scenario_results": [
            {
                "scenario_id": "base",
                "scenario_role": "base",
                "validation_metrics_v2": {"total_return_pct": 10.0, "profit_factor": 2.0, "trade_count": 3},
            }
        ],
    }


def test_exploratory_mode_does_not_set_promotion_allowed() -> None:
    report = {
        "best_candidate_id": "candidate_001",
        "promotion_eligibility_gate_result": "PASS",
        "gate_result": "PASS",
        "diagnostic_mode": "exploratory",
        "diagnostic_only": True,
        "candidates": [_candidate()],
    }

    summary = build_research_run_summary(report)

    assert summary.promotion_allowed is False
    assert summary.next_action == "revise_hypothesis_from_exploratory_diagnostics"


def test_exploratory_mode_writes_exploratory_result() -> None:
    candidate = _candidate()

    _attach_candidate_diagnostic_blocks(
        candidate=candidate,
        manifest=_manifest(),
        strategy_plugin=CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN,
    )

    assert candidate["exploratory_result"]["promotion_gate_evaluated"] is False
    assert candidate["exploratory_result"]["cost_sensitivity"] == candidate["cost_sensitivity"]
    assert candidate["acceptance_gate_result"] == "FAIL"


def test_exploratory_mode_keeps_acceptance_gate_non_authoritative() -> None:
    candidate = _candidate()

    _attach_candidate_diagnostic_blocks(
        candidate=candidate,
        manifest=_manifest(),
        strategy_plugin=CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN,
    )

    assert candidate["acceptance_gate_status"] == "diagnostic_only"
    assert "exploratory_mode_not_promotable" in candidate["gate_fail_reasons"]


def test_research_backtest_cli_accepts_exploratory_diagnostic_mode() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    spec = next(item for item in command_specs() if item.name == "research-backtest")
    spec.register_parser(subparsers)

    args = parser.parse_args(
        [
            "research-backtest",
            "--manifest",
            "manifest.json",
            "--diagnostic-mode",
            "exploratory",
        ]
    )

    assert args.command == "research-backtest"
    assert args.diagnostic_mode == "exploratory"
