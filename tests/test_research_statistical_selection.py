from __future__ import annotations

from bithumb_bot.research.experiment_manifest import parse_manifest
from bithumb_bot.research.statistical_selection import (
    build_statistical_selection_evidence,
    selection_universe_hash,
)


def _manifest():
    return parse_manifest(
        {
            "experiment_id": "stat_exp",
            "hypothesis": "Synthetic edge should survive selection correction.",
            "strategy_name": "sma_with_filter",
            "market": "KRW-BTC",
            "interval": "1m",
            "dataset": {
                "source": "sqlite_candles",
                "snapshot_id": "snap",
                "train": {"start": "2023-01-01", "end": "2023-01-01"},
                "validation": {"start": "2023-01-02", "end": "2023-01-02"},
                "final_holdout": {"start": "2023-01-03", "end": "2023-01-03"},
            },
            "parameter_space": {"SMA_SHORT": [2, 3], "SMA_LONG": [4]},
            "cost_model": {"fee_rate": 0.0, "slippage_bps": [0]},
            "acceptance_gate": {
                "min_trade_count": 1,
                "max_mdd_pct": 50,
                "min_profit_factor": 1.0,
                "oos_return_must_be_positive": True,
                "parameter_stability_required": False,
            },
            "statistical_validation": {
                "required_for_promotion": True,
                "benchmark": "cash",
                "primary_metric": "net_excess_return",
                "selection_universe": "all_parameter_candidates_all_required_scenarios",
                "multiple_testing_scope": "experiment",
                "bootstrap": {
                    "method": "metric_centered_max_bootstrap",
                    "n_bootstrap": 100,
                    "block_length_policy": "not_applicable_summary_metric",
                    "seed_policy": "derived_from_selection_universe_hash",
                },
                "gates": {
                    "max_reality_check_p_value": 0.05,
                    "max_spa_p_value": None,
                    "min_deflated_sharpe_probability": None,
                    "max_holdout_reuse_count": 0,
                    "max_attempt_index_without_new_hypothesis": 1,
                },
            },
        }
    )


def _candidates() -> list[dict[str, object]]:
    return [
        {
            "parameter_candidate_id": "candidate_001",
            "parameter_values": {"SMA_SHORT": 2, "SMA_LONG": 4},
            "validation_metrics": {"return_pct": 1.0},
        },
        {
            "parameter_candidate_id": "candidate_002",
            "parameter_values": {"SMA_SHORT": 3, "SMA_LONG": 4},
            "validation_metrics": {"return_pct": 0.0},
        },
    ]


def test_selection_universe_hash_is_deterministic_and_binds_candidates() -> None:
    manifest = _manifest()
    contract = manifest.statistical_validation.as_dict()
    first = selection_universe_hash(
        manifest_hash=manifest.manifest_hash(),
        dataset_content_hash="sha256:dataset",
        dataset_quality_hash="sha256:quality",
        experiment_family_id="family",
        hypothesis_id="hypothesis",
        hypothesis_status="pre_registered",
        candidates=_candidates(),
        required_scenario_ids=["scenario_001"],
        primary_metric_source="validation_metrics",
        benchmark="cash",
        statistical_validation_contract=contract,
    )
    reordered = selection_universe_hash(
        manifest_hash=manifest.manifest_hash(),
        dataset_content_hash="sha256:dataset",
        dataset_quality_hash="sha256:quality",
        experiment_family_id="family",
        hypothesis_id="hypothesis",
        hypothesis_status="pre_registered",
        candidates=list(reversed(_candidates())),
        required_scenario_ids=["scenario_001"],
        primary_metric_source="validation_metrics",
        benchmark="cash",
        statistical_validation_contract=contract,
    )
    changed = _candidates()
    changed[0]["parameter_values"] = {"SMA_SHORT": 5, "SMA_LONG": 9}
    changed_hash = selection_universe_hash(
        manifest_hash=manifest.manifest_hash(),
        dataset_content_hash="sha256:dataset",
        dataset_quality_hash="sha256:quality",
        experiment_family_id="family",
        hypothesis_id="hypothesis",
        hypothesis_status="pre_registered",
        candidates=changed,
        required_scenario_ids=["scenario_001"],
        primary_metric_source="validation_metrics",
        benchmark="cash",
        statistical_validation_contract=contract,
    )

    assert first == reordered
    assert changed_hash != first


def test_statistical_evidence_content_hash_is_stable_and_fails_no_edge_large_universe() -> None:
    manifest = _manifest()
    candidates = _candidates()
    selection_hash = "sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"

    evidence = build_statistical_selection_evidence(
        manifest=manifest,
        candidates=candidates,
        manifest_hash=manifest.manifest_hash(),
        dataset_content_hash="sha256:dataset",
        dataset_quality_hash="sha256:quality",
        experiment_family_id="family",
        hypothesis_id="hypothesis",
        hypothesis_status="pre_registered",
        selection_hash=selection_hash,
        search_budget=5000,
        parameter_grid_size=5000,
        attempt_index=3,
        holdout_reuse_count=2,
        dataset_reuse_policy="reuse_visible",
    )
    repeat = build_statistical_selection_evidence(
        manifest=manifest,
        candidates=candidates,
        manifest_hash=manifest.manifest_hash(),
        dataset_content_hash="sha256:dataset",
        dataset_quality_hash="sha256:quality",
        experiment_family_id="family",
        hypothesis_id="hypothesis",
        hypothesis_status="pre_registered",
        selection_hash=selection_hash,
        search_budget=5000,
        parameter_grid_size=5000,
        attempt_index=3,
        holdout_reuse_count=2,
        dataset_reuse_policy="reuse_visible",
    )

    assert evidence["content_hash"] == repeat["content_hash"]
    assert evidence["statistical_gate_result"] == "FAIL"
    assert "attempt_budget_exceeded" in evidence["gate_fail_reasons"]
    assert "holdout_reuse_budget_exceeded" in evidence["gate_fail_reasons"]
