from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping


TARGET_DELTA_SUBMIT_SOURCE = "target_delta"
H74_SOURCE_OBSERVATION_SUBMIT_SOURCE = "h74_source_observation"
H74_SOURCE_OBSERVATION_SUBMIT_AUTHORITY = "h74_fixed_fill_quote_notional_buy"
TARGET_DELTA_SUBMIT_AUTHORITIES = frozenset(
    {
        "canonical_target_delta_sizing",
        "target_position_delta",
        H74_SOURCE_OBSERVATION_SUBMIT_AUTHORITY,
    }
)
RESIDUAL_SUBMIT_SOURCE = "residual_inventory"
RESIDUAL_SUBMIT_AUTHORITIES = frozenset({"residual_inventory_policy"})
LEGACY_BUY_SUBMIT_SOURCES = frozenset({"strategy_position"})
LEGACY_BUY_SUBMIT_AUTHORITIES = frozenset(
    {
        "configured_strategy_order_size",
        "residual_inventory_delta",
        "strategy_execution_intent",
        "research_compatibility_execution_intent",
    }
)


@dataclass(frozen=True)
class SubmitAuthorityPolicy:
    submit_authority_mode: str
    live_real_order_requires_target_delta: bool
    legacy_lot_native_compat_enabled: bool
    allowed_submit_plan_sources: tuple[str, ...]
    allowed_submit_plan_authorities: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "submit_authority_mode": self.submit_authority_mode,
            "live_real_order_requires_target_delta": bool(
                self.live_real_order_requires_target_delta
            ),
            "legacy_lot_native_compat_enabled": bool(self.legacy_lot_native_compat_enabled),
            "allowed_submit_plan_sources": list(self.allowed_submit_plan_sources),
            "allowed_submit_plan_authorities": list(self.allowed_submit_plan_authorities),
        }

    def content_hash(self) -> str:
        return submit_authority_policy_hash(self.as_dict())


@dataclass(frozen=True)
class SubmitAuthorityPolicyDecision:
    allowed: bool
    reason: str
    policy: SubmitAuthorityPolicy
    plan_kind: str
    mode: str
    live_dry_run: bool
    live_real_order_armed: bool
    execution_engine: str
    source: str
    authority: str
    side: str
    submit_expected: bool
    pre_submit_proof_status: str
    pre_submit_risk_approval_status: str = "not_required"
    pre_submit_risk_block_reason: str = "none"
    entry_authority_status: str = "not_required"
    entry_authority_reason_code: str = "none"
    position_management_authority_status: str = "not_required"
    closeout_authority_status: str = "not_required"

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": bool(self.allowed),
            "reason": self.reason,
            "plan_kind": self.plan_kind,
            "mode": self.mode,
            "live_dry_run": bool(self.live_dry_run),
            "live_real_order_armed": bool(self.live_real_order_armed),
            "execution_engine": self.execution_engine,
            "source": self.source,
            "authority": self.authority,
            "side": self.side,
            "submit_expected": bool(self.submit_expected),
            "pre_submit_proof_status": self.pre_submit_proof_status,
            "pre_submit_risk_approval_status": self.pre_submit_risk_approval_status,
            "pre_submit_risk_block_reason": self.pre_submit_risk_block_reason,
            "entry_authority_status": self.entry_authority_status,
            "entry_authority_reason_code": self.entry_authority_reason_code,
            "position_management_authority_status": self.position_management_authority_status,
            "closeout_authority_status": self.closeout_authority_status,
            "submit_authority_mode": self.policy.submit_authority_mode,
            "submit_authority_policy_hash": self.policy.content_hash(),
        }


@dataclass(frozen=True)
class PreSubmitRiskApproval:
    approved: bool
    reason: str
    integrity_valid: bool
    action_authorized: bool
    status: str


