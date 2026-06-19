from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .research.hashing import sha256_prefixed
from .storage_io import write_json_atomic


H74_OBSERVATION_AUTHORITY_ARTIFACT_TYPE = "h74_live_observation_authority"
H74_STRATEGY_NAME = "daily_participation_sma"
H74_SOURCE_CANDIDATE_ID = "candidate_9738b8d6"
H74_SOURCE_MAX_ORDER_KRW = 100_000
H74_OBSERVATION_MAX_ORDER_KRW = 50_000
H74_OBSERVATION_WINDOW_DAYS = 7

def _h74_observation_parameters() -> dict[str, object]:
    from .research.strategy_spec import runtime_bound_behavior_parameter_names
    from .strategy_plugins.daily_participation_sma import DAILY_PARTICIPATION_SMA_SPEC

    parameters = {
        name: DAILY_PARTICIPATION_SMA_SPEC.default_parameters[name]
        for name in runtime_bound_behavior_parameter_names(H74_STRATEGY_NAME)
        if name in DAILY_PARTICIPATION_SMA_SPEC.default_parameters
    }
    parameters.update(
        {
            "SMA_SHORT": 10,
            "SMA_LONG": 86,
            "STRATEGY_EXIT_MAX_HOLDING_MIN": 74,
            "DAILY_PARTICIPATION_ENABLED": True,
            "DAILY_PARTICIPATION_TIMEZONE": "Asia/Seoul",
            "DAILY_PARTICIPATION_COUNT_BASIS": "filled",
            "DAILY_PARTICIPATION_FALLBACK_MODE": "unconditional_participation",
            "DAILY_PARTICIPATION_WINDOW_START_HOUR_KST": 9,
            "DAILY_PARTICIPATION_WINDOW_END_HOUR_KST": 11,
            "DAILY_PARTICIPATION_MAX_ORDER_KRW": H74_OBSERVATION_MAX_ORDER_KRW,
        }
    )
    parameters.update(
        {
            "strategy_name": H74_STRATEGY_NAME,
            "market": "KRW-BTC",
            "interval": "1m",
            "max_daily_order_count": 1,
            "max_notional_krw": H74_OBSERVATION_MAX_ORDER_KRW,
        }
    )
    return parameters


H74_OBSERVATION_PARAMETERS: dict[str, object] = _h74_observation_parameters()


class H74ObservationAuthorityError(ValueError):
    pass


def h74_parameter_hash(parameters: dict[str, object]) -> str:
    return sha256_prefixed(dict(sorted(parameters.items())))


def build_h74_capital_scaled_variant() -> dict[str, Any]:
    source_parameters = dict(H74_OBSERVATION_PARAMETERS)
    source_parameters["DAILY_PARTICIPATION_MAX_ORDER_KRW"] = H74_SOURCE_MAX_ORDER_KRW
    observation_parameters = dict(H74_OBSERVATION_PARAMETERS)
    invariant = sorted(k for k in observation_parameters if observation_parameters[k] == source_parameters[k])
    changed = sorted(k for k in observation_parameters if observation_parameters[k] != source_parameters[k])
    return {
        "artifact_type": "h74_capital_scaled_observation_variant",
        "source_candidate_id": H74_SOURCE_CANDIDATE_ID,
        "source_candidate_parameter_hash": h74_parameter_hash(source_parameters),
        "source_authority_bound_parameter_hash": h74_parameter_hash(source_parameters),
        "source_daily_max_order_krw": H74_SOURCE_MAX_ORDER_KRW,
        "observation_daily_max_order_krw": H74_OBSERVATION_MAX_ORDER_KRW,
        "capital_scaling_ratio": 0.5,
        "invariant_parameters": invariant,
        "changed_parameters": changed,
        "not_same_candidate": True,
        "observation_parameter_hash": h74_parameter_hash(observation_parameters),
        "observation_authority_bound_parameter_hash": h74_parameter_hash(observation_parameters),
        "source_backtest_pnl": None,
        "live_observed_pnl": None,
    }


def build_h74_observation_authority_payload(
    *,
    expires_at: datetime | None = None,
    max_daily_order_count: int = 1,
    max_notional_krw: float = H74_OBSERVATION_MAX_ORDER_KRW,
) -> dict[str, Any]:
    expiry = expires_at or (datetime.now(timezone.utc) + timedelta(days=H74_OBSERVATION_WINDOW_DAYS))
    variant = build_h74_capital_scaled_variant()
    from .research.strategy_spec import runtime_bound_behavior_parameter_names

    required_behavior_parameters = set(runtime_bound_behavior_parameter_names(H74_STRATEGY_NAME))
    missing = sorted(required_behavior_parameters - set(H74_OBSERVATION_PARAMETERS))
    if missing:
        raise H74ObservationAuthorityError(
            "h74_observation_authority_missing_behavior_parameters:" + ",".join(missing)
        )
    hash_bound = {
        **{
            k: H74_OBSERVATION_PARAMETERS[k]
            for k in sorted(required_behavior_parameters | {"strategy_name", "market", "interval"})
        },
        "max_daily_order_count": int(max_daily_order_count),
        "max_notional_krw": float(max_notional_krw),
        "expires_at": expiry.astimezone(timezone.utc).isoformat(),
        "observation_window_days": H74_OBSERVATION_WINDOW_DAYS,
        "source_candidate_id": H74_SOURCE_CANDIDATE_ID,
        "source_candidate_max_order_krw": H74_SOURCE_MAX_ORDER_KRW,
        "capital_scaling_policy": {
            "ratio": 0.5,
            "not_same_candidate": True,
            "changed_parameters": ["DAILY_PARTICIPATION_MAX_ORDER_KRW"],
        },
    }
    payload: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": H74_OBSERVATION_AUTHORITY_ARTIFACT_TYPE,
        "promotion_grade": False,
        "research_promotion_evidence": False,
        "approved_profile_evidence": False,
        "hash_bound_parameters": hash_bound,
        "runtime_bound_behavior_parameter_names": sorted(required_behavior_parameters),
        "capital_scaled_variant": variant,
        "authority_parameter_hash": sha256_prefixed(hash_bound),
    }
    payload["authority_content_hash"] = sha256_prefixed(
        {k: v for k, v in payload.items() if k != "authority_content_hash"}
    )
    return payload


