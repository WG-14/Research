from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .hashing import canonical_payload_hash
from market_research.market_regime import RegimeCoverageRow

from .backtest_types import (
    BacktestResourceLimitExceeded,
    BacktestRun,
    BacktestRunContext,
    MemorySample,
)
from .execution_model import ExecutionFill
from .streaming_evidence import StreamingEvidenceDigest


@dataclass
class BacktestAccumulator:
    context: BacktestRunContext
    total_candles: int
    diagnostics_namespace: str
    decision_count: int = 0
    signal_count: int = 0
    retained_decision_count: int = 0
    retained_equity_point_count: int = 0
    trade_count: int = 0
    closed_trade_count: int = 0
    period_start_ts: int | None = None
    period_end_ts: int | None = None
    active_bar_count: int = 0
    last_heartbeat_s: float = field(default_factory=time.perf_counter)
    last_heartbeat_bar: int = 0
    decision_hash_material: StreamingEvidenceDigest = field(
        default_factory=lambda: StreamingEvidenceDigest("decision_hash_material")
    )
    behavior_hash_material: StreamingEvidenceDigest = field(
        default_factory=lambda: StreamingEvidenceDigest("behavior_hash_material")
    )
    common_behavior_hash_material: StreamingEvidenceDigest = field(
        default_factory=lambda: StreamingEvidenceDigest("common_behavior_hash_material")
    )
    strategy_behavior_hash_material: StreamingEvidenceDigest = field(
        default_factory=lambda: StreamingEvidenceDigest("strategy_behavior_hash_material")
    )
    trade_ledger_hash_material: list[dict[str, object]] = field(default_factory=list)
    equity_curve_hash_material: list[dict[str, object]] = field(default_factory=list)
    strategy_diagnostic_counts: dict[str, int] = field(default_factory=dict)
    canonical_payload_hash_call_count: int = 0
    canonical_hash_payload_bytes: int = 0
    largest_canonical_hash_payload_bytes: int = 0
    largest_canonical_hash_label: str = ""
    stable_value_call_count: int = 0
    stable_value_wall_seconds: float = 0.0
    canonical_json_wall_seconds: float = 0.0
    decision_payload_build_wall_seconds: float = 0.0
    observability_wall_seconds: float = 0.0
    audit_decision_event_count: int = 0
    audit_equity_event_count: int = 0
    initialized_portfolio_policy_evidence: dict[str, Any] = field(default_factory=dict)
    baseline_memory_sample: MemorySample = field(init=False)

    def __post_init__(self) -> None:
        self.baseline_memory_sample = self.context.memory_sampler()

    @property
    def report_detail(self) -> str:
        detail = str(self.context.report_detail or "full").strip().lower()
        return detail if detail in {"index", "summary", "standard", "full"} else "full"

    def retain_full_detail(self) -> bool:
        return self.report_detail == "full"

    def retain_decision(self) -> bool:
        limit = self.context.resource_limits.max_decisions_retained
        if self.report_detail == "full" and limit is None:
            return True
        if limit is None:
            return True
        return self.retained_decision_count < int(limit)

    def retain_equity_point(self) -> bool:
        limit = self.context.resource_limits.max_equity_points_retained
        if self.report_detail == "full" and limit is None:
            return True
        if limit is None:
            return True
        return self.retained_equity_point_count < int(limit)

    def record_initialized_portfolio_policy(self, policy: Any) -> None:
        self.initialized_portfolio_policy_evidence = portfolio_policy_evidence(policy)

    def update_decision(self, payload: dict[str, object], retained: bool) -> None:
        self.decision_count += 1
        raw_signal = str(payload.get("raw_signal") or "").upper()
        if raw_signal in {"BUY", "SELL"}:
            self.signal_count += 1
        for key, value in _diagnostic_count_defaults(payload).items():
            self.strategy_diagnostic_counts.setdefault(key, int(value))
        for key, value in _diagnostic_count_increments(payload).items():
            self.strategy_diagnostic_counts[key] = (
                int(self.strategy_diagnostic_counts.get(key, 0)) + int(value)
            )
        self.decision_hash_material.update(str(payload.get("replay_fingerprint_hash") or ""))
        self.behavior_hash_material.update(
            {
                "candle_ts": payload.get("candle_ts"),
                "raw_signal": payload.get("raw_signal"),
                "entry_signal": payload.get("entry_signal"),
                "exit_signal": payload.get("exit_signal"),
                "final_signal": payload.get("final_signal"),
                "entry_reason": payload.get("entry_reason"),
                "exit_rule": payload.get("exit_rule"),
                "exit_reason": payload.get("exit_reason"),
                "blocked_filters": payload.get("blocked_filters"),
                "regime_decision": payload.get("regime_decision"),
                "regime_block_reason": payload.get("regime_block_reason"),
            }
        )
        self.common_behavior_hash_material.update(
            {
                "candle_ts": payload.get("candle_ts"),
                "raw_signal": payload.get("raw_signal"),
                "final_signal": payload.get("final_signal"),
                "position_state_hash": payload.get("position_state_hash"),
                "execution_intent": payload.get("execution_intent"),
                "order_intent": payload.get("order_intent"),
                "exit_intent": payload.get("exit_intent"),
            }
        )
        strategy_namespace = str(
            payload.get("strategy_diagnostics_namespace")
            or payload.get("strategy_name")
            or self.diagnostics_namespace
        )
        self.strategy_behavior_hash_material.update(
            {
                "namespace": strategy_namespace,
                "payload": payload.get("strategy_behavior_payload")
                or payload.get("strategy_diagnostics")
                or {
                    "raw_signal": payload.get("raw_signal"),
                    "entry_signal": payload.get("entry_signal"),
                    "exit_signal": payload.get("exit_signal"),
                    "entry_reason": payload.get("entry_reason"),
                    "exit_rule": payload.get("exit_rule"),
                    "blocked_filters": payload.get("blocked_filters"),
                    "feature_hash": payload.get("feature_hash"),
                },
            }
        )
        if retained:
            self.retained_decision_count += 1

    def record_canonical_observability(self, observed: dict[str, Any]) -> None:
        self.canonical_payload_hash_call_count += int(observed.get("canonical_payload_hash_call_count") or 0)
        self.canonical_hash_payload_bytes += int(observed.get("canonical_hash_payload_bytes") or 0)
        self.stable_value_call_count += int(observed.get("stable_value_call_count") or 0)
        self.stable_value_wall_seconds += float(observed.get("stable_value_wall_seconds") or 0.0)
        self.canonical_json_wall_seconds += float(observed.get("canonical_json_wall_seconds") or 0.0)
        largest = int(observed.get("largest_canonical_hash_payload_bytes") or 0)
        if largest > self.largest_canonical_hash_payload_bytes:
            self.largest_canonical_hash_payload_bytes = largest
            self.largest_canonical_hash_label = str(observed.get("largest_canonical_hash_label") or "")

    def record_decision_payload_build_time(self, elapsed: float) -> None:
        self.decision_payload_build_wall_seconds += float(elapsed)

    def record_observability_time(self, elapsed: float) -> None:
        self.observability_wall_seconds += float(elapsed)

    def record_audit_decision_event(self) -> None:
        self.audit_decision_event_count += 1

    def record_audit_equity_event(self) -> None:
        self.audit_equity_event_count += 1

    def update_equity(self, *, retained: bool, ts: int, asset_qty: float) -> None:
        if self.period_start_ts is None:
            self.period_start_ts = int(ts)
        self.period_end_ts = int(ts)
        if float(asset_qty) > 1e-12:
            self.active_bar_count += 1
        if retained:
            self.retained_equity_point_count += 1

    def record_equity_point(self, *, ts: int, equity: float, cash: float, asset_qty: float) -> None:
        self.equity_curve_hash_material.append(
            {
                "ts": int(ts),
                "equity": round(float(equity), 12),
                "cash": round(float(cash), 12),
                "asset_qty": round(float(asset_qty), 12),
            }
        )

    def record_trade_ledger(self, trade: dict[str, object]) -> None:
        self.trade_ledger_hash_material.append(trade_hash_payload(trade))

    def update_trades(self, trades: list[dict[str, object]]) -> None:
        self.trade_count = len(trades)
        self.closed_trade_count = sum(
            1 for trade in trades if str(trade.get("side") or "").upper() == "SELL"
        )

    def maybe_emit_heartbeat(self, candles_processed: int) -> None:
        callback = self.context.progress_callback
        if callback is None:
            return
        now = time.perf_counter()
        interval = self.context.heartbeat.interval_s
        bar_interval = self.context.heartbeat.bar_interval
        by_time = interval is not None and now - self.last_heartbeat_s >= float(interval)
        by_bar = (
            bar_interval is not None
            and int(bar_interval) > 0
            and candles_processed - self.last_heartbeat_bar >= int(bar_interval)
        )
        if not by_time and not by_bar:
            return
        self.last_heartbeat_s = now
        self.last_heartbeat_bar = candles_processed
        callback(self.heartbeat_payload(candles_processed=candles_processed))

    def heartbeat_payload(self, *, candles_processed: int, memory: dict[str, Any] | None = None) -> dict[str, Any]:
        memory = memory if memory is not None else self.memory_payload()
        return {
            "stage": "heartbeat",
            "experiment_id": self.context.experiment_id,
            "candidate_id": self.context.candidate_id,
            "scenario": self.context.scenario_id,
            "split": self.context.split_name,
            "candles_processed": int(candles_processed),
            "total_candles": int(self.total_candles),
            "signal_count": int(self.signal_count),
            "trade_count": int(self.trade_count),
            "closed_trade_count": int(self.closed_trade_count),
            "decision_count": int(self.decision_count),
            "retained_decision_count": int(self.retained_decision_count),
            "retained_equity_point_count": int(self.retained_equity_point_count),
            "elapsed_s": round(time.perf_counter() - self.context.started_at, 3),
            **memory,
            "rss_mb": memory["current_rss_mb"],
            "report_detail": self.report_detail,
        }

    def memory_payload(self) -> dict[str, Any]:
        sample = self.context.memory_sampler()
        baseline = self.baseline_memory_sample.current_rss_mb
        current = sample.current_rss_mb
        delta = (
            round(max(0.0, float(current) - float(baseline)), 3)
            if current is not None and baseline is not None
            else None
        )
        return {
            "memory_measurement": "candidate_local_current_rss_delta",
            "memory_sample_source": sample.source,
            "peak_rss_source_units": sample.peak_rss_source_units,
            "peak_rss_platform": sample.peak_rss_platform,
            "current_rss_mb": current,
            "peak_rss_mb": sample.peak_rss_mb,
            "baseline_rss_mb": baseline,
            "rss_delta_mb": delta,
        }

    def check_limits(self, *, candles_processed: int, trades: list[dict[str, object]]) -> None:
        self.update_trades(trades)
        limits = self.context.resource_limits
        reasons: list[str] = []
        elapsed = time.perf_counter() - self.context.started_at
        memory = self.memory_payload()
        rss_delta = memory["rss_delta_mb"]
        if (
            limits.max_runtime_s_per_candidate_split is not None
            and elapsed > float(limits.max_runtime_s_per_candidate_split)
        ):
            reasons.append("max_runtime_exceeded")
        if limits.max_trades is not None and self.trade_count > int(limits.max_trades):
            reasons.append("max_trades_exceeded")
        if limits.max_rss_mb is not None and rss_delta is not None and rss_delta > float(limits.max_rss_mb):
            reasons.append("max_rss_exceeded")
        if not reasons:
            return
        evidence = self.heartbeat_payload(candles_processed=candles_processed, memory=memory)
        evidence.update(self.initialized_portfolio_policy_evidence)
        evidence.update(
            {
                "status": "TRIPPED",
                "reasons": sorted(set(reasons)),
                "resource_limit_semantics": {
                    "max_rss_mb": "candidate_local_rss_delta_mb",
                    "peak_rss_mb": "observability_high_water_not_limit_authority",
                    "memory_sample_reused_for_failure_evidence": True,
                },
            }
        )
        if self.context.audit_trace is not None:
            evidence["audit_trace_index"] = self.context.audit_trace.complete(status="failed")
        raise BacktestResourceLimitExceeded("candidate_resource_limit_exceeded", evidence)

    def resource_usage(self, *, candles_processed: int) -> dict[str, Any]:
        payload = self.heartbeat_payload(candles_processed=candles_processed)
        payload.pop("stage", None)
        payload.pop("elapsed_s", None)
        payload.pop("rss_mb", None)
        payload["applied_resource_limits"] = self.context.resource_limits.as_dict()
        payload["resource_policy"] = self.context.resource_limits.as_dict()
        payload["memory_sampling_policy"] = self.context.resource_limits.as_dict()["memory_sampling_policy"]
        policy = self.context.tick_observability_policy()
        payload["canonical_evidence_policy"] = policy.name
        payload["observability_policy"] = policy.name
        payload["tick_observability_policy"] = policy.as_dict()
        payload["estimated_full_tick_canonical_enabled"] = bool(policy.full_tick_canonical_enabled)
        payload["decision_hash"] = self.decision_hash_material.hash
        payload["decision_hash_material_count"] = int(self.decision_hash_material.count)
        payload.update(self.initialized_portfolio_policy_evidence)
        payload.update(
            _behavior_hashes(
                decision_material=self.behavior_hash_material,
                common_decision_material=self.common_behavior_hash_material,
                strategy_decision_material=self.strategy_behavior_hash_material,
                trade_material=self.trade_ledger_hash_material,
                equity_material=self.equity_curve_hash_material,
            )
        )
        payload["strategy_diagnostics"] = self.strategy_diagnostics(trades=[])
        payload.update(
            {
                "behavior_hash_material_count": int(self.behavior_hash_material.count),
                "behavior_hash_material_retention_policy": self.behavior_hash_material.finalize()[
                    "retention_policy"
                ],
                "behavior_hash_material_sample_count": int(self.behavior_hash_material.finalize()["sample_count"]),
                "canonical_payload_hash_call_count": int(self.canonical_payload_hash_call_count),
                "canonical_hash_payload_bytes": int(self.canonical_hash_payload_bytes),
                "largest_canonical_hash_payload_bytes": int(self.largest_canonical_hash_payload_bytes),
                "largest_canonical_hash_label": str(self.largest_canonical_hash_label),
                "stable_value_call_count": int(self.stable_value_call_count),
                "stable_value_wall_seconds": float(self.stable_value_wall_seconds),
                "canonical_json_wall_seconds": float(self.canonical_json_wall_seconds),
                "decision_payload_build_wall_seconds": float(self.decision_payload_build_wall_seconds),
                "observability_wall_seconds": float(self.observability_wall_seconds),
                "retained_decision_count": int(self.retained_decision_count),
                "audit_decision_event_count": int(self.audit_decision_event_count),
                "audit_equity_event_count": int(self.audit_equity_event_count),
            }
        )
        return payload

    def metrics_summary_inputs(self, *, max_drawdown_pct: float) -> dict[str, Any]:
        elapsed_ms = (
            int(self.period_end_ts) - int(self.period_start_ts)
            if self.period_start_ts is not None and self.period_end_ts is not None
            else None
        )
        return {
            "summary_period_start_ts": self.period_start_ts,
            "summary_period_end_ts": self.period_end_ts,
            "summary_elapsed_ms": elapsed_ms,
            "summary_max_drawdown_pct": float(max_drawdown_pct),
            "summary_active_bar_count": int(self.active_bar_count),
        }

    def strategy_diagnostics(self, *, trades: list[dict[str, object]]) -> dict[str, object]:
        payload = _generic_strategy_diagnostics_from_trades(
            namespace=self.diagnostics_namespace,
            trades=trades,
        )
        for key in sorted(self.strategy_diagnostic_counts):
            payload[key] = int(self.strategy_diagnostic_counts[key])
        _expand_diagnostic_distributions(payload)
        if "entry_count" not in payload:
            payload["entry_count"] = int(payload.get("entry_signal_count") or 0)
        if "exit_count" not in payload:
            payload["exit_count"] = int(payload.get("exit_signal_count") or 0)
        payload.setdefault("raw_signal_count", 0)
        payload.setdefault("final_signal_count", 0)
        payload.setdefault("entry_signal_count", 0)
        payload.setdefault("blocked_filter_distribution", {})
        payload.setdefault("entry_reason_distribution", {})
        payload.setdefault("exit_reason_distribution", {})
        payload.setdefault("p95_mfe_pct", _percentile(list(payload.get("mfe_pct_by_trade") or []), 0.95))
        strategy_specific = dict(payload)
        payload["strategy_specific_diagnostics"] = {self.diagnostics_namespace: strategy_specific}
        return payload


