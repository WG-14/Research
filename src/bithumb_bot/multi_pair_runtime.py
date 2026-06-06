from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Sequence

from .canonical_decision import sha256_prefixed
from .db_core import multi_asset_ledger_authority_status
from .execution_plan_batch import ExecutionPlanBatch, PairExecutionPlan
from .execution_service import ExecutionSubmitPlan
from .portfolio_allocation import (
    PortfolioAllocationDecision,
    PortfolioAllocationInput,
    PortfolioAllocator,
    PortfolioAllocatorConfig,
    StrategyPreference,
    StrategyPreferenceSet,
)
from .runtime_scope import (
    ReplayHashChain,
    RuntimeScopeKey,
    validate_replay_hash_chain,
    validate_scope_key_hash,
)
from .virtual_target_state import assert_not_live_submit_authority


MULTI_PAIR_GATE_OFF_REASON = "multi_pair_runtime_unsupported"
MULTI_PAIR_SCOPE_MODE = "multi_pair_portfolio"
MULTI_PAIR_REPLAY_POLICY = "multi_pair_replay_hash_chain_required_v1"


def _clean(value: object) -> str:
    return str(value or "").strip()


def _base_currency_from_pair(pair: str) -> str:
    text = _clean(pair)
    return text.split("-", 1)[1].upper() if "-" in text else text.upper()


