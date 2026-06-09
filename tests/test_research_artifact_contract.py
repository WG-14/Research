from __future__ import annotations

import pytest

from bithumb_bot.research.artifact_contract import (
    apply_artifact_contract,
    artifact_contract_for_type,
    diagnostic_artifact_rejection_reasons,
    validate_artifact_contract,
)


def _payload(artifact_type: str = "forward_return_diagnostic_report") -> dict[str, object]:
    return apply_artifact_contract({"schema_version": 1, "artifact_type": artifact_type})


def test_forward_diagnostics_report_contract_is_registered() -> None:
    assert artifact_contract_for_type("forward_return_diagnostic_report").artifact_type == "forward_return_diagnostic_report"


def test_forward_diagnostics_failure_contract_is_registered() -> None:
    assert artifact_contract_for_type("forward_return_diagnostic_failure").artifact_type == "forward_return_diagnostic_failure"


def test_forward_diagnostics_policy_denial_contract_is_registered() -> None:
    assert (
        artifact_contract_for_type("forward_return_diagnostic_policy_denial").artifact_type
        == "forward_return_diagnostic_policy_denial"
    )


def test_contract_rejects_promotion_eligible_true() -> None:
    payload = _payload()
    payload["promotion_eligible"] = True

    with pytest.raises(ValueError, match="diagnostic-only"):
        validate_artifact_contract(payload)


def test_contract_rejects_missing_forbidden_uses() -> None:
    payload = _payload()
    payload["forbidden_uses"] = ["strategy_promotion"]

    with pytest.raises(ValueError, match="forbidden_uses"):
        validate_artifact_contract(payload)


def test_unknown_diagnostic_artifact_type_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown diagnostic artifact_type"):
        artifact_contract_for_type("forward_return_diagnostic_unknown")


def test_diagnostic_contract_rejection_reasons_include_all_forbidden_uses() -> None:
    reasons = diagnostic_artifact_rejection_reasons(_payload())

    assert "forbidden_use:strategy_promotion" in reasons
    assert "forbidden_use:approved_profile" in reasons
    assert "forbidden_use:live_readiness" in reasons
    assert "forbidden_use:capital_allocation" in reasons
