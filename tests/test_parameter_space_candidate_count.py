from __future__ import annotations

from pathlib import Path

import pytest

import market_research.research.parameter_space as parameter_space_module
import market_research.research.workload_estimate as workload_estimate_module
from market_research.research.experiment_manifest import load_manifest
from market_research.research.parameter_space import (
    count_parameter_candidates,
    iter_parameter_candidates,
)
from market_research.research_composition import builtin_strategy_registry


def test_candidate_count_matches_cartesian_iteration_semantics() -> None:
    spaces = (
        {},
        {"ONLY": (1,)},
        {"B": (True, False), "A": (1, 2, 3)},
        {"EMPTY": ()},
        {"A": (1, 2), "EMPTY": (), "B": (3, 4, 5)},
    )

    for parameter_space in spaces:
        assert count_parameter_candidates(parameter_space) == len(
            iter_parameter_candidates(parameter_space)
        )


def test_candidate_count_handles_empty_and_large_dimensions_without_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_iteration(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("candidate grid must not be materialized to count it")

    monkeypatch.setattr(
        parameter_space_module,
        "iter_parameter_candidates",
        forbidden_iteration,
    )
    large_space = {
        f"PARAMETER_{index:03d}": (0, 1, 2, 3, 4)
        for index in range(128)
    }

    assert count_parameter_candidates({}) == 1
    assert count_parameter_candidates({"EMPTY": ()}) == 0
    assert count_parameter_candidates(large_space) == 5**128


def test_workload_estimate_does_not_iterate_parameter_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = load_manifest(
        Path("examples/research/sma_filter_manifest.example.json"),
        registry=builtin_strategy_registry(),
    )
    expected_count = count_parameter_candidates(manifest.parameter_space)

    def forbidden_iteration(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("workload estimate must not materialize candidates")

    monkeypatch.setattr(
        parameter_space_module,
        "iter_parameter_candidates",
        forbidden_iteration,
    )
    if hasattr(workload_estimate_module, "iter_parameter_candidates"):
        monkeypatch.setattr(
            workload_estimate_module,
            "iter_parameter_candidates",
            forbidden_iteration,
        )

    estimate = workload_estimate_module.build_manifest_workload_estimate(manifest)

    assert estimate["candidate_count"] == expected_count == 81
    assert estimate["work_unit_count"] == (
        expected_count * estimate["scenario_count"]
    )