def submit_authority_policy_hash(policy_payload: Mapping[str, object]) -> str:
    encoded = json.dumps(policy_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def live_real_order_enabled(settings_obj: object) -> bool:
    return (
        str(getattr(settings_obj, "MODE", "") or "").strip().lower() == "live"
        and not bool(getattr(settings_obj, "LIVE_DRY_RUN", True))
        and bool(getattr(settings_obj, "LIVE_REAL_ORDER_ARMED", False))
    )


def h74_source_observation_submit_enabled(settings_obj: object) -> bool:
    return bool(
        live_real_order_enabled(settings_obj)
        and str(getattr(settings_obj, "H74_SOURCE_OBSERVATION_AUTHORITY_PATH", "") or "").strip()
    )


def submit_authority_policy_from_settings(settings_obj: object) -> SubmitAuthorityPolicy:
    if live_real_order_enabled(settings_obj):
        h74_enabled = h74_source_observation_submit_enabled(settings_obj)
        target_sources = [TARGET_DELTA_SUBMIT_SOURCE]
        target_authorities = set(TARGET_DELTA_SUBMIT_AUTHORITIES)
        if h74_enabled:
            target_sources.append(H74_SOURCE_OBSERVATION_SUBMIT_SOURCE)
        else:
            target_authorities.discard(H74_SOURCE_OBSERVATION_SUBMIT_AUTHORITY)
        return SubmitAuthorityPolicy(
            submit_authority_mode="live_real_order_target_delta_only",
            live_real_order_requires_target_delta=True,
            legacy_lot_native_compat_enabled=False,
            allowed_submit_plan_sources=tuple(target_sources + [RESIDUAL_SUBMIT_SOURCE]),
            allowed_submit_plan_authorities=tuple(
                sorted(target_authorities | RESIDUAL_SUBMIT_AUTHORITIES)
            ),
        )
    if str(getattr(settings_obj, "MODE", "") or "").strip().lower() == "live":
        return SubmitAuthorityPolicy(
            submit_authority_mode="live_dry_run_non_submitting_compat",
            live_real_order_requires_target_delta=False,
            legacy_lot_native_compat_enabled=True,
            allowed_submit_plan_sources=tuple(
                sorted(
                    {
                        TARGET_DELTA_SUBMIT_SOURCE,
                        H74_SOURCE_OBSERVATION_SUBMIT_SOURCE,
                        RESIDUAL_SUBMIT_SOURCE,
                    }
                    | LEGACY_BUY_SUBMIT_SOURCES
                )
            ),
            allowed_submit_plan_authorities=tuple(
                sorted(
                    TARGET_DELTA_SUBMIT_AUTHORITIES
                    | RESIDUAL_SUBMIT_AUTHORITIES
                    | LEGACY_BUY_SUBMIT_AUTHORITIES
                )
            ),
        )
    return SubmitAuthorityPolicy(
        submit_authority_mode="paper_research_compat",
        live_real_order_requires_target_delta=False,
        legacy_lot_native_compat_enabled=True,
        allowed_submit_plan_sources=tuple(
            sorted(
                {
                    TARGET_DELTA_SUBMIT_SOURCE,
                    H74_SOURCE_OBSERVATION_SUBMIT_SOURCE,
                    RESIDUAL_SUBMIT_SOURCE,
                    "research_backtest",
                }
                | LEGACY_BUY_SUBMIT_SOURCES
            )
        ),
        allowed_submit_plan_authorities=tuple(
            sorted(
                TARGET_DELTA_SUBMIT_AUTHORITIES
                | RESIDUAL_SUBMIT_AUTHORITIES
                | LEGACY_BUY_SUBMIT_AUTHORITIES
                | {"target_position_delta"}
            )
        ),
    )


def evaluate_submit_authority_policy(
    plan: object,
    *,
    settings_obj: object,
    plan_kind: str,
    require_final_payload: bool = False,
) -> SubmitAuthorityPolicyDecision:
    policy = submit_authority_policy_from_settings(settings_obj)
    payload = plan.as_dict() if hasattr(plan, "as_dict") else dict(plan or {})
    mode = str(getattr(settings_obj, "MODE", "") or "").strip().lower()
    live_dry_run = bool(getattr(settings_obj, "LIVE_DRY_RUN", True))
    live_real_order_armed = bool(getattr(settings_obj, "LIVE_REAL_ORDER_ARMED", False))
    execution_engine = str(getattr(settings_obj, "EXECUTION_ENGINE", "") or "").strip().lower()
    source = str(payload.get("source") or "").strip()
    authority = str(payload.get("authority") or "").strip()
    side = str(payload.get("side") or "").strip().upper()
    submit_expected = bool(payload.get("submit_expected"))
    proof = str(payload.get("pre_submit_proof_status") or "").strip()
    normalized_kind = str(plan_kind or "").strip().lower()
    final_payload_required = bool(require_final_payload or hasattr(plan, "as_final_payload"))

    def final_payload_error() -> str | None:
        try:
            schema_version = int(payload.get("schema_version") or 0)
        except (TypeError, ValueError):
            schema_version = 0
        if schema_version != 1:
            return "live_real_order_submit_plan_missing_final_schema"
        if str(payload.get("authority_label") or "") != "ExecutionSubmitPlan.final_payload.v1":
            return "live_real_order_submit_plan_missing_final_authority_label"
        if not _valid_sha256_prefixed(payload.get("content_hash")):
            return "live_real_order_submit_plan_missing_final_content_hash"
        if not _valid_sha256_prefixed(payload.get("submit_plan_hash")):
            return "live_real_order_submit_plan_missing_submit_plan_hash"
        return None

    def decision(allowed: bool, reason: str) -> SubmitAuthorityPolicyDecision:
        risk_error = None
        risk_status = "not_required"
        if (
            allowed
            and policy.live_real_order_requires_target_delta
            and submit_expected
            and (source == TARGET_DELTA_SUBMIT_SOURCE or bool(payload.get("pre_submit_risk_required")))
        ):
            expected_hash = str(payload.get("submit_plan_hash") or "").strip()
            risk_error = operational_pre_submit_risk_approval_error(
                payload,
                expected_submit_plan_hash=expected_hash,
            )
            risk_status = "approved" if risk_error is None else "blocked"
        return SubmitAuthorityPolicyDecision(
            allowed=allowed if risk_error is None else False,
            reason=reason if risk_error is None else risk_error,
            policy=policy,
            plan_kind=normalized_kind,
            mode=mode,
            live_dry_run=live_dry_run,
            live_real_order_armed=live_real_order_armed,
            execution_engine=execution_engine,
            source=source,
            authority=authority,
            side=side,
            submit_expected=submit_expected,
            pre_submit_proof_status=proof,
            pre_submit_risk_approval_status=risk_status,
            pre_submit_risk_block_reason="none" if risk_error is None else risk_error,
            entry_authority_status=str(payload.get("entry_authority_status") or "not_required"),
            entry_authority_reason_code=str(payload.get("entry_authority_reason_code") or "none"),
            position_management_authority_status=str(
                payload.get("position_management_authority_status") or "not_required"
            ),
            closeout_authority_status=str(payload.get("closeout_authority_status") or "not_required"),
        )

    if mode == "live" and live_dry_run:
        return decision(False, "live_dry_run_non_submitting")
    if policy.live_real_order_requires_target_delta:
        if normalized_kind == "residual":
            if final_payload_required:
                final_error = final_payload_error()
                if final_error is not None:
                    return decision(False, final_error)
            if source != RESIDUAL_SUBMIT_SOURCE:
                return decision(False, "live_real_order_residual_plan_invalid_source")
            if authority not in RESIDUAL_SUBMIT_AUTHORITIES:
                return decision(False, "live_real_order_residual_plan_invalid_authority")
            if side != "SELL":
                return decision(False, "live_real_order_residual_plan_invalid_side")
            if not submit_expected:
                return decision(False, "live_real_order_residual_plan_submit_not_expected")
            if proof != "passed":
                return decision(False, "live_real_order_residual_plan_pre_submit_proof_not_passed")
            residual_mode = str(getattr(settings_obj, "RESIDUAL_LIVE_SELL_MODE", "") or "").strip().lower()
            if residual_mode != "enabled":
                return decision(False, "live_real_order_residual_policy_not_enabled")
            return decision(True, "allowed_residual_inventory_policy")
        if execution_engine != "target_delta":
            return decision(False, "live_real_order_requires_execution_engine_target_delta")
        if normalized_kind == "target":
            if final_payload_required:
                final_error = final_payload_error()
                if final_error is not None:
                    return decision(False, final_error)
            if source not in policy.allowed_submit_plan_sources:
                return decision(False, "submit_plan_source_not_allowed_for_mode")
            if source not in {TARGET_DELTA_SUBMIT_SOURCE, H74_SOURCE_OBSERVATION_SUBMIT_SOURCE}:
                return decision(False, "live_real_order_target_plan_invalid_source")
            if authority not in policy.allowed_submit_plan_authorities:
                return decision(False, "submit_plan_authority_not_allowed_for_mode")
            if authority not in TARGET_DELTA_SUBMIT_AUTHORITIES:
                return decision(False, "live_real_order_target_plan_invalid_authority")
            if side not in {"BUY", "SELL"}:
                return decision(False, "live_real_order_target_plan_invalid_side")
            if not submit_expected:
                return decision(False, "live_real_order_target_plan_submit_not_expected")
            if side == "BUY" and str(payload.get("entry_authority_status") or "") == "BLOCK":
                return decision(False, "target_delta_entry_without_strategy_buy_authority")
            if proof != "passed":
                return decision(False, "live_real_order_target_plan_pre_submit_proof_not_passed")
            if not bool(payload.get("portfolio_target_authoritative")):
                return decision(False, "live_real_order_target_plan_missing_authoritative_portfolio_target")
            if not str(payload.get("portfolio_target_hash") or "").strip():
                return decision(False, "live_real_order_target_plan_missing_portfolio_target_hash")
            if not str(payload.get("allocation_decision_hash") or "").strip():
                return decision(False, "live_real_order_target_plan_missing_allocation_decision_hash")
            if not str(payload.get("strategy_contribution_hash") or "").strip():
                return decision(False, "live_real_order_target_plan_missing_strategy_contribution_hash")
            if source == H74_SOURCE_OBSERVATION_SUBMIT_SOURCE:
                if side != "BUY":
                    return decision(False, "h74_source_observation_requires_buy")
                if authority != H74_SOURCE_OBSERVATION_SUBMIT_AUTHORITY:
                    return decision(False, "h74_source_observation_invalid_authority")
                if str(payload.get("submit_semantics") or "") != "quote_notional_market_buy":
                    return decision(False, "h74_source_observation_submit_semantics_missing")
                if str(payload.get("sizing_mode") or "") != "quote_notional":
                    return decision(False, "h74_source_observation_sizing_mode_missing")
                try:
                    quote_notional_krw = float(payload.get("quote_notional_krw") or 0.0)
                except (TypeError, ValueError):
                    quote_notional_krw = 0.0
                if quote_notional_krw <= 0.0:
                    return decision(False, "h74_source_observation_quote_notional_missing")
                if str(payload.get("fill_qty_authority") or "") != "broker_fill":
                    return decision(False, "h74_source_observation_fill_qty_authority_missing")
                if str(payload.get("position_mode") or "") != "fixed_fill_qty_until_exit":
                    return decision(False, "h74_source_observation_position_mode_missing")
                if str(payload.get("exchange_order_type") or "") != "price":
                    return decision(False, "h74_source_observation_order_type_not_price")
                if str(payload.get("exchange_submit_field") or "") != "price":
                    return decision(False, "h74_source_observation_submit_field_not_price")
            return decision(True, "allowed_target_delta")
        if normalized_kind == "buy":
            return decision(False, "live_real_order_buy_plan_rejected_target_delta_required")
        if source in LEGACY_BUY_SUBMIT_SOURCES:
            return decision(False, "live_real_order_legacy_source_rejected")
        if authority in LEGACY_BUY_SUBMIT_AUTHORITIES:
            return decision(False, "live_real_order_legacy_authority_rejected")
        return decision(False, "live_real_order_submit_plan_kind_rejected")

    if source not in policy.allowed_submit_plan_sources:
        return decision(False, "submit_plan_source_not_allowed_for_mode")
    if authority not in policy.allowed_submit_plan_authorities:
        return decision(False, "submit_plan_authority_not_allowed_for_mode")
    return decision(True, "allowed_mode_compatibility")


def live_real_order_legacy_buy_submit_plan_error(
    plan: object,
    *,
    settings_obj: object,
) -> str | None:
    policy = submit_authority_policy_from_settings(settings_obj)
    if not policy.live_real_order_requires_target_delta:
        return None
    payload = plan.as_dict() if hasattr(plan, "as_dict") else dict(plan or {})
    side = str(payload.get("side") or "").strip().upper()
    source = str(payload.get("source") or "").strip()
    authority = str(payload.get("authority") or "").strip()
    if side == "BUY" and (
        source != TARGET_DELTA_SUBMIT_SOURCE
        or authority not in TARGET_DELTA_SUBMIT_AUTHORITIES
    ):
        return "live_real_order_buy_plan_rejected_target_delta_required"
    return None


def _valid_sha256_prefixed(value: object) -> bool:
    text = str(value or "").strip()
    if not text.startswith("sha256:"):
        return False
    digest = text.removeprefix("sha256:")
    return len(digest) == 64 and all(char in "0123456789abcdef" for char in digest.lower())


def operational_pre_submit_risk_approval_error(
    payload: Mapping[str, object],
    *,
    expected_submit_plan_hash: str,
) -> str | None:
    approval = is_pre_submit_risk_approved_for_plan(
        payload,
        expected_submit_plan_hash=expected_submit_plan_hash,
    )
    return None if approval.approved else approval.reason


def validate_pre_submit_risk_proof_integrity(
    payload: Mapping[str, object],
    *,
    expected_submit_plan_hash: str,
) -> str | None:
    for field in (
        "pre_submit_risk_decision_hash",
        "pre_submit_risk_policy_hash",
        "pre_submit_risk_input_hash",
        "pre_submit_risk_evidence_hash",
        "effective_pre_submit_risk_policy_hash",
    ):
        if not _valid_sha256_prefixed(payload.get(field)):
            return f"live_real_order_{field}_missing"
    for field in (
        "pre_submit_risk_reason_code",
        "pre_submit_risk_state_source",
        "risk_policy_source",
        "pre_submit_risk_policy_composition_rule",
    ):
        if not str(payload.get(field) or "").strip():
            return f"live_real_order_{field}_missing"
    authority_hash_fields = (
        "strategy_risk_profile_hashes",
        "portfolio_risk_policy_hash",
        "operational_risk_policy_hash",
        "residual_risk_policy_hash",
    )
    if not any(
        (
            isinstance(payload.get(field), list)
            and any(_valid_sha256_prefixed(item) for item in payload.get(field) or [])
        )
        or _valid_sha256_prefixed(payload.get(field))
        for field in authority_hash_fields
    ):
        return "live_real_order_pre_submit_explicit_policy_authority_hash_missing"
    actual_plan_hash = str(payload.get("pre_submit_risk_plan_hash") or "").strip()
    if not str(expected_submit_plan_hash or "").strip():
        return "live_real_order_pre_submit_expected_plan_hash_missing"
    if actual_plan_hash != str(expected_submit_plan_hash):
        return "live_real_order_pre_submit_risk_plan_hash_mismatch"
    return None


def _pre_submit_allowed_actions(payload: Mapping[str, object]) -> set[str]:
    decision = payload.get("pre_submit_risk_decision")
    raw_actions: object = None
    if isinstance(decision, Mapping):
        raw_actions = decision.get("allowed_actions")
    if raw_actions is None:
        raw_actions = payload.get("pre_submit_risk_allowed_actions")
    if isinstance(raw_actions, str):
        actions = [raw_actions]
    elif isinstance(raw_actions, (list, tuple, set)):
        actions = list(raw_actions)
    else:
        actions = []
    return {str(action or "").strip().upper() for action in actions if str(action or "").strip()}


def evaluate_pre_submit_risk_action_authorization(payload: Mapping[str, object]) -> str | None:
    status = str(payload.get("pre_submit_risk_status") or "").strip().upper()
    if status == "ALLOW":
        return None
    if status == "REDUCE_ONLY":
        side = str(payload.get("side") or "").strip().upper()
        source = str(payload.get("source") or "").strip()
        authority = str(payload.get("authority") or "").strip()
        reason_code = str(payload.get("pre_submit_risk_reason_code") or "").strip().upper()
        try:
            target_delta_qty = float(payload.get("target_delta_qty") or 0.0)
        except (TypeError, ValueError):
            target_delta_qty = 0.0
        if (
            source == TARGET_DELTA_SUBMIT_SOURCE
            and authority == "canonical_target_delta_sizing"
            and side == "SELL"
            and bool(payload.get("submit_expected"))
            and target_delta_qty < 0.0
            and reason_code == "POSITION_LOSS_LIMIT"
            and "SELL" in _pre_submit_allowed_actions(payload)
        ):
            return None
        return "live_real_order_pre_submit_risk_reduce_only_not_authorized_for_plan"
    if status == "REQUIRE_RECONCILE":
        return "live_real_order_pre_submit_risk_requires_reconcile"
    if status == "BLOCK":
        return "live_real_order_pre_submit_risk_block"
    return "live_real_order_pre_submit_risk_not_allow"


def is_pre_submit_risk_approved_for_plan(
    payload: Mapping[str, object],
    *,
    expected_submit_plan_hash: str,
) -> PreSubmitRiskApproval:
    status = str(payload.get("pre_submit_risk_status") or "").strip().upper()
    integrity_error = validate_pre_submit_risk_proof_integrity(
        payload,
        expected_submit_plan_hash=expected_submit_plan_hash,
    )
    if integrity_error is not None:
        return PreSubmitRiskApproval(
            approved=False,
            reason=integrity_error,
            integrity_valid=False,
            action_authorized=False,
            status=status,
        )
    action_error = evaluate_pre_submit_risk_action_authorization(payload)
    if action_error is not None:
        return PreSubmitRiskApproval(
            approved=False,
            reason=action_error,
            integrity_valid=True,
            action_authorized=False,
            status=status,
        )
    return PreSubmitRiskApproval(
        approved=True,
        reason="approved",
        integrity_valid=True,
        action_authorized=True,
        status=status,
    )


__all__ = [
    "PreSubmitRiskApproval",
    "SubmitAuthorityPolicy",
    "SubmitAuthorityPolicyDecision",
    "evaluate_pre_submit_risk_action_authorization",
    "evaluate_submit_authority_policy",
    "is_pre_submit_risk_approved_for_plan",
    "operational_pre_submit_risk_approval_error",
    "submit_authority_policy_from_settings",
    "validate_pre_submit_risk_proof_integrity",
]
