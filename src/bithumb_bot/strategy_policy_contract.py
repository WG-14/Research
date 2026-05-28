from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


Signal = str


@dataclass(frozen=True)
class PositionSnapshot:
    in_position: bool
    entry_allowed: bool
    exit_allowed: bool
    entry_block_reason: str = ""
    exit_block_reason: str = ""
    terminal_state: str = "flat"
    entry_ts: int | None = None
    entry_price: float | None = None
    qty_open: float = 0.0
    holding_time_sec: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_ratio: float = 0.0
    raw_qty_open: float = 0.0
    raw_total_asset_qty: float = 0.0
    open_lot_count: int = 0
    dust_tracking_lot_count: int = 0
    reserved_exit_lot_count: int = 0
    sellable_executable_lot_count: int = 0
    dust_classification: str = ""
    dust_state: str = ""
    effective_flat: bool = True
    has_executable_exposure: bool = False
    has_any_position_residue: bool = False
    has_non_executable_residue: bool = False
    has_dust_only_remainder: bool = False

    def policy_input_payload(self) -> dict[str, object]:
        return {
            "in_position": bool(self.in_position),
            "entry_allowed": bool(self.entry_allowed),
            "exit_allowed": bool(self.exit_allowed),
            "entry_block_reason": self.entry_block_reason,
            "exit_block_reason": self.exit_block_reason,
            "terminal_state": self.terminal_state,
            "dust_classification": self.dust_classification,
            "dust_state": self.dust_state,
            "effective_flat": bool(self.effective_flat),
            "has_executable_exposure": bool(self.has_executable_exposure),
            "has_any_position_residue": bool(self.has_any_position_residue),
            "has_non_executable_residue": bool(self.has_non_executable_residue),
            "has_dust_only_remainder": bool(self.has_dust_only_remainder),
        }


@dataclass(frozen=True)
class ExecutionConstraintSnapshot:
    fee_rate_for_decision: float
    fee_authority_degraded_blocks_entry: bool = False
    fee_authority: dict[str, object] = field(default_factory=dict)
    order_rules: dict[str, object] = field(default_factory=dict)

    def policy_input_payload(self) -> dict[str, object]:
        fee_authority = {
            key: value
            for key, value in dict(self.fee_authority).items()
            if key not in {"retrieved_at_sec", "expires_at_sec"}
        }
        return {
            "fee_rate_for_decision": float(self.fee_rate_for_decision),
            "fee_authority_degraded_blocks_entry": bool(
                self.fee_authority_degraded_blocks_entry
            ),
            "fee_authority": fee_authority,
            "order_rules": dict(self.order_rules),
        }


@dataclass(frozen=True)
class ExecutionIntentV1:
    side: str
    intent: str
    pair: str
    requires_execution_sizing: bool
    schema_version: int = 1
    intent_version: int = 1

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": int(self.schema_version),
            "intent_version": int(self.intent_version),
            "side": self.side,
            "intent": self.intent,
            "pair": self.pair,
            "requires_execution_sizing": bool(self.requires_execution_sizing),
        }


@dataclass(frozen=True)
class EntryExecutionIntent(ExecutionIntentV1):
    budget_model: str = "cash_fraction_capped_by_max_order_krw"
    budget_fraction_of_cash: float = 0.0
    max_budget_krw: float = 0.0

    def as_dict(self) -> dict[str, object]:
        payload = super().as_dict()
        payload.update(
            {
                "budget_model": self.budget_model,
                "budget_fraction_of_cash": float(self.budget_fraction_of_cash),
                "max_budget_krw": float(self.max_budget_krw),
            }
        )
        return payload


@dataclass(frozen=True)
class ExitExecutionIntent(ExecutionIntentV1):
    def as_dict(self) -> dict[str, object]:
        return super().as_dict()


@dataclass(frozen=True)
class StrategyDecisionV2:
    strategy_name: str
    raw_signal: Signal
    raw_reason: str
    entry_signal: Signal
    entry_reason: str
    exit_signal: Signal
    exit_reason: str
    final_signal: Signal
    final_reason: str
    blocked_filters: tuple[str, ...]
    entry_blocked: bool
    entry_block_reason: str | None
    exit_rule: str | None
    exit_evaluations: tuple[dict[str, object], ...]
    protective_exit_overrode_entry: bool
    exit_filter_suppression_prevented: bool
    position_snapshot: PositionSnapshot
    execution_intent: ExecutionIntentV1 | None
    entry_decision: object | None
    trace: dict[str, object]
    policy_hash: str
    policy_contract_hash: str
    policy_input_hash: str
    policy_decision_hash: str

    def as_trace(self) -> dict[str, object]:
        payload = dict(self.trace)
        payload["policy_hash"] = self.policy_hash
        payload["policy_contract_hash"] = self.policy_contract_hash
        payload["policy_input_hash"] = self.policy_input_hash
        payload["policy_decision_hash"] = self.policy_decision_hash
        return payload
