from __future__ import annotations

import argparse
import json
from pathlib import Path

from market_research.research import cli
from market_research.research.hashing import report_content_hash_payload, sha256_prefixed
from market_research.research_cli.commands import execute_research_command
from tests.test_run_lifecycle import _context
from tests.test_strategy_research_package import _result


def test_governance_cli_records_transition_and_human_change_request(tmp_path: Path) -> None:
    context = _context(tmp_path)
    transition = argparse.Namespace(
        subject_type="strategy_candidate", subject_id="candidate-1", subject_version="1",
        from_state=None, to_state="DRAFT", actor="researcher-a", reason="candidate created",
        evidence=[],
    )
    assert execute_research_command("research-governance-transition", transition, context) == 0

    changes_path = tmp_path / "changes.json"
    changes_path.write_text(json.dumps([{
        "requirement_id": "REQ-1",
        "description": "explain economic mechanism",
        "verification_condition": "report contains reviewed mechanism analysis",
    }]), encoding="utf-8")
    review = argparse.Namespace(
        subject_type="strategy_candidate", subject_id="candidate-1", subject_version="1",
        decision="CHANGES_REQUESTED", reviewer="reviewer-a", reviewer_role="research_reviewer",
        rationale="mechanism explanation is incomplete", reviewed_artifact_hash="sha256:" + "a" * 64,
        requested_changes=str(changes_path), resolved_requirement=[],
    )
    assert execute_research_command("research-record-human-review", review, context) == 0


def test_approval_cli_rejects_report_with_stale_content_hash(tmp_path: Path) -> None:
    context = _context(tmp_path)
    report = _result()
    report["selected_candidate_id"] = "tampered"
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    rc = cli.cmd_research_approve_strategy_candidate(
        context=context, result_path=str(report_path), subject_version="1",
        reviewer_id="approver-a", rationale="review complete",
        resolved_requirement_ids=(), out_path=str(tmp_path / "approval.json"),
    )
    assert rc == 1
    assert not (tmp_path / "approval.json").exists()


def test_approval_cli_rejects_nonpassing_validated_result(tmp_path: Path) -> None:
    context = _context(tmp_path)
    report = _result()
    report.update(
        {
            "schema_version": 3,
            "artifact_type": "validated_research_result",
            "end_to_end_validation_result": "FAIL",
        }
    )
    report["content_hash"] = sha256_prefixed(
        report_content_hash_payload(report)
    )
    report_path = tmp_path / "failed-validation.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    rc = cli.cmd_research_approve_strategy_candidate(
        context=context,
        result_path=str(report_path),
        subject_version="1",
        reviewer_id="approver-a",
        rationale="must not approve failed evidence",
        resolved_requirement_ids=(),
        out_path=str(tmp_path / "approval.json"),
    )

    assert rc == 1
    assert not (tmp_path / "approval.json").exists()


def test_approval_cli_rejects_pass_summary_with_failed_stage(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    output: list[str] = []
    context.printer = output.append
    report = _result()
    next(
        stage
        for stage in report["validation_stages"]
        if stage["name"] == "dataset_quality"
    )["status"] = "FAIL"
    report["validation_blocking_reasons"] = ["dataset_quality_failed"]
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))
    report_path = tmp_path / "contradictory-validation.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    rc = cli.cmd_research_approve_strategy_candidate(
        context=context,
        result_path=str(report_path),
        subject_version="1",
        reviewer_id="approver-a",
        rationale="must not approve contradictory evidence",
        resolved_requirement_ids=(),
        out_path=str(tmp_path / "approval.json"),
    )

    assert rc == 1
    assert not (tmp_path / "approval.json").exists()
    assert any(
        "validated_research_result_stage_not_passed:dataset_quality" in line
        for line in output
    )
