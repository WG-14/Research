from __future__ import annotations

import math
import copy
from dataclasses import replace
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.backtest_types import BacktestRun
from market_research.research.dataset_snapshot import Candle, DatasetSnapshot
from market_research.research.experiment_manifest import DateRange
from market_research.research.experiment_registry import (
    final_holdout_authority_scope_hash,
)
from market_research.research.final_selection import (
    build_selection_artifact,
    validate_final_selection_report,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.metrics import ResearchMetrics
from market_research.research.metrics_contract import EquityPoint
from market_research.research.parameter_space import candidate_id, iter_parameter_candidates
from market_research.research.validation_experiment_bundle import (
    derive_validation_experiment_capability,
)
from market_research.research.validation_experiment_execution import (
    ManifestCandidateRangeEvaluation,
    NativeValidationExperimentExecutionError,
    execute_manifest_validation_experiments,
    native_candidate_definition_hash,
    validate_native_validation_computation_receipt,
)
from market_research.research.validation_pipeline import (
    ValidationRunError,
    _freeze_native_nested_selection_artifact,
    _native_nested_final_selection_result,
    _native_nested_selection_artifact_reasons,
    _pre_holdout_gate_reasons,
    aggregate_validation_gates,
    run_research_validation,
)
from market_research.research.cli import (
    _run_terminal_validation_experiment_reproduction,
)
from market_research.research_composition import (
    builtin_strategy_registry,
    parse_builtin_manifest,
)
from market_research.settings import ResearchSettings
from tests.test_validation_experiment_manifest_contract import (
    _payload_with_validation_experiments,
)
from tests.test_validation_experiments import _nested_plan


HASH_A = "sha256:" + "a" * 64


def _manager(tmp_path: Path) -> ResearchPathManager:
    return ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "data",
            artifact_root=tmp_path / "artifacts",
            report_root=tmp_path / "reports",
            cache_root=tmp_path / "cache",
            db_path=tmp_path / "unused.sqlite",
            max_workers=1,
            random_seed=0,
        ),
        project_root=Path.cwd(),
    )


def _manifest():
    payload = _payload_with_validation_experiments()
    payload["parameter_space"] = {"NOOP_DECISION_START_INDEX": [0, 1]}
    dataset = payload["dataset"]
    assert isinstance(dataset, dict)
    dataset.update(
        {
            "train": {"start": "2025-01-01", "end": "2025-02-28"},
            "validation": {"start": "2025-03-01", "end": "2025-03-20"},
            "final_holdout": {"start": "2025-03-21", "end": "2025-03-31"},
        }
    )
    experiments = payload["validation_experiments"]
    assert isinstance(experiments, dict)
    falsification = experiments["falsification"]
    assert isinstance(falsification, dict)
    falsification.update(
        {
            "minimum_sample_count": 20,
            "minimum_baseline_abs_effect": 0.0,
            "maximum_control_abs_effect": 1.0,
            "minimum_confounder_adjusted_retention": 0.0,
        }
    )
    provider = experiments["provider_sensitivity"]
    assert isinstance(provider, dict)
    for tolerance in provider["tolerances"]:
        tolerance["absolute_tolerance"] = 100.0
        tolerance["relative_tolerance"] = 100.0
    nested = experiments["nested_selection"]
    assert isinstance(nested, dict)
    nested["minimum_inner_sample_count"] = 5
    nested["minimum_outer_sample_count"] = 5
    return replace(
        parse_builtin_manifest(payload),
        research_classification="validated_candidate",
    )


