from __future__ import annotations

import time
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    SupportsFloat,
    SupportsIndex,
    TypeVar,
    cast,
)
from .decision_event import OrderIntent, ResearchDecisionEvent
from .execution_model.base import ExecutionFill, ExecutionRequest
from .portfolio_ledger import LedgerEntry
from .hashing import sha256_prefixed

if TYPE_CHECKING:
    from .strategy_contract import CompiledStrategyContract

from market_research.market_regime import RegimeCoverageRow, RegimePerformanceRow

from .metrics import ResearchMetrics
from .metrics_contract import (
    ClosedTradeRecord,
    EquityPoint,
    MetricContractV2,
    PositionInterval,
)


ProgressCallback = Callable[[dict[str, Any]], None]
MemorySampler = Callable[[], "MemorySample"]
_StreamItem = TypeVar("_StreamItem")


def _stream_index(
    name: str,
    values: tuple[_StreamItem, ...],
    getter: Callable[[_StreamItem], object],
) -> dict[str, _StreamItem]:
    ids = [str(getter(value)) for value in values]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate_{name}_id")
    if any(not value for value in ids):
        raise ValueError(f"missing_{name}_id")
    return dict(zip(ids, values))


def _float_or_zero(value: object) -> float:
    if value is None:
        return 0.0
    numeric = cast(str | bytes | bytearray | SupportsFloat | SupportsIndex, value)
    try:
        return float(numeric)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class MemorySample:
    current_rss_mb: float | None
    peak_rss_mb: float | None
    source: str
    peak_rss_source_units: str | None = None
    peak_rss_platform: str | None = None


def ru_maxrss_to_mb(
    raw_value: float, *, platform: str | None = None
) -> tuple[float, str]:
    """Convert getrusage ru_maxrss using platform-defined units.

    Linux reports KiB and Darwin reports bytes. Other platforms are surfaced as
    KiB-compatible best effort because AWS Linux is the validation reference.
    """
    platform_name = (platform or sys.platform).lower()
    if platform_name.startswith("darwin"):
        return round(float(raw_value) / (1024.0 * 1024.0), 3), "bytes"
    if platform_name.startswith("linux"):
        return round(float(raw_value) / 1024.0, 3), "kib"
    return round(
        float(raw_value) / 1024.0, 3
    ), f"kib_assumed_for_platform:{platform_name}"


def sample_process_memory() -> MemorySample:
    current_rss_mb: float | None = None
    peak_rss_mb: float | None = None
    sources: list[str] = []
    try:
        status = Path("/proc/self/status")
        for line in status.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2:
                    current_rss_mb = round(float(parts[1]) / 1024.0, 3)
                    sources.append("procfs_status_vmrss")
                break
    except Exception:
        current_rss_mb = None
    try:
        import resource

        rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        peak_rss_mb, peak_units = ru_maxrss_to_mb(rss)
        sources.append("getrusage_ru_maxrss")
    except Exception:
        peak_rss_mb = None
        peak_units = None
    return MemorySample(
        current_rss_mb=current_rss_mb,
        peak_rss_mb=peak_rss_mb,
        source="+".join(sources) if sources else "unavailable",
        peak_rss_source_units=peak_units,
        peak_rss_platform=sys.platform,
    )


@dataclass(frozen=True)
class BacktestResourceLimits:
    max_runtime_s_per_candidate_split: float | None = None
    max_decisions_retained: int | None = None
    max_trades: int | None = None
    max_equity_points_retained: int | None = None
    max_rss_mb: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "max_runtime_s_per_candidate_split": self.max_runtime_s_per_candidate_split,
            "max_decisions_retained": self.max_decisions_retained,
            "max_trades": self.max_trades,
            "max_equity_points_retained": self.max_equity_points_retained,
            "max_rss_mb": self.max_rss_mb,
            "max_rss_mb_semantics": "candidate_local_rss_delta_mb",
            "memory_sampling_policy": {
                "cadence": "per_resource_limit_check_event",
                "check_event": "backtest_candle_or_event_limit_check",
                "current_rss_source": "procfs_status_vmrss_when_available",
                "peak_rss_source": "getrusage_ru_maxrss_observability_only",
                "limit_authority": "rss_delta_mb",
            },
        }


