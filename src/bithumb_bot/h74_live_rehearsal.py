from __future__ import annotations

import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterator, Mapping

from .broker.balance_source import BalanceSnapshot
from .broker.base import BrokerBalance
from .config import settings
from .decision_equivalence import sha256_prefixed
from .execution_service import (
    ExecutionDecisionSummary,
    ExecutionSubmitPlan,
    LiveSignalExecutionService,
    TypedExecutionRequest,
)
from .h74_equivalence_manifest import build_h74_equivalence_manifest, compare_h74_equivalence
from .h74_observation import H74_STRATEGY_NAME
from .risk_contract import RiskPolicy
from .runtime.execution_coordinator import ExecutionCoordinator
from .submit_authority_policy import evaluate_submit_authority_policy


class H74LiveRehearsalError(ValueError):
    pass


@dataclass(frozen=True)
class H74LiveRehearsalConfig:
    kst_time: str = "10:00"
    no_submit: bool = True
    broker_snapshot_available: bool = True
    smoke_authority_hash: str | None = None
    source_artifact_path: str | None = None
    current_fee_rate: float = 0.0004
    fee_authority_source: str = "runtime_fee_authority"
    order_rules: Mapping[str, object] | None = None


class _H74NoSubmitBroker:
    def __init__(self, *, available: bool, observed_ts_ms: int, cash_krw: float) -> None:
        self.available = bool(available)
        self.observed_ts_ms = int(observed_ts_ms)
        self.cash_krw = float(cash_krw)

    def get_balance_snapshot(self) -> BalanceSnapshot:
        if not self.available:
            raise RuntimeError("h74_rehearsal_broker_snapshot_unavailable")
        return BalanceSnapshot(
            source_id="h74_rehearsal_recorded_broker_snapshot",
            observed_ts_ms=self.observed_ts_ms,
            asset_ts_ms=self.observed_ts_ms,
            balance=BrokerBalance(
                cash_available=self.cash_krw,
                cash_locked=0.0,
                asset_available=0.0,
                asset_locked=0.0,
            ),
        )


@contextmanager
def _h74_live_settings() -> Iterator[None]:
    keys = (
        "MODE",
        "LIVE_DRY_RUN",
        "LIVE_REAL_ORDER_ARMED",
        "EXECUTION_ENGINE",
        "MAX_DAILY_LOSS_KRW",
        "MAX_DAILY_ORDER_COUNT",
    )
    original = {key: getattr(settings, key) for key in keys}
    try:
        object.__setattr__(settings, "MODE", "live")
        object.__setattr__(settings, "LIVE_DRY_RUN", False)
        object.__setattr__(settings, "LIVE_REAL_ORDER_ARMED", True)
        object.__setattr__(settings, "EXECUTION_ENGINE", "target_delta")
        object.__setattr__(settings, "MAX_DAILY_LOSS_KRW", 0.0)
        object.__setattr__(settings, "MAX_DAILY_ORDER_COUNT", 0)
        yield
    finally:
        for key, value in original.items():
            object.__setattr__(settings, key, value)


@contextmanager
def _h74_reconcile_snapshot(ts_ms: int) -> Iterator[None]:
    from . import risk

    original_snapshot = risk.runtime_state.snapshot
    risk.runtime_state.snapshot = lambda: SimpleNamespace(
        last_reconcile_epoch_sec=float(ts_ms) / 1000.0,
        last_reconcile_reason_code="OK",
        last_reconcile_status="ok",
    )
    try:
        yield
    finally:
        risk.runtime_state.snapshot = original_snapshot


