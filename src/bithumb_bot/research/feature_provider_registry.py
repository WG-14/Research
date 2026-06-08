from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from bithumb_bot.research.feature_diagnostic_features import (
    BreakoutDistanceProvider,
    FeatureProvider,
    RangeRatioProvider,
    RegimeProvider,
    RollingReturnProvider,
    SmaGapProvider,
    VolumeRatioProvider,
    ZScoreProvider,
)
from bithumb_bot.research.hashing import sha256_prefixed


FeatureValueType = Literal["float", "str", "bool"]
BucketizerType = Literal["quantile", "category"]


@dataclass(frozen=True)
class FeatureProviderSpec:
    name: str
    provider: FeatureProvider
    value_type: FeatureValueType
    required_history: int
    definition_hash: str
    bucketizer_type: BucketizerType
    causal_inputs: tuple[str, ...]
    causal_contract_exemption_reason: str | None = None


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
            causal_inputs=("candle.close", "candle.high", "candle.low", "candle.volume"),
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
        raise ValueError(f"unknown diagnostic feature={name!r}; allowed values: {allowed}") from exc


def feature_provider_specs_for_names(names: tuple[str, ...]) -> tuple[FeatureProviderSpec, ...]:
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
    )


def _definition_hash(provider: FeatureProvider) -> str:
    parameters = asdict(provider) if hasattr(provider, "__dataclass_fields__") else {"name": provider.name}
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
            raise ValueError(f"diagnostic feature provider {spec.name} has invalid value_type")
        if spec.bucketizer_type not in {"quantile", "category"}:
            raise ValueError(f"diagnostic feature provider {spec.name} has invalid bucketizer_type")
        if spec.required_history <= 0:
            raise ValueError(f"diagnostic feature provider {spec.name} must declare positive required_history")
        if not spec.definition_hash:
            raise ValueError(f"diagnostic feature provider {spec.name} must declare definition_hash")
        if not spec.causal_inputs:
            raise ValueError(f"diagnostic feature provider {spec.name} must declare causal_inputs")
