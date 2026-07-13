from __future__ import annotations

import copy
from collections import Counter
from pathlib import Path

import pytest

from market_research.research.artifact_contract import apply_artifact_contract, validate_artifact_contract
from market_research.research.experiment_manifest import ManifestValidationError
from market_research.research.final_selection import (
    apply_final_selection_contract,
    build_selection_artifact,
    validate_selection_artifact,
)
from market_research.research_composition import (
    load_builtin_manifest as load_manifest,
    parse_builtin_manifest as parse_manifest,
)
from market_research.research.run_summary import _next_action


def _manifest_payload() -> dict[str, object]:
    return {
        "experiment_id": "semantics_v2_contract",
        "hypothesis": "research semantics contract",
        "strategy_name": "noop_baseline",
        "research_classification": "research_only",
        "market": "KRW-BTC",
        "interval": "1m",
        "dataset": {
            "source": "sqlite_candles",
            "snapshot_id": "contract",
            "train": {"start": "2026-01-01", "end": "2026-01-01"},
            "validation": {"start": "2026-01-02", "end": "2026-01-02"},
        },
        "parameter_space": {"NOOP_DECISION_START_INDEX": [0]},
        "cost_model": {"fee_rate": 0.001, "slippage_bps": [10]},
        "acceptance_gate": {
            "min_trade_count": 1,
            "max_mdd_pct": 100,
            "min_profit_factor": 0.1,
            "oos_return_must_be_positive": False,
            "parameter_stability_required": False,
            "final_holdout_required_for_validation": False,
        },
    }


def _final_selection_payload(*, schema_version: int = 2, metric: str = "validation.metrics_v2.return_risk.cagr_pct") -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "required_for_validation": False,
        "candidate_universe": "acceptance_gate_passed_required_scenarios",
        "must_pass": {"dataset_quality_gate_status": "PASS"},
        "selection_exposure_policy": {
            "final_holdout_usage": "prohibited_during_selection" if schema_version == 2 else "confirmatory_metric_in_rank",
            "counts_as_holdout_reuse": False if schema_version == 2 else True,
        },
        "method": "lexicographic",
        "null_metric_policy": "fail_if_required_else_worst_rank",
        "ranking": [
            {"metric": metric, "order": "desc", "required": True},
            {"metric": "parameter_candidate_id", "order": "asc", "required": True},
        ],
        "unsupported_metric_policy": {"sharpe_ratio": "fail_if_required", "sortino_ratio": "fail_if_required"},
    }


def _selection_candidate(candidate_id: str, cagr: float, holdout_return: float) -> dict[str, object]:
    return {
        "parameter_candidate_id": candidate_id,
        "parameter_values": {"NOOP_DECISION_START_INDEX": 0},
        "parameter_values_raw": {"NOOP_DECISION_START_INDEX": 0},
        "effective_strategy_parameters_hash": "sha256:" + "1" * 64,
        "compiled_strategy_contract_hash": "sha256:" + "2" * 64,
        "aggregate_acceptance_gate_result": "PASS",
        "acceptance_gate_result": "PASS",
        "metrics_v2_source": "computed",
        "candidate_failed_before_complete_metrics": False,
        "evaluation_status": "completed",
        "metrics_status": "complete",
        "primary_metric_source_semantics": "primary_base_scenario_alias",
        "primary_metric_scenario_role": "base",
        "aggregate_gate_source": "required_scenario_policy",
        "validation_metrics_v2": {"return_risk": {"cagr_pct": cagr}},
        "final_holdout_metrics_v2": {"return_risk": {"return_pct": holdout_return}},
    }


def test_final_selection_rejects_final_holdout_ranking_metric() -> None:
    payload = _manifest_payload()
    payload["final_selection"] = _final_selection_payload(metric="final_holdout.metrics_v2.return_risk.return_pct")

    with pytest.raises(ManifestValidationError, match="must not reference final_holdout"):
        parse_manifest(payload)


def test_legacy_confirmatory_metric_in_rank_is_rejected_not_translated() -> None:
    payload = _manifest_payload()
    payload["final_selection"] = _final_selection_payload(
        schema_version=1,
        metric="final_holdout.metrics_v2.return_risk.return_pct",
    )

    with pytest.raises(ManifestValidationError, match="legacy schema_version 1 is not translated"):
        parse_manifest(payload)


def test_selection_and_artifact_are_invariant_to_final_holdout_metric_values() -> None:
    payload = _manifest_payload()
    payload["final_selection"] = _final_selection_payload()
    manifest = parse_manifest(payload)
    candidates = [_selection_candidate("candidate-a", 10.0, -20.0), _selection_candidate("candidate-b", 5.0, 50.0)]
    reversed_holdout = [_selection_candidate("candidate-a", 10.0, 100.0), _selection_candidate("candidate-b", 5.0, -100.0)]

    first = apply_final_selection_contract(
        contract=manifest.final_selection,
        candidates=candidates,
        report_context={"dataset_quality_gate_status": "PASS"},
        validation_required=False,
    )
    second = apply_final_selection_contract(
        contract=manifest.final_selection,
        candidates=reversed_holdout,
        report_context={"dataset_quality_gate_status": "PASS"},
        validation_required=False,
    )
    first_artifact = build_selection_artifact(manifest_hash=manifest.manifest_hash(), selection_result=first, candidates=candidates)
    second_artifact = build_selection_artifact(manifest_hash=manifest.manifest_hash(), selection_result=second, candidates=reversed_holdout)

    assert first["selected_candidate_id"] == second["selected_candidate_id"] == "candidate-a"
    assert first_artifact == second_artifact
    assert first_artifact is not None
    assert validate_selection_artifact(first_artifact) == []


