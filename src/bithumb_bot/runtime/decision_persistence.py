from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
import logging
import os
import sqlite3
import threading
import time

from ..config import settings
from ..db_core import (
    create_or_get_budget_lock,
    create_or_get_order_lock,
    record_execution_plan,
    record_execution_plan_batch,
    record_portfolio_allocation_decision,
    record_runtime_strategy_decision_bundle,
    record_strategy_decision,
    upsert_strategy_virtual_target_state,
    upsert_target_position_state,
)
from ..observability import format_log_kv
from ..sqlite_resilience import is_lock_error
from ..target_position import (
    ACTUAL_PAIR_TARGET_SOURCE,
    ACTUAL_PAIR_TARGET_SOURCE_PROVENANCE_INCOMPLETE,
)
from ..virtual_target_state import StrategyVirtualTargetState

RUN_LOG = logging.getLogger("bithumb_bot.run")


@dataclass(frozen=True)
class DecisionPersistenceError(RuntimeError):
    reason: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True)
class DecisionPersistenceResult:
    context: dict[str, object]
    decision_id: int
    retry_count: int = 0
    max_retry_count: int = 0
    transaction_elapsed_ms: float = 0.0
    lock_wait_elapsed_ms: float = 0.0
    db_connection_id: int | None = None
    pid: int | None = None
    thread_id: int | None = None

    def metadata(self) -> dict[str, object]:
        return {
            "retry_count": int(self.retry_count),
            "max_retry_count": int(self.max_retry_count),
            "transaction_elapsed_ms": float(self.transaction_elapsed_ms),
            "lock_wait_elapsed_ms": float(self.lock_wait_elapsed_ms),
            "db_connection_id": self.db_connection_id,
            "pid": self.pid,
            "thread_id": self.thread_id,
        }


