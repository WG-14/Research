from __future__ import annotations

import json

from bithumb_bot.profile_cli import cmd_promotion_provenance_verify
from bithumb_bot.promotion_provenance import (
    PromotionArtifact,
    validate_promotion_artifact,
    validate_promotion_artifact_provenance,
)


class _TypedEvidence:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def as_dict(self) -> dict[str, object]:
        return dict(self.payload)


class _TypedSubmitEvidence(_TypedEvidence):
    def as_final_payload(self) -> dict[str, object]:
        return dict(self.payload)

HASH = "sha256:" + "a" * 64


def _typed_payload(**overrides: object) -> dict[str, object]:
    payload = PromotionArtifact.create_from_typed_bundle(
        canonical_decision_v2={
            "decision_contract_version": 2,
            "decision_authority_source": "DecisionEnvelope.strategy_decision",
        },
        runtime_decision_request_hash=HASH,
        runtime_strategy_set_manifest_hash=HASH,
        approved_profile_hash=HASH,
        execution_plan_bundle=_TypedEvidence({"bundle": "typed"}),
        execution_summary=_TypedEvidence(
            {
                "final_action": "STRATEGY_HOLD",
                "submit_expected": False,
                "block_reason": "raw_hold_no_entry_or_exit_signal",
            }
        ),
        execution_submit_plan=_TypedSubmitEvidence(
            {
                "side": "HOLD",
                "source": "target_delta",
                "authority": "canonical_target_delta_sizing",
                "final_action": "STRATEGY_HOLD",
                "qty": None,
                "notional_krw": None,
                "target_exposure_krw": None,
                "current_effective_exposure_krw": None,
                "delta_krw": None,
                "submit_expected": False,
                "pre_submit_proof_status": "not_required",
                "block_reason": "raw_hold_no_entry_or_exit_signal",
                "idempotency_key": None,
                "schema_version": 1,
                "authority_label": "ExecutionSubmitPlan.final_payload.v1",
                "content_hash": HASH,
            }
        ),
    ).as_dict()
    payload.update(overrides)
    return payload


def test_promotion_provenance_contract_accepts_typed_authority_only() -> None:
    result = validate_promotion_artifact_provenance(_typed_payload())

    assert result.ok is True
    assert result.reason_codes == ()
    assert result.recommended_next_action == "none"


def test_promotion_rejects_legacy_context_planning() -> None:
    result = validate_promotion_artifact_provenance(
        _typed_payload(
            legacy_context_planning_used=True,
            compatibility_fallback=True,
            authority_plane="compatibility_context",
            execution_evidence_source="diagnostic_context_fallback",
            artifact_grade="diagnostic_only",
            promotion_rejection_reason="legacy_context_planning_diagnostic_only",
        )
    )

    assert result.ok is False
    assert "canonical_promotion_legacy_context_planning" in result.reason_codes
    assert "canonical_promotion_compatibility_fallback" in result.reason_codes
    assert "canonical_promotion_typed_execution_provenance_missing" in result.reason_codes
    assert result.recommended_next_action == "regenerate_with_typed_execution_authority"


def test_promotion_provenance_verify_cli_reports_structured_failure(tmp_path, capsys) -> None:
    artifact = tmp_path / "canonical.json"
    artifact.write_text(
        json.dumps(
            _typed_payload(
                execution_plan_bundle_hash="",
                runtime_replay_planning_error="runtime_replay_execution_readiness_unavailable",
            )
        ),
        encoding="utf-8",
    )

    rc = cmd_promotion_provenance_verify(artifact_path=str(artifact))

    captured = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert captured["ok"] is False
    assert captured["authority_plane"] == "typed_execution_plan_bundle"
    assert captured["execution_evidence_source"] == "typed_execution_plan_bundle"
    assert captured["execution_plan_bundle_hash"] == ""
    assert captured["runtime_decision_request_hash"] == HASH
    assert captured["runtime_strategy_set_manifest_hash"] == HASH
    assert captured["approved_profile_hash"] == HASH
    assert captured["typed_execution_summary_present"] is True
    assert captured["compatibility_fallback"] is False
    assert captured["legacy_context_planning_used"] is False
    assert captured["artifact_grade"] == "promotion_candidate"
    assert captured["promotion_rejection_reason"] == ""
    assert "canonical_promotion_execution_plan_bundle_hash_missing" in captured["reason_codes"]
    assert "canonical_promotion_runtime_replay_planning_error" in captured["reason_codes"]
    assert captured["recommended_next_action"] == "regenerate_with_typed_execution_authority"


