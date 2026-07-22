from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.storage_io import (
    write_json_atomic,
    write_json_atomic_create_or_verify,
)
from market_research.research.artifact_store import ArtifactStore
from market_research.research.hash_chain import append_hash_chained_jsonl_idempotent
from market_research.research.hashing import (
    content_hash_payload,
    report_content_hash_payload,
    sha256_prefixed,
)
from market_research.research.hypothesis_contract import parse_hypothesis_spec
from market_research.research.knowledge_contract import KnowledgeRef
from market_research.research.knowledge_registry import (
    KNOWLEDGE_REGISTRY_HASH_LABEL,
    knowledge_registry_path,
    validate_knowledge_registry,
)
from market_research.research.prospective_validation import (
    PROSPECTIVE_VALIDATION_SCHEMA_VERSION,
    ImmutableEvidenceRef,
    MetricGuard,
    ProspectiveObservation,
    ProspectiveEvaluation,
    ProspectiveStatus,
    ProspectiveValidationSpec,
    SimulatedFillEvidence,
    build_research_conclusion,
    evaluate_prospective_validation,
    publish_prospective_spec,
    publish_research_conclusion,
    record_prospective_observation,
    validate_prospective_registry,
)
from market_research.research.research_package_registry import (
    ResearchPackageManifest,
    ResearchPackageRegistry,
    ResearchPackageRegistryError,
    build_research_package_manifest,
    cost_assumption_content_hash,
    diff_research_packages,
    feature_definition_content_hash,
    fill_assumption_content_hash,
    historical_distribution_content_hash,
    research_package_registry_path,
    validated_rule_set_content_hash,
    validate_research_package_registry,
)
from market_research.research.validation_decision import (
    VALIDATION_DECISION_SCHEMA_VERSION,
    CriterionDecision,
    TerminalValidationReportRef,
    ValidationDecision,
    publish_validation_decision,
    terminal_validation_report_path,
    validate_validation_decision_registry,
)
from market_research.settings import ResearchSettings


def _manager(tmp_path: Path) -> ResearchPathManager:
    return ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "data",
            artifact_root=tmp_path / "artifacts",
            report_root=tmp_path / "reports",
            cache_root=tmp_path / "cache",
            db_path=tmp_path / "input.sqlite",
            max_workers=1,
            random_seed=0,
        ),
        project_root=Path.cwd(),
    )


def _ref(
    authority: str, logical_id: str, content_hash: str, *, version: str = "1"
) -> ImmutableEvidenceRef:
    return ImmutableEvidenceRef(
        authority=authority,
        logical_id=logical_id,
        version=version,
        content_hash=content_hash,
    )


def _guards() -> tuple[MetricGuard, ...]:
    names = (
        "expected_value",
        "win_rate",
        "pnl_p10",
        "pnl_p50",
        "pnl_p90",
        "mean_holding_period_seconds",
        "signal_frequency_per_day",
        "mean_cost",
        "max_drawdown",
    )
    return tuple(
        MetricGuard(
            metric=name,
            historical_value=0.0,
            degradation_lower=-1_000_000.0,
            degradation_upper=1_000_000.0,
            invalidation_lower=-2_000_000.0,
            invalidation_upper=2_000_000.0,
        )
        for name in names
    )


def _hypothesis_payload(
    *, instrument: str, market: str, phenomenon: str
) -> dict[str, object]:
    suffix = instrument.lower()
    observation = {
        "schema_version": 1,
        "observation_id": f"obs-{suffix}",
        "version": "1",
        "statement": "Externally prepared samples motivate a falsifiable study.",
        "actor_id": "researcher-a",
        "observed_at": "2025-12-01T00:00:00+00:00",
        "recorded_at": "2025-12-02T00:00:00+00:00",
        "market": market,
        "interval": "1m",
        "confidence": 0.6,
        "status": "recorded",
        "fact_status": "not_verified",
        "evidence_hashes": [sha256_prefixed({"observation": suffix})],
    }
    observation_ref = {
        "observation_id": observation["observation_id"],
        "version": observation["version"],
        "observation_hash": sha256_prefixed(observation),
    }
    hypothesis_id = f"hypothesis-{suffix}"
    hypothesis_text = f"{instrument} exhibits the preregistered {phenomenon} effect."
    question = {
        "schema_version": 1,
        "question_id": f"question-{suffix}",
        "version": "1",
        "question_text": f"Does {instrument} exhibit the proposed effect after costs?",
        "actor_id": "researcher-a",
        "recorded_at": "2025-12-03T00:00:00+00:00",
        "observation_refs": [observation_ref],
        "competing_hypotheses": [
            {
                "hypothesis_id": hypothesis_id,
                "version": "1",
                "hypothesis_text": hypothesis_text,
            },
            {
                "hypothesis_id": f"hypothesis-{suffix}-null",
                "version": "1",
                "hypothesis_text": f"{instrument} has no positive effect after costs.",
            },
        ],
    }
    return {
        "schema_version": 2,
        "hypothesis_id": hypothesis_id,
        "version": "1",
        "hypothesis_text": hypothesis_text,
        "actor_id": "researcher-a",
        "created_at": "2025-12-04T00:00:00+00:00",
        "phenomenon": phenomenon,
        "mechanism": "delayed information diffusion",
        "observation_conditions": ["sufficient immutable candle coverage"],
        "comparison_target": "cash",
        "falsification_criteria": ["expected value is non-positive after costs"],
        "experiment_family_id": f"family-{suffix}",
        "registration_status": "unregistered",
        "pre_registered_at": None,
        "registration_evidence_hash": None,
        "observations": [observation],
        "research_question": question,
        "research_question_ref": {
            "question_id": question["question_id"],
            "version": question["version"],
            "question_hash": sha256_prefixed(question),
        },
        "observation_refs": [observation_ref],
    }