def _fake_range_evaluation(**kwargs):
    manifest = kwargs["manifest"]
    candidate_id = kwargs["candidate_id"]
    date_range = kwargs["date_range"]
    split_name = kwargs["split_name"]
    prices = [100.0]
    for index in range(1, 31):
        prices.append(prices[-1] * (1.0 + math.sin(index * 0.73) * 0.01))
    candles = tuple(
        Candle(
            ts=1_700_000_000_000 + index * 3_600_000,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=1.0,
        )
        for index, price in enumerate(prices)
    )
    points = []
    for index, candle in enumerate(candles):
        next_return = (
            candles[index + 1].close / candle.close - 1.0
            if index + 1 < len(candles)
            else 0.0
        )
        exposure = 0.5 + next_return * 10.0
        points.append(
            EquityPoint(
                ts=candle.available_at_ms(interval=manifest.interval),
                equity=100.0,
                cash=50.0,
                asset_qty=exposure * 100.0 / candle.close,
                mark_price=candle.close,
                mark_price_source="candle_close",
            )
        )
    artifact_ref = manifest.dataset.artifact_ref
    assert artifact_ref is not None
    provider_bias = 0.001 if artifact_ref.artifact_manifest_hash != HASH_A else 0.0
    candidate_score = (
        2.0
        if kwargs["parameter_values"]["NOOP_DECISION_START_INDEX"] == 0
        else 1.0
    )
    metrics = ResearchMetrics(
        return_pct=candidate_score + provider_bias,
        max_drawdown_pct=0.5 + provider_bias,
        profit_factor=2.0,
        trade_count=10,
        win_rate=0.6,
        avg_win=1.0,
        avg_loss=-0.5,
        fee_total=0.1,
        slippage_total=0.1,
        max_consecutive_losses=1,
        single_trade_dependency_score=0.2,
        parameter_stability_score=None,
    )
    snapshot = DatasetSnapshot(
        snapshot_id="native-test-snapshot",
        source="frozen_sqlite_candles",
        market=manifest.market,
        interval=manifest.interval,
        split_name=split_name,
        date_range=date_range,
        candles=candles,
        artifact_id="immutable-candle:test",
        artifact_content_hash=sha256_prefixed(
            {"artifact": artifact_ref.artifact_manifest_hash}
        ),
        artifact_schema_hash=sha256_prefixed({"schema": 1}),
        artifact_manifest_hash=artifact_ref.artifact_manifest_hash,
        source_provenance_hash=sha256_prefixed({"provenance": 1}),
    )
    run = BacktestRun(
        metrics=metrics,
        trades=(),
        candle_count=len(candles),
        warnings=(),
        equity_curve=tuple(points),
        metrics_hash=sha256_prefixed(metrics.as_dict()),
        decision_stream_hash=sha256_prefixed(
            {"candidate": candidate_id, "split": split_name}
        ),
    )
    evidence_hash = sha256_prefixed(
        {
            "manifest_hash": manifest.manifest_hash(),
            "candidate_id": candidate_id,
            "split_name": split_name,
            "date_range": date_range.as_dict(),
            "metrics": metrics.as_dict(),
            "snapshot": snapshot.snapshot_fingerprint_hash(),
        },
        label="fake_native_range_evaluation",
    )
    return ManifestCandidateRangeEvaluation(
        split_name=split_name,
        candidate_id=candidate_id,
        parameter_values=dict(kwargs["parameter_values"]),
        snapshot=snapshot,
        run=run,
        scenario_id="scenario-1",
        scenario_index=0,
        compiled_strategy_contract_hash=sha256_prefixed(
            {"compiled": kwargs["parameter_values"]}
        ),
        evidence_hash=evidence_hash,
    )


def _candidate(candidate_id: str, start_index: int) -> dict[str, object]:
    parameters = {"NOOP_DECISION_START_INDEX": start_index}
    return {
        "parameter_candidate_id": candidate_id,
        "parameter_values": parameters,
        "parameter_values_raw": parameters,
        "effective_strategy_parameters_hash": sha256_prefixed(
            {"effective": parameters}
        ),
        "compiled_strategy_contract_hash": sha256_prefixed(
            {"compiled": parameters}
        ),
        "acceptance_gate_result": "PASS",
    }


def _candidates(manifest) -> list[dict[str, object]]:
    return [
        _candidate(candidate_id(parameters, index), int(parameters["NOOP_DECISION_START_INDEX"]))
        for index, parameters in enumerate(
            iter_parameter_candidates(manifest.parameter_space)
        )
    ]


