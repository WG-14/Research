from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.hashing import (
    report_content_hash_payload,
    sha256_prefixed,
)
from market_research.research.hypothesis_contract import parse_hypothesis_spec
from market_research.research.knowledge_contract import KnowledgeRef
from market_research.research.knowledge_registry import validate_knowledge_registry
from market_research.research.validation_decision import (
    VALIDATION_DECISION_SCHEMA_VERSION,
    CriterionDecision,
    ValidationDecision,
    ValidationDecisionError,
    preserve_failed_validation,
    preserve_validation_result,
    publish_validation_decision,
    query_validation_decisions,
    validate_validation_decision_registry,
)
from market_research.settings import ResearchSettings
from tests.hypothesis_lineage_fixture import hypothesis_spec_v2


def _manager(tmp_path: Path) -> ResearchPathManager:
    return ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "data",
            artifact_root=tmp_path / "artifacts",
            report_root=tmp_path / "reports",
            cache_root=tmp_path / "cache",
            db_path=tmp_path / "input.sqlite",
            max_workers=1,
            random_seed=0,
        ),
        project_root=Path.cwd(),
    )


def _hash(char: str) -> str:
    return "sha256:" + char * 64


def _hypothesis_ref() -> KnowledgeRef:
    hypothesis = parse_hypothesis_spec(hypothesis_spec_v2())
    return KnowledgeRef(
        "hypothesis",
        hypothesis.hypothesis_id,
        hypothesis.version,
        hypothesis.contract_hash(),
    )