class MultiPairFailClosed(RuntimeError):
    def __init__(self, reason: str, *, evidence: Mapping[str, object]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.evidence = MappingProxyType(dict(evidence))

    def as_dict(self) -> dict[str, object]:
        return dict(self.evidence)


@dataclass(frozen=True)
class MultiPairRuntimeAuthority:
    enabled: bool = False
    authority_source: str = "runtime_config"
    shard_authority_verified: bool = False
    batch_risk_authority_verified: bool = False
    ledger_authority_verified: bool = False
    reconcile_authority_verified: bool = False
    required_migration: str = "RuntimeScopeV2_multi_pair_authority"
    schema_version: int = 1

    def missing_authorities(self) -> tuple[str, ...]:
        if not self.enabled:
            return ("live_multi_pair_enablement_gate",)
        checks = {
            "pair_scoped_shard_authority": self.shard_authority_verified,
            "batch_risk_authority": self.batch_risk_authority_verified,
            "multi_asset_ledger_authority": self.ledger_authority_verified,
            "pair_reconcile_authority": self.reconcile_authority_verified,
        }
        return tuple(key for key, value in checks.items() if not bool(value))

    def as_dict(self) -> dict[str, object]:
        missing = self.missing_authorities()
        return {
            "schema_version": int(self.schema_version),
            "runtime_scope_mode": MULTI_PAIR_SCOPE_MODE,
            "live_multi_pair_enabled": bool(self.enabled),
            "authority_source": self.authority_source,
            "shard_authority_verified": bool(self.shard_authority_verified),
            "batch_risk_authority_verified": bool(self.batch_risk_authority_verified),
            "ledger_authority_verified": bool(self.ledger_authority_verified),
            "reconcile_authority_verified": bool(self.reconcile_authority_verified),
            "missing_authorities": list(missing),
            "authority_verified": bool(self.enabled and not missing),
            "required_migration": self.required_migration,
        }


@dataclass(frozen=True)
class PairRuntimeInputs:
    pair: str
    interval: str
    scope_key: RuntimeScopeKey
    data_preflight: Mapping[str, object]
    selected_candle: Mapping[str, object]
    feature_snapshot: Mapping[str, object]
    strategy_preference: StrategyPreference
    decision_artifact: Mapping[str, object]
    previous_target_exposure_krw: float | None
    reference_price: float | None
    reconcile_status: str = "reconcile_verified"
    submit_plan: ExecutionSubmitPlan | None = None
    recovery_evidence: Mapping[str, object] = field(default_factory=dict)
    schema_version: int = 1

    def __post_init__(self) -> None:
        pair = _clean(self.pair)
        interval = _clean(self.interval)
        if not pair:
            raise ValueError("multi_pair_input_pair_missing")
        if not interval:
            raise ValueError("multi_pair_input_interval_missing")
        if not isinstance(self.scope_key, RuntimeScopeKey):
            raise TypeError("multi_pair_input_scope_key_required")
        if self.scope_key.pair != pair:
            raise ValueError("multi_pair_input_scope_pair_mismatch")
        if self.scope_key.interval != interval:
            raise ValueError("multi_pair_input_scope_interval_mismatch")
        if self.strategy_preference.pair != pair:
            raise ValueError("multi_pair_input_preference_pair_mismatch")
        object.__setattr__(self, "pair", pair)
        object.__setattr__(self, "interval", interval)
        object.__setattr__(self, "data_preflight", MappingProxyType(dict(self.data_preflight)))
        object.__setattr__(self, "selected_candle", MappingProxyType(dict(self.selected_candle)))
        object.__setattr__(self, "feature_snapshot", MappingProxyType(dict(self.feature_snapshot)))
        object.__setattr__(self, "decision_artifact", MappingProxyType(dict(self.decision_artifact)))
        object.__setattr__(self, "recovery_evidence", MappingProxyType(dict(self.recovery_evidence)))

    def data_preflight_hash(self) -> str:
        return sha256_prefixed(dict(self.data_preflight))

    def selected_candle_hash(self) -> str:
        return sha256_prefixed(dict(self.selected_candle))

    def feature_snapshot_hash(self) -> str:
        return sha256_prefixed(dict(self.feature_snapshot))

    def decision_artifact_hash(self) -> str:
        return sha256_prefixed(dict(self.decision_artifact))


@dataclass(frozen=True)
class PairRuntimeShard:
    pair: str
    interval: str
    scope_key: RuntimeScopeKey
    data_preflight: Mapping[str, object]
    selected_candle: Mapping[str, object]
    feature_snapshot: Mapping[str, object]
    decision_artifact: Mapping[str, object]
    allocation_target: Mapping[str, object]
    execution_pair_plan: PairExecutionPlan
    reconcile_status: str
    replay_hash_chain: ReplayHashChain
    recovery_evidence: Mapping[str, object] = field(default_factory=dict)
    submit_status: str = "not_submitted"
    broker_response: Mapping[str, object] = field(default_factory=dict)
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.scope_key, RuntimeScopeKey):
            raise TypeError("pair_runtime_shard_scope_key_required")
        if not isinstance(self.execution_pair_plan, PairExecutionPlan):
            raise TypeError("pair_runtime_shard_pair_plan_required")
        if self.scope_key.pair != _clean(self.pair):
            raise ValueError("pair_runtime_shard_scope_pair_mismatch")
        if self.execution_pair_plan.pair != _clean(self.pair):
            raise ValueError("pair_runtime_shard_plan_pair_mismatch")
        object.__setattr__(self, "data_preflight", MappingProxyType(dict(self.data_preflight)))
        object.__setattr__(self, "selected_candle", MappingProxyType(dict(self.selected_candle)))
        object.__setattr__(self, "feature_snapshot", MappingProxyType(dict(self.feature_snapshot)))
        object.__setattr__(self, "decision_artifact", MappingProxyType(dict(self.decision_artifact)))
        object.__setattr__(self, "allocation_target", MappingProxyType(dict(self.allocation_target)))
        object.__setattr__(self, "recovery_evidence", MappingProxyType(dict(self.recovery_evidence)))
        object.__setattr__(self, "broker_response", MappingProxyType(dict(self.broker_response)))

    def as_dict(self) -> dict[str, object]:
        payload = {
            "schema_version": int(self.schema_version),
            "pair": self.pair,
            "interval": self.interval,
            "runtime_scope_key": self.scope_key.as_dict(),
            "scope_key_hash": self.scope_key.scope_key_hash(),
            "data_preflight": dict(self.data_preflight),
            "data_preflight_hash": sha256_prefixed(dict(self.data_preflight)),
            "selected_candle": dict(self.selected_candle),
            "selected_candle_hash": sha256_prefixed(dict(self.selected_candle)),
            "feature_snapshot": dict(self.feature_snapshot),
            "feature_snapshot_hash": sha256_prefixed(dict(self.feature_snapshot)),
            "decision_artifact": dict(self.decision_artifact),
            "decision_artifact_hash": sha256_prefixed(dict(self.decision_artifact)),
            "allocation_target": dict(self.allocation_target),
            "portfolio_target_hash": str(self.allocation_target.get("final_portfolio_target_hash") or ""),
            "execution_pair_plan": self.execution_pair_plan.as_dict(),
            "pair_execution_plan_hash": self.execution_pair_plan.content_hash(),
            "reconcile_status": self.reconcile_status,
            "replay_hash_chain": self.replay_hash_chain.as_dict(),
            "replay_hash_chain_hash": self.replay_hash_chain.chain_hash(),
            "submit_status": self.submit_status,
            "broker_response": dict(self.broker_response),
            "recovery_evidence": dict(self.recovery_evidence),
        }
        payload["runtime_shard_hash"] = sha256_prefixed(payload)
        return payload

    def content_hash(self) -> str:
        return str(self.as_dict()["runtime_shard_hash"])


