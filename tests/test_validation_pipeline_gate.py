from __future__ import annotations

from types import SimpleNamespace

from market_research.research.final_selection import build_selection_artifact
from market_research.research.hashing import sha256_prefixed
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


def _candidate_scores() -> list[dict[str, object]]:
    score_material = {"candidate_id": "candidate-a", "eligible": True}
    return [
        {**score_material, "score_hash": sha256_prefixed(score_material)}
    ]


def _artifact(candidate: dict[str, object]) -> dict[str, object]:
    candidate_scores = _candidate_scores()
    result = build_selection_artifact(
        manifest_hash="sha256:" + "a" * 64,
        selection_result={
            "selected_candidate_id": "candidate-a",
            "final_selection_contract_hash": "sha256:" + "3" * 64,
            "candidate_final_scores": candidate_scores,
            "candidate_final_scores_hash": sha256_prefixed(candidate_scores),
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


def _report(
    *, candidate: dict[str, object], artifact: dict[str, object], **overrides
):
    candidate_scores = _candidate_scores()
    payload = {
        "manifest_hash": artifact["manifest_hash"],
        "selected_candidate_id": artifact["selected_candidate_id"],
        "final_selection_contract_hash": artifact["final_selection_contract_hash"],
        "candidate_final_scores": candidate_scores,
        "candidate_final_scores_hash": sha256_prefixed(candidate_scores),
        "selection_artifact": artifact,
        "selection_artifact_hash": artifact["content_hash"],
        "candidates": [candidate],
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
    artifact = _artifact(candidate)
    result, stages, reasons = aggregate_validation_gates(
        manifest=_manifest(),
        selection_report=_report(
            candidate=candidate,
            artifact=artifact,
            statistical_gate_result="FAIL",
            validation_eligibility_gate_result="FAIL",
            validation_blocking_reasons=["reality_check_p_value_failed"],
        ),
        selection_artifact=artifact,
        selected_candidate=candidate,
        final_holdout_confirmation=None,
    )

    assert result == "FAIL"
    assert stages["statistical_validation"] == "FAIL"
    assert stages["research_candidate_report"] == "FAIL"
    assert "reality_check_p_value_failed" in reasons


def test_required_final_holdout_missing_is_insufficient_evidence() -> None:
    candidate = _candidate()
    artifact = _artifact(candidate)
    result, stages, reasons = aggregate_validation_gates(
        manifest=_manifest(holdout_required=True),
        selection_report=_report(candidate=candidate, artifact=artifact),
        selection_artifact=artifact,
        selected_candidate=candidate,
        final_holdout_confirmation=None,
    )

    assert result == "INSUFFICIENT_EVIDENCE"
    assert stages["final_holdout"] == "INSUFFICIENT_EVIDENCE"
    assert "final_holdout_required_but_missing" in reasons


def test_all_required_stage_evidence_produces_consistent_pass(monkeypatch) -> None:
    candidate = _candidate()
    artifact = _artifact(candidate)
    monkeypatch.setattr(
        "market_research.research.validation_pipeline.validate_confirmation_artifact",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "market_research.research.validation_pipeline.validate_experiment_registry_binding",
        lambda *_args, **_kwargs: [],
    )
    result, stages, reasons = aggregate_validation_gates(
        manifest=_manifest(holdout_required=True, holdout_present=True),
        selection_report=_report(candidate=candidate, artifact=artifact),
        selection_artifact=artifact,
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


def test_invalid_final_holdout_binding_cannot_produce_terminal_pass(
    monkeypatch,
) -> None:
    candidate = _candidate()
    artifact = _artifact(candidate)
    monkeypatch.setattr(
        "market_research.research.validation_pipeline.validate_confirmation_artifact",
        lambda *_args, **_kwargs: ["final_holdout_confirmation_content_hash_mismatch"],
    )
    monkeypatch.setattr(
        "market_research.research.validation_pipeline.validate_experiment_registry_binding",
        lambda *_args, **_kwargs: [],
    )

    result, stages, reasons = aggregate_validation_gates(
        manifest=_manifest(holdout_required=True, holdout_present=True),
        selection_report=_report(candidate=candidate, artifact=artifact),
        selection_artifact=artifact,
        selected_candidate=candidate,
        final_holdout_confirmation={"confirmation_gate_result": "PASS"},
    )

    assert result == "FAIL"
    assert stages["final_holdout"] == "FAIL"
    assert "final_holdout_confirmation_invalid" in reasons


def test_stale_selection_artifact_cannot_produce_terminal_pass() -> None:
    frozen_candidate = _candidate()
    artifact = _artifact(frozen_candidate)
    substituted_candidate = {
        **frozen_candidate,
        "parameter_candidate_id": "candidate-b",
        "parameter_values": {"x": 2},
        "parameter_values_raw": {"x": 2},
        "compiled_strategy_contract_hash": "sha256:" + "9" * 64,
    }
    report = _report(candidate=substituted_candidate, artifact=artifact)
    report["selected_candidate_id"] = "candidate-b"

    result, stages, reasons = aggregate_validation_gates(
        manifest=_manifest(),
        selection_report=report,
        selection_artifact=artifact,
        selected_candidate=substituted_candidate,
        final_holdout_confirmation=None,
    )

    assert result == "FAIL"
    assert stages["research_candidate_report"] == "FAIL"
    assert "selection_artifact_selected_candidate_mismatch" in reasons
