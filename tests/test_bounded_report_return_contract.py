from __future__ import annotations

import json
from dataclasses import replace

import pytest

from market_research.research.validation_protocol import (
    run_research_backtest,
    run_research_walk_forward,
)
from market_research.research_composition import builtin_strategy_registry
from tests.test_frozen_dataset_multi_split_integration import (
    frozen_manifest_and_manager,
)


@pytest.mark.parametrize("report_detail", ["index", "standard"])
@pytest.mark.parametrize(
    ("runner", "report_name", "walk_forward"),
    [
        (run_research_backtest, "backtest", False),
        (run_research_walk_forward, "walk_forward", True),
    ],
)
def test_bounded_report_keeps_full_candidates_only_in_memory(
    tmp_path,
    report_detail,
    runner,
    report_name,
    walk_forward,
) -> None:
    _, manifest, manager = frozen_manifest_and_manager(
        tmp_path,
        walk_forward=walk_forward,
    )
    manifest = replace(
        manifest,
        research_run=replace(
            manifest.research_run,
            report_detail=report_detail,
        ),
    )

    returned = runner(
        manifest=manifest,
        db_path=None,
        manager=manager,
        strategy_registry=builtin_strategy_registry(),
    )

    returned_candidate = returned["candidates"][0]
    assert isinstance(returned_candidate["compiled_strategy_contract"], dict)
    assert returned_candidate["scenario_results"]

    persisted_path = manager.report_path(
        "research",
        manifest.experiment_id,
        f"{report_name}_report.json",
    )
    persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
    persisted_candidate = persisted["candidates"][0]
    assert "compiled_strategy_contract" not in persisted_candidate
    assert "scenario_results" not in persisted_candidate
