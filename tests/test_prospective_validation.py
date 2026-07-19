from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.artifact_store import ArtifactStore
from market_research.research.governance import (
    GovernanceSubject,
    GovernanceSubjectType,
    append_lifecycle_transition,
    current_lifecycle_state,
)
from market_research.research.prospective_application import (
    ProspectiveValidationApplicationService,
)
from market_research.research.hash_chain import append_hash_chained_jsonl_idempotent
from market_research.research import prospective_validation as prospective_module
from market_research.research.prospective_validation import (
    PROSPECTIVE_VALIDATION_SCHEMA_VERSION,
    ImmutableEvidenceRef,
    MetricGuard,
    ProspectiveObservation,
    ProspectiveStatus,
    ProspectiveValidationError,
    ProspectiveValidationSpec,
    SimulatedFillEvidence,
    build_research_conclusion,
    evaluate_prospective_validation,
    prospective_observation_path,
    publish_prospective_spec,
    publish_research_conclusion,
    record_prospective_observation,
    validate_prospective_registry,
    verify_published_prospective_conclusion,
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


def _hash(char: str) -> str:
    return "sha256:" + char * 64


def _ref(authority: str, logical_id: str, char: str) -> ImmutableEvidenceRef:
    return ImmutableEvidenceRef(
        authority=authority,
        logical_id=logical_id,
        version="1",
        content_hash=_hash(char),
    )


def _guards(*, strict_expected_value: bool = False) -> tuple[MetricGuard, ...]:
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
    values = []
    for name in names:
        if name == "expected_value" and strict_expected_value:
            values.append(
                MetricGuard(
                    metric=name,
                    historical_value=0.02,
                    degradation_lower=0.0,
                    invalidation_lower=-0.05,
                )
            )
        else:
            values.append(
                MetricGuard(
                    metric=name,
                    historical_value=0.0,
                    degradation_lower=-1_000_000_000.0,
                    degradation_upper=1_000_000_000.0,
                    invalidation_lower=-2_000_000_000.0,
                    invalidation_upper=2_000_000_000.0,
                )
            )
    return tuple(values)


def _spec(
    *,
    minimum_observations: int = 2,
    guards: tuple[MetricGuard, ...] | None = None,
    maximum_missing_rate: float = 0.25,
    maximum_late_rate: float = 0.25,
    maximum_latency_seconds: float = 30.0,
) -> ProspectiveValidationSpec:
    return ProspectiveValidationSpec(
        schema_version=PROSPECTIVE_VALIDATION_SCHEMA_VERSION,
        validation_id="pv-edge-001",
        version="1",
        source_package_ref=_ref("research_package_registry", "package-edge", "a"),
        hypothesis_ref=_ref("knowledge_registry", "hypothesis-edge", "b"),
        validation_decision_ref=_ref(
            "knowledge_registry", "validation-decision-edge", "c"
        ),
        validated_rule_set_hash=_hash("d"),
        feature_definition_hash=_hash("e"),
        cost_assumption_hash=_hash("f"),
        fill_assumption_hash=_hash("1"),
        historical_distribution_hash=_hash("2"),
        metric_guards=guards or _guards(),
        frozen_at="2026-01-01T00:00:00+00:00",
        start_at="2026-01-02T00:00:00+00:00",
        end_at="2026-01-05T00:00:00+00:00",
        minimum_observations=minimum_observations,
        minimum_elapsed_seconds=3600,
        maximum_missing_rate=maximum_missing_rate,
        maximum_late_rate=maximum_late_rate,
        maximum_latency_seconds=maximum_latency_seconds,
        stopping_rules=("invalidation boundary crossed after minimum sample",),
        review_rules=("review data quality and metric degradation",),
        frozen_by="researcher-a",
    )


def _fill(
    fill_id: str,
    occurred_at: str,
    *,
    realized_return: float,
) -> SimulatedFillEvidence:
    return SimulatedFillEvidence(
        simulated_fill_id=fill_id,
        occurred_at=occurred_at,
        side="SELL",
        quantity=1.0,
        price=101.0,
        cost=0.001,
        realized_return=realized_return,
        holding_period_seconds=1800.0,
        execution_assumption_hash=_hash("1"),
        cost_assumption_hash=_hash("f"),
    )


def _observation(
    observation_id: str,
    source_event_at: str,
    *,
    received_at: str,
    fill: SimulatedFillEvidence,
    normalize_fill_at_receipt: bool = True,
) -> ProspectiveObservation:
    if normalize_fill_at_receipt and fill.occurred_at < received_at:
        fill = replace(fill, occurred_at=received_at)
    return ProspectiveObservation(
        schema_version=PROSPECTIVE_VALIDATION_SCHEMA_VERSION,
        observation_id=observation_id,
        source_event_id=f"source-{observation_id}",
        source_event_at=source_event_at,
        data_available_at=source_event_at,
        received_at=received_at,
        signal_generated_at=received_at,
        expected_signal="EXIT_LONG",
        data_status="AVAILABLE",
        actual_data_hash=_hash("3"),
        feature_values_hash=_hash("4"),
        simulated_fill=fill,
    )


def test_confirmed_stream_is_frozen_auditable_and_conclusion_linked(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    spec = _spec()
    published = publish_prospective_spec(manager=manager, spec=spec)
    assert publish_prospective_spec(manager=manager, spec=spec) == published

    first_time = "2026-01-02T01:00:00+00:00"
    second_time = "2026-01-02T02:00:00+00:00"
    record_prospective_observation(
        manager=manager,
        spec=spec,
        observation=_observation(
            "obs-1",
            first_time,
            received_at="2026-01-02T01:00:05+00:00",
            fill=_fill("fill-1", "2026-01-02T01:00:05+00:00", realized_return=0.02),
        ),
    )
    record_prospective_observation(
        manager=manager,
        spec=spec,
        observation=_observation(
            "obs-2",
            second_time,
            received_at="2026-01-02T02:00:05+00:00",
            fill=_fill("fill-2", "2026-01-02T02:00:05+00:00", realized_return=0.01),
        ),
    )

    evaluation = evaluate_prospective_validation(
        manager=manager,
        spec=spec,
        evaluated_at="2026-01-03T00:00:00+00:00",
    )
    assert evaluation.status == ProspectiveStatus.CONFIRMED
    assert evaluation.outcome_count == 2
    assert evaluation.observed_metrics["expected_value"] == pytest.approx(0.015)
    assert prospective_observation_path(manager, spec).is_file()

    conclusion = build_research_conclusion(
        spec=spec,
        evaluation=evaluation,
        conclusion_id="conclusion-edge-001",
        version="1",
        rationale="The frozen prospective criteria were satisfied.",
        known_limitations=("Short prospective horizon",),
        decided_by="reviewer-a",
        decided_at="2026-01-03T01:00:00+00:00",
    )
    receipt = publish_research_conclusion(
        manager=manager,
        spec=spec,
        evaluation=evaluation,
        conclusion=conclusion,
    )
    assert receipt["record_hash"] == conclusion.content_hash()
    assert validate_prospective_registry(manager)["status"] == "PASS"
    assert (
        verify_published_prospective_conclusion(
            manager=manager,
            spec=spec,
            evaluation=evaluation,
            conclusion=conclusion,
        )["research_conclusion_row_hash"]
        == receipt["row_hash"]
    )

    with pytest.raises(ProspectiveValidationError, match="already_closed"):
        record_prospective_observation(
            manager=manager,
            spec=spec,
            observation=_observation(
                "obs-after-close",
                "2026-01-03T02:00:00+00:00",
                received_at="2026-01-03T02:00:01+00:00",
                fill=_fill(
                    "fill-after-close",
                    "2026-01-03T02:00:01+00:00",
                    realized_return=0.01,
                ),
            ),
        )


def test_small_losing_sample_remains_inconclusive(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    spec = _spec(minimum_observations=3, guards=_guards(strict_expected_value=True))
    publish_prospective_spec(manager=manager, spec=spec)
    source_time = "2026-01-02T01:00:00+00:00"
    record_prospective_observation(
        manager=manager,
        spec=spec,
        observation=_observation(
            "obs-loss",
            source_time,
            received_at="2026-01-02T01:00:01+00:00",
            fill=_fill("fill-loss", "2026-01-02T01:00:01+00:00", realized_return=-0.20),
        ),
    )

    evaluation = evaluate_prospective_validation(
        manager=manager,
        spec=spec,
        evaluated_at="2026-01-03T00:00:00+00:00",
    )

    assert evaluation.status == ProspectiveStatus.INCONCLUSIVE
    assert evaluation.stopping_triggered is False
    assert any("minimum_observations_not_met" in item for item in evaluation.reasons)
    assert any(
        row["metric"] == "expected_value" and row["classification"] == "INVALIDATED"
        for row in evaluation.comparison
    )


def test_missing_and_late_arrivals_are_preserved_and_degrade_result(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    spec = _spec(
        minimum_observations=1,
        maximum_missing_rate=0.10,
        maximum_late_rate=0.10,
        maximum_latency_seconds=10.0,
    )
    publish_prospective_spec(manager=manager, spec=spec)
    source_time = "2026-01-02T01:00:00+00:00"
    available = _observation(
        "obs-late",
        source_time,
        received_at="2026-01-02T01:02:00+00:00",
        fill=_fill("fill-late", "2026-01-02T01:02:00+00:00", realized_return=0.01),
    )
    missing = ProspectiveObservation(
        schema_version=PROSPECTIVE_VALIDATION_SCHEMA_VERSION,
        observation_id="obs-missing",
        source_event_id="source-obs-missing",
        source_event_at="2026-01-02T02:00:00+00:00",
        received_at="2026-01-02T02:05:00+00:00",
        signal_generated_at="2026-01-02T02:05:00+00:00",
        expected_signal="DATA_MISSING",
        data_status="MISSING",
        actual_data_hash=None,
        data_available_at=None,
        feature_values_hash=None,
        simulated_fill=None,
        notes=("Source row was not available at the scheduled check.",),
    )
    record_prospective_observation(manager=manager, spec=spec, observation=available)
    record_prospective_observation(manager=manager, spec=spec, observation=missing)

    evaluation = evaluate_prospective_validation(
        manager=manager,
        spec=spec,
        evaluated_at="2026-01-03T00:00:00+00:00",
    )

    assert evaluation.status == ProspectiveStatus.DEGRADED
    assert evaluation.missing_count == 1
    assert evaluation.late_count == 1
    assert evaluation.missing_rate == 0.5
    assert evaluation.late_rate == 1.0


def test_frozen_spec_and_observations_reject_content_reuse(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    spec = _spec()
    publish_prospective_spec(manager=manager, spec=spec)

    with pytest.raises(ValueError, match="event_id_conflict"):
        publish_prospective_spec(
            manager=manager,
            spec=replace(spec, validated_rule_set_hash=_hash("9")),
        )

    source_time = "2026-01-02T01:00:00+00:00"
    observation = _observation(
        "obs-immutable",
        source_time,
        received_at="2026-01-02T01:00:01+00:00",
        fill=_fill(
            "fill-immutable",
            "2026-01-02T01:00:01+00:00",
            realized_return=0.01,
        ),
    )
    record_prospective_observation(manager=manager, spec=spec, observation=observation)
    with pytest.raises(ValueError, match="event_id_conflict"):
        record_prospective_observation(
            manager=manager,
            spec=spec,
            observation=replace(observation, actual_data_hash=_hash("8")),
        )


def test_contract_rejects_post_start_freeze_and_non_simulated_fill() -> None:
    with pytest.raises(ProspectiveValidationError, match="not_frozen_before_start"):
        replace(_spec(), frozen_at="2026-01-03T00:00:00+00:00")

    with pytest.raises(ProspectiveValidationError, match="only_simulated"):
        replace(
            _fill(
                "fill-boundary",
                "2026-01-02T01:00:00+00:00",
                realized_return=0.01,
            ),
            evidence_type="BROKER_FILL",
        )

    with pytest.raises(ProspectiveValidationError, match="requires_supersedes"):
        replace(_spec(), version="2")


def test_application_service_joins_prospective_evidence_to_governance(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    spec = _spec()
    subject = GovernanceSubject(
        GovernanceSubjectType.HYPOTHESIS,
        spec.hypothesis_ref.logical_id,
        spec.hypothesis_ref.version,
    )
    transitions = (
        (None, "IDEA", {"hypothesis_semantic_fingerprint": _hash("5")}),
        ("IDEA", "HYPOTHESIS_DEFINED", {"hypothesis_contract_hash": _hash("b")}),
        ("HYPOTHESIS_DEFINED", "EXPLORING", {}),
        ("EXPLORING", "VALIDATING", {"validation_manifest_hash": _hash("6")}),
        ("VALIDATING", "SUPPORTED", {"validation_report_hash": _hash("7")}),
    )
    for source, target, evidence in transitions:
        append_lifecycle_transition(
            manager=manager,
            subject=subject,
            from_state=source,
            to_state=target,
            actor_id="researcher-a",
            reason=f"prepare prospective integration: {target}",
            evidence_hashes=evidence,
            recorded_at="2026-01-01T00:00:00+00:00",
        )

    service = ProspectiveValidationApplicationService(manager)
    start = service.start(
        spec=spec,
        actor_id="researcher-a",
        reason="Begin the frozen prospective study.",
        recorded_at="2026-01-01T00:00:00+00:00",
    )
    assert start["lifecycle_state"] == "PROSPECTIVE_VALIDATION"
    for index, realized_return in ((1, 0.02), (2, 0.01)):
        event_at = f"2026-01-02T0{index}:00:00+00:00"
        service.record(
            spec=spec,
            observation=_observation(
                f"obs-service-{index}",
                event_at,
                received_at=f"2026-01-02T0{index}:00:01+00:00",
                fill=_fill(
                    f"fill-service-{index}",
                    f"2026-01-02T0{index}:00:01+00:00",
                    realized_return=realized_return,
                ),
            ),
        )

    result = service.evaluate_and_conclude(
        spec=spec,
        evaluated_at="2026-01-03T00:00:00+00:00",
        conclusion_id="conclusion-service-001",
        conclusion_version="1",
        rationale="Prospective evidence remained inside every frozen boundary.",
        known_limitations=("Short prospective horizon",),
        decided_by="reviewer-a",
        decided_at="2026-01-03T01:00:00+00:00",
        transition_reason="Accept the immutable prospective conclusion.",
    )

    assert result["lifecycle_state"] == "CONFIRMED"
    assert current_lifecycle_state(manager=manager, subject=subject) == "CONFIRMED"
    assert validate_prospective_registry(manager)["status"] == "PASS"


def test_fill_timeline_no_fill_and_cost_binding_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ProspectiveValidationError, match="fill_before_signal"):
        _observation(
            "obs-pre-signal",
            "2026-01-02T01:00:00+00:00",
            received_at="2026-01-02T01:00:05+00:00",
            fill=_fill(
                "fill-pre-signal",
                "2026-01-02T01:00:04+00:00",
                realized_return=0.01,
            ),
            normalize_fill_at_receipt=False,
        )

    with pytest.raises(ProspectiveValidationError, match="no_fill_values_must_be_zero"):
        replace(
            _fill(
                "fill-invalid-no-fill",
                "2026-01-02T01:00:05+00:00",
                realized_return=0.01,
            ),
            side="NO_FILL",
        )

    manager = _manager(tmp_path)
    spec = _spec(minimum_observations=1)
    publish_prospective_spec(manager=manager, spec=spec)
    mismatched_cost = replace(
        _fill(
            "fill-cost-mismatch",
            "2026-01-02T01:00:05+00:00",
            realized_return=0.01,
        ),
        cost_assumption_hash=_hash("9"),
    )
    with pytest.raises(
        ProspectiveValidationError, match="cost_assumption_hash_mismatch"
    ):
        record_prospective_observation(
            manager=manager,
            spec=spec,
            observation=_observation(
                "obs-cost-mismatch",
                "2026-01-02T01:00:00+00:00",
                received_at="2026-01-02T01:00:05+00:00",
                fill=mismatched_cost,
            ),
        )

    no_fill = SimulatedFillEvidence(
        simulated_fill_id="no-fill-valid",
        occurred_at="2026-01-02T01:00:05+00:00",
        side="NO_FILL",
        quantity=0.0,
        price=0.0,
        cost=0.0,
        realized_return=0.0,
        holding_period_seconds=0.0,
        execution_assumption_hash=spec.fill_assumption_hash,
        cost_assumption_hash=spec.cost_assumption_hash,
    )
    record_prospective_observation(
        manager=manager,
        spec=spec,
        observation=_observation(
            "obs-no-fill",
            "2026-01-02T01:00:00+00:00",
            received_at="2026-01-02T01:00:05+00:00",
            fill=no_fill,
        ),
    )
    evaluation = evaluate_prospective_validation(
        manager=manager,
        spec=spec,
        evaluated_at="2026-01-03T00:00:00+00:00",
    )
    assert evaluation.outcome_count == 0
    assert evaluation.status is ProspectiveStatus.INCONCLUSIVE


def test_future_fill_and_duplicate_source_or_fill_ids_are_rejected(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    spec = _spec(minimum_observations=1)
    publish_prospective_spec(manager=manager, spec=spec)
    first = _observation(
        "obs-first-unique",
        "2026-01-02T01:00:00+00:00",
        received_at="2026-01-02T01:00:05+00:00",
        fill=_fill(
            "fill-first-unique",
            "2026-01-02T01:00:05+00:00",
            realized_return=0.01,
        ),
    )
    record_prospective_observation(manager=manager, spec=spec, observation=first)
    second = _observation(
        "obs-second-unique",
        "2026-01-02T02:00:00+00:00",
        received_at="2026-01-02T02:00:05+00:00",
        fill=_fill(
            "fill-second-unique",
            "2026-01-02T02:00:05+00:00",
            realized_return=0.02,
        ),
    )
    with pytest.raises(ProspectiveValidationError, match="source_event_id_duplicate"):
        record_prospective_observation(
            manager=manager,
            spec=spec,
            observation=replace(second, source_event_id=first.source_event_id),
        )
    assert second.simulated_fill is not None
    with pytest.raises(ProspectiveValidationError, match="simulated_fill_id_duplicate"):
        record_prospective_observation(
            manager=manager,
            spec=spec,
            observation=replace(
                second,
                simulated_fill=replace(
                    second.simulated_fill,
                    simulated_fill_id=first.simulated_fill.simulated_fill_id
                    if first.simulated_fill is not None
                    else "unreachable",
                ),
            ),
        )

    future = _observation(
        "obs-future-fill",
        "2026-01-02T03:00:00+00:00",
        received_at="2026-01-02T03:00:05+00:00",
        fill=_fill(
            "fill-future",
            "2026-01-04T00:00:00+00:00",
            realized_return=0.03,
        ),
    )
    record_prospective_observation(manager=manager, spec=spec, observation=future)
    with pytest.raises(
        ProspectiveValidationError, match="evaluation_precedes_simulated_fill"
    ):
        evaluate_prospective_validation(
            manager=manager,
            spec=spec,
            evaluated_at="2026-01-03T00:00:00+00:00",
        )


def test_close_fence_prevents_append_from_crossing_evaluation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    spec = _spec(minimum_observations=1)
    publish_prospective_spec(manager=manager, spec=spec)
    record_prospective_observation(
        manager=manager,
        spec=spec,
        observation=_observation(
            "obs-before-close",
            "2026-01-02T01:00:00+00:00",
            received_at="2026-01-02T01:00:05+00:00",
            fill=_fill(
                "fill-before-close",
                "2026-01-02T01:00:05+00:00",
                realized_return=0.01,
            ),
        ),
    )
    snapshot_read = threading.Event()
    allow_close = threading.Event()
    recorder_done = threading.Event()
    original_read = prospective_module._read_observation_snapshot

    def blocking_read(*args, **kwargs):
        snapshot = original_read(*args, **kwargs)
        if threading.current_thread().name == "prospective-evaluator":
            snapshot_read.set()
            assert allow_close.wait(timeout=5)
        return snapshot

    monkeypatch.setattr(prospective_module, "_read_observation_snapshot", blocking_read)
    evaluation_errors: list[BaseException] = []
    recorder_errors: list[BaseException] = []

    def close_study() -> None:
        try:
            evaluate_prospective_validation(
                manager=manager,
                spec=spec,
                evaluated_at="2026-01-03T00:00:00+00:00",
            )
        except BaseException as exc:  # pragma: no cover - assertion diagnostics
            evaluation_errors.append(exc)

    def append_during_close() -> None:
        try:
            record_prospective_observation(
                manager=manager,
                spec=spec,
                observation=_observation(
                    "obs-racing-close",
                    "2026-01-02T02:00:00+00:00",
                    received_at="2026-01-02T02:00:05+00:00",
                    fill=_fill(
                        "fill-racing-close",
                        "2026-01-02T02:00:05+00:00",
                        realized_return=0.02,
                    ),
                ),
            )
        except BaseException as exc:
            recorder_errors.append(exc)
        finally:
            recorder_done.set()

    evaluator = threading.Thread(target=close_study, name="prospective-evaluator")
    evaluator.start()
    assert snapshot_read.wait(timeout=5)
    recorder = threading.Thread(target=append_during_close, name="prospective-recorder")
    recorder.start()
    assert not recorder_done.wait(timeout=0.1)
    allow_close.set()
    evaluator.join(timeout=5)
    recorder.join(timeout=5)

    assert not evaluation_errors
    assert len(recorder_errors) == 1
    assert "already_closed" in str(recorder_errors[0])
    assert validate_prospective_registry(manager)["status"] == "PASS"


def test_validator_detects_deleted_or_post_close_observation_stream(
    tmp_path: Path,
) -> None:
    def closed_study(root: Path):
        manager = _manager(root)
        spec = _spec(minimum_observations=1)
        publish_prospective_spec(manager=manager, spec=spec)
        record_prospective_observation(
            manager=manager,
            spec=spec,
            observation=_observation(
                "obs-closed-stream",
                "2026-01-02T01:00:00+00:00",
                received_at="2026-01-02T01:00:05+00:00",
                fill=_fill(
                    "fill-closed-stream",
                    "2026-01-02T01:00:05+00:00",
                    realized_return=0.01,
                ),
            ),
        )
        evaluation = evaluate_prospective_validation(
            manager=manager,
            spec=spec,
            evaluated_at="2026-01-03T00:00:00+00:00",
        )
        conclusion = build_research_conclusion(
            spec=spec,
            evaluation=evaluation,
            conclusion_id=f"conclusion-{root.name}",
            version="1",
            rationale="Frozen criteria were satisfied before tamper simulation.",
            known_limitations=("Tamper simulation fixture",),
            decided_by="reviewer-a",
            decided_at="2026-01-03T01:00:00+00:00",
        )
        return manager, spec, evaluation, conclusion

    deleted_manager, deleted_spec, deleted_evaluation, deleted_conclusion = (
        closed_study(tmp_path / "deleted")
    )
    prospective_observation_path(deleted_manager, deleted_spec).unlink()
    deleted_validation = validate_prospective_registry(deleted_manager)
    assert deleted_validation["status"] == "FAIL"
    assert any(
        "observation_stream" in reason for reason in deleted_validation["reasons"]
    )
    with pytest.raises(ProspectiveValidationError, match="observation_stream"):
        publish_research_conclusion(
            manager=deleted_manager,
            spec=deleted_spec,
            evaluation=deleted_evaluation,
            conclusion=deleted_conclusion,
        )

    append_manager, append_spec, append_evaluation, append_conclusion = closed_study(
        tmp_path / "appended"
    )
    extra = _observation(
        "obs-raw-post-close",
        "2026-01-02T02:00:00+00:00",
        received_at="2026-01-02T02:00:05+00:00",
        fill=_fill(
            "fill-raw-post-close",
            "2026-01-02T02:00:05+00:00",
            realized_return=0.02,
        ),
    )
    append_hash_chained_jsonl_idempotent(
        store=ArtifactStore(root=append_manager.artifact_root),
        path=prospective_observation_path(append_manager, append_spec),
        payload={
            "event_id": f"observation:{extra.observation_id}",
            "record_type": "PROSPECTIVE_OBSERVATION",
            "validation_id": append_spec.validation_id,
            "validation_version": append_spec.version,
            "spec_hash": append_spec.contract_hash(),
            "payload": extra.as_dict(),
        },
        label=(
            f"prospective_observation_{append_spec.validation_id}_{append_spec.version}"
        ),
    )
    appended_validation = validate_prospective_registry(append_manager)
    assert appended_validation["status"] == "FAIL"
    assert any(
        "observation_stream" in reason for reason in appended_validation["reasons"]
    )
    with pytest.raises(ProspectiveValidationError, match="observation_stream"):
        publish_research_conclusion(
            manager=append_manager,
            spec=append_spec,
            evaluation=append_evaluation,
            conclusion=append_conclusion,
        )


def test_evaluation_is_semantically_immutable_and_forged_conclusion_is_rejected(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    spec = _spec(minimum_observations=1)
    publish_prospective_spec(manager=manager, spec=spec)
    record_prospective_observation(
        manager=manager,
        spec=spec,
        observation=_observation(
            "obs-semantic-evaluation",
            "2026-01-02T01:00:00+00:00",
            received_at="2026-01-02T01:00:05+00:00",
            fill=_fill(
                "fill-semantic-evaluation",
                "2026-01-02T01:00:05+00:00",
                realized_return=0.01,
            ),
        ),
    )
    evaluation = evaluate_prospective_validation(
        manager=manager,
        spec=spec,
        evaluated_at="2026-01-03T00:00:00+00:00",
    )
    with pytest.raises(TypeError):
        evaluation.observed_metrics["expected_value"] = 999.0  # type: ignore[index]
    with pytest.raises(ProspectiveValidationError, match="stream_row_count_mismatch"):
        replace(evaluation, observation_stream_row_count=999)

    conclusion = build_research_conclusion(
        spec=spec,
        evaluation=evaluation,
        conclusion_id="conclusion-semantic-evaluation",
        version="1",
        rationale="The immutable evaluation supports this conclusion.",
        known_limitations=("Short horizon",),
        decided_by="reviewer-a",
        decided_at="2026-01-03T01:00:00+00:00",
    )
    with pytest.raises(ProspectiveValidationError, match="status_mismatch"):
        publish_research_conclusion(
            manager=manager,
            spec=spec,
            evaluation=evaluation,
            conclusion=replace(conclusion, status=ProspectiveStatus.DEGRADED),
        )
    with pytest.raises(
        ProspectiveValidationError, match="hypothesis_reference_mismatch"
    ):
        publish_research_conclusion(
            manager=manager,
            spec=spec,
            evaluation=evaluation,
            conclusion=replace(
                conclusion,
                hypothesis_ref=_ref("knowledge_registry", "different-hypothesis", "8"),
            ),
        )
    with pytest.raises(
        ProspectiveValidationError, match="before_prospective_evaluation"
    ):
        publish_research_conclusion(
            manager=manager,
            spec=spec,
            evaluation=evaluation,
            conclusion=replace(
                conclusion,
                decided_at="2026-01-02T23:59:59+00:00",
            ),
        )
    publish_research_conclusion(
        manager=manager,
        spec=spec,
        evaluation=evaluation,
        conclusion=conclusion,
    )


def test_validator_recomputes_hash_chain_valid_evaluation_from_observations(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    spec = _spec(minimum_observations=1)
    publish_prospective_spec(manager=manager, spec=spec)
    record_prospective_observation(
        manager=manager,
        spec=spec,
        observation=_observation(
            "obs-forged-evaluation",
            "2026-01-02T01:00:00+00:00",
            received_at="2026-01-02T01:00:05+00:00",
            fill=_fill(
                "fill-forged-evaluation",
                "2026-01-02T01:00:05+00:00",
                realized_return=0.01,
            ),
        ),
    )
    snapshot = prospective_module._read_observation_snapshot(manager, spec)
    rebuilt = prospective_module._calculate_prospective_evaluation(
        spec=spec,
        snapshot=snapshot,
        evaluated_at="2026-01-03T00:00:00+00:00",
    )
    forged = replace(
        rebuilt,
        reasons=(*rebuilt.reasons, "attacker_supplied_reason"),
    )
    append_hash_chained_jsonl_idempotent(
        store=ArtifactStore(root=manager.artifact_root),
        path=prospective_module.prospective_registry_path(manager),
        payload={
            "event_id": f"evaluation:{spec.validation_id}:{spec.version}",
            "record_type": "PROSPECTIVE_EVALUATION",
            "logical_id": spec.validation_id,
            "version": spec.version,
            "record_hash": forged.content_hash(),
            "payload": forged.as_dict(),
        },
        label="prospective_validation",
    )

    validation = validate_prospective_registry(manager)

    assert validation["status"] == "FAIL"
    assert any(
        "semantic_recomputation_mismatch" in reason for reason in validation["reasons"]
    )


def test_publication_time_and_supersedes_must_resolve_exactly(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    spec = _spec()
    with pytest.raises(ProspectiveValidationError, match="outside_freeze_window"):
        publish_prospective_spec(
            manager=manager,
            spec=spec,
            published_at="2026-01-02T00:00:01+00:00",
        )
    publish_prospective_spec(
        manager=manager,
        spec=spec,
        published_at="2026-01-01T12:00:00+00:00",
    )
    revised = replace(spec, version="2", supersedes=spec.ref())
    publish_prospective_spec(
        manager=manager,
        spec=revised,
        published_at="2026-01-01T12:00:00+00:00",
    )
    forged_ref = replace(spec.ref(), content_hash=_hash("9"))
    with pytest.raises(ProspectiveValidationError, match="not_published"):
        publish_prospective_spec(
            manager=manager,
            spec=replace(spec, version="3", supersedes=forged_ref),
            published_at="2026-01-01T12:00:00+00:00",
        )
