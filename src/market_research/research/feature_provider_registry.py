from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Literal

from market_research.research.feature_diagnostic_features import (
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
from market_research.research.hashing import sha256_prefixed


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
    name: str
    provider: FeatureProvider
    value_type: FeatureValueType
    required_history: int
    definition_hash: str
    bucketizer_type: BucketizerType
    causal_inputs: tuple[str, ...]
    category_universe: tuple[str, ...] = ()
    causal_contract_exemption_reason: str | None = None

    def as_report_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value_type": self.value_type,
            "required_history": self.required_history,
            "bucketizer_type": self.bucketizer_type,
            "category_universe": list(self.category_universe),
            "definition_hash": self.definition_hash,
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
        ),
        _spec(
            provider=RangeRatioProvider(),
            value_type="float",
            required_history=1,
            bucketizer_type="quantile",
            causal_inputs=("candle.high", "candle.low", "candle.close"),
        ),
        _spec(
            provider=VolumeRatioProvider(),
            value_type="float",
            required_history=11,
            bucketizer_type="quantile",
            causal_inputs=("candle.volume",),
        ),
        _spec(
            provider=BreakoutDistanceProvider(),
            value_type="float",
            required_history=20,
            bucketizer_type="quantile",
            causal_inputs=("candle.high", "candle.close"),
        ),
        _spec(
            provider=RollingReturnProvider(),
            value_type="float",
            required_history=6,
            bucketizer_type="quantile",
            causal_inputs=("candle.close",),
        ),
        _spec(
            provider=ZScoreProvider(),
            value_type="float",
            required_history=20,
            bucketizer_type="quantile",
            causal_inputs=("candle.close",),
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
    category_universe: tuple[str, ...] = (),
) -> FeatureProviderSpec:
    name = str(provider.name)
    return FeatureProviderSpec(
        name=name,
        provider=provider,
        value_type=value_type,
        required_history=int(required_history),
        definition_hash=_definition_hash(provider),
        bucketizer_type=bucketizer_type,
        causal_inputs=tuple(causal_inputs),
        category_universe=tuple(category_universe),
    )


def _definition_hash(provider: FeatureProvider) -> str:
    raw_parameters = getattr(provider, "__dict__", None)
    parameters = (
        {str(key): value for key, value in raw_parameters.items()}
        if isinstance(raw_parameters, dict)
        else {"name": provider.name}
    )
    return sha256_prefixed(
        {
            "provider_name": str(provider.name),
            "provider_parameters": parameters,
        }
    )


def _validate_specs(specs: tuple[FeatureProviderSpec, ...]) -> None:
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
