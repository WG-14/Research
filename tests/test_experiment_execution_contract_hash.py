from __future__ import annotations

from bithumb_bot.experiment_execution_contract import ExperimentExecutionContract


def _contract(**overrides) -> ExperimentExecutionContract:
    payload = {
        "source_artifact_hash": "sha256:source",
        "authority_hash": "sha256:authority",
        "code_commit_sha": "abc",
        "env_file_hash": "sha256:env",
        "strategy_parameter_hash": "sha256:params",
        "position_mode": "fixed_fill_qty_until_exit",
        "quantity_contract_hash": "sha256:qty",
        "order_rule_snapshot_hash": "sha256:rules",
        "fee_slippage_timing_hash": "sha256:fee",
        "startup_gate_hash": "sha256:gate",
    }
    payload.update(overrides)
    return ExperimentExecutionContract(**payload)


def test_contract_hash_changes_when_env_hash_changes() -> None:
    assert _contract().contract_hash() != _contract(env_file_hash="sha256:env2").contract_hash()


def test_contract_hash_changes_when_order_rule_snapshot_changes() -> None:
    assert _contract().contract_hash() != _contract(order_rule_snapshot_hash="sha256:rules2").contract_hash()


def test_contract_hash_changes_when_position_mode_changes() -> None:
    assert _contract().contract_hash() != _contract(position_mode="continuous_notional_target").contract_hash()


def test_h74_start_blocks_when_contract_hash_mismatch() -> None:
    certificate = {"contract_hash": _contract().contract_hash()}
    current = _contract(env_file_hash="sha256:env2").contract_hash()

    assert certificate["contract_hash"] != current