def portfolio_policy_evidence(policy: Any) -> dict[str, Any]:
    return {
        "executed_portfolio_policy": policy.as_dict(),
        "executed_portfolio_policy_hash": policy.policy_hash(),
        "ledger_starting_cash_krw": float(policy.starting_cash_krw),
        "ledger_initial_position_qty": float(policy.initial_position_qty),
        "position_sizing_policy": policy.position_sizing.as_dict(),
        "legacy_research_portfolio_policy_used": policy.source == "legacy_research_default",
    }


@dataclass
class RegimeCoverageAccumulator:
    total: int = 0
    counts: dict[str, dict[str, int]] = field(default_factory=dict)

    def update(self, snapshot: dict[str, object]) -> None:
        self.total += 1
        for dimension in ("price_regime", "volatility_bucket", "volume_bucket", "composite_regime"):
            bucket = str(snapshot.get(dimension) or "unknown")
            dimension_counts = self.counts.setdefault(dimension, {})
            dimension_counts[bucket] = dimension_counts.get(bucket, 0) + 1

    def coverage(self, *, trades: list[dict[str, object]]) -> tuple[RegimeCoverageRow, ...]:
        trade_counts: dict[tuple[str, str], int] = {}
        for trade in trades:
            if not _trade_is_effective(trade) or str(trade.get("side") or "").upper() != "BUY":
                continue
            snapshot = trade.get("entry_regime_snapshot")
            for dimension in ("price_regime", "volatility_bucket", "volume_bucket", "composite_regime"):
                regime = _regime_snapshot_value(snapshot, dimension)
                key = (dimension, regime)
                trade_counts[key] = trade_counts.get(key, 0) + 1
        rows: list[RegimeCoverageRow] = []
        for dimension in ("price_regime", "volatility_bucket", "volume_bucket", "composite_regime"):
            candle_counts = self.counts.get(dimension, {})
            regimes = sorted(
                set(candle_counts)
                | {regime for item_dimension, regime in trade_counts if item_dimension == dimension}
            )
            for regime in regimes:
                candles = int(candle_counts.get(regime, 0))
                rows.append(
                    RegimeCoverageRow(
                        dimension=dimension,
                        regime=regime,
                        candle_count=candles,
                        candle_share=(candles / self.total) if self.total else 0.0,
                        trade_count=int(trade_counts.get((dimension, regime), 0)),
                    )
                )
        return tuple(rows)