def test_manifest_native_execution_is_deterministic_receipted_and_pre_holdout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from market_research.research import validation_experiment_execution as module

    manifest = _manifest()
    monkeypatch.setattr(
        module,
        "build_manifest_nested_temporal_validation_plan",
        lambda _manifest: _nested_plan(),
    )
    monkeypatch.setattr(module, "run_manifest_candidate_range", _fake_range_evaluation)
    candidates = _candidates(manifest)
    expected_selected_id = str(candidates[0]["parameter_candidate_id"])
    capability = derive_validation_experiment_capability(
        manifest_hash=manifest.manifest_hash(),
        research_classification=manifest.research_classification,
    )
    kwargs = {
        "manifest": manifest,
        "db_path": None,
        "manager": _manager(tmp_path),
        "candidates": candidates,
        "preliminary_selection_artifact_hash": sha256_prefixed(
            {"preliminary_selection": 1}
        ),
        "dataset_snapshot_hash": sha256_prefixed({"dataset": "selection"}),
        "capability": capability,
        "strategy_registry": builtin_strategy_registry(),
    }

    first = execute_manifest_validation_experiments(**kwargs)
    repeated = execute_manifest_validation_experiments(**kwargs)

    assert first.bundle.gate_result.value == "PASS"
    assert first.selected_candidate_id == expected_selected_id
    assert first.bundle.selected_candidate_id == expected_selected_id
    assert first.bundle.content_hash == repeated.bundle.content_hash
    assert first.computation_receipt_hash == repeated.computation_receipt_hash
    assert Path(first.computation_receipt_path).is_file()
    assert first.computation_receipt["holdout_accessed"] is False
    assert all(
        "holdout" not in item["split_name"]
        for item in first.computation_receipt["source_access_records"]
    )
    assert validate_native_validation_computation_receipt(
        first.computation_receipt,
        expected_manifest_hash=manifest.manifest_hash(),
        expected_bundle_hash=first.bundle.content_hash,
        expected_bundle=first.bundle.as_dict(),
        expected_selected_candidate_id=expected_selected_id,
        expected_selected_candidate_hash=first.selected_candidate_hash,
    ) == []


