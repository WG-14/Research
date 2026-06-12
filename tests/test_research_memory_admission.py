from __future__ import annotations

import pytest

from bithumb_bot.research.experiment_manifest import parse_manifest
from bithumb_bot.research.workload_estimate import build_manifest_workload_estimate
from tests.test_research_backtest_reproducibility import _manifest


def _manifest_with_workers(max_workers: int, *, entry_modes: list[str] | None = None, max_total_memory_mb: float | None = None):
    payload = _manifest()
    payload["strategy_name"] = "channel_breakout_with_regime_filter"
    payload["parameter_space"] = {
        "CHANNEL_BREAKOUT_LOOKBACK": [3],
        "CHANNEL_BREAKOUT_RANGE_WINDOW": [3],
        "CHANNEL_BREAKOUT_VOLUME_WINDOW": [3],
        "ENTRY_MODE": entry_modes or ["immediate_breakout"],
    }
    payload["research_run"] = {
        "execution": {"mode": "parallel", "max_workers": max_workers},
        "resource_limits": {},
    }
    if max_total_memory_mb is not None:
        payload["research_run"]["resource_limits"]["max_total_memory_mb"] = max_total_memory_mb
    return parse_manifest(payload)


@pytest.mark.contract
@pytest.mark.resource_guard
def test_workload_estimate_includes_parallel_snapshot_fanout_bytes() -> None:
    one = build_manifest_workload_estimate(_manifest_with_workers(2))
    eight = build_manifest_workload_estimate(_manifest_with_workers(8))

    assert one["estimated_snapshot_bytes_per_worker"] > 0
    assert eight["estimated_parallel_snapshot_fanout_bytes"] > one[
        "estimated_parallel_snapshot_fanout_bytes"
    ]
    assert eight["max_in_flight_tasks"] == 16


@pytest.mark.contract
@pytest.mark.resource_guard
def test_memory_admission_fails_when_estimated_parent_and_worker_bytes_exceed_budget() -> None:
    estimate = build_manifest_workload_estimate(_manifest_with_workers(8, max_total_memory_mb=1.0))

    assert estimate["memory_budget_status"] == "WARN"
    assert "estimated_parent_and_worker_bytes_exceed_memory_budget" in estimate["memory_budget_reasons"]


@pytest.mark.contract
@pytest.mark.resource_guard
def test_memory_admission_caps_effective_workers_when_policy_is_cap_workers() -> None:
    estimate = build_manifest_workload_estimate(_manifest_with_workers(8, max_total_memory_mb=1.0))

    assert estimate["safe_max_workers_by_memory_budget"] >= 1
    assert estimate["safe_max_workers_by_memory_budget"] < 8


@pytest.mark.contract
@pytest.mark.resource_guard
def test_delayed_confirmation_parameter_space_increases_estimated_payload_bytes() -> None:
    immediate = build_manifest_workload_estimate(
        _manifest_with_workers(2, entry_modes=["immediate_breakout"])
    )
    delayed = build_manifest_workload_estimate(
        _manifest_with_workers(2, entry_modes=["immediate_breakout", "delayed_confirmation"])
    )

    assert delayed["estimated_event_materialization_bytes_per_split"] > immediate[
        "estimated_event_materialization_bytes_per_split"
    ]