def _base_package(
    *,
    market: str = "KRW-BTC",
    instrument: str = "BTC",
    phenomenon: str = "momentum",
    threshold: float = 1.0,
    limitation: str = "short validation period",
    run_suffix: str = "one",
) -> dict[str, object]:
    rule_spec = {
        "entry": {"operator": ">", "feature": "sma_gap", "value": threshold},
        "exit": {"operator": "<=", "feature": "sma_gap", "value": 0.0},
        "position_sizing": {"type": "fractional_cash", "fraction": 0.5},
        "edge_invalidation": {"metric": "expected_value", "operator": "<="},
    }
    strategy_spec = {
        "strategy_name": "sma_with_filter",
        "strategy_version": "1",
        "rule_spec": rule_spec,
    }
    hypothesis = parse_hypothesis_spec(
        _hypothesis_payload(instrument=instrument, market=market, phenomenon=phenomenon)
    ).as_dict()
    features = [
        {
            "feature_id": "sma-gap",
            "version": "1",
            "expression": "close / sma(close, 20) - 1",
        }
    ]
    cost = {"fee_rate": 0.001, "slippage_bps": 10.0}
    fill = {
        "execution_timing_policy": {"fill_reference": "next_candle_open"},
        "execution_model": {"type": "fixed_bps", "version": "1"},
    }
    historical = {
        "basis": "validation_and_final_holdout_observed_range",
        "metric_ranges": {
            "return_pct": {
                "minimum": threshold,
                "maximum": threshold + 1.0,
                "observation_count": 2,
            }
        },
    }
    run_hash = sha256_prefixed({"run": run_suffix})
    package: dict[str, object] = {
        "schema_version": 5,
        "authoritative": True,
        "package_authority_status": "CANONICAL_REGISTRIES_VERIFIED",
        "package_authority_result": "PASS",
        "validation_result": "PASS",
        "source_report_content_hash": run_hash,
        "strategy_spec": strategy_spec,
        "strategy_spec_hash": sha256_prefixed(strategy_spec),
        "decision_contract_version": "research_decision_v2",
        "effective_strategy_parameters": {"threshold": threshold},
        "effective_strategy_parameters_hash": sha256_prefixed({"threshold": threshold}),
        "target_asset": {
            "market": market,
            "interval": "1m",
            "instrument_evidence": {"instrument_id": instrument},
        },
        "hypothesis": hypothesis,
        "hypothesis_contract_hash": sha256_prefixed(hypothesis),
        "feature_definitions": features,
        "data_requirements": {"interval": "1m", "minimum_candles": 1000},
        "signal_calculation_timing": {
            "decision_at": "candle_close",
            "point_in_time": True,
        },
        "fill_assumptions": fill,
        "cost_assumptions": cost,
        "portfolio_policy": {"initial_position_qty": 0.0},
        "risk_policy": {"max_open_positions": 1},
        "expected_performance_range": historical,
        "entry_conditions": {"entry": rule_spec["entry"]},
        "take_profit": None,
        "stop_loss": None,
        "time_exit": {"maximum_bars": 120},
        "position_sizing": rule_spec["position_sizing"],
        "edge_invalidation": rule_spec["edge_invalidation"],
        "allowed_market_regimes": {
            "allowed": ["trend"],
            "blocked": ["illiquid"],
        },
        "strategy_suspension_conditions": ["expected value becomes non-positive"],
        "known_limitations": {
            "data": [limitation],
            "execution": ["queue position unavailable"],
        },
    }
    package["content_hash"] = sha256_prefixed(package)
    return package


def _bind_instrument_source_authorities(base: dict[str, object]) -> None:
    target = base["target_asset"]
    assert isinstance(target, dict)
    evidence = target["instrument_evidence"]
    assert isinstance(evidence, dict)
    evidence.update(
        {
            "market_calendar": {
                "calendar_id": "calendar-24x7",
                "calendar_version_id": "calendar-24x7-v1",
                "calendar_contract_hash": sha256_prefixed({"calendar": "24x7-v1"}),
                "source_content_hash": sha256_prefixed(
                    {"calendar_source": "immutable"}
                ),
                "source_schema_hash": sha256_prefixed({"calendar_schema": "v1"}),
                "source_uri": "/external/authorities/calendar-24x7.json",
            },
            "point_in_time_universe": {
                "universe_id": "universe-btc",
                "universe_version_id": "universe-btc-v1",
                "universe_contract_hash": sha256_prefixed({"universe": "btc-v1"}),
                "source_content_hash": sha256_prefixed(
                    {"universe_source": "immutable"}
                ),
                "source_schema_hash": sha256_prefixed({"universe_schema": "v1"}),
                "source_uri": "/external/authorities/universe-btc.json",
            },
        }
    )
    base["content_hash"] = sha256_prefixed(
        {key: value for key, value in base.items() if key != "content_hash"}
    )


