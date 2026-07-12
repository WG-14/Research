from __future__ import annotations

import copy
from collections import Counter

import pytest

from market_research.research.artifact_contract import apply_artifact_contract, validate_artifact_contract
from market_research.research.experiment_manifest import ManifestValidationError, parse_manifest
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
