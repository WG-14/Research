from __future__ import annotations

from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.governance import (
    GovernanceError,
    GovernanceSubject,
    GovernanceSubjectType,
    append_lifecycle_transition,
    current_lifecycle_state,
    validate_governance_registry,
)
from market_research.settings import ResearchSettings


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


def _transition(
    manager: ResearchPathManager,
    subject: GovernanceSubject,
    source: str | None,
    target: str,
    evidence: dict[str, str] | None = None,
) -> None:
    append_lifecycle_transition(
        manager=manager,
        subject=subject,
        from_state=source,
        to_state=target,
        actor_id="researcher-a",
        reason=f"advance frozen research lifecycle to {target}",
        evidence_hashes=evidence,
        recorded_at="2026-01-01T00:00:00+00:00",
    )


def test_complete_research_lifecycle_requires_every_evidence_gate(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    subject = GovernanceSubject(
        GovernanceSubjectType.HYPOTHESIS,
        "hypothesis-complete-lifecycle",
        "1",
    )

    _transition(
        manager,
        subject,
        None,
        "IDEA",
        {"hypothesis_semantic_fingerprint": _hash("0")},
    )
    _transition(
        manager,
        subject,
        "IDEA",
        "STRUCTURED",
        {"hypothesis_contract_hash": _hash("1")},
    )
    _transition(manager, subject, "STRUCTURED", "EXPLORATORY")
    _transition(
        manager,
        subject,
        "EXPLORATORY",
        "PREREGISTERED",
        {"preregistration_hash": _hash("2")},
    )
    _transition(
        manager,
        subject,
        "PREREGISTERED",
        "VALIDATING",
        {"validation_manifest_hash": _hash("3")},
    )

    with pytest.raises(GovernanceError, match="validation_decision_hash"):
        _transition(manager, subject, "VALIDATING", "VALIDATED")

    _transition(
        manager,
        subject,
        "VALIDATING",
        "VALIDATED",
        {
            "validation_decision_hash": _hash("4"),
            "validation_report_hash": _hash("5"),
        },
    )
    _transition(
        manager,
        subject,
        "VALIDATED",
        "PROSPECTIVE_VALIDATION",
        {"prospective_validation_spec_hash": _hash("6")},
    )
    _transition(
        manager,
        subject,
        "PROSPECTIVE_VALIDATION",
        "CONFIRMED",
        {
            "prospective_evaluation_hash": _hash("7"),
            "research_conclusion_hash": _hash("8"),
        },
    )

    assert current_lifecycle_state(manager=manager, subject=subject) == "CONFIRMED"
    assert validate_governance_registry(manager)["status"] == "PASS"


def test_prospective_inconclusive_requires_evaluation_evidence(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    subject = GovernanceSubject(
        GovernanceSubjectType.HYPOTHESIS,
        "hypothesis-inconclusive-lifecycle",
        "1",
    )
    _transition(
        manager,
        subject,
        None,
        "IDEA",
        {"hypothesis_semantic_fingerprint": _hash("0")},
    )
    _transition(
        manager,
        subject,
        "IDEA",
        "STRUCTURED",
        {"hypothesis_contract_hash": _hash("1")},
    )
    _transition(manager, subject, "STRUCTURED", "EXPLORATORY")
    _transition(
        manager,
        subject,
        "EXPLORATORY",
        "PREREGISTERED",
        {"preregistration_hash": _hash("2")},
    )
    _transition(
        manager,
        subject,
        "PREREGISTERED",
        "VALIDATING",
        {"validation_manifest_hash": _hash("3")},
    )
    _transition(
        manager,
        subject,
        "VALIDATING",
        "VALIDATED",
        {
            "validation_decision_hash": _hash("4"),
            "validation_report_hash": _hash("5"),
        },
    )
    _transition(
        manager,
        subject,
        "VALIDATED",
        "PROSPECTIVE_VALIDATION",
        {"prospective_validation_spec_hash": _hash("6")},
    )

    with pytest.raises(GovernanceError, match="prospective_evaluation_hash"):
        _transition(
            manager,
            subject,
            "PROSPECTIVE_VALIDATION",
            "INCONCLUSIVE",
        )

    _transition(
        manager,
        subject,
        "PROSPECTIVE_VALIDATION",
        "INCONCLUSIVE",
        {"prospective_evaluation_hash": _hash("7")},
    )
    assert current_lifecycle_state(manager=manager, subject=subject) == "INCONCLUSIVE"
