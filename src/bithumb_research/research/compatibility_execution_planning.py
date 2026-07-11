from __future__ import annotations

from bithumb_research.execution_service import ExecutionSubmitPlan
from bithumb_research.strategy_policy_contract import StrategyDecisionV2

from .diagnostic_authority import (
    DIAGNOSTIC_EXECUTION_ARTIFACT_GRADE,
    DIAGNOSTIC_EXECUTION_AUTHORITY_PLANE,
    DIAGNOSTIC_EXECUTION_EVIDENCE_SOURCE,
)


def _research_execution_submit_plan(
    *,
    side: str,
    cash: float,
    buy_fraction: float,
    sellable_qty: float,
    reference_price: float,
    policy_decision: StrategyDecisionV2 | None,
) -> ExecutionSubmitPlan:
    """Compatibility-only adapter for exploratory research strategies without typed plans."""
    normalized_side = str(side or "").upper()
    execution_intent = policy_decision.execution_intent if policy_decision is not None else None
    intent_payload = (
        execution_intent.as_dict()
        if execution_intent is not None and hasattr(execution_intent, "as_dict")
        else {}
    )
    authority = "strategy_execution_intent" if intent_payload else "research_compatibility_execution_intent"
    if normalized_side == "BUY":
        fraction = float(intent_payload.get("budget_fraction_of_cash") or buy_fraction)
        requested_notional = max(0.0, float(cash) * fraction)
        max_budget = float(intent_payload.get("max_budget_krw") or 0.0)
        if max_budget > 0.0:
            requested_notional = min(requested_notional, max_budget)
        qty = requested_notional / float(reference_price) if reference_price > 0.0 else None
        submit_expected = bool(requested_notional > 0.0)
        return ExecutionSubmitPlan(
            side="BUY",
            source="research_backtest",
            authority=authority,
            final_action="ENTER_STRATEGY_POSITION" if submit_expected else "BLOCK_RESEARCH_ZERO_SIZE",
            qty=qty,
            notional_krw=requested_notional if submit_expected else None,
            target_exposure_krw=requested_notional if submit_expected else None,
            current_effective_exposure_krw=0.0,
            delta_krw=requested_notional if submit_expected else None,
            submit_expected=submit_expected,
            pre_submit_proof_status="not_required",
            block_reason="none" if submit_expected else "research_zero_buy_notional",
            idempotency_key=None,
            extra_payload={
                "execution_engine": "research_virtual",
                "compatibility_fallback": True,
                "research_compatibility_execution_fallback": True,
                "statistical_evidence": False,
                "artifact_grade": DIAGNOSTIC_EXECUTION_ARTIFACT_GRADE,
                "authority_plane": DIAGNOSTIC_EXECUTION_AUTHORITY_PLANE,
                "execution_evidence_source": DIAGNOSTIC_EXECUTION_EVIDENCE_SOURCE,
                "live_authoritative": False,
            },
        )
    if normalized_side == "SELL":
        qty = max(0.0, float(sellable_qty))
        notional = qty * float(reference_price) if reference_price > 0.0 else None
        submit_expected = bool(qty > 0.0)
        return ExecutionSubmitPlan(
            side="SELL",
            source="research_backtest",
            authority=authority,
            final_action="EXIT_STRATEGY_POSITION" if submit_expected else "BLOCK_RESEARCH_ZERO_SIZE",
            qty=qty if submit_expected else None,
            notional_krw=notional if submit_expected else None,
            target_exposure_krw=0.0 if submit_expected else None,
            current_effective_exposure_krw=notional if submit_expected else None,
            delta_krw=-(notional or 0.0) if submit_expected else None,
            submit_expected=submit_expected,
            pre_submit_proof_status="not_required",
            block_reason="none" if submit_expected else "research_zero_sell_qty",
            idempotency_key=None,
            extra_payload={
                "execution_engine": "research_virtual",
                "compatibility_fallback": True,
                "research_compatibility_execution_fallback": True,
                "statistical_evidence": False,
                "artifact_grade": DIAGNOSTIC_EXECUTION_ARTIFACT_GRADE,
                "authority_plane": DIAGNOSTIC_EXECUTION_AUTHORITY_PLANE,
                "execution_evidence_source": DIAGNOSTIC_EXECUTION_EVIDENCE_SOURCE,
                "live_authoritative": False,
            },
        )
    raise ValueError(f"research_submit_plan_unsupported_side:{normalized_side or 'missing'}")


__all__ = ["_research_execution_submit_plan"]