@dataclass
class PendingFill:
    fill: ExecutionFill
    trade_index: int
    side: str
    effective_ts: int
    qty: float
    fee: float
    slippage: float
    cash_delta: float
    entry_regime_snapshot: dict[str, object] | None = None
    exit_regime_snapshot: dict[str, object] | None = None
    entry_feature_snapshot: dict[str, object] | None = None


@dataclass(frozen=True)
class ResearchPositionContext:
    in_position: bool
    entry_ts: int | None = None
    entry_price: float | None = None
    qty_open: float = 0.0
    holding_time_sec: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_ratio: float = 0.0


def _diagnostic_count_defaults(payload: dict[str, object]) -> dict[str, int]:
    defaults = payload.get("strategy_diagnostic_count_defaults")
    if not isinstance(defaults, dict):
        return {}
    return {
        str(key): int(value)
        for key, value in defaults.items()
        if _diagnostic_key_is_public(str(key))
    }


def _diagnostic_count_increments(payload: dict[str, object]) -> dict[str, int]:
    counts = payload.get("strategy_diagnostic_counts")
    if not isinstance(counts, dict):
        return {}
    increments: dict[str, int] = {}
    for key, value in counts.items():
        normalized = str(key)
        if not _diagnostic_key_is_public(normalized):
            continue
        increments[normalized] = increments.get(normalized, 0) + int(value)
    return increments