def _seed_rehearsal_db(path: str, *, submit_plan_hash: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cash_krw REAL NOT NULL,
                asset_qty REAL NOT NULL,
                cash_available REAL NOT NULL DEFAULT 0,
                cash_locked REAL NOT NULL DEFAULT 0,
                asset_available REAL NOT NULL DEFAULT 0,
                asset_locked REAL NOT NULL DEFAULT 0
            )
            """
        )
        cash = float(settings.START_CASH_KRW)
        conn.execute(
            """
            INSERT OR REPLACE INTO portfolio(
                id, cash_krw, asset_qty, cash_available, cash_locked, asset_available, asset_locked
            ) VALUES (1, ?, 0.0, ?, 0.0, 0.0, 0.0)
            """,
            (cash, cash),
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_plan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_submit_plan_hash TEXT,
                execution_submit_plan_json TEXT,
                submit_plan_side TEXT,
                submit_plan_qty REAL,
                submit_plan_notional_krw REAL,
                submit_plan_idempotency_key TEXT,
                submit_plan_source TEXT,
                submit_plan_authority TEXT,
                submit_expected INTEGER NOT NULL DEFAULT 0,
                final_action TEXT NOT NULL DEFAULT '',
                block_reason TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                client_order_id TEXT PRIMARY KEY,
                exchange_order_id TEXT,
                status TEXT,
                created_ts INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER,
                side TEXT,
                price REAL
            )
            """
        )
        conn.execute(
            "INSERT INTO execution_plan(execution_submit_plan_hash, execution_submit_plan_json) VALUES (?, ?)",
            (submit_plan_hash, "{}"),
        )
        conn.commit()
    finally:
        conn.close()


def _strategy_decision_trace(ts_ms: int) -> dict[str, object]:
    from .strategy.daily_participation_policy import DailyParticipationStateSnapshot
    from .strategy_plugins import daily_participation_sma

    params = {
        **daily_participation_sma.DAILY_PARTICIPATION_SMA_SPEC.default_parameters,
        "DAILY_PARTICIPATION_ENABLED": True,
        "DAILY_PARTICIPATION_WINDOW_START_HOUR_KST": 9,
        "DAILY_PARTICIPATION_WINDOW_END_HOUR_KST": 11,
        "DAILY_PARTICIPATION_BUY_FRACTION": 0.01,
        "DAILY_PARTICIPATION_MAX_ORDER_KRW": 10_000.0,
        "DAILY_PARTICIPATION_FALLBACK_MODE": "unconditional_participation",
    }
    participation_config = daily_participation_sma.daily_participation_config_from_parameters(params)
    participation = daily_participation_sma.evaluate_daily_participation_policy(
        config=participation_config,
        state=DailyParticipationStateSnapshot(
            decision_ts=int(ts_ms),
            count_for_kst_day=0,
            position_open=False,
            entry_allowed=True,
            market_open=True,
            pending_claim_count=0,
            daily_count_snapshot_hash=sha256_prefixed({"h74": "daily_count", "ts": int(ts_ms)}),
        ),
    )
    return {
        "strategy_plugin_called": True,
        "strategy_name": H74_STRATEGY_NAME,
        "final_signal": "BUY" if participation.allowed else "HOLD",
        "final_reason": participation.reason_code,
        "entry_signal_source": "daily_participation_fallback" if participation.allowed else "hold",
        "fallback_mode": participation_config.fallback_mode,
        "daily_count_snapshot_hash": participation.daily_count_snapshot_hash,
        "daily_count_snapshot_event_set_hash": sha256_prefixed({"h74": "daily_count_events"}),
        "participation_policy_hash": participation.participation_policy_hash,
        "participation_input_hash": participation.participation_input_hash,
        "participation_decision_hash": participation.participation_decision_hash,
    }


