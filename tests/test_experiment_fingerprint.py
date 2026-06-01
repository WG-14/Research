from __future__ import annotations

from bithumb_bot.experiment_fingerprint import (
    build_experiment_fingerprint_payload,
    experiment_fingerprint,
)


CANARY_PARAMS = {
    "CANARY_ORDER_START_INDEX": 0,
    "CANARY_ORDER_SIDE": "BUY",
    "CANARY_ORDER_REASON": "unit_canary",
}


def test_experiment_fingerprint_is_stable_for_same_runtime_contract() -> None:
    assert experiment_fingerprint(
        strategy_name="canary_non_sma",
        parameter_overrides=CANARY_PARAMS,
    ) == experiment_fingerprint(
        strategy_name="canary_non_sma",
        parameter_overrides=CANARY_PARAMS,
    )


def test_experiment_fingerprint_changes_when_canary_behavior_param_changes() -> None:
    baseline = experiment_fingerprint(
        strategy_name="canary_non_sma",
        parameter_overrides={
            "CANARY_ORDER_START_INDEX": 0,
            "CANARY_ORDER_SIDE": "BUY",
            "CANARY_ORDER_REASON": "left",
        },
    )
    changed = experiment_fingerprint(
        strategy_name="canary_non_sma",
        parameter_overrides={
            "CANARY_ORDER_START_INDEX": 1,
            "CANARY_ORDER_SIDE": "BUY",
            "CANARY_ORDER_REASON": "left",
        },
    )

    assert changed != baseline


def test_experiment_fingerprint_payload_uses_contract_hash_identity() -> None:
    payload = build_experiment_fingerprint_payload(
        strategy_name="canary_non_sma",
        parameter_overrides=CANARY_PARAMS,
    )

    assert payload["strategy_name"] == "canary_non_sma"
    assert str(payload["strategy_parameters_hash"]).startswith("sha256:")
    assert str(payload["runtime_contract_hash"]).startswith("sha256:")
    assert str(payload["plugin_contract_hash"]).startswith("sha256:")
    assert str(payload["runtime_decision_request_hash"]).startswith("sha256:")
    assert "sma_short" not in payload
    assert "sma_long" not in payload