def test_native_computation_receipt_rejects_tamper_rebind_and_downgrade(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from market_research.research import validation_experiment_execution as module

    manifest = _manifest()
    monkeypatch.setattr(
        module,
        "build_manifest_nested_temporal_validation_plan",
        lambda _manifest: _nested_plan(),
    )
    monkeypatch.setattr(module, "run_manifest_candidate_range", _fake_range_evaluation)
    capability = derive_validation_experiment_capability(
        manifest_hash=manifest.manifest_hash(),
        research_classification=manifest.research_classification,
    )
    execution = execute_manifest_validation_experiments(
        manifest=manifest,
        db_path=None,
        manager=_manager(tmp_path),
        candidates=_candidates(manifest),
        preliminary_selection_artifact_hash=sha256_prefixed(
            {"preliminary_selection": 1}
        ),
        dataset_snapshot_hash=sha256_prefixed({"dataset": "selection"}),
        capability=capability,
        strategy_registry=builtin_strategy_registry(),
    )
    receipt = execution.computation_receipt
    assert validate_native_validation_computation_receipt(
        receipt,
        expected_bundle=execution.bundle.as_dict(),
    ) == []

    tampered = {**receipt, "selected_candidate_id": "candidate_forged"}
    assert "native_validation_computation_receipt_hash_mismatch" in (
        validate_native_validation_computation_receipt(tampered)
    )
    rehashed_tamper_material = {
        key: value for key, value in tampered.items() if key != "content_hash"
    }
    tampered["content_hash"] = sha256_prefixed(
        rehashed_tamper_material,
        label="native_validation_experiment_computation_receipt",
    )
    rehashed_reasons = validate_native_validation_computation_receipt(tampered)
    assert "native_validation_selected_candidate_pre_holdout_ineligible" in (
        rehashed_reasons
    )
    assert "native_validation_terminal_selection_scores_invalid" in rehashed_reasons
    downgraded = {**receipt, "execution_authority": "caller_precomputed_bundle"}
    downgraded_material = {
        key: value for key, value in downgraded.items() if key != "content_hash"
    }
    downgraded["content_hash"] = sha256_prefixed(
        downgraded_material,
        label="native_validation_experiment_computation_receipt",
    )
    assert "native_validation_computation_authority_invalid" in (
        validate_native_validation_computation_receipt(downgraded)
    )
    assert "native_validation_computation_manifest_hash_mismatch" in (
        validate_native_validation_computation_receipt(
            receipt,
            expected_manifest_hash=sha256_prefixed({"other": "manifest"}),
        )
    )


def test_native_universe_uses_no_prior_outcome_or_rank_fields(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from market_research.research import validation_experiment_execution as module

    manifest = _manifest()
    monkeypatch.setattr(
        module,
        "build_manifest_nested_temporal_validation_plan",
        lambda _manifest: _nested_plan(),
    )
    monkeypatch.setattr(module, "run_manifest_candidate_range", _fake_range_evaluation)
    capability = derive_validation_experiment_capability(
        manifest_hash=manifest.manifest_hash(),
        research_classification=manifest.research_classification,
    )
    pristine = _candidates(manifest)
    mutated = copy.deepcopy(pristine)
    for index, item in enumerate(mutated):
        item.update(
            {
                "acceptance_gate_result": "FAIL",
                "candidate_failed_before_complete_metrics": True,
                "evaluation_status": "resource_limited",
                "final_holdout_metrics": {"return_pct": 10_000 + index},
                "final_selection_rank": 999 - index,
                "metrics_status": "failed",
                "resource_integrity_status": "FAIL",
                "simulation_integrity_status": "FAIL",
                "strategy_performance_gate_status": "FAIL",
            }
        )
    common = {
        "manifest": manifest,
        "db_path": None,
        "manager": _manager(tmp_path),
        "preliminary_selection_artifact_hash": sha256_prefixed(
            {"preliminary_selection": 1}
        ),
        "dataset_snapshot_hash": sha256_prefixed({"dataset": "selection"}),
        "capability": capability,
        "strategy_registry": builtin_strategy_registry(),
    }
    first = execute_manifest_validation_experiments(candidates=pristine, **common)
    second = execute_manifest_validation_experiments(candidates=mutated, **common)

    assert first.selected_candidate_id == second.selected_candidate_id
    assert first.selected_candidate_hash == second.selected_candidate_hash
    assert first.manifest_candidate_universe_hash == (
        second.manifest_candidate_universe_hash
    )
    assert first.pre_holdout_eligibility_hash == second.pre_holdout_eligibility_hash
    assert first.bundle.content_hash == second.bundle.content_hash
    assert first.computation_receipt_hash == second.computation_receipt_hash
    assert all(item["status"] == "PASS" for item in second.pre_holdout_eligibility)


@pytest.mark.parametrize("attack", ["remove", "add", "rebind"])
def test_native_universe_rejects_candidate_removal_addition_and_identity_rebind(
    tmp_path: Path,
    monkeypatch,
    attack: str,
) -> None:
    from market_research.research import validation_experiment_execution as module

    manifest = _manifest()
    monkeypatch.setattr(
        module,
        "build_manifest_nested_temporal_validation_plan",
        lambda _manifest: _nested_plan(),
    )
    monkeypatch.setattr(module, "run_manifest_candidate_range", _fake_range_evaluation)
    candidates = _candidates(manifest)
    if attack == "remove":
        candidates.pop()
    elif attack == "add":
        forged = copy.deepcopy(candidates[0])
        forged["parameter_candidate_id"] = "candidate_forged"
        candidates.append(forged)
    else:
        candidates[0]["parameter_values"] = dict(candidates[1]["parameter_values"])
        candidates[0]["parameter_values_raw"] = dict(
            candidates[1]["parameter_values_raw"]
        )
    capability = derive_validation_experiment_capability(
        manifest_hash=manifest.manifest_hash(),
        research_classification=manifest.research_classification,
    )

    with pytest.raises(
        NativeValidationExperimentExecutionError,
        match=(
            "validation_experiments_manifest_candidate_identity_"
            "(?:mismatch|rebound)"
        ),
    ):
        execute_manifest_validation_experiments(
            manifest=manifest,
            db_path=None,
            manager=_manager(tmp_path),
            candidates=candidates,
            preliminary_selection_artifact_hash=sha256_prefixed(
                {"preliminary_selection": 1}
            ),
            dataset_snapshot_hash=sha256_prefixed({"dataset": "selection"}),
            capability=capability,
            strategy_registry=builtin_strategy_registry(),
        )


def test_native_plan_touching_final_holdout_fails_before_any_dataset_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from market_research.research import validation_experiment_execution as module

    manifest = _manifest()
    split = replace(
        manifest.dataset.split,
        final_holdout=DateRange(start="2025-03-05", end="2025-03-31"),
    )
    manifest = replace(manifest, dataset=replace(manifest.dataset, split=split))
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        module,
        "build_manifest_nested_temporal_validation_plan",
        lambda _manifest: _nested_plan(),
    )
    monkeypatch.setattr(
        module,
        "run_manifest_candidate_range",
        lambda **kwargs: calls.append(kwargs),
    )
    capability = derive_validation_experiment_capability(
        manifest_hash=manifest.manifest_hash(),
        research_classification=manifest.research_classification,
    )

    with pytest.raises(
        NativeValidationExperimentExecutionError,
        match="validation_experiments_nested_plan_touches_holdout",
    ):
        execute_manifest_validation_experiments(
            manifest=manifest,
            db_path=None,
            manager=_manager(tmp_path),
            candidates=_candidates(manifest),
            preliminary_selection_artifact_hash=sha256_prefixed(
                {"preliminary_selection": 1}
            ),
            dataset_snapshot_hash=sha256_prefixed({"dataset": "selection"}),
            capability=capability,
            strategy_registry=builtin_strategy_registry(),
        )
    assert calls == []


def test_nested_winner_is_frozen_in_immutable_selection_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from market_research.research import validation_experiment_execution as module

    manifest = _manifest()
    monkeypatch.setattr(
        module,
        "build_manifest_nested_temporal_validation_plan",
        lambda _manifest: _nested_plan(),
    )
    monkeypatch.setattr(module, "run_manifest_candidate_range", _fake_range_evaluation)
    candidates = _candidates(manifest)
    preliminary_id = str(candidates[1]["parameter_candidate_id"])
    preliminary_hash = sha256_prefixed({"preliminary_selection": preliminary_id})
    capability = derive_validation_experiment_capability(
        manifest_hash=manifest.manifest_hash(),
        research_classification=manifest.research_classification,
    )
    execution = execute_manifest_validation_experiments(
        manifest=manifest,
        db_path=None,
        manager=_manager(tmp_path),
        candidates=candidates,
        preliminary_selection_artifact_hash=preliminary_hash,
        dataset_snapshot_hash=sha256_prefixed({"dataset": "selection"}),
        capability=capability,
        strategy_registry=builtin_strategy_registry(),
    )
    score_rows = [
        {"candidate_id": str(item["parameter_candidate_id"]), "score": index}
        for index, item in enumerate(candidates)
    ]
    selection_report = {
        "candidate_final_scores": score_rows,
        "candidate_final_scores_hash": sha256_prefixed(score_rows),
        "final_selection_contract_hash": sha256_prefixed(
            {"preliminary_contract": 1}
        ),
    }
    artifact = _freeze_native_nested_selection_artifact(
        manifest=manifest,
        selection_report=selection_report,
        candidates=candidates,
        preliminary_selection_artifact={"content_hash": preliminary_hash},
        execution=execution,
    )

    assert execution.selected_candidate_id != preliminary_id
    assert artifact["selected_candidate_id"] == execution.selected_candidate_id
    native_result = _native_nested_final_selection_result(
        manifest=manifest,
        execution=execution,
    )
    assert artifact["candidate_scores_hash"] == native_result[
        "candidate_final_scores_hash"
    ]
    assert artifact["candidate_scores_hash"] != selection_report[
        "candidate_final_scores_hash"
    ]
    assert artifact["final_selection_contract_hash"] == native_result[
        "final_selection_contract_hash"
    ]
    assert artifact["final_selection_contract_hash"] != selection_report[
        "final_selection_contract_hash"
    ]
    authoritative_report = {
        "manifest_hash": manifest.manifest_hash(),
        "final_selection_required": True,
        "candidates": candidates,
        **native_result,
        "selection_artifact": artifact,
        "selection_artifact_hash": artifact["content_hash"],
    }
    assert validate_final_selection_report(authoritative_report) == []
    authority = artifact["selection_authority_binding"]
    assert authority["terminal_selection_scores"] == list(
        execution.terminal_selection_scores
    )
    assert authority["selected_candidate_hash"] == native_candidate_definition_hash(
        manifest_hash=manifest.manifest_hash(),
        strategy_name=manifest.strategy_name,
        strategy_version=manifest.strategy_version,
        candidate=candidates[0],
    )
    assert _native_nested_selection_artifact_reasons(
        artifact=artifact,
        execution=execution,
    ) == []

    rebound = copy.deepcopy(artifact)
    rebound["selection_authority_binding"]["selected_candidate_id"] = preliminary_id
    rebound_authority = rebound["selection_authority_binding"]
    rebound_material = {
        key: value for key, value in rebound_authority.items() if key != "content_hash"
    }
    rebound_authority["content_hash"] = sha256_prefixed(
        rebound_material,
        label="manifest_native_nested_selection_authority",
    )
    rebound["selection_authority_binding_hash"] = rebound_authority["content_hash"]
    rebound_material = {
        key: value for key, value in rebound.items() if key != "content_hash"
    }
    rebound["content_hash"] = sha256_prefixed(
        rebound_material,
        label="selection_artifact",
    )
    assert "native_nested_selection_authority_selected_candidate_id_mismatch" in (
        _native_nested_selection_artifact_reasons(
            artifact=rebound,
            execution=execution,
        )
    )


def test_terminal_reproduction_recomputes_and_freezes_native_winner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from market_research.research import validation_experiment_execution as module

    manifest = _manifest()
    monkeypatch.setattr(
        module,
        "build_manifest_nested_temporal_validation_plan",
        lambda _manifest: _nested_plan(),
    )
    monkeypatch.setattr(module, "run_manifest_candidate_range", _fake_range_evaluation)
    candidates = _candidates(manifest)
    preliminary_id = str(candidates[1]["parameter_candidate_id"])
    score_rows = [
        {"candidate_id": str(item["parameter_candidate_id"]), "score": index}
        for index, item in enumerate(candidates)
    ]
    preliminary_result = {
        "selected_candidate_id": preliminary_id,
        "candidate_final_scores": score_rows,
        "candidate_final_scores_hash": sha256_prefixed(score_rows),
        "final_selection_contract_hash": sha256_prefixed(
            {"preliminary_contract": 1}
        ),
    }
    preliminary_artifact = build_selection_artifact(
        manifest_hash=manifest.manifest_hash(),
        selection_result=preliminary_result,
        candidates=candidates,
    )
    assert isinstance(preliminary_artifact, dict)
    dataset_hash = sha256_prefixed({"dataset": "selection"})
    reproduced_report = {
        "manifest_hash": manifest.manifest_hash(),
        "dataset_content_hash": dataset_hash,
        "candidates": candidates,
        "selected_candidate_id": preliminary_id,
        "selection_artifact": preliminary_artifact,
        "selection_artifact_hash": preliminary_artifact["content_hash"],
        **preliminary_result,
    }
    selection_report_hash = sha256_prefixed({"selection_report": 1})

    execution, gate_hash, authoritative = (
        _run_terminal_validation_experiment_reproduction(
            manifest=manifest,
            reproduced_report=reproduced_report,
            baseline_receipt={
                "source_evidence_binding": {
                    "selection_report_hash": selection_report_hash,
                }
            },
            db_path=None,
            manager=_manager(tmp_path),
            strategy_registry=builtin_strategy_registry(),
            progress_callback=None,
        )
    )

    assert execution.selected_candidate_id != preliminary_id
    assert authoritative["selected_candidate_id"] == execution.selected_candidate_id
    assert authoritative["selection_artifact"]["selection_authority"] == (
        "manifest_native_nested_selection"
    )
    assert authoritative["selection_artifact_hash"] == authoritative[
        "selection_artifact"
    ]["content_hash"]
    assert gate_hash == sha256_prefixed(
        {
            "schema_version": 1,
            "artifact_type": "pre_holdout_validation_gate",
            "final_holdout_authority_scope_hash": (
                final_holdout_authority_scope_hash(manifest)
            ),
            "manifest_hash": manifest.manifest_hash(),
            "selection_report_hash": selection_report_hash,
            "selection_artifact_hash": authoritative[
                "selection_artifact_hash"
            ],
            "validation_experiment_bundle_hash": execution.bundle.content_hash,
            "native_validation_computation_receipt_hash": (
                execution.computation_receipt_hash
            ),
            "selected_candidate_id": execution.selected_candidate_id,
            "gate_result": "PASS",
            "gate_reasons": [],
        },
        label="pre_holdout_validation_gate",
    )

    selected = next(
        item
        for item in candidates
        if item["parameter_candidate_id"] == execution.selected_candidate_id
    )
    authoritative.update(
        {
            "validation_eligibility_gate_result": "PASS",
            "dataset_quality_gate_status": "PASS",
            "stress_suite_gate_result": "PASS",
            "statistical_gate_result": "PASS",
            "walk_forward_gate_result": "PASS",
            "final_selection_gate_result": "PASS",
            "nested_temporal_validation": {
                "plan_hash": execution.bundle.temporal_plan_hash,
            },
        }
    )
    _status, _stages, reasons = aggregate_validation_gates(
        manifest=manifest,
        selection_report=authoritative,
        selection_artifact=authoritative["selection_artifact"],
        selected_candidate=selected,
        final_holdout_confirmation=None,
        validation_experiment_policy=execution.bundle.policy,
        validation_experiment_bundle=execution.bundle,
    )
    assert "validation_experiment_bundle_selected_candidate_hash_mismatch" not in (
        reasons
    )
    assert "validation_experiment_nested_selection_candidate_hash_mismatch" not in (
        reasons
    )


def test_validation_bound_external_precomputed_bundle_is_not_promotable(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    bundle_path = tmp_path / "external-results.json"
    bundle_path.write_text("{}", encoding="utf-8")

    try:
        run_research_validation(
            manifest=manifest,
            db_path=None,
            manager=_manager(tmp_path),
            manifest_path=str(tmp_path / "manifest.json"),
            strategy_registry=builtin_strategy_registry(),
            validation_experiment_bundle_path=bundle_path,
        )
    except ValidationRunError as exc:
        # Malformed/downgraded bundles may fail even earlier; neither error is
        # an authentication bypass.
        assert str(exc).startswith("validation_experiment_bundle_") or (
            str(exc) == "external_validation_experiment_evidence_not_authenticated"
        )
    else:  # pragma: no cover - explicit fail-closed assertion
        raise AssertionError("external result bundle unexpectedly promoted")


def test_missing_native_bundle_is_a_pre_holdout_blocker() -> None:
    manifest = _manifest()
    capability = derive_validation_experiment_capability(
        manifest_hash=manifest.manifest_hash(),
        research_classification=manifest.research_classification,
    )
    reasons = _pre_holdout_gate_reasons(
        manifest=manifest,
        selection_report={
            "validation_eligibility_gate_result": "PASS",
            "dataset_quality_gate_status": "PASS",
            "final_selection_gate_result": "PASS",
            "stress_suite_gate_result": "PASS",
            "statistical_gate_result": "PASS",
            "walk_forward_gate_result": "PASS",
        },
        selection_artifact=None,
        selected_candidate=None,
        validation_experiment_capability=capability,
        validation_experiment_bundle=None,
    )

    assert "pre_holdout_validation_experiment_bundle_missing" in reasons
    assert "selection_artifact_missing" in reasons
    assert "selected_candidate_missing" in reasons
