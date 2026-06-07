from __future__ import annotations

import json
import hashlib
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .promotion_evidence_verifier import verify_promotion_candidate_execution_evidence


PROMOTION_ARTIFACT_GRADE = "promotion_candidate"
DIAGNOSTIC_ARTIFACT_GRADE = "diagnostic_only"
PROMOTION_AUTHORITY_PLANE = "typed_execution_plan_bundle"
PROMOTION_EXECUTION_EVIDENCE_SOURCE = "typed_execution_plan_bundle"
PROMOTION_NEXT_ACTION = "regenerate_with_typed_execution_authority"
NO_SUBMIT_PROOF_AUTHORITY_LABEL = "ExecutionSubmitPlan.no_submit_proof.v1"

LEGACY_DECISION_AUTHORITY_SOURCES = frozenset(
    {
        "legacy_context",
        "context",
        "decision_context",
        "diagnostic_context",
        "compatibility_context",
    }
)


def sha256_prefixed(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PromotionArtifactProvenance:
    authority_plane: str
    decision_authority_source: str
    execution_evidence_source: str
    execution_plan_bundle_present: bool
    execution_plan_bundle_hash: str
    typed_execution_summary_present: bool
    execution_summary_hash: str
    execution_submit_plan_hash: str
    runtime_decision_request_hash: str
    runtime_strategy_set_manifest_hash: str
    approved_profile_hash: str
    compatibility_fallback: bool
    legacy_context_planning_used: bool
    runtime_replay_planning_error: str | None
    artifact_grade: str
    promotion_rejection_reason: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "PromotionArtifactProvenance":
        return cls(
            authority_plane=str(payload.get("authority_plane") or ""),
            decision_authority_source=str(payload.get("decision_authority_source") or ""),
            execution_evidence_source=str(payload.get("execution_evidence_source") or ""),
            execution_plan_bundle_present=payload.get("execution_plan_bundle_present") is True,
            execution_plan_bundle_hash=str(payload.get("execution_plan_bundle_hash") or ""),
            typed_execution_summary_present=payload.get("typed_execution_summary_present") is True,
            execution_summary_hash=str(payload.get("execution_summary_hash") or ""),
            execution_submit_plan_hash=str(payload.get("execution_submit_plan_hash") or ""),
            runtime_decision_request_hash=str(payload.get("runtime_decision_request_hash") or ""),
            runtime_strategy_set_manifest_hash=str(payload.get("runtime_strategy_set_manifest_hash") or ""),
            approved_profile_hash=str(payload.get("approved_profile_hash") or ""),
            compatibility_fallback=payload.get("compatibility_fallback") is True,
            legacy_context_planning_used=payload.get("legacy_context_planning_used") is True,
            runtime_replay_planning_error=(
                str(payload.get("runtime_replay_planning_error"))
                if str(payload.get("runtime_replay_planning_error") or "").strip()
                else None
            ),
            artifact_grade=str(payload.get("artifact_grade") or ""),
            promotion_rejection_reason=str(payload.get("promotion_rejection_reason") or ""),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PromotionProvenanceValidation:
    ok: bool
    reason_codes: tuple[str, ...]
    recommended_next_action: str
    provenance: PromotionArtifactProvenance

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "ok": self.ok,
            "reason_codes": list(self.reason_codes),
            "recommended_next_action": self.recommended_next_action,
        }
        payload.update(self.provenance.as_dict())
        return payload


@dataclass(frozen=True)
class PromotionArtifact:
    payload: dict[str, Any]

    @classmethod
    def create_from_typed_bundle(
        cls,
        *,
        canonical_decision_v2: dict[str, Any],
        runtime_decision_request_hash: str,
        runtime_strategy_set_manifest_hash: str,
        approved_profile_hash: str,
        execution_plan_bundle: Any,
        execution_summary: Any,
        execution_submit_plan: Any | None = None,
        typed_no_submit_proof: dict[str, Any] | None = None,
    ) -> "PromotionArtifact":
        if int(canonical_decision_v2.get("decision_contract_version") or 0) < 2:
            raise ValueError("promotion_artifact_requires_canonical_v2")
        bundle_payload = _as_dict_payload(execution_plan_bundle, "execution_plan_bundle")
        summary_payload = _as_dict_payload(execution_summary, "execution_summary")
        submit_payload = (
            None
            if execution_submit_plan is None
            else _as_submit_plan_payload(execution_submit_plan)
        )
        if submit_payload is None:
            proof_payload = (
                dict(typed_no_submit_proof)
                if typed_no_submit_proof is not None
                else build_typed_no_submit_proof(summary_payload)
            )
            submit_hash = sha256_prefixed(proof_payload)
            submit_evidence_key = "typed_no_submit_proof"
            submit_evidence = proof_payload
        else:
            submit_hash = sha256_prefixed(submit_payload)
            submit_evidence_key = "execution_submit_plan_evidence"
            submit_evidence = submit_payload
        payload = dict(canonical_decision_v2)
        payload.update(
            {
                "decision_contract_version": 2,
                "runtime_decision_request_hash": runtime_decision_request_hash,
                "runtime_strategy_set_manifest_hash": runtime_strategy_set_manifest_hash,
                "approved_profile_hash": approved_profile_hash,
                "authority_plane": PROMOTION_AUTHORITY_PLANE,
                "decision_authority_source": str(
                    payload.get("decision_authority_source")
                    or "DecisionEnvelope.strategy_decision"
                ),
                "execution_evidence_source": PROMOTION_EXECUTION_EVIDENCE_SOURCE,
                "execution_plan_bundle_present": True,
                "execution_plan_bundle_hash": sha256_prefixed(bundle_payload),
                "execution_plan_bundle_evidence": bundle_payload,
                "typed_execution_summary_present": True,
                "typed_submit_plan": submit_payload is not None,
                "execution_summary_hash": sha256_prefixed(summary_payload),
                "typed_execution_summary_evidence": summary_payload,
                "execution_submit_plan_hash": submit_hash,
                submit_evidence_key: submit_evidence,
                "compatibility_fallback": False,
                "research_compatibility_execution_fallback": False,
                "legacy_context_planning_used": False,
                "runtime_replay_planning_error": "",
                "artifact_grade": PROMOTION_ARTIFACT_GRADE,
                "promotion_grade": submit_payload is not None,
                "promotion_rejection_reason": "",
            }
        )
        validation = validate_promotion_artifact(payload)
        if not validation.ok:
            raise ValueError(
                "promotion_artifact_invalid:" + ",".join(validation.reason_codes)
            )
        return cls(payload=payload)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def validate_promotion_artifact_provenance(
    payload: dict[str, Any],
) -> PromotionProvenanceValidation:
    provenance = PromotionArtifactProvenance.from_payload(payload)
    failures = promotion_provenance_failure_codes(provenance)
    failures.extend(_typed_evidence_failure_codes(payload, provenance))
    failures.extend(
        "canonical_" + reason
        for reason in verify_promotion_candidate_execution_evidence(payload).reason_codes
    )
    return PromotionProvenanceValidation(
        ok=not failures,
        reason_codes=tuple(sorted(set(failures))),
        recommended_next_action="none" if not failures else PROMOTION_NEXT_ACTION,
        provenance=provenance,
    )


def validate_promotion_artifact(payload: dict[str, Any]) -> PromotionProvenanceValidation:
    """Hard gate for promotion artifacts: canonical v2 plus verified typed evidence."""
    failures: list[str] = []
    try:
        version = int(payload.get("decision_contract_version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version < 2:
        failures.append("canonical_promotion_legacy_contract_version")
    validation = validate_promotion_artifact_provenance(payload)
    failures.extend(validation.reason_codes)
    failures.extend(_canonical_decision_failure_codes(payload))
    failures = sorted(set(failures))
    return PromotionProvenanceValidation(
        ok=not failures,
        reason_codes=tuple(failures),
        recommended_next_action="none" if not failures else PROMOTION_NEXT_ACTION,
        provenance=validation.provenance,
    )


def promotion_provenance_failure_codes(
    provenance: PromotionArtifactProvenance,
) -> list[str]:
    failures: list[str] = []
    if provenance.compatibility_fallback:
        failures.append("canonical_promotion_compatibility_fallback")
    if provenance.legacy_context_planning_used:
        failures.append("canonical_promotion_legacy_context_planning")
    if not provenance.execution_plan_bundle_present:
        failures.append("canonical_promotion_execution_plan_bundle_missing")
    if not _valid_sha256_hash(provenance.execution_plan_bundle_hash):
        failures.append("canonical_promotion_execution_plan_bundle_hash_missing")
    if not provenance.typed_execution_summary_present:
        failures.append("canonical_promotion_typed_execution_summary_missing")
    if not _valid_sha256_hash(provenance.execution_summary_hash):
        failures.append("canonical_promotion_execution_summary_hash_missing")
    if not _valid_sha256_hash(provenance.execution_submit_plan_hash):
        failures.append("canonical_promotion_execution_submit_plan_hash_missing")
    if not _valid_sha256_hash(provenance.runtime_decision_request_hash):
        failures.append("canonical_promotion_runtime_decision_request_hash_missing")
    if not _valid_sha256_hash(provenance.runtime_strategy_set_manifest_hash):
        failures.append("canonical_promotion_runtime_strategy_set_manifest_hash_missing")
    if not _valid_sha256_hash(provenance.approved_profile_hash):
        failures.append("canonical_promotion_approved_profile_hash_missing")
    if provenance.decision_authority_source.strip() in LEGACY_DECISION_AUTHORITY_SOURCES:
        failures.append("canonical_promotion_legacy_context_authority")
    if provenance.runtime_replay_planning_error:
        failures.append("canonical_promotion_runtime_replay_planning_error")
    if provenance.execution_evidence_source != PROMOTION_EXECUTION_EVIDENCE_SOURCE:
        failures.append("canonical_promotion_typed_execution_provenance_missing")
    if provenance.authority_plane != PROMOTION_AUTHORITY_PLANE:
        failures.append("canonical_promotion_typed_authority_plane_missing")
    if provenance.artifact_grade != PROMOTION_ARTIFACT_GRADE:
        failures.append("canonical_promotion_artifact_grade_not_promotion")
    if provenance.promotion_rejection_reason.strip():
        failures.append("canonical_promotion_rejection_reason_present")
    return sorted(set(failures))


def _canonical_decision_failure_codes(payload: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if str(payload.get("policy_materialization_mode") or "") == "research_exploratory":
        failures.append("canonical_promotion_research_exploratory_materialization")
    if payload.get("runtime_comparable") is False:
        failures.append("canonical_promotion_runtime_comparable_false")
    if payload.get("allow_execution_compatibility_fallback") is True:
        failures.append("canonical_promotion_execution_compatibility_fallback")
    for field_name in (
        "policy_input_hash",
        "policy_decision_hash",
        "policy_contract_hash",
        "decision_input_bundle_hash",
        "decision_input_contract_hash",
        "decision_input_bundle_payload_hash",
        "market_snapshot_hash",
        "final_exit_decision_input_hash",
        "position_snapshot_hash",
        "execution_constraints_hash",
        "policy_config_hash",
        "exit_policy_config_hash",
        "fee_authority_hash",
        "order_rules_hash",
        "snapshot_projector_hash",
        "replay_fingerprint_hash",
    ):
        if not _valid_sha256_hash(str(payload.get(field_name) or "")):
            failures.append(f"canonical_promotion_{field_name}_missing")
    if not (
        _valid_sha256_hash(str(payload.get("market_feature_hash") or ""))
        or _valid_sha256_hash(str(payload.get("canonical_feature_projection_hash") or ""))
    ):
        failures.append("canonical_promotion_market_feature_hash_missing")
    if not str(payload.get("snapshot_projector_version") or "").strip():
        failures.append("canonical_promotion_snapshot_projector_version_missing")
    provenance = payload.get("strategy_evaluation_provenance")
    if not isinstance(provenance, dict):
        failures.append("canonical_promotion_strategy_evaluation_provenance_missing")
    elif provenance.get("decision_boundary") != "StrategyDecisionService.evaluate":
        failures.append("canonical_promotion_strategy_evaluation_boundary_invalid")
    return failures


def build_typed_no_submit_proof(summary_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "authority_label": NO_SUBMIT_PROOF_AUTHORITY_LABEL,
        "execution_summary_hash": sha256_prefixed(summary_payload),
        "submit_expected": bool(summary_payload.get("submit_expected")),
        "final_action": str(summary_payload.get("final_action") or ""),
        "block_reason": str(summary_payload.get("block_reason") or ""),
    }


def _typed_evidence_failure_codes(
    payload: dict[str, Any],
    provenance: PromotionArtifactProvenance,
) -> list[str]:
    failures: list[str] = []
    bundle_evidence = payload.get("execution_plan_bundle_evidence")
    summary_evidence = payload.get("typed_execution_summary_evidence")
    submit_evidence = payload.get("execution_submit_plan_evidence")
    no_submit_proof = payload.get("typed_no_submit_proof")
    if not isinstance(bundle_evidence, dict) or not isinstance(summary_evidence, dict):
        failures.append("canonical_promotion_forged_or_unverified_typed_evidence")
        return failures
    if bundle_evidence.get("compatibility_fallback") is True:
        failures.append("canonical_promotion_compatibility_fallback")
    if bundle_evidence.get("promotion_grade") is False:
        failures.append("canonical_promotion_bundle_not_promotion_grade")
    if str(bundle_evidence.get("artifact_grade") or PROMOTION_ARTIFACT_GRADE) != PROMOTION_ARTIFACT_GRADE:
        failures.append("canonical_promotion_artifact_grade_not_promotion")
    if str(bundle_evidence.get("authority_plane") or PROMOTION_AUTHORITY_PLANE) != PROMOTION_AUTHORITY_PLANE:
        failures.append("canonical_promotion_typed_authority_plane_missing")
    if (
        str(bundle_evidence.get("execution_evidence_source") or PROMOTION_EXECUTION_EVIDENCE_SOURCE)
        != PROMOTION_EXECUTION_EVIDENCE_SOURCE
    ):
        failures.append("canonical_promotion_typed_execution_provenance_missing")
    if bundle_evidence.get("live_authoritative") is True:
        failures.append("canonical_promotion_research_bundle_claims_live_authority")
    if sha256_prefixed(bundle_evidence) != provenance.execution_plan_bundle_hash:
        failures.append("canonical_promotion_forged_or_unverified_typed_evidence")
    if sha256_prefixed(summary_evidence) != provenance.execution_summary_hash:
        failures.append("canonical_promotion_forged_or_unverified_typed_evidence")
    if isinstance(submit_evidence, dict):
        if str(submit_evidence.get("authority_label") or "") != "ExecutionSubmitPlan.final_payload.v1":
            failures.append("canonical_promotion_dict_only_submit_evidence_not_authority")
        try:
            submit_schema_version = int(submit_evidence.get("schema_version") or 0)
        except (TypeError, ValueError):
            submit_schema_version = 0
        if submit_schema_version != 1:
            failures.append("canonical_promotion_dict_only_submit_evidence_not_authority")
        if submit_evidence.get("compatibility_fallback") is True:
            failures.append("canonical_promotion_compatibility_fallback")
        if submit_evidence.get("promotion_grade") is False:
            failures.append("canonical_promotion_submit_plan_not_promotion_grade")
        if str(submit_evidence.get("artifact_grade") or PROMOTION_ARTIFACT_GRADE) != PROMOTION_ARTIFACT_GRADE:
            failures.append("canonical_promotion_artifact_grade_not_promotion")
        if sha256_prefixed(submit_evidence) != provenance.execution_submit_plan_hash:
            failures.append("canonical_promotion_forged_or_unverified_typed_evidence")
    elif isinstance(no_submit_proof, dict):
        if (
            str(no_submit_proof.get("authority_label") or "")
            != NO_SUBMIT_PROOF_AUTHORITY_LABEL
            or sha256_prefixed(no_submit_proof) != provenance.execution_submit_plan_hash
        ):
            failures.append("canonical_promotion_forged_or_unverified_typed_evidence")
    else:
        failures.append("canonical_promotion_forged_or_unverified_typed_evidence")
    return failures


def payload_has_promotion_provenance_markers(payload: dict[str, Any]) -> bool:
    return any(
        key in payload
        for key in (
            "authority_plane",
            "decision_authority_source",
            "execution_evidence_source",
            "execution_plan_bundle_present",
            "execution_plan_bundle_hash",
            "typed_execution_summary_present",
            "execution_summary_hash",
            "execution_submit_plan_hash",
            "runtime_decision_request_hash",
            "runtime_strategy_set_manifest_hash",
            "approved_profile_hash",
            "legacy_context_planning_used",
            "runtime_replay_planning_error",
            "artifact_grade",
            "promotion_rejection_reason",
        )
    )


def verify_promotion_provenance_artifact_file(path: str | Path) -> dict[str, Any]:
    resolved = Path(path).resolve()
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        validation = validate_promotion_artifact({}).as_dict()
        validation.update(
            {
                "ok": False,
                "reason_codes": sorted(
                    set(list(validation["reason_codes"]) + ["promotion_artifact_unreadable"])
                ),
                "recommended_next_action": PROMOTION_NEXT_ACTION,
                "artifact_path": str(resolved),
                "load_error": str(exc),
            }
        )
        return validation
    if not isinstance(payload, dict):
        validation = validate_promotion_artifact({}).as_dict()
        validation["ok"] = False
        validation["reason_codes"] = sorted(
            set(list(validation["reason_codes"]) + ["promotion_artifact_schema_not_object"])
        )
        validation["recommended_next_action"] = PROMOTION_NEXT_ACTION
        validation["artifact_path"] = str(resolved)
        return validation
    validation = validate_promotion_artifact(payload).as_dict()
    validation["artifact_path"] = str(resolved)
    return validation


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _valid_sha256_hash(value: str) -> bool:
    return bool(_SHA256_RE.fullmatch(str(value or "").strip()))


def _as_dict_payload(value: Any, field_name: str) -> dict[str, Any]:
    as_dict = getattr(value, "as_dict", None)
    if not callable(as_dict):
        raise TypeError(f"{field_name}_missing_as_dict")
    payload = as_dict()
    if not isinstance(payload, dict):
        raise TypeError(f"{field_name}_as_dict_not_object")
    return dict(payload)


def _as_submit_plan_payload(value: Any) -> dict[str, Any]:
    final_payload = getattr(value, "as_final_payload", None)
    if callable(final_payload):
        payload = final_payload()
    else:
        payload = _as_dict_payload(value, "execution_submit_plan")
    if not isinstance(payload, dict):
        raise TypeError("execution_submit_plan_payload_not_object")
    return dict(payload)