@dataclass(frozen=True)
class BacktestHeartbeatPolicy:
    interval_s: float | None = None
    bar_interval: int | None = None


@dataclass(frozen=True)
class BacktestTickObservabilityPolicy:
    name: str
    audit_mode: str = "summary_only"
    full_tick_canonical_decision: bool = False
    audit_decision: str = "aggregate"
    audit_equity_mark: str = "aggregate"
    strict_required_hashes: bool = False
    diagnostic_sample_limit: int = 3
    allow_fallback_hash: bool = True

    @property
    def full_tick_canonical_enabled(self) -> bool:
        return self.full_tick_canonical_decision

    def should_build_full_payload(self, event_number: int) -> bool:
        if self.name in {"full_tick_canonical", "validation_evidence"}:
            return True
        if self.name == "diagnostic_sampled":
            return int(event_number) <= int(self.diagnostic_sample_limit)
        return False

    def should_hash_full_decision(self, event_number: int) -> bool:
        return self.should_build_full_payload(event_number)

    def should_record_audit_decision(self, event_number: int) -> bool:
        if self.audit_decision == "per_tick":
            return True
        if self.audit_decision == "sampled":
            return int(event_number) <= int(self.diagnostic_sample_limit)
        return False

    def should_record_audit_equity_mark(self, event_number: int | None = None) -> bool:
        if self.audit_equity_mark == "per_tick":
            return True
        if self.audit_equity_mark == "sampled" and event_number is not None:
            return int(event_number) <= int(self.diagnostic_sample_limit)
        return False

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "audit_mode": self.audit_mode,
            "full_tick_canonical_decision": bool(self.full_tick_canonical_decision),
            "audit_decision": self.audit_decision,
            "audit_equity_mark": self.audit_equity_mark,
            "strict_required_hashes": bool(self.strict_required_hashes),
            "diagnostic_sample_limit": int(self.diagnostic_sample_limit),
            "allow_fallback_hash": bool(self.allow_fallback_hash),
        }


def resolve_tick_observability_policy(
    *,
    report_detail: str = "full",
    diagnostic_mode: str = "candidate_validation",
    audit_trail: Any | None = None,
    policy_materialization_mode: str = "research_exploratory",
    explicit_policy: str | BacktestTickObservabilityPolicy | None = None,
) -> BacktestTickObservabilityPolicy:
    if isinstance(explicit_policy, BacktestTickObservabilityPolicy):
        return explicit_policy
    audit_mode = str(getattr(audit_trail, "mode", "") or "summary_only").strip().lower()
    policy_name = str(explicit_policy or "").strip().lower()
    if not policy_name:
        if audit_mode == "complete_external":
            policy_name = "full_tick_canonical"
        elif (
            str(policy_materialization_mode or "").strip().lower()
            == "research_validation"
        ):
            policy_name = "validation_evidence"
        elif str(diagnostic_mode or "").strip().lower() == "exploratory":
            policy_name = "diagnostic_sampled"
        elif str(report_detail or "").strip().lower() in {
            "index",
            "summary",
            "standard",
        }:
            policy_name = "summary_aggregate"
        else:
            policy_name = "full_tick_canonical"
    if policy_name == "validation_evidence":
        return BacktestTickObservabilityPolicy(
            name="validation_evidence",
            audit_mode=audit_mode,
            full_tick_canonical_decision=True,
            audit_decision="per_tick",
            audit_equity_mark="per_tick",
            strict_required_hashes=True,
            allow_fallback_hash=True,
        )
    if policy_name == "full_tick_canonical":
        return BacktestTickObservabilityPolicy(
            name="full_tick_canonical",
            audit_mode=audit_mode,
            full_tick_canonical_decision=True,
            audit_decision="per_tick"
            if audit_mode == "complete_external"
            else "aggregate",
            audit_equity_mark="per_tick"
            if audit_mode == "complete_external"
            else "aggregate",
            strict_required_hashes=audit_mode == "complete_external",
            allow_fallback_hash=True,
        )
    if policy_name == "diagnostic_sampled":
        return BacktestTickObservabilityPolicy(
            name="diagnostic_sampled",
            audit_mode=audit_mode,
            full_tick_canonical_decision=False,
            audit_decision="sampled",
            audit_equity_mark="sampled",
            strict_required_hashes=False,
            allow_fallback_hash=False,
        )
    return BacktestTickObservabilityPolicy(
        name="summary_aggregate",
        audit_mode=audit_mode,
        full_tick_canonical_decision=False,
        audit_decision="aggregate",
        audit_equity_mark="aggregate",
        strict_required_hashes=False,
        allow_fallback_hash=False,
    )


