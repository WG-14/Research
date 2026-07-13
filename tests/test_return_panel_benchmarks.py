from __future__ import annotations

import pytest

from market_research.research.return_panel import build_candidate_return_panel


def _curve(values: tuple[float, ...]) -> list[dict[str, float | int]]:
    return [
        {"ts": index * 60_000, "equity": value, "cash": value, "asset_qty": 0.0}
        for index, value in enumerate(values)
    ]


def _candidate() -> dict[str, object]:
    return {
        "parameter_candidate_id": "candidate-1",
        "parameter_values": {},
        "validation_equity_curve": _curve((100.0, 110.0, 121.0)),
        "validation_metrics": {
            "benchmark_buy_and_hold_equity_curve": _curve((100.0, 105.0, 110.25)),
            "benchmark_configured_equity_curve": _curve((100.0, 102.0, 104.04)),
        },
        "scenario_results": [],
    }


@pytest.mark.parametrize(
    ("benchmark", "expected_benchmark_return", "expected_excess_return"),
    (("buy_and_hold", 5.0, 5.0), ("configured", 2.0, 8.0)),
)
def test_aligned_return_panel_uses_executable_benchmark_curve(
    benchmark: str,
    expected_benchmark_return: float,
    expected_excess_return: float,
) -> None:
    panel = build_candidate_return_panel(
        experiment_id="benchmark-panel",
        manifest_hash="sha256:" + "a" * 64,
        dataset_content_hash="sha256:" + "b" * 64,
        dataset_quality_hash="sha256:" + "c" * 64,
        split="validation",
        benchmark=benchmark,
        candidates=[_candidate()],
    )

    assert panel["statistical_evidence_available"] is True
    row = panel["candidate_return_series"][0]
    assert row["benchmark_return_series_values"][0]["return_pct"] == pytest.approx(
        expected_benchmark_return
    )
    assert row["excess_return_series_values"][0]["excess_return_pct"] == pytest.approx(
        expected_excess_return
    )


def test_aligned_return_panel_fails_closed_when_benchmark_curve_is_missing() -> None:
    candidate = _candidate()
    candidate["validation_metrics"] = {}

    panel = build_candidate_return_panel(
        experiment_id="benchmark-panel",
        manifest_hash="sha256:" + "a" * 64,
        dataset_content_hash="sha256:" + "b" * 64,
        dataset_quality_hash=None,
        split="validation",
        benchmark="configured",
        candidates=[candidate],
    )

    assert panel["statistical_evidence_available"] is False
    assert "aligned_bar_portfolio_return_panel_not_generated" in panel["limitations"]
