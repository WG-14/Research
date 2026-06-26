from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .research.hashing import sha256_prefixed
from .storage_io import write_json_atomic
from .strategy_risk_profile import risk_policy_from_mapping
from .experiment_execution_contract import POSITION_MODE_FIXED_FILL_QTY_UNTIL_EXIT
from .h74_submit_semantics import (
    H74_ENTRY_SUBMIT_SEMANTICS,
    H74_ENTRY_SUBMIT_SEMANTICS_AUTHORITY,
    H74_ENTRY_SUBMIT_SEMANTICS_NAME,
    H74_SOURCE_MAX_ORDER_KRW,
)


H74_OBSERVATION_AUTHORITY_ARTIFACT_TYPE = "h74_live_observation_authority"
H74_SOURCE_OBSERVATION_AUTHORITY_ARTIFACT_TYPE = "h74_source_live_observation_authority"
H74_SOURCE_VARIANT_OBSERVATION_AUTHORITY_ARTIFACT_TYPE = "h74_source_variant_live_probe_authority"
H74_SOURCE_OBSERVATION_AUTHORITY_ENV = "H74_SOURCE_OBSERVATION_AUTHORITY_PATH"
H74_SOURCE_OBSERVATION_SMOKE_EVIDENCE_ENV = "H74_SOURCE_OBSERVATION_LIVE_PIPELINE_SMOKE_EVIDENCE_PATH"
H74_STRATEGY_NAME = "daily_participation_sma"
H74_SOURCE_CANDIDATE_ID = "candidate_9738b8d6"
H74_OBSERVATION_MAX_ORDER_KRW = 50_000
H74_OBSERVATION_WINDOW_DAYS = 7
H74_SOURCE_OBSERVATION_MAX_DAILY_ENTRY_COUNT = 1
H74_SOURCE_OBSERVATION_MAX_DAILY_TOTAL_ORDER_COUNT = 2
H74_SOURCE_OBSERVATION_RISK_POLICY_SOURCE = H74_SOURCE_OBSERVATION_AUTHORITY_ARTIFACT_TYPE
H74_SOURCE_OBSERVATION_MAX_DAILY_LOSS_KRW = 5_000.0
H74_SOURCE_OBSERVATION_MAX_POSITION_LOSS_PCT = 0.03
H74_SOURCE_OBSERVATION_RISK_CAPITAL_BASIS = "fixed_observation_notional"
H74_SOURCE_OBSERVATION_RISK_CAPITAL_KRW = float(H74_SOURCE_MAX_ORDER_KRW)
H74_POSITION_MODE = POSITION_MODE_FIXED_FILL_QTY_UNTIL_EXIT

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


def _h74_source_observation_parameters() -> dict[str, object]:
    parameters = dict(H74_OBSERVATION_PARAMETERS)
    parameters.update(
        {
            "SMA_FILTER_GAP_MIN_RATIO": 0.0002,
            "SMA_FILTER_VOL_MIN_RANGE_RATIO": 0.001,
            "SMA_FILTER_OVEREXT_LOOKBACK": 5,
            "SMA_FILTER_OVEREXT_MAX_RETURN_RATIO": 0.01,
            "ENTRY_EDGE_BUFFER_RATIO": 0.0,
            "STRATEGY_EXIT_RULES": "max_holding_time",
            "STRATEGY_EXIT_MIN_TAKE_PROFIT_RATIO": 0.0008,
            "STRATEGY_EXIT_SMALL_LOSS_TOLERANCE_RATIO": 0.0005,
            "DAILY_PARTICIPATION_BUY_FRACTION": 1.0,
            "DAILY_PARTICIPATION_MAX_ORDER_KRW": H74_SOURCE_MAX_ORDER_KRW,
            "max_daily_entry_count": H74_SOURCE_OBSERVATION_MAX_DAILY_ENTRY_COUNT,
            "max_daily_total_order_count": H74_SOURCE_OBSERVATION_MAX_DAILY_TOTAL_ORDER_COUNT,
            "max_daily_order_count": H74_SOURCE_OBSERVATION_MAX_DAILY_TOTAL_ORDER_COUNT,
            "daily_participation_count_scope": "strategy_instance",
            "strategy_instance_id": "h74-source-observation",
            "daily_order_count_scope": "account_global",
            "max_entry_notional_krw": H74_SOURCE_MAX_ORDER_KRW,
            "max_notional_krw": H74_SOURCE_MAX_ORDER_KRW,
            "risk_capital_basis": H74_SOURCE_OBSERVATION_RISK_CAPITAL_BASIS,
            "risk_capital_krw": H74_SOURCE_OBSERVATION_RISK_CAPITAL_KRW,
            "exit_closeout_not_blocked_by_entry_cap": True,
            "position_mode": H74_POSITION_MODE,
            "hold_policy": "hold_acquired_fill_qty_until_max_holding_exit",
            "residual_inventory_mode": "terminal_dust_reported_not_reused_without_authority",
            "initial_position_policy": "flat_start_required",
            "partial_fill_policy": "accumulate_cycle_acquired_qty",
            "fee_application_policy": "repository_observed_fee_fields",
            "entry_submit_semantics": dict(H74_ENTRY_SUBMIT_SEMANTICS),
            "entry_submit_semantics_name": H74_ENTRY_SUBMIT_SEMANTICS_NAME,
        }
    )
    return parameters


H74_SOURCE_OBSERVATION_PARAMETERS: dict[str, object] = _h74_source_observation_parameters()


class H74ObservationAuthorityError(ValueError):
    pass


def h74_parameter_hash(parameters: dict[str, object]) -> str:
    return sha256_prefixed(dict(sorted(parameters.items())))


def h74_source_observation_risk_policy() -> dict[str, object]:
    return {
        "schema_version": 1,
        "policy_status": "enabled",
        "missing_policy": "fail_closed_for_live",
        "source": H74_SOURCE_OBSERVATION_RISK_POLICY_SOURCE,
        "max_daily_loss_krw": H74_SOURCE_OBSERVATION_MAX_DAILY_LOSS_KRW,
        "max_position_loss_pct": H74_SOURCE_OBSERVATION_MAX_POSITION_LOSS_PCT,
        "max_daily_order_count": H74_SOURCE_OBSERVATION_MAX_DAILY_TOTAL_ORDER_COUNT,
        "max_trade_count_per_day": H74_SOURCE_OBSERVATION_MAX_DAILY_TOTAL_ORDER_COUNT,
        "max_drawdown_pct": H74_SOURCE_OBSERVATION_MAX_POSITION_LOSS_PCT,
        "cooldown_after_loss_min": 0,
        "max_open_positions": 1,
        "unresolved_order_policy": "block",
        "kill_switch": False,
    }


def h74_source_observation_risk_policy_hash(policy: dict[str, object] | None = None) -> str:
    return risk_policy_from_mapping(policy or h74_source_observation_risk_policy()).policy_hash()