def _prospective_inputs(
    base: dict[str, object],
    *,
    validation_id: str = "pv-one",
    researcher: str = "researcher-a",
    rationale: str = "prospective evidence remains consistent",
    limitation: str = "prospective window remains short",
    validation_decision_ref: ImmutableEvidenceRef | None = None,
    manager: ResearchPathManager | None = None,
):
    source_ref = _ref(
        "strategy_research_package",
        f"source-{validation_id}",
        str(base["content_hash"]),
    )
    hypothesis = base["hypothesis"]
    assert isinstance(hypothesis, dict)
    hypothesis_ref = _ref(
        "knowledge_registry",
        str(hypothesis["hypothesis_id"]),
        str(base["hypothesis_contract_hash"]),
    )
    validation_decision_ref = validation_decision_ref or _ref(
        "validation_decision_registry",
        f"validation-decision-{validation_id}",
        sha256_prefixed({"validation": validation_id}),
    )
    spec = ProspectiveValidationSpec(
        schema_version=PROSPECTIVE_VALIDATION_SCHEMA_VERSION,
        validation_id=validation_id,
        version="1",
        source_package_ref=source_ref,
        hypothesis_ref=hypothesis_ref,
        validation_decision_ref=validation_decision_ref,
        validated_rule_set_hash=validated_rule_set_content_hash(base),
        feature_definition_hash=feature_definition_content_hash(base),
        cost_assumption_hash=cost_assumption_content_hash(base),
        fill_assumption_hash=fill_assumption_content_hash(base),
        historical_distribution_hash=historical_distribution_content_hash(base),
        metric_guards=_guards(),
        frozen_at="2026-01-01T00:00:00+00:00",
        start_at="2026-01-02T00:00:00+00:00",
        end_at="2026-02-02T00:00:00+00:00",
        minimum_observations=10,
        minimum_elapsed_seconds=86_400,
        maximum_missing_rate=0.05,
        maximum_late_rate=0.05,
        maximum_latency_seconds=30.0,
        stopping_rules=("stop when an invalidation bound is crossed",),
        review_rules=("review all degradation classifications",),
        frozen_by=researcher,
    )
    if manager is None:
        evaluation = ProspectiveEvaluation(
            schema_version=PROSPECTIVE_VALIDATION_SCHEMA_VERSION,
            validation_ref=spec.ref(),
            evaluated_at="2026-02-01T00:00:00+00:00",
            status=ProspectiveStatus.CONFIRMED,
            reasons=("all_frozen_review_criteria_satisfied",),
            comparison=tuple(
                {
                    "metric": guard.metric,
                    "historical_value": guard.historical_value,
                    "prospective_value": 0.0,
                    "classification": "CONFIRMED",
                    "degradation_lower": guard.degradation_lower,
                    "degradation_upper": guard.degradation_upper,
                    "invalidation_lower": guard.invalidation_lower,
                    "invalidation_upper": guard.invalidation_upper,
                }
                for guard in spec.metric_guards
            ),
            observed_metrics={guard.metric: 0.0 for guard in spec.metric_guards},
            observation_count=20,
            outcome_count=20,
            missing_count=0,
            late_count=0,
            missing_rate=0.0,
            late_rate=0.0,
            elapsed_seconds=2_592_000.0,
            stopping_triggered=False,
            review_required=False,
            observation_stream_hash=sha256_prefixed({"stream": validation_id}),
            observation_stream_row_count=20,
        )
    else:
        publish_prospective_spec(manager=manager, spec=spec)
        for index in range(10):
            day = index + 3
            prefix = f"2026-01-{day:02d}T00:00:"
            record_prospective_observation(
                manager=manager,
                spec=spec,
                observation=ProspectiveObservation(
                    schema_version=PROSPECTIVE_VALIDATION_SCHEMA_VERSION,
                    observation_id=f"{validation_id}-observation-{index:02d}",
                    source_event_id=f"{validation_id}-source-{index:02d}",
                    source_event_at=prefix + "00+00:00",
                    data_available_at=prefix + "01+00:00",
                    received_at=prefix + "02+00:00",
                    signal_generated_at=prefix + "03+00:00",
                    expected_signal="BUY",
                    data_status="AVAILABLE",
                    actual_data_hash=sha256_prefixed(
                        {"validation": validation_id, "data": index}
                    ),
                    feature_values_hash=sha256_prefixed(
                        {"validation": validation_id, "features": index}
                    ),
                    simulated_fill=SimulatedFillEvidence(
                        simulated_fill_id=f"{validation_id}-fill-{index:02d}",
                        occurred_at=prefix + "04+00:00",
                        side="BUY",
                        quantity=1.0,
                        price=100.0,
                        cost=0.1,
                        realized_return=0.01,
                        holding_period_seconds=60.0,
                        execution_assumption_hash=spec.fill_assumption_hash,
                        cost_assumption_hash=spec.cost_assumption_hash,
                    ),
                ),
            )
        evaluation = evaluate_prospective_validation(
            manager=manager,
            spec=spec,
            evaluated_at="2026-02-01T00:00:00+00:00",
        )
    conclusion = build_research_conclusion(
        spec=spec,
        evaluation=evaluation,
        conclusion_id=f"conclusion-{validation_id}",
        version="1",
        rationale=rationale,
        known_limitations=(limitation,),
        decided_by=researcher,
        decided_at="2026-02-01T01:00:00+00:00",
    )
    if manager is not None:
        publish_research_conclusion(
            manager=manager,
            spec=spec,
            evaluation=evaluation,
            conclusion=conclusion,
        )
    return spec, evaluation, conclusion, validation_decision_ref


