from __future__ import annotations

import copy
from collections import Counter
from pathlib import Path

import pytest

from market_research.research.artifact_contract import apply_artifact_contract, validate_artifact_contract
from market_research.research.experiment_manifest import ManifestValidationError
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
