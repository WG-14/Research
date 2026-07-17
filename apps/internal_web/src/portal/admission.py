"""Bounded raw-manifest admission checks for the internal web adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from django.conf import settings
from django.core.exceptions import ValidationError


@dataclass(frozen=True, slots=True)
class ManifestAdmissionEstimate:
    candidate_count: int
    scenario_count: int
    work_unit_count: int


def validate_raw_manifest_admission(
    payload: dict[str, Any],
) -> ManifestAdmissionEstimate:
    """Reject combinatorial work before the core parser materializes products."""

    candidate_limit = int(settings.INTERNAL_WEB_MAX_PARAMETER_CANDIDATES)
    scenario_limit = int(settings.INTERNAL_WEB_MAX_EXECUTION_SCENARIOS)
    work_unit_limit = int(settings.INTERNAL_WEB_MAX_WORK_UNITS)
    if min(candidate_limit, scenario_limit, work_unit_limit) <= 0:
        raise RuntimeError("internal_web_admission_limits_must_be_positive")

    parameter_space = payload.get("parameter_space")
    if not isinstance(parameter_space, dict) or not parameter_space:
        raise ValidationError("manifest_admission_parameter_space_invalid")
    candidate_sizes: list[int] = []
    for values in parameter_space.values():
        if not isinstance(values, list) or not values:
            raise ValidationError("manifest_admission_parameter_space_invalid")
        candidate_sizes.append(len(values))
    candidate_count = _bounded_product(
        candidate_sizes,
        limit=candidate_limit,
        error_code="manifest_admission_candidate_limit_exceeded",
    )

    scenario_count = _raw_execution_scenario_count(
        payload,
        limit=scenario_limit,
    )
    if candidate_count > work_unit_limit // scenario_count:
        raise ValidationError("manifest_admission_work_unit_limit_exceeded")
    return ManifestAdmissionEstimate(
        candidate_count=candidate_count,
        scenario_count=scenario_count,
        work_unit_count=candidate_count * scenario_count,
    )


def _raw_execution_scenario_count(payload: dict[str, Any], *, limit: int) -> int:
    execution_model = payload.get("execution_model")
    cost_model = payload.get("cost_model")
    legacy_slippage_count = _non_empty_list_length(
        cost_model.get("slippage_bps") if isinstance(cost_model, dict) else None
    )
    if execution_model is None:
        return _bounded_product(
            (legacy_slippage_count,),
            limit=limit,
            error_code="manifest_admission_scenario_limit_exceeded",
        )
    if not isinstance(execution_model, dict):
        # The canonical parser will provide the schema diagnostic. Admission
        # still uses a conservative non-zero count and never materializes it.
        return 1

    if "scenarios" in execution_model:
        scenarios = execution_model.get("scenarios")
        explicit_count = _non_empty_list_length(scenarios)
        return _bounded_product(
            (explicit_count,),
            limit=limit,
            error_code="manifest_admission_scenario_limit_exceeded",
        )

    dimensions = (
        _non_empty_list_length(execution_model.get("fee_rate")),
        (
            _non_empty_list_length(execution_model.get("slippage_bps"))
            if "slippage_bps" in execution_model
            else legacy_slippage_count
        ),
        _non_empty_list_length(execution_model.get("latency_ms")),
        _non_empty_list_length(execution_model.get("partial_fill_rate")),
        _non_empty_list_length(execution_model.get("order_failure_rate")),
        _non_empty_list_length(execution_model.get("market_order_extra_cost_bps")),
    )
    return _bounded_product(
        dimensions,
        limit=limit,
        error_code="manifest_admission_scenario_limit_exceeded",
    )


def _non_empty_list_length(value: Any) -> int:
    return max(1, len(value)) if isinstance(value, list) else 1


def _bounded_product(
    dimensions: Iterable[int],
    *,
    limit: int,
    error_code: str,
) -> int:
    product = 1
    for dimension in dimensions:
        size = max(1, int(dimension))
        if product > limit // size:
            raise ValidationError(error_code)
        product *= size
    return product
