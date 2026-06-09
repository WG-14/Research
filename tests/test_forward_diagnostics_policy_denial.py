from __future__ import annotations

import json

import pytest

from bithumb_bot.research.forward_diagnostics_policy_denial import (
    POLICY_DENIAL_ARTIFACT_TYPE,
    build_forward_diagnostics_policy_denial_payload,
    validate_forward_diagnostics_policy_denial_flags,
    write_forward_diagnostics_policy_denial_artifact,
)
from tests.test_forward_diagnostics_report import _manager, _manifest


def _payload() -> dict[str, object]:
    return build_forward_diagnostics_policy_denial_payload(
        manifest=_manifest(),
        reason="final_holdout_diagnostic_override_required",
        split_name="final_holdout",
        feature_names=("sma_gap",),
        horizon_steps=(1,),
    )


def test_policy_denial_payload_has_diagnostic_only_flags() -> None:
    payload = _payload()

    assert payload["artifact_type"] == POLICY_DENIAL_ARTIFACT_TYPE
    assert payload["diagnostic_status"] == "policy_denied"
    assert payload["reason"] == "final_holdout_diagnostic_override_required"
    assert payload["split_name"] == "final_holdout"
    assert payload["diagnostic_only"] is True
    assert payload["promotion_evidence"] is False
    assert payload["approved_profile_evidence"] is False
    assert payload["live_readiness_evidence"] is False
    assert payload["capital_allocation_evidence"] is False
    assert payload["operator_next_action"] == "rerun_with_explicit_override_or_use_train_validation"


def test_policy_denial_payload_rejects_promotion_evidence_true() -> None:
    payload = _payload()
    payload["promotion_evidence"] = True

    with pytest.raises(ValueError, match="diagnostic-only"):
        validate_forward_diagnostics_policy_denial_flags(payload)


def test_policy_denial_artifact_written_under_research_report_path(tmp_path) -> None:
    manager = _manager(tmp_path)
    payload = write_forward_diagnostics_policy_denial_artifact(
        manager=manager,
        manifest=_manifest(),
        reason="final_holdout_diagnostic_override_required",
        split_name="final_holdout",
        feature_names=("sma_gap",),
        horizon_steps=(1,),
    )
    path = manager.data_dir() / "reports/research/exp1/forward_diagnostics_policy_denial.json"

    assert path.exists()
    assert payload["artifact_paths"]["policy_denial"] == str(path)
    assert json.loads(path.read_text(encoding="utf-8"))["artifact_type"] == POLICY_DENIAL_ARTIFACT_TYPE


def test_policy_denial_uses_policy_denied_status() -> None:
    assert _payload()["diagnostic_status"] == "policy_denied"


def test_policy_denial_does_not_use_forward_return_diagnostic_report_type() -> None:
    assert _payload()["artifact_type"] != "forward_return_diagnostic_report"


def test_policy_denial_rejects_unavailable_status() -> None:
    payload = _payload()
    payload["diagnostic_status"] = "unavailable"

    with pytest.raises(ValueError, match="policy_denied"):
        validate_forward_diagnostics_policy_denial_flags(payload)


def test_policy_denial_includes_non_promotable_taxonomy() -> None:
    payload = _payload()

    assert payload["evidence_scope"] == "diagnostic_feature_mining"
    assert payload["promotion_eligible"] is False
    assert payload["promotion_grade"] is False
    assert payload["non_promotable"] is True
    assert set(payload["forbidden_uses"]) >= {
        "strategy_promotion",
        "approved_profile",
        "live_readiness",
        "capital_allocation",
    }