def _diagnostic_key_is_public(key: str) -> bool:
    return bool(key) and not key.startswith("_")


def _expand_diagnostic_distributions(payload: dict[str, object]) -> None:
    distributions: dict[str, dict[str, int]] = {}
    for key, value in list(payload.items()):
        if "." not in key:
            continue
        prefix, label = key.split(".", 1)
        if prefix not in {
            "blocked_filter_distribution",
            "entry_reason_distribution",
            "exit_reason_distribution",
            "exit_rule_distribution",
        }:
            continue
        if not label:
            continue
        bucket = distributions.setdefault(prefix, {})
        bucket[label] = bucket.get(label, 0) + int(value)
    for prefix, values in distributions.items():
        existing = payload.get(prefix)
        merged = dict(existing) if isinstance(existing, dict) else {}
        for label, count in values.items():
            merged[label] = int(merged.get(label, 0)) + int(count)
        payload[prefix] = dict(sorted(merged.items()))


def _generic_strategy_diagnostics_from_trades(
    *,
    namespace: str,
    trades: list[dict[str, object]],
) -> dict[str, object]:
    closed = [
        trade
        for trade in trades
        if isinstance(trade, dict)
        and bool(trade.get("is_portfolio_applied_trade"))
        and str(trade.get("side") or "").upper() == "SELL"
    ]
    exit_reason_distribution: dict[str, int] = {}
    exit_rule_distribution: dict[str, int] = {}
    return_groups: dict[str, dict[str, list[float] | int]] = {}
    holding_minutes_by_reason: dict[str, list[float]] = {}
    excursion_groups: dict[str, dict[str, list[float] | int]] = {}
    mae_pct_by_trade: list[float] = []
    mfe_pct_by_trade: list[float] = []
    loss_holding_minutes: list[float] = []
    for trade in closed:
        reason = str(trade.get("exit_rule") or trade.get("exit_reason") or "unknown")
        exit_reason_distribution[reason] = exit_reason_distribution.get(reason, 0) + 1
        exit_rule_distribution[reason] = exit_rule_distribution.get(reason, 0) + 1
        return_group = return_groups.setdefault(
            reason,
            {"count": 0, "return_pct": [], "pnl": []},
        )
        return_group["count"] = int(return_group["count"]) + 1
        if trade.get("return_pct") is not None:
            return_group["return_pct"].append(float(trade.get("return_pct") or 0.0))  # type: ignore[union-attr]
        pnl = trade.get("net_pnl") if trade.get("net_pnl") is not None else trade.get("closed_trade_pnl")
        if pnl is not None:
            return_group["pnl"].append(float(pnl))  # type: ignore[union-attr]
        if trade.get("holding_minutes") is not None:
            holding_minutes_by_reason.setdefault(reason, []).append(float(trade.get("holding_minutes") or 0.0))
        excursion_group = excursion_groups.setdefault(
            reason,
            {"count": 0, "mae_pct": [], "mfe_pct": []},
        )
        if trade.get("mae_pct") is not None or trade.get("mfe_pct") is not None:
            excursion_group["count"] = int(excursion_group["count"]) + 1
        if trade.get("mae_pct") is not None:
            mae_pct = float(trade.get("mae_pct") or 0.0)
            mae_pct_by_trade.append(mae_pct)
            excursion_group["mae_pct"].append(mae_pct)  # type: ignore[union-attr]
        if trade.get("mfe_pct") is not None:
            mfe_pct = float(trade.get("mfe_pct") or 0.0)
            mfe_pct_by_trade.append(mfe_pct)
            excursion_group["mfe_pct"].append(mfe_pct)  # type: ignore[union-attr]
        if pnl is not None and float(pnl) < 0.0 and trade.get("holding_minutes") is not None:
            loss_holding_minutes.append(float(trade.get("holding_minutes") or 0.0))
    payload = {
        "schema_version": 1,
        "strategy_diagnostics_namespace": str(namespace),
        "exit_reason_distribution": dict(sorted(exit_reason_distribution.items())),
        "exit_rule_distribution": dict(sorted(exit_rule_distribution.items())),
        "return_by_exit_reason": _return_summary_by_exit_reason(return_groups),
        "avg_holding_minutes_by_exit_reason": {
            reason: _average(values)
            for reason, values in sorted(holding_minutes_by_reason.items())
            if values
        },
        "mae_mfe_by_exit_reason": _mae_mfe_summary_by_exit_reason(excursion_groups),
        "mae_pct_by_trade": mae_pct_by_trade,
        "mfe_pct_by_trade": mfe_pct_by_trade,
        "p95_mae_pct": _percentile(mae_pct_by_trade, 0.95),
        "p05_mae_pct": _percentile(mae_pct_by_trade, 0.05),
        "p95_adverse_excursion_abs_pct": _percentile(
            [abs(value) for value in mae_pct_by_trade],
            0.95,
        ),
        "worst_trade_mae_pct": min(mae_pct_by_trade) if mae_pct_by_trade else None,
        "avg_loss_holding_minutes": (
            sum(loss_holding_minutes) / len(loss_holding_minutes)
            if loss_holding_minutes
            else None
        ),
    }
    payload["strategy_specific_diagnostics"] = {str(namespace): dict(payload)}
    return payload


