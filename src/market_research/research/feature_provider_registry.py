from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Literal, cast

from market_research.research.feature_diagnostic_features import (
    AsOfCandleView,
    BreakoutDistanceProvider,
    FeatureProvider,
    RangeRatioProvider,
    RegimeProvider,
    RollingReturnProvider,
    SmaGapProvider,
    VolumeRatioProvider,
    ZScoreProvider,
    FeatureValue,
)
from market_research.research.feature_definition import (
    FeatureDefinition,
    validate_feature_definition_set,
)


FeatureValueType = Literal["float", "str", "bool"]
BucketizerType = Literal["quantile", "category"]
NUMERIC_FEATURE_VALUE_TYPES = frozenset({"float", "int", "number"})
CATEGORY_FEATURE_VALUE_TYPES = frozenset({"str", "string", "bool", "category"})
REGIME_CATEGORY_UNIVERSE = tuple(
    "_".join(parts)
    for parts in product(
        ("unknown", "sideways", "uptrend", "downtrend"),
        ("low_vol", "normal_vol", "high_vol"),
        ("unknown", "volume_decreasing", "volume_normal", "volume_increasing"),
    )
)


@dataclass(frozen=True)
class FeatureProviderSpec:
    definition: FeatureDefinition
    provider: FeatureProvider
    bucketizer_type: BucketizerType
    category_universe: tuple[str, ...] = ()
    causal_contract_exemption_reason: str | None = None

    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def value_type(self) -> FeatureValueType:
        return cast(FeatureValueType, self.definition.value_type)

    @property
    def required_history(self) -> int:
        return self.definition.warm_up_bars

    @property
    def definition_hash(self) -> str:
        return self.definition.definition_hash

    @property
    def causal_inputs(self) -> tuple[str, ...]:
        return self.definition.inputs

    def compute(self, *, view: AsOfCandleView) -> FeatureValue | None:
        value = self.definition.compute(view=view)
        if value is None:
            return None
        if not isinstance(value, FeatureValue):
            raise ValueError(
                f"feature provider {self.name!r} returned non-FeatureValue"
            )
        return value

    def as_report_dict(self) -> dict[str, object]:
        return {
            **self.definition.as_dict(),
            "required_history": self.required_history,
            "bucketizer_type": self.bucketizer_type,
            "category_universe": list(self.category_universe),
            "causal_inputs": list(self.causal_inputs),
            "causal_contract_exemption_reason": self.causal_contract_exemption_reason,
        }


def list_feature_provider_specs() -> tuple[FeatureProviderSpec, ...]:
    specs = (
        _spec(
            provider=SmaGapProvider(),
            value_type="float",
            required_history=20,
            bucketizer_type="quantile",
            causal_inputs=("candle.close",),
            formula=(
                "(mean(close[-short_window:]) - mean(close[-long_window:])) / "
                "mean(close[-long_window:])"
            ),
            unit="ratio",
            missing_policy="return_none_until_long_window_or_zero_denominator",
        ),
        _spec(
            provider=RangeRatioProvider(),
            value_type="float",
            required_history=1,
            bucketizer_type="quantile",
            causal_inputs=("candle.high", "candle.low", "candle.close"),
            formula="(current.high - current.low) / current.close",
            unit="ratio",
            missing_policy="return_none_when_current_close_is_non_positive",
        ),
        _spec(
            provider=VolumeRatioProvider(),
            value_type="float",
            required_history=11,
            bucketizer_type="quantile",
            causal_inputs=("candle.volume",),
            formula="current.volume / mean(previous_volume[-window:])",
            unit="ratio",
            missing_policy="return_none_until_window_or_non_positive_baseline",
        ),
        _spec(
            provider=BreakoutDistanceProvider(),
            value_type="float",
            required_history=20,
            bucketizer_type="quantile",
            causal_inputs=("candle.high", "candle.close"),
            formula="(current.close - max(prior_high[-window:])) / max(prior_high[-window:])",
            unit="ratio",
            missing_policy="return_none_until_window_or_non_positive_prior_high",
        ),
        _spec(
            provider=RollingReturnProvider(),
            value_type="float",
            required_history=6,
            bucketizer_type="quantile",
            causal_inputs=("candle.close",),
            formula="current.close / close[-lookback] - 1",
            unit="ratio",
            missing_policy="return_none_until_lookback_or_non_positive_past_close",
        ),
        _spec(
            provider=ZScoreProvider(),
            value_type="float",
            required_history=20,
            bucketizer_type="quantile",
            causal_inputs=("candle.close",),
            formula="(current.close - mean(close[-window:])) / std(close[-window:])",
            unit="standard_deviation",
            missing_policy="return_none_until_window;zero_when_standard_deviation_is_zero",
        ),
        _spec(
            provider=RegimeProvider(),
            value_type="str",
            required_history=2,
            bucketizer_type="category",
            causal_inputs=(
                "candle.close",
                "candle.high",
                "candle.low",
                "candle.volume",
            ),
            category_universe=REGIME_CATEGORY_UNIVERSE,
            formula="classify_market_regime_from_arrays(completed_as_of_history)",
            unit="category",
            missing_policy="return_none_before_two_completed_bars",
            outlier_policy="delegate_to_versioned_regime_classifier",
        ),
    )
    _validate_specs(specs)
    return specs


