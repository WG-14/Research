from __future__ import annotations

from bithumb_bot.h74_probe_acceptance import evaluate_h74_execution_path_probe_acceptance


def _pass_report() -> dict[str, object]:
    return {
        "artifact_type": "h74_execution_path_probe_report",
        "probe_run_id": "probe-1",
        "execution_path_probe_status": "PASS",
        "buy_decision_id": 1,
        "buy_execution_plan_id": 2,
        "buy_order_id": 3,
        "buy_client_order_id": "buy-1",
        "buy_fill_id": 4,
        "open_lot_id": 5,
        "sell_decision_id": 6,
        "sell_execution_plan_id": 7,
        "sell_order_id": 8,
        "sell_client_order_id": "sell-1",
        "sell_fill_id": 9,
        "lifecycle_id": 10,
        "accounting": {"validated": True},
        "final_flat_or_documented_dust": True,
        "research_equivalence": False,
        "research_equivalence_status": "NOT_APPLICABLE",
        "production_approval": False,
    }


def test_acceptance_consumes_probe_report_schema() -> None:
    result = evaluate_h74_execution_path_probe_acceptance(_pass_report())
    assert result["execution_path_probe_status"] == "PASS"
    assert result["acceptance_track"] == "execution_path_probe"


def test_acceptance_rejects_report_without_lifecycle_id() -> None:
    report = _pass_report()
    report["lifecycle_id"] = None
    result = evaluate_h74_execution_path_probe_acceptance(report)
    assert result["execution_path_probe_status"] != "PASS"
    assert "lifecycle_id" in result["missing_evidence"]


def test_acceptance_artifact_never_enables_research_or_production() -> None:
    result = evaluate_h74_execution_path_probe_acceptance(_pass_report())
    assert result["research_equivalence"] is False
    assert result["research_equivalence_status"] == "NOT_APPLICABLE"
    assert result["production_approval"] is False
    assert result["promotion_grade"] is False
