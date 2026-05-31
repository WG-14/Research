from __future__ import annotations

import time
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from bithumb_bot.market_regime import RegimeCoverageRow, RegimePerformanceRow

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
    KiB-compatible best effort because AWS Linux is the production reference.
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


@dataclass
class BacktestRunContext:
    experiment_id: str = ""
    candidate_id: str = ""
    scenario_id: str = ""
    scenario_index: int | None = None
    split_name: str = ""
    report_detail: str = "full"
    resource_limits: BacktestResourceLimits = field(default_factory=BacktestResourceLimits)
    heartbeat: BacktestHeartbeatPolicy = field(default_factory=BacktestHeartbeatPolicy)
    progress_callback: ProgressCallback | None = None
    audit_trace: Any | None = None
    approved_profile: dict[str, object] | None = None
    candidate_regime_policy: dict[str, object] | None = None
    candidate_regime_policy_drives_research_execution: bool = False
    policy_materialization_mode: str = "research_exploratory"
    memory_sampler: MemorySampler = sample_process_memory
    started_at: float = field(default_factory=time.perf_counter)


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
