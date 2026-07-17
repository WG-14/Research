from __future__ import annotations

import pytest

from market_research.research.decision_event import ResearchDecisionEvent
from market_research.research.execution_model.base import (
    ExecutionFill,
    ExecutionRequest,
)
from market_research.research.exit_decision import ExitDecision
from market_research.research.hashing import sha256_prefixed


def _decision() -> ResearchDecisionEvent:
    return ResearchDecisionEvent(
        1,
        2,
        "fixture",
        "v1",
        "HOLD",
        "HOLD",
        "test",
        {"nested": {"x": 1}},
        {"nested": {"x": 1}},
        extra_payload={"nested": {"x": 1}},
    )


@pytest.mark.parametrize(
    "field", ("feature_snapshot", "strategy_diagnostics", "extra_payload")
)
def test_decision_nested_payloads_are_immutable(field):
    with pytest.raises(TypeError):
        getattr(_decision(), field)["nested"]["x"] = 2


def test_exit_decision_evidence_is_immutable():
    value = ExitDecision(False, None, "none", {"nested": {"x": 1}})
    with pytest.raises(TypeError):
        value.evidence["nested"]["x"] = 2


def test_execution_request_and_fill_payloads_are_immutable_and_detached():
    request = ExecutionRequest(
        1,
        2,
        "BUY",
        10.0,
        0.0,
        feature_snapshot={"nested": {"x": 1}},
        regime_snapshot={"nested": {"x": 1}},
    )
    fill = ExecutionFill(
        1,
        2,
        3,
        "BUY",
        "market",
        10.0,
        request_id=request.request_id,
        feature_snapshot={"nested": {"x": 1}},
        regime_snapshot={"nested": {"x": 1}},
        seed_derivation_inputs={"nested": {"x": 1}},
    )
    for owner, field in (
        (request, "feature_snapshot"),
        (request, "regime_snapshot"),
        (fill, "feature_snapshot"),
        (fill, "regime_snapshot"),
        (fill, "seed_derivation_inputs"),
    ):
        with pytest.raises(TypeError):
            getattr(owner, field)["nested"]["x"] = 2
    detached = fill.as_dict()
    detached["seed_derivation_inputs"]["nested"]["x"] = 9
    assert fill.seed_derivation_inputs["nested"]["x"] == 1


def test_authoritative_ids_match_current_canonical_payload():
    event = _decision()
    assert event.decision_id() == event._calculated_decision_id()
    request = ExecutionRequest(1, 2, "BUY", 10.0, 0.0, feature_snapshot={"x": 1})
    request_payload = request.as_dict()
    request_id = request_payload.pop("request_id")
    assert request_id == sha256_prefixed(request_payload)
    fill = ExecutionFill(1, 2, 3, "BUY", "market", 10.0, request_id=request.request_id)
    fill_payload = fill.as_dict()
    fill_id = fill_payload.pop("fill_id")
    assert fill_id == sha256_prefixed(fill_payload)
