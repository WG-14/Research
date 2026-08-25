from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from market_research.research import cli
from market_research.research.governance import governance_registry_path
from market_research.research.hashing import (
    report_content_hash_payload,
    sha256_prefixed,
)
from market_research.research_cli.commands import execute_research_command
from tests.test_run_lifecycle import _context
from tests.test_strategy_research_package import _result


@pytest.mark.parametrize(
    ("command", "arguments"),
    (
        (
            "research-governance-transition",
            argparse.Namespace(
                subject_type="strategy_candidate",
                subject_id="candidate-1",
                subject_version="1",
                from_state=None,
                to_state="RESEARCH_APPROVED",
                actor="forged-admin",
                reason="caller claims approval authority",
                evidence=[],
            ),
        ),
        (
            "research-record-human-review",
            argparse.Namespace(
                subject_type="strategy_candidate",
                subject_id="candidate-1",
                subject_version="1",
                decision="REJECTED",
                reviewer="forged-reviewer",
                reviewer_role="research_approver",
                rationale="caller claims reviewer role",
                reviewed_artifact_hash="sha256:" + "a" * 64,
                requested_changes="/does/not/exist.json",
                resolved_requirement=[],
            ),
        ),
        (
            "research-approve-strategy-candidate",
            argparse.Namespace(
                result="/does/not/exist.json",
                subject_version="1",
                reviewer="forged-approver",
                rationale="caller claims final approval authority",
                resolved_requirement=[],
                verification_id="forged-verification",
                verification_version="1",
                verification_hash="sha256:" + "b" * 64,
                originator=["someone-else"],
                out="/does/not/exist/approval.json",
            ),
        ),
    ),
)
def test_governance_cli_rejects_caller_supplied_identity_without_mutation(
    tmp_path: Path,
    command: str,
    arguments: argparse.Namespace,
) -> None:
    context = _context(tmp_path)
    output: list[str] = []
    context.printer = output.append

    assert execute_research_command(command, arguments, context) == 1
    assert not governance_registry_path(context.paths).exists()
    assert output == [
        "[RESEARCH-GOVERNANCE-CLI-DISABLED] "
        f"command={command} error=authenticated_internal_web_governance_required"
    ]


def test_approval_cli_rejects_report_with_stale_content_hash(tmp_path: Path) -> None:
    context = _context(tmp_path)
    report = _result()
    report["selected_candidate_id"] = "tampered"
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    rc = cli.cmd_research_approve_strategy_candidate(
        context=context,
        result_path=str(report_path),
        subject_version="1",
        reviewer_id="approver-a",
        rationale="review complete",
        resolved_requirement_ids=(),
        verification_id="unresolved-verification",
        verification_version="1",
        verification_hash="sha256:" + "0" * 64,
        originator_ids=("researcher-a",),
        out_path=str(tmp_path / "approval.json"),
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
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))
    report_path = tmp_path / "failed-validation.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    rc = cli.cmd_research_approve_strategy_candidate(
        context=context,
        result_path=str(report_path),
        subject_version="1",
        reviewer_id="approver-a",
        rationale="must not approve failed evidence",
        resolved_requirement_ids=(),
        verification_id="unresolved-verification",
        verification_version="1",
        verification_hash="sha256:" + "0" * 64,
        originator_ids=("researcher-a",),
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
        verification_id="unresolved-verification",
        verification_version="1",
        verification_hash="sha256:" + "0" * 64,
        originator_ids=("researcher-a",),
        out_path=str(tmp_path / "approval.json"),
    )

    assert rc == 1
    assert not (tmp_path / "approval.json").exists()
    assert output == [
        "[RESEARCH-GOVERNANCE-CLI-DISABLED] "
        "command=research-approve-strategy-candidate "
        "error=authenticated_internal_web_governance_required"
    ]