@dataclass
class DecisionPersistenceUnitOfWork:
    record_runtime_strategy_decision_bundle_fn: Callable[..., dict[str, object]] = record_runtime_strategy_decision_bundle
    record_portfolio_allocation_decision_fn: Callable[..., dict[str, object]] = record_portfolio_allocation_decision
    record_execution_plan_batch_fn: Callable[..., dict[str, object]] = record_execution_plan_batch
    record_execution_plan_fn: Callable[..., dict[str, object]] = record_execution_plan
    record_strategy_decision_fn: Callable[..., int] = record_strategy_decision
    target_state_persister: Callable[..., None] = upsert_target_position_state
    virtual_target_state_persister: Callable[..., None] = upsert_strategy_virtual_target_state
    budget_lock_persister: Callable[..., dict[str, object]] = create_or_get_budget_lock
    order_lock_persister: Callable[..., dict[str, object]] = create_or_get_order_lock
    retry_count: int | None = None
    retry_backoff_ms: int | None = None

    def persist(
        self,
        conn: sqlite3.Connection,
        *,
        typed_bundle: object,
        planning_bundle: object,
        context: dict[str, object],
        strategy_name: str,
        signal: str,
        reason: str,
        updated_ts: int,
        settings_obj: object,
        run_start_manifest_payload: dict[str, object] | None = None,
        run_start_manifest_id: int | None = None,
        run_start_manifest_hash: str | None = None,
    ) -> DecisionPersistenceResult:
        attempts = max(0, int(settings.DB_LOCK_RETRY_COUNT if self.retry_count is None else self.retry_count))
        sleep_ms = max(0, int(settings.DB_LOCK_RETRY_BACKOFF_MS if self.retry_backoff_ms is None else self.retry_backoff_ms))
        last_lock_error = ""
        total_lock_wait_ms = 0.0
        started_all = time.monotonic()
        for attempt in range(attempts + 1):
            try:
                result = self._persist_once(
                    conn,
                    typed_bundle=typed_bundle,
                    planning_bundle=planning_bundle,
                    context=dict(context),
                    strategy_name=strategy_name,
                    signal=signal,
                    reason=reason,
                    updated_ts=int(updated_ts),
                    settings_obj=settings_obj,
                    run_start_manifest_payload=run_start_manifest_payload,
                    run_start_manifest_id=run_start_manifest_id,
                    run_start_manifest_hash=run_start_manifest_hash,
                    retry_count=attempt,
                    max_retry_count=attempts,
                    lock_wait_elapsed_ms=total_lock_wait_ms,
                    transaction_started_at=started_all,
                )
                return result
            except Exception as exc:
                if not is_lock_error(exc):
                    raise
                last_lock_error = f"{type(exc).__name__}: {exc}"
                metadata = _lock_metadata(
                    conn,
                    retry_count=attempt,
                    max_retry_count=attempts,
                    last_lock_error=last_lock_error,
                    db_subphase=_db_subphase_from_exception(exc),
                    sql_group=_sql_group_from_exception(exc),
                    transaction_started_at=started_all,
                    lock_wait_elapsed_ms=total_lock_wait_ms,
                )
                RUN_LOG.warning(
                    format_log_kv(
                        "[WARN] sqlite decision persistence lock",
                        db_subphase=metadata["db_subphase"],
                        sql_group=metadata["sql_group"],
                        retry_count=metadata["retry_count"],
                        max_retry_count=metadata["max_retry_count"],
                    )
                )
                if attempt >= attempts:
                    raise DecisionPersistenceError(
                        "decision_persistence_sqlite_lock_exhausted",
                        metadata,
                    ) from exc
                if sleep_ms > 0:
                    sleep_s = (sleep_ms / 1000.0) * (attempt + 1)
                    time.sleep(sleep_s)
                    total_lock_wait_ms += sleep_s * 1000.0
        raise RuntimeError("unreachable")

    def _persist_once(
        self,
        conn: sqlite3.Connection,
        *,
        typed_bundle: object,
        planning_bundle: object,
        context: dict[str, object],
        strategy_name: str,
        signal: str,
        reason: str,
        updated_ts: int,
        settings_obj: object,
        run_start_manifest_payload: dict[str, object] | None,
        run_start_manifest_id: int | None,
        run_start_manifest_hash: str | None,
        retry_count: int,
        max_retry_count: int,
        lock_wait_elapsed_ms: float,
        transaction_started_at: float,
    ) -> DecisionPersistenceResult:
        db_subphase = "begin_immediate"
        sql_group = "decision_persistence_transaction"
        sqlite_connection = callable(getattr(conn, "execute", None))
        try:
            if sqlite_connection:
                conn.rollback()
        except Exception:
            pass
        try:
            if sqlite_connection:
                conn.execute("BEGIN IMMEDIATE")
            db_subphase = "runtime_strategy_bundle"
            sql_group = "runtime_strategy_bundle_insert"
            bundle_refs = self.record_runtime_strategy_decision_bundle_fn(
                conn,
                result_bundle=typed_bundle,
                pair=str(typed_bundle.strategy_set.market_scope.pair),
                interval=str(typed_bundle.strategy_set.market_scope.interval),
                created_ts=updated_ts,
                settings_obj=settings_obj,
                manifest_payload=run_start_manifest_payload,
                runtime_strategy_set_manifest_id=run_start_manifest_id,
                runtime_strategy_set_manifest_hash=run_start_manifest_hash,
            )
            context.update(bundle_refs)
            allocation_payload = context.get("portfolio_allocation_decision")
            if not isinstance(allocation_payload, dict):
                planning_error = str(getattr(planning_bundle, "planning_error", "") or "").strip()
                if planning_error:
                    raise RuntimeError(f"planning_failed_before_allocation_payload:{planning_error}")
                raise RuntimeError("portfolio_allocation_decision_missing_after_successful_planning")

            db_subphase = "portfolio_allocation"
            sql_group = "portfolio_allocation_insert"
            allocation_refs = self.record_portfolio_allocation_decision_fn(
                conn,
                bundle_id=int(bundle_refs["runtime_strategy_decision_bundle_id"]),
                allocation_decision=allocation_payload,
            )
            context.update(allocation_refs)

            execution_plan_batch = getattr(planning_bundle, "execution_plan_batch", None)
            if execution_plan_batch is None:
                raise RuntimeError("execution_plan_batch_missing")
            db_subphase = "execution_plan_batch"
            sql_group = "execution_plan_batch_insert"
            batch_refs = self.record_execution_plan_batch_fn(
                conn,
                execution_plan_batch=execution_plan_batch,
                created_ts=updated_ts,
            )
            context.update(batch_refs)

            db_subphase = "execution_plan"
            sql_group = "execution_plan_insert"
            execution_refs = self.record_execution_plan_fn(
                conn,
                allocation_id=int(allocation_refs["portfolio_allocation_decision_id"]),
                portfolio_target_hash=str(allocation_refs.get("portfolio_target_hash") or ""),
                execution_plan_bundle=planning_bundle,
            )
            context.update(execution_refs)
            context["runtime_strategy_decision_bundle_hash"] = bundle_refs[
                "runtime_strategy_decision_bundle_hash"
            ]
            context["portfolio_allocation_decision_hash"] = allocation_refs[
                "allocation_decision_hash"
            ]
            context["execution_submit_plan_hash"] = execution_refs["execution_submit_plan_hash"]
            context["execution_plan_batch_hash"] = batch_refs["execution_plan_batch_hash"]
            context["execution_plan_batch_id"] = batch_refs["execution_plan_batch_id"]

            db_subphase = "strategy_decision"
            sql_group = "strategy_decision_insert"
            candle_ts_raw = context.get("ts")
            market_price_raw = context.get("last_close")
            confidence_raw = context.get("confidence")
            decision_id = self.record_strategy_decision_fn(
                conn,
                decision_ts=updated_ts,
                strategy_name=strategy_name,
                signal=signal,
                reason=reason,
                candle_ts=int(candle_ts_raw) if candle_ts_raw is not None else None,
                market_price=float(market_price_raw) if market_price_raw is not None else None,
                confidence=float(confidence_raw) if confidence_raw is not None else None,
                context=context,
                runtime_strategy_decision_bundle_id=bundle_refs.get("runtime_strategy_decision_bundle_id"),
                portfolio_allocation_decision_id=allocation_refs.get("portfolio_allocation_decision_id"),
                portfolio_target_id=allocation_refs.get("portfolio_target_id"),
                execution_plan_id=execution_refs.get("execution_plan_id"),
                strategy_decision_projection_type=context.get("strategy_decision_projection_type"),
                strategy_decisions_authority=context.get("strategy_decisions_authority"),
            )
            context["decision_id"] = decision_id

            db_subphase = "target_state"
            sql_group = "target_state_upsert"
            self._persist_target_state_intents(
                conn,
                context=context,
                signal=signal,
                decision_id=decision_id,
                updated_ts=updated_ts,
                settings_obj=settings_obj,
            )

            db_subphase = "virtual_target_state"
            sql_group = "virtual_target_state_upsert"
            self._persist_virtual_target_state_intents(conn, context=context)

            db_subphase = "lock_intent"
            sql_group = "budget_or_order_lock_insert"
            self._persist_lock_intents(conn, context=context)

            if sqlite_connection:
                conn.commit()
            elapsed_ms = max(0.0, (time.monotonic() - transaction_started_at) * 1000.0)
            context.update(
                {
                    "decision_persistence_retry_count": retry_count,
                    "decision_persistence_max_retry_count": max_retry_count,
                    "decision_persistence_transaction_elapsed_ms": elapsed_ms,
                    "decision_persistence_lock_wait_elapsed_ms": lock_wait_elapsed_ms,
                }
            )
            return DecisionPersistenceResult(
                context=context,
                decision_id=decision_id,
                retry_count=retry_count,
                max_retry_count=max_retry_count,
                transaction_elapsed_ms=elapsed_ms,
                lock_wait_elapsed_ms=lock_wait_elapsed_ms,
                db_connection_id=id(conn),
                pid=os.getpid(),
                thread_id=threading.get_ident(),
            )
        except Exception as exc:
            try:
                if sqlite_connection:
                    conn.rollback()
            except Exception:
                pass
            setattr(exc, "db_subphase", db_subphase)
            setattr(exc, "sql_group", sql_group)
            raise

    def _persist_target_state_intents(
        self,
        conn: sqlite3.Connection,
        *,
        context: Mapping[str, object],
        signal: str,
        decision_id: int,
        updated_ts: int,
        settings_obj: object,
    ) -> None:
        intents: list[Mapping[str, object]] = []
        target_policy_intent = context.get("target_state_update_intent")
        if isinstance(target_policy_intent, Mapping):
            intents.append(target_policy_intent)
        execution_decision = context.get("execution_decision")
        target_decision = (
            execution_decision.get("target_shadow_decision")
            if isinstance(execution_decision, Mapping)
            and isinstance(execution_decision.get("target_shadow_decision"), Mapping)
            else None
        )
        if isinstance(target_decision, Mapping) and all(
            target_decision.get(key) is not None
            for key in ("target_new_exposure_krw", "target_qty", "target_reference_price")
        ):
            provenance = dict(context)
            required_provenance = {
                "runtime_strategy_set_manifest_hash": provenance.get("runtime_strategy_set_manifest_hash"),
                "runtime_strategy_decision_bundle_hash": provenance.get("runtime_strategy_decision_bundle_hash"),
                "portfolio_allocation_decision_hash": (
                    provenance.get("portfolio_allocation_decision_hash")
                    or provenance.get("allocation_decision_hash")
                ),
                "portfolio_target_hash": provenance.get("portfolio_target_hash"),
                "execution_plan_batch_hash": provenance.get("execution_plan_batch_hash"),
                "execution_submit_plan_hash": provenance.get("execution_submit_plan_hash"),
            }
            missing = [key for key, value in required_provenance.items() if not str(value or "").strip()]
            if missing:
                raise RuntimeError(
                    "actual_pair_target_allocator_provenance_incomplete:"
                    + ",".join(sorted(missing))
                )
            intents.append(
                {
                    "pair": str(context.get("runtime_pair") or getattr(settings_obj, "PAIR")),
                    "target_exposure_krw": float(target_decision["target_new_exposure_krw"] or 0.0),
                    "target_qty": float(target_decision["target_qty"] or 0.0),
                    "last_signal": signal,
                    "last_decision_id": decision_id,
                    "last_reference_price": float(target_decision["target_reference_price"] or 0.0),
                    "updated_ts": updated_ts,
                    "target_origin": str(target_decision.get("target_origin") or ""),
                    "adoption_reason": str(target_decision.get("target_adoption_reason") or ""),
                    "adopted_broker_qty": target_decision.get("target_adopted_broker_qty"),
                    "adopted_broker_exposure_krw": target_decision.get("target_adopted_exposure_krw"),
                    "created_from_signal": str(target_decision.get("target_strategy_signal_source") or signal),
                    **required_provenance,
                    "actual_target_source": ACTUAL_PAIR_TARGET_SOURCE,
                }
            )
        for intent in intents:
            required = {
                "runtime_strategy_set_manifest_hash": intent.get("runtime_strategy_set_manifest_hash", ""),
                "runtime_strategy_decision_bundle_hash": intent.get("runtime_strategy_decision_bundle_hash", ""),
                "portfolio_allocation_decision_hash": intent.get("portfolio_allocation_decision_hash", ""),
                "portfolio_target_hash": intent.get("portfolio_target_hash", ""),
                "execution_plan_batch_hash": intent.get("execution_plan_batch_hash", ""),
                "execution_submit_plan_hash": intent.get("execution_submit_plan_hash", ""),
            }
            missing = [key for key, value in required.items() if not str(value or "").strip()]
            actual_target_source = (
                ACTUAL_PAIR_TARGET_SOURCE_PROVENANCE_INCOMPLETE
                if missing
                else str(intent.get("actual_target_source") or ACTUAL_PAIR_TARGET_SOURCE)
            )
            self.target_state_persister(
                conn,
                pair=str(intent["pair"]),
                target_exposure_krw=float(intent.get("target_exposure_krw") or 0.0),
                target_qty=float(intent.get("target_qty") or 0.0),
                last_signal=str(intent.get("last_signal") or signal).upper(),
                last_decision_id=(
                    decision_id
                    if intent.get("last_decision_id") is None
                    else int(intent.get("last_decision_id"))  # type: ignore[arg-type]
                ),
                last_reference_price=float(intent.get("last_reference_price") or 0.0),
                updated_ts=int(intent.get("updated_ts") or updated_ts),
                target_origin=str(intent.get("target_origin") or ""),
                adoption_reason=str(intent.get("adoption_reason") or ""),
                adopted_broker_qty=(
                    None if intent.get("adopted_broker_qty") is None else float(intent.get("adopted_broker_qty") or 0.0)
                ),
                adopted_broker_exposure_krw=(
                    None
                    if intent.get("adopted_broker_exposure_krw") is None
                    else float(intent.get("adopted_broker_exposure_krw") or 0.0)
                ),
                created_from_signal=str(intent.get("created_from_signal") or signal),
                runtime_strategy_set_manifest_hash=str(required["runtime_strategy_set_manifest_hash"] or ""),
                runtime_strategy_decision_bundle_hash=str(required["runtime_strategy_decision_bundle_hash"] or ""),
                portfolio_allocation_decision_hash=str(required["portfolio_allocation_decision_hash"] or ""),
                portfolio_target_hash=str(required["portfolio_target_hash"] or ""),
                execution_plan_batch_hash=str(required["execution_plan_batch_hash"] or ""),
                execution_submit_plan_hash=str(required["execution_submit_plan_hash"] or ""),
                actual_target_source=actual_target_source,
            )

    def _persist_virtual_target_state_intents(
        self,
        conn: sqlite3.Connection,
        *,
        context: Mapping[str, object],
    ) -> None:
        for payload in context.get("virtual_target_state_update_intents") or []:
            if not isinstance(payload, Mapping):
                continue
            risk_status = str(
                payload.get("strategy_risk_status")
                or payload.get("risk_status")
                or ""
            ).strip().upper()
            submit_expected = payload.get("submit_expected")
            lifecycle_state = str(payload.get("lifecycle_state") or "").strip()
            last_signal = str(payload.get("last_signal") or "").strip().upper()
            if (
                risk_status == "BLOCK"
                and submit_expected is False
                and last_signal == "BUY"
                and lifecycle_state == "virtual_open"
            ):
                RUN_LOG.warning(
                    "event=virtual_target_state_suppressed reason=risk_blocked_buy_virtual_open"
                )
                continue
            state = StrategyVirtualTargetState(
                strategy_instance_id=str(payload["strategy_instance_id"]),
                strategy_name=str(payload.get("strategy_name") or ""),
                pair=str(payload["pair"]),
                interval=str(payload["interval"]),
                scope_key_hash=str(payload["scope_key_hash"]),
                runtime_contract_hash=str(payload["runtime_contract_hash"]),
                virtual_target_exposure_krw=float(payload["virtual_target_exposure_krw"]),
                virtual_target_qty=(
                    None
                    if payload.get("virtual_target_qty") is None
                    else float(payload["virtual_target_qty"])
                ),
                lifecycle_state=str(payload["lifecycle_state"]),
                last_signal=str(payload["last_signal"]),
                updated_ts=int(payload["updated_ts"]),
                evidence_hash=str(payload.get("evidence_hash") or ""),
            )
            self.virtual_target_state_persister(conn, state)

    def _persist_lock_intents(self, conn: sqlite3.Connection, *, context: Mapping[str, object]) -> None:
        for intent in context.get("lock_intents") or []:
            if not isinstance(intent, Mapping):
                continue
            common = {
                "pair": str(intent.get("pair") or ""),
                "currency": str(intent.get("currency") or ""),
                "amount": float(intent.get("amount") or 0.0),
                "reason": str(intent.get("reason") or ""),
                "created_ts": int(intent.get("created_ts") or 0),
                "idempotency_key": str(intent.get("idempotency_key") or ""),
                "evidence": dict(intent.get("evidence") or {}),
            }
            if str(intent.get("lock_kind") or "") == "budget":
                self.budget_lock_persister(conn, **common)
            elif str(intent.get("lock_kind") or "") == "order":
                self.order_lock_persister(conn, **common)


def _db_subphase_from_exception(exc: BaseException) -> str:
    return str(getattr(exc, "db_subphase", "") or "decision_persistence")


def _sql_group_from_exception(exc: BaseException) -> str:
    return str(getattr(exc, "sql_group", "") or "decision_persistence_transaction")


def _lock_metadata(
    conn: sqlite3.Connection,
    *,
    retry_count: int,
    max_retry_count: int,
    last_lock_error: str,
    db_subphase: str,
    sql_group: str,
    transaction_started_at: float,
    lock_wait_elapsed_ms: float,
) -> dict[str, object]:
    return {
        "db_connection_id": id(conn),
        "pid": os.getpid(),
        "thread_id": threading.get_ident(),
        "db_subphase": db_subphase,
        "sql_group": sql_group,
        "retry_count": int(retry_count),
        "max_retry_count": int(max_retry_count),
        "last_lock_error": last_lock_error,
        "transaction_elapsed_ms": max(0.0, (time.monotonic() - transaction_started_at) * 1000.0),
        "lock_wait_elapsed_ms": max(0.0, lock_wait_elapsed_ms),
    }


__all__ = [
    "DecisionPersistenceError",
    "DecisionPersistenceResult",
    "DecisionPersistenceUnitOfWork",
]
