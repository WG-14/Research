from __future__ import annotations

from bithumb_bot.research import validation_protocol
from bithumb_bot.research.execution_plan import scenario_id
from bithumb_bot.research.executor import (
    ResearchWorkResult,
    canonical_work_results_content_hash,
    canonical_work_results_payload,
    sort_work_results_deterministically,
)
from bithumb_bot.research.experiment_manifest import parse_manifest
from bithumb_bot.research.parameter_space import candidate_id
from tests.factories.research_reports import (
    DeterministicResearchEvaluator,
    minimal_candidate_payload,
    minimal_metrics,
    minimal_research_report,
    minimal_scenario_result,
)
from tests.test_research_backtest_reproducibility import _manifest, _snapshot_from_closes


def _work_unit(*, candidate_index: int, scenario_index: int, params: dict[str, object] | None = None):
    manifest = parse_manifest(_manifest())
    snapshots = {
        "train": _snapshot_from_closes([100.0, 101.0, 102.0]),
        "validation": _snapshot_from_closes([100.0, 99.0, 101.0]),
    }
    scenario = manifest.execution_model.scenarios[scenario_index]
    scenario_id = f"scenario_{scenario_index}"
    return validation_protocol.build_research_work_unit(
        manifest=manifest,
        snapshots=snapshots,
        params=params or {"SMA_SHORT": 2, "SMA_LONG": 4},
        candidate_index=candidate_index,
        scenario=scenario,
        scenario_index=scenario_index,
        scenario_id=scenario_id,
        manifest_hash=manifest.manifest_hash(),
        simulation_seed_scope_hash=manifest.simulation_seed_scope_hash(),
    )


def _scenario_manifest(*, base_fee: float = 0.0004, stress_fee: float = 0.0025):
    payload = _manifest()
    payload["execution_model"] = {
        "scenario_policy": "must_pass_base_and_survive_stress",
        "scenarios": [
            {
                "type": "fixed_bps",
                "scenario_role": "base",
                "label": "base",
                "fee_rate": base_fee,
                "fee_source": "operator_declared_bithumb_app_fee",
                "fee_authority_policy": "runtime_fee_authority_must_match_or_fail",
                "slippage_bps": 10,
                "slippage_source": "execution_calibration",
                "promotable_as_base": True,
            },
            {
                "type": "fixed_bps",
                "scenario_role": "stress",
                "label": "stress",
                "fee_rate": stress_fee,
                "fee_source": "stress_assumption",
                "fee_authority_policy": "not_promotable_as_runtime_base",
                "slippage_bps": 20,
                "slippage_source": "stress_assumption",
                "promotable_as_base": False,
            },
        ],
    }
    return parse_manifest(payload)


def _scenario_work_unit(manifest, role: str):
    snapshots = {
        "train": _snapshot_from_closes([100.0, 101.0, 102.0]),
        "validation": _snapshot_from_closes([100.0, 99.0, 101.0]),
    }
    scenario_index, scenario = next(
        (index, scenario)
        for index, scenario in enumerate(manifest.execution_model.scenarios)
        if scenario.scenario_role == role
    )
    return validation_protocol.build_research_work_unit(
        manifest=manifest,
        snapshots=snapshots,
        params={"SMA_SHORT": 2, "SMA_LONG": 4},
        candidate_index=0,
        scenario=scenario,
        scenario_index=scenario_index,
        scenario_id=scenario_id(scenario, scenario_index),
        manifest_hash=manifest.manifest_hash(),
        simulation_seed_scope_hash=manifest.simulation_seed_scope_hash(),
    )


def _completed_result(*, candidate_index: int, scenario_index: int) -> ResearchWorkResult:
    work_unit = _work_unit(candidate_index=candidate_index, scenario_index=scenario_index)
    return ResearchWorkResult(
        work_unit=work_unit,
        work_unit_hash=work_unit.work_unit_hash,
        candidate_index=work_unit.candidate_index,
        candidate_id=work_unit.candidate_id,
        scenario_index=work_unit.scenario_index,
        scenario_id=work_unit.scenario_id,
        status="completed",
        base_result={"candidate_id": work_unit.candidate_id},
        observability={"completion_order": 99 - candidate_index},
    )


def test_serial_and_parallel_completion_order_normalize_to_deterministic_work_result_order() -> None:
    serial_results = [
        _completed_result(candidate_index=0, scenario_index=0),
        _completed_result(candidate_index=1, scenario_index=0),
    ]
    parallel_completion_order = list(reversed(serial_results))

    assert [
        (result.scenario_index, result.candidate_index)
        for result in sort_work_results_deterministically(parallel_completion_order)
    ] == [(0, 0), (0, 1)]
    assert canonical_work_results_payload(serial_results) == canonical_work_results_payload(parallel_completion_order)
    assert canonical_work_results_content_hash(serial_results) == canonical_work_results_content_hash(
        parallel_completion_order
    )