def test_failed_attempt_is_immutable_searchable_negative_evidence(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    hypothesis = parse_hypothesis_spec(hypothesis_spec_v2())
    manifest = SimpleNamespace(
        experiment_id="experiment-failed-validation",
        hypothesis_spec=hypothesis,
        manifest_hash=lambda: _hash("a"),
    )

    row = preserve_failed_validation(
        manager=manager,
        manifest=manifest,
        run_id="RUN-failed-001",
        error=RuntimeError("synthetic failure detail"),
        decided_at="2026-01-02T00:00:00+00:00",
    )
    replay = preserve_failed_validation(
        manager=manager,
        manifest=manifest,
        run_id="RUN-failed-001",
        error=RuntimeError("synthetic failure detail"),
        decided_at="2026-01-02T00:00:00+00:00",
    )

    assert replay == row
    matches = query_validation_decisions(
        manager=manager,
        hypothesis_id=hypothesis.hypothesis_id,
        decision="INCONCLUSIVE",
        failure_type="execution_failure",
    )
    assert len(matches) == 1
    payload = matches[0]["payload"]
    assert payload["run_id"] == "RUN-failed-001"
    assert payload["learned"]
    assert "synthetic failure detail" not in str(payload)
    assert validate_validation_decision_registry(manager)["status"] == "PASS"
    assert validate_knowledge_registry(manager)["status"] == "PASS"

    with pytest.raises(ValueError, match="validation_decision_subject_conflict"):
        preserve_failed_validation(
            manager=manager,
            manifest=manifest,
            run_id="RUN-failed-001",
            error=RuntimeError("different failure"),
            decided_at="2026-01-02T00:00:00+00:00",
        )


def test_validated_decision_requires_every_criterion_to_pass(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    hypothesis = parse_hypothesis_spec(hypothesis_spec_v2())
    decision = ValidationDecision(
        schema_version=VALIDATION_DECISION_SCHEMA_VERSION,
        decision_id="validation-decision-success-001",
        version="1",
        hypothesis_ref=_hypothesis_ref(),
        experiment_id="experiment-validation-success",
        run_id="RUN-success-001",
        decision="VALIDATED",
        criterion_results=(
            CriterionDecision(
                criterion_id="final_holdout_gate",
                passed=True,
                observed="PASS",
                required="PASS",
            ),
            CriterionDecision(
                criterion_id="robustness_gate",
                passed=True,
                observed="PASS",
                required="PASS",
            ),
        ),
        evidence_hashes=(_hash("b"), _hash("c")),
        researcher_interpretation="The preregistered criteria were satisfied.",
        reviewer_comment="Reviewed against the frozen validation protocol.",
        decided_by="reviewer-a",
        decided_at="2026-01-02T00:00:00+00:00",
    )

    row = publish_validation_decision(
        manager=manager,
        hypothesis=hypothesis,
        decision=decision,
    )
    assert row["record_hash"] == decision.content_hash()

    with pytest.raises(
        ValidationDecisionError, match="validation_decision_subject_conflict"
    ):
        publish_validation_decision(
            manager=manager,
            hypothesis=hypothesis,
            decision=replace(
                decision,
                decision_id="validation-decision-conflicting-id",
                reviewer_comment="A conflicting decision for the same immutable run.",
            ),
        )

    with pytest.raises(ValidationDecisionError, match="contains_failed_criterion"):
        ValidationDecision(
            schema_version=VALIDATION_DECISION_SCHEMA_VERSION,
            decision_id="validation-decision-invalid",
            version="1",
            hypothesis_ref=_hypothesis_ref(),
            experiment_id="experiment-validation-invalid",
            run_id="RUN-invalid-001",
            decision="VALIDATED",
            criterion_results=(
                CriterionDecision(
                    criterion_id="final_holdout_gate",
                    passed=False,
                    observed="FAIL",
                    required="PASS",
                ),
            ),
            evidence_hashes=(_hash("d"),),
            researcher_interpretation="Invalid fixture.",
            reviewer_comment="Invalid fixture.",
            decided_by="reviewer-a",
            decided_at="2026-01-02T00:00:00+00:00",
        )


def test_failed_terminal_report_is_rejected_and_searchable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    monkeypatch.setattr(
        "market_research.research.validation_decision.validation_admission_binding_reasons",
        lambda *_args, **_kwargs: [],
    )
    hypothesis = parse_hypothesis_spec(hypothesis_spec_v2())
    manifest = SimpleNamespace(
        experiment_id="experiment-rejected-validation",
        hypothesis_spec=hypothesis,
        manifest_hash=lambda: _hash("e"),
    )

    report = {
        "schema_version": 3,
        "artifact_type": "validated_research_result",
        "experiment_id": manifest.experiment_id,
        "run_id": "RUN-rejected-001",
        "manifest_hash": manifest.manifest_hash(),
        "hypothesis_id": hypothesis.hypothesis_id,
        "hypothesis_version": hypothesis.version,
        "hypothesis_contract_hash": hypothesis.contract_hash(),
        "end_to_end_validation_result": "FAIL",
        "validation_blocking_reasons": ["frozen_acceptance_gate_failed"],
        "validation_stages": [{"name": "final_holdout", "status": "FAIL"}],
    }
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))

    with pytest.raises(
        ValidationDecisionError, match="validation_result_content_hash_mismatch"
    ):
        preserve_validation_result(
            manager=manager,
            manifest=manifest,
            run_id="RUN-rejected-001",
            report={**report, "content_hash": _hash("f")},
            decided_at="2026-01-03T00:00:00+00:00",
        )

    preserve_validation_result(
        manager=manager,
        manifest=manifest,
        run_id="RUN-rejected-001",
        report=report,
        decided_at="2026-01-03T00:00:00+00:00",
    )

    rows = query_validation_decisions(
        manager=manager,
        decision="REJECTED",
        failure_type="validation_criteria_failed",
    )
    assert len(rows) == 1
    assert rows[0]["payload"]["learned"]
    report_path = Path(rows[0]["payload"]["terminal_report_ref"]["artifact_path"])
    report_path.unlink()
    validation = validate_validation_decision_registry(manager)
    assert validation["status"] == "FAIL"
    assert any(
        "terminal_report_ref_unreadable" in item for item in validation["reasons"]
    )