@dataclass(frozen=True)
class MultiPairRuntimeResult:
    authority: MultiPairRuntimeAuthority
    shards: tuple[PairRuntimeShard, ...]
    allocation_decision: PortfolioAllocationDecision
    execution_plan_batch: ExecutionPlanBatch
    ledger_authority: Mapping[str, object]
    observability: Mapping[str, object]
    schema_version: int = 1

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": int(self.schema_version),
            "runtime_scope_mode": MULTI_PAIR_SCOPE_MODE,
            "authority": self.authority.as_dict(),
            "runtime_shards": [shard.as_dict() for shard in self.shards],
            "allocation_decision": self.allocation_decision.as_dict(),
            "execution_plan_batch": self.execution_plan_batch.as_dict(),
            "ledger_authority": dict(self.ledger_authority),
            "observability": dict(self.observability),
        }


def fail_closed_evidence(
    *,
    blocked_layer: str,
    reason: str,
    authority: MultiPairRuntimeAuthority | None = None,
    pair: str = "",
    scope_key_hash: str = "",
    replay_layer: str = "",
    missing_authorities: Sequence[str] = (),
) -> dict[str, object]:
    authority_payload = {} if authority is None else authority.as_dict()
    missing = list(missing_authorities or authority_payload.get("missing_authorities") or [])
    return {
        "schema_version": 1,
        "runtime_scope_mode": MULTI_PAIR_SCOPE_MODE,
        "blocked_layer": blocked_layer,
        "unsupported_reason": reason,
        "fail_closed_reason": reason,
        "required_migration": authority_payload.get("required_migration", "RuntimeScopeV2_multi_pair_authority"),
        "missing_authority": missing[0] if missing else "",
        "missing_authorities": missing,
        "pair": pair,
        "scope_key_hash": scope_key_hash,
        "replay_layer": replay_layer,
        "operator_next_action": "inspect_multi_pair_runtime_authority",
    }


def require_multi_pair_enabled(
    pairs: Sequence[str],
    authority: MultiPairRuntimeAuthority,
) -> None:
    normalized_pairs = tuple(dict.fromkeys(_clean(pair) for pair in pairs if _clean(pair)))
    if len(normalized_pairs) <= 1:
        return
    missing = authority.missing_authorities()
    if not authority.enabled:
        raise MultiPairFailClosed(
            MULTI_PAIR_GATE_OFF_REASON,
            evidence=fail_closed_evidence(
                blocked_layer="runtime_scope_validation",
                reason=MULTI_PAIR_GATE_OFF_REASON,
                authority=authority,
                missing_authorities=missing,
            ),
        )
    if missing:
        reason = f"multi_pair_missing_authority:{missing[0]}"
        raise MultiPairFailClosed(
            reason,
            evidence=fail_closed_evidence(
                blocked_layer="runtime_authority_validation",
                reason=reason,
                authority=authority,
                missing_authorities=missing,
            ),
        )


def enforce_single_interval(inputs: Sequence[PairRuntimeInputs]) -> str:
    intervals = sorted({_clean(item.interval) for item in inputs})
    if len(intervals) != 1:
        raise MultiPairFailClosed(
            "single_interval_runtime_unsupported",
            evidence=fail_closed_evidence(
                blocked_layer="decision_clock_preflight",
                reason="single_interval_runtime_unsupported",
                missing_authorities=("single_interval_same_closed_candle_fail_closed_v1",),
            ),
        )
    return intervals[0]


def validate_pair_input_preflight(item: PairRuntimeInputs) -> None:
    scope_hash = item.scope_key.scope_key_hash()
    for layer, payload, expected_pair in (
        ("data_preflight", item.data_preflight, item.pair),
        ("selected_candle", item.selected_candle, item.pair),
        ("feature_snapshot", item.feature_snapshot, item.pair),
        ("decision_artifact", item.decision_artifact, item.pair),
    ):
        pair = _clean(payload.get("pair"))
        interval = _clean(payload.get("interval"))
        payload_scope_hash = _clean(payload.get("scope_key_hash"))
        if pair and pair != expected_pair:
            raise MultiPairFailClosed(
                f"{layer}_pair_mismatch",
                evidence=fail_closed_evidence(
                    blocked_layer=layer,
                    reason=f"{layer}_pair_mismatch",
                    pair=item.pair,
                    scope_key_hash=scope_hash,
                ),
            )
        if interval and interval != item.interval:
            raise MultiPairFailClosed(
                f"{layer}_interval_mismatch",
                evidence=fail_closed_evidence(
                    blocked_layer=layer,
                    reason=f"{layer}_interval_mismatch",
                    pair=item.pair,
                    scope_key_hash=scope_hash,
                ),
            )
        if payload_scope_hash and payload_scope_hash != scope_hash:
            raise MultiPairFailClosed(
                f"{layer}_scope_key_hash_mismatch",
                evidence=fail_closed_evidence(
                    blocked_layer=layer,
                    reason=f"{layer}_scope_key_hash_mismatch",
                    pair=item.pair,
                    scope_key_hash=scope_hash,
                    replay_layer=layer,
                ),
            )


