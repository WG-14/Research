from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .h74_observation import (
    H74ObservationAuthorityError,
    H74_SOURCE_OBSERVATION_AUTHORITY_ARTIFACT_TYPE,
    H74_SOURCE_VARIANT_OBSERVATION_AUTHORITY_ARTIFACT_TYPE,
    h74_source_runtime_values_from_settings,
    verify_h74_source_observation_authority,
    verify_h74_source_variant_observation_authority,
)
from .runtime_strategy_set import h74_runtime_adapter_materialized_values_from_settings


H74_AUTHORITY_ENV_BEHAVIOR_MISMATCH = "H74_AUTHORITY_ENV_BEHAVIOR_MISMATCH"
H74_FIXED_POSITION_REQUIRED_FIELDS = (
    "strategy_instance_id",
    "authority_content_hash",
    "position_mode",
    "hold_policy",
    "partial_fill_policy",
)


@dataclass(frozen=True)
class H74AuthorityEnvAlignment:
    ok: bool
    reason_code: str
    authority_type: str
    mismatched_keys: tuple[str, ...]
    raw_settings_parameters: Mapping[str, object]
    effective_behavior_parameters: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "reason_code": self.reason_code,
            "authority_type": self.authority_type,
            "mismatched_keys": list(self.mismatched_keys),
            "raw_settings_parameters": dict(self.raw_settings_parameters),
            "effective_behavior_parameters": dict(self.effective_behavior_parameters),
        }


def load_h74_authority_payload(path: str | Path) -> dict[str, object]:
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise H74ObservationAuthorityError("h74_authority_payload_not_object")
    return payload


def _match(actual: object, expected: object) -> bool:
    if isinstance(expected, Mapping):
        return isinstance(actual, Mapping) and dict(actual) == dict(expected)
    if isinstance(expected, bool):
        if isinstance(actual, bool):
            return actual is expected
        return (str(actual).strip().lower() in {"1", "true", "yes", "on"}) is expected
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return float(actual) == float(expected)
        except (TypeError, ValueError):
            return False
    return str(actual) == str(expected)


def validate_h74_authority_env_alignment(
    authority_payload: Mapping[str, object],
    *,
    settings_obj: object,
    raise_on_mismatch: bool = True,
) -> H74AuthorityEnvAlignment:
    payload = dict(authority_payload)
    authority_type = str(payload.get("authority_type") or payload.get("artifact_type") or "")
    raw_settings_values = h74_source_runtime_values_from_settings(settings_obj)
    effective_behavior_values = h74_runtime_adapter_materialized_values_from_settings(settings_obj)
    bound = dict(payload.get("hash_bound_parameters") or {})
    position_mode = str(payload.get("position_mode") or bound.get("position_mode") or "").strip()
    if position_mode == "fixed_fill_qty_until_exit":
        for field in H74_FIXED_POSITION_REQUIRED_FIELDS:
            value = payload.get(field)
            if value is None or str(value).strip() == "":
                value = bound.get(field)
            if value is None or str(value).strip() == "":
                raise H74ObservationAuthorityError(f"h74_authority_contract_incomplete:{field}")
    structural_runtime_values = {
        **raw_settings_values,
        **effective_behavior_values,
        **{key: value for key, value in bound.items() if key in raw_settings_values or key in effective_behavior_values},
    }
    if authority_type == H74_SOURCE_OBSERVATION_AUTHORITY_ARTIFACT_TYPE:
        verify_h74_source_observation_authority(payload, runtime_values=structural_runtime_values)
    elif authority_type == H74_SOURCE_VARIANT_OBSERVATION_AUTHORITY_ARTIFACT_TYPE:
        verify_h74_source_variant_observation_authority(payload, runtime_values=structural_runtime_values)
    else:
        raise H74ObservationAuthorityError("h74_authority_type_invalid")

    behavior_keys = [key for key in bound if key in effective_behavior_values]
    mismatched = tuple(sorted(key for key in behavior_keys if not _match(effective_behavior_values.get(key), bound.get(key))))
    ok = not mismatched
    result = H74AuthorityEnvAlignment(
        ok=ok,
        reason_code="OK" if ok else H74_AUTHORITY_ENV_BEHAVIOR_MISMATCH,
        authority_type=authority_type,
        mismatched_keys=mismatched,
        raw_settings_parameters={key: raw_settings_values.get(key) for key in sorted(raw_settings_values)},
        effective_behavior_parameters={key: effective_behavior_values.get(key) for key in sorted(effective_behavior_values)},
    )
    if not ok and raise_on_mismatch:
        raise H74ObservationAuthorityError(
            f"{H74_AUTHORITY_ENV_BEHAVIOR_MISMATCH}:" + ",".join(mismatched)
        )
    return result


def validate_h74_authority_file_env_alignment(
    path: str | Path,
    *,
    settings_obj: object,
    raise_on_mismatch: bool = True,
) -> H74AuthorityEnvAlignment:
    return validate_h74_authority_env_alignment(
        load_h74_authority_payload(path),
        settings_obj=settings_obj,
        raise_on_mismatch=raise_on_mismatch,
    )
