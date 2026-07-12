from __future__ import annotations

import time
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

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


@dataclass(frozen=True)
class MemorySample:
    current_rss_mb: float | None
    peak_rss_mb: float | None
    source: str
    peak_rss_source_units: str | None = None
    peak_rss_platform: str | None = None


def ru_maxrss_to_mb(raw_value: float, *, platform: str | None = None) -> tuple[float, str]:
    """Convert getrusage ru_maxrss using platform-defined units.

    Linux reports KiB and Darwin reports bytes. Other platforms are surfaced as
    KiB-compatible best effort because AWS Linux is the validation reference.
    """
    platform_name = (platform or sys.platform).lower()
    if platform_name.startswith("darwin"):
        return round(float(raw_value) / (1024.0 * 1024.0), 3), "bytes"
    if platform_name.startswith("linux"):
        return round(float(raw_value) / 1024.0, 3), "kib"
    return round(float(raw_value) / 1024.0, 3), f"kib_assumed_for_platform:{platform_name}"


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
        elif str(policy_materialization_mode or "").strip().lower() == "research_validation":
            policy_name = "validation_evidence"
        elif str(diagnostic_mode or "").strip().lower() == "exploratory":
            policy_name = "diagnostic_sampled"
        elif str(report_detail or "").strip().lower() in {"index", "summary", "standard"}:
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
            audit_decision="per_tick" if audit_mode == "complete_external" else "aggregate",
            audit_equity_mark="per_tick" if audit_mode == "complete_external" else "aggregate",
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
    resource_limits: BacktestResourceLimits = field(default_factory=BacktestResourceLimits)
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
    decisions: tuple[dict[str, object], ...] = ()
    equity_curve: tuple[EquityPoint, ...] = ()
    position_intervals: tuple[PositionInterval, ...] = ()
    closed_trades: tuple[ClosedTradeRecord, ...] = ()
    metrics_v2: MetricContractV2 | None = None
    resource_usage: dict[str, object] | None = None
    strategy_diagnostics: dict[str, object] | None = None
    retained_detail_summary: dict[str, object] | None = None
    audit_trace_index: dict[str, object] | None = None
    # Authoritative, independently hashable execution lineage streams.
    order_intents: tuple[object, ...] = ()
    execution_requests: tuple[object, ...] = ()
    fills: tuple[object, ...] = ()
    ledger_entries: tuple[object, ...] = ()