def verify_multi_asset_ledger_authority(
    conn: sqlite3.Connection,
    *,
    required_pairs: Sequence[str],
    quote_currency: str = "KRW",
) -> dict[str, object]:
    base_status = multi_asset_ledger_authority_status(conn)
    required_pair_set = tuple(sorted({_clean(pair) for pair in required_pairs if _clean(pair)}))
    required_currencies = tuple(sorted({quote_currency.upper(), *(_base_currency_from_pair(pair) for pair in required_pair_set)}))
    missing_balances: list[str] = []
    missing_positions: list[str] = []
    if not base_status.get("missing_tables"):
        for currency in required_currencies:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM account_balances
                WHERE currency=? AND COALESCE(evidence_hash, '') != '' AND COALESCE(updated_ts, 0) > 0
                """,
                (currency,),
            ).fetchone()
            if int(row["count"] or 0) <= 0:
                missing_balances.append(currency)
        for pair in required_pair_set:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM pair_positions
                WHERE pair=? AND COALESCE(evidence_hash, '') != '' AND COALESCE(updated_ts, 0) > 0
                """,
                (pair,),
            ).fetchone()
            if int(row["count"] or 0) <= 0:
                missing_positions.append(pair)
    locks_present = bool(
        not base_status.get("missing_tables")
        and int(dict(base_status.get("table_counts") or {}).get("budget_locks") or 0) > 0
        and int(dict(base_status.get("table_counts") or {}).get("order_locks") or 0) > 0
    )
    reconcile_verified = bool(base_status.get("authority_verified")) and not missing_balances and not missing_positions
    authority_verified = bool(reconcile_verified and locks_present)
    reason = "OK"
    if base_status.get("missing_tables"):
        reason = "multi_asset_ledger_authority_missing"
    elif missing_balances:
        reason = "multi_asset_balance_authority_missing"
    elif missing_positions:
        reason = "multi_asset_pair_position_authority_missing"
    elif not locks_present:
        reason = "multi_asset_lock_authority_missing"
    elif not authority_verified:
        reason = "multi_asset_ledger_authority_unverified"
    payload = {
        **base_status,
        "required_pairs": list(required_pair_set),
        "required_currencies": list(required_currencies),
        "missing_required_balances": missing_balances,
        "missing_required_pair_positions": missing_positions,
        "quote_budget_locks_present": locks_present,
        "base_order_locks_present": locks_present,
        "reconcile_status": "verified" if reconcile_verified else "not_multi_pair_verified",
        "authority_verified": authority_verified,
        "authority_verification_status": "verified" if authority_verified else "present_unverified",
        "portfolio_id_1_multi_pair_live_authority": False,
        "live_multi_pair_enablement": "enabled_authority_verified" if authority_verified else "fail_closed_until_scoped_batch_ledger_authority_verified",
        "fail_closed_reason": "" if authority_verified else reason,
    }
    payload["ledger_authority_hash"] = sha256_prefixed(payload)
    return payload


