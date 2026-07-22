"""Typed prospective-maintainability monitoring for derivative research.

This module is deliberately an offline evidence contract.  It neither obtains
market data nor makes live decisions.  Baselines are frozen before monitoring
starts, current observations must be point-in-time admissible at evaluation,
and every value is represented as an exact :class:`~decimal.Decimal`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from market_research.research.derivatives.common import (
    DerivativeResearchError,
    decimal_text,
    exact_decimal,
    parse_timestamp,
    require_hash,
    require_stable_id,
)
from market_research.research.hashing import sha256_prefixed


PROSPECTIVE_MONITORING_SCHEMA_VERSION = 1
_ZERO = Decimal("0")
_ONE = Decimal("1")


class MonitoringProductKind(StrEnum):
    """Supported research products; ``MULTI_LEG`` means an option structure."""

    FUTURE = "FUTURE"
    OPTION = "OPTION"
    MULTI_LEG = "MULTI_LEG"


class MonitoringMetric(StrEnum):
    """The complete S6-M01..M14 maintainability metric authority."""

    EXPECTED_VALUE = "expected_value"
    WIN_RATE = "win_rate"
    PNL_DISTRIBUTION = "pnl_distribution"
    SIGNAL_FREQUENCY = "signal_frequency"
    HOLDING_PERIOD = "holding_period"
    COSTS = "costs"
    SLIPPAGE = "slippage"
    LIQUIDITY = "liquidity"
    FEATURE_DISTRIBUTION = "feature_distribution"
    MARKET_REGIME = "market_regime"
    FUTURES_TERM_STRUCTURE = "futures_term_structure"
    OPTION_SURFACE_SKEW = "option_surface_skew"
    GREEKS_EXPOSURE = "greeks_exposure"
    TAIL_EVENT_CONTRIBUTION = "tail_event_contribution"


class ObservationRole(StrEnum):
    BASELINE = "BASELINE"
    CURRENT = "CURRENT"


class DriftMethod(StrEnum):
    ABSOLUTE_MAX = "ABSOLUTE_MAX"
    RELATIVE_MAX = "RELATIVE_MAX"
    DOWNSIDE_RELATIVE_MAX = "DOWNSIDE_RELATIVE_MAX"
    UPSIDE_RELATIVE_MAX = "UPSIDE_RELATIVE_MAX"


class MonitoringOutcome(StrEnum):
    CONFIRMED = "CONFIRMED"
    DEGRADED = "DEGRADED"
    INVALIDATED = "INVALIDATED"
    INCONCLUSIVE = "INCONCLUSIVE"


METRIC_DIMENSIONS: Mapping[MonitoringMetric, tuple[str, ...]] = {
    MonitoringMetric.EXPECTED_VALUE: ("net_expected_value",),
    MonitoringMetric.WIN_RATE: ("win_fraction",),
    MonitoringMetric.PNL_DISTRIBUTION: ("p05", "p25", "p50", "p75", "p95"),
    MonitoringMetric.SIGNAL_FREQUENCY: ("signals_per_session",),
    MonitoringMetric.HOLDING_PERIOD: ("median_seconds", "p95_seconds"),
    MonitoringMetric.COSTS: ("total_cost", "cost_per_fill"),
    MonitoringMetric.SLIPPAGE: ("median_bps", "p95_bps"),
    MonitoringMetric.LIQUIDITY: ("median_spread_bps", "p05_depth"),
    MonitoringMetric.FEATURE_DISTRIBUTION: (
        "standardized_mean",
        "scale_ratio",
        "tail_fraction",
    ),
    MonitoringMetric.MARKET_REGIME: (
        "trend_fraction",
        "range_fraction",
        "stress_fraction",
    ),
    MonitoringMetric.FUTURES_TERM_STRUCTURE: (
        "front_spread",
        "annualized_roll_yield",
        "curve_slope",
    ),
    MonitoringMetric.OPTION_SURFACE_SKEW: (
        "atm_iv",
        "put_25d_skew",
        "call_25d_skew",
    ),
    MonitoringMetric.GREEKS_EXPOSURE: ("delta", "gamma", "vega", "theta"),
    MonitoringMetric.TAIL_EVENT_CONTRIBUTION: (
        "tail_pnl_fraction",
        "worst_event_pnl",
    ),
}


_COMMON_REQUIRED = (
    MonitoringMetric.EXPECTED_VALUE,
    MonitoringMetric.WIN_RATE,
    MonitoringMetric.PNL_DISTRIBUTION,
    MonitoringMetric.SIGNAL_FREQUENCY,
    MonitoringMetric.HOLDING_PERIOD,
    MonitoringMetric.COSTS,
    MonitoringMetric.SLIPPAGE,
    MonitoringMetric.LIQUIDITY,
    MonitoringMetric.FEATURE_DISTRIBUTION,
    MonitoringMetric.MARKET_REGIME,
    MonitoringMetric.TAIL_EVENT_CONTRIBUTION,
)

_REQUIRED_METRICS: Mapping[MonitoringProductKind, tuple[MonitoringMetric, ...]] = {
    MonitoringProductKind.FUTURE: (
        *_COMMON_REQUIRED,
        MonitoringMetric.FUTURES_TERM_STRUCTURE,
    ),
    MonitoringProductKind.OPTION: (
        *_COMMON_REQUIRED,
        MonitoringMetric.OPTION_SURFACE_SKEW,
        MonitoringMetric.GREEKS_EXPOSURE,
    ),
    MonitoringProductKind.MULTI_LEG: (
        *_COMMON_REQUIRED,
        MonitoringMetric.OPTION_SURFACE_SKEW,
        MonitoringMetric.GREEKS_EXPOSURE,
    ),
}


EXPECTED_DRIFT_METHOD: Mapping[MonitoringMetric, DriftMethod] = {
    MonitoringMetric.EXPECTED_VALUE: DriftMethod.DOWNSIDE_RELATIVE_MAX,
    MonitoringMetric.WIN_RATE: DriftMethod.DOWNSIDE_RELATIVE_MAX,
    MonitoringMetric.PNL_DISTRIBUTION: DriftMethod.RELATIVE_MAX,
    MonitoringMetric.SIGNAL_FREQUENCY: DriftMethod.RELATIVE_MAX,
    MonitoringMetric.HOLDING_PERIOD: DriftMethod.RELATIVE_MAX,
    MonitoringMetric.COSTS: DriftMethod.UPSIDE_RELATIVE_MAX,
    MonitoringMetric.SLIPPAGE: DriftMethod.UPSIDE_RELATIVE_MAX,
    MonitoringMetric.LIQUIDITY: DriftMethod.RELATIVE_MAX,
    MonitoringMetric.FEATURE_DISTRIBUTION: DriftMethod.ABSOLUTE_MAX,
    MonitoringMetric.MARKET_REGIME: DriftMethod.ABSOLUTE_MAX,
    MonitoringMetric.FUTURES_TERM_STRUCTURE: DriftMethod.ABSOLUTE_MAX,
    MonitoringMetric.OPTION_SURFACE_SKEW: DriftMethod.ABSOLUTE_MAX,
    MonitoringMetric.GREEKS_EXPOSURE: DriftMethod.RELATIVE_MAX,
    MonitoringMetric.TAIL_EVENT_CONTRIBUTION: DriftMethod.RELATIVE_MAX,
}


def required_metrics(
    product_kind: MonitoringProductKind,
) -> tuple[MonitoringMetric, ...]:
    if not isinstance(product_kind, MonitoringProductKind):
        raise DerivativeResearchError("monitoring_product_kind_invalid")
    return _REQUIRED_METRICS[product_kind]


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise DerivativeResearchError(f"{label}_must_be_object")
    return value


def _strict_keys(
    value: Mapping[str, object], required: frozenset[str], label: str
) -> None:
    keys = frozenset(value)
    if keys != required:
        missing = ",".join(sorted(required - keys))
        unknown = ",".join(sorted(keys - required))
        raise DerivativeResearchError(
            f"{label}_fields_invalid:missing={missing}:unknown={unknown}"
        )


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise DerivativeResearchError(f"{label}_must_be_string")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DerivativeResearchError(f"{label}_must_be_integer")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise DerivativeResearchError(f"{label}_must_be_array")
    return value


def _enum_value[T: StrEnum](enum_type: type[T], value: object, label: str) -> T:
    try:
        return enum_type(_string(value, label))
    except ValueError as exc:
        raise DerivativeResearchError(f"{label}_invalid") from exc


def _verify_hash(provided: object, actual: str, label: str) -> None:
    supplied = _string(provided, f"{label}.content_hash")
    require_hash(supplied, f"{label}.content_hash")
    if supplied != actual:
        raise DerivativeResearchError(f"{label}_content_hash_mismatch")


def _decimal_tuple(value: object, label: str) -> tuple[Decimal, ...]:
    return tuple(
        exact_decimal(item, f"{label}[{index}]")
        for index, item in enumerate(_sequence(value, label))
    )


def _validate_metric_values(
    metric: MonitoringMetric, values: tuple[Decimal, ...]
) -> None:
    if len(values) != len(METRIC_DIMENSIONS[metric]):
        raise DerivativeResearchError("monitoring_metric_dimension_mismatch")
    non_negative_metrics = {
        MonitoringMetric.SIGNAL_FREQUENCY,
        MonitoringMetric.HOLDING_PERIOD,
        MonitoringMetric.COSTS,
        MonitoringMetric.SLIPPAGE,
        MonitoringMetric.LIQUIDITY,
    }
    if metric in non_negative_metrics and any(value < _ZERO for value in values):
        raise DerivativeResearchError("monitoring_metric_value_must_be_non_negative")
    if metric is MonitoringMetric.WIN_RATE and not _ZERO <= values[0] <= _ONE:
        raise DerivativeResearchError("monitoring_win_rate_out_of_range")
    if metric is MonitoringMetric.PNL_DISTRIBUTION and tuple(sorted(values)) != values:
        raise DerivativeResearchError("monitoring_pnl_quantiles_not_monotonic")
    if metric in {MonitoringMetric.HOLDING_PERIOD, MonitoringMetric.SLIPPAGE}:
        if values[0] > values[1]:
            raise DerivativeResearchError("monitoring_quantile_order_invalid")
    if metric is MonitoringMetric.FEATURE_DISTRIBUTION:
        if values[1] < _ZERO or not _ZERO <= values[2] <= _ONE:
            raise DerivativeResearchError("monitoring_feature_summary_invalid")
    if metric is MonitoringMetric.MARKET_REGIME:
        if any(value < _ZERO for value in values) or sum(values, _ZERO) != _ONE:
            raise DerivativeResearchError("monitoring_regime_fractions_invalid")


@dataclass(frozen=True, slots=True)
class MetricObservation:
    """A frozen baseline or PIT-admissible current metric observation."""

    observation_id: str
    role: ObservationRole
    product_kind: MonitoringProductKind
    metric: MonitoringMetric
    period_started_at: str
    period_ended_at: str
    known_at: str
    dataset_snapshot_hash: str
    source_manifest_hash: str
    calculation_policy_hash: str
    observed_count: int
    missing_count: int
    values: tuple[Decimal, ...] | None
    frozen_spec_hash: str | None = None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.observation_id, "monitoring_observation.observation_id")
        if not isinstance(self.role, ObservationRole):
            raise DerivativeResearchError("monitoring_observation_role_invalid")
        if not isinstance(self.product_kind, MonitoringProductKind):
            raise DerivativeResearchError("monitoring_observation_product_invalid")
        if not isinstance(self.metric, MonitoringMetric):
            raise DerivativeResearchError("monitoring_observation_metric_invalid")
        started = parse_timestamp(
            self.period_started_at, "monitoring_observation.period_started_at"
        )
        ended = parse_timestamp(
            self.period_ended_at, "monitoring_observation.period_ended_at"
        )
        known = parse_timestamp(self.known_at, "monitoring_observation.known_at")
        if started > ended or ended > known:
            raise DerivativeResearchError("monitoring_observation_time_order_invalid")
        for name, value in (
            ("dataset_snapshot_hash", self.dataset_snapshot_hash),
            ("source_manifest_hash", self.source_manifest_hash),
            ("calculation_policy_hash", self.calculation_policy_hash),
        ):
            require_hash(value, f"monitoring_observation.{name}")
        if self.observed_count < 0 or self.missing_count < 0:
            raise DerivativeResearchError("monitoring_observation_count_invalid")
        if self.observed_count + self.missing_count <= 0:
            raise DerivativeResearchError("monitoring_observation_sample_empty")
        parsed_values: tuple[Decimal, ...] | None
        if self.values is None:
            if self.observed_count != 0 or self.missing_count <= 0:
                raise DerivativeResearchError("monitoring_missing_metric_state_invalid")
            parsed_values = None
        else:
            if self.observed_count <= 0:
                raise DerivativeResearchError(
                    "monitoring_observed_values_without_sample"
                )
            parsed_values = tuple(
                exact_decimal(value, "monitoring_observation.value")
                for value in self.values
            )
            _validate_metric_values(self.metric, parsed_values)
        object.__setattr__(self, "values", parsed_values)
        if self.role is ObservationRole.BASELINE:
            if self.frozen_spec_hash is not None or parsed_values is None:
                raise DerivativeResearchError("monitoring_baseline_binding_invalid")
        else:
            if self.frozen_spec_hash is None:
                raise DerivativeResearchError("monitoring_current_spec_hash_required")
            require_hash(
                self.frozen_spec_hash, "monitoring_observation.frozen_spec_hash"
            )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="monitoring_observation"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": PROSPECTIVE_MONITORING_SCHEMA_VERSION,
            "observation_id": self.observation_id,
            "role": self.role.value,
            "product_kind": self.product_kind.value,
            "metric": self.metric.value,
            "period_started_at": self.period_started_at,
            "period_ended_at": self.period_ended_at,
            "known_at": self.known_at,
            "dataset_snapshot_hash": self.dataset_snapshot_hash,
            "source_manifest_hash": self.source_manifest_hash,
            "calculation_policy_hash": self.calculation_policy_hash,
            "observed_count": self.observed_count,
            "missing_count": self.missing_count,
            "dimensions": list(METRIC_DIMENSIONS[self.metric]),
            "values": (
                None
                if self.values is None
                else [decimal_text(value) for value in self.values]
            ),
            "frozen_spec_hash": self.frozen_spec_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> MetricObservation:
        data = _mapping(value, "monitoring_observation")
        _strict_keys(
            data,
            frozenset(
                {
                    "schema_version",
                    "observation_id",
                    "role",
                    "product_kind",
                    "metric",
                    "period_started_at",
                    "period_ended_at",
                    "known_at",
                    "dataset_snapshot_hash",
                    "source_manifest_hash",
                    "calculation_policy_hash",
                    "observed_count",
                    "missing_count",
                    "dimensions",
                    "values",
                    "frozen_spec_hash",
                    "content_hash",
                }
            ),
            "monitoring_observation",
        )
        if (
            _integer(data["schema_version"], "monitoring_observation.schema_version")
            != 1
        ):
            raise DerivativeResearchError("monitoring_observation_schema_unsupported")
        metric = _enum_value(
            MonitoringMetric, data["metric"], "monitoring_observation.metric"
        )
        dimensions = tuple(
            _string(item, "monitoring_observation.dimension")
            for item in _sequence(
                data["dimensions"], "monitoring_observation.dimensions"
            )
        )
        if dimensions != METRIC_DIMENSIONS[metric]:
            raise DerivativeResearchError("monitoring_metric_dimensions_tampered")
        raw_values = data["values"]
        values = (
            None
            if raw_values is None
            else _decimal_tuple(raw_values, "monitoring_observation.values")
        )
        raw_spec_hash = data["frozen_spec_hash"]
        result = cls(
            observation_id=_string(
                data["observation_id"], "monitoring_observation.observation_id"
            ),
            role=_enum_value(
                ObservationRole, data["role"], "monitoring_observation.role"
            ),
            product_kind=_enum_value(
                MonitoringProductKind,
                data["product_kind"],
                "monitoring_observation.product_kind",
            ),
            metric=metric,
            period_started_at=_string(
                data["period_started_at"],
                "monitoring_observation.period_started_at",
            ),
            period_ended_at=_string(
                data["period_ended_at"], "monitoring_observation.period_ended_at"
            ),
            known_at=_string(data["known_at"], "monitoring_observation.known_at"),
            dataset_snapshot_hash=_string(
                data["dataset_snapshot_hash"],
                "monitoring_observation.dataset_snapshot_hash",
            ),
            source_manifest_hash=_string(
                data["source_manifest_hash"],
                "monitoring_observation.source_manifest_hash",
            ),
            calculation_policy_hash=_string(
                data["calculation_policy_hash"],
                "monitoring_observation.calculation_policy_hash",
            ),
            observed_count=_integer(
                data["observed_count"], "monitoring_observation.observed_count"
            ),
            missing_count=_integer(
                data["missing_count"], "monitoring_observation.missing_count"
            ),
            values=values,
            frozen_spec_hash=(
                None
                if raw_spec_hash is None
                else _string(raw_spec_hash, "monitoring_observation.frozen_spec_hash")
            ),
        )
        _verify_hash(
            data["content_hash"], result.content_hash, "monitoring_observation"
        )
        return result


@dataclass(frozen=True, slots=True)
class MetricDriftRule:
    metric: MonitoringMetric
    method: DriftMethod
    threshold_version: str
    minimum_observed_count: int
    maximum_missing_fraction: Decimal
    degradation_threshold: Decimal
    invalidation_threshold: Decimal
    relative_scale_floor: Decimal
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.metric, MonitoringMetric):
            raise DerivativeResearchError("monitoring_rule_metric_invalid")
        if not isinstance(self.method, DriftMethod):
            raise DerivativeResearchError("monitoring_rule_method_invalid")
        if self.method is not EXPECTED_DRIFT_METHOD[self.metric]:
            raise DerivativeResearchError("monitoring_rule_method_not_authoritative")
        require_stable_id(self.threshold_version, "monitoring_rule.threshold_version")
        if self.minimum_observed_count <= 0:
            raise DerivativeResearchError("monitoring_rule_minimum_sample_invalid")
        maximum_missing = exact_decimal(
            self.maximum_missing_fraction,
            "monitoring_rule.maximum_missing_fraction",
        )
        degradation = exact_decimal(
            self.degradation_threshold, "monitoring_rule.degradation_threshold"
        )
        invalidation = exact_decimal(
            self.invalidation_threshold, "monitoring_rule.invalidation_threshold"
        )
        scale_floor = exact_decimal(
            self.relative_scale_floor,
            "monitoring_rule.relative_scale_floor",
            positive=True,
        )
        if not _ZERO <= maximum_missing <= _ONE:
            raise DerivativeResearchError("monitoring_rule_missing_fraction_invalid")
        if degradation < _ZERO or invalidation <= degradation:
            raise DerivativeResearchError("monitoring_rule_threshold_order_invalid")
        object.__setattr__(self, "maximum_missing_fraction", maximum_missing)
        object.__setattr__(self, "degradation_threshold", degradation)
        object.__setattr__(self, "invalidation_threshold", invalidation)
        object.__setattr__(self, "relative_scale_floor", scale_floor)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="monitoring_drift_rule"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "metric": self.metric.value,
            "method": self.method.value,
            "threshold_version": self.threshold_version,
            "minimum_observed_count": self.minimum_observed_count,
            "maximum_missing_fraction": decimal_text(self.maximum_missing_fraction),
            "degradation_threshold": decimal_text(self.degradation_threshold),
            "invalidation_threshold": decimal_text(self.invalidation_threshold),
            "relative_scale_floor": decimal_text(self.relative_scale_floor),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> MetricDriftRule:
        data = _mapping(value, "monitoring_rule")
        _strict_keys(
            data,
            frozenset(
                {
                    "metric",
                    "method",
                    "threshold_version",
                    "minimum_observed_count",
                    "maximum_missing_fraction",
                    "degradation_threshold",
                    "invalidation_threshold",
                    "relative_scale_floor",
                    "content_hash",
                }
            ),
            "monitoring_rule",
        )
        result = cls(
            metric=_enum_value(
                MonitoringMetric, data["metric"], "monitoring_rule.metric"
            ),
            method=_enum_value(DriftMethod, data["method"], "monitoring_rule.method"),
            threshold_version=_string(
                data["threshold_version"], "monitoring_rule.threshold_version"
            ),
            minimum_observed_count=_integer(
                data["minimum_observed_count"],
                "monitoring_rule.minimum_observed_count",
            ),
            maximum_missing_fraction=exact_decimal(
                data["maximum_missing_fraction"],
                "monitoring_rule.maximum_missing_fraction",
            ),
            degradation_threshold=exact_decimal(
                data["degradation_threshold"],
                "monitoring_rule.degradation_threshold",
            ),
            invalidation_threshold=exact_decimal(
                data["invalidation_threshold"],
                "monitoring_rule.invalidation_threshold",
            ),
            relative_scale_floor=exact_decimal(
                data["relative_scale_floor"], "monitoring_rule.relative_scale_floor"
            ),
        )
        _verify_hash(data["content_hash"], result.content_hash, "monitoring_rule")
        return result


@dataclass(frozen=True, slots=True)
class FrozenMonitoringSpec:
    """Pre-start baseline and threshold authority for one product study."""

    monitoring_id: str
    product_kind: MonitoringProductKind
    research_rule_hash: str
    experiment_spec_hash: str
    validation_decision_hash: str
    baseline_observations: tuple[MetricObservation, ...]
    drift_rules: tuple[MetricDriftRule, ...]
    frozen_at: str
    monitoring_started_at: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.monitoring_id, "monitoring_spec.monitoring_id")
        if not isinstance(self.product_kind, MonitoringProductKind):
            raise DerivativeResearchError("monitoring_spec_product_invalid")
        for name, value in (
            ("research_rule_hash", self.research_rule_hash),
            ("experiment_spec_hash", self.experiment_spec_hash),
            ("validation_decision_hash", self.validation_decision_hash),
        ):
            require_hash(value, f"monitoring_spec.{name}")
        frozen = parse_timestamp(self.frozen_at, "monitoring_spec.frozen_at")
        started = parse_timestamp(
            self.monitoring_started_at, "monitoring_spec.monitoring_started_at"
        )
        if started <= frozen:
            raise DerivativeResearchError("monitoring_spec_start_not_after_freeze")
        required = set(required_metrics(self.product_kind))
        observations = tuple(self.baseline_observations)
        rules = tuple(self.drift_rules)
        if any(not isinstance(item, MetricObservation) for item in observations):
            raise DerivativeResearchError("monitoring_spec_baseline_item_invalid")
        if any(not isinstance(item, MetricDriftRule) for item in rules):
            raise DerivativeResearchError("monitoring_spec_rule_item_invalid")
        if {item.metric for item in observations} != required or len(
            observations
        ) != len(required):
            raise DerivativeResearchError("monitoring_spec_required_baselines_invalid")
        if {item.metric for item in rules} != required or len(rules) != len(required):
            raise DerivativeResearchError("monitoring_spec_required_rules_invalid")
        if len({item.observation_id for item in observations}) != len(observations):
            raise DerivativeResearchError("monitoring_spec_baseline_id_duplicate")
        for observation in observations:
            if observation.role is not ObservationRole.BASELINE:
                raise DerivativeResearchError("monitoring_spec_baseline_role_invalid")
            if observation.product_kind is not self.product_kind:
                raise DerivativeResearchError(
                    "monitoring_spec_baseline_product_mismatch"
                )
            if (
                parse_timestamp(
                    observation.known_at, "monitoring_spec.baseline_known_at"
                )
                > frozen
            ):
                raise DerivativeResearchError(
                    "monitoring_spec_future_baseline_forbidden"
                )
            if (
                parse_timestamp(
                    observation.period_ended_at,
                    "monitoring_spec.baseline_period_ended_at",
                )
                >= started
            ):
                raise DerivativeResearchError("monitoring_spec_baseline_period_overlap")
        object.__setattr__(
            self,
            "baseline_observations",
            tuple(sorted(observations, key=lambda item: item.metric.value)),
        )
        object.__setattr__(
            self,
            "drift_rules",
            tuple(sorted(rules, key=lambda item: item.metric.value)),
        )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="frozen_monitoring_spec"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": PROSPECTIVE_MONITORING_SCHEMA_VERSION,
            "monitoring_id": self.monitoring_id,
            "product_kind": self.product_kind.value,
            "research_rule_hash": self.research_rule_hash,
            "experiment_spec_hash": self.experiment_spec_hash,
            "validation_decision_hash": self.validation_decision_hash,
            "baseline_observations": [
                item.as_dict() for item in self.baseline_observations
            ],
            "drift_rules": [item.as_dict() for item in self.drift_rules],
            "frozen_at": self.frozen_at,
            "monitoring_started_at": self.monitoring_started_at,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> FrozenMonitoringSpec:
        data = _mapping(value, "monitoring_spec")
        _strict_keys(
            data,
            frozenset(
                {
                    "schema_version",
                    "monitoring_id",
                    "product_kind",
                    "research_rule_hash",
                    "experiment_spec_hash",
                    "validation_decision_hash",
                    "baseline_observations",
                    "drift_rules",
                    "frozen_at",
                    "monitoring_started_at",
                    "content_hash",
                }
            ),
            "monitoring_spec",
        )
        if _integer(data["schema_version"], "monitoring_spec.schema_version") != 1:
            raise DerivativeResearchError("monitoring_spec_schema_unsupported")
        result = cls(
            monitoring_id=_string(
                data["monitoring_id"], "monitoring_spec.monitoring_id"
            ),
            product_kind=_enum_value(
                MonitoringProductKind,
                data["product_kind"],
                "monitoring_spec.product_kind",
            ),
            research_rule_hash=_string(
                data["research_rule_hash"], "monitoring_spec.research_rule_hash"
            ),
            experiment_spec_hash=_string(
                data["experiment_spec_hash"], "monitoring_spec.experiment_spec_hash"
            ),
            validation_decision_hash=_string(
                data["validation_decision_hash"],
                "monitoring_spec.validation_decision_hash",
            ),
            baseline_observations=tuple(
                MetricObservation.from_dict(item)
                for item in _sequence(
                    data["baseline_observations"],
                    "monitoring_spec.baseline_observations",
                )
            ),
            drift_rules=tuple(
                MetricDriftRule.from_dict(item)
                for item in _sequence(
                    data["drift_rules"], "monitoring_spec.drift_rules"
                )
            ),
            frozen_at=_string(data["frozen_at"], "monitoring_spec.frozen_at"),
            monitoring_started_at=_string(
                data["monitoring_started_at"], "monitoring_spec.monitoring_started_at"
            ),
        )
        _verify_hash(data["content_hash"], result.content_hash, "monitoring_spec")
        return result


@dataclass(frozen=True, slots=True)
class MetricMonitoringDecision:
    metric: MonitoringMetric
    method: DriftMethod
    threshold_version: str
    baseline_observation_hash: str
    current_observation_hash: str
    drift: Decimal | None
    missing_fraction: Decimal
    observed_count: int
    missing_count: int
    outcome: MonitoringOutcome
    diagnostic: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.metric, MonitoringMetric) or not isinstance(
            self.method, DriftMethod
        ):
            raise DerivativeResearchError("monitoring_decision_type_invalid")
        require_stable_id(
            self.threshold_version, "monitoring_decision.threshold_version"
        )
        require_hash(
            self.baseline_observation_hash,
            "monitoring_decision.baseline_observation_hash",
        )
        require_hash(
            self.current_observation_hash,
            "monitoring_decision.current_observation_hash",
        )
        drift = (
            None
            if self.drift is None
            else exact_decimal(self.drift, "monitoring_decision.drift")
        )
        missing_fraction = exact_decimal(
            self.missing_fraction, "monitoring_decision.missing_fraction"
        )
        if drift is not None and drift < _ZERO:
            raise DerivativeResearchError("monitoring_decision_drift_negative")
        if not _ZERO <= missing_fraction <= _ONE:
            raise DerivativeResearchError(
                "monitoring_decision_missing_fraction_invalid"
            )
        if self.observed_count < 0 or self.missing_count < 0:
            raise DerivativeResearchError("monitoring_decision_count_invalid")
        if not isinstance(self.outcome, MonitoringOutcome):
            raise DerivativeResearchError("monitoring_decision_outcome_invalid")
        require_stable_id(self.diagnostic, "monitoring_decision.diagnostic")
        object.__setattr__(self, "drift", drift)
        object.__setattr__(self, "missing_fraction", missing_fraction)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(), label="monitoring_metric_decision"
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "metric": self.metric.value,
            "method": self.method.value,
            "threshold_version": self.threshold_version,
            "baseline_observation_hash": self.baseline_observation_hash,
            "current_observation_hash": self.current_observation_hash,
            "drift": None if self.drift is None else decimal_text(self.drift),
            "missing_fraction": decimal_text(self.missing_fraction),
            "observed_count": self.observed_count,
            "missing_count": self.missing_count,
            "outcome": self.outcome.value,
            "diagnostic": self.diagnostic,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def _calculate_drift(
    baseline: tuple[Decimal, ...],
    current: tuple[Decimal, ...],
    rule: MetricDriftRule,
) -> Decimal:
    if len(baseline) != len(current):
        raise DerivativeResearchError("monitoring_drift_dimension_mismatch")
    values: list[Decimal] = []
    for before, after in zip(baseline, current, strict=True):
        difference = after - before
        denominator = max(abs(before), rule.relative_scale_floor)
        if rule.method is DriftMethod.ABSOLUTE_MAX:
            values.append(abs(difference))
        elif rule.method is DriftMethod.RELATIVE_MAX:
            values.append(abs(difference) / denominator)
        elif rule.method is DriftMethod.DOWNSIDE_RELATIVE_MAX:
            values.append(max(_ZERO, -difference) / denominator)
        else:
            values.append(max(_ZERO, difference) / denominator)
    return max(values, default=_ZERO)


def _metric_decision(
    baseline: MetricObservation,
    current: MetricObservation,
    rule: MetricDriftRule,
) -> MetricMonitoringDecision:
    total = current.observed_count + current.missing_count
    missing_fraction = Decimal(current.missing_count) / Decimal(total)
    if current.values is None or current.observed_count < rule.minimum_observed_count:
        drift = None
        outcome = MonitoringOutcome.INCONCLUSIVE
        diagnostic = "insufficient_sample"
    elif missing_fraction > rule.maximum_missing_fraction:
        drift = None
        outcome = MonitoringOutcome.INCONCLUSIVE
        diagnostic = "missingness_limit_exceeded"
    else:
        assert baseline.values is not None
        drift = _calculate_drift(baseline.values, current.values, rule)
        if drift >= rule.invalidation_threshold:
            outcome = MonitoringOutcome.INVALIDATED
            diagnostic = "invalidation_threshold_reached"
        elif drift >= rule.degradation_threshold:
            outcome = MonitoringOutcome.DEGRADED
            diagnostic = "degradation_threshold_reached"
        else:
            outcome = MonitoringOutcome.CONFIRMED
            diagnostic = "within_frozen_threshold"
    return MetricMonitoringDecision(
        metric=rule.metric,
        method=rule.method,
        threshold_version=rule.threshold_version,
        baseline_observation_hash=baseline.content_hash,
        current_observation_hash=current.content_hash,
        drift=drift,
        missing_fraction=missing_fraction,
        observed_count=current.observed_count,
        missing_count=current.missing_count,
        outcome=outcome,
        diagnostic=diagnostic,
    )


def _aggregate_outcome(
    decisions: tuple[MetricMonitoringDecision, ...],
) -> MonitoringOutcome:
    outcomes = {item.outcome for item in decisions}
    if MonitoringOutcome.INVALIDATED in outcomes:
        return MonitoringOutcome.INVALIDATED
    if MonitoringOutcome.DEGRADED in outcomes:
        return MonitoringOutcome.DEGRADED
    if MonitoringOutcome.INCONCLUSIVE in outcomes:
        return MonitoringOutcome.INCONCLUSIVE
    return MonitoringOutcome.CONFIRMED


@dataclass(frozen=True, slots=True)
class ProspectiveMonitoringArtifact:
    """Hash-bound result; it is E4-compatible evidence, not an E5 claim."""

    spec: FrozenMonitoringSpec
    current_observations: tuple[MetricObservation, ...]
    metric_decisions: tuple[MetricMonitoringDecision, ...]
    outcome: MonitoringOutcome
    evaluated_at: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.spec, FrozenMonitoringSpec):
            raise DerivativeResearchError("monitoring_artifact_spec_invalid")
        evaluated = parse_timestamp(
            self.evaluated_at, "monitoring_artifact.evaluated_at"
        )
        started = parse_timestamp(
            self.spec.monitoring_started_at,
            "monitoring_artifact.monitoring_started_at",
        )
        if evaluated < started:
            raise DerivativeResearchError("monitoring_artifact_evaluated_before_start")
        if not isinstance(self.outcome, MonitoringOutcome):
            raise DerivativeResearchError("monitoring_artifact_outcome_invalid")
        observations = tuple(self.current_observations)
        decisions = tuple(self.metric_decisions)
        if any(not isinstance(item, MetricObservation) for item in observations):
            raise DerivativeResearchError("monitoring_artifact_observation_invalid")
        if any(not isinstance(item, MetricMonitoringDecision) for item in decisions):
            raise DerivativeResearchError("monitoring_artifact_decision_invalid")
        required = set(required_metrics(self.spec.product_kind))
        if {item.metric for item in observations} != required or len(
            observations
        ) != len(required):
            raise DerivativeResearchError(
                "monitoring_artifact_required_metrics_invalid"
            )
        if len({item.observation_id for item in observations}) != len(observations):
            raise DerivativeResearchError(
                "monitoring_artifact_observation_id_duplicate"
            )
        for observation in observations:
            if observation.role is not ObservationRole.CURRENT:
                raise DerivativeResearchError(
                    "monitoring_artifact_current_role_required"
                )
            if observation.product_kind is not self.spec.product_kind:
                raise DerivativeResearchError("monitoring_artifact_product_mismatch")
            if observation.frozen_spec_hash != self.spec.content_hash:
                raise DerivativeResearchError("monitoring_artifact_spec_hash_mismatch")
            if (
                parse_timestamp(
                    observation.period_started_at,
                    "monitoring_artifact.current_period_started_at",
                )
                < started
            ):
                raise DerivativeResearchError("monitoring_artifact_pre_start_data")
            if (
                parse_timestamp(
                    observation.known_at, "monitoring_artifact.current_known_at"
                )
                > evaluated
            ):
                raise DerivativeResearchError("monitoring_artifact_future_data")
        baseline_by_metric = {
            item.metric: item for item in self.spec.baseline_observations
        }
        current_by_metric = {item.metric: item for item in observations}
        rule_by_metric = {item.metric: item for item in self.spec.drift_rules}
        expected_decisions = tuple(
            _metric_decision(
                baseline_by_metric[metric],
                current_by_metric[metric],
                rule_by_metric[metric],
            )
            for metric in sorted(required, key=lambda item: item.value)
        )
        if tuple(sorted(decisions, key=lambda item: item.metric.value)) != (
            expected_decisions
        ):
            raise DerivativeResearchError("monitoring_artifact_decisions_invalid")
        if self.outcome is not _aggregate_outcome(expected_decisions):
            raise DerivativeResearchError("monitoring_artifact_aggregate_invalid")
        object.__setattr__(
            self,
            "current_observations",
            tuple(sorted(observations, key=lambda item: item.metric.value)),
        )
        object.__setattr__(
            self,
            "metric_decisions",
            tuple(sorted(decisions, key=lambda item: item.metric.value)),
        )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(), label="prospective_monitoring_artifact"
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": PROSPECTIVE_MONITORING_SCHEMA_VERSION,
            "artifact_kind": "DERIVATIVE_PROSPECTIVE_MONITORING",
            "spec": self.spec.as_dict(),
            "current_observations": [
                item.as_dict() for item in self.current_observations
            ],
            "metric_decisions": [item.as_dict() for item in self.metric_decisions],
            "outcome": self.outcome.value,
            "evaluated_at": self.evaluated_at,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def reference(self) -> MonitoringArtifactRef:
        return MonitoringArtifactRef(
            monitoring_id=self.spec.monitoring_id,
            product_kind=self.spec.product_kind,
            artifact_hash=self.content_hash,
            evaluated_at=self.evaluated_at,
        )

    @classmethod
    def from_dict(cls, value: object) -> ProspectiveMonitoringArtifact:
        data = _mapping(value, "monitoring_artifact")
        _strict_keys(
            data,
            frozenset(
                {
                    "schema_version",
                    "artifact_kind",
                    "spec",
                    "current_observations",
                    "metric_decisions",
                    "outcome",
                    "evaluated_at",
                    "content_hash",
                }
            ),
            "monitoring_artifact",
        )
        if _integer(data["schema_version"], "monitoring_artifact.schema_version") != 1:
            raise DerivativeResearchError("monitoring_artifact_schema_unsupported")
        if _string(data["artifact_kind"], "monitoring_artifact.artifact_kind") != (
            "DERIVATIVE_PROSPECTIVE_MONITORING"
        ):
            raise DerivativeResearchError("monitoring_artifact_kind_invalid")
        spec = FrozenMonitoringSpec.from_dict(data["spec"])
        current = tuple(
            MetricObservation.from_dict(item)
            for item in _sequence(
                data["current_observations"],
                "monitoring_artifact.current_observations",
            )
        )
        rebuilt = evaluate_prospective_monitoring(
            spec,
            current,
            evaluated_at=_string(
                data["evaluated_at"], "monitoring_artifact.evaluated_at"
            ),
        )
        supplied_decisions = _sequence(
            data["metric_decisions"], "monitoring_artifact.metric_decisions"
        )
        if list(supplied_decisions) != [
            item.as_dict() for item in rebuilt.metric_decisions
        ]:
            raise DerivativeResearchError("monitoring_artifact_decisions_tampered")
        if (
            _enum_value(
                MonitoringOutcome, data["outcome"], "monitoring_artifact.outcome"
            )
            is not rebuilt.outcome
        ):
            raise DerivativeResearchError("monitoring_artifact_outcome_tampered")
        _verify_hash(data["content_hash"], rebuilt.content_hash, "monitoring_artifact")
        return rebuilt


@dataclass(frozen=True, slots=True)
class MonitoringArtifactRef:
    monitoring_id: str
    product_kind: MonitoringProductKind
    artifact_hash: str
    evaluated_at: str

    def __post_init__(self) -> None:
        require_stable_id(self.monitoring_id, "monitoring_ref.monitoring_id")
        if not isinstance(self.product_kind, MonitoringProductKind):
            raise DerivativeResearchError("monitoring_ref_product_invalid")
        require_hash(self.artifact_hash, "monitoring_ref.artifact_hash")
        parse_timestamp(self.evaluated_at, "monitoring_ref.evaluated_at")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PROSPECTIVE_MONITORING_SCHEMA_VERSION,
            "artifact_kind": "DERIVATIVE_PROSPECTIVE_MONITORING",
            "monitoring_id": self.monitoring_id,
            "product_kind": self.product_kind.value,
            "artifact_hash": self.artifact_hash,
            "evaluated_at": self.evaluated_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> MonitoringArtifactRef:
        data = _mapping(value, "monitoring_ref")
        _strict_keys(
            data,
            frozenset(
                {
                    "schema_version",
                    "artifact_kind",
                    "monitoring_id",
                    "product_kind",
                    "artifact_hash",
                    "evaluated_at",
                }
            ),
            "monitoring_ref",
        )
        if _integer(data["schema_version"], "monitoring_ref.schema_version") != 1:
            raise DerivativeResearchError("monitoring_ref_schema_unsupported")
        if _string(data["artifact_kind"], "monitoring_ref.artifact_kind") != (
            "DERIVATIVE_PROSPECTIVE_MONITORING"
        ):
            raise DerivativeResearchError("monitoring_ref_kind_invalid")
        return cls(
            monitoring_id=_string(
                data["monitoring_id"], "monitoring_ref.monitoring_id"
            ),
            product_kind=_enum_value(
                MonitoringProductKind,
                data["product_kind"],
                "monitoring_ref.product_kind",
            ),
            artifact_hash=_string(
                data["artifact_hash"], "monitoring_ref.artifact_hash"
            ),
            evaluated_at=_string(data["evaluated_at"], "monitoring_ref.evaluated_at"),
        )


def evaluate_prospective_monitoring(
    spec: FrozenMonitoringSpec,
    current_observations: Sequence[MetricObservation],
    *,
    evaluated_at: str,
) -> ProspectiveMonitoringArtifact:
    """Evaluate required current metrics against a previously frozen authority."""

    if not isinstance(spec, FrozenMonitoringSpec):
        raise DerivativeResearchError("monitoring_evaluation_spec_invalid")
    evaluated = parse_timestamp(evaluated_at, "monitoring_evaluation.evaluated_at")
    started = parse_timestamp(
        spec.monitoring_started_at, "monitoring_evaluation.monitoring_started_at"
    )
    if evaluated < started:
        raise DerivativeResearchError("monitoring_evaluation_before_start")
    observations = tuple(current_observations)
    if any(not isinstance(item, MetricObservation) for item in observations):
        raise DerivativeResearchError("monitoring_evaluation_observation_invalid")
    required = set(required_metrics(spec.product_kind))
    if {item.metric for item in observations} != required or len(observations) != len(
        required
    ):
        raise DerivativeResearchError("monitoring_evaluation_required_metrics_missing")
    if len({item.observation_id for item in observations}) != len(observations):
        raise DerivativeResearchError("monitoring_evaluation_observation_id_duplicate")
    for observation in observations:
        if observation.role is not ObservationRole.CURRENT:
            raise DerivativeResearchError("monitoring_evaluation_current_role_required")
        if observation.product_kind is not spec.product_kind:
            raise DerivativeResearchError("monitoring_evaluation_product_mismatch")
        if observation.frozen_spec_hash != spec.content_hash:
            raise DerivativeResearchError("monitoring_evaluation_spec_hash_mismatch")
        if (
            parse_timestamp(
                observation.period_started_at,
                "monitoring_evaluation.current_period_started_at",
            )
            < started
        ):
            raise DerivativeResearchError(
                "monitoring_evaluation_pre_start_data_forbidden"
            )
        if (
            parse_timestamp(
                observation.known_at, "monitoring_evaluation.current_known_at"
            )
            > evaluated
        ):
            raise DerivativeResearchError("monitoring_evaluation_future_data_forbidden")
    baseline_by_metric = {item.metric: item for item in spec.baseline_observations}
    current_by_metric = {item.metric: item for item in observations}
    rule_by_metric = {item.metric: item for item in spec.drift_rules}
    decisions = tuple(
        _metric_decision(
            baseline_by_metric[metric],
            current_by_metric[metric],
            rule_by_metric[metric],
        )
        for metric in sorted(required, key=lambda item: item.value)
    )
    return ProspectiveMonitoringArtifact(
        spec=spec,
        current_observations=observations,
        metric_decisions=decisions,
        outcome=_aggregate_outcome(decisions),
        evaluated_at=evaluated_at,
    )
