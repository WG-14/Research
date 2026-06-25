from __future__ import annotations

from collections.abc import Mapping


REQUIRED_REPORT_FIELDS = (
    "buy_decision_id",
    "buy_execution_plan_id",
    "buy_order_id",
    "buy_client_order_id",
    "buy_fill_id",
    "open_lot_id",
    "sell_decision_id",
    "sell_execution_plan_id",
    "sell_order_id",
    "sell_client_order_id",
    "sell_fill_id",
    "lifecycle_id",
)


def evaluate_h74_execution_path_probe_acceptance(report: Mapping[str, object]) -> dict[str, object]:
    if str(report.get("artifact_type") or "") != "h74_execution_path_probe_report":
        missing = ["h74_execution_path_probe_report_schema"]
    else:
        missing = [key for key in REQUIRED_REPORT_FIELDS if not report.get(key)]
    accounting = report.get("accounting")
    if not isinstance(accounting, Mapping) or not bool(accounting.get("validated")):
        missing.append("accounting.validated")
    if not bool(report.get("final_flat_or_documented_dust")):
        missing.append("final_flat_or_documented_dust")
    report_status = str(report.get("execution_path_probe_status") or "")
    if report_status != "PASS":
        missing.append("execution_path_probe_status")

    status = "PASS" if not missing else "INCOMPLETE"
    return {
        "artifact_type": "h74_execution_path_probe_acceptance",
        "acceptance_track": "execution_path_probe",
        "probe_run_id": str(report.get("probe_run_id") or ""),
        "execution_path_probe_status": status,
        "source_execution_path_probe_status": report_status,
        "missing_evidence": missing,
        "research_equivalence": False,
        "research_equivalence_status": "NOT_APPLICABLE",
        "production_approval": False,
        "promotion_grade": False,
    }