def _pair_plan_for_target(
    *,
    pair: str,
    target_payload: Mapping[str, object],
    item: PairRuntimeInputs,
    manifest_hash: str,
) -> PairExecutionPlan:
    submit_plan = item.submit_plan
    submit_hash = (
        submit_plan.content_hash()
        if submit_plan is not None
        else sha256_prefixed(
            {
                "pair": pair,
                "submit_expected": False,
                "target_hash": target_payload.get("final_portfolio_target_hash"),
                "reason": "hold_or_block_without_submit_plan",
            }
        )
    )
    submit_expected = bool(submit_plan is not None and submit_plan.submit_expected)
    pre_submit_required = bool(
        submit_expected
        and submit_plan is not None
        and bool(submit_plan.extra_payload.get("pre_submit_risk_required"))
    )
    pre_submit_hash = ""
    if submit_plan is not None:
        pre_submit_hash = _clean(submit_plan.extra_payload.get("pre_submit_risk_decision_hash"))
    order_rule_evidence = {
        "schema_version": 1,
        "pair": pair,
        "authority": "pair_scoped_order_rule_evidence",
        "scope_key_hash": item.scope_key.scope_key_hash(),
        "manifest_hash": manifest_hash,
    }
    order_rule_hash = sha256_prefixed(order_rule_evidence)
    lock_evidence = {
        "schema_version": 1,
        "pair": pair,
        "scope_key_hash": item.scope_key.scope_key_hash(),
        "submit_plan_hash": submit_hash,
        "lock_scope": "pair",
        "lock_status": "verified",
    }
    lock_hash = sha256_prefixed(lock_evidence)
    return PairExecutionPlan(
        pair=pair,
        scope_key_hash=item.scope_key.scope_key_hash(),
        scope_key_hashes=tuple(target_payload.get("scope_key_hashes") or (item.scope_key.scope_key_hash(),)),
        portfolio_target_hash=str(target_payload.get("final_portfolio_target_hash") or ""),
        execution_submit_plan_hash=submit_hash,
        execution_plan_hash=sha256_prefixed({"pair": pair, "target": dict(target_payload), "submit_hash": submit_hash}),
        order_rule_snapshot_hash=order_rule_hash,
        order_rule_signature=sha256_prefixed({"pair": pair, "order_rule_snapshot_hash": order_rule_hash}),
        order_rule_snapshot=order_rule_evidence,
        idempotency_key=(
            _clean(submit_plan.idempotency_key)
            if submit_plan is not None and _clean(submit_plan.idempotency_key)
            else sha256_prefixed({"pair": pair, "scope_key_hash": item.scope_key.scope_key_hash(), "submit_hash": submit_hash})
        ),
        submit_authority_policy_hash=(
            _clean(submit_plan.submit_authority_policy_hash)
            if submit_plan is not None and _clean(submit_plan.submit_authority_policy_hash)
            else sha256_prefixed({"policy": "multi_pair_submit_authority_v1", "pair": pair})
        ),
        pre_submit_risk_decision_hash=pre_submit_hash,
        pre_submit_risk_required=pre_submit_required,
        pre_submit_risk_proof_status="ALLOW" if pre_submit_required else "not_required",
        pre_submit_risk_not_required_reason="" if pre_submit_required else "not_live_real_submit_path_or_no_submit",
        submit_expected=submit_expected,
        lock_evidence_hash=lock_hash,
        lock_type="quote_budget" if submit_plan is not None and submit_plan.side.upper() == "BUY" else "base_order" if submit_plan is not None and submit_plan.side.upper() == "SELL" else "none",
        lock_status="verified",
        replay_evidence={
            "scope_key_hash": item.scope_key.scope_key_hash(),
            "runtime_data_availability_hash": item.data_preflight_hash(),
            "feature_snapshot_hash": item.feature_snapshot_hash(),
            "portfolio_target_hash": str(target_payload.get("final_portfolio_target_hash") or ""),
            "execution_submit_plan_hash": submit_hash,
            "lock_evidence_hash": lock_hash,
            "order_rule_snapshot_hash": order_rule_hash,
        },
    )