def test_promotion_provenance_verify_cli_reports_structured_success(tmp_path, capsys) -> None:
    artifact = tmp_path / "canonical.json"
    artifact.write_text(json.dumps(_typed_payload()), encoding="utf-8")

    rc = cmd_promotion_provenance_verify(artifact_path=str(artifact))

    captured = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert captured["ok"] is True
    assert captured["reason_codes"] == []
    assert captured["artifact_path"] == str(artifact.resolve())
    assert captured["artifact_grade"] == "promotion_candidate"
    assert captured["authority_plane"] == "typed_execution_plan_bundle"
    assert captured["execution_plan_bundle_present"] is True
    assert captured["typed_execution_summary_present"] is True
    assert captured["runtime_decision_request_hash"] == HASH
    assert captured["runtime_strategy_set_manifest_hash"] == HASH
    assert captured["approved_profile_hash"] == HASH


def test_canonical_promotion_rejects_v1_contract() -> None:
    result = validate_promotion_artifact(_typed_payload(decision_contract_version=1))

    assert result.ok is False
    assert "canonical_promotion_legacy_contract_version" in result.reason_codes


def test_canonical_promotion_rejects_compatibility_fallback_context() -> None:
    result = validate_promotion_artifact(
        _typed_payload(
            compatibility_fallback=True,
            execution_evidence_source="diagnostic_context_fallback",
            authority_plane="compatibility_context",
        )
    )

    assert result.ok is False
    assert "canonical_promotion_compatibility_fallback" in result.reason_codes
    assert "canonical_promotion_typed_execution_provenance_missing" in result.reason_codes
    assert "canonical_promotion_typed_authority_plane_missing" in result.reason_codes


def test_promotion_provenance_rejects_malformed_execution_hashes() -> None:
    result = validate_promotion_artifact(
        _typed_payload(
            execution_plan_bundle_hash="sha256:bundle",
            execution_summary_hash="sha256:summary",
            execution_submit_plan_hash="sha256:plan",
            runtime_decision_request_hash="sha256:anything",
        )
    )

    assert result.ok is False
    assert "canonical_promotion_execution_plan_bundle_hash_missing" in result.reason_codes
    assert "canonical_promotion_execution_summary_hash_missing" in result.reason_codes
    assert "canonical_promotion_execution_submit_plan_hash_missing" in result.reason_codes
    assert "canonical_promotion_runtime_decision_request_hash_missing" in result.reason_codes


def test_promotion_provenance_rejects_forged_markers_without_typed_evidence() -> None:
    payload = _typed_payload()
    payload.pop("execution_plan_bundle_evidence")
    payload.pop("typed_execution_summary_evidence")
    payload.pop("execution_submit_plan_evidence")

    result = validate_promotion_artifact(payload)

    assert result.ok is False
    assert "canonical_promotion_forged_or_unverified_typed_evidence" in result.reason_codes


def test_promotion_provenance_rejects_missing_binding_hashes_independently() -> None:
    cases = {
        "runtime_decision_request_hash": "canonical_promotion_runtime_decision_request_hash_missing",
        "runtime_strategy_set_manifest_hash": "canonical_promotion_runtime_strategy_set_manifest_hash_missing",
        "approved_profile_hash": "canonical_promotion_approved_profile_hash_missing",
    }

    for field, reason in cases.items():
        payload = _typed_payload(**{field: ""})
        result = validate_promotion_artifact_provenance(payload)
        assert result.ok is False
        assert reason in result.reason_codes