def _terminal_report(
    base: dict[str, object], *, validation_id: str, dataset_id: str
) -> dict[str, object]:
    hypothesis = base["hypothesis"]
    assert isinstance(hypothesis, dict)
    report: dict[str, object] = {
        "schema_version": 3,
        "artifact_type": "validated_research_result",
        "experiment_id": f"experiment-spec-{validation_id}",
        "run_id": f"run-{validation_id}",
        "manifest_hash": sha256_prefixed({"experiment_spec": validation_id}),
        "selection_report_hash": sha256_prefixed({"selection_report": validation_id}),
        "dataset_snapshot_id": dataset_id,
        "dataset_content_hash": sha256_prefixed({"dataset": dataset_id}),
        "hypothesis_id": hypothesis["hypothesis_id"],
        "hypothesis_version": hypothesis["version"],
        "hypothesis_contract_hash": base["hypothesis_contract_hash"],
        "end_to_end_validation_result": "PASS",
    }
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))
    return report


def _decision_contract(
    *, manager: ResearchPathManager, report: dict[str, object]
) -> tuple[ValidationDecision, ImmutableEvidenceRef]:
    experiment_id = str(report["experiment_id"])
    run_id = str(report["run_id"])
    snapshot_hash = sha256_prefixed(report, label="terminal_validation_report_snapshot")
    terminal_path = terminal_validation_report_path(
        manager,
        experiment_id=experiment_id,
        run_id=run_id,
        snapshot_hash=snapshot_hash,
    )
    decision_id = f"validation-decision-{run_id.removeprefix('run-')}"
    terminal_ref = TerminalValidationReportRef(
        schema_version=1,
        artifact_type="validated_research_result",
        experiment_id=experiment_id,
        run_id=run_id,
        content_hash=str(report["content_hash"]),
        snapshot_hash=snapshot_hash,
        artifact_path=str(terminal_path.resolve()),
    )
    decision = ValidationDecision(
        schema_version=VALIDATION_DECISION_SCHEMA_VERSION,
        decision_id=decision_id,
        version="1",
        hypothesis_ref=KnowledgeRef(
            "hypothesis",
            str(report["hypothesis_id"]),
            str(report["hypothesis_version"]),
            str(report["hypothesis_contract_hash"]),
        ),
        experiment_id=experiment_id,
        run_id=run_id,
        decision="VALIDATED",
        criterion_results=(
            CriterionDecision(
                criterion_id="end_to_end_validation_result",
                passed=True,
                observed="PASS",
                required="PASS",
            ),
        ),
        evidence_hashes=tuple(
            sorted({str(report["manifest_hash"]), str(report["content_hash"])})
        ),
        researcher_interpretation="The frozen validation criteria passed.",
        reviewer_comment="Recorded from immutable terminal evidence.",
        decided_by="validation-decision-policy",
        decided_at="2026-01-01T00:00:00+00:00",
        terminal_report_ref=terminal_ref,
    )
    decision_hash = decision.content_hash()
    return decision, _ref("validation_decision_registry", decision_id, decision_hash)


def _decision_payload(
    *, manager: ResearchPathManager, report: dict[str, object]
) -> tuple[dict[str, object], ImmutableEvidenceRef]:
    decision, ref = _decision_contract(manager=manager, report=report)
    return decision.as_dict(), ref


def _reproduction_receipt(
    *, report: dict[str, object]
) -> tuple[dict[str, object], ImmutableEvidenceRef]:
    stable = {
        "schema_version": 1,
        "experiment_id": report["experiment_id"],
        "manifest_hash": report["manifest_hash"],
    }
    stable["stable_fingerprint_hash"] = sha256_prefixed(
        stable, label="reproduction_stable_fingerprint"
    )
    receipt: dict[str, object] = {
        "schema_version": 1,
        "receipt_type": "research_run_reproduction_receipt",
        "experiment_id": report["experiment_id"],
        "manifest_hash": report["manifest_hash"],
        "source_report_hash": report["selection_report_hash"],
        "stable_fingerprint": stable,
        "stable_fingerprint_hash": stable["stable_fingerprint_hash"],
    }
    receipt["receipt_content_hash"] = sha256_prefixed(
        content_hash_payload(receipt), label="reproduction_receipt_content"
    )
    return receipt, _ref(
        "reproduction_receipt_store",
        f"{report['experiment_id']}:receipt",
        str(receipt["receipt_content_hash"]),
    )


