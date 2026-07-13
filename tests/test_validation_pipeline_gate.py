from __future__ import annotations

from types import SimpleNamespace

from market_research.research.final_selection import build_selection_artifact
from market_research.research.validation_pipeline import aggregate_validation_gates


def _candidate() -> dict[str, object]:
    return {
        "parameter_candidate_id": "candidate-a",
        "parameter_values": {"x": 1},
        "parameter_values_raw": {"x": 1},
        "effective_strategy_parameters_hash": "sha256:" + "1" * 64,
        "compiled_strategy_contract_hash": "sha256:" + "2" * 64,
        "validation_metrics": {"return_pct": 1.0},
        "validation_metrics_v2": {"return_risk": {"total_return_pct": 1.0}},
        "validation_stress_suite": {"gate_result": "PASS"},
        "walk_forward_metrics": None,
        "acceptance_gate_result": "PASS",
    }


def _artifact(candidate: dict[str, object]) -> dict[str, object]:
    result = build_selection_artifact(
        manifest_hash="sha256:" + "a" * 64,
        selection_result={
            "selected_candidate_id": "candidate-a",
            "final_selection_contract_hash": "sha256:" + "3" * 64,
            "candidate_final_scores_hash": "sha256:" + "4" * 64,
        },
        candidates=[candidate],
    )
    assert result is not None
    return result


def _manifest(*, holdout_required: bool = False, holdout_present: bool = False):
    return SimpleNamespace(
        acceptance_gate=SimpleNamespace(
            walk_forward_required=False,
            final_holdout_required_for_validation=holdout_required,
        ),
        stress_suite=SimpleNamespace(required_for_validation=True),
        statistical_validation=SimpleNamespace(required_for_validation=True),
        final_selection=SimpleNamespace(required_for_validation=True),
        dataset=SimpleNamespace(
            split=SimpleNamespace(final_holdout=object() if holdout_present else None),
        ),
    )


def _report(**overrides):
    payload = {
        "dataset_quality_gate_status": "PASS",
        "stress_suite_gate_result": "PASS",
        "statistical_gate_result": "PASS",
        "final_selection_gate_result": "PASS",
        "validation_eligibility_gate_result": "PASS",
        "validation_blocking_reasons": [],
    }
    payload.update(overrides)
    return payload


def test_failed_statistical_validation_cannot_produce_terminal_pass() -> None:
    candidate = _candidate()
    result, stages, reasons = aggregate_validation_gates(
        manifest=_manifest(),
        selection_report=_report(
            statistical_gate_result="FAIL",
            validation_eligibility_gate_result="FAIL",
            validation_blocking_reasons=["reality_check_p_value_failed"],
        ),
        selection_artifact=_artifact(candidate),
        selected_candidate=candidate,
        final_holdout_confirmation=None,
    )

    assert result == "FAIL"
    assert stages["statistical_validation"] == "FAIL"
    assert stages["research_candidate_report"] == "FAIL"
    assert "reality_check_p_value_failed" in reasons


def test_required_final_holdout_missing_is_insufficient_evidence() -> None:
    candidate = _candidate()
    result, stages, reasons = aggregate_validation_gates(
        manifest=_manifest(holdout_required=True),
        selection_report=_report(),
        selection_artifact=_artifact(candidate),
        selected_candidate=candidate,
        final_holdout_confirmation=None,
    )

    assert result == "INSUFFICIENT_EVIDENCE"
    assert stages["final_holdout"] == "INSUFFICIENT_EVIDENCE"
    assert "final_holdout_required_but_missing" in reasons


def test_all_required_stage_evidence_produces_consistent_pass() -> None:
    candidate = _candidate()
    result, stages, reasons = aggregate_validation_gates(
        manifest=_manifest(holdout_required=True, holdout_present=True),
        selection_report=_report(),
        selection_artifact=_artifact(candidate),
        selected_candidate=candidate,
        final_holdout_confirmation={"confirmation_gate_result": "PASS"},
    )

    assert result == "PASS"
    assert reasons == []
    assert all(
        stage == "PASS"
        for name, stage in stages.items()
        if name not in {"walk_forward", "backtest"}
    )
