"""Exploratory signal screening with no execution or portfolio authority.

Vector screening is bound to an authoritative Research Semantics v2 manifest,
its exact non-holdout split, the shared strategy compiler, and the immutable
dataset fingerprint.  It projects decisions and causally available forward
labels only.  Orders, fills, cash, positions, and validation conclusions remain
owned by the event simulation and validation authorities.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from math import isfinite
from typing import Any, Mapping

from .backtest_types import BacktestRun, BacktestRunContext
from .dataset_snapshot import DatasetSnapshot
from .decision_event import ResearchDecisionEvent
from .experiment_manifest import (
    DateRange,
    ExecutionTimingPolicy,
    ExperimentManifest,
)
from .hashing import sha256_prefixed
from .immutable_contract import canonical_mutable, deep_freeze
from .point_in_time_selection import point_in_time_execution_snapshot
from .research_classification import normalize_research_classification
from .strategy_compiler import StrategyCompiler, validate_compiled_strategy_contract
from .strategy_contract import CompiledStrategyContract, ResearchStrategyPlugin
from .strategy_registry import StrategyRegistry


VECTOR_SCREENING_SCHEMA_VERSION = 2
VECTOR_SCREENING_PURPOSE = "exploratory_signal_screening_only"
_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class VectorScreeningError(ValueError):
    """The requested screen would cross the exploratory-only boundary."""


@dataclass(frozen=True, slots=True)
class VectorSignalRow:
    decision_id: str
    candle_ts: int
    decision_ts: int
    strategy_name: str
    strategy_version: str
    raw_signal: str
    final_signal: str
    reason: str
    feature_snapshot: Mapping[str, object]
    feature_snapshot_hash: str
    blocked_filters: tuple[str, ...]

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (self.candle_ts, self.decision_ts)
        ):
            raise VectorScreeningError("vector_signal_timestamp_invalid")
        if self.decision_ts < self.candle_ts:
            raise VectorScreeningError("vector_signal_timeline_invalid")
        for label, value in (
            ("strategy_name", self.strategy_name),
            ("strategy_version", self.strategy_version),
            ("raw_signal", self.raw_signal),
            ("final_signal", self.final_signal),
            ("reason", self.reason),
        ):
            _require_text(value, f"vector_signal_{label}_invalid")
        if not isinstance(self.feature_snapshot, Mapping):
            raise VectorScreeningError("vector_signal_feature_snapshot_invalid")
        object.__setattr__(
            self, "feature_snapshot", deep_freeze(dict(self.feature_snapshot))
        )
        if not isinstance(self.blocked_filters, tuple) or any(
            not isinstance(value, str) or not value for value in self.blocked_filters
        ):
            raise VectorScreeningError("vector_signal_blocked_filters_invalid")
        calculated_feature_hash = sha256_prefixed(self.feature_snapshot)
        if self.feature_snapshot_hash != calculated_feature_hash:
            raise VectorScreeningError("vector_signal_feature_hash_mismatch")
        _require_hash(self.decision_id, "vector_signal_decision_id_invalid")
        calculated_decision_id = sha256_prefixed(
            {
                "strategy_name": self.strategy_name,
                "strategy_version": self.strategy_version,
                "candle_ts": self.candle_ts,
                "decision_ts": self.decision_ts,
                "raw_signal": self.raw_signal,
                "final_signal": self.final_signal,
                "reason": self.reason,
                "feature_snapshot": self.feature_snapshot,
            }
        )
        if self.decision_id != calculated_decision_id:
            raise VectorScreeningError("vector_signal_decision_id_content_mismatch")

    def as_dict(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "candle_ts": self.candle_ts,
            "decision_ts": self.decision_ts,
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "raw_signal": self.raw_signal,
            "final_signal": self.final_signal,
            "reason": self.reason,
            "feature_snapshot": canonical_mutable(self.feature_snapshot),
            "feature_snapshot_hash": self.feature_snapshot_hash,
            "blocked_filters": list(self.blocked_filters),
        }


@dataclass(frozen=True, slots=True)
class ForwardLabelRow:
    """A label kept separate from the Feature/decision calculation path."""

    decision_id: str
    horizon_bars: int
    label_available_at_ts: int
    forward_close_return: float

    def __post_init__(self) -> None:
        _require_hash(self.decision_id, "vector_label_decision_id_invalid")
        if (
            isinstance(self.horizon_bars, bool)
            or not isinstance(self.horizon_bars, int)
            or self.horizon_bars <= 0
        ):
            raise VectorScreeningError("vector_label_horizon_invalid")
        if (
            isinstance(self.label_available_at_ts, bool)
            or not isinstance(self.label_available_at_ts, int)
            or self.label_available_at_ts < 0
        ):
            raise VectorScreeningError("vector_label_available_at_invalid")
        if isinstance(self.forward_close_return, bool) or not isfinite(
            float(self.forward_close_return)
        ):
            raise VectorScreeningError("vector_forward_label_non_finite")

    def as_dict(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "horizon_bars": self.horizon_bars,
            "label_available_at_ts": self.label_available_at_ts,
            "forward_close_return": self.forward_close_return,
        }


@dataclass(frozen=True, slots=True)
class VectorScreeningResult:
    schema_version: int
    purpose: str
    manifest_hash: str
    experiment_id: str
    strategy_name: str
    strategy_version: str
    strategy_plugin_contract_hash: str
    strategy_rule_spec_hash: str
    compiled_strategy_contract: CompiledStrategyContract
    compiled_strategy_contract_hash: str
    strategy_registry_hash: str
    raw_parameter_values_hash: str
    parameter_values_hash: str
    parameter_source_map_hash: str
    dataset_snapshot_id: str
    dataset_source: str
    dataset_market: str
    dataset_interval: str
    dataset_split_name: str
    dataset_period_start: str
    dataset_period_end: str
    dataset_artifact_manifest_hash: str | None
    dataset_snapshot_hash: str
    dataset_data_hash: str
    dataset_query_hash: str
    split_binding_hash: str
    execution_timing_hash: str
    forward_horizon_bars: int
    signals: tuple[VectorSignalRow, ...]
    labels: tuple[ForwardLabelRow, ...]
    decision_stream_hash: str
    label_stream_hash: str
    content_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != VECTOR_SCREENING_SCHEMA_VERSION:
            raise VectorScreeningError("vector_screening_schema_unsupported")
        if self.purpose != VECTOR_SCREENING_PURPOSE:
            raise VectorScreeningError("vector_screening_purpose_invalid")
        for label, value in (
            ("manifest_hash", self.manifest_hash),
            ("strategy_plugin_contract_hash", self.strategy_plugin_contract_hash),
            ("strategy_rule_spec_hash", self.strategy_rule_spec_hash),
            ("compiled_strategy_contract_hash", self.compiled_strategy_contract_hash),
            ("strategy_registry_hash", self.strategy_registry_hash),
            ("raw_parameter_values_hash", self.raw_parameter_values_hash),
            ("parameter_values_hash", self.parameter_values_hash),
            ("parameter_source_map_hash", self.parameter_source_map_hash),
            ("dataset_snapshot_hash", self.dataset_snapshot_hash),
            ("dataset_data_hash", self.dataset_data_hash),
            ("dataset_query_hash", self.dataset_query_hash),
            ("split_binding_hash", self.split_binding_hash),
            ("execution_timing_hash", self.execution_timing_hash),
            ("decision_stream_hash", self.decision_stream_hash),
            ("label_stream_hash", self.label_stream_hash),
            ("content_hash", self.content_hash),
        ):
            _require_hash(value, f"vector_screening_{label}_invalid")
        if self.dataset_artifact_manifest_hash is not None:
            _require_hash(
                self.dataset_artifact_manifest_hash,
                "vector_screening_dataset_artifact_manifest_hash_invalid",
            )
        for label, value in (
            ("experiment_id", self.experiment_id),
            ("strategy_name", self.strategy_name),
            ("strategy_version", self.strategy_version),
            ("dataset_snapshot_id", self.dataset_snapshot_id),
            ("dataset_source", self.dataset_source),
            ("dataset_market", self.dataset_market),
            ("dataset_interval", self.dataset_interval),
            ("dataset_split_name", self.dataset_split_name),
            ("dataset_period_start", self.dataset_period_start),
            ("dataset_period_end", self.dataset_period_end),
        ):
            _require_text(value, f"vector_screening_{label}_invalid")
        if self.dataset_split_name == "final_holdout":
            raise VectorScreeningError("vector_screening_final_holdout_forbidden")
        if (
            isinstance(self.forward_horizon_bars, bool)
            or not isinstance(self.forward_horizon_bars, int)
            or self.forward_horizon_bars <= 0
        ):
            raise VectorScreeningError("vector_screening_horizon_must_be_positive_int")
        try:
            compiled = validate_compiled_strategy_contract(
                self.compiled_strategy_contract,
                expected_strategy_name=self.strategy_name,
                expected_strategy_version=self.strategy_version,
                expected_plugin_hash=self.strategy_plugin_contract_hash,
                expected_compiled_hash=self.compiled_strategy_contract_hash,
            )
        except ValueError as exc:
            raise VectorScreeningError(
                "vector_screening_compiled_contract_invalid"
            ) from exc
        if (
            compiled.strategy_registry_hash != self.strategy_registry_hash
            or compiled.materialized_parameters_hash != self.parameter_values_hash
            or sha256_prefixed(compiled.raw_parameters)
            != self.raw_parameter_values_hash
            or sha256_prefixed(compiled.parameter_source_map)
            != self.parameter_source_map_hash
        ):
            raise VectorScreeningError(
                "vector_screening_compiled_contract_binding_mismatch"
            )
        expected_split_hash = sha256_prefixed(
            self.split_binding_payload(), label="vector_screening_split_binding"
        )
        if self.split_binding_hash != expected_split_hash:
            raise VectorScreeningError("vector_screening_split_binding_hash_mismatch")
        signal_ids = tuple(item.decision_id for item in self.signals)
        if len(signal_ids) != len(set(signal_ids)):
            raise VectorScreeningError("vector_signal_decision_id_duplicate")
        signal_keys = tuple(
            (item.candle_ts, item.decision_ts, item.decision_id)
            for item in self.signals
        )
        if any(left >= right for left, right in zip(signal_keys, signal_keys[1:])):
            raise VectorScreeningError("vector_signal_stream_not_strictly_ordered")
        if any(
            item.strategy_name != self.strategy_name
            or item.strategy_version != self.strategy_version
            for item in self.signals
        ):
            raise VectorScreeningError("vector_signal_strategy_binding_mismatch")
        decision_times = {item.decision_id: item.decision_ts for item in self.signals}
        label_ids = tuple(item.decision_id for item in self.labels)
        if len(label_ids) != len(set(label_ids)):
            raise VectorScreeningError("vector_label_decision_id_duplicate")
        signal_order = {
            decision_id: index for index, decision_id in enumerate(signal_ids)
        }
        try:
            label_order = tuple(signal_order[decision_id] for decision_id in label_ids)
        except KeyError as exc:
            raise VectorScreeningError("vector_label_orphan_decision") from exc
        if any(left >= right for left, right in zip(label_order, label_order[1:])):
            raise VectorScreeningError("vector_label_stream_not_strictly_ordered")
        if any(
            item.horizon_bars != self.forward_horizon_bars
            or item.label_available_at_ts <= decision_times[item.decision_id]
            for item in self.labels
        ):
            raise VectorScreeningError("vector_label_contract_mismatch")
        if self.decision_stream_hash != _decision_stream_hash(self.signals):
            raise VectorScreeningError("vector_decision_stream_hash_mismatch")
        if self.label_stream_hash != _label_stream_hash(self.labels):
            raise VectorScreeningError("vector_label_stream_hash_mismatch")
        if self.content_hash != sha256_prefixed(
            self.identity_payload(), label="vector_screening_result"
        ):
            raise VectorScreeningError("vector_screening_content_hash_mismatch")

    def split_binding_payload(self) -> dict[str, object]:
        return {
            "manifest_hash": self.manifest_hash,
            "experiment_id": self.experiment_id,
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "dataset_source": self.dataset_source,
            "dataset_market": self.dataset_market,
            "dataset_interval": self.dataset_interval,
            "dataset_split_name": self.dataset_split_name,
            "dataset_period_start": self.dataset_period_start,
            "dataset_period_end": self.dataset_period_end,
            "dataset_artifact_manifest_hash": self.dataset_artifact_manifest_hash,
            "dataset_snapshot_hash": self.dataset_snapshot_hash,
            "dataset_data_hash": self.dataset_data_hash,
            "dataset_query_hash": self.dataset_query_hash,
        }

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "purpose": self.purpose,
            "manifest_hash": self.manifest_hash,
            "experiment_id": self.experiment_id,
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "strategy_plugin_contract_hash": self.strategy_plugin_contract_hash,
            "strategy_rule_spec_hash": self.strategy_rule_spec_hash,
            "compiled_strategy_contract": self.compiled_strategy_contract.as_dict(),
            "compiled_strategy_contract_hash": self.compiled_strategy_contract_hash,
            "strategy_registry_hash": self.strategy_registry_hash,
            "raw_parameter_values_hash": self.raw_parameter_values_hash,
            "parameter_values_hash": self.parameter_values_hash,
            "parameter_source_map_hash": self.parameter_source_map_hash,
            **self.split_binding_payload(),
            "split_binding_hash": self.split_binding_hash,
            "execution_timing_hash": self.execution_timing_hash,
            "forward_horizon_bars": self.forward_horizon_bars,
            "signals": [item.as_dict() for item in self.signals],
            "labels": [item.as_dict() for item in self.labels],
            "decision_stream_hash": self.decision_stream_hash,
            "label_stream_hash": self.label_stream_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def run_vector_signal_screen(
    *,
    manifest: ExperimentManifest,
    registry: StrategyRegistry,
    plugin: ResearchStrategyPlugin,
    dataset: DatasetSnapshot,
    parameter_values: dict[str, Any],
    execution_timing_policy: ExecutionTimingPolicy | None = None,
    forward_horizon_bars: int = 1,
) -> VectorScreeningResult:
    """Produce manifest-bound exploratory decisions and forward labels."""

    if not isinstance(manifest, ExperimentManifest) or not isinstance(
        registry, StrategyRegistry
    ):
        raise VectorScreeningError("vector_screening_authoritative_manifest_required")
    classification = normalize_research_classification(manifest.research_classification)
    if classification != "exploratory":
        raise VectorScreeningError(
            "vector_screening_requires_exploratory_classification"
        )
    if (
        registry.resolve(plugin.name).contract_hash() != plugin.contract_hash()
        or manifest.validated_strategy_registry_hash
        != registry.execution_scope_hash(plugin.name)
    ):
        raise VectorScreeningError("vector_screening_registry_binding_mismatch")
    if manifest.strategy_name != plugin.name or (
        manifest.strategy_version is not None
        and manifest.strategy_version != plugin.version
    ):
        raise VectorScreeningError("vector_screening_manifest_strategy_mismatch")
    timing = manifest.execution_timing
    if execution_timing_policy is not None and (
        execution_timing_policy.as_dict() != timing.as_dict()
    ):
        raise VectorScreeningError("vector_screening_manifest_timing_mismatch")
    split_range = _validate_manifest_dataset_binding(manifest=manifest, dataset=dataset)
    if (
        isinstance(forward_horizon_bars, bool)
        or not isinstance(forward_horizon_bars, int)
        or forward_horizon_bars <= 0
    ):
        raise VectorScreeningError("vector_screening_horizon_must_be_positive_int")
    _validate_manifest_parameter_candidate(
        manifest=manifest, parameter_values=parameter_values
    )
    slippage_bps = float(manifest.cost_model.slippage_bps[0])
    compiled = StrategyCompiler(registry).compile(
        strategy_name=plugin.name,
        raw_parameters=dict(parameter_values),
        fee_rate=float(manifest.cost_model.fee_rate),
        slippage_bps=slippage_bps,
        context=BacktestRunContext(
            split_name=str(dataset.split_name),
            diagnostic_mode="exploratory",
            policy_materialization_mode="research_exploratory",
        ),
    )
    rule_spec = plugin.spec.rule_spec
    if rule_spec is None:
        raise VectorScreeningError("vector_screening_strategy_rule_spec_missing")
    materialized = dict(compiled.materialized_parameters)
    execution_dataset, _ = point_in_time_execution_snapshot(
        snapshot=dataset,
        expected_decision_guard_ms=int(timing.decision_guard_ms),
    )
    generated = tuple(
        plugin.event_builder(
            dataset=execution_dataset,
            parameter_values=materialized,
            fee_rate=float(manifest.cost_model.fee_rate),
            slippage_bps=slippage_bps,
            execution_timing_policy=timing,
            portfolio_policy=None,
            context=None,
        )
    )
    index_by_ts = {
        int(candle.ts): index for index, candle in enumerate(execution_dataset.candles)
    }
    if len(index_by_ts) != len(execution_dataset.candles):
        raise VectorScreeningError("vector_dataset_candle_timestamp_duplicate")
    signals: list[VectorSignalRow] = []
    labels: list[ForwardLabelRow] = []
    for event in generated:
        candle_index = index_by_ts.get(int(event.candle_ts))
        if candle_index is None:
            raise VectorScreeningError("vector_signal_candle_outside_snapshot")
        candle = execution_dataset.candles[candle_index]
        if int(event.decision_ts) < candle.available_at_ms(
            interval=execution_dataset.interval
        ):
            raise VectorScreeningError("vector_signal_precedes_candle_availability")
        signal = _signal_from_event(event)
        signals.append(signal)
        label_index = candle_index + forward_horizon_bars
        if label_index >= len(execution_dataset.candles):
            continue
        future = execution_dataset.candles[label_index]
        current_close = float(candle.close)
        if not isfinite(current_close) or current_close <= 0:
            raise VectorScreeningError("vector_current_close_invalid")
        forward_return = float(future.close) / current_close - 1.0
        labels.append(
            ForwardLabelRow(
                decision_id=signal.decision_id,
                horizon_bars=forward_horizon_bars,
                label_available_at_ts=future.available_at_ms(
                    interval=execution_dataset.interval
                ),
                forward_close_return=forward_return,
            )
        )

    signal_rows = tuple(signals)
    label_rows = tuple(labels)
    split_payload = _split_binding_payload(
        manifest=manifest, dataset=dataset, split_range=split_range
    )
    values: dict[str, Any] = {
        "schema_version": VECTOR_SCREENING_SCHEMA_VERSION,
        "purpose": VECTOR_SCREENING_PURPOSE,
        "manifest_hash": manifest.manifest_hash(),
        "experiment_id": manifest.experiment_id,
        "strategy_name": plugin.name,
        "strategy_version": plugin.version,
        "strategy_plugin_contract_hash": plugin.contract_hash(),
        "strategy_rule_spec_hash": sha256_prefixed(rule_spec.as_dict()),
        "compiled_strategy_contract": compiled,
        "compiled_strategy_contract_hash": compiled.compiled_contract_hash,
        "strategy_registry_hash": compiled.strategy_registry_hash,
        "raw_parameter_values_hash": sha256_prefixed(compiled.raw_parameters),
        "parameter_values_hash": compiled.materialized_parameters_hash,
        "parameter_source_map_hash": sha256_prefixed(compiled.parameter_source_map),
        **split_payload,
        "split_binding_hash": sha256_prefixed(
            split_payload, label="vector_screening_split_binding"
        ),
        "execution_timing_hash": sha256_prefixed(timing.as_dict()),
        "forward_horizon_bars": forward_horizon_bars,
        "signals": signal_rows,
        "labels": label_rows,
        "decision_stream_hash": _decision_stream_hash(signal_rows),
        "label_stream_hash": _label_stream_hash(label_rows),
    }
    identity = _result_identity_payload(values)
    return VectorScreeningResult(
        **values,
        content_hash=sha256_prefixed(identity, label="vector_screening_result"),
    )


def assert_vector_event_decision_parity(
    *,
    screening: VectorScreeningResult,
    event_run: BacktestRun,
    manifest: ExperimentManifest,
) -> None:
    """Fail unless vector and event execution share every authoritative input."""

    # Dataclass replacement/manual construction cannot bypass result semantics.
    if screening.content_hash != sha256_prefixed(
        screening.identity_payload(), label="vector_screening_result"
    ):
        raise VectorScreeningError("vector_screening_content_hash_mismatch")
    _validate_screening_manifest_binding(screening=screening, manifest=manifest)
    try:
        event_run.validate_execution_lineage()
    except ValueError as exc:
        raise VectorScreeningError("vector_event_lineage_invalid") from exc
    compiled = event_run.compiled_strategy_contract
    if compiled is None:
        raise VectorScreeningError("vector_event_compiled_contract_missing")
    try:
        validate_compiled_strategy_contract(
            compiled,
            expected_strategy_name=screening.strategy_name,
            expected_strategy_version=screening.strategy_version,
            expected_plugin_hash=screening.strategy_plugin_contract_hash,
        )
    except ValueError as exc:
        raise VectorScreeningError("vector_event_compiled_contract_invalid") from exc
    if (
        compiled.compiled_contract_hash != screening.compiled_strategy_contract_hash
        or event_run.compiled_strategy_contract_hash
        != screening.compiled_strategy_contract_hash
        or event_run.strategy_registry_hash != screening.strategy_registry_hash
        or event_run.strategy_plugin_contract_hash
        != screening.strategy_plugin_contract_hash
        or event_run.dataset_snapshot_id != screening.dataset_snapshot_id
        or event_run.dataset_source != screening.dataset_source
        or event_run.dataset_market != screening.dataset_market
        or event_run.dataset_interval != screening.dataset_interval
        or event_run.dataset_period_start != screening.dataset_period_start
        or event_run.dataset_period_end != screening.dataset_period_end
        or event_run.dataset_artifact_manifest_hash
        != screening.dataset_artifact_manifest_hash
        or event_run.dataset_snapshot_hash != screening.dataset_snapshot_hash
        or event_run.dataset_data_hash != screening.dataset_data_hash
        or event_run.dataset_query_hash != screening.dataset_query_hash
        or event_run.dataset_split_name != screening.dataset_split_name
        or event_run.execution_timing_hash != screening.execution_timing_hash
        or event_run.materialized_parameters_hash != screening.parameter_values_hash
        or event_run.parameter_source_map_hash != screening.parameter_source_map_hash
    ):
        raise VectorScreeningError("vector_event_authoritative_input_mismatch")
    event_rows = tuple(_signal_from_event(item) for item in event_run.decisions)
    if screening.signals != event_rows:
        raise VectorScreeningError("vector_event_decision_stream_mismatch")
    if screening.decision_stream_hash != _decision_stream_hash(event_rows):
        raise VectorScreeningError("vector_event_decision_stream_hash_mismatch")
    event_full_stream_hash = sha256_prefixed(
        [item.as_dict() for item in event_run.decisions]
    )
    if (
        event_run.decision_stream_hash != event_full_stream_hash
        or tuple(item.decision_id() for item in event_run.decisions)
        != event_run.authoritative_decision_ids
    ):
        raise VectorScreeningError("vector_event_authoritative_decision_ids_mismatch")


def _signal_from_event(event: ResearchDecisionEvent) -> VectorSignalRow:
    return VectorSignalRow(
        decision_id=event.decision_id(),
        candle_ts=int(event.candle_ts),
        decision_ts=int(event.decision_ts),
        strategy_name=str(event.strategy_name),
        strategy_version=str(event.strategy_version),
        raw_signal=str(event.raw_signal),
        final_signal=str(event.final_signal),
        reason=str(event.reason),
        feature_snapshot=event.feature_snapshot,
        feature_snapshot_hash=sha256_prefixed(event.feature_snapshot),
        blocked_filters=tuple(str(item) for item in event.blocked_filters),
    )


def _validate_manifest_parameter_candidate(
    *, manifest: ExperimentManifest, parameter_values: Mapping[str, Any]
) -> None:
    if set(parameter_values) != set(manifest.parameter_space):
        raise VectorScreeningError("vector_screening_manifest_parameter_set_mismatch")
    for name, value in parameter_values.items():
        if value not in manifest.parameter_space[name]:
            raise VectorScreeningError(
                f"vector_screening_parameter_outside_manifest:{name}"
            )


def _manifest_split_range(manifest: ExperimentManifest, split_name: str) -> DateRange:
    if split_name == "train":
        return manifest.dataset.split.train
    if split_name == "validation":
        return manifest.dataset.split.validation
    if split_name == "final_holdout":
        raise VectorScreeningError("vector_screening_final_holdout_forbidden")
    raise VectorScreeningError("vector_screening_manifest_split_unknown")


def _validate_manifest_dataset_binding(
    *, manifest: ExperimentManifest, dataset: DatasetSnapshot
) -> DateRange:
    split_name = str(dataset.split_name).strip().lower()
    split_range = _manifest_split_range(manifest, split_name)
    if (
        dataset.split_name != split_name
        or dataset.snapshot_id != manifest.dataset.snapshot_id
        or dataset.source != manifest.dataset.source
        or dataset.market != manifest.market
        or dataset.interval != manifest.interval
        or dataset.date_range != split_range
        or dataset.artifact_manifest_hash
        != (
            manifest.dataset.artifact_ref.artifact_manifest_hash
            if manifest.dataset.artifact_ref is not None
            else None
        )
    ):
        raise VectorScreeningError("vector_screening_manifest_dataset_mismatch")
    if not dataset.candles:
        raise VectorScreeningError("vector_screening_dataset_empty")
    if any(
        int(candle.ts) < split_range.start_ts_ms()
        or int(candle.ts) > split_range.end_ts_ms()
        for candle in dataset.candles
    ):
        raise VectorScreeningError("vector_screening_candle_outside_manifest_split")
    return split_range


def _split_binding_payload(
    *,
    manifest: ExperimentManifest,
    dataset: DatasetSnapshot,
    split_range: DateRange,
) -> dict[str, object]:
    return {
        "manifest_hash": manifest.manifest_hash(),
        "experiment_id": manifest.experiment_id,
        "dataset_snapshot_id": dataset.snapshot_id,
        "dataset_source": dataset.source,
        "dataset_market": dataset.market,
        "dataset_interval": dataset.interval,
        "dataset_split_name": dataset.split_name,
        "dataset_period_start": split_range.start,
        "dataset_period_end": split_range.end,
        "dataset_artifact_manifest_hash": dataset.artifact_manifest_hash,
        "dataset_snapshot_hash": dataset.snapshot_fingerprint_hash(),
        "dataset_data_hash": dataset.snapshot_data_hash(),
        "dataset_query_hash": dataset.snapshot_query_hash(),
    }


def _validate_screening_manifest_binding(
    *, screening: VectorScreeningResult, manifest: ExperimentManifest
) -> None:
    split_range = _manifest_split_range(manifest, screening.dataset_split_name)
    expected_artifact_hash = (
        manifest.dataset.artifact_ref.artifact_manifest_hash
        if manifest.dataset.artifact_ref is not None
        else None
    )
    if (
        manifest.manifest_hash() != screening.manifest_hash
        or manifest.experiment_id != screening.experiment_id
        or manifest.strategy_name != screening.strategy_name
        or (
            manifest.strategy_version is not None
            and manifest.strategy_version != screening.strategy_version
        )
        or manifest.dataset.snapshot_id != screening.dataset_snapshot_id
        or manifest.dataset.source != screening.dataset_source
        or manifest.market != screening.dataset_market
        or manifest.interval != screening.dataset_interval
        or split_range.start != screening.dataset_period_start
        or split_range.end != screening.dataset_period_end
        or expected_artifact_hash != screening.dataset_artifact_manifest_hash
        or sha256_prefixed(manifest.execution_timing.as_dict())
        != screening.execution_timing_hash
    ):
        raise VectorScreeningError("vector_screening_manifest_binding_mismatch")


def _decision_stream_hash(signals: tuple[VectorSignalRow, ...]) -> str:
    return sha256_prefixed(
        [item.as_dict() for item in signals],
        label="vector_screening_decision_stream",
    )


def _label_stream_hash(labels: tuple[ForwardLabelRow, ...]) -> str:
    return sha256_prefixed(
        [item.as_dict() for item in labels],
        label="vector_screening_label_stream",
    )


def _result_identity_payload(values: Mapping[str, Any]) -> dict[str, object]:
    compiled = values["compiled_strategy_contract"]
    assert isinstance(compiled, CompiledStrategyContract)
    signals = values["signals"]
    labels = values["labels"]
    assert isinstance(signals, tuple) and isinstance(labels, tuple)
    return {
        key: (
            compiled.as_dict()
            if key == "compiled_strategy_contract"
            else [item.as_dict() for item in signals]
            if key == "signals"
            else [item.as_dict() for item in labels]
            if key == "labels"
            else value
        )
        for key, value in values.items()
    }


def _require_hash(value: object, reason: str) -> None:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise VectorScreeningError(reason)


def _require_text(value: object, reason: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise VectorScreeningError(reason)


__all__ = [
    "ForwardLabelRow",
    "VECTOR_SCREENING_PURPOSE",
    "VectorScreeningError",
    "VectorScreeningResult",
    "VectorSignalRow",
    "assert_vector_event_decision_parity",
    "run_vector_signal_screen",
]