def _build_package(
    base: dict[str, object],
    *,
    package_id: str = "final-research-package",
    version: str = "1",
    validation_id: str = "pv-one",
    dataset_id: str = "dataset-one",
    researcher: str = "researcher-a",
    rationale: str = "prospective evidence remains consistent",
    limitation: str = "prospective window remains short",
    supersedes: ImmutableEvidenceRef | None = None,
    manager: ResearchPathManager | None = None,
):
    report = _terminal_report(base, validation_id=validation_id, dataset_id=dataset_id)
    base["source_report_content_hash"] = report["content_hash"]
    base["content_hash"] = sha256_prefixed(
        {key: value for key, value in base.items() if key != "content_hash"}
    )
    decision_ref = None
    if manager is not None:
        source_path = manager.report_path(
            "research", str(report["experiment_id"]), "strategy_package.json"
        )
        write_json_atomic_create_or_verify(source_path, base)
        decision, decision_ref = _decision_contract(manager=manager, report=report)
        terminal_ref = decision.terminal_report_ref
        assert terminal_ref is not None
        write_json_atomic_create_or_verify(Path(terminal_ref.artifact_path), report)
        hypothesis = parse_hypothesis_spec(base["hypothesis"])
        publish_validation_decision(
            manager=manager,
            hypothesis=hypothesis,
            decision=decision,
        )
    spec, evaluation, conclusion, decision_ref = _prospective_inputs(
        base,
        validation_id=validation_id,
        researcher=researcher,
        rationale=rationale,
        limitation=limitation,
        validation_decision_ref=decision_ref,
        manager=manager,
    )
    receipt_ref = (
        _reproduction_receipt(report=report)[1]
        if manager is not None
        else _ref(
            "reproduction_receipt_store",
            f"{report['experiment_id']}:receipt",
            sha256_prefixed({"receipt": validation_id}),
        )
    )
    package = build_research_package_manifest(
        package_id=package_id,
        version=version,
        base_package=base,
        prospective_spec=spec,
        prospective_evaluation=evaluation,
        research_conclusion=conclusion,
        experiment_run_ref=_ref(
            "run_lifecycle_registry",
            f"run-{validation_id}",
            str(report["content_hash"]),
        ),
        dataset_snapshot_ref=_ref(
            "dataset_registry",
            dataset_id,
            sha256_prefixed({"dataset": dataset_id}),
        ),
        feature_definition_ref=_ref(
            "feature_registry",
            f"features-{validation_id}",
            spec.feature_definition_hash,
        ),
        experiment_spec_ref=_ref(
            "experiment_registry",
            f"experiment-spec-{validation_id}",
            sha256_prefixed({"experiment_spec": validation_id}),
        ),
        validation_decision_ref=decision_ref,
        reproduction_receipt_ref=_ref(
            receipt_ref.authority,
            receipt_ref.logical_id,
            receipt_ref.content_hash,
            version=receipt_ref.version,
        ),
        supersedes=supersedes,
    )
    if manager is not None:
        receipt, actual_receipt_ref = _reproduction_receipt(report=report)
        assert actual_receipt_ref == package.refs.reproduction_receipt
        write_json_atomic_create_or_verify(
            manager.report_path(
                "research",
                str(report["experiment_id"]),
                "reproduction_receipt.json",
            ),
            receipt,
        )
    return package, spec, evaluation, conclusion


def test_final_manifest_binds_all_evidence_and_is_self_hashing() -> None:
    base = _base_package()
    package, spec, evaluation, conclusion = _build_package(base)

    payload = package.as_dict()
    assert payload["schema_version"] == 1
    assert payload["export_contract_version"] == "research_package_manifest_v1"
    assert payload["content_hash"] == package.content_hash
    assert payload["refs"]["source_package"] == spec.source_package_ref.as_dict()
    assert payload["refs"]["prospective_evaluation"]["content_hash"] == (
        evaluation.content_hash()
    )
    assert payload["refs"]["research_conclusion"]["content_hash"] == (
        conclusion.content_hash()
    )
    assert set(payload["refs"]) == {
        "source_package",
        "hypothesis",
        "experiment_run",
        "dataset_snapshot",
        "feature_definition",
        "experiment_spec",
        "validation_decision",
        "prospective_validation",
        "prospective_evaluation",
        "research_conclusion",
        "reproduction_receipt",
    }
    assert package.validated_rule_set_hash == spec.validated_rule_set_hash
    assert payload["assumptions"]["cost_hash"] == spec.cost_assumption_hash
    assert payload["limitations"]["prospective_limitations"]
    assert payload["reproduction_recipe"]["command"] == "research-reproduce-run"
    assert payload["reproduction_recipe"]["tolerance"]["hashes"] == "exact_match"

    payload["validated_rule_set"]["rule_spec"]["entry"]["value"] = 999.0
    assert package.as_dict()["validated_rule_set"]["rule_spec"]["entry"]["value"] == 1.0