def _return_summary_by_exit_reason(
    groups: dict[str, dict[str, list[float] | int]],
) -> dict[str, dict[str, float | int | None]]:
    summary: dict[str, dict[str, float | int | None]] = {}
    for reason, group in sorted(groups.items()):
        return_values = list(group.get("return_pct") or [])
        pnl_values = list(group.get("pnl") or [])
        summary[reason] = {
            "count": int(group.get("count") or 0),
            "avg_return_pct": _average(return_values) if return_values else None,
            "total_return_pct": sum(return_values) if return_values else None,
            "avg_pnl": _average(pnl_values) if pnl_values else None,
            "total_pnl": sum(pnl_values) if pnl_values else None,
        }
    return summary


def _mae_mfe_summary_by_exit_reason(
    groups: dict[str, dict[str, list[float] | int]],
) -> dict[str, dict[str, float | int | None]]:
    summary: dict[str, dict[str, float | int | None]] = {}
    for reason, group in sorted(groups.items()):
        mae_values = list(group.get("mae_pct") or [])
        mfe_values = list(group.get("mfe_pct") or [])
        if not mae_values and not mfe_values:
            continue
        summary[reason] = {
            "count": int(group.get("count") or 0),
            "avg_mae_pct": _average(mae_values) if mae_values else None,
            "min_mae_pct": min(mae_values) if mae_values else None,
            "avg_mfe_pct": _average(mfe_values) if mfe_values else None,
            "max_mfe_pct": max(mfe_values) if mfe_values else None,
        }
    return summary


