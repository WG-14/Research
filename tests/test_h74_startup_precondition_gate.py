from __future__ import annotations

from bithumb_bot.h74_startup_gate import evaluate_h74_startup_gate


def _readiness(**overrides) -> dict[str, object]:
    payload = {
        "broker_position_evidence": {"broker_qty_known": True, "broker_qty": 0.0},
        "projection_convergence": {"portfolio_qty": 0.0, "projected_total_qty": 0.0},
        "open_order_count": 0,
        "submit_unknown_count": 0,
        "recovery_required_count": 0,
        "residual_inventory_state": "flat",
    }
    payload.update(overrides)
    return payload


def test_h74_start_blocks_when_broker_qty_executable_residual_exists() -> None:
    result = evaluate_h74_startup_gate(
        readiness_payload=_readiness(
            broker_position_evidence={"broker_qty_known": True, "broker_qty": 0.0001},
            projection_convergence={"portfolio_qty": 0.0001, "projected_total_qty": 0.0001},
        )
    )

    assert result.status == "START_BLOCKED"
    assert result.reason_code == "broker_executable_residual_exists"


def test_h74_start_blocks_when_persisted_target_state_nonzero() -> None:
    result = evaluate_h74_startup_gate(readiness_payload=_readiness(), target_state={"target_exposure_krw": 100_000.0})

    assert result.status == "START_BLOCKED"
    assert result.reason_code == "target_state_nonzero"


def test_h74_start_blocks_when_submit_unknown_exists() -> None:
    result = evaluate_h74_startup_gate(readiness_payload=_readiness(submit_unknown_count=1))

    assert result.status == "START_BLOCKED"
    assert result.reason_code == "submit_unknown_count_nonzero"


def test_h74_start_allows_clean_flat_broker_and_local_state() -> None:
    result = evaluate_h74_startup_gate(readiness_payload=_readiness())

    assert result.status == "START_ALLOWED"
    assert result.allowed is True
    assert result.as_dict()["startup_gate_hash"].startswith("sha256:")


def test_h74_start_true_dust_requires_explicit_authority_policy() -> None:
    blocked = evaluate_h74_startup_gate(
        readiness_payload=_readiness(
            broker_position_evidence={"broker_qty_known": True, "broker_qty": 0.00001},
            projection_convergence={"portfolio_qty": 0.00001, "projected_total_qty": 0.00001},
            residual_inventory_state="terminal_true_dust",
        )
    )
    allowed = evaluate_h74_startup_gate(
        readiness_payload=_readiness(
            broker_position_evidence={"broker_qty_known": True, "broker_qty": 0.00001},
            projection_convergence={"portfolio_qty": 0.00001, "projected_total_qty": 0.00001},
            residual_inventory_state="terminal_true_dust",
        ),
        authority={"residual_inventory_mode": "allow_terminal_true_dust"},
    )

    assert blocked.status == "START_BLOCKED"
    assert allowed.status == "START_ALLOWED_WITH_TERMINAL_DUST"