def build_h74_observation_experiment_envelope(
    *,
    experiment_run_id: str,
    runtime_git_commit_sha: str,
    runtime_git_diff_hash: str = "",
    runtime_git_clean: bool = False,
    env_hash: str,
    strategy_revision_id: str,
    risk_scope_id: str,
    risk_baseline_certificate_hash: str,
    starting_broker_position: dict[str, object],
    starting_local_position: dict[str, object],
    db_snapshot_hash: str = "",
    db_snapshot_locator: str = "",
    included_history_policy: str,
) -> dict[str, object]:
    payload = {
        "schema_version": 1,
        "artifact_type": "h74_observation_experiment_envelope",
        "experiment_run_id": str(experiment_run_id or ""),
        "runtime_git_commit_sha": str(runtime_git_commit_sha or ""),
        "runtime_git_diff_hash": str(runtime_git_diff_hash or ""),
        "runtime_git_clean": bool(runtime_git_clean),
        "env_hash": str(env_hash or ""),
        "strategy_revision_id": str(strategy_revision_id or ""),
        "risk_scope_id": str(risk_scope_id or ""),
        "risk_capital_basis": H74_SOURCE_OBSERVATION_RISK_CAPITAL_BASIS,
        "risk_capital_krw": H74_SOURCE_OBSERVATION_RISK_CAPITAL_KRW,
        "risk_baseline_certificate_hash": str(risk_baseline_certificate_hash or ""),
        "starting_broker_position": dict(starting_broker_position or {}),
        "starting_local_position": dict(starting_local_position or {}),
        "db_snapshot_hash": str(db_snapshot_hash or ""),
        "db_snapshot_locator": str(db_snapshot_locator or ""),
        "included_history_policy": str(included_history_policy or ""),
    }
    missing = [
        key
        for key in (
            "experiment_run_id",
            "runtime_git_commit_sha",
            "env_hash",
            "strategy_revision_id",
            "risk_scope_id",
            "risk_baseline_certificate_hash",
            "included_history_policy",
        )
        if not payload[key]
    ]
    if not payload["runtime_git_clean"] and not payload["runtime_git_diff_hash"]:
        missing.append("runtime_git_diff_hash")
    if not payload["db_snapshot_hash"] and not payload["db_snapshot_locator"]:
        missing.append("db_snapshot_hash_or_locator")
    if missing:
        raise H74ObservationAuthorityError("h74_observation_experiment_envelope_missing:" + ",".join(missing))
    payload["experiment_envelope_hash"] = sha256_prefixed(
        {key: value for key, value in payload.items() if key != "experiment_envelope_hash"}
    )
    return payload


def verify_h74_observation_experiment_envelope(payload: dict[str, Any]) -> None:
    if str(payload.get("artifact_type") or "") != "h74_observation_experiment_envelope":
        raise H74ObservationAuthorityError("h74_observation_experiment_envelope_type_invalid")
    expected = str(payload.get("experiment_envelope_hash") or "")
    actual = sha256_prefixed({key: value for key, value in payload.items() if key != "experiment_envelope_hash"})
    if expected != actual:
        raise H74ObservationAuthorityError("h74_observation_experiment_envelope_hash_mismatch")


def _required_h74_source_envelope_fields(envelope: Mapping[str, object]) -> dict[str, object]:
    verify_h74_observation_experiment_envelope(dict(envelope))
    required = {
        "experiment_run_id": str(envelope.get("experiment_run_id") or "").strip(),
        "env_hash": str(envelope.get("env_hash") or "").strip(),
        "strategy_revision_id": str(envelope.get("strategy_revision_id") or "").strip(),
        "risk_scope_id": str(envelope.get("risk_scope_id") or "").strip(),
        "starting_broker_position": dict(envelope.get("starting_broker_position") or {}),
        "starting_local_position": dict(envelope.get("starting_local_position") or {}),
        "db_snapshot_hash": str(envelope.get("db_snapshot_hash") or "").strip(),
        "db_snapshot_locator": str(envelope.get("db_snapshot_locator") or "").strip(),
        "included_history_policy": str(envelope.get("included_history_policy") or "").strip(),
        "experiment_envelope_hash": str(envelope.get("experiment_envelope_hash") or "").strip(),
        "risk_baseline_certificate_hash": str(envelope.get("risk_baseline_certificate_hash") or "").strip(),
    }
    missing = [
        key
        for key in (
            "experiment_run_id",
            "env_hash",
            "strategy_revision_id",
            "risk_scope_id",
            "included_history_policy",
            "experiment_envelope_hash",
            "risk_baseline_certificate_hash",
        )
        if not required[key]
    ]
    if not required["starting_broker_position"]:
        missing.append("starting_broker_position")
    if not required["starting_local_position"]:
        missing.append("starting_local_position")
    if not required["db_snapshot_hash"] and not required["db_snapshot_locator"]:
        missing.append("db_snapshot_hash_or_locator")
    if missing:
        raise H74ObservationAuthorityError(
            "h74_source_observation_authority_experiment_envelope_missing:" + ",".join(missing)
        )
    return required


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