def feature_provider_spec_for_name(name: str) -> FeatureProviderSpec:
    normalized = str(name).strip()
    specs = {spec.name: spec for spec in list_feature_provider_specs()}
    try:
        return specs[normalized]
    except KeyError as exc:
        allowed = ", ".join(sorted(specs))
        raise ValueError(
            f"unknown diagnostic feature={name!r}; allowed values: {allowed}"
        ) from exc


def feature_provider_specs_for_names(
    names: tuple[str, ...],
) -> tuple[FeatureProviderSpec, ...]:
    if not names:
        raise ValueError("features must not be empty")
    return tuple(feature_provider_spec_for_name(name) for name in names)


def _spec(
    *,
    provider: FeatureProvider,
    value_type: FeatureValueType,
    required_history: int,
    bucketizer_type: BucketizerType,
    causal_inputs: tuple[str, ...],
    formula: str,
    unit: str,
    missing_policy: str,
    outlier_policy: str = "preserve_finite_value",
    category_universe: tuple[str, ...] = (),
) -> FeatureProviderSpec:
    name = str(provider.name)
    raw_parameters = getattr(provider, "__dict__", None)
    parameters = (
        tuple(sorted((str(key), value) for key, value in raw_parameters.items()))
        if isinstance(raw_parameters, dict)
        else (("name", name),)
    )
    definition = FeatureDefinition(
        name=name,
        description=f"Causal diagnostic feature provider for {name}.",
        source_data=tuple(causal_inputs),
        calculation=formula,
        feature_id=f"diagnostic.{name}",
        version="1.0.0",
        value_type=value_type,
        warm_up_bars=int(required_history),
        current_bar_rule="completed_current_bar_inclusive",
        availability_lag_ms=0,
        missing_policy=missing_policy,
        outlier_policy=outlier_policy,
        unit=unit,
        leakage_risk="low_as_of_view_enforced",
        consumers=("forward_diagnostics.feature_mining",),
        implementation_parameters=parameters,
        calculator=provider.compute,
    )
    return FeatureProviderSpec(
        definition=definition,
        provider=provider,
        bucketizer_type=bucketizer_type,
        category_universe=tuple(category_universe),
    )


def _validate_specs(specs: tuple[FeatureProviderSpec, ...]) -> None:
    validate_feature_definition_set(tuple(spec.definition for spec in specs))
    names = [spec.name for spec in specs]
    if len(names) != len(set(names)):
        raise ValueError("diagnostic feature provider names must be unique")
    for spec in specs:
        if not spec.name:
            raise ValueError("diagnostic feature provider name must be non-empty")
        if spec.value_type not in {"float", "str", "bool"}:
            raise ValueError(
                f"diagnostic feature provider {spec.name} has invalid value_type"
            )
        if spec.bucketizer_type not in {"quantile", "category"}:
            raise ValueError(
                f"diagnostic feature provider {spec.name} has invalid bucketizer_type"
            )
        _validate_bucketizer_value_type(spec)
        if spec.required_history <= 0:
            raise ValueError(
                f"diagnostic feature provider {spec.name} must declare positive required_history"
            )
        if not spec.definition_hash:
            raise ValueError(
                f"diagnostic feature provider {spec.name} must declare definition_hash"
            )
        if not spec.causal_inputs:
            raise ValueError(
                f"diagnostic feature provider {spec.name} must declare causal_inputs"
            )
        if spec.category_universe and spec.bucketizer_type != "category":
            raise ValueError(
                f"diagnostic feature provider {spec.name} declares category_universe for non-category bucketizer"
            )


def _validate_bucketizer_value_type(spec: FeatureProviderSpec) -> None:
    value_type = str(spec.value_type).lower()
    if (
        spec.bucketizer_type == "quantile"
        and value_type not in NUMERIC_FEATURE_VALUE_TYPES
    ):
        raise ValueError(
            f"diagnostic feature provider {spec.name} quantile bucketizer requires numeric value_type"
        )
    if (
        spec.bucketizer_type == "category"
        and value_type not in CATEGORY_FEATURE_VALUE_TYPES
    ):
        raise ValueError(
            f"diagnostic feature provider {spec.name} category bucketizer requires categorical value_type"
        )


def validate_feature_value_against_spec(
    spec: FeatureProviderSpec, value: FeatureValue
) -> None:
    if value.name != spec.name:
        raise ValueError(
            f"feature provider contract violation: value name {value.name!r} does not match spec {spec.name!r}"
        )
    if value.value_type != spec.value_type:
        raise ValueError(
            f"feature provider contract violation: value_type {value.value_type!r} "
            f"does not match spec {spec.value_type!r} for feature {spec.name!r}"
        )
    _validate_bucketizer_value_type(spec)