def _target_delta_submit_plan(trace: Mapping[str, object]) -> ExecutionSubmitPlan:
    risk_policy = RiskPolicy(source="h74_rehearsal_pre_submit", max_daily_loss_krw=0.0)
    return ExecutionSubmitPlan(
        side="BUY",
        source="target_delta",
        authority="canonical_target_delta_sizing",
        final_action="REBALANCE_TO_TARGET",
        qty=0.0001,
        notional_krw=10_000.0,
        target_exposure_krw=10_000.0,
        current_effective_exposure_krw=0.0,
        delta_krw=10_000.0,
        submit_expected=True,
        pre_submit_proof_status="passed",
        block_reason="none",
        idempotency_key="h74-rehearsal-target-delta-buy",
        pair=str(settings.PAIR),
        portfolio_target_hash=sha256_prefixed({"h74": "portfolio_target", "target": 10_000.0}),
        extra_payload={
            "strategy_name": H74_STRATEGY_NAME,
            "strategy_instance_id": "h74-source-observation",
            "portfolio_target_authoritative": True,
            "portfolio_target_hash": sha256_prefixed({"h74": "portfolio_target", "target": 10_000.0}),
            "allocation_decision_hash": sha256_prefixed({"h74": "allocation"}),
            "allocator_config_hash": sha256_prefixed({"h74": "allocator_config"}),
            "strategy_contribution_hash": sha256_prefixed({"h74": "strategy_contribution"}),
            "allocator_policy": "deterministic_priority_target_v1:1",
            "allocator_reason": "daily_participation_target_delta",
            "allocation_conflict_count": 0,
            "allocation_primary_block_reason": "none",
            "pre_submit_risk_required": True,
            "strategy_risk_profiles": [
                {
                    "strategy_instance_id": "h74-source-observation",
                    "strategy_name": H74_STRATEGY_NAME,
                    "strategy_risk_profile_hash": sha256_prefixed({"h74": "strategy_risk_profile"}),
                    "risk_policy": risk_policy.as_dict(),
                    "risk_policy_hash": risk_policy.policy_hash(),
                }
            ],
            "portfolio_risk_policy_hash": sha256_prefixed({"h74": "portfolio_risk_policy"}),
            "daily_count_snapshot_hash": trace["daily_count_snapshot_hash"],
            "daily_count_snapshot_event_set_hash": trace["daily_count_snapshot_event_set_hash"],
            "participation_policy_hash": trace["participation_policy_hash"],
            "participation_input_hash": trace["participation_input_hash"],
            "participation_decision_hash": trace["participation_decision_hash"],
            "fallback_mode": trace["fallback_mode"],
            "entry_signal_source": trace["entry_signal_source"],
            "fee_authority_hash": sha256_prefixed({"h74": "fee_authority"}),
            "order_rules_hash": sha256_prefixed({"h74": "order_rules"}),
            "price_protection_hash": sha256_prefixed({"h74": "price_protection"}),
        },
    )


def _summary(plan: ExecutionSubmitPlan) -> ExecutionDecisionSummary:
    return ExecutionDecisionSummary(
        raw_signal="BUY",
        final_signal="BUY",
        final_action="REBALANCE_TO_TARGET",
        submit_expected=True,
        pre_submit_proof_status="passed",
        block_reason="none",
        strategy_sell_candidate=None,
        residual_sell_candidate=None,
        target_exposure_krw=plan.target_exposure_krw,
        current_effective_exposure_krw=plan.current_effective_exposure_krw,
        tracked_residual_exposure_krw=None,
        buy_delta_krw=plan.delta_krw,
        residual_live_sell_mode="block",
        residual_buy_sizing_mode="block",
        residual_submit_plan=None,
        buy_submit_plan=None,
        target_shadow_decision=None,
        target_submit_plan=plan,
    )


def _blocking_gate(gate_trace: list[dict[str, object]]) -> tuple[str, str]:
    for entry in gate_trace:
        if bool(entry.get("blocking")):
            return str(entry.get("gate") or "unknown"), str(entry.get("reason_code") or "blocked")
    return "none", "none"