def test_build_rejects_source_prospective_conclusion_and_operational_tampering() -> (
    None
):
    base = _base_package()
    package, spec, evaluation, conclusion = _build_package(base)
    assert package.content_hash
    common = {
        "package_id": "rejected-package",
        "version": "1",
        "base_package": base,
        "prospective_evaluation": evaluation,
        "research_conclusion": conclusion,
        "experiment_run_ref": _ref(
            "experiment_registry", "run", str(base["source_report_content_hash"])
        ),
        "dataset_snapshot_ref": _ref(
            "dataset_registry", "dataset", sha256_prefixed({"dataset": 1})
        ),
        "feature_definition_ref": _ref(
            "feature_registry", "features", spec.feature_definition_hash
        ),
        "experiment_spec_ref": _ref(
            "experiment_registry", "spec", sha256_prefixed({"spec": 1})
        ),
        "validation_decision_ref": spec.validation_decision_ref,
        "reproduction_receipt_ref": _ref(
            "reproduction_receipt_store",
            "receipt",
            sha256_prefixed({"receipt": 1}),
        ),
    }

    tampered_source = dict(base)
    tampered_source["known_limitations"] = {"data": ["silently changed"]}
    with pytest.raises(
        ResearchPackageRegistryError,
        match="research_package_source_content_hash_mismatch",
    ):
        build_research_package_manifest(
            **(common | {"base_package": tampered_source}),
            prospective_spec=spec,
        )

    with pytest.raises(
        ResearchPackageRegistryError,
        match="research_package_prospective_validated_rule_set_hash_mismatch",
    ):
        build_research_package_manifest(
            **common,
            prospective_spec=replace(
                spec, validated_rule_set_hash=sha256_prefixed({"wrong": "rules"})
            ),
        )

    with pytest.raises(
        ResearchPackageRegistryError,
        match="research_package_conclusion_evaluation_hash_mismatch",
    ):
        build_research_package_manifest(
            **(
                common
                | {
                    "research_conclusion": replace(
                        conclusion,
                        prospective_evaluation_hash=sha256_prefixed(
                            {"wrong": "evaluation"}
                        ),
                    )
                }
            ),
            prospective_spec=spec,
        )

    operational = dict(base)
    operational["broker_account"] = "forbidden"
    operational["content_hash"] = sha256_prefixed(
        {key: value for key, value in operational.items() if key != "content_hash"}
    )
    with pytest.raises(
        ResearchPackageRegistryError,
        match="research_package_operational_field_forbidden",
    ):
        build_research_package_manifest(
            **(common | {"base_package": operational}),
            prospective_spec=spec,
        )


