from __future__ import annotations

import pytest

from bithumb_bot.cli.parser import build_parser
from bithumb_bot.cli.registry import command_registry
from bithumb_bot.research.forward_diagnostics_report import (
    validate_forward_diagnostics_report_flags,
    write_forward_diagnostics_report,
)
from tests.test_forward_diagnostics_docs import RUNBOOK
from tests.test_forward_diagnostics_report import _manager, _manifest, _result


def test_forward_diagnostics_runbook_documents_holdout_override() -> None:
    source = RUNBOOK.read_text(encoding="utf-8")

    assert "--allow-final-holdout-diagnostics" in source
    assert "final_holdout_diagnostic_contamination_risk" in source
    assert "registry accounting is not used for this diagnostic override" in source.lower()


def test_runbook_states_holdout_diagnostic_override_registry_policy() -> None:
    source = RUNBOOK.read_text(encoding="utf-8").lower()

    assert "registry accounting is not used for this diagnostic override" in source
    assert "forward-return diagnostics remain report-only policy evidence" in source


def test_forward_diagnostics_runbook_forbidden_uses_match_report_flags(tmp_path) -> None:
    source = RUNBOOK.read_text(encoding="utf-8")
    report = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())

    for phrase, field in (
        ("promotion evidence", "promotion_evidence"),
        ("approved profile evidence", "approved_profile_evidence"),
        ("live readiness evidence", "live_readiness_evidence"),
        ("capital allocation evidence", "capital_allocation_evidence"),
    ):
        assert phrase in source
        assert field in report
        assert report[field] is False
    for field in (
        "diagnostic_only",
        "final_holdout_diagnostic_override",
        "measurement_contract",
        "warnings",
        "evidence_scope",
        "promotion_eligible",
        "promotion_grade",
        "non_promotable",
        "forbidden_uses",
        "operator_next_action",
    ):
        assert field in report


def test_forward_diagnostics_cli_help_matches_runbook_holdout_override(capsys) -> None:
    parser = build_parser(command_registry())

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["research-forward-diagnostics", "--help"])

    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--allow-final-holdout-diagnostics" in RUNBOOK.read_text(encoding="utf-8")
    assert "--allow-final-holdout-diagnostics" in help_text


def test_forward_diagnostics_cli_help_matches_runbook_degraded_override(capsys) -> None:
    parser = build_parser(command_registry())

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["research-forward-diagnostics", "--help"])

    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--allow-degraded-diagnostics" in RUNBOOK.read_text(encoding="utf-8")
    assert "--allow-degraded-diagnostics" in help_text


def test_runbook_and_report_schema_use_same_return_basis(tmp_path) -> None:
    source = RUNBOOK.read_text(encoding="utf-8")
    report = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())

    assert '"return_basis": "gross_forward_return"' in source
    assert report["measurement_contract"]["return_basis"] == "gross_forward_return"


def test_forward_diagnostics_report_flags_reject_forbidden_evidence_true(tmp_path) -> None:
    base = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())

    for field in (
        "promotion_evidence",
        "approved_profile_evidence",
        "live_readiness_evidence",
        "capital_allocation_evidence",
    ):
        payload = dict(base)
        payload[field] = True
        with pytest.raises(ValueError, match="diagnostic-only"):
            validate_forward_diagnostics_report_flags(payload)