@dataclass
class BacktestRunContext:
    experiment_id: str = ""
    candidate_id: str = ""
    scenario_id: str = ""
    scenario_index: int | None = None
    split_name: str = ""
    report_detail: str = "full"
    diagnostic_mode: str = "candidate_validation"
    audit_trail_policy: Any | None = None
    observability_policy: str | BacktestTickObservabilityPolicy | None = None
    resource_limits: BacktestResourceLimits = field(
        default_factory=BacktestResourceLimits
    )
    heartbeat: BacktestHeartbeatPolicy = field(default_factory=BacktestHeartbeatPolicy)
    progress_callback: ProgressCallback | None = None
    audit_trace: Any | None = None
    research_profile: dict[str, object] | None = None
    candidate_regime_policy: dict[str, object] | None = None
    candidate_regime_policy_drives_research_execution: bool = False
    policy_materialization_mode: str = "research_exploratory"
    participation_count_basis: str | None = None
    memory_sampler: MemorySampler = sample_process_memory
    started_at: float = field(default_factory=time.perf_counter)

    def tick_observability_policy(self) -> BacktestTickObservabilityPolicy:
        return resolve_tick_observability_policy(
            report_detail=self.report_detail,
            diagnostic_mode=self.diagnostic_mode,
            audit_trail=self.audit_trail_policy,
            policy_materialization_mode=self.policy_materialization_mode,
            explicit_policy=self.observability_policy,
        )