def test_registry_publish_is_idempotent_and_conflicts_fail_closed(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    registry = ResearchPackageRegistry(manager)
    base = _base_package()
    package, _, _, _ = _build_package(base, manager=manager)

    first = registry.publish(package)
    assert registry.publish(package) == first
    assert (
        research_package_registry_path(manager).read_text(encoding="utf-8").count("\n")
        == 1
    )
    assert registry.get(package.package_id, package.version) == package
    assert validate_research_package_registry(manager)["status"] == "PASS"

    conflict, _, _, _ = _build_package(
        _base_package(run_suffix="conflicting-run"),
        validation_id="pv-conflicting-run",
        dataset_id="dataset-conflicting-run",
        manager=manager,
    )
    with pytest.raises(
        ResearchPackageRegistryError, match="research_package_identity_conflict"
    ):
        registry.publish(conflict)


@pytest.mark.parametrize(
    "field_name",
    ("live_approved", "broker_account", "deployment_target", "capital_allocation"),
)
def test_final_package_rejects_operational_field_families(field_name: str) -> None:
    base = _base_package()
    base[field_name] = "forbidden"
    base["content_hash"] = sha256_prefixed(
        {key: value for key, value in base.items() if key != "content_hash"}
    )

    with pytest.raises(
        ResearchPackageRegistryError,
        match="research_package_operational_field_forbidden",
    ):
        _build_package(base)


@pytest.mark.parametrize(
    "sensitive_payload",
    (
        {"password": "not-allowed"},
        {"access_token": "not-allowed"},
        {"private_key": "not-allowed"},
        {"dataset_path": "/external/data.csv"},
        {"source_uri": "/external/unreviewed-source.json"},
        {"review_note": "submit orders to the broker"},
        {"review_note": "/external/secret/location"},
    ),
)
def test_final_package_rejects_secrets_paths_and_operational_command_values(
    sensitive_payload: dict[str, str],
) -> None:
    base = _base_package()
    base["review_metadata"] = sensitive_payload
    base["content_hash"] = sha256_prefixed(
        {key: value for key, value in base.items() if key != "content_hash"}
    )

    with pytest.raises(
        ResearchPackageRegistryError,
        match="research_package_operational_(?:field|value)_forbidden",
    ):
        _build_package(base)


def test_final_manifest_projects_out_canonical_source_registry_paths() -> None:
    base = _base_package()
    base["knowledge_registry_path"] = "/external/authority/knowledge.jsonl"
    base["content_hash"] = sha256_prefixed(
        {key: value for key, value in base.items() if key != "content_hash"}
    )

    package, _, _, _ = _build_package(base)

    assert "knowledge_registry_path" not in package.as_dict()["source_package"]


def test_final_manifest_preserves_reviewed_intra_candle_path_semantics() -> None:
    base = _base_package()
    timing = base["signal_calculation_timing"]
    assert isinstance(timing, dict)
    timing["intra_candle_path_required"] = False
    base["known_limitations"] = {
        "data": {"intra_candle_path_available": False},
        "execution": ["queue position unavailable"],
    }
    base["content_hash"] = sha256_prefixed(
        {key: value for key, value in base.items() if key != "content_hash"}
    )

    package, _, _, _ = _build_package(base)
    payload = package.as_dict()

    assert (
        payload["source_package"]["signal_calculation_timing"][
            "intra_candle_path_required"
        ]
        is False
    )
    assert (
        payload["assumptions"]["point_in_time_and_signal_timing"][
            "intra_candle_path_required"
        ]
        is False
    )
    assert (
        payload["limitations"]["known_limitations"]["data"][
            "intra_candle_path_available"
        ]
        is False
    )
    assert package.refs.source_package.content_hash == base["content_hash"]


def test_final_manifest_projects_instrument_source_locations_but_keeps_authority() -> (
    None
):
    base = _base_package()
    _bind_instrument_source_authorities(base)

    package, spec, _, _ = _build_package(base)
    payload = package.as_dict()
    source_target = payload["source_package"]["target_asset"]
    rule_target = payload["validated_rule_set"]["applicability"]["target_asset"]
    raw_target = base["target_asset"]
    assert isinstance(raw_target, dict)
    raw_evidence = raw_target["instrument_evidence"]
    assert isinstance(raw_evidence, dict)
    for target in (source_target, rule_target):
        evidence = target["instrument_evidence"]
        assert evidence["instrument_id"] == "BTC"
        for authority_name in ("market_calendar", "point_in_time_universe"):
            raw_authority = raw_evidence[authority_name]
            assert isinstance(raw_authority, dict)
            projected_authority = evidence[authority_name]
            assert "source_uri" not in projected_authority
            assert projected_authority == {
                key: value
                for key, value in raw_authority.items()
                if key != "source_uri"
            }
    assert package.index.instrument == "BTC"
    assert package.refs.source_package.content_hash == base["content_hash"]
    assert package.validated_rule_set_hash == spec.validated_rule_set_hash
    assert package.validated_rule_set_hash == validated_rule_set_content_hash(base)


def test_final_manifest_rejects_incomplete_instrument_source_authority() -> None:
    base = _base_package()
    _bind_instrument_source_authorities(base)
    target = base["target_asset"]
    assert isinstance(target, dict)
    evidence = target["instrument_evidence"]
    assert isinstance(evidence, dict)
    calendar = evidence["market_calendar"]
    assert isinstance(calendar, dict)
    calendar.pop("source_schema_hash")
    base["content_hash"] = sha256_prefixed(
        {key: value for key, value in base.items() if key != "content_hash"}
    )

    with pytest.raises(
        ResearchPackageRegistryError,
        match=(
            "research_package_instrument_source_authority_"
            "market_calendar_source_schema_hash_invalid"
        ),
    ):
        _build_package(base)


def test_manifest_parser_rejects_unknown_top_level_nested_and_ref_fields() -> None:
    package, _, _, _ = _build_package(_base_package())

    top_level = package.as_dict()
    top_level["unknown"] = True
    with pytest.raises(ResearchPackageRegistryError, match="fields_invalid"):
        ResearchPackageManifest.from_dict(top_level)

    nested = package.as_dict()
    nested["prospective_validation"]["payload"]["unknown"] = True
    nested["prospective_validation"]["content_hash"] = sha256_prefixed(
        nested["prospective_validation"]["payload"],
        label="prospective_validation_spec",
    )
    nested["refs"]["prospective_validation"]["content_hash"] = nested[
        "prospective_validation"
    ]["content_hash"]
    nested["content_hash"] = sha256_prefixed(
        {key: value for key, value in nested.items() if key != "content_hash"},
        label="research_package_manifest",
    )
    with pytest.raises(ResearchPackageRegistryError, match="fields_invalid"):
        ResearchPackageManifest.from_dict(nested)

    ref_payload = package.as_dict()
    ref_payload["refs"]["dataset_snapshot"]["unknown"] = "forbidden"
    with pytest.raises(ResearchPackageRegistryError, match="fields_invalid"):
        ResearchPackageManifest.from_dict(ref_payload)


def test_publish_rejects_fabricated_refs_without_authoritative_targets(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    package, _, _, _ = _build_package(_base_package())

    with pytest.raises(
        ResearchPackageRegistryError,
        match="research_package_hypothesis_reference_unresolved",
    ):
        ResearchPackageRegistry(manager).publish(package)
    assert not research_package_registry_path(manager).exists()


def test_hash_chained_but_semantically_incomplete_authority_row_is_rejected(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    package, _, _, _ = _build_package(_base_package(), manager=manager)
    fake_payload = {"schema_version": 999, "claim": "hash-chain-only forgery"}
    append_hash_chained_jsonl_idempotent(
        store=ArtifactStore(root=manager.artifact_root),
        path=knowledge_registry_path(manager),
        payload={
            "event_id": "hypothesis:hash-chain-only-forgery:1",
            "record_type": "hypothesis",
            "logical_id": "hash-chain-only-forgery",
            "version": "1",
            "record_hash": sha256_prefixed(fake_payload),
            "payload": fake_payload,
        },
        label=KNOWLEDGE_REGISTRY_HASH_LABEL,
    )
    assert validate_knowledge_registry(manager)["status"] == "FAIL"

    with pytest.raises(
        ResearchPackageRegistryError,
        match="knowledge_registry_semantic_invalid",
    ):
        ResearchPackageRegistry(manager).publish(package)


def test_cross_study_dataset_substitution_is_rejected(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    first, first_spec, first_evaluation, first_conclusion = _build_package(
        _base_package(), manager=manager
    )
    second, second_spec, second_evaluation, second_conclusion = _build_package(
        _base_package(
            market="KRW-ETH",
            instrument="ETH",
            phenomenon="mean_reversion",
            run_suffix="two",
        ),
        package_id="other-package",
        validation_id="pv-two",
        dataset_id="dataset-two",
        manager=manager,
    )
    assert validate_knowledge_registry(manager)["status"] == "PASS"
    assert validate_validation_decision_registry(manager)["status"] == "PASS"
    assert validate_prospective_registry(manager)["status"] == "PASS"

    substituted = first.as_dict()
    other_ref = second.refs.dataset_snapshot.as_dict()
    substituted["refs"]["dataset_snapshot"] = other_ref
    substituted["index"]["dataset_id"] = other_ref["logical_id"]
    substituted["index"]["dataset_hash"] = other_ref["content_hash"]
    substituted["reproduction_recipe"]["arguments"]["dataset_snapshot_ref"] = other_ref
    substituted["reproduction_recipe"]["data_access"]["dataset_snapshot_ref"] = (
        other_ref
    )
    substituted["content_hash"] = sha256_prefixed(
        {key: value for key, value in substituted.items() if key != "content_hash"},
        label="research_package_manifest",
    )
    cross_study = ResearchPackageManifest.from_dict(substituted)

    with pytest.raises(
        ResearchPackageRegistryError,
        match="research_package_dataset_snapshot_authority_mismatch",
    ):
        ResearchPackageRegistry(manager).publish(cross_study)


def test_deleted_and_tampered_authoritative_targets_fail_registry_validation(
    tmp_path: Path,
) -> None:
    deleted_manager = _manager(tmp_path / "deleted")
    package, spec, evaluation, conclusion = _build_package(
        _base_package(), manager=deleted_manager
    )
    deleted_registry = ResearchPackageRegistry(deleted_manager)
    deleted_registry.publish(package)
    receipt_path = deleted_manager.report_path(
        "research",
        package.refs.experiment_spec.logical_id,
        "reproduction_receipt.json",
    )
    receipt_path.unlink()
    assert validate_research_package_registry(deleted_manager)["status"] == "FAIL"
    with pytest.raises(
        ResearchPackageRegistryError,
        match="research_package_reproduction_receipt_unresolved",
    ):
        deleted_registry.get(package.package_id, package.version)

    tampered_manager = _manager(tmp_path / "tampered")
    tampered, t_spec, t_evaluation, t_conclusion = _build_package(
        _base_package(), manager=tampered_manager
    )
    ResearchPackageRegistry(tampered_manager).publish(tampered)
    report = _terminal_report(
        tampered.as_dict()["source_package"],
        validation_id=t_spec.validation_id,
        dataset_id=tampered.refs.dataset_snapshot.logical_id,
    )
    decision, _ = _decision_payload(manager=tampered_manager, report=report)
    raw_report_ref = decision["terminal_report_ref"]
    assert isinstance(raw_report_ref, dict)
    report["end_to_end_validation_result"] = "FAIL"
    write_json_atomic(Path(str(raw_report_ref["artifact_path"])), report)
    assert validate_research_package_registry(tampered_manager)["status"] == "FAIL"


def test_search_lineage_and_diff_cover_review_critical_dimensions(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    registry = ResearchPackageRegistry(manager)
    first, _, _, _ = _build_package(_base_package(), manager=manager)
    registry.publish(first)
    second_base = _base_package(
        market="KRW-ETH",
        instrument="ETH",
        phenomenon="mean_reversion",
        threshold=2.0,
        limitation="ETH-only evidence",
        run_suffix="two",
    )
    second, _, _, _ = _build_package(
        second_base,
        version="2",
        validation_id="pv-two",
        dataset_id="dataset-two",
        researcher="researcher-b",
        rationale="updated hypothesis remains prospectively confirmed",
        limitation="ETH prospective sample is still small",
        supersedes=first.ref(),
        manager=manager,
    )
    registry.publish(second)

    matches = registry.search(
        market="KRW-BTC",
        instrument="BTC",
        hypothesis_type="momentum",
        status="PASS",
        researcher="researcher-a",
        dataset="dataset-one",
        period_start="2026-01-15T00:00:00+00:00",
        period_end="2026-01-20T00:00:00+00:00",
        prospective_status="CONFIRMED",
    )
    assert matches == (first,)
    assert registry.search(dataset=second.index.dataset_hash) == (second,)

    lineage = registry.lineage(second.package_id, second.version)
    assert lineage["supersedes_chain"] == [first.ref().as_dict()]
    assert registry.lineage(first.package_id, first.version)["direct_descendants"] == [
        second.ref().as_dict()
    ]
    assert lineage["evidence_refs"]["dataset_snapshot"] == (
        second.refs.dataset_snapshot.as_dict()
    )

    package_diff = registry.diff(
        first.package_id,
        first.version,
        second.package_id,
        second.version,
    )
    assert package_diff == diff_research_packages(first, second)
    for section in (
        "hypothesis",
        "validated_rule_set",
        "data",
        "result",
        "limitations",
        "assumptions",
    ):
        assert package_diff["changes"][section]["changed"] is True
    assert "$.validated_rule_set.rule_spec.entry.value" in package_diff["changed_paths"]