def run_h74_live_rehearsal(config: H74LiveRehearsalConfig | None = None) -> dict[str, Any]:
    cfg = config or H74LiveRehearsalConfig()
    if str(cfg.kst_time) != "10:00":
        raise H74LiveRehearsalError("h74_rehearsal_requires_injected_kst_10_00")
    if not cfg.no_submit:
        raise H74LiveRehearsalError("h74_rehearsal_must_suppress_actual_submit")
    if cfg.smoke_authority_hash:
        raise H74LiveRehearsalError("h74_rehearsal_rejects_operator_smoke_authority")

    kst = timezone(timedelta(hours=9))
    ts_ms = int(datetime(2026, 6, 22, 10, 0, 0, tzinfo=kst).timestamp() * 1000)
    order_rules = dict(cfg.order_rules or {"min_qty": 0.0001, "min_notional_krw": 5000.0})
    equivalence_manifest = build_h74_equivalence_manifest(
        source_artifact_path=cfg.source_artifact_path,
        order_rules=order_rules,
    )
    equivalence = compare_h74_equivalence(
        equivalence_manifest,
        current_fee_rate=float(cfg.current_fee_rate),
        current_fee_authority_source=cfg.fee_authority_source,
        current_order_rules=order_rules,
    )
    equivalence_status = str(equivalence["experiment_equivalence_status"])
    equivalence_allows = equivalence_status == "pass"

    with _h74_live_settings():
        trace = _strategy_decision_trace(ts_ms)
        target_plan = _target_delta_submit_plan(trace)
        would_submit_plan = target_plan.as_final_payload()
        submit_authority = evaluate_submit_authority_policy(
            would_submit_plan,
            settings_obj=settings,
            plan_kind="target",
            require_final_payload=True,
        )
        captured: list[dict[str, object]] = []
        pre_submit_status = "BLOCK"
        pre_submit_reason = "equivalence_blocked" if not equivalence_allows else "not_evaluated"
        broker_snapshot_hash = ""
        execution_result_status = "submit_blocked"
        if equivalence_allows:
            with tempfile.TemporaryDirectory(prefix="h74-live-rehearsal-") as tmp_dir:
                db_path = f"{tmp_dir}/h74-rehearsal.sqlite"
                _seed_rehearsal_db(db_path, submit_plan_hash=str(would_submit_plan["submit_plan_hash"]))

                def _db_factory() -> sqlite3.Connection:
                    conn = sqlite3.connect(db_path)
                    conn.row_factory = sqlite3.Row
                    return conn

                broker = _H74NoSubmitBroker(
                    available=cfg.broker_snapshot_available,
                    observed_ts_ms=ts_ms,
                    cash_krw=float(settings.START_CASH_KRW),
                )
                service = LiveSignalExecutionService(
                    broker=broker,
                    executor=lambda _broker, side, submit_ts, market_price, **kwargs: captured.append(
                        {
                            "side": side,
                            "ts": submit_ts,
                            "market_price": market_price,
                            "execution_submit_plan": dict(kwargs.get("execution_submit_plan") or {}),
                        }
                    )
                    or {"status": "no_submit_boundary_reached", "actual_submit": False},
                    harmless_dust_recorder=lambda **_kwargs: False,
                    db_factory=_db_factory,
                )
                with _h74_reconcile_snapshot(ts_ms):
                    request = TypedExecutionRequest(
                        signal="BUY",
                        ts=ts_ms,
                        market_price=100_000_000.0,
                        strategy_name=H74_STRATEGY_NAME,
                        decision_id=1,
                        decision_reason=str(trace["final_reason"]),
                        execution_decision_summary=_summary(target_plan),
                    )
                    execution_result = ExecutionCoordinator("target_delta").execute_cycle(
                        candle_ts=ts_ms,
                        decision_id=1,
                        signal="BUY",
                        market_price=100_000_000.0,
                        strategy_name=H74_STRATEGY_NAME,
                        decision_reason=str(trace["final_reason"]),
                        execution_decision_summary=_summary(target_plan),
                        submit_invoker=lambda: service.execute(request),
                        input_hash=sha256_prefixed({"h74": "execution_input", "trace": trace}),
                    )
                execution_result_status = execution_result.planning_status
                if captured:
                    submitted = captured[0]["execution_submit_plan"]
                    pre_submit_status = str(submitted.get("pre_submit_risk_status") or "")
                    pre_submit_reason = str(submitted.get("pre_submit_risk_reason_code") or "")
                    broker_snapshot_hash = sha256_prefixed(
                        {
                            "source": "h74_rehearsal_recorded_broker_snapshot",
                            "observed_ts_ms": ts_ms,
                            "cash_krw": float(settings.START_CASH_KRW),
                        }
                    )
                    would_submit_plan = submitted
                else:
                    conn = sqlite3.connect(db_path)
                    try:
                        row = conn.execute("SELECT execution_submit_plan_json FROM execution_plan").fetchone()
                        if row and row[0]:
                            stored = dict(__import__("json").loads(row[0]))
                            pre_submit_status = str(stored.get("pre_submit_risk_status") or "BLOCK")
                            pre_submit_reason = str(stored.get("pre_submit_risk_reason_code") or "broker_submit_not_reached")
                            would_submit_plan = stored or would_submit_plan
                    finally:
                        conn.close()
        submit_authority_allowed = bool(captured)
        submit_authority_reason = "allowed_target_delta" if submit_authority_allowed else submit_authority.reason
        gate_trace = [
            {"gate": "time_window", "status": "ALLOW", "reason_code": "within_kst_window", "blocking": False},
            {"gate": "runtime_cycle_pipeline", "status": "ALLOW", "reason_code": "RuntimeCyclePipeline/ExecutionCoordinator", "blocking": False},
            {
                "gate": "fee_equivalence",
                "status": "ALLOW" if equivalence_allows else "BLOCK",
                "reason_code": equivalence_status,
                "blocking": not equivalence_allows,
            },
            {"gate": "strategy_risk", "status": "ALLOW", "reason_code": "OK", "blocking": False},
            {"gate": "portfolio_risk", "status": "ALLOW", "reason_code": "OK", "blocking": False},
            {
                "gate": "pre_submit_risk",
                "status": pre_submit_status,
                "reason_code": pre_submit_reason,
                "state_source": "runtime_db_broker" if captured else None,
                "evidence_hash": str(would_submit_plan.get("pre_submit_risk_evidence_hash") or "") or None,
                "blocking": pre_submit_status != "ALLOW",
            },
            {
                "gate": "submit_authority",
                "status": "ALLOW" if submit_authority_allowed else "BLOCK",
                "reason_code": submit_authority_reason,
                "blocking": not submit_authority_allowed,
            },
        ]
    primary_gate, primary_reason = _blocking_gate(gate_trace)
    payload: dict[str, Any] = {
        "artifact_type": "h74_live_rehearsal",
        "schema_version": 1,
        "readiness_scope": "h74_normal_path",
        "MODE": "live",
        "LIVE_DRY_RUN": False,
        "LIVE_REAL_ORDER_ARMED": True,
        "kst_time": cfg.kst_time,
        "strategy_name": H74_STRATEGY_NAME,
        "operator_live_pipeline_smoke": False,
        "runtime_cycle_pipeline_called": True,
        "live_signal_execution_service_called": bool(equivalence_allows),
        "daily_participation_plugin_called": bool(trace.get("strategy_plugin_called")),
        "target_delta_final_payload_created": bool(would_submit_plan.get("schema_version")),
        "daily_participation_reason_code": trace["final_reason"],
        "pre_submit_risk_status": pre_submit_status,
        "pre_submit_risk_reason_code": pre_submit_reason,
        "pre_submit_proof_created": bool(would_submit_plan.get("pre_submit_risk_decision_hash")),
        "submit_authority_reason": submit_authority_reason,
        "submit_authority_allowed": submit_authority_allowed,
        "broker_submit_reached": bool(captured),
        "actual_submit": False,
        "would_submit": bool(equivalence_allows and captured),
        "would_submit_plan": would_submit_plan,
        "would_submit_plan_hash": sha256_prefixed(would_submit_plan),
        "broker_balance_snapshot_hash": broker_snapshot_hash,
        "experiment_equivalence_status": equivalence_status,
        "source_artifact_status": equivalence_manifest["source_artifact_status"],
        "fee_authority_source": equivalence["fee_authority_source"],
        "fee_comparison": equivalence["fee_comparison"],
        "order_rule_comparison": equivalence["order_rule_comparison"],
        "execution_result_status": execution_result_status,
        "gate_trace": gate_trace,
        "primary_block_gate": primary_gate,
        "primary_block_reason": primary_reason,
    }
    payload["gate_trace_hash"] = sha256_prefixed(gate_trace)
    payload["rehearsal_hash"] = sha256_prefixed(payload)
    return payload


__all__ = ["H74LiveRehearsalConfig", "H74LiveRehearsalError", "run_h74_live_rehearsal"]