def _average(values: list[float]) -> float:
    return sum(float(value) for value in values) / len(values)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * float(percentile)))))
    return ordered[index]


def _behavior_hashes(
    *,
    decision_material: StreamingEvidenceDigest | list[object],
    common_decision_material: StreamingEvidenceDigest | None,
    strategy_decision_material: StreamingEvidenceDigest | None,
    trade_material: list[dict[str, object]],
    equity_material: list[dict[str, object]],
) -> dict[str, str | int | object]:
    decision_final = _finalize_behavior_material(decision_material, label="behavior_hash_material")
    common_final = (
        _finalize_behavior_material(common_decision_material, label="common_behavior_hash_material")
        if common_decision_material is not None
        else StreamingEvidenceDigest("empty_common_behavior_hash_material").finalize()
    )
    strategy_final = (
        _finalize_behavior_material(strategy_decision_material, label="strategy_behavior_hash_material")
        if strategy_decision_material is not None
        else StreamingEvidenceDigest("empty_strategy_behavior_hash_material").finalize()
    )
    decision_hash = str(decision_final["hash"])
    common_decision_hash = str(common_final["hash"])
    strategy_decision_hash = str(strategy_final["hash"])
    trade_hash = canonical_payload_hash(trade_material)
    equity_hash = canonical_payload_hash(equity_material)
    composite_hash = canonical_payload_hash(
        {
            "decision_behavior_hash": decision_hash,
            "trade_ledger_hash": trade_hash,
            "equity_curve_hash": equity_hash,
        }
    )
    composite_hash_v2 = canonical_payload_hash(
        {
            "common_decision_behavior_hash": common_decision_hash,
            "strategy_behavior_hash": strategy_decision_hash,
            "trade_ledger_hash": trade_hash,
            "equity_curve_hash": equity_hash,
        }
    )
    return {
        "decision_behavior_hash": decision_hash,
        "common_decision_behavior_hash": common_decision_hash,
        "strategy_behavior_hash": strategy_decision_hash,
        "trade_ledger_hash": trade_hash,
        "equity_curve_hash": equity_hash,
        "composite_behavior_hash": composite_hash,
        "composite_behavior_hash_v2": composite_hash_v2,
        "behavior_hash": composite_hash,
        "behavior_hash_material_count": int(decision_final["count"]),
        "common_behavior_hash_material_count": int(common_final["count"]),
        "strategy_behavior_hash_material_count": int(strategy_final["count"]),
        "behavior_hash_material_retention_policy": str(decision_final["retention_policy"]),
        "behavior_hash_material_sample_hash": str(decision_final["sample_hash"]),
        "behavior_hash_material_sample_count": int(decision_final["sample_count"]),
    }


