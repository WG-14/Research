"""Production application authority for validated multi-asset research.

The pure product engines remain authoritative for economics.  This service
owns the cross-product execution boundary: it resolves immutable inputs,
executes typed T-01 through T-04 runners twice, derives T-05 from the repeated
economic objects, builds the existing validated study, publishes it, and emits
an immutable run manifest for both successful and failed attempts.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import re
import subprocess
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Protocol, Sequence, TypeVar

from market_research.paths import ResearchPathManager
from market_research.research.multi_asset.accounting import (
    ReportLedgerReconciliation,
)
from market_research.research.multi_asset.evidence import (
    PublishedMultiAssetStudy,
    ReproductionReceipt,
    ResearchEvidenceBindings,
    ScenarioObjectHashes,
    ValidatedMultiAssetStudy,
    compare_studies,
    evidence_hash,
    publish_validated_study,
    scenario_object_hashes,
)
from market_research.research.multi_asset.research_package import (
    ArtifactChecksum,
    BoundedEvidenceArtifactResolver,
    EvidenceArtifactRef,
    EvidenceArtifactRole,
    MultiAssetResearchPackageError,
    MultiAssetRunManifest,
    PublishedRunManifest,
    ResolvedEvidenceArtifact,
    RunStatus,
    RuntimeEnvironment,
    publish_failure_run_manifest,
    publish_run_manifest,
    reserve_run_id,
)
from market_research.research.multi_asset.study import (
    FuturesScenarioTrace,
    IntegratedScenarioTrace,
    OptionScenarioTrace,
    ReproducibilityScenarioTrace,
    SpotScenarioTrace,
    build_validated_multi_asset_study,
    reproduction_object_hashes,
)


MULTI_ASSET_EXPERIMENT_SCHEMA_VERSION = 1
_MANDATORY_SCENARIOS = ("T-01", "T-02", "T-03", "T-04", "T-05")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_QUALITY_FLAG = re.compile(r"^[A-Z][A-Z0-9_:-]{0,127}$")
_TraceT = TypeVar(
    "_TraceT",
    SpotScenarioTrace,
    FuturesScenarioTrace,
    OptionScenarioTrace,
    IntegratedScenarioTrace,
)


class MultiAssetApplicationError(ValueError):
    """A failed run with its immutable failure manifest attached."""

    def __init__(
        self,
        message: str,
        *,
        run_manifest: MultiAssetRunManifest,
        published_manifest: PublishedRunManifest,
    ) -> None:
        super().__init__(message)
        self.run_manifest = run_manifest
        self.published_manifest = published_manifest


class MultiAssetExperimentError(ValueError):
    """The declarative experiment specification is invalid."""


def _require_text(value: str, field_name: str) -> None:
    if not value or value.strip() != value:
        raise MultiAssetExperimentError(f"{field_name}_must_be_nonempty_and_trimmed")


def _require_id(value: str, field_name: str) -> None:
    if not _STABLE_ID.fullmatch(value):
        raise MultiAssetExperimentError(f"{field_name}_invalid")


def _timestamp(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MultiAssetExperimentError(f"{field_name}_invalid_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MultiAssetExperimentError(f"{field_name}_timezone_required")
    return parsed.astimezone(UTC)


def _sorted_parameters(
    values: Sequence[tuple[str, str]],
    *,
    field_name: str,
) -> tuple[tuple[str, str], ...]:
    result = tuple(values)
    names = [name for name, _ in result]
    if names != sorted(set(names)):
        raise MultiAssetExperimentError(f"{field_name}_must_be_sorted_and_unique")
    for name, value in result:
        _require_id(name, f"{field_name}.name")
        _require_text(value, f"{field_name}.value")
    return result


@dataclass(frozen=True, slots=True)
class HypothesisDefinition:
    logical_id: str
    version: str
    statement: str
    rationale: str
    expected_direction: str

    def __post_init__(self) -> None:
        _require_id(self.logical_id, "hypothesis.logical_id")
        _require_id(self.version, "hypothesis.version")
        _require_text(self.statement, "hypothesis.statement")
        _require_text(self.rationale, "hypothesis.rationale")
        _require_text(
            self.expected_direction,
            "hypothesis.expected_direction",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "logical_id": self.logical_id,
            "version": self.version,
            "statement": self.statement,
            "rationale": self.rationale,
            "expected_direction": self.expected_direction,
        }


@dataclass(frozen=True, slots=True)
class DataRange:
    start_at: str
    end_at: str

    def __post_init__(self) -> None:
        if _timestamp(self.end_at, "data_range.end_at") <= _timestamp(
            self.start_at,
            "data_range.start_at",
        ):
            raise MultiAssetExperimentError("data_range_order_invalid")

    def as_dict(self) -> dict[str, str]:
        return {"start_at": self.start_at, "end_at": self.end_at}


@dataclass(frozen=True, slots=True)
class UniverseDefinition:
    logical_id: str
    version: str
    instrument_ids: tuple[str, ...]
    asset_classes: tuple[str, ...]
    point_in_time_selection_rule: str

    def __post_init__(self) -> None:
        _require_id(self.logical_id, "universe.logical_id")
        _require_id(self.version, "universe.version")
        if not self.instrument_ids or self.instrument_ids != tuple(
            sorted(set(self.instrument_ids))
        ):
            raise MultiAssetExperimentError(
                "universe.instrument_ids_must_be_sorted_and_unique"
            )
        for instrument_id in self.instrument_ids:
            _require_id(instrument_id, "universe.instrument_id")
        if self.asset_classes != ("FUTURE", "OPTION", "SPOT"):
            raise MultiAssetExperimentError(
                "universe.asset_classes_must_cover_spot_future_option"
            )
        _require_text(
            self.point_in_time_selection_rule,
            "universe.point_in_time_selection_rule",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "logical_id": self.logical_id,
            "version": self.version,
            "instrument_ids": list(self.instrument_ids),
            "asset_classes": list(self.asset_classes),
            "point_in_time_selection_rule": self.point_in_time_selection_rule,
        }


@dataclass(frozen=True, slots=True)
class SignalDefinition:
    logical_id: str
    version: str
    expression: str
    observation_lag: str
    rebalance_frequency: str

    def __post_init__(self) -> None:
        _require_id(self.logical_id, "signal.logical_id")
        _require_id(self.version, "signal.version")
        _require_text(self.expression, "signal.expression")
        _require_text(self.observation_lag, "signal.observation_lag")
        _require_text(
            self.rebalance_frequency,
            "signal.rebalance_frequency",
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "logical_id": self.logical_id,
            "version": self.version,
            "expression": self.expression,
            "observation_lag": self.observation_lag,
            "rebalance_frequency": self.rebalance_frequency,
        }


@dataclass(frozen=True, slots=True)
class VersionedRule:
    logical_id: str
    version: str
    rule: str
    parameters: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _require_id(self.logical_id, "rule.logical_id")
        _require_id(self.version, "rule.version")
        _require_text(self.rule, "rule.rule")
        _sorted_parameters(self.parameters, field_name="rule.parameters")

    def as_dict(self) -> dict[str, object]:
        return {
            "logical_id": self.logical_id,
            "version": self.version,
            "rule": self.rule,
            "parameters": {name: value for name, value in self.parameters},
        }


@dataclass(frozen=True, slots=True)
class ScenarioDefinition:
    scenario_id: str
    logical_id: str
    version: str
    runner_id: str
    runner_version: str
    description: str
    parameters: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if self.scenario_id not in _MANDATORY_SCENARIOS:
            raise MultiAssetExperimentError("scenario.scenario_id_invalid")
        _require_id(self.logical_id, "scenario.logical_id")
        _require_id(self.version, "scenario.version")
        _require_id(self.runner_id, "scenario.runner_id")
        _require_id(self.runner_version, "scenario.runner_version")
        _require_text(self.description, "scenario.description")
        _sorted_parameters(
            self.parameters,
            field_name="scenario.parameters",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "logical_id": self.logical_id,
            "version": self.version,
            "runner_id": self.runner_id,
            "runner_version": self.runner_version,
            "description": self.description,
            "parameters": {name: value for name, value in self.parameters},
        }


@dataclass(frozen=True, slots=True)
class EvaluationMetric:
    logical_id: str
    version: str
    description: str
    unit: str
    higher_is_better: bool

    def __post_init__(self) -> None:
        _require_id(self.logical_id, "metric.logical_id")
        _require_id(self.version, "metric.version")
        _require_text(self.description, "metric.description")
        _require_text(self.unit, "metric.unit")

    def as_dict(self) -> dict[str, object]:
        return {
            "logical_id": self.logical_id,
            "version": self.version,
            "description": self.description,
            "unit": self.unit,
            "higher_is_better": self.higher_is_better,
        }


@dataclass(frozen=True, slots=True)
class MultiAssetExperimentSpec:
    """Complete, declarative Research Semantics v2 experiment authority."""

    experiment_id: str
    hypothesis: HypothesisDefinition
    data_range: DataRange
    universe: UniverseDefinition
    signal: SignalDefinition
    product_selection: VersionedRule
    roll_policy: VersionedRule
    exercise_policy: VersionedRule
    cost_policy: VersionedRule
    margin_policy: VersionedRule
    scenarios: tuple[ScenarioDefinition, ...]
    evaluation_metrics: tuple[EvaluationMetric, ...]
    seed: int
    code_logical_id: str
    code_version: str
    data_version: str
    dirty_worktree: bool
    frozen_at: str
    content_hash: str = field(init=False)
    research_semantics_version: int = 2
    schema_version: int = MULTI_ASSET_EXPERIMENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MULTI_ASSET_EXPERIMENT_SCHEMA_VERSION:
            raise MultiAssetExperimentError("experiment.schema_version_invalid")
        if self.research_semantics_version != 2:
            raise MultiAssetExperimentError("experiment.research_semantics_v2_required")
        _require_id(self.experiment_id, "experiment.experiment_id")
        if tuple(item.scenario_id for item in self.scenarios) != (_MANDATORY_SCENARIOS):
            raise MultiAssetExperimentError(
                "experiment.scenarios_must_be_ordered_t01_through_t05"
            )
        if (
            self.scenarios[4].runner_id != "multi-asset-application-reproduction"
            or self.scenarios[4].runner_version != "v1"
        ):
            raise MultiAssetExperimentError("experiment.t05_runner_authority_invalid")
        metric_ids = [
            (item.logical_id, item.version) for item in self.evaluation_metrics
        ]
        if not metric_ids or len(metric_ids) != len(set(metric_ids)):
            raise MultiAssetExperimentError("experiment.evaluation_metrics_invalid")
        rule_ids = [(item.logical_id, item.version) for item in self.policy_rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise MultiAssetExperimentError("experiment.policy_rule_identity_duplicate")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise MultiAssetExperimentError("experiment.seed_invalid")
        if self.seed < 0:
            raise MultiAssetExperimentError("experiment.seed_invalid")
        _require_id(self.code_logical_id, "experiment.code_logical_id")
        _require_id(self.code_version, "experiment.code_version")
        _require_id(self.data_version, "experiment.data_version")
        _timestamp(self.frozen_at, "experiment.frozen_at")
        object.__setattr__(
            self,
            "content_hash",
            evidence_hash(
                self.identity_payload(),
                label="multi-asset-experiment-spec",
            ),
        )

    @property
    def policy_rules(self) -> tuple[VersionedRule, ...]:
        return (
            self.product_selection,
            self.roll_policy,
            self.exercise_policy,
            self.cost_policy,
            self.margin_policy,
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "research_semantics_version": self.research_semantics_version,
            "experiment_id": self.experiment_id,
            "hypothesis": self.hypothesis.as_dict(),
            "data_range": self.data_range.as_dict(),
            "universe": self.universe.as_dict(),
            "signal": self.signal.as_dict(),
            "product_selection": self.product_selection.as_dict(),
            "roll_policy": self.roll_policy.as_dict(),
            "exercise_policy": self.exercise_policy.as_dict(),
            "cost_policy": self.cost_policy.as_dict(),
            "margin_policy": self.margin_policy.as_dict(),
            "scenarios": [item.as_dict() for item in self.scenarios],
            "evaluation_metrics": [item.as_dict() for item in self.evaluation_metrics],
            "seed": self.seed,
            "code_logical_id": self.code_logical_id,
            "code_version": self.code_version,
            "data_version": self.data_version,
            "dirty_worktree": self.dirty_worktree,
            "frozen_at": self.frozen_at,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def _mapping(
    value: object,
    field_name: str,
) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise MultiAssetExperimentError(f"{field_name}_object_required")
    return value


def _string(
    payload: Mapping[str, object],
    key: str,
    field_name: str,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise MultiAssetExperimentError(f"{field_name}_string_required")
    return value


def _parameters_from_dict(
    value: object,
    field_name: str,
) -> tuple[tuple[str, str], ...]:
    payload = _mapping(value, field_name)
    if any(not isinstance(item, str) for item in payload.values()):
        raise MultiAssetExperimentError(f"{field_name}_string_values_required")
    return tuple(sorted((name, str(item)) for name, item in payload.items()))


def _rule_from_dict(value: object, field_name: str) -> VersionedRule:
    payload = _mapping(value, field_name)
    if set(payload) != {"logical_id", "version", "rule", "parameters"}:
        raise MultiAssetExperimentError(f"{field_name}_fields_invalid")
    return VersionedRule(
        logical_id=_string(payload, "logical_id", field_name),
        version=_string(payload, "version", field_name),
        rule=_string(payload, "rule", field_name),
        parameters=_parameters_from_dict(
            payload["parameters"],
            f"{field_name}.parameters",
        ),
    )


def multi_asset_experiment_spec_from_dict(
    value: Mapping[str, object],
) -> MultiAssetExperimentSpec:
    """Decode and re-hash one strict Research Semantics v2 specification."""

    required = {
        "schema_version",
        "research_semantics_version",
        "experiment_id",
        "hypothesis",
        "data_range",
        "universe",
        "signal",
        "product_selection",
        "roll_policy",
        "exercise_policy",
        "cost_policy",
        "margin_policy",
        "scenarios",
        "evaluation_metrics",
        "seed",
        "code_logical_id",
        "code_version",
        "data_version",
        "dirty_worktree",
        "frozen_at",
        "content_hash",
    }
    if set(value) != required:
        raise MultiAssetExperimentError("experiment.fields_invalid")
    hypothesis_payload = _mapping(value["hypothesis"], "hypothesis")
    if set(hypothesis_payload) != {
        "logical_id",
        "version",
        "statement",
        "rationale",
        "expected_direction",
    }:
        raise MultiAssetExperimentError("hypothesis.fields_invalid")
    data_range_payload = _mapping(value["data_range"], "data_range")
    if set(data_range_payload) != {"start_at", "end_at"}:
        raise MultiAssetExperimentError("data_range.fields_invalid")
    universe_payload = _mapping(value["universe"], "universe")
    if set(universe_payload) != {
        "logical_id",
        "version",
        "instrument_ids",
        "asset_classes",
        "point_in_time_selection_rule",
    }:
        raise MultiAssetExperimentError("universe.fields_invalid")
    instrument_ids = universe_payload["instrument_ids"]
    asset_classes = universe_payload["asset_classes"]
    if (
        not isinstance(instrument_ids, list)
        or any(not isinstance(item, str) for item in instrument_ids)
        or not isinstance(asset_classes, list)
        or any(not isinstance(item, str) for item in asset_classes)
    ):
        raise MultiAssetExperimentError("universe.sequence_fields_invalid")
    signal_payload = _mapping(value["signal"], "signal")
    if set(signal_payload) != {
        "logical_id",
        "version",
        "expression",
        "observation_lag",
        "rebalance_frequency",
    }:
        raise MultiAssetExperimentError("signal.fields_invalid")
    scenario_values = value["scenarios"]
    if not isinstance(scenario_values, list):
        raise MultiAssetExperimentError("experiment.scenarios_array_required")
    scenarios: list[ScenarioDefinition] = []
    for raw_scenario in scenario_values:
        payload = _mapping(raw_scenario, "scenario")
        if set(payload) != {
            "scenario_id",
            "logical_id",
            "version",
            "runner_id",
            "runner_version",
            "description",
            "parameters",
        }:
            raise MultiAssetExperimentError("scenario.fields_invalid")
        scenarios.append(
            ScenarioDefinition(
                scenario_id=_string(payload, "scenario_id", "scenario"),
                logical_id=_string(payload, "logical_id", "scenario"),
                version=_string(payload, "version", "scenario"),
                runner_id=_string(payload, "runner_id", "scenario"),
                runner_version=_string(
                    payload,
                    "runner_version",
                    "scenario",
                ),
                description=_string(payload, "description", "scenario"),
                parameters=_parameters_from_dict(
                    payload["parameters"],
                    "scenario.parameters",
                ),
            )
        )
    metric_values = value["evaluation_metrics"]
    if not isinstance(metric_values, list):
        raise MultiAssetExperimentError("experiment.evaluation_metrics_array_required")
    metrics: list[EvaluationMetric] = []
    for raw_metric in metric_values:
        payload = _mapping(raw_metric, "metric")
        if set(payload) != {
            "logical_id",
            "version",
            "description",
            "unit",
            "higher_is_better",
        }:
            raise MultiAssetExperimentError("metric.fields_invalid")
        higher_is_better = payload["higher_is_better"]
        if not isinstance(higher_is_better, bool):
            raise MultiAssetExperimentError("metric.higher_is_better_bool_required")
        metrics.append(
            EvaluationMetric(
                logical_id=_string(payload, "logical_id", "metric"),
                version=_string(payload, "version", "metric"),
                description=_string(payload, "description", "metric"),
                unit=_string(payload, "unit", "metric"),
                higher_is_better=higher_is_better,
            )
        )
    seed = value["seed"]
    dirty_worktree = value["dirty_worktree"]
    schema_version = value["schema_version"]
    semantics_version = value["research_semantics_version"]
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise MultiAssetExperimentError("experiment.seed_invalid")
    if not isinstance(dirty_worktree, bool):
        raise MultiAssetExperimentError("experiment.dirty_worktree_bool_required")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or isinstance(semantics_version, bool)
        or not isinstance(semantics_version, int)
    ):
        raise MultiAssetExperimentError("experiment.version_fields_invalid")
    result = MultiAssetExperimentSpec(
        experiment_id=_string(value, "experiment_id", "experiment"),
        hypothesis=HypothesisDefinition(
            logical_id=_string(
                hypothesis_payload,
                "logical_id",
                "hypothesis",
            ),
            version=_string(hypothesis_payload, "version", "hypothesis"),
            statement=_string(
                hypothesis_payload,
                "statement",
                "hypothesis",
            ),
            rationale=_string(
                hypothesis_payload,
                "rationale",
                "hypothesis",
            ),
            expected_direction=_string(
                hypothesis_payload,
                "expected_direction",
                "hypothesis",
            ),
        ),
        data_range=DataRange(
            start_at=_string(data_range_payload, "start_at", "data_range"),
            end_at=_string(data_range_payload, "end_at", "data_range"),
        ),
        universe=UniverseDefinition(
            logical_id=_string(universe_payload, "logical_id", "universe"),
            version=_string(universe_payload, "version", "universe"),
            instrument_ids=tuple(instrument_ids),
            asset_classes=tuple(asset_classes),
            point_in_time_selection_rule=_string(
                universe_payload,
                "point_in_time_selection_rule",
                "universe",
            ),
        ),
        signal=SignalDefinition(
            logical_id=_string(signal_payload, "logical_id", "signal"),
            version=_string(signal_payload, "version", "signal"),
            expression=_string(signal_payload, "expression", "signal"),
            observation_lag=_string(
                signal_payload,
                "observation_lag",
                "signal",
            ),
            rebalance_frequency=_string(
                signal_payload,
                "rebalance_frequency",
                "signal",
            ),
        ),
        product_selection=_rule_from_dict(
            value["product_selection"],
            "product_selection",
        ),
        roll_policy=_rule_from_dict(value["roll_policy"], "roll_policy"),
        exercise_policy=_rule_from_dict(
            value["exercise_policy"],
            "exercise_policy",
        ),
        cost_policy=_rule_from_dict(value["cost_policy"], "cost_policy"),
        margin_policy=_rule_from_dict(
            value["margin_policy"],
            "margin_policy",
        ),
        scenarios=tuple(scenarios),
        evaluation_metrics=tuple(metrics),
        seed=seed,
        code_logical_id=_string(
            value,
            "code_logical_id",
            "experiment",
        ),
        code_version=_string(value, "code_version", "experiment"),
        data_version=_string(value, "data_version", "experiment"),
        dirty_worktree=dirty_worktree,
        frozen_at=_string(value, "frozen_at", "experiment"),
        research_semantics_version=semantics_version,
        schema_version=schema_version,
    )
    expected_hash = _string(value, "content_hash", "experiment")
    if result.content_hash != expected_hash:
        raise MultiAssetExperimentError("experiment.content_hash_mismatch")
    return result


@dataclass(frozen=True, slots=True)
class ScenarioRunContext:
    spec: MultiAssetExperimentSpec
    evidence_artifacts: tuple[ResolvedEvidenceArtifact, ...]
    repeat_index: int

    def __post_init__(self) -> None:
        if self.repeat_index not in {1, 2}:
            raise MultiAssetExperimentError("scenario_context.repeat_index_invalid")
        if not self.evidence_artifacts:
            raise MultiAssetExperimentError(
                "scenario_context.evidence_artifacts_required"
            )

    def artifacts_for(
        self,
        role: EvidenceArtifactRole,
    ) -> tuple[ResolvedEvidenceArtifact, ...]:
        return tuple(
            item for item in self.evidence_artifacts if item.reference.role is role
        )

    def one_artifact(
        self,
        role: EvidenceArtifactRole,
    ) -> ResolvedEvidenceArtifact:
        found = self.artifacts_for(role)
        if len(found) != 1:
            raise MultiAssetExperimentError(
                f"scenario_context.{role.value.lower()}_cardinality_invalid"
            )
        return found[0]


class SpotScenarioRunner(Protocol):
    runner_id: str
    runner_version: str

    def run_spot(self, context: ScenarioRunContext) -> SpotScenarioTrace: ...


class FuturesScenarioRunner(Protocol):
    runner_id: str
    runner_version: str

    def run_futures(
        self,
        context: ScenarioRunContext,
    ) -> FuturesScenarioTrace: ...


class OptionScenarioRunner(Protocol):
    runner_id: str
    runner_version: str

    def run_option(self, context: ScenarioRunContext) -> OptionScenarioTrace: ...


@dataclass(frozen=True, slots=True)
class IntegratedScenarioExecution:
    trace: IntegratedScenarioTrace
    accounting_reconciliation: ReportLedgerReconciliation

    def __post_init__(self) -> None:
        if not isinstance(self.trace, IntegratedScenarioTrace):
            raise MultiAssetExperimentError("integrated_execution.trace_invalid")
        if not isinstance(
            self.accounting_reconciliation,
            ReportLedgerReconciliation,
        ):
            raise MultiAssetExperimentError(
                "integrated_execution.accounting_reconciliation_invalid"
            )


class IntegratedScenarioRunner(Protocol):
    runner_id: str
    runner_version: str

    def run_integrated(
        self,
        context: ScenarioRunContext,
        *,
        spot: SpotScenarioTrace,
        futures: FuturesScenarioTrace,
        option: OptionScenarioTrace,
    ) -> IntegratedScenarioExecution: ...


@dataclass(frozen=True, slots=True)
class MultiAssetScenarioRunners:
    spot: SpotScenarioRunner
    futures: FuturesScenarioRunner
    option: OptionScenarioRunner
    integrated: IntegratedScenarioRunner

    def identities(self) -> tuple[tuple[str, str, str], ...]:
        rows: list[tuple[str, str, str]] = []
        for scenario_id, runner in (
            ("T-01", self.spot),
            ("T-02", self.futures),
            ("T-03", self.option),
            ("T-04", self.integrated),
        ):
            runner_id = getattr(runner, "runner_id", "")
            runner_version = getattr(runner, "runner_version", "")
            _require_id(runner_id, f"runner.{scenario_id}.runner_id")
            _require_id(
                runner_version,
                f"runner.{scenario_id}.runner_version",
            )
            rows.append((scenario_id, runner_id, runner_version))
        return tuple(rows)


@dataclass(frozen=True, slots=True)
class MultiAssetRunRequest:
    run_id: str
    spec: MultiAssetExperimentSpec
    evidence_references: tuple[EvidenceArtifactRef, ...]
    runners: MultiAssetScenarioRunners
    paths: ResearchPathManager
    command: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_id(self.run_id, "run_request.run_id")
        if not isinstance(self.spec, MultiAssetExperimentSpec):
            raise MultiAssetExperimentError("run_request.spec_invalid")
        if not self.evidence_references:
            raise MultiAssetExperimentError("run_request.evidence_references_required")
        if not isinstance(self.paths, ResearchPathManager):
            raise MultiAssetExperimentError("run_request.paths_invalid")
        if not self.command or any(
            not item or item.strip() != item for item in self.command
        ):
            raise MultiAssetExperimentError("run_request.command_invalid")
        actual_runners = self.runners.identities()
        expected_runners = tuple(
            (
                scenario.scenario_id,
                scenario.runner_id,
                scenario.runner_version,
            )
            for scenario in self.spec.scenarios[:4]
        )
        if actual_runners != expected_runners:
            raise MultiAssetExperimentError(
                "run_request.scenario_runner_binding_mismatch"
            )


@dataclass(frozen=True, slots=True)
class EconomicScenarioExecution:
    spot: SpotScenarioTrace
    futures: FuturesScenarioTrace
    option: OptionScenarioTrace
    integrated: IntegratedScenarioTrace
    accounting_reconciliation: ReportLedgerReconciliation


@dataclass(frozen=True, slots=True)
class DeterministicStudyCoreExecution:
    """The repository-independent economic core shared by execute and replay."""

    study: ValidatedMultiAssetStudy
    first_execution: EconomicScenarioExecution
    repeated_execution: EconomicScenarioExecution


@dataclass(frozen=True, slots=True)
class MultiAssetResearchExecution:
    study: ValidatedMultiAssetStudy
    published_study: PublishedMultiAssetStudy
    run_manifest: MultiAssetRunManifest
    published_manifest: PublishedRunManifest
    first_execution: EconomicScenarioExecution
    repeated_execution: EconomicScenarioExecution


@dataclass(frozen=True, slots=True)
class MultiAssetReproductionExecution:
    expected_run_manifest_hash: str
    reproduced_run_manifest_hash: str
    experiment_spec_hash: str
    receipt: ReproductionReceipt
    reproduced_execution: MultiAssetResearchExecution
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "content_hash",
            evidence_hash(
                {
                    "expected_run_manifest_hash": (self.expected_run_manifest_hash),
                    "reproduced_run_manifest_hash": (self.reproduced_run_manifest_hash),
                    "experiment_spec_hash": self.experiment_spec_hash,
                    "receipt_hash": self.receipt.content_hash,
                },
                label="multi-asset-application-reproduction",
            ),
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _working_tree_hash(
    root: Path,
    *,
    commit: str,
) -> str:
    """Hash tracked changes and every non-ignored untracked file."""

    try:
        tracked_diff = subprocess.run(
            ["git", "diff", "--binary", "--no-ext-diff", "HEAD", "--"],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=15,
        ).stdout
        untracked_output = subprocess.run(
            [
                "git",
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise MultiAssetExperimentError(
            "runtime.working_tree_hash_unavailable"
        ) from exc
    digest = hashlib.sha256()
    digest.update(b"multi-asset-working-tree-v1\0")
    digest.update(commit.encode("utf-8"))
    digest.update(b"\0tracked-diff\0")
    digest.update(tracked_diff)
    for raw_relative in sorted(item for item in untracked_output.split(b"\0") if item):
        relative = Path(os.fsdecode(raw_relative))
        if relative.is_absolute() or ".." in relative.parts:
            raise MultiAssetExperimentError("runtime.untracked_path_invalid")
        candidate = root / relative
        digest.update(b"\0untracked-path\0")
        digest.update(raw_relative)
        if candidate.is_symlink():
            digest.update(b"\0symlink\0")
            digest.update(os.readlink(candidate).encode("utf-8"))
            continue
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise MultiAssetExperimentError(
                "runtime.untracked_path_outside_project"
            ) from exc
        if not resolved.is_file():
            raise MultiAssetExperimentError("runtime.untracked_regular_file_required")
        digest.update(b"\0file\0")
        digest.update(resolved.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def capture_runtime_environment(project_root: Path) -> RuntimeEnvironment:
    """Capture commit, dirty state, interpreter, platform, and dependencies."""

    root = project_root.expanduser().resolve()
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        dirty_output = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise MultiAssetExperimentError("runtime.git_metadata_unavailable") from exc
    dependencies: set[str] = set()
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if name and distribution.version:
            dependencies.add(f"{name}=={distribution.version}")
    return RuntimeEnvironment.basic(
        git_commit=commit,
        dirty_worktree=bool(dirty_output),
        working_tree_hash=_working_tree_hash(root, commit=commit),
        dependency_versions=tuple(sorted(dependencies)),
    )


def _role_items(
    artifacts: Sequence[ResolvedEvidenceArtifact],
    role: EvidenceArtifactRole,
) -> tuple[ResolvedEvidenceArtifact, ...]:
    return tuple(item for item in artifacts if item.reference.role is role)


def _one_role(
    artifacts: Sequence[ResolvedEvidenceArtifact],
    role: EvidenceArtifactRole,
) -> ResolvedEvidenceArtifact:
    found = _role_items(artifacts, role)
    if len(found) != 1:
        raise MultiAssetExperimentError(
            f"evidence_authority.{role.value.lower()}_cardinality_invalid"
        )
    return found[0]


def _validate_evidence_authority(
    *,
    spec: MultiAssetExperimentSpec,
    artifacts: tuple[ResolvedEvidenceArtifact, ...],
    runtime: RuntimeEnvironment,
) -> None:
    datasets = _role_items(artifacts, EvidenceArtifactRole.DATASET)
    market_states = _role_items(artifacts, EvidenceArtifactRole.MARKET_STATE)
    policies = _role_items(artifacts, EvidenceArtifactRole.POLICY)
    if not datasets:
        raise MultiAssetExperimentError("evidence_authority.dataset_required")
    if not market_states:
        raise MultiAssetExperimentError("evidence_authority.market_state_required")
    for role in (
        EvidenceArtifactRole.RESEARCH_INPUTS,
        EvidenceArtifactRole.PRODUCT_REGISTRY,
        EvidenceArtifactRole.HYPOTHESIS,
        EvidenceArtifactRole.CODE,
        EvidenceArtifactRole.ENVIRONMENT,
        EvidenceArtifactRole.CONFIGURATION,
    ):
        _one_role(artifacts, role)
    if any(item.reference.version != spec.data_version for item in datasets):
        raise MultiAssetExperimentError("evidence_authority.data_version_mismatch")
    if any(item.payload.get("data_version") != spec.data_version for item in datasets):
        raise MultiAssetExperimentError(
            "evidence_authority.dataset_payload_version_mismatch"
        )
    hypothesis = _one_role(artifacts, EvidenceArtifactRole.HYPOTHESIS)
    if (
        hypothesis.reference.logical_id != spec.hypothesis.logical_id
        or hypothesis.reference.version != spec.hypothesis.version
        or hypothesis.payload != spec.hypothesis.as_dict()
    ):
        raise MultiAssetExperimentError("evidence_authority.hypothesis_mismatch")
    expected_policies = {
        (item.logical_id, item.version): item.as_dict() for item in spec.policy_rules
    }
    actual_policies = {
        (item.reference.logical_id, item.reference.version): item.payload
        for item in policies
    }
    if actual_policies != expected_policies:
        raise MultiAssetExperimentError("evidence_authority.policy_mismatch")
    code = _one_role(artifacts, EvidenceArtifactRole.CODE)
    if (
        code.reference.logical_id != spec.code_logical_id
        or code.reference.version != spec.code_version
        or code.payload
        != {
            "git_commit": runtime.git_commit,
            "dirty_worktree": runtime.dirty_worktree,
            "working_tree_hash": runtime.working_tree_hash,
        }
    ):
        raise MultiAssetExperimentError("evidence_authority.code_mismatch")
    if (
        runtime.git_commit != spec.code_version
        or runtime.dirty_worktree != spec.dirty_worktree
    ):
        raise MultiAssetExperimentError(
            "evidence_authority.runtime_code_state_mismatch"
        )
    environment = _one_role(
        artifacts,
        EvidenceArtifactRole.ENVIRONMENT,
    )
    if environment.payload != runtime.as_dict():
        raise MultiAssetExperimentError("evidence_authority.environment_mismatch")
    configuration = _one_role(
        artifacts,
        EvidenceArtifactRole.CONFIGURATION,
    )
    if configuration.payload != spec.as_dict():
        raise MultiAssetExperimentError("evidence_authority.configuration_mismatch")


def _bindings(
    artifacts: tuple[ResolvedEvidenceArtifact, ...],
    spec: MultiAssetExperimentSpec,
) -> ResearchEvidenceBindings:
    return ResearchEvidenceBindings(
        dataset_snapshot_hashes=tuple(
            sorted(
                item.reference.content_hash
                for item in _role_items(
                    artifacts,
                    EvidenceArtifactRole.DATASET,
                )
            )
        ),
        product_registry_hash=_one_role(
            artifacts,
            EvidenceArtifactRole.PRODUCT_REGISTRY,
        ).reference.content_hash,
        market_state_hashes=tuple(
            sorted(
                item.reference.content_hash
                for item in _role_items(
                    artifacts,
                    EvidenceArtifactRole.MARKET_STATE,
                )
            )
        ),
        hypothesis_hash=_one_role(
            artifacts,
            EvidenceArtifactRole.HYPOTHESIS,
        ).reference.content_hash,
        policy_hashes=tuple(
            sorted(
                item.reference.content_hash
                for item in _role_items(
                    artifacts,
                    EvidenceArtifactRole.POLICY,
                )
            )
        ),
        code_hash=_one_role(
            artifacts,
            EvidenceArtifactRole.CODE,
        ).reference.content_hash,
        environment_hash=_one_role(
            artifacts,
            EvidenceArtifactRole.ENVIRONMENT,
        ).reference.content_hash,
        configuration_hash=_one_role(
            artifacts,
            EvidenceArtifactRole.CONFIGURATION,
        ).reference.content_hash,
        seed=spec.seed,
        research_inputs_hash=_one_role(
            artifacts,
            EvidenceArtifactRole.RESEARCH_INPUTS,
        ).reference.content_hash,
    )


def _input_quality_flags(
    artifacts: Sequence[ResolvedEvidenceArtifact],
) -> tuple[str, ...]:
    flags = {
        f"INPUT:{item.reference.role.value}:{flag}"
        for item in artifacts
        for flag in item.quality_flags
    }
    return tuple(sorted(flags))


def _enrich_trace_quality(
    trace: _TraceT,
    *,
    scenario_id: str,
    input_flags: tuple[str, ...],
) -> _TraceT:
    trace_flags = tuple(trace.quality_flags)
    if (
        not trace_flags
        or trace_flags != tuple(sorted(set(trace_flags)))
        or any(not _QUALITY_FLAG.fullmatch(item) for item in trace_flags)
    ):
        raise MultiAssetExperimentError(
            f"quality_flags.{scenario_id}_trace_flags_required"
        )
    return replace(
        trace,
        quality_flags=tuple(sorted(set(trace_flags) | set(input_flags))),
    )


def _aggregate_objects(
    execution: EconomicScenarioExecution,
) -> ScenarioObjectHashes:
    objects = (
        execution.spot.object_hashes,
        execution.futures.object_hashes,
        execution.option.object_hashes,
        execution.integrated.object_hashes,
    )
    return scenario_object_hashes(
        trades=tuple(item.trades_hash for item in objects),
        positions=tuple(item.positions_hash for item in objects),
        ledger_events=tuple(item.ledger_events_hash for item in objects),
        nav=tuple(item.nav_hash for item in objects),
        exposure=tuple(item.exposure_hash for item in objects),
        attribution=tuple(item.attribution_hash for item in objects),
        scenario_output={
            "scenario_output_hashes": [item.scenario_output_hash for item in objects],
            "accounting_reconciliation_hash": (
                execution.accounting_reconciliation.content_hash
            ),
        },
    )


def _core_execution_hash(execution: EconomicScenarioExecution) -> str:
    return evidence_hash(
        {
            "spot": execution.spot,
            "futures": execution.futures,
            "option": execution.option,
            "integrated": execution.integrated,
            "accounting_reconciliation_hash": (
                execution.accounting_reconciliation.content_hash
            ),
        },
        label="multi-asset-economic-execution",
    )


def execute_deterministic_study_core(
    *,
    spec: MultiAssetExperimentSpec,
    first_artifacts: tuple[ResolvedEvidenceArtifact, ...],
    repeated_artifacts: tuple[ResolvedEvidenceArtifact, ...],
    runners: MultiAssetScenarioRunners,
    runtime: RuntimeEnvironment,
) -> DeterministicStudyCoreExecution:
    """Execute and reconcile the economic study without publishing or Git access.

    Both the public application service and the cold-package replay entry point
    call this function.  The caller must independently resolve the immutable
    artifacts twice; this function validates both resolutions against the same
    captured runtime authority before running the source-owned scenario graph.
    """

    first_identities = tuple(
        (
            item.reference.role.value,
            item.reference.logical_id,
            item.reference.version,
            item.reference.content_hash,
        )
        for item in first_artifacts
    )
    repeated_identities = tuple(
        (
            item.reference.role.value,
            item.reference.logical_id,
            item.reference.version,
            item.reference.content_hash,
        )
        for item in repeated_artifacts
    )
    if first_identities != repeated_identities:
        raise MultiAssetExperimentError("evidence_authority.repeat_resolution_mismatch")
    _validate_evidence_authority(
        spec=spec,
        artifacts=first_artifacts,
        runtime=runtime,
    )
    _validate_evidence_authority(
        spec=spec,
        artifacts=repeated_artifacts,
        runtime=runtime,
    )
    first = MultiAssetResearchApplicationService._execute_once(
        spec=spec,
        artifacts=first_artifacts,
        runners=runners,
        repeat_index=1,
    )
    repeated = MultiAssetResearchApplicationService._execute_once(
        spec=spec,
        artifacts=repeated_artifacts,
        runners=runners,
        repeat_index=2,
    )
    first_objects = _aggregate_objects(first)
    repeated_objects = _aggregate_objects(repeated)
    reproduction = ReproducibilityScenarioTrace(
        first=first_objects,
        second=repeated_objects,
        first_core_artifact_hash=_core_execution_hash(first),
        second_core_artifact_hash=_core_execution_hash(repeated),
        object_hashes=reproduction_object_hashes(
            first_objects,
            repeated_objects,
        ),
    )
    study = build_validated_multi_asset_study(
        experiment_id=spec.experiment_id,
        bindings=_bindings(first_artifacts, spec),
        spot=first.spot,
        futures=first.futures,
        option=first.option,
        integrated=first.integrated,
        reproduction=reproduction,
        accounting_reconciliation=first.accounting_reconciliation,
    )
    t05_flags = tuple(
        sorted(
            {
                *_input_quality_flags(first_artifacts),
                "DETERMINISTIC_REPEAT_VERIFIED",
            }
        )
    )
    study = replace(
        study,
        scenarios=(
            *study.scenarios[:4],
            replace(study.scenarios[4], quality_flags=t05_flags),
        ),
    )
    return DeterministicStudyCoreExecution(
        study=study,
        first_execution=first,
        repeated_execution=repeated,
    )


def _failure_code(error: Exception) -> str:
    message = str(error)
    if isinstance(
        error,
        (MultiAssetExperimentError, MultiAssetResearchPackageError),
    ) and _STABLE_ID.fullmatch(message):
        return message
    return "multi_asset_scenario_execution_failed"


class MultiAssetResearchApplicationService:
    """The sole production coordinator for complete T-01 through T-05 runs."""

    def execute(
        self,
        request: MultiAssetRunRequest,
    ) -> MultiAssetResearchExecution:
        runtime = capture_runtime_environment(request.paths.project_root)
        started_at = _utc_now()
        ordered_references = tuple(
            sorted(
                request.evidence_references,
                key=lambda item: (
                    item.role.value,
                    item.logical_id,
                    item.version,
                ),
            )
        )
        resolved: tuple[ResolvedEvidenceArtifact, ...] = ()
        try:
            claim = reserve_run_id(
                run_id=request.run_id,
                experiment_id=request.spec.experiment_id,
                experiment_spec_hash=request.spec.content_hash,
                command=request.command,
                evidence_references=ordered_references,
                paths=request.paths,
            )
            if not claim.created:
                raise MultiAssetExperimentError("run_id_already_reserved")
            if _timestamp(
                request.spec.frozen_at,
                "experiment.frozen_at",
            ) > _timestamp(started_at, "run.started_at"):
                raise MultiAssetExperimentError("experiment.frozen_after_run_start")
            resolver = BoundedEvidenceArtifactResolver.from_paths(request.paths)
            resolved = resolver.resolve_all(
                ordered_references,
                verified_at=started_at,
            )
            repeated_resolved = resolver.resolve_all(
                ordered_references,
                verified_at=started_at,
            )
            core_execution = execute_deterministic_study_core(
                spec=request.spec,
                first_artifacts=resolved,
                repeated_artifacts=repeated_resolved,
                runners=request.runners,
                runtime=runtime,
            )
            study = core_execution.study
            first = core_execution.first_execution
            repeated = core_execution.repeated_execution
            finishing_runtime = capture_runtime_environment(request.paths.project_root)
            if finishing_runtime != runtime:
                raise MultiAssetExperimentError(
                    "runtime_environment_changed_during_run"
                )
            published_study = publish_validated_study(
                study,
                paths=request.paths,
            )
            checksums = tuple(
                sorted(
                    (
                        ArtifactChecksum.from_path(
                            "multi_asset_study",
                            published_study.artifact_path,
                        ),
                        ArtifactChecksum.from_path(
                            "validated_study_report",
                            published_study.report_path,
                        ),
                    ),
                    key=lambda item: item.logical_id,
                )
            )
            finished_at = _utc_now()
            manifest = MultiAssetRunManifest(
                run_id=request.run_id,
                experiment_id=request.spec.experiment_id,
                experiment_spec_hash=request.spec.content_hash,
                started_at=started_at,
                finished_at=finished_at,
                status=RunStatus.SUCCEEDED,
                runtime=runtime,
                command=request.command,
                evidence_references=ordered_references,
                evidence_artifacts=resolved,
                artifact_checksums=checksums,
                study_content_hash=study.content_hash,
            )
            published_manifest = publish_run_manifest(
                manifest,
                paths=request.paths,
            )
            return MultiAssetResearchExecution(
                study=study,
                published_study=published_study,
                run_manifest=manifest,
                published_manifest=published_manifest,
                first_execution=first,
                repeated_execution=repeated,
            )
        except MultiAssetApplicationError:
            raise
        except Exception as exc:
            finished_at = _utc_now()
            failure = MultiAssetRunManifest(
                run_id=request.run_id,
                experiment_id=request.spec.experiment_id,
                experiment_spec_hash=request.spec.content_hash,
                started_at=started_at,
                finished_at=finished_at,
                status=RunStatus.FAILED,
                runtime=runtime,
                command=request.command,
                evidence_references=ordered_references,
                evidence_artifacts=resolved,
                artifact_checksums=(),
                study_content_hash=None,
                failure_code=_failure_code(exc),
                failure_message_hash=evidence_hash(
                    {
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                    label="multi-asset-run-failure-message",
                ),
            )
            published_failure = publish_failure_run_manifest(
                failure,
                paths=request.paths,
            )
            raise MultiAssetApplicationError(
                "multi_asset_research_run_failed",
                run_manifest=failure,
                published_manifest=published_failure,
            ) from exc

    def reproduce(
        self,
        request: MultiAssetRunRequest,
        expected: MultiAssetResearchExecution,
        *,
        reproduction_run_id: str,
    ) -> MultiAssetReproductionExecution:
        """Rerun the same typed spec and compare every mandatory scenario."""

        if request.spec.content_hash != (expected.run_manifest.experiment_spec_hash):
            raise MultiAssetExperimentError("reproduction.experiment_spec_mismatch")
        reproduced_request = replace(
            request,
            run_id=reproduction_run_id,
        )
        reproduced = self.execute(reproduced_request)
        receipt = compare_studies(expected.study, reproduced.study)
        return MultiAssetReproductionExecution(
            expected_run_manifest_hash=(expected.run_manifest.content_hash),
            reproduced_run_manifest_hash=(reproduced.run_manifest.content_hash),
            experiment_spec_hash=request.spec.content_hash,
            receipt=receipt,
            reproduced_execution=reproduced,
        )

    @staticmethod
    def compare(
        first: MultiAssetResearchExecution,
        second: MultiAssetResearchExecution,
    ) -> ReproductionReceipt:
        return compare_studies(first.study, second.study)

    @staticmethod
    def _execute_once(
        *,
        spec: MultiAssetExperimentSpec,
        artifacts: tuple[ResolvedEvidenceArtifact, ...],
        runners: MultiAssetScenarioRunners,
        repeat_index: int,
    ) -> EconomicScenarioExecution:
        context = ScenarioRunContext(
            spec=spec,
            evidence_artifacts=artifacts,
            repeat_index=repeat_index,
        )
        input_flags = _input_quality_flags(artifacts)
        spot_raw = runners.spot.run_spot(context)
        if not isinstance(spot_raw, SpotScenarioTrace):
            raise MultiAssetExperimentError("runner.T-01_trace_type_invalid")
        spot = _enrich_trace_quality(
            spot_raw,
            scenario_id="T-01",
            input_flags=input_flags,
        )
        futures_raw = runners.futures.run_futures(context)
        if not isinstance(futures_raw, FuturesScenarioTrace):
            raise MultiAssetExperimentError("runner.T-02_trace_type_invalid")
        futures = _enrich_trace_quality(
            futures_raw,
            scenario_id="T-02",
            input_flags=input_flags,
        )
        option_raw = runners.option.run_option(context)
        if not isinstance(option_raw, OptionScenarioTrace):
            raise MultiAssetExperimentError("runner.T-03_trace_type_invalid")
        option = _enrich_trace_quality(
            option_raw,
            scenario_id="T-03",
            input_flags=input_flags,
        )
        integrated_result = runners.integrated.run_integrated(
            context,
            spot=spot,
            futures=futures,
            option=option,
        )
        if not isinstance(
            integrated_result,
            IntegratedScenarioExecution,
        ):
            raise MultiAssetExperimentError("runner.T-04_execution_type_invalid")
        integrated = _enrich_trace_quality(
            integrated_result.trace,
            scenario_id="T-04",
            input_flags=input_flags,
        )
        return EconomicScenarioExecution(
            spot=spot,
            futures=futures,
            option=option,
            integrated=integrated,
            accounting_reconciliation=(integrated_result.accounting_reconciliation),
        )


__all__ = [
    "DataRange",
    "DeterministicStudyCoreExecution",
    "EconomicScenarioExecution",
    "EvaluationMetric",
    "FuturesScenarioRunner",
    "HypothesisDefinition",
    "IntegratedScenarioExecution",
    "IntegratedScenarioRunner",
    "MULTI_ASSET_EXPERIMENT_SCHEMA_VERSION",
    "MultiAssetApplicationError",
    "MultiAssetExperimentError",
    "MultiAssetExperimentSpec",
    "MultiAssetResearchApplicationService",
    "MultiAssetResearchExecution",
    "MultiAssetReproductionExecution",
    "MultiAssetRunRequest",
    "MultiAssetScenarioRunners",
    "OptionScenarioRunner",
    "ScenarioDefinition",
    "ScenarioRunContext",
    "SignalDefinition",
    "SpotScenarioRunner",
    "UniverseDefinition",
    "VersionedRule",
    "capture_runtime_environment",
    "execute_deterministic_study_core",
    "multi_asset_experiment_spec_from_dict",
]
