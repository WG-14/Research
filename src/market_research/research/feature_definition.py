from __future__ import annotations

import math
from dataclasses import KW_ONLY, dataclass, field
from types import CodeType
from typing import Callable

from .hashing import sha256_prefixed


FeatureCalculator = Callable[..., object]


class FeatureDefinitionError(ValueError):
    """A feature definition is incomplete or not bound to its implementation."""


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    """Versioned authority shared by strategy and diagnostic feature runtimes.

    The first five fields preserve the historical ``StrategyFeatureDefinition``
    constructor.  New definitions should explicitly supply the remaining causal
    and lifecycle fields.  A callable implementation is excluded from the
    serialized contract, but its normalized code hash is included so calculation
    changes necessarily change the definition hash.
    """

    name: str
    description: str
    source_data: tuple[str, ...]
    calculation: str
    lookback_parameter_names: tuple[str, ...] = ()
    _: KW_ONLY
    feature_id: str = ""
    version: str = "1"
    value_type: str = "float"
    warm_up_bars: int = 1
    warm_up_parameter_names: tuple[str, ...] = ()
    current_bar_rule: str = "completed_current_bar_inclusive"
    availability_lag_ms: int = 0
    missing_policy: str = "not_available_until_warm_up"
    outlier_policy: str = "preserve_finite_value"
    unit: str = "unspecified"
    leakage_risk: str = "low_as_of_only"
    consumers: tuple[str, ...] = ("strategy_runtime",)
    implementation_parameters: tuple[tuple[str, object], ...] = ()
    calculator: FeatureCalculator | None = field(
        default=None, repr=False, compare=False
    )
    implementation_dependencies: tuple[FeatureCalculator, ...] = field(
        default=(), repr=False, compare=False
    )
    implementation_code_hash: str = ""
    schema_version: int = 1
    implementation_kind: str = field(init=False)

    def __post_init__(self) -> None:
        feature_id = self.feature_id.strip() or f"feature.{self.name.strip()}"
        object.__setattr__(self, "feature_id", feature_id)
        if self.schema_version != 1:
            raise FeatureDefinitionError(
                "feature_definition_schema_version_unsupported"
            )
        required_text = {
            "feature_id": self.feature_id,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "calculation": self.calculation,
            "value_type": self.value_type,
            "current_bar_rule": self.current_bar_rule,
            "missing_policy": self.missing_policy,
            "outlier_policy": self.outlier_policy,
            "unit": self.unit,
            "leakage_risk": self.leakage_risk,
        }
        missing = sorted(
            key for key, value in required_text.items() if not value.strip()
        )
        if missing:
            raise FeatureDefinitionError(
                "feature_definition_required_field_missing:" + ",".join(missing)
            )
        if not self.source_data or any(not item.strip() for item in self.source_data):
            raise FeatureDefinitionError(
                f"feature_definition_inputs_required:{self.feature_id}"
            )
        _require_unique(self.source_data, "inputs", self.feature_id)
        _require_unique(
            self.lookback_parameter_names,
            "lookback_parameter_names",
            self.feature_id,
        )
        _require_unique(
            self.warm_up_parameter_names,
            "warm_up_parameter_names",
            self.feature_id,
        )
        _require_unique(self.consumers, "consumers", self.feature_id)
        if not self.consumers or any(not item.strip() for item in self.consumers):
            raise FeatureDefinitionError(
                f"feature_definition_consumers_required:{self.feature_id}"
            )
        if self.warm_up_bars < 0:
            raise FeatureDefinitionError(
                f"feature_definition_warm_up_negative:{self.feature_id}"
            )
        if self.availability_lag_ms < 0:
            raise FeatureDefinitionError(
                f"feature_definition_availability_lag_negative:{self.feature_id}"
            )
        parameter_names = [name for name, _value in self.implementation_parameters]
        if any(not name.strip() for name in parameter_names):
            raise FeatureDefinitionError(
                f"feature_definition_parameter_name_invalid:{self.feature_id}"
            )
        _require_unique(parameter_names, "implementation_parameters", self.feature_id)

        if self.calculator is None:
            implementation_hash = self.implementation_code_hash or sha256_prefixed(
                {
                    "feature_id": self.feature_id,
                    "version": self.version,
                    "formula": self.calculation,
                },
                label="feature_formula_contract",
            )
            implementation_kind = "formula_contract"
        else:
            actual_hash = feature_implementation_hash(
                self.calculator, dependencies=self.implementation_dependencies
            )
            if (
                self.implementation_code_hash
                and self.implementation_code_hash != actual_hash
            ):
                raise FeatureDefinitionError(
                    f"feature_definition_code_hash_mismatch:{self.feature_id}"
                )
            implementation_hash = actual_hash
            implementation_kind = "callable_code"
        if not _is_sha256(implementation_hash):
            raise FeatureDefinitionError(
                f"feature_definition_code_hash_invalid:{self.feature_id}"
            )
        object.__setattr__(self, "implementation_code_hash", implementation_hash)
        object.__setattr__(self, "implementation_kind", implementation_kind)

    @property
    def inputs(self) -> tuple[str, ...]:
        return self.source_data

    @property
    def formula(self) -> str:
        return self.calculation

    @property
    def definition_hash(self) -> str:
        return sha256_prefixed(
            self.identity_payload(), label="versioned_feature_definition"
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "feature_id": self.feature_id,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "inputs": list(self.inputs),
            "formula": self.formula,
            "value_type": self.value_type,
            "implementation_kind": self.implementation_kind,
            "implementation_code_hash": self.implementation_code_hash,
            "implementation_parameters": {
                name: value for name, value in self.implementation_parameters
            },
            "warm_up": {
                "minimum_bars": self.warm_up_bars,
                "parameter_names": list(self.warm_up_parameter_names),
            },
            "lookback_parameter_names": list(self.lookback_parameter_names),
            "current_bar_rule": self.current_bar_rule,
            "availability_lag_ms": self.availability_lag_ms,
            "missing_policy": self.missing_policy,
            "outlier_policy": self.outlier_policy,
            "unit": self.unit,
            "leakage_risk": self.leakage_risk,
            "consumers": list(self.consumers),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "definition_hash": self.definition_hash}

    def compute(self, **kwargs: object) -> object:
        if self.calculator is None:
            raise FeatureDefinitionError(
                f"feature_definition_calculator_missing:{self.feature_id}"
            )
        return self.calculator(**kwargs)