def _finalize_behavior_material(
    material: StreamingEvidenceDigest | list[object],
    *,
    label: str,
) -> dict[str, object]:
    if isinstance(material, StreamingEvidenceDigest):
        return material.finalize()
    digest = StreamingEvidenceDigest(label)
    for item in material:
        digest.update(item)
    return digest.finalize()


def _regime_snapshot_value(snapshot: Any, key: str) -> str:
    if isinstance(snapshot, dict):
        return str(snapshot.get(key) or "unknown")
    return str(getattr(snapshot, key, "unknown") or "unknown")


def _trade_is_effective(trade: dict[str, object]) -> bool:
    if "is_portfolio_applied_trade" in trade:
        return bool(trade.get("is_portfolio_applied_trade"))
    if "is_effective_trade" in trade:
        return bool(trade.get("is_effective_trade"))
    execution = trade.get("execution")
    if isinstance(execution, dict):
        status = str(execution.get("fill_status") or "")
        return float(execution.get("filled_qty") or 0.0) > 0.0 and status in {"filled", "partial"}
    return float(trade.get("qty") or 0.0) > 0.0


def create_exit_rules(**kwargs: Any):
    from .backtest_common import create_exit_rules as impl
    return impl(**kwargs)


def retained_detail_summary(*args: Any, **kwargs: Any) -> dict[str, object]:
    from .backtest_common import retained_detail_summary as impl
    return impl(*args, **kwargs)