def verify_h74_observation_authority(
    payload: dict[str, Any],
    *,
    runtime_values: dict[str, object],
    now: datetime | None = None,
) -> None:
    if str(payload.get("artifact_type") or "") != H74_OBSERVATION_AUTHORITY_ARTIFACT_TYPE:
        raise H74ObservationAuthorityError("h74_observation_authority_artifact_type_invalid")
    if bool(payload.get("promotion_grade")) or bool(payload.get("research_promotion_evidence")):
        raise H74ObservationAuthorityError("h74_observation_authority_not_promotion_profile")
    expected_hash = str(payload.get("authority_content_hash") or "")
    actual_hash = sha256_prefixed({k: v for k, v in payload.items() if k != "authority_content_hash"})
    if expected_hash != actual_hash:
        raise H74ObservationAuthorityError("h74_observation_authority_hash_mismatch")
    bound = dict(payload.get("hash_bound_parameters") or {})
    from .research.strategy_spec import runtime_bound_behavior_parameter_names

    required_behavior_parameters = set(runtime_bound_behavior_parameter_names(H74_STRATEGY_NAME))
    missing_bound = sorted(required_behavior_parameters - set(bound))
    if missing_bound:
        raise H74ObservationAuthorityError(
            "h74_observation_authority_missing_behavior_parameters:" + ",".join(missing_bound)
        )
    for key, expected in bound.items():
        if key in {"expires_at", "capital_scaling_policy", "observation_window_days", "source_candidate_id", "source_candidate_max_order_krw"}:
            continue
        actual = runtime_values.get(key)
        if key in {"max_notional_krw", "DAILY_PARTICIPATION_MAX_ORDER_KRW"}:
            try:
                matched = float(actual) == float(expected)
            except (TypeError, ValueError):
                matched = False
        else:
            matched = str(actual) == str(expected)
        if not matched:
            raise H74ObservationAuthorityError(f"h74_observation_authority_runtime_mismatch:{key}")
    expires_at = datetime.fromisoformat(str(bound.get("expires_at")).replace("Z", "+00:00"))
    if expires_at <= (now or datetime.now(timezone.utc)).astimezone(timezone.utc):
        raise H74ObservationAuthorityError("h74_observation_authority_expired")
    if int(bound.get("DAILY_PARTICIPATION_WINDOW_START_HOUR_KST")) != 9:
        raise H74ObservationAuthorityError("h74_observation_authority_window_start_invalid")
    if int(bound.get("DAILY_PARTICIPATION_WINDOW_END_HOUR_KST")) != 11:
        raise H74ObservationAuthorityError("h74_observation_authority_window_end_invalid")
    if int(bound.get("STRATEGY_EXIT_MAX_HOLDING_MIN")) != 74:
        raise H74ObservationAuthorityError("h74_observation_authority_holding_invalid")


def h74_runtime_values_from_settings(settings_obj: object) -> dict[str, object]:
    from .research.strategy_spec import runtime_bound_behavior_parameter_names

    values = {
        "strategy_name": str(getattr(settings_obj, "STRATEGY_NAME", H74_STRATEGY_NAME) or H74_STRATEGY_NAME),
        "market": str(getattr(settings_obj, "PAIR", "KRW-BTC") or "KRW-BTC"),
        "interval": str(getattr(settings_obj, "INTERVAL", "1m") or "1m"),
        "max_daily_order_count": int(getattr(settings_obj, "MAX_DAILY_ORDER_COUNT", 1) or 1),
        "max_notional_krw": float(getattr(settings_obj, "DAILY_PARTICIPATION_MAX_ORDER_KRW", H74_OBSERVATION_MAX_ORDER_KRW) or H74_OBSERVATION_MAX_ORDER_KRW),
    }
    for name in runtime_bound_behavior_parameter_names(H74_STRATEGY_NAME):
        fallback = H74_OBSERVATION_PARAMETERS.get(name)
        values[name] = getattr(settings_obj, name, fallback)
    return values


def verify_h74_observation_authority_file(path: str | Path, *, settings_obj: object) -> None:
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise H74ObservationAuthorityError("h74_observation_authority_payload_not_object")
    verify_h74_observation_authority(payload, runtime_values=h74_runtime_values_from_settings(settings_obj))


def cmd_h74_observation_authority_generate(*, out_path: str | None = None) -> int:
    payload = build_h74_observation_authority_payload()
    if out_path:
        write_json_atomic(Path(out_path).expanduser(), payload)
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return 0


def cmd_h74_observation_authority_verify(*, authority_path: str) -> int:
    with Path(authority_path).expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    verify_h74_observation_authority(payload, runtime_values=H74_OBSERVATION_PARAMETERS)
    print(json.dumps({"ok": True, "authority_path": str(authority_path)}, sort_keys=True))
    return 0
