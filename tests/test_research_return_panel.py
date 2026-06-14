from __future__ import annotations

from copy import deepcopy

from bithumb_bot.research.return_panel import build_candidate_return_panel


def _candidate(order: list[str]) -> dict[str, object]:
    scenarios = {
        "stress": {
            "scenario_id": "scenario_stress",
            "scenario_role": "stress",
            "validation_closed_trades": [{"exit_ts": 1, "return_pct": -99.0}],
        },
        "base": {
            "scenario_id": "scenario_base",
            "scenario_role": "base",
            "validation_closed_trades": [{"exit_ts": 2, "return_pct": 3.5}],
        },
    }
    return {
        "parameter_candidate_id": "candidate_001",
        "parameter_values": {"SMA_SHORT": 2},
        "scenario_results": [deepcopy(scenarios[key]) for key in order],
    }


def _panel(candidate: dict[str, object]) -> dict[str, object]:
    return build_candidate_return_panel(
        experiment_id="return_panel_unit",
        manifest_hash="sha256:manifest",
        dataset_content_hash="sha256:dataset",
        dataset_quality_hash="sha256:quality",
        split="validation",
        benchmark="cash",
        candidates=[candidate],
    )


def test_return_panel_fallback_uses_base_scenario_closed_trades() -> None:
    panel = _panel(_candidate(["stress", "base"]))
    row = panel["candidate_return_series"][0]
    series = row["candidate_return_series_values"]

    assert series[0]["return_pct"] == 3.5
    assert series[0]["return_panel_scenario_role"] == "base"
    assert series[0]["return_panel_scenario_id"] == "scenario_base"
    assert series[0]["return_panel_series_source"] == "scenario_results.base.validation_closed_trades"


def test_return_panel_does_not_depend_on_scenario_results_order() -> None:
    stress_first = _panel(_candidate(["stress", "base"]))
    base_first = _panel(_candidate(["base", "stress"]))

    assert stress_first["candidate_return_series"][0]["candidate_return_series_hash"] == (
        base_first["candidate_return_series"][0]["candidate_return_series_hash"]
    )


def test_return_panel_fails_closed_when_base_scenario_series_missing() -> None:
    candidate = {
        "parameter_candidate_id": "candidate_001",
        "parameter_values": {"SMA_SHORT": 2},
        "scenario_results": [
            {
                "scenario_id": "scenario_stress",
                "scenario_role": "stress",
                "validation_closed_trades": [{"exit_ts": 1, "return_pct": -99.0}],
            }
        ],
    }

    panel = _panel(candidate)
    row = panel["candidate_return_series"][0]

    assert row["return_series_available"] is False
    assert row["candidate_return_series_values"] == []