def feature_implementation_hash(
    calculator: FeatureCalculator,
    *,
    dependencies: tuple[FeatureCalculator, ...] = (),
) -> str:
    """Hash normalized callable code without paths or source line numbers."""

    callables = (calculator, *dependencies)
    payload = [_callable_payload(item) for item in callables]
    return sha256_prefixed(payload, label="feature_implementation_code")


def validate_computed_feature_value(
    definition: FeatureDefinition, value: object
) -> None:
    value_type = definition.value_type
    if value_type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FeatureDefinitionError(
                f"feature_value_type_mismatch:{definition.feature_id}:float"
            )
        if not math.isfinite(float(value)):
            raise FeatureDefinitionError(
                f"feature_value_non_finite:{definition.feature_id}"
            )
        return
    if value_type == "int" and isinstance(value, int) and not isinstance(value, bool):
        return
    if value_type == "str" and isinstance(value, str):
        return
    if value_type == "bool" and isinstance(value, bool):
        return
    raise FeatureDefinitionError(
        f"feature_value_type_mismatch:{definition.feature_id}:{value_type}"
    )


def validate_feature_definition_set(
    definitions: tuple[FeatureDefinition, ...],
) -> None:
    if not definitions:
        return
    _require_unique(
        [item.name for item in definitions], "names", "feature_definition_set"
    )
    _require_unique(
        [item.feature_id for item in definitions],
        "feature_ids",
        "feature_definition_set",
    )
    _require_unique(
        [f"{item.feature_id}@{item.version}" for item in definitions],
        "versioned_feature_ids",
        "feature_definition_set",
    )


def _callable_payload(calculator: FeatureCalculator) -> dict[str, object]:
    target = getattr(calculator, "__func__", calculator)
    code = getattr(target, "__code__", None)
    if not isinstance(code, CodeType):
        raise FeatureDefinitionError("feature_calculator_python_code_required")
    return {
        "module": str(getattr(target, "__module__", "")),
        "qualname": str(getattr(target, "__qualname__", "")),
        "code": _code_payload(code),
        "defaults": _stable_value(getattr(target, "__defaults__", None)),
        "kwdefaults": _stable_value(getattr(target, "__kwdefaults__", None)),
    }


def _code_payload(code: CodeType) -> dict[str, object]:
    return {
        "argcount": code.co_argcount,
        "posonlyargcount": code.co_posonlyargcount,
        "kwonlyargcount": code.co_kwonlyargcount,
        "flags": code.co_flags,
        "bytecode": code.co_code.hex(),
        "constants": [_stable_value(value) for value in code.co_consts],
        "names": list(code.co_names),
        "varnames": list(code.co_varnames),
        "freevars": list(code.co_freevars),
        "cellvars": list(code.co_cellvars),
    }


def _stable_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if isinstance(value, CodeType):
        return {"code": _code_payload(value)}
    if isinstance(value, (tuple, list)):
        return [_stable_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _stable_value(item)
            for key, item in sorted(value.items(), key=lambda row: str(row[0]))
        }
    if isinstance(value, (set, frozenset)):
        rows = [_stable_value(item) for item in value]
        return sorted(rows, key=repr)
    return {
        "type": f"{type(value).__module__}.{type(value).__qualname__}",
        "repr": repr(value),
    }


def _require_unique(
    values: list[str] | tuple[str, ...], field: str, owner: str
) -> None:
    if len(values) != len(set(values)):
        raise FeatureDefinitionError(f"feature_definition_{field}_not_unique:{owner}")


def _is_sha256(value: str) -> bool:
    prefix = "sha256:"
    if not value.startswith(prefix) or len(value) != len(prefix) + 64:
        return False
    return all(character in "0123456789abcdef" for character in value[len(prefix) :])


__all__ = [
    "FeatureCalculator",
    "FeatureDefinition",
    "FeatureDefinitionError",
    "feature_implementation_hash",
    "validate_computed_feature_value",
    "validate_feature_definition_set",
]
