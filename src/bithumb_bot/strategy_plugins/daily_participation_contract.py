from __future__ import annotations

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


__all__ = ["DAILY_PARTICIPATION_DECISION_EVIDENCE_CONTRACT", "DAILY_PARTICIPATION_REQUIRED_FIELDS"]