def build_h74_source_observation_authority_payload(
    *,
    expires_at: datetime | None = None,
    source_candidate_artifact_hash: str,
    backtest_report_hash: str | None = None,
    validation_run_hash: str | None = None,
    code_commit_sha: str | None = None,
    experiment_envelope_hash: str | None = None,
    experiment_envelope_payload: Mapping[str, object] | None = None,
    experiment_envelope_locator: str | None = None,
    risk_baseline_certificate_hash: str | None = None,
    included_history_policy: str | None = None,
    db_snapshot_hash: str | None = None,
) -> dict[str, Any]:
    expiry = expires_at or (datetime.now(timezone.utc) + timedelta(days=H74_OBSERVATION_WINDOW_DAYS))
    from .config import runtime_code_provenance
    from .research.strategy_spec import runtime_bound_behavior_parameter_names

    required_behavior_parameters = set(runtime_bound_behavior_parameter_names(H74_STRATEGY_NAME))
    missing = sorted(required_behavior_parameters - set(H74_SOURCE_OBSERVATION_PARAMETERS))
    if missing:
        raise H74ObservationAuthorityError(
            "h74_source_observation_authority_missing_behavior_parameters:" + ",".join(missing)
        )
    risk_policy = h74_source_observation_risk_policy()
    risk_policy_hash = h74_source_observation_risk_policy_hash(risk_policy)
    commit = str(code_commit_sha or runtime_code_provenance().get("commit_sha") or "unavailable")
    if experiment_envelope_payload is None:
        raise H74ObservationAuthorityError(
            "h74_source_observation_authority_experiment_envelope_payload_required"
        )
    envelope_fields = _required_h74_source_envelope_fields(experiment_envelope_payload)
    envelope_hash = str(envelope_fields["experiment_envelope_hash"])
    if experiment_envelope_hash is not None and str(experiment_envelope_hash or "").strip() != envelope_hash:
        raise H74ObservationAuthorityError("h74_source_observation_authority_experiment_envelope_hash_mismatch")
    baseline_hash = str(envelope_fields["risk_baseline_certificate_hash"])
    if risk_baseline_certificate_hash is not None and str(risk_baseline_certificate_hash or "").strip() != baseline_hash:
        raise H74ObservationAuthorityError("h74_source_observation_authority_risk_baseline_hash_mismatch")
    history_policy = str(envelope_fields["included_history_policy"])
    if included_history_policy is not None and str(included_history_policy or "").strip() != history_policy:
        raise H74ObservationAuthorityError("h74_source_observation_authority_included_history_policy_mismatch")
    snapshot_hash = str(envelope_fields["db_snapshot_hash"])
    if db_snapshot_hash is not None and str(db_snapshot_hash or "").strip() != snapshot_hash:
        raise H74ObservationAuthorityError("h74_source_observation_authority_db_snapshot_hash_mismatch")
    snapshot_locator = str(envelope_fields["db_snapshot_locator"])
    envelope_summary = {
        key: envelope_fields[key]
        for key in (
            "experiment_run_id",
            "env_hash",
            "strategy_revision_id",
            "risk_scope_id",
            "starting_broker_position",
            "starting_local_position",
            "db_snapshot_hash",
            "db_snapshot_locator",
            "included_history_policy",
            "experiment_envelope_hash",
            "risk_baseline_certificate_hash",
        )
    }
    hash_bound = {
        **{
            k: H74_SOURCE_OBSERVATION_PARAMETERS[k]
            for k in sorted(required_behavior_parameters | {"strategy_name", "market", "interval"})
        },
        "candidate_id": H74_SOURCE_CANDIDATE_ID,
        "source_candidate_artifact_hash": str(source_candidate_artifact_hash or "").strip(),
        "backtest_report_hash": str(backtest_report_hash or "").strip() or None,
        "validation_run_hash": str(validation_run_hash or "").strip() or None,
        "max_entry_notional_krw": H74_SOURCE_MAX_ORDER_KRW,
        "max_daily_entry_count": H74_SOURCE_OBSERVATION_MAX_DAILY_ENTRY_COUNT,
        "max_daily_total_order_count": H74_SOURCE_OBSERVATION_MAX_DAILY_TOTAL_ORDER_COUNT,
        "daily_participation_count_scope": "strategy_instance",
        "strategy_instance_id": "h74-source-observation",
        "daily_order_count_scope": "account_global",
        "exit_closeout_not_blocked_by_entry_cap": True,
        "expires_at": expiry.astimezone(timezone.utc).isoformat(),
        "observation_window_days": H74_OBSERVATION_WINDOW_DAYS,
        "market": "KRW-BTC",
        "interval": "1m",
        "code_commit_sha": commit,
        "production_approval": False,
        "approved_profile_evidence": False,
        "risk_policy_hash": risk_policy_hash,
        "experiment_run_id": str(envelope_fields["experiment_run_id"]),
        "env_hash": str(envelope_fields["env_hash"]),
        "strategy_revision_id": str(envelope_fields["strategy_revision_id"]),
        "risk_scope_id": str(envelope_fields["risk_scope_id"]),
        "starting_broker_position": dict(envelope_fields["starting_broker_position"]),
        "starting_local_position": dict(envelope_fields["starting_local_position"]),
        "experiment_envelope_hash": envelope_hash,
        "experiment_envelope_locator": str(experiment_envelope_locator or ""),
        "risk_baseline_certificate_hash": baseline_hash,
        "included_history_policy": history_policy,
        "db_snapshot_hash": snapshot_hash,
        "db_snapshot_locator": snapshot_locator,
        "risk_capital_basis": H74_SOURCE_OBSERVATION_RISK_CAPITAL_BASIS,
        "risk_capital_krw": H74_SOURCE_OBSERVATION_RISK_CAPITAL_KRW,
        "position_mode": H74_POSITION_MODE,
        "entry_submit_semantics": dict(H74_ENTRY_SUBMIT_SEMANTICS),
        "entry_submit_semantics_name": H74_ENTRY_SUBMIT_SEMANTICS_NAME,
        "submit_semantics_hash": sha256_prefixed(H74_ENTRY_SUBMIT_SEMANTICS),
        "hold_policy": "hold_acquired_fill_qty_until_max_holding_exit",
        "residual_inventory_mode": "terminal_dust_reported_not_reused_without_authority",
        "initial_position_policy": "flat_start_required",
        "partial_fill_policy": "accumulate_cycle_acquired_qty",
        "fee_application_policy": "repository_observed_fee_fields",
    }
    if not hash_bound["source_candidate_artifact_hash"]:
        raise H74ObservationAuthorityError("h74_source_observation_authority_source_hash_missing")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": H74_SOURCE_OBSERVATION_AUTHORITY_ARTIFACT_TYPE,
        "authority_type": H74_SOURCE_OBSERVATION_AUTHORITY_ARTIFACT_TYPE,
        "candidate_id": H74_SOURCE_CANDIDATE_ID,
        "promotion_grade": False,
        "research_promotion_evidence": False,
        "approved_profile_evidence": False,
        "production_approval": False,
        "contract_scope": "h74_source_live_observation_only",
        "hash_bound_parameters": hash_bound,
        "risk_policy": risk_policy,
        "risk_policy_hash": risk_policy_hash,
        "experiment_run_id": str(envelope_fields["experiment_run_id"]),
        "env_hash": str(envelope_fields["env_hash"]),
        "strategy_revision_id": str(envelope_fields["strategy_revision_id"]),
        "risk_scope_id": str(envelope_fields["risk_scope_id"]),
        "starting_broker_position": dict(envelope_fields["starting_broker_position"]),
        "starting_local_position": dict(envelope_fields["starting_local_position"]),
        "experiment_envelope_hash": envelope_hash,
        "experiment_envelope_locator": str(experiment_envelope_locator or ""),
        "experiment_envelope_summary": envelope_summary,
        "risk_baseline_certificate_hash": baseline_hash,
        "included_history_policy": history_policy,
        "db_snapshot_hash": snapshot_hash,
        "db_snapshot_locator": snapshot_locator,
        "risk_profile_source": H74_SOURCE_OBSERVATION_RISK_POLICY_SOURCE,
        "risk_enforcement_mode": "enforced",
        "position_mode": H74_POSITION_MODE,
        "strategy_instance_id": "h74-source-observation",
        "runtime_bound_behavior_parameter_names": sorted(required_behavior_parameters),
        "authority_parameter_hash": sha256_prefixed(hash_bound),
    }
    payload["authority_content_hash"] = sha256_prefixed(
        {k: v for k, v in payload.items() if k != "authority_content_hash"}
    )
    return payload


