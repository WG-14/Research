from __future__ import annotations

import csv

import pytest

from bithumb_bot.research.forward_diagnostics_report import validate_forward_diagnostics_report_flags
from bithumb_bot.research.forward_targets import ForwardDiagnosticsMeasurementContract
from tests.test_forward_diagnostics_report import _manager, _manifest, _result
from bithumb_bot.research.forward_diagnostics_report import write_forward_diagnostics_report


def test_report_includes_measurement_contract(tmp_path) -> None:
    report = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())

    assert report["measurement_contract"] == {
        "return_basis": "gross_forward_return",
        "cost_adjustment": "none",
        "diagnostic_cost_model": "none",
        "execution_simulation": False,
        "fill_simulation": False,
        "order_lifecycle_simulation": False,
        "operator_interpretation": "feature_mining_only_not_expected_pnl",
    }


def test_measurement_contract_rejects_execution_simulation_true() -> None:
    with pytest.raises(ValueError, match="execution_simulation"):
        ForwardDiagnosticsMeasurementContract(execution_simulation=True)


def test_measurement_contract_rejects_fill_simulation_true() -> None:
    with pytest.raises(ValueError, match="fill_simulation"):
        ForwardDiagnosticsMeasurementContract(fill_simulation=True)


def test_metrics_csv_includes_measurement_contract_columns(tmp_path) -> None:
    manager = _manager(tmp_path)
    write_forward_diagnostics_report(manager=manager, manifest=_manifest(), result=_result())
    csv_path = manager.data_dir() / "derived/research/exp1/forward_diagnostics/feature_bucket_metrics.csv"
    header = next(csv.reader(csv_path.read_text(encoding="utf-8").splitlines()))

    for column in ("return_basis", "cost_adjustment", "execution_simulation", "fill_simulation"):
        assert column in header


def test_report_validation_rejects_missing_measurement_contract(tmp_path) -> None:
    report = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())
    report.pop("measurement_contract")

    with pytest.raises(ValueError, match="measurement_contract"):
        validate_forward_diagnostics_report_flags(report)


def test_report_validation_rejects_non_gross_return_basis(tmp_path) -> None:
    report = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())
    report["measurement_contract"] = dict(report["measurement_contract"])
    report["measurement_contract"]["return_basis"] = "net_forward_return"

    with pytest.raises(ValueError, match="measurement_contract"):
        validate_forward_diagnostics_report_flags(report)