def build_multi_pair_runtime(
    *,
    inputs: Sequence[PairRuntimeInputs],
    authority: MultiPairRuntimeAuthority,
    allocator_config: PortfolioAllocatorConfig,
    manifest_hash: str,
    conn: sqlite3.Connection | None = None,
) -> MultiPairRuntimeResult:
    if not inputs:
        raise MultiPairFailClosed(
            "multi_pair_inputs_missing",
            evidence=fail_closed_evidence(blocked_layer="runtime_scope_validation", reason="multi_pair_inputs_missing"),
        )
    pairs = tuple(item.pair for item in inputs)
    require_multi_pair_enabled(pairs, authority)
    interval = enforce_single_interval(inputs)
    for item in inputs:
        validate_pair_input_preflight(item)
        try:
            assert_not_live_submit_authority(item.decision_artifact.get("virtual_target_state"))
        except TypeError as exc:
            raise MultiPairFailClosed(
                str(exc),
                evidence=fail_closed_evidence(
                    blocked_layer="actual_target_authority",
                    reason=str(exc),
                    authority=authority,
                    pair=item.pair,
                    scope_key_hash=item.scope_key.scope_key_hash(),
                ),
            ) from exc
    previous_by_pair = {item.pair: item.previous_target_exposure_krw for item in inputs}
    reference_by_pair = {item.pair: item.reference_price for item in inputs}
    if any(value is None for value in previous_by_pair.values()):
        missing_pair = next(pair for pair, value in previous_by_pair.items() if value is None)
        raise MultiPairFailClosed(
            "multi_pair_previous_target_missing",
            evidence=fail_closed_evidence(
                blocked_layer="portfolio_allocation_input",
                reason="multi_pair_previous_target_missing",
                authority=authority,
                pair=missing_pair,
            ),
        )
    if any(value is None for value in reference_by_pair.values()):
        missing_pair = next(pair for pair, value in reference_by_pair.items() if value is None)
        raise MultiPairFailClosed(
            "multi_pair_reference_price_missing",
            evidence=fail_closed_evidence(
                blocked_layer="portfolio_allocation_input",
                reason="multi_pair_reference_price_missing",
                authority=authority,
                pair=missing_pair,
            ),
        )
    allocation_input = PortfolioAllocationInput(
        preference_set=StrategyPreferenceSet(tuple(item.strategy_preference for item in inputs)),
        allocator_config=allocator_config,
        previous_target_exposure_by_pair=previous_by_pair,
        reference_price_by_pair=reference_by_pair,
    )
    allocation_decision = PortfolioAllocator(allocator_config).allocate(allocation_input)
    if not allocation_decision.authoritative:
        raise MultiPairFailClosed(
            allocation_decision.primary_block_reason,
            evidence=fail_closed_evidence(
                blocked_layer="portfolio_allocation",
                reason=allocation_decision.primary_block_reason,
                authority=authority,
            ),
        )
    ledger_authority = (
        verify_multi_asset_ledger_authority(conn, required_pairs=pairs)
        if conn is not None
        else {
            "authority_verified": bool(authority.ledger_authority_verified),
            "portfolio_id_1_multi_pair_live_authority": False,
            "reconcile_status": "verified" if authority.reconcile_authority_verified else "not_multi_pair_verified",
        }
    )
    if not bool(ledger_authority.get("authority_verified")):
        raise MultiPairFailClosed(
            str(ledger_authority.get("fail_closed_reason") or "multi_asset_ledger_authority_unverified"),
            evidence=fail_closed_evidence(
                blocked_layer="multi_asset_ledger_authority",
                reason=str(ledger_authority.get("fail_closed_reason") or "multi_asset_ledger_authority_unverified"),
                authority=authority,
                missing_authorities=("multi_asset_ledger_authority",),
            ),
        )
    pair_plans: list[PairExecutionPlan] = []
    target_by_pair = {target.pair: target.as_dict() for target in allocation_decision.targets}
    for item in inputs:
        pair_plans.append(
            _pair_plan_for_target(
                pair=item.pair,
                target_payload=target_by_pair[item.pair],
                item=item,
                manifest_hash=manifest_hash,
            )
        )
    batch_risk_evidence = {
        "schema_version": 1,
        "risk_scope": "multi_pair_portfolio",
        "decision_clock_policy": "single_interval_same_closed_candle_fail_closed_v1",
        "interval": interval,
        "pair_plan_hashes": [plan.content_hash() for plan in pair_plans],
        "lock_evidence_hashes": [plan.lock_evidence_hash for plan in pair_plans],
        "status": "ALLOW",
    }
    batch = ExecutionPlanBatch(
        runtime_strategy_set_manifest_hash=manifest_hash,
        allocation_decision_hash=allocation_decision.content_hash(),
        pair_plans=tuple(pair_plans),
        batch_risk_decision_evidence=batch_risk_evidence,
        budget_lock_hash=sha256_prefixed(
            {
                "schema_version": 1,
                "lock_scope": "multi_pair_portfolio",
                "lock_evidence_hashes": [plan.lock_evidence_hash for plan in pair_plans],
            }
        ),
        status="planned",
    )
    plan_by_pair = {plan.pair: plan for plan in batch.pair_plans}
    shards: list[PairRuntimeShard] = []
    for item in inputs:
        target_payload = target_by_pair[item.pair]
        plan = plan_by_pair[item.pair]
        chain = ReplayHashChain(
            manifest_hash=manifest_hash,
            scope_key_hash=item.scope_key.scope_key_hash(),
            runtime_data_availability_hash=item.data_preflight_hash(),
            feature_snapshot_hash=item.feature_snapshot_hash(),
            runtime_decision_request_hash=sha256_prefixed(
                {
                    "pair": item.pair,
                    "interval": item.interval,
                    "scope_key_hash": item.scope_key.scope_key_hash(),
                    "decision_artifact_hash": item.decision_artifact_hash(),
                }
            ),
            allocation_input_hash=allocation_decision.allocation_input_hash,
            portfolio_target_hash=str(target_payload.get("final_portfolio_target_hash") or ""),
            execution_plan_batch_hash=batch.content_hash(),
            pair_execution_plan_hash=plan.content_hash(),
            execution_submit_plan_hash=plan.execution_submit_plan_hash,
            pre_submit_risk_decision_hash=(
                plan.pre_submit_risk_decision_hash
                or sha256_prefixed({"pre_submit_risk": "not_required", "pair": item.pair})
            ),
        )
        shards.append(
            PairRuntimeShard(
                pair=item.pair,
                interval=item.interval,
                scope_key=item.scope_key,
                data_preflight=item.data_preflight,
                selected_candle=item.selected_candle,
                feature_snapshot=item.feature_snapshot,
                decision_artifact=item.decision_artifact,
                allocation_target=target_payload,
                execution_pair_plan=plan,
                reconcile_status=item.reconcile_status,
                replay_hash_chain=chain,
                recovery_evidence=item.recovery_evidence,
            )
        )
    observability = build_operator_observability(
        authority=authority,
        shards=tuple(shards),
        execution_plan_batch=batch,
        fail_closed_reason="",
    )
    return MultiPairRuntimeResult(
        authority=authority,
        shards=tuple(shards),
        allocation_decision=allocation_decision,
        execution_plan_batch=batch,
        ledger_authority=ledger_authority,
        observability=observability,
    )