def build_h74_source_variant_observation_authority_payload(
    *,
    base_authority: Mapping[str, object],
    variant_overrides: Mapping[str, object],
    experiment_envelope_payload: Mapping[str, object],
    variant_id: str = "h74_no_window_execution_path_probe_v1",
    variant_reason: str = "remove_entry_window_for_buy_sell_execution_path_probe",
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    from .h74_variant_contract import allowed_variant_override_keys, validate_h74_variant_overrides

    verify_h74_source_observation_authority(
        dict(base_authority),
        runtime_values=H74_SOURCE_OBSERVATION_PARAMETERS,
    )
    overrides = validate_h74_variant_overrides(variant_overrides)
    start = int(overrides.get("DAILY_PARTICIPATION_WINDOW_START_HOUR_KST", -1))
    end = int(overrides.get("DAILY_PARTICIPATION_WINDOW_END_HOUR_KST", -1))
    if (start, end) != (0, 24):
        raise H74ObservationAuthorityError("h74_source_variant_authority_no_window_override_required")
    variant_id_text = str(variant_id or "").strip()
    if not variant_id_text:
        raise H74ObservationAuthorityError("h74_source_variant_authority_variant_id_missing")
    envelope_fields = _required_h74_source_envelope_fields(experiment_envelope_payload)
    base_bound = dict(base_authority.get("hash_bound_parameters") or {})
    variant_bound = {**base_bound, **overrides}
    expiry = expires_at or (datetime.now(timezone.utc) + timedelta(days=H74_OBSERVATION_WINDOW_DAYS))
    variant_bound["expires_at"] = expiry.astimezone(timezone.utc).isoformat()
    variant_bound["base_candidate_id"] = H74_SOURCE_CANDIDATE_ID
    variant_bound["variant_id"] = variant_id_text
    variant_bound["variant_overrides"] = dict(overrides)
    variant_bound["probe_scope"] = "buy_sell_path_only"
    base_behavior_parameter_hash = h74_parameter_hash(base_bound)
    variant_behavior_parameter_hash = h74_parameter_hash(variant_bound)
    equivalent = base_behavior_parameter_hash == variant_behavior_parameter_hash
    if equivalent:
        raise H74ObservationAuthorityError("h74_source_variant_authority_behavior_hash_unchanged")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": H74_SOURCE_VARIANT_OBSERVATION_AUTHORITY_ARTIFACT_TYPE,
        "authority_type": H74_SOURCE_VARIANT_OBSERVATION_AUTHORITY_ARTIFACT_TYPE,
        "base_candidate_id": H74_SOURCE_CANDIDATE_ID,
        "candidate_id": None,
        "base_source_authority_hash": str(base_authority.get("authority_content_hash") or "").strip(),
        "variant_id": variant_id_text,
        "variant_reason": str(variant_reason or "").strip(),
        "variant_overrides": dict(overrides),
        "allowed_variant_override_keys": list(allowed_variant_override_keys()),
        "equivalence_to_source_candidate": False,
        "production_approval": False,
        "research_promotion_evidence": False,
        "research_promotion_evidence_status": False,
        "promotion_grade": False,
        "approved_profile_evidence": False,
        "research_equivalence_status": "NOT_APPLICABLE",
        "acceptance_track": "execution_path_probe",
        "probe_scope": "buy_sell_path_only",
        "contract_scope": "h74_source_variant_live_probe_buy_sell_path_only",
        "hash_bound_parameters": variant_bound,
        "strategy_instance_id": str(variant_bound.get("strategy_instance_id") or ""),
        "position_mode": str(variant_bound.get("position_mode") or ""),
        "hold_policy": str(variant_bound.get("hold_policy") or ""),
        "partial_fill_policy": str(variant_bound.get("partial_fill_policy") or ""),
        "base_behavior_parameter_hash": base_behavior_parameter_hash,
        "variant_behavior_parameter_hash": variant_behavior_parameter_hash,
        "risk_policy": dict(base_authority.get("risk_policy") or {}),
        "risk_policy_hash": str(base_authority.get("risk_policy_hash") or "").strip(),
        "runtime_bound_behavior_parameter_names": list(base_authority.get("runtime_bound_behavior_parameter_names") or []),
        "experiment_run_id": str(envelope_fields["experiment_run_id"]),
        "env_hash": str(envelope_fields["env_hash"]),
        "experiment_envelope_hash": str(envelope_fields["experiment_envelope_hash"]),
        "db_snapshot_hash": str(envelope_fields["db_snapshot_hash"]),
        "db_snapshot_locator": str(envelope_fields["db_snapshot_locator"]),
        "authority_parameter_hash": sha256_prefixed(variant_bound),
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


def _values_match(key: str, actual: object, expected: object) -> bool:
    if key in {
        "max_notional_krw",
        "max_entry_notional_krw",
        "DAILY_PARTICIPATION_MAX_ORDER_KRW",
        "DAILY_PARTICIPATION_BUY_FRACTION",
        "SMA_FILTER_GAP_MIN_RATIO",
        "SMA_FILTER_VOL_MIN_RANGE_RATIO",
        "SMA_FILTER_OVEREXT_MAX_RETURN_RATIO",
        "ENTRY_EDGE_BUFFER_RATIO",
        "SMA_COST_EDGE_MIN_RATIO",
        "STRATEGY_MIN_EXPECTED_EDGE_RATIO",
        "STRATEGY_ENTRY_SLIPPAGE_BPS",
        "LIVE_FEE_RATE_ESTIMATE",
        "STRATEGY_EXIT_STOP_LOSS_RATIO",
        "STRATEGY_EXIT_MIN_TAKE_PROFIT_RATIO",
        "STRATEGY_EXIT_SMALL_LOSS_TOLERANCE_RATIO",
    }:
        try:
            return float(actual) == float(expected)
        except (TypeError, ValueError):
            return False
    if isinstance(expected, bool):
        if isinstance(actual, bool):
            return actual is expected
        return (str(actual).strip().lower() in {"1", "true", "yes", "on"}) is expected
    return str(actual) == str(expected)


def verify_h74_source_observation_authority(
    payload: dict[str, Any],
    *,
    runtime_values: dict[str, object],
    now: datetime | None = None,
) -> None:
    if str(payload.get("artifact_type") or "") != H74_SOURCE_OBSERVATION_AUTHORITY_ARTIFACT_TYPE:
        raise H74ObservationAuthorityError("h74_source_observation_authority_artifact_type_invalid")
    if str(payload.get("candidate_id") or "") != H74_SOURCE_CANDIDATE_ID:
        raise H74ObservationAuthorityError("h74_source_observation_authority_candidate_id_invalid")
    for key in ("promotion_grade", "research_promotion_evidence", "approved_profile_evidence", "production_approval"):
        if bool(payload.get(key)) is not False:
            raise H74ObservationAuthorityError(f"h74_source_observation_authority_{key}_must_be_false")
    expected_hash = str(payload.get("authority_content_hash") or "")
    actual_hash = sha256_prefixed({k: v for k, v in payload.items() if k != "authority_content_hash"})
    if expected_hash != actual_hash:
        raise H74ObservationAuthorityError("h74_source_observation_authority_hash_mismatch")
    bound = dict(payload.get("hash_bound_parameters") or {})
    risk_policy_payload = payload.get("risk_policy")
    if not isinstance(risk_policy_payload, dict):
        raise H74ObservationAuthorityError("h74_source_observation_authority_risk_policy_missing")
    risk_policy_hash = str(payload.get("risk_policy_hash") or "").strip()
    if not risk_policy_hash:
        raise H74ObservationAuthorityError("h74_source_observation_authority_risk_policy_hash_missing")
    actual_risk_policy_hash = h74_source_observation_risk_policy_hash(risk_policy_payload)
    if risk_policy_hash != actual_risk_policy_hash:
        raise H74ObservationAuthorityError("h74_source_observation_authority_risk_policy_hash_mismatch")
    bound_risk_policy_hash = str(bound.get("risk_policy_hash") or "").strip()
    if bound_risk_policy_hash != risk_policy_hash:
        raise H74ObservationAuthorityError("h74_source_observation_authority_bound_risk_policy_hash_mismatch")
    _verify_h74_source_observation_risk_policy(risk_policy_payload)
    from .research.strategy_spec import runtime_bound_behavior_parameter_names

    required_behavior_parameters = set(runtime_bound_behavior_parameter_names(H74_STRATEGY_NAME))
    missing_bound = sorted(required_behavior_parameters - set(bound))
    if missing_bound:
        raise H74ObservationAuthorityError(
            "h74_source_observation_authority_missing_behavior_parameters:" + ",".join(missing_bound)
        )
    for required_key in (
        "candidate_id",
        "source_candidate_artifact_hash",
        "max_entry_notional_krw",
        "max_daily_entry_count",
        "max_daily_total_order_count",
        "observation_window_days",
        "expires_at",
        "market",
        "interval",
        "code_commit_sha",
        "risk_policy_hash",
        "experiment_run_id",
        "env_hash",
        "strategy_revision_id",
        "risk_scope_id",
        "starting_broker_position",
        "starting_local_position",
        "experiment_envelope_hash",
        "experiment_envelope_locator",
        "risk_baseline_certificate_hash",
        "included_history_policy",
        "db_snapshot_hash",
        "db_snapshot_locator",
    ):
        if required_key not in bound or bound.get(required_key) in (None, ""):
            if required_key not in {"experiment_envelope_locator", "db_snapshot_hash", "db_snapshot_locator"}:
                raise H74ObservationAuthorityError(
                    f"h74_source_observation_authority_required_field_missing:{required_key}"
                )
    if not bound.get("db_snapshot_hash") and not bound.get("db_snapshot_locator"):
        raise H74ObservationAuthorityError(
            "h74_source_observation_authority_required_field_missing:db_snapshot_hash_or_locator"
        )
    for required_key in (
        "experiment_run_id",
        "env_hash",
        "strategy_revision_id",
        "risk_scope_id",
        "experiment_envelope_hash",
        "risk_baseline_certificate_hash",
        "included_history_policy",
    ):
        if payload.get(required_key) in (None, ""):
            raise H74ObservationAuthorityError(
                f"h74_source_observation_authority_required_field_missing:{required_key}"
            )
        if str(payload.get(required_key)) != str(bound.get(required_key)):
            raise H74ObservationAuthorityError(
                f"h74_source_observation_authority_envelope_field_mismatch:{required_key}"
            )
    for required_key in ("starting_broker_position", "starting_local_position"):
        if not payload.get(required_key):
            raise H74ObservationAuthorityError(
                f"h74_source_observation_authority_required_field_missing:{required_key}"
            )
        if payload.get(required_key) != bound.get(required_key):
            raise H74ObservationAuthorityError(
                f"h74_source_observation_authority_envelope_field_mismatch:{required_key}"
            )
    if str(payload.get("db_snapshot_hash") or "") != str(bound.get("db_snapshot_hash") or ""):
        raise H74ObservationAuthorityError("h74_source_observation_authority_envelope_field_mismatch:db_snapshot_hash")
    if str(payload.get("db_snapshot_locator") or "") != str(bound.get("db_snapshot_locator") or ""):
        raise H74ObservationAuthorityError("h74_source_observation_authority_envelope_field_mismatch:db_snapshot_locator")
    if not payload.get("db_snapshot_hash") and not payload.get("db_snapshot_locator"):
        raise H74ObservationAuthorityError(
            "h74_source_observation_authority_required_field_missing:db_snapshot_hash_or_locator"
        )
    if int(bound.get("observation_window_days")) != H74_OBSERVATION_WINDOW_DAYS:
        raise H74ObservationAuthorityError("h74_source_observation_authority_window_days_invalid")
    if float(bound.get("max_entry_notional_krw") or 0.0) > H74_SOURCE_MAX_ORDER_KRW:
        raise H74ObservationAuthorityError("h74_source_observation_authority_max_notional_above_100000")
    if float(bound.get("DAILY_PARTICIPATION_MAX_ORDER_KRW") or 0.0) > H74_SOURCE_MAX_ORDER_KRW:
        raise H74ObservationAuthorityError("h74_source_observation_authority_daily_max_order_above_100000")
    if int(bound.get("max_daily_entry_count")) != H74_SOURCE_OBSERVATION_MAX_DAILY_ENTRY_COUNT:
        raise H74ObservationAuthorityError("h74_source_observation_authority_entry_count_invalid")
    if int(bound.get("max_daily_total_order_count")) != H74_SOURCE_OBSERVATION_MAX_DAILY_TOTAL_ORDER_COUNT:
        raise H74ObservationAuthorityError("h74_source_observation_authority_total_order_count_invalid")
    if int(bound.get("DAILY_PARTICIPATION_WINDOW_START_HOUR_KST")) != 9:
        raise H74ObservationAuthorityError("h74_source_observation_authority_window_start_invalid")
    if int(bound.get("DAILY_PARTICIPATION_WINDOW_END_HOUR_KST")) != 11:
        raise H74ObservationAuthorityError("h74_source_observation_authority_window_end_invalid")
    if int(bound.get("STRATEGY_EXIT_MAX_HOLDING_MIN")) != 74:
        raise H74ObservationAuthorityError("h74_source_observation_authority_holding_invalid")
    if str(bound.get("STRATEGY_EXIT_RULES") or "").strip().lower() != "max_holding_time":
        raise H74ObservationAuthorityError("h74_source_observation_authority_exit_rules_invalid")
    if str(bound.get("market") or "").strip().upper() != "KRW-BTC":
        raise H74ObservationAuthorityError("h74_source_observation_authority_market_invalid")
    if str(bound.get("interval") or "").strip() != "1m":
        raise H74ObservationAuthorityError("h74_source_observation_authority_interval_invalid")
    if str(bound.get("source_candidate_artifact_hash") or "").strip() == "":
        raise H74ObservationAuthorityError("h74_source_observation_authority_source_hash_missing")
    entry_submit_semantics = bound.get("entry_submit_semantics")
    if not isinstance(entry_submit_semantics, dict):
        raise H74ObservationAuthorityError("h74_source_observation_authority_entry_submit_semantics_missing")
    for key, expected in H74_ENTRY_SUBMIT_SEMANTICS.items():
        if entry_submit_semantics.get(key) != expected:
            raise H74ObservationAuthorityError(
                f"h74_source_observation_authority_entry_submit_semantics_mismatch:{key}"
            )
    if str(bound.get("entry_submit_semantics_name") or "").strip() != H74_ENTRY_SUBMIT_SEMANTICS_NAME:
        raise H74ObservationAuthorityError("h74_source_observation_authority_submit_semantics_name_invalid")
    if str(bound.get("submit_semantics_hash") or "").strip() != sha256_prefixed(entry_submit_semantics):
        raise H74ObservationAuthorityError("h74_source_observation_authority_submit_semantics_hash_mismatch")
    for key, expected in bound.items():
        if key in {
            "expires_at",
            "observation_window_days",
            "candidate_id",
            "source_candidate_artifact_hash",
            "backtest_report_hash",
            "validation_run_hash",
            "code_commit_sha",
            "approved_profile_evidence",
            "production_approval",
            "risk_policy_hash",
            "experiment_run_id",
            "env_hash",
            "strategy_revision_id",
            "risk_scope_id",
            "starting_broker_position",
            "starting_local_position",
            "experiment_envelope_hash",
            "experiment_envelope_locator",
            "risk_baseline_certificate_hash",
            "included_history_policy",
            "db_snapshot_hash",
            "db_snapshot_locator",
            "entry_submit_semantics",
            "entry_submit_semantics_name",
            "submit_semantics_hash",
        }:
            continue
        actual = runtime_values.get(key)
        if not _values_match(key, actual, expected):
            raise H74ObservationAuthorityError(f"h74_source_observation_authority_runtime_mismatch:{key}")
    expires_at = datetime.fromisoformat(str(bound.get("expires_at")).replace("Z", "+00:00"))
    if expires_at <= (now or datetime.now(timezone.utc)).astimezone(timezone.utc):
        raise H74ObservationAuthorityError("h74_source_observation_authority_expired")


def verify_h74_source_variant_observation_authority(
    payload: dict[str, Any],
    *,
    runtime_values: dict[str, object],
    now: datetime | None = None,
) -> None:
    if str(payload.get("artifact_type") or "") != H74_SOURCE_VARIANT_OBSERVATION_AUTHORITY_ARTIFACT_TYPE:
        raise H74ObservationAuthorityError("h74_source_variant_authority_artifact_type_invalid")
    if str(payload.get("authority_type") or "") != H74_SOURCE_VARIANT_OBSERVATION_AUTHORITY_ARTIFACT_TYPE:
        raise H74ObservationAuthorityError("h74_source_variant_authority_authority_type_invalid")
    if str(payload.get("base_candidate_id") or "") != H74_SOURCE_CANDIDATE_ID:
        raise H74ObservationAuthorityError("h74_source_variant_authority_base_candidate_id_invalid")
    if not str(payload.get("variant_id") or "").strip():
        raise H74ObservationAuthorityError("h74_source_variant_authority_variant_id_missing")
    for key in ("promotion_grade", "research_promotion_evidence", "approved_profile_evidence", "production_approval", "equivalence_to_source_candidate"):
        if bool(payload.get(key)) is not False:
            raise H74ObservationAuthorityError(f"h74_source_variant_authority_{key}_must_be_false")
    expected_hash = str(payload.get("authority_content_hash") or "")
    actual_hash = sha256_prefixed({k: v for k, v in payload.items() if k != "authority_content_hash"})
    if expected_hash != actual_hash:
        raise H74ObservationAuthorityError("h74_source_variant_authority_hash_mismatch")
    overrides = dict(payload.get("variant_overrides") or {})
    from .h74_variant_contract import validate_h74_variant_overrides

    validate_h74_variant_overrides(overrides)
    bound = dict(payload.get("hash_bound_parameters") or {})
    if int(bound.get("DAILY_PARTICIPATION_WINDOW_START_HOUR_KST")) != 0:
        raise H74ObservationAuthorityError("h74_source_variant_authority_window_start_invalid")
    if int(bound.get("DAILY_PARTICIPATION_WINDOW_END_HOUR_KST")) != 24:
        raise H74ObservationAuthorityError("h74_source_variant_authority_window_end_invalid")
    if str(payload.get("base_behavior_parameter_hash") or "") == str(payload.get("variant_behavior_parameter_hash") or ""):
        raise H74ObservationAuthorityError("h74_source_variant_authority_behavior_hash_not_distinct")
    for expected_key, expected_value in overrides.items():
        if bound.get(expected_key) != expected_value:
            raise H74ObservationAuthorityError(f"h74_source_variant_authority_override_not_bound:{expected_key}")
    for key, expected in bound.items():
        if key in {
            "expires_at",
            "observation_window_days",
            "candidate_id",
            "base_candidate_id",
            "source_candidate_artifact_hash",
            "backtest_report_hash",
            "validation_run_hash",
            "code_commit_sha",
            "approved_profile_evidence",
            "production_approval",
            "risk_policy_hash",
            "experiment_run_id",
            "env_hash",
            "strategy_revision_id",
            "risk_scope_id",
            "starting_broker_position",
            "starting_local_position",
            "experiment_envelope_hash",
            "experiment_envelope_locator",
            "risk_baseline_certificate_hash",
            "included_history_policy",
            "db_snapshot_hash",
            "db_snapshot_locator",
            "entry_submit_semantics",
            "entry_submit_semantics_name",
            "submit_semantics_hash",
            "variant_id",
            "variant_overrides",
            "probe_scope",
        }:
            continue
        actual = runtime_values.get(key)
        if not _values_match(key, actual, expected):
            raise H74ObservationAuthorityError(f"h74_source_variant_authority_runtime_mismatch:{key}")
    for key, expected in overrides.items():
        actual = runtime_values.get(key)
        if not _values_match(key, actual, expected):
            raise H74ObservationAuthorityError(f"h74_source_variant_authority_runtime_mismatch:{key}")
    expires_at = datetime.fromisoformat(str(bound.get("expires_at")).replace("Z", "+00:00"))
    if expires_at <= (now or datetime.now(timezone.utc)).astimezone(timezone.utc):
        raise H74ObservationAuthorityError("h74_source_variant_authority_expired")


def _verify_h74_source_observation_risk_policy(policy_payload: dict[str, object]) -> None:
    policy = risk_policy_from_mapping(policy_payload)
    if policy.policy_status != "enabled":
        raise H74ObservationAuthorityError("h74_source_observation_authority_risk_policy_disabled")
    if policy.missing_policy != "fail_closed_for_live":
        raise H74ObservationAuthorityError(
            "h74_source_observation_authority_risk_policy_missing_policy_invalid"
        )
    if policy.source != H74_SOURCE_OBSERVATION_RISK_POLICY_SOURCE:
        raise H74ObservationAuthorityError("h74_source_observation_authority_risk_policy_source_invalid")
    if policy.max_daily_order_count > H74_SOURCE_OBSERVATION_MAX_DAILY_TOTAL_ORDER_COUNT:
        raise H74ObservationAuthorityError(
            "h74_source_observation_authority_risk_policy_daily_order_count_too_high"
        )
    if policy.max_trade_count_per_day > H74_SOURCE_OBSERVATION_MAX_DAILY_TOTAL_ORDER_COUNT:
        raise H74ObservationAuthorityError(
            "h74_source_observation_authority_risk_policy_trade_count_too_high"
        )
    if policy.max_open_positions != 1:
        raise H74ObservationAuthorityError(
            "h74_source_observation_authority_risk_policy_open_positions_invalid"
        )
    if policy.unresolved_order_policy != "block":
        raise H74ObservationAuthorityError(
            "h74_source_observation_authority_risk_policy_unresolved_order_policy_invalid"
        )
    if policy.kill_switch is not False:
        raise H74ObservationAuthorityError("h74_source_observation_authority_risk_policy_kill_switch_invalid")
    if policy.max_daily_loss_krw <= 0.0 or policy.max_daily_loss_krw > H74_SOURCE_MAX_ORDER_KRW:
        raise H74ObservationAuthorityError(
            "h74_source_observation_authority_risk_policy_daily_loss_invalid"
        )
    if policy.max_position_loss_pct <= 0.0 or policy.max_position_loss_pct > 0.05:
        raise H74ObservationAuthorityError(
            "h74_source_observation_authority_risk_policy_position_loss_invalid"
        )


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


def h74_source_runtime_values_from_settings(settings_obj: object) -> dict[str, object]:
    values: dict[str, object] = {
        "strategy_name": str(getattr(settings_obj, "STRATEGY_NAME", H74_STRATEGY_NAME) or H74_STRATEGY_NAME),
        "market": str(getattr(settings_obj, "PAIR", "KRW-BTC") or "KRW-BTC"),
        "interval": str(getattr(settings_obj, "INTERVAL", "1m") or "1m"),
        "max_daily_order_count": int(
            getattr(settings_obj, "MAX_DAILY_ORDER_COUNT", H74_SOURCE_OBSERVATION_MAX_DAILY_TOTAL_ORDER_COUNT)
            or H74_SOURCE_OBSERVATION_MAX_DAILY_TOTAL_ORDER_COUNT
        ),
        "max_daily_entry_count": int(
            getattr(settings_obj, "DAILY_PARTICIPATION_MAX_DAILY_ENTRY_COUNT", H74_SOURCE_OBSERVATION_MAX_DAILY_ENTRY_COUNT)
            or H74_SOURCE_OBSERVATION_MAX_DAILY_ENTRY_COUNT
        ),
        "max_daily_total_order_count": int(
            getattr(settings_obj, "MAX_DAILY_ORDER_COUNT", H74_SOURCE_OBSERVATION_MAX_DAILY_TOTAL_ORDER_COUNT)
            or H74_SOURCE_OBSERVATION_MAX_DAILY_TOTAL_ORDER_COUNT
        ),
        "max_entry_notional_krw": float(
            getattr(settings_obj, "MAX_ORDER_KRW", H74_SOURCE_MAX_ORDER_KRW) or H74_SOURCE_MAX_ORDER_KRW
        ),
        "max_notional_krw": float(
            getattr(settings_obj, "DAILY_PARTICIPATION_MAX_ORDER_KRW", H74_SOURCE_MAX_ORDER_KRW)
            or H74_SOURCE_MAX_ORDER_KRW
        ),
        "exit_closeout_not_blocked_by_entry_cap": True,
    }
    for name, fallback in H74_SOURCE_OBSERVATION_PARAMETERS.items():
        if name in values:
            continue
        values[name] = getattr(settings_obj, name, os.getenv(name, fallback))
    return values


def verify_h74_observation_authority_file(path: str | Path, *, settings_obj: object) -> None:
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise H74ObservationAuthorityError("h74_observation_authority_payload_not_object")
    verify_h74_observation_authority(payload, runtime_values=h74_runtime_values_from_settings(settings_obj))


def verify_h74_source_observation_authority_file(path: str | Path, *, settings_obj: object) -> None:
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise H74ObservationAuthorityError("h74_source_observation_authority_payload_not_object")
    verify_h74_source_observation_authority(
        payload,
        runtime_values=h74_source_runtime_values_from_settings(settings_obj),
    )
    smoke_evidence_path = (
        str(getattr(settings_obj, H74_SOURCE_OBSERVATION_SMOKE_EVIDENCE_ENV, "") or "").strip()
        or os.getenv(H74_SOURCE_OBSERVATION_SMOKE_EVIDENCE_ENV, "").strip()
    )
    real_order = (
        str(getattr(settings_obj, "MODE", "") or "").strip().lower() == "live"
        and not bool(getattr(settings_obj, "LIVE_DRY_RUN", False))
        and bool(getattr(settings_obj, "LIVE_REAL_ORDER_ARMED", False))
    )
    rehearsal_no_submit_boundary = bool(
        getattr(settings_obj, "H74_LIVE_REHEARSAL_NO_SUBMIT_BOUNDARY", False)
        or os.getenv("H74_LIVE_REHEARSAL_NO_SUBMIT_BOUNDARY", "").strip().lower() == "true"
    )
    if real_order and not smoke_evidence_path and not rehearsal_no_submit_boundary:
        raise H74ObservationAuthorityError("h74_source_observation_live_pipeline_smoke_evidence_missing")
    if smoke_evidence_path:
        verify_h74_source_live_pipeline_smoke_evidence_file(smoke_evidence_path)


def verify_h74_source_variant_observation_authority_file(path: str | Path, *, settings_obj: object) -> None:
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise H74ObservationAuthorityError("h74_source_variant_authority_payload_not_object")
    verify_h74_source_variant_observation_authority(
        payload,
        runtime_values=h74_source_runtime_values_from_settings(settings_obj),
    )


def h74_source_observation_risk_profile_payload_from_settings(
    settings_obj: object,
) -> dict[str, object] | None:
    authority_path = (
        str(getattr(settings_obj, H74_SOURCE_OBSERVATION_AUTHORITY_ENV, "") or "").strip()
        or os.getenv(H74_SOURCE_OBSERVATION_AUTHORITY_ENV, "").strip()
    )
    if not authority_path:
        return None
    with Path(authority_path).expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise H74ObservationAuthorityError("h74_source_observation_authority_payload_not_object")
    from .h74_authority_alignment import validate_h74_authority_file_env_alignment

    validate_h74_authority_file_env_alignment(authority_path, settings_obj=settings_obj)
    risk_policy = payload.get("risk_policy")
    if not isinstance(risk_policy, dict):
        raise H74ObservationAuthorityError("h74_source_observation_authority_risk_policy_missing")
    return {
        "risk_policy": dict(risk_policy),
        "risk_policy_hash": str(payload.get("risk_policy_hash") or "").strip(),
        "risk_profile_source": H74_SOURCE_OBSERVATION_RISK_POLICY_SOURCE,
        "risk_enforcement_mode": "enforced",
        "missing_risk_policy_behavior": "fail_closed_for_live",
        "approved_profile_verification_ok": False,
        "approved_profile_block_reason": "h74_source_observation_authority_used",
        "approved_profile_contract_scope": "h74_source_live_observation_only",
        "production_approval": False,
    }


def h74_source_observation_risk_profile_payload_for_runtime_strategy(
    *,
    settings_obj: object,
    strategy_name: str,
    approved_profile_path: str | None,
    live_like: bool,
) -> dict[str, object] | None:
    if not live_like:
        return None
    if str(strategy_name or "").strip().lower() != H74_STRATEGY_NAME:
        return None
    if str(approved_profile_path or "").strip():
        return None
    return h74_source_observation_risk_profile_payload_from_settings(settings_obj)


def verify_h74_source_live_pipeline_smoke_evidence_file(path: str | Path) -> None:
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise H74ObservationAuthorityError("h74_source_observation_smoke_evidence_payload_not_object")
    if str(payload.get("status") or "").strip().lower() != "passed":
        raise H74ObservationAuthorityError("h74_source_observation_smoke_evidence_not_passed")
    if str(payload.get("execution_mode") or "").strip() != "live_pipeline_smoke":
        raise H74ObservationAuthorityError("h74_source_observation_smoke_evidence_mode_invalid")
    if bool(payload.get("manual_intervention_required")):
        raise H74ObservationAuthorityError("h74_source_observation_smoke_evidence_manual_intervention")
    final = payload.get("final")
    if not isinstance(final, dict):
        raise H74ObservationAuthorityError("h74_source_observation_smoke_evidence_final_missing")
    for key in ("broker_qty", "portfolio_qty", "projected_total_qty"):
        try:
            value = float(final.get(key))
        except (TypeError, ValueError):
            raise H74ObservationAuthorityError(
                f"h74_source_observation_smoke_evidence_final_invalid:{key}"
            ) from None
        if value != 0.0:
            raise H74ObservationAuthorityError(
                f"h74_source_observation_smoke_evidence_not_flat:{key}"
            )
    for key in ("open_order_count", "submit_unknown_count", "recovery_required_count"):
        try:
            value = int(final.get(key))
        except (TypeError, ValueError):
            raise H74ObservationAuthorityError(
                f"h74_source_observation_smoke_evidence_final_invalid:{key}"
            ) from None
        if value != 0:
            raise H74ObservationAuthorityError(
                f"h74_source_observation_smoke_evidence_not_converged:{key}"
            )


def h74_source_observation_policy_from_settings(settings_obj: object) -> dict[str, object] | None:
    authority_path = (
        str(getattr(settings_obj, H74_SOURCE_OBSERVATION_AUTHORITY_ENV, "") or "").strip()
        or os.getenv(H74_SOURCE_OBSERVATION_AUTHORITY_ENV, "").strip()
    )
    if not authority_path:
        return None
    try:
        verify_h74_source_observation_authority_file(authority_path, settings_obj=settings_obj)
    except Exception as exc:
        return {
            "_policy_load_error": f"h74_source_observation_authority_invalid:{type(exc).__name__}:{exc}",
            "_policy_source": authority_path,
            "h74_observation_authority_verified": False,
            "approved_profile_verification_ok": False,
            "approved_profile_block_reason": "h74_source_observation_authority_invalid",
            "approved_profile_contract_scope": "h74_source_live_observation_only",
            "production_approval": False,
        }
    return {
        "_policy_source": authority_path,
        "h74_observation_authority_verified": True,
        "h74_source_observation_authority_path": str(Path(authority_path).expanduser().resolve()),
        "approved_profile_verification_ok": False,
        "approved_profile_block_reason": "h74_source_observation_authority_used",
        "approved_profile_loaded": False,
        "approved_profile_schema_hash_valid": False,
        "approved_profile_source_verified": False,
        "approved_profile_evidence_verified": False,
        "approved_profile_runtime_verified": False,
        "approved_profile_contract_scope": "h74_source_live_observation_only",
        "production_approval": False,
        "risk_profile_source": H74_SOURCE_OBSERVATION_RISK_POLICY_SOURCE,
        "risk_enforcement_mode": "enforced",
    }


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


def cmd_h74_source_observation_authority_generate(
    *,
    out_path: str | None = None,
    source_candidate_artifact_hash: str,
    backtest_report_hash: str | None = None,
    validation_run_hash: str | None = None,
    code_commit_sha: str | None = None,
    experiment_envelope_path: str,
) -> int:
    with Path(experiment_envelope_path).expanduser().open("r", encoding="utf-8") as handle:
        experiment_envelope = json.load(handle)
    if not isinstance(experiment_envelope, dict):
        raise H74ObservationAuthorityError("h74_source_observation_experiment_envelope_payload_not_object")
    payload = build_h74_source_observation_authority_payload(
        source_candidate_artifact_hash=source_candidate_artifact_hash,
        backtest_report_hash=backtest_report_hash,
        validation_run_hash=validation_run_hash,
        code_commit_sha=code_commit_sha,
        experiment_envelope_payload=experiment_envelope,
        experiment_envelope_locator=str(Path(experiment_envelope_path).expanduser().resolve()),
    )
    if out_path:
        write_json_atomic(Path(out_path).expanduser(), payload)
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return 0


def cmd_h74_source_observation_authority_verify(*, authority_path: str) -> int:
    with Path(authority_path).expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    verify_h74_source_observation_authority(payload, runtime_values=H74_SOURCE_OBSERVATION_PARAMETERS)
    print(json.dumps({"ok": True, "authority_path": str(authority_path)}, sort_keys=True))
    return 0
