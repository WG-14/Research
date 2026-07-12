from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExecutionCalibrationThresholds:
    """Immutable quality gates for externally prepared calibration artifacts."""

    min_sample: int = 30
    max_p90_slippage_bps: float = 20.0
    max_p95_full_fill_latency_ms: float = 3000.0
    max_partial_fill_rate: float = 0.05
    max_model_breach_rate: float = 0.10