def test_selection_artifact_rejects_candidate_or_contract_hash_tampering() -> None:
    payload = _manifest_payload()
    payload["final_selection"] = _final_selection_payload()
    manifest = parse_manifest(payload)
    candidates = [_selection_candidate("candidate-a", 10.0, 1.0)]
    selection = apply_final_selection_contract(
        contract=manifest.final_selection,
        candidates=candidates,
        report_context={"dataset_quality_gate_status": "PASS"},
        validation_required=False,
    )
    artifact = build_selection_artifact(manifest_hash=manifest.manifest_hash(), selection_result=selection, candidates=candidates)
    assert artifact is not None

    artifact["selected_candidate_id"] = "candidate-b"
    artifact["compiled_strategy_contract_hash"] = "sha256:" + "3" * 64

    assert "selection_artifact_content_hash_mismatch" in validate_selection_artifact(artifact)


def test_manifest_uses_research_classification_and_validation_contract_names() -> None:
    manifest = parse_manifest(_manifest_payload())

    assert manifest.research_classification == "research_only"
    assert manifest.research_run.diagnostic_mode == "candidate_validation"
    assert "research_classification" in manifest.canonical_payload()
    assert "deployment_tier" not in manifest.canonical_payload()
    assert manifest.acceptance_gate.as_dict()["final_holdout_required_for_validation"] is False


def test_dataset_quality_policy_is_strict_and_hash_equivalent_when_omitted() -> None:
    omitted = parse_manifest(_manifest_payload())
    explicit_payload = copy.deepcopy(_manifest_payload())
    explicit_payload["dataset_quality_policy"] = {
        "dense_candles_required": True,
        "missing_candle_policy": "fail",
    }
    explicit = parse_manifest(explicit_payload)

    assert omitted.canonical_payload()["dataset_quality_policy"] == {
        "dense_candles_required": True,
        "missing_candle_policy": "fail",
    }
    assert explicit.canonical_payload()["dataset_quality_policy"] == omitted.canonical_payload()["dataset_quality_policy"]
    assert explicit.manifest_hash() == omitted.manifest_hash()


def test_default_strict_manifest_hash_is_preserved() -> None:
    manifest_path = Path(__file__).resolve().parents[1] / "examples/research/sma_filter_manifest.example.json"

    assert load_manifest(manifest_path).manifest_hash() == (
        "sha256:0becc8d3c136a813aa13a3400519aa301b7e7647c7a5cff16aa9f64c6aaf01f7"
    )


@pytest.mark.parametrize(
    "policy, message",
    (
        (
            {"dense_candles_required": False, "missing_candle_policy": "diagnostic_only"},
            "dataset_quality_policy.missing_candle_policy must be fail",
        ),
        (
            {"dense_candles_required": False, "missing_candle_policy": "fail"},
            "dataset_quality_policy.dense_candles_required must be true",
        ),
        (
            {"dense_candles_required": True, "missing_candle_policy": None},
            "dataset_quality_policy.missing_candle_policy must be fail",
        ),
    ),
)
def test_dataset_quality_policy_rejects_non_strict_contract(
    policy: dict[str, object], message: str
) -> None:
    payload = _manifest_payload()
    payload["dataset_quality_policy"] = policy

    with pytest.raises(ManifestValidationError, match=message):
        parse_manifest(payload)


def test_dataset_quality_policy_rejects_explicit_top_level_null() -> None:
    payload = _manifest_payload()
    payload["dataset_quality_policy"] = None

    with pytest.raises(
        ManifestValidationError,
        match="dataset_quality_policy must be an object when supplied",
    ):
        parse_manifest(payload)


@pytest.mark.parametrize("legacy_key", ("deployment_tier", "promotion_target"))
def test_legacy_manifest_classification_keys_are_unknown(legacy_key: str) -> None:
    payload = copy.deepcopy(_manifest_payload())
    payload[legacy_key] = "research_only"

    with pytest.raises(ManifestValidationError, match="unknown manifest field"):
        parse_manifest(payload)


def test_diagnostic_artifact_contract_is_research_schema_v2() -> None:
    payload = apply_artifact_contract({"artifact_type": "forward_return_diagnostic_report"})

    assert payload == {
        "artifact_type": "forward_return_diagnostic_report",
        "schema_version": 2,
        "artifact_role": "diagnostic",
        "diagnostic_only": True,
        "validation_evidence": False,
        "candidate_selection_eligible": False,
        "evidence_scope": "diagnostic_feature_mining",
        "forbidden_uses": ["final_candidate_selection", "validation_pass_claim"],
        "researcher_next_action": "run_research_validate_from_fixed_manifest",
    }
    validate_artifact_contract(payload)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    (
        ({"final_selection_gate_failed": True}, "candidate_not_selected_review_final_selection_contract"),
        ({"statistical_gate_failed": True}, "candidate_not_selected_review_statistical_selection"),
        ({"registry_gate_failed": True}, "candidate_not_selected_review_experiment_registry"),
        ({"validation_eligibility_failed": True}, "candidate_ineligible_review_blocking_reasons"),
        ({"top_fail_reasons": Counter({"walk_forward_failed": 1})}, "candidate_not_selected_review_walk_forward_windows"),
        ({"top_fail_reasons": Counter({"profit_factor_failed": 1})}, "candidate_not_selected_revise_strategy_hypothesis"),
        ({"gate_result": "FAIL"}, "inspect_report_or_adjust_hypothesis"),
    ),
)
def test_run_summary_uses_research_candidate_next_actions(kwargs: dict[str, object], expected: str) -> None:
    arguments: dict[str, object] = {
        "validation_allowed": False,
        "has_candidates": True,
        "top_fail_reasons": Counter(),
        "gate_result": "UNKNOWN",
    }
    arguments.update(kwargs)

    assert _next_action(**arguments) == expected  # type: ignore[arg-type]