def test_failed_work_result_content_hash_is_stable_across_completion_order() -> None:
    completed = _completed_result(candidate_index=0, scenario_index=0)
    failed_unit = _work_unit(candidate_index=1, scenario_index=0)
    failed = ResearchWorkResult(
        work_unit=failed_unit,
        work_unit_hash=failed_unit.work_unit_hash,
        candidate_index=failed_unit.candidate_index,
        candidate_id=failed_unit.candidate_id,
        scenario_index=failed_unit.scenario_index,
        scenario_id=failed_unit.scenario_id,
        status="failed",
        failure_reason="parallel_executor_exception",
        failure_evidence={
            "phase": "future_result",
            "work_unit_hash": failed_unit.work_unit_hash,
            "exception_type": "RuntimeError",
        },
    )

    assert canonical_work_results_content_hash([completed, failed]) == canonical_work_results_content_hash(
        [failed, completed]
    )
    payload = canonical_work_results_payload([failed])[0]
    assert payload["failure_reason"] == "parallel_executor_exception"
    assert str(payload["failure_evidence_hash"]).startswith("sha256:")


def test_candidate_ranking_is_independent_of_synthetic_result_input_order() -> None:
    high_return_metrics = minimal_metrics(return_pct=2.0, max_drawdown_pct=1.0)
    low_return_metrics = minimal_metrics(return_pct=1.0, max_drawdown_pct=1.0)
    first = minimal_candidate_payload(
        parameter_candidate_id="candidate_a",
        validation_metrics=low_return_metrics,
        scenario_results=[minimal_scenario_result(validation_metrics=low_return_metrics)],
    )
    second = minimal_candidate_payload(
        parameter_candidate_id="candidate_b",
        validation_metrics=high_return_metrics,
        scenario_results=[minimal_scenario_result(validation_metrics=high_return_metrics)],
    )

    ranked = sorted([first, second], key=validation_protocol._candidate_rank_key)
    reranked = sorted([second, first], key=validation_protocol._candidate_rank_key)

    assert [candidate["parameter_candidate_id"] for candidate in ranked] == ["candidate_b", "candidate_a"]
    assert [candidate["parameter_candidate_id"] for candidate in reranked] == ["candidate_b", "candidate_a"]


def test_scenario_policy_application_uses_synthetic_scenario_results() -> None:
    payload = _manifest()
    payload["execution_model"] = {
        "type": "stress",
        "fee_rate": [0.0],
        "slippage_bps": [0.0],
        "order_failure_rate": [0.0, 1.0],
        "scenario_policy": "must_pass_base_and_survive_stress",
        "seed": 42,
    }
    manifest = parse_manifest(payload)
    candidate = minimal_candidate_payload(
        scenario_policy="must_pass_base_and_survive_stress",
        scenario_results=[
            minimal_scenario_result(
                scenario_id="base",
                scenario_index=0,
                scenario_role="base",
                scenario_acceptance_gate_result="PASS",
            ),
            minimal_scenario_result(
                scenario_id="stress",
                scenario_index=1,
                scenario_role="stress",
                scenario_acceptance_gate_result="FAIL",
                scenario_fail_reasons=["synthetic_stress_failure"],
            ),
        ],
    )

    validation_protocol._apply_scenario_policy(manifest=manifest, candidate=candidate)

    assert candidate["acceptance_gate_result"] == "FAIL"
    assert candidate["scenario_pass_count"] == 1
    assert candidate["scenario_fail_count"] == 1
    assert "scenario_policy_required_scenario_failed:stress:synthetic_stress_failure" in candidate["gate_fail_reasons"]
    assert "scenario_policy_no_passing_stress_scenario" in candidate["gate_fail_reasons"]


def test_candidate_payload_declares_primary_and_aggregate_semantics() -> None:
    base_metrics = minimal_metrics(return_pct=3.0)
    stress_metrics = minimal_metrics(return_pct=-1.0)
    candidate = minimal_candidate_payload(
        acceptance_gate_result="FAIL",
        aggregate_acceptance_gate_result="FAIL",
        validation_metrics=base_metrics,
        primary_validation_metrics=base_metrics,
        primary_scenario_role="base",
        primary_metric_source="primary_base_scenario_alias",
        aggregate_gate_policy="must_pass_base_and_survive_stress",
        scenario_results=[
            minimal_scenario_result(
                scenario_id="base",
                scenario_role="base",
                validation_metrics=base_metrics,
                scenario_acceptance_gate_result="PASS",
            ),
            minimal_scenario_result(
                scenario_id="stress",
                scenario_role="stress",
                validation_metrics=stress_metrics,
                scenario_acceptance_gate_result="FAIL",
            ),
        ],
    )

    assert candidate["primary_scenario_role"] == "base"
    assert candidate["aggregate_acceptance_gate_result"] == "FAIL"
    assert candidate["acceptance_gate_result"] == candidate["aggregate_acceptance_gate_result"]
    assert candidate["validation_metrics"] == candidate["primary_validation_metrics"]
    assert candidate["validation_metrics"] != stress_metrics


