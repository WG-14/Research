from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bithumb_bot.execution_service import _h74_execution_path_probe_authority_allows_submit
from bithumb_bot.h74_observation import (
    H74_SOURCE_OBSERVATION_PARAMETERS,
    H74_SOURCE_VARIANT_OBSERVATION_AUTHORITY_ARTIFACT_TYPE,
    build_h74_observation_experiment_envelope,
    build_h74_source_observation_authority_payload,
    build_h74_source_variant_observation_authority_payload,
)
from bithumb_bot.research.hashing import sha256_prefixed


pytestmark = pytest.mark.fast_regression


def _envelope() -> dict[str, object]:
    return build_h74_observation_experiment_envelope(
        experiment_run_id="probe-exp",
        runtime_git_commit_sha="commit",
        runtime_git_clean=True,
        env_hash="sha256:" + "1" * 64,
        strategy_revision_id="sha256:" + "2" * 64,
        risk_scope_id="sha256:" + "3" * 64,
        risk_baseline_certificate_hash="sha256:" + "4" * 64,
        starting_broker_position={"qty": 0},
        starting_local_position={"qty": 0},
        db_snapshot_hash="sha256:" + "5" * 64,
        included_history_policy="declared_live_history_scope",
    )


def _source_authority() -> dict[str, object]:
    return build_h74_source_observation_authority_payload(
        source_candidate_artifact_hash="sha256:source",
        backtest_report_hash="sha256:backtest",
        validation_run_hash="sha256:validation",
        code_commit_sha="commit",
        experiment_envelope_payload=_envelope(),
    )


def _variant_authority() -> dict[str, object]:
    return build_h74_source_variant_observation_authority_payload(
        base_authority=_source_authority(),
        variant_overrides={
            "DAILY_PARTICIPATION_WINDOW_START_HOUR_KST": 0,
            "DAILY_PARTICIPATION_WINDOW_END_HOUR_KST": 24,
        },
        experiment_envelope_payload=_envelope(),
    )


def _write_authority(tmp_path, payload: dict[str, object]) -> str:
    path = tmp_path / "h74-authority.json"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return str(path)


def _settings(authority_path: str, **overrides: object) -> SimpleNamespace:
    values = dict(H74_SOURCE_OBSERVATION_PARAMETERS)
    values.update(
        {
            "MODE": "live",
            "LIVE_DRY_RUN": False,
            "LIVE_REAL_ORDER_ARMED": True,
            "STRATEGY_NAME": "daily_participation_sma",
            "PAIR": "KRW-BTC",
            "INTERVAL": "1m",
            "MAX_ORDER_KRW": 100_000.0,
            "MAX_DAILY_ORDER_COUNT": 2,
            "H74_SOURCE_OBSERVATION_AUTHORITY_PATH": authority_path,
            "H74_EXECUTION_PATH_PROBE_RUN_ID": "probe-run-1",
        }
    )
    values["DAILY_PARTICIPATION_WINDOW_START_HOUR_KST"] = 0
    values["DAILY_PARTICIPATION_WINDOW_END_HOUR_KST"] = 24
    values.update(overrides)
    return SimpleNamespace(**values)


def _payload(**overrides: object) -> dict[str, object]:
    payload = {
        "strategy": "daily_participation_sma",
        "h74_fixed_position_contract_active": True,
        "h74_execution_path_probe_run_id": "probe-run-1",
    }
    payload.update(overrides)
    return payload


def _rehash(payload: dict[str, object]) -> dict[str, object]:
    payload["authority_content_hash"] = sha256_prefixed(
        {key: value for key, value in payload.items() if key != "authority_content_hash"}
    )
    return payload


def test_h74_production_missing_certificate_without_probe_authority_fails_closed(tmp_path) -> None:
    cfg = _settings("")

    assert _h74_execution_path_probe_authority_allows_submit(_payload(), cfg) is False


def test_h74_no_window_variant_probe_authority_allows_missing_certificate_branch(tmp_path) -> None:
    authority_path = _write_authority(tmp_path, _variant_authority())
    cfg = _settings(authority_path)

    assert _h74_execution_path_probe_authority_allows_submit(_payload(), cfg) is True


def test_h74_probe_authority_requires_probe_run_id(tmp_path) -> None:
    authority_path = _write_authority(tmp_path, _variant_authority())
    cfg = _settings(authority_path, H74_EXECUTION_PATH_PROBE_RUN_ID="")

    assert _h74_execution_path_probe_authority_allows_submit(_payload(), cfg) is False


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("artifact_type", "h74_source_live_observation_authority"),
        ("contract_scope", "h74_source_live_observation_only"),
        ("acceptance_track", "production_readiness"),
        ("probe_scope", "full_runtime"),
    ),
)
def test_h74_probe_authority_rejects_wrong_variant_metadata(tmp_path, field, value) -> None:
    authority = _variant_authority()
    authority[field] = value
    authority_path = _write_authority(tmp_path, _rehash(authority))
    cfg = _settings(authority_path)

    assert _h74_execution_path_probe_authority_allows_submit(_payload(), cfg) is False


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("DAILY_PARTICIPATION_WINDOW_START_HOUR_KST", 9),
        ("DAILY_PARTICIPATION_WINDOW_END_HOUR_KST", 11),
        ("SMA_SHORT", 11),
        ("SMA_LONG", 87),
        ("STRATEGY_EXIT_MAX_HOLDING_MIN", 75),
        ("DAILY_PARTICIPATION_MAX_ORDER_KRW", 100_001),
    ),
)
def test_h74_probe_authority_rejects_wrong_runtime_alignment(tmp_path, field, value) -> None:
    authority_path = _write_authority(tmp_path, _variant_authority())
    cfg = _settings(authority_path, **{field: value})

    assert _h74_execution_path_probe_authority_allows_submit(_payload(), cfg) is False


def test_h74_probe_authority_rejects_max_order_above_100000(tmp_path) -> None:
    authority_path = _write_authority(tmp_path, _variant_authority())
    cfg = _settings(authority_path, MAX_ORDER_KRW=100_001)

    assert _h74_execution_path_probe_authority_allows_submit(_payload(), cfg) is False


def test_h74_probe_authority_rejects_authority_bound_order_cap_above_100000(tmp_path) -> None:
    authority = _variant_authority()
    bound = dict(authority["hash_bound_parameters"])
    bound["max_entry_notional_krw"] = 100_001
    authority["hash_bound_parameters"] = bound
    authority_path = _write_authority(tmp_path, _rehash(authority))
    cfg = _settings(authority_path)

    assert _h74_execution_path_probe_authority_allows_submit(_payload(), cfg) is False


def test_h74_source_production_authority_remains_distinct_from_no_window_probe(tmp_path) -> None:
    authority = _source_authority()
    assert authority["artifact_type"] != H74_SOURCE_VARIANT_OBSERVATION_AUTHORITY_ARTIFACT_TYPE
    authority_path = _write_authority(tmp_path, authority)
    cfg = _settings(
        authority_path,
        DAILY_PARTICIPATION_WINDOW_START_HOUR_KST=9,
        DAILY_PARTICIPATION_WINDOW_END_HOUR_KST=11,
    )

    assert _h74_execution_path_probe_authority_allows_submit(_payload(), cfg) is False