def validate_multi_pair_replay_boundaries(result: MultiPairRuntimeResult) -> dict[str, object]:
    failing: list[dict[str, object]] = []
    batch_hash = result.execution_plan_batch.content_hash()
    plan_hashes = {plan.content_hash() for plan in result.execution_plan_batch.pair_plans}
    for shard in result.shards:
        scope_status = validate_scope_key_hash(
            {
                "runtime_scope_key": shard.scope_key.as_dict(),
                "scope_key_hash": shard.scope_key.scope_key_hash(),
            }
        )
        if scope_status["status"] != "pass":
            failing.append({**scope_status, "pair": shard.pair})
        replay_payload = shard.replay_hash_chain.with_hash_payload()
        replay_status = validate_replay_hash_chain(replay_payload)
        if replay_status["status"] != "pass":
            failing.append({**replay_status, "pair": shard.pair})
        if shard.replay_hash_chain.execution_plan_batch_hash != batch_hash:
            failing.append(
                {
                    "status": "fail",
                    "layer": "execution_plan_batch",
                    "pair": shard.pair,
                    "mismatch_reason": "execution_plan_batch_hash_mismatch",
                }
            )
        if shard.execution_pair_plan.content_hash() not in plan_hashes:
            failing.append(
                {
                    "status": "fail",
                    "layer": "pair_execution_plan",
                    "pair": shard.pair,
                    "mismatch_reason": "pair_execution_plan_not_in_batch",
                }
            )
    return {
        "schema_version": 1,
        "status": "fail" if failing else "pass",
        "policy": MULTI_PAIR_REPLAY_POLICY,
        "failures": failing,
        "failing_layer": "" if not failing else str(failing[0].get("layer") or "replay_hash_chain"),
    }


def apply_pair_submit_result(
    shard: PairRuntimeShard,
    *,
    submit_status: str,
    broker_response: Mapping[str, object] | None = None,
    recovery_status: str = "reconcile_pending",
) -> PairRuntimeShard:
    response = dict(broker_response or {})
    recovery_evidence = {
        **dict(shard.recovery_evidence),
        "pair": shard.pair,
        "submit_status": submit_status,
        "broker_response_hash": sha256_prefixed(response),
        "recovery_status": recovery_status,
    }
    return PairRuntimeShard(
        pair=shard.pair,
        interval=shard.interval,
        scope_key=shard.scope_key,
        data_preflight=shard.data_preflight,
        selected_candle=shard.selected_candle,
        feature_snapshot=shard.feature_snapshot,
        decision_artifact=shard.decision_artifact,
        allocation_target=shard.allocation_target,
        execution_pair_plan=shard.execution_pair_plan,
        reconcile_status=recovery_status,
        replay_hash_chain=shard.replay_hash_chain,
        recovery_evidence=recovery_evidence,
        submit_status=submit_status,
        broker_response=response,
    )


