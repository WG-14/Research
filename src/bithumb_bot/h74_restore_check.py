from __future__ import annotations

from collections.abc import Mapping

from .h74_authority_alignment import validate_h74_authority_env_alignment
from .h74_observation import (
    H74_SOURCE_OBSERVATION_AUTHORITY_ARTIFACT_TYPE,
    H74_SOURCE_OBSERVATION_PARAMETERS,
    H74ObservationAuthorityError,
    h74_parameter_hash,
    h74_source_runtime_values_from_settings,
)
from .runtime_strategy_set import h74_runtime_adapter_materialized_values_from_settings
from .research.hashing import sha256_prefixed


RESTORE_REQUIRED_KEYS = (
    "DAILY_PARTICIPATION_WINDOW_START_HOUR_KST",
    "DAILY_PARTICIPATION_WINDOW_END_HOUR_KST",
    "SMA_SHORT",
    "SMA_LONG",
    "STRATEGY_EXIT_MAX_HOLDING_MIN",
    "DAILY_PARTICIPATION_MAX_ORDER_KRW",
    "MAX_DAILY_ORDER_COUNT",
)


def verify_h74_restore_original_window(
    *,
    authority_payload: Mapping[str, object],
    settings_obj: object,
    env_hash: str,
) -> dict[str, object]:
    if str(authority_payload.get("authority_type") or authority_payload.get("artifact_type") or "") != H74_SOURCE_OBSERVATION_AUTHORITY_ARTIFACT_TYPE:
        raise H74ObservationAuthorityError("restore_original_window_requires_source_authority")
    alignment = validate_h74_authority_env_alignment(authority_payload, settings_obj=settings_obj)
    effective = {
        **h74_source_runtime_values_from_settings(settings_obj),
        **h74_runtime_adapter_materialized_values_from_settings(settings_obj),
    }
    expected = {key: H74_SOURCE_OBSERVATION_PARAMETERS.get(key) for key in RESTORE_REQUIRED_KEYS}
    expected["MAX_DAILY_ORDER_COUNT"] = H74_SOURCE_OBSERVATION_PARAMETERS.get("max_daily_order_count")
    actual = {key: effective.get(key) for key in RESTORE_REQUIRED_KEYS}
    actual["MAX_DAILY_ORDER_COUNT"] = effective.get("max_daily_order_count")
    mismatched = [key for key in RESTORE_REQUIRED_KEYS if str(actual.get(key)) != str(expected.get(key))]
    if mismatched:
        raise H74ObservationAuthorityError("restore_original_window_behavior_mismatch:" + ",".join(mismatched))
    artifact = {
        "artifact_type": "h74_restore_original_window_check",
        "status": "PASS",
        "source_authority_hash": str(authority_payload.get("authority_content_hash") or ""),
        "env_hash": str(env_hash or ""),
        "effective_behavior_parameters": actual,
        "effective_behavior_parameter_hash": h74_parameter_hash(actual),
        "authority_env_alignment": alignment.as_dict(),
    }
    artifact["restore_check_hash"] = sha256_prefixed(artifact)
    return artifact
