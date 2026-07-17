from __future__ import annotations

from types import SimpleNamespace

from market_research.research.research_decision_report import (
    REPORT_SECTIONS,
    build_research_decision_report,
    validate_research_decision_report,
)


class _Manifest:
    experiment_id = "decision-report"
    hypothesis = "test hypothesis"
    hypothesis_spec = SimpleNamespace(as_dict=lambda: {"hypothesis_id": "h1"})
    strategy_name = "noop_baseline"
    strategy_version = "v1"
    market = "KRW-BTC"
    interval = "1m"
    risk_policy = SimpleNamespace(
        as_dict=lambda: {"policy_status": "disabled_explicit"}
    )

    @staticmethod
    def manifest_hash():
        return "sha256:" + "1" * 64


def _report():
    selected = {
        "parameter_candidate_id": "candidate-1",
        "primary_scenario_id": "base",
        "scenario_results": [
            {
                "scenario_id": "base",
                "scenario_role": "base",
                "cost_model": {"fee_rate": 0.001},
                "validation_metrics": {"return_pct": 1.0},
                "execution_event_summary": {"fill_count": 2},
            }
        ],
        "validation_stress_suite": {
            "limitations": ["bootstrap_assumption"],
            "period_ablation": {"worst_period": "2025"},
        },
        "walk_forward_metrics": {"windows": [{"window": 1, "return_pct": -1.0}]},
    }
    selection = {
        "content_hash": "sha256:" + "2" * 64,
        "dataset_quality_gate_status": "PASS",
        "dataset_splits": {"validation": {"content_hash": "sha256:" + "3" * 64}},
        "dataset_quality_reports": {"validation": {"coverage_pct": 100.0}},
        "closed_trade_diagnostics_summary": {"top_losing_trades": [{"net_pnl": -1.0}]},
        "cost_assumption_contract": {"scenarios": []},
        "data_limitations": {"queue_available": False},
        "execution_limitations": ["queue_unavailable"],
        "statistical_evidence_limitations": ["finite_sample"],
        "market_regime_bucket_performance": [],
        "allowed_live_regimes": [],
        "blocked_live_regimes": [],
        "stress_suite_gate_result": "PASS",
        "statistical_gate_result": "PASS",
        "walk_forward_gate_result": "PASS",
        "selection_artifact_hash": "sha256:" + "4" * 64,
    }
    confirmation = {
        "content_hash": "sha256:" + "5" * 64,
        "confirmation_gate_result": "PASS",
        "candidate_results": [
            {"candidate_id": "candidate-1", "metrics": {"return_pct": 2.0}}
        ],
    }
    return selected, selection, confirmation


def test_decision_report_contains_every_required_review_section():
    selected, selection, confirmation = _report()
    report = build_research_decision_report(
        manifest=_Manifest(),
        selection_report=selection,
        selected_candidate=selected,
        final_holdout_confirmation=confirmation,
        validation_result="PASS",
        validation_stages=[{"name": "final_selection", "status": "PASS"}],
        blocking_reasons=[],
        run_id="run-1",
    )

    assert set(report["sections"]) == set(REPORT_SECTIONS)
    assert report["sections"]["failure_periods"]["top_losing_trades"]
    assert report["sections"]["research_conclusion"] == {
        "automated_evidence_conclusion": "AUTOMATED_RESEARCH_EVIDENCE_PASSED",
        "validation_result": "PASS",
        "validation_stages": [{"name": "final_selection", "status": "PASS"}],
        "blocking_reasons": [],
        "human_research_decision": "NOT_REVIEWED",
        "operational_permission": False,
    }
    assert validate_research_decision_report(report) == []


def test_decision_report_rejects_missing_section_and_operational_permission():
    selected, selection, confirmation = _report()
    report = build_research_decision_report(
        manifest=_Manifest(),
        selection_report=selection,
        selected_candidate=selected,
        final_holdout_confirmation=confirmation,
        validation_result="FAIL",
        validation_stages=[],
        blocking_reasons=["failed"],
        run_id=None,
    )
    report["sections"].pop("known_limitations")
    report["sections"]["research_conclusion"]["operational_permission"] = True

    reasons = validate_research_decision_report(report)
    assert any(reason.startswith("report_sections_missing") for reason in reasons)
    assert "report_must_not_grant_operational_permission" in reasons
