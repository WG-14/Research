from __future__ import annotations

from pathlib import Path


def _research_validation_doc() -> str:
    return Path("docs/research-validation.md").read_text(encoding="utf-8")


def test_research_validation_docs_describe_semantic_holdout_identity_not_fingerprint_only() -> None:
    doc = _research_validation_doc()

    assert "`final_holdout_identity_hash` is the semantic reuse-counting key based on\n  dataset source, market, interval" in doc
    assert "`final_holdout_reuse_key_hash` is the key used to compute\n  `computed_holdout_reuse_count`" in doc
    assert "Reuse counting uses the semantic identity hash, not byte-identical split content." in doc
    assert "`final_holdout_fingerprint` is retained as a compatibility alias for the\n  semantic identity hash." in doc


def test_research_validation_docs_describe_pre_content_reservation_and_completion_binding() -> None:
    doc = _research_validation_doc()

    assert "checked reservation happens before the\n`final_holdout` split is loaded" in doc
    assert "The reservation row uses only the semantic\nfinal-holdout identity needed for reuse counting." in doc
    assert "final_holdout_content_pending_until_completion=true" in doc
    assert "pre-content reservation that must have matching\ncompletion/artifact content before promotion" in doc


def test_research_validation_docs_describe_bound_evidence_hash_and_final_content_hash_difference() -> None:
    doc = _research_validation_doc()

    assert "The pre-completion evidence `content_hash` is recorded in the\n   `research_attempt_completed` row" in doc
    assert "experiment_registry_bound_evidence_hash" in doc
    assert "experiment_registry_evidence_hash_phase=pre_completion_evidence_hash" in doc
    assert "final `content_hash` is recomputed" in doc


def test_research_validation_docs_describe_lifecycle_status_separate_from_statistical_gate() -> None:
    doc = _research_validation_doc()

    assert "Registry lifecycle status is separate from statistical gate result:" in doc
    assert "`result_status=IN_PROGRESS` for a counted reservation." in doc
    assert "`result_status=COMPLETED` for a completed lifecycle event." in doc
    assert "`result_status=ABORTED` for an interrupted counted attempt." in doc
    assert "`result_status=REJECTED` for an uncounted preflight rejection." in doc
    assert "Only `COMPLETED` is promotion-permitted at the lifecycle layer." in doc
    assert "`statistical_gate_result=PASS|FAIL|UNKNOWN` remains separate evidence" in doc


def test_research_validation_docs_describe_registry_validate_artifact_bound_row_and_lifecycle_summary() -> None:
    doc = _research_validation_doc()

    assert "validation_scope=registry_only" in doc
    assert "validation_scope=registry_and_artifacts" in doc
    assert "The command determines one\n`artifact_bound_row_hash`" in doc
    assert "validates artifact binding for\nthat row exactly once" in doc
    assert "`artifact_binding_valid` plus\n`artifact_reasons`" in doc
    assert "All reservation rows for the experiment remain visible\nin `registry_lifecycle_summary`" in doc
    assert "`row_valid_only=true` means the reservation row is hash-valid but the\n  lifecycle is not promotion-permitted." in doc


def test_research_validation_docs_list_current_registry_refusal_reasons() -> None:
    doc = _research_validation_doc()

    for reason in (
        "experiment_registry_bound_evidence_hash_missing",
        "experiment_registry_evidence_hash_phase_mismatch",
        "experiment_registry_statistical_evidence_hash_mismatch",
        "experiment_registry_identity_source_missing",
        "experiment_registry_final_holdout_identity_mismatch",
        "experiment_registry_final_holdout_content_mismatch",
        "experiment_registry_final_holdout_reuse_key_mismatch",
        "experiment_registry_artifact_bound_row_missing",
        "experiment_registry_artifact_bound_row_hash_mismatch",
        "experiment_registry_report_evidence_row_hash_mismatch",
        "artifact_binding_not_checked",
        "attempt_budget_exceeded",
        "holdout_reuse_budget_exceeded",
    ):
        assert reason in doc