def test_work_unit_hash_changes_when_only_scenario_fee_changes() -> None:
    original = _scenario_work_unit(_scenario_manifest(base_fee=0.0004), "base")
    changed = _scenario_work_unit(_scenario_manifest(base_fee=0.0005), "base")
    stress_original = _scenario_work_unit(_scenario_manifest(base_fee=0.0004), "stress")
    stress_unchanged = _scenario_work_unit(_scenario_manifest(base_fee=0.0005), "stress")

    assert original.work_unit_hash != changed.work_unit_hash
    assert original.execution_model_hash != changed.execution_model_hash
    assert stress_original.scenario_id == stress_unchanged.scenario_id
    assert stress_original.execution_model_hash == stress_unchanged.execution_model_hash


def test_scenario_id_changes_when_scenario_cost_changes() -> None:
    original = _scenario_work_unit(_scenario_manifest(stress_fee=0.0025), "stress")
    changed = _scenario_work_unit(_scenario_manifest(stress_fee=0.0030), "stress")
    base_original = _scenario_work_unit(_scenario_manifest(stress_fee=0.0025), "base")
    base_unchanged = _scenario_work_unit(_scenario_manifest(stress_fee=0.0030), "base")

    assert original.scenario_id != changed.scenario_id
    assert original.execution_model_hash != changed.execution_model_hash
    assert base_original.scenario_id == base_unchanged.scenario_id
    assert base_original.execution_model_hash == base_unchanged.execution_model_hash


def test_stress_seed_metadata_is_derived_without_full_research_runner() -> None:
    payload = _manifest()
    payload["execution_model"] = {
        "type": "stress",
        "fee_rate": [0.0],
        "slippage_bps": [5],
        "partial_fill_rate": [0.5],
        "order_failure_rate": [0.1],
        "scenario_policy": "must_pass_base_and_survive_stress",
        "seed": 42,
    }
    manifest = parse_manifest(payload)
    snapshots = {
        "train": _snapshot_from_closes([100.0, 101.0, 102.0]),
        "validation": _snapshot_from_closes([100.0, 99.0, 101.0]),
    }
    target_params = {
        "SMA_SHORT": 2,
        "SMA_LONG": 4,
        "SMA_FILTER_GAP_MIN_RATIO": 0.0,
        "SMA_FILTER_VOL_MIN_RANGE_RATIO": 0.0,
    }
    target_id = candidate_id(target_params, 0)
    work_unit = validation_protocol.build_research_work_unit(
        manifest=manifest,
        snapshots=snapshots,
        params=target_params,
        candidate_index=0,
        scenario=manifest.execution_model.scenarios[0],
        scenario_index=0,
        scenario_id="scenario_0",
        manifest_hash=manifest.manifest_hash(),
        simulation_seed_scope_hash=manifest.simulation_seed_scope_hash(),
    )
    evaluator = DeterministicResearchEvaluator()
    result = evaluator.evaluate(
        work_unit,
        validation_protocol.EvaluationContext(
            manifest=manifest,
            manager=None,
            snapshots=snapshots,
            manifest_hash=manifest.manifest_hash(),
            simulation_seed_scope_hash=manifest.simulation_seed_scope_hash(),
            include_walk_forward=False,
            raw_candidate_count=1,
            params=target_params,
            candidate_index=0,
            scenario=manifest.execution_model.scenarios[0],
            scenario_index=0,
            scenario_id="scenario_0",
            progress_callback=None,
            worker_pid=None,
        ),
    )

    execution = result.base_result["validation_execution_metadata"][0]  # type: ignore[index]
    assert execution["base_seed"] == 42
    assert execution["seed_derivation_inputs"]["parameter_candidate_id"] == target_id
    assert execution["derived_seed_hash"].startswith("sha256:")


def test_contract_report_execution_boundary_observability_is_in_process() -> None:
    report = minimal_research_report(
        execution_observability={
            "contract_evaluator_used": True,
            "production_evaluator_used": False,
            "parallel_executor_used": False,
            "actual_execution_mode": "contract_evaluator_in_process",
        }
    )

    assert report["execution_observability"]["contract_evaluator_used"] is True
    assert report["execution_observability"]["production_evaluator_used"] is False
    assert report["execution_observability"]["parallel_executor_used"] is False
    assert report["execution_observability"]["actual_execution_mode"] == "contract_evaluator_in_process"
