from __future__ import annotations

from typing import Mapping

from bithumb_bot.strategy_evidence_contract import DecisionEvidenceContract
from bithumb_bot.strategy_plugins.sma_with_filter_contract import SMA_DECISION_EVIDENCE_CONTRACT


DAILY_PARTICIPATION_REQUIRED_FIELDS = (
    "daily_count_snapshot_hash",
    "daily_count_snapshot_event_set_hash",
    "participation_policy_hash",
    "participation_input_hash",
    "participation_decision_hash",
    "entry_signal_source",
    "fallback_mode",
)


DAILY_PARTICIPATION_DECISION_EVIDENCE_CONTRACT = DecisionEvidenceContract(
    requires_decision_input_bundle=SMA_DECISION_EVIDENCE_CONTRACT.requires_decision_input_bundle,
    required_promotion_provenance_fields=(
        *SMA_DECISION_EVIDENCE_CONTRACT.required_promotion_provenance_fields,
        *DAILY_PARTICIPATION_REQUIRED_FIELDS,
    ),
    required_live_real_order_fields=(
        *SMA_DECISION_EVIDENCE_CONTRACT.required_live_real_order_fields,
        *DAILY_PARTICIPATION_REQUIRED_FIELDS,
    ),
    required_live_real_order_one_of_field_groups=SMA_DECISION_EVIDENCE_CONTRACT.required_live_real_order_one_of_field_groups,
    snapshot_projector_contract=SMA_DECISION_EVIDENCE_CONTRACT.snapshot_projector_contract,
    decision_input_contract_kind="daily_participation_sma",
)


DAILY_PARTICIPATION_BUY_SUBMIT_REQUIRED_FIELDS = (
    "daily_count_snapshot_hash",
    "participation_policy_hash",
    "participation_decision_hash",
    "fallback_mode",
    "entry_signal_source",
)


def daily_participation_submit_payload_error(payload: Mapping[str, object]) -> str | None:
    if str(payload.get("strategy_name") or "").strip().lower() != "daily_participation_sma":
        return None
    if str(payload.get("side") or "").strip().upper() != "BUY":
        return None
    if not bool(payload.get("submit_expected")):
        return None
    missing = [
        field
        for field in DAILY_PARTICIPATION_BUY_SUBMIT_REQUIRED_FIELDS
        if not str(payload.get(field) or "").strip()
    ]
    fee_ok = bool(
        str(payload.get("fee_authority_hash") or "").strip()
        or str(payload.get("fee_authority_payload_hash") or "").strip()
        or str(payload.get("fee_authority") or "").strip()
    )
    if not fee_ok:
        missing.append("fee_authority")
    price_ok = bool(
        str(payload.get("price_protection_hash") or "").strip()
        or str(payload.get("price_protection_evidence_hash") or "").strip()
        or str(payload.get("price_protection") or "").strip()
        or str(payload.get("order_rules_hash") or "").strip()
        or str(payload.get("order_rules_payload_hash") or "").strip()
    )
    if not price_ok:
        missing.append("price_protection")
    if bool(payload.get("live_real_order") or payload.get("live_real_order_allowed")):
        max_slippage = payload.get("price_protection_max_slippage_bps")
        try:
            max_slippage_value = float(max_slippage)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            max_slippage_value = 0.0
        if max_slippage_value <= 0.0:
            missing.append("price_protection_positive_max_slippage")
    if missing:
        return "daily_participation_submit_evidence_missing:" + ",".join(sorted(set(missing)))
    return None


def daily_participation_submit_plan_extra(payload: Mapping[str, object]) -> dict[str, object]:
    if str(payload.get("strategy_name") or payload.get("strategy") or "").strip().lower() != "daily_participation_sma":
        return {}
    extra: dict[str, object] = {
        "strategy_name": "daily_participation_sma",
    }
    for key in (
        "strategy_instance_id",
        "pair",
        "daily_count_snapshot_hash",
        "daily_count_snapshot_event_set_hash",
        "participation_policy_hash",
        "participation_input_hash",
        "participation_decision_hash",
        "fallback_mode",
        "entry_signal_source",
        "fee_authority_hash",
        "fee_authority_payload_hash",
        "order_rules_hash",
        "order_rules_payload_hash",
        "price_protection_hash",
        "price_protection_evidence_hash",
    ):
        value = payload.get(key)
        if str(value or "").strip():
            extra[key] = value
    if "price_protection_hash" not in extra and str(payload.get("order_rules_hash") or "").strip():
        extra["price_protection_hash"] = payload["order_rules_hash"]
    return extra


__all__ = [
    "DAILY_PARTICIPATION_DECISION_EVIDENCE_CONTRACT",
    "DAILY_PARTICIPATION_REQUIRED_FIELDS",
    "daily_participation_submit_payload_error",
    "daily_participation_submit_plan_extra",
]
