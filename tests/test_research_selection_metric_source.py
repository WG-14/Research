from __future__ import annotations

from bithumb_bot.research.experiment_manifest import parse_manifest
from bithumb_bot.research.final_selection import apply_final_selection_contract
from bithumb_bot.research.hashing import sha256_prefixed
from bithumb_bot.research.statistical_selection import build_statistical_selection_evidence
from tests.test_research_final_selection import _context, _final_selection, _manifest_payload


def _candidate() -> dict[str, object]:
    return {
        "parameter_candidate_id": "candidate_001",
        "parameter_values": {"SMA_SHORT": 2, "SMA_LONG": 4},
        "acceptance_gate_result": "PASS",
        "aggregate_acceptance_gate_result": "PASS",
        "primary_scenario_role": "base",
        "primary_metric_source": "primary_base_scenario_alias",
        "primary_metric_source_semantics": "primary_base_scenario_alias",
        "primary_metric_scenario_role": "base",
        "primary_metric_scenario_id": "scenario_base",
        "aggregate_gate_source": "required_scenario_policy",
        "validation_metrics": {"net_excess_return": 1.2, "return_pct": 1.2},
        "metrics_v2_source": "computed",
        "candidate_failed_before_complete_metrics": False,
        "evaluation_status": "completed",
        "metrics_status": "complete",
        "validation_metrics_v2": {
            "trade_quality": {"expectancy_per_trade_krw": 100.0},
            "return_risk": {"max_drawdown_pct": 5.0},
        },
        "final_holdout_metrics_v2": {
            "trade_quality": {"expectancy_per_trade_krw": 90.0},
            "return_risk": {"max_drawdown_pct": 4.0},
        },
        "validation_stress_suite": {"risk_adjusted_score": {"calmar_ratio": 1.0}},
        "benchmark_metrics": {"final_holdout": {"excess_return_vs_buy_and_hold_pct": 1.0}},
        "metrics_schema_version": 2,
        "final_holdout_present": True,
        "statistical_gate_result": "PASS",
        "stress_suite_gate_result": "PASS",
        "production_calibration_policy_result": {"status": "PASS"},
    }


def test_statistical_metric_source_declares_primary_base_semantics() -> None:
    manifest = parse_manifest(_manifest_payload())
    candidate = _candidate()

    evidence = build_statistical_selection_evidence(
        manifest=manifest,
        candidates=[candidate],
        manifest_hash=manifest.manifest_hash(),
        dataset_content_hash="sha256:dataset",
        dataset_quality_hash="sha256:quality",
        experiment_family_id=None,
        hypothesis_id=None,
        hypothesis_status=None,
        selection_hash=sha256_prefixed({"selection": "unit"}),
        required_scenario_ids=["scenario_base", "scenario_stress"],
        search_budget=1,
        parameter_grid_size=1,
        attempt_index=0,
        holdout_reuse_count=0,
        dataset_reuse_policy="new_dataset",
    )

    assert candidate["primary_scenario_role"] == "base"
    assert candidate["aggregate_acceptance_gate_result"] == "PASS"
    assert evidence is not None
    assert evidence["primary_metric_source"] == "validation_metrics"
    assert evidence["primary_metric_source_semantics"] == "primary_base_scenario_alias"
    assert evidence["primary_metric_scenario_role"] == "base"
    assert evidence["aggregate_gate_source"] == "required_scenario_policy"
    assert evidence["selection_metric_policy"]["primary_metric_scenario_role"] == "base"


def test_final_selection_metric_source_declares_primary_base_semantics() -> None:
    candidate = _candidate()

    result = apply_final_selection_contract(
        contract=_final_selection(
            ranking=[
                {
                    "metric": "validation.metrics_v2.trade_quality.expectancy_per_trade_krw",
                    "order": "desc",
                    "required": True,
                }
            ]
        ),
        candidates=[candidate],
        report_context=_context(),
        production_bound=True,
    )

    score = result["candidate_final_scores"][0]
    assert score["eligible"] is True
    assert score["rank_components"][0]["source"] == "validation_metrics_v2"
    assert score["rank_components"][0]["primary_metric_source_semantics"] == "primary_base_scenario_alias"
    assert score["selection_metric_policy"]["primary_metric_scenario_role"] == "base"
    assert score["selection_metric_policy"]["aggregate_gate_source"] == "required_scenario_policy"