def trade_hash_payload(trade: dict[str, object]) -> dict[str, object]:
    from .backtest_common import trade_hash_payload as impl
    return impl(trade)


def trace_decision(*args: Any, **kwargs: Any) -> None:
    from .backtest_common import trace_decision as impl
    return impl(*args, **kwargs)


def trace_equity_mark(*args: Any, **kwargs: Any) -> None:
    from .backtest_common import trace_equity_mark as impl
    return impl(*args, **kwargs)


def trace_execution(*args: Any, **kwargs: Any) -> None:
    from .backtest_common import trace_execution as impl
    return impl(*args, **kwargs)


def complete_audit_trace(*args: Any, **kwargs: Any) -> dict[str, object] | None:
    from .backtest_common import complete_audit_trace as impl
    return impl(*args, **kwargs)


def record_equity_mark(*args: Any, **kwargs: Any):
    from .backtest_common import record_equity_mark as impl
    return impl(*args, **kwargs)


def fill_applies_to_mark(*args: Any, **kwargs: Any) -> bool:
    from .backtest_common import fill_applies_to_mark as impl
    return impl(*args, **kwargs)


def apply_pending_fills(*args: Any, **kwargs: Any):
    """Compatibility wrapper; PortfolioLedger owns authority-facing mutation."""
    from .backtest_common import apply_pending_fills as impl
    return impl(*args, **kwargs)


def timing_request_fields(*args: Any, **kwargs: Any) -> dict[str, object]:
    from .backtest_common import timing_request_fields as impl
    return impl(*args, **kwargs)


def depth_request_fields(*args: Any, **kwargs: Any) -> dict[str, object]:
    from .backtest_common import depth_request_fields as impl
    return impl(*args, **kwargs)


def research_decision_payload(*args: Any, **kwargs: Any) -> dict[str, object]:
    from .backtest_common import research_decision_payload as impl
    return impl(*args, **kwargs)


def research_order_rules_payload(*args: Any, **kwargs: Any) -> dict[str, object]:
    from .backtest_common import research_order_rules_payload as impl
    return impl(*args, **kwargs)


def model_latency_ms(*args: Any, **kwargs: Any) -> int:
    from .backtest_common import model_latency_ms as impl
    return impl(*args, **kwargs)


def failed_fill(*args: Any, **kwargs: Any):
    from .backtest_common import failed_fill as impl
    return impl(*args, **kwargs)


def trade_from_fill(*args: Any, **kwargs: Any) -> dict[str, object]:
    from .backtest_common import trade_from_fill as impl
    return impl(*args, **kwargs)


def pending_trade_from_fill(*args: Any, **kwargs: Any) -> dict[str, object]:
    from .backtest_common import pending_trade_from_fill as impl
    return impl(*args, **kwargs)


def fill_effective_ts(*args: Any, **kwargs: Any) -> int:
    from .backtest_common import fill_effective_ts as impl
    return impl(*args, **kwargs)


def mark_pending_fills_at_end(*args: Any, **kwargs: Any) -> None:
    from .backtest_common import mark_pending_fills_at_end as impl
    return impl(*args, **kwargs)


def execution_event_summary(*args: Any, **kwargs: Any) -> dict[str, object]:
    from .backtest_common import execution_event_summary as impl
    return impl(*args, **kwargs)


def empty_execution_event_summary() -> dict[str, object]:
    from .backtest_common import empty_execution_event_summary as impl
    return impl()


def empty_metrics_v2(*args: Any, **kwargs: Any):
    from .backtest_common import empty_metrics_v2 as impl
    return impl(*args, **kwargs)


def metrics_v2_ledgers_from_trades(*args: Any, **kwargs: Any):
    from .backtest_common import metrics_v2_ledgers_from_trades as impl
    return impl(*args, **kwargs)


def closed_trade_diagnostics(*args: Any, **kwargs: Any):
    from .backtest_common import closed_trade_diagnostics as impl
    return impl(*args, **kwargs)


def execution_reference_warnings(*args: Any, **kwargs: Any):
    from .backtest_common import execution_reference_warnings as impl
    return impl(*args, **kwargs)


def empty_metrics(*args: Any, **kwargs: Any):
    from .backtest_common import empty_metrics as impl
    return impl(*args, **kwargs)


def metrics(*args: Any, **kwargs: Any):
    from .backtest_common import metrics as impl
    return impl(*args, **kwargs)