class BacktestResourceLimitExceeded(RuntimeError):
    def __init__(self, reason: str, evidence: dict[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.evidence = evidence


@dataclass(frozen=True)
class BacktestRun:
    metrics: ResearchMetrics
    trades: tuple[dict[str, object], ...]
    candle_count: int
    warnings: tuple[str, ...]
    regime_performance: tuple[RegimePerformanceRow, ...] = ()
    regime_coverage: tuple[RegimeCoverageRow, ...] = ()
    execution_event_summary: dict[str, object] | None = None
    decisions: tuple[ResearchDecisionEvent, ...] = ()
    equity_curve: tuple[EquityPoint, ...] = ()
    position_intervals: tuple[PositionInterval, ...] = ()
    closed_trades: tuple[ClosedTradeRecord, ...] = ()
    metrics_v2: MetricContractV2 | None = None
    resource_usage: dict[str, object] | None = None
    strategy_diagnostics: dict[str, object] | None = None
    retained_detail_summary: dict[str, object] | None = None
    audit_trace_index: dict[str, object] | None = None
    # Authoritative, independently hashable execution lineage streams.
    order_intents: tuple[OrderIntent, ...] = ()
    execution_requests: tuple[ExecutionRequest, ...] = ()
    fills: tuple[ExecutionFill, ...] = ()
    ledger_entries: tuple[LedgerEntry, ...] = ()
    compiled_strategy_contract: "CompiledStrategyContract | None" = None
    compiled_strategy_contract_hash: str | None = None
    strategy_registry_hash: str | None = None
    strategy_plugin_contract_hash: str | None = None
    decision_stream_hash: str | None = None
    dataset_snapshot_id: str | None = None
    dataset_source: str | None = None
    dataset_market: str | None = None
    dataset_interval: str | None = None
    dataset_period_start: str | None = None
    dataset_period_end: str | None = None
    dataset_artifact_manifest_hash: str | None = None
    dataset_snapshot_hash: str | None = None
    dataset_data_hash: str | None = None
    dataset_query_hash: str | None = None
    dataset_split_name: str | None = None
    execution_timing_hash: str | None = None
    materialized_parameters_hash: str | None = None
    parameter_source_map_hash: str | None = None
    point_in_time_decision_evidence: tuple[dict[str, object], ...] = ()
    point_in_time_decision_stream_hash: str | None = None
    point_in_time_authority_binding_hash: str | None = None
    metrics_hash: str | None = None
    authoritative_decision_ids: tuple[str, ...] = ()

    def validate_execution_lineage(self) -> None:
        """Fail closed on duplicate, orphaned, or inconsistent execution lineage."""
        from .execution_invariants import (
            fill_request_binding_violations,
            fill_timeline_violations,
        )

        if self.point_in_time_decision_evidence:
            row_hashes: list[str] = []
            for row in self.point_in_time_decision_evidence:
                payload = dict(row)
                recorded = payload.pop("row_hash", None)
                calculated = sha256_prefixed(
                    payload, label="point_in_time_decision_row"
                )
                if recorded != calculated:
                    raise ValueError("point_in_time_decision_row_hash_mismatch")
                row_hashes.append(calculated)
            calculated_stream_hash = sha256_prefixed(
                {"schema_version": 1, "row_hashes": row_hashes},
                label="point_in_time_decision_stream",
            )
            if self.point_in_time_decision_stream_hash != calculated_stream_hash:
                raise ValueError("point_in_time_decision_stream_hash_mismatch")
            summary = self.execution_event_summary or {}
            if summary.get("point_in_time_decision_stream_hash") != (
                calculated_stream_hash
            ):
                raise ValueError("point_in_time_summary_stream_hash_mismatch")
            if summary.get("point_in_time_authority_binding_hash") != (
                self.point_in_time_authority_binding_hash
            ):
                raise ValueError("point_in_time_summary_authority_hash_mismatch")

        if self.dataset_snapshot_hash is not None:
            summary = self.execution_event_summary or {}
            lineage = {
                "dataset_snapshot_id": self.dataset_snapshot_id,
                "dataset_source": self.dataset_source,
                "dataset_market": self.dataset_market,
                "dataset_interval": self.dataset_interval,
                "dataset_period_start": self.dataset_period_start,
                "dataset_period_end": self.dataset_period_end,
                "dataset_snapshot_hash": self.dataset_snapshot_hash,
                "dataset_data_hash": self.dataset_data_hash,
                "dataset_query_hash": self.dataset_query_hash,
                "dataset_split_name": self.dataset_split_name,
                "execution_timing_hash": self.execution_timing_hash,
                "materialized_parameters_hash": self.materialized_parameters_hash,
                "parameter_source_map_hash": self.parameter_source_map_hash,
            }
            if any(
                not isinstance(value, str) or not value for value in lineage.values()
            ):
                raise ValueError("backtest_authoritative_input_lineage_incomplete")
            lineage["dataset_artifact_manifest_hash"] = (
                self.dataset_artifact_manifest_hash
            )
            if any(summary.get(key) != value for key, value in lineage.items()):
                raise ValueError("backtest_authoritative_input_lineage_mismatch")

        decision_index = _stream_index(
            "decision", self.decisions, lambda value: value.decision_id()
        )
        intent_index = _stream_index(
            "intent", self.order_intents, lambda value: value.intent_id
        )
        request_index = _stream_index(
            "request", self.execution_requests, lambda value: value.request_id
        )
        fill_index = _stream_index("fill", self.fills, lambda value: value.fill_id)
        ledger_index = _stream_index(
            "ledger_entry", self.ledger_entries, lambda value: value.ledger_entry_id
        )
        decision_ids = set(decision_index)
        if self.authoritative_decision_ids:
            authoritative_ids = tuple(
                str(value) for value in self.authoritative_decision_ids
            )
            if len(authoritative_ids) != len(set(authoritative_ids)):
                raise ValueError("duplicate_authoritative_decision_id")
            decision_ids = set(authoritative_ids)
        for intent in self.order_intents:
            if intent.decision_id not in decision_ids:
                raise ValueError("orphan_intent")
        for request in self.execution_requests:
            if (
                request.intent_id not in intent_index
                or request.decision_id not in decision_ids
            ):
                raise ValueError("orphan_request")
            if intent_index[request.intent_id].decision_id != request.decision_id:
                raise ValueError("request_intent_decision_mismatch")
        for fill in self.fills:
            if fill.request_id not in request_index:
                raise ValueError("orphan_fill")
            request = request_index[fill.request_id]
            if (
                fill.decision_id != request.decision_id
                or fill.intent_id != request.intent_id
            ):
                raise ValueError("fill_request_lineage_mismatch")
            binding_violations = fill_request_binding_violations(request, fill)
            if binding_violations:
                raise ValueError(binding_violations[0])
            violations = fill_timeline_violations(fill)
            if violations:
                raise ValueError(violations[0])
        fill_request_ids = [fill.request_id for fill in self.fills]
        if len(fill_request_ids) != len(set(fill_request_ids)):
            raise ValueError("multiple_fills_for_execution_request")
        for entry in self.ledger_entries:
            ledger_fill = fill_index.get(entry.fill_id)
            if ledger_fill is None:
                raise ValueError("orphan_ledger_entry")
            if (
                ledger_fill.fill_status not in {"filled", "partial"}
                or float(ledger_fill.filled_qty) <= 0
            ):
                raise ValueError("invalid_ledger_fill")
            if (
                entry.side != ledger_fill.side
                or abs(float(entry.qty) - float(ledger_fill.filled_qty)) > 1e-8
                or abs(float(entry.fee) - float(ledger_fill.fee)) > 1e-8
                or entry.effective_ts != ledger_fill.portfolio_effective_ts
            ):
                raise ValueError("ledger_fill_value_mismatch")
        if len({entry.fill_id for entry in self.ledger_entries}) != len(
            self.ledger_entries
        ):
            raise ValueError("multiple_mutating_ledger_entries_for_fill")
        if set(fill_request_ids) != set(request_index):
            raise ValueError("execution_request_fill_bijection_mismatch")
        mutating_fills = {
            fill.fill_id
            for fill in self.fills
            if fill.fill_status in {"filled", "partial"}
            and float(fill.filled_qty) > 0
            and fill.portfolio_effective_ts is not None
        }
        applied = {entry.fill_id for entry in self.ledger_entries}
        pending = {
            str(trade.get("fill_id"))
            for trade in self.trades
            if trade.get("pending_execution_at_end") is True
            and trade.get("pending_execution_after_dataset_end") is True
        }
        if applied & pending:
            raise ValueError("fill_both_applied_and_pending")
        if mutating_fills != applied | pending:
            raise ValueError("mutating_fill_ledger_correspondence_mismatch")
        for trade in self.trades:
            if not trade.get("ledger_entry_id"):
                continue
            projected_fill = fill_index.get(str(trade.get("fill_id")))
            projected_entry = ledger_index.get(str(trade.get("ledger_entry_id")))
            if (
                projected_fill is None
                or projected_entry is None
                or projected_entry.fill_id != projected_fill.fill_id
            ):
                raise ValueError("trade_projection_lineage_mismatch")
            if (
                trade.get("side") != projected_fill.side
                or _float_or_zero(trade.get("qty")) != float(projected_fill.filled_qty)
                or trade.get("price") != projected_fill.avg_fill_price
            ):
                raise ValueError("trade_projection_value_mismatch")