def replay_pair_submit_status(shards: Sequence[PairRuntimeShard]) -> dict[str, object]:
    pair_status = {
        shard.pair: {
            "submit_status": shard.submit_status,
            "broker_response_hash": sha256_prefixed(dict(shard.broker_response)),
            "reconcile_status": shard.reconcile_status,
            "recovery_evidence_hash": sha256_prefixed(dict(shard.recovery_evidence)),
        }
        for shard in shards
    }
    failed = [pair for pair, status in pair_status.items() if str(status["submit_status"]) not in {"success", "not_submitted"}]
    succeeded = [pair for pair, status in pair_status.items() if str(status["submit_status"]) == "success"]
    return {
        "schema_version": 1,
        "pair_status": pair_status,
        "batch_status": "partial_failure" if failed and succeeded else "failed" if failed else "success_or_no_submit",
        "failed_pairs": failed,
        "succeeded_pairs": succeeded,
        "replayable": True,
    }


def build_operator_observability(
    *,
    authority: MultiPairRuntimeAuthority,
    shards: Sequence[PairRuntimeShard],
    execution_plan_batch: ExecutionPlanBatch | None,
    fail_closed_reason: str,
) -> dict[str, object]:
    batch_hash = "" if execution_plan_batch is None else execution_plan_batch.content_hash()
    batch_id = "" if execution_plan_batch is None else execution_plan_batch.batch_id
    pair_context = []
    for shard in shards:
        pair_context.append(
            {
                "pair": shard.pair,
                "strategy_instance_id": shard.scope_key.strategy_instance_id,
                "scope_key_hash": shard.scope_key.scope_key_hash(),
                "execution_plan_batch_hash": batch_hash,
                "execution_plan_batch_id": batch_id,
                "pair_execution_plan_hash": shard.execution_pair_plan.content_hash(),
                "lock_status": shard.execution_pair_plan.lock_status,
                "fail_closed_reason": fail_closed_reason,
                "replay_layer": "replay_hash_chain",
                "reconcile_status": shard.reconcile_status,
            }
        )
    payload = {
        "schema_version": 1,
        "runtime_scope_mode": MULTI_PAIR_SCOPE_MODE,
        "authority": authority.as_dict(),
        "execution_plan_batch_hash": batch_hash,
        "execution_plan_batch_id": batch_id,
        "fail_closed_reason": fail_closed_reason,
        "pairs": pair_context,
    }
    payload["observability_hash"] = sha256_prefixed(payload)
    return payload


def validate_execution_batch_for_runtime_scope(
    batch: ExecutionPlanBatch,
    *,
    multi_pair_enabled: bool,
    expected_pairs: Sequence[str],
) -> dict[str, object]:
    pairs = tuple(plan.pair for plan in batch.pair_plans)
    expected = tuple(sorted({_clean(pair) for pair in expected_pairs if _clean(pair)}))
    actual = tuple(sorted(pairs))
    if multi_pair_enabled:
        missing_fields: list[str] = []
        for plan in batch.pair_plans:
            if not plan.lock_evidence_hash:
                missing_fields.append(f"{plan.pair}:lock_evidence_hash")
            if not plan.order_rule_snapshot_hash:
                missing_fields.append(f"{plan.pair}:order_rule_snapshot_hash")
            if not plan.scope_key_hashes:
                missing_fields.append(f"{plan.pair}:scope_key_hashes")
        status = "pass" if actual == expected and len(batch.pair_plans) >= 2 and not missing_fields else "fail"
        reason = ""
        if actual != expected:
            reason = "multi_pair_batch_pair_set_mismatch"
        elif len(batch.pair_plans) < 2:
            reason = "multi_pair_batch_requires_multiple_pair_plans"
        elif missing_fields:
            reason = "multi_pair_batch_pair_plan_evidence_missing"
        return {
            "schema_version": 1,
            "status": status,
            "runtime_scope_mode": MULTI_PAIR_SCOPE_MODE,
            "pair_count": len(batch.pair_plans),
            "expected_pairs": list(expected),
            "actual_pairs": list(actual),
            "missing_fields": missing_fields,
            "reason": reason,
        }
    status = "pass" if len(batch.pair_plans) == 1 and actual == expected else "fail"
    return {
        "schema_version": 1,
        "status": status,
        "runtime_scope_mode": "single_pair",
        "pair_count": len(batch.pair_plans),
        "expected_pairs": list(expected),
        "actual_pairs": list(actual),
        "reason": "" if status == "pass" else "single_pair_batch_size_one_required",
    }
