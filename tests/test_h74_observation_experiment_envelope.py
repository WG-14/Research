from __future__ import annotations

import pytest

from bithumb_bot.h74_observation import (
    H74ObservationAuthorityError,
    H74_SOURCE_OBSERVATION_PARAMETERS,
    build_h74_observation_experiment_envelope,
    build_h74_source_observation_authority_payload,
    h74_source_observation_risk_policy,
    verify_h74_observation_experiment_envelope,
    verify_h74_source_observation_authority,
)
from bithumb_bot.risk_policy_engine import RiskPolicyEngine
from bithumb_bot.strategy_risk_profile import risk_policy_from_mapping
from bithumb_bot.strategy_risk_state import StrategyRiskStateProvider
from bithumb_bot.db_core import ensure_db


def _envelope(**overrides: object) -> dict[str, object]:
    payload = {
        "experiment_run_id": "exp-1",
        "runtime_git_commit_sha": "abc",
        "runtime_git_clean": True,
        "env_hash": "sha256:" + "1" * 64,
        "strategy_revision_id": "sha256:" + "2" * 64,
        "risk_scope_id": "sha256:" + "3" * 64,
        "risk_baseline_certificate_hash": "sha256:" + "4" * 64,
        "starting_broker_position": {"qty": 0},
        "starting_local_position": {"qty": 0},
        "db_snapshot_hash": "sha256:" + "5" * 64,
        "included_history_policy": "declared_live_history_scope",
    }
    payload.update(overrides)
    return build_h74_observation_experiment_envelope(**payload)  # type: ignore[arg-type]


def test_h74_real_observation_requires_experiment_envelope() -> None:
    with pytest.raises(H74ObservationAuthorityError, match="experiment_run_id"):
        _envelope(experiment_run_id="")


def test_h74_envelope_binds_risk_scope_and_baseline() -> None:
    payload = _envelope()
    verify_h74_observation_experiment_envelope(payload)

    assert payload["risk_scope_id"] == "sha256:" + "3" * 64
    assert payload["risk_baseline_certificate_hash"] == "sha256:" + "4" * 64


def test_h74_envelope_records_included_history_policy() -> None:
    payload = _envelope(included_history_policy="explicit_allowlist")

    assert payload["included_history_policy"] == "explicit_allowlist"


def test_h74_source_authority_requires_experiment_envelope_hash() -> None:
    payload = build_h74_source_observation_authority_payload(
        source_candidate_artifact_hash="sha256:source-candidate",
        backtest_report_hash="sha256:backtest",
        validation_run_hash="sha256:validation",
        code_commit_sha="test-commit",
    )
    verify_h74_source_observation_authority(payload, runtime_values=H74_SOURCE_OBSERVATION_PARAMETERS)

    assert str(payload["experiment_envelope_hash"]).startswith("sha256:")
    assert str(payload["risk_baseline_certificate_hash"]).startswith("sha256:")
    assert payload["included_history_policy"] == "declared_live_history_scope"

    broken = dict(payload)
    broken["experiment_envelope_hash"] = ""
    broken["hash_bound_parameters"] = dict(payload["hash_bound_parameters"])
    broken["hash_bound_parameters"]["experiment_envelope_hash"] = ""
    from bithumb_bot.research.hashing import sha256_prefixed

    broken["authority_content_hash"] = sha256_prefixed(
        {k: v for k, v in broken.items() if k != "authority_content_hash"}
    )
    with pytest.raises(H74ObservationAuthorityError, match="experiment_envelope_hash"):
        verify_h74_source_observation_authority(broken, runtime_values=H74_SOURCE_OBSERVATION_PARAMETERS)


def test_h74_risk_decision_references_experiment_envelope_hash(tmp_path) -> None:
    envelope = _envelope()
    conn = ensure_db(str(tmp_path / "h74-risk-envelope.sqlite"))
    policy = risk_policy_from_mapping(h74_source_observation_risk_policy())

    snapshot = StrategyRiskStateProvider(conn).snapshot(
        strategy_instance_id="h74",
        strategy_name="daily_participation_sma",
        pair="KRW-BTC",
        interval="1m",
        as_of_ts_ms=1_800_000_000_000,
        mark_price=100.0,
        policy=policy,
        enforced=True,
        risk_scope_id=str(envelope["risk_scope_id"]),
        experiment_envelope_hash=str(envelope["experiment_envelope_hash"]),
        risk_baseline_certificate_hash=str(envelope["risk_baseline_certificate_hash"]),
        included_history_policy=str(envelope["included_history_policy"]),
        db_snapshot_hash=str(envelope["db_snapshot_hash"]),
    )
    decision = RiskPolicyEngine(policy).evaluate_pre_decision(snapshot)

    assert decision.evidence["experiment_envelope_hash"] == envelope["experiment_envelope_hash"]
