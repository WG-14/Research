from __future__ import annotations

from dataclasses import replace

import pytest

from market_research.research.execution_evidence import (
    ExecutionEvidenceError,
    validate_execution_evidence,
)
from market_research.research.execution_model import FixedBpsExecutionModel
from market_research.research.experiment_manifest import ExecutionTimingPolicy
from tests.test_common_simulation_engine import SpyModel, _run


def test_validation_fails_when_executed_model_hash_differs():
    model = SpyModel()
    run = _run(model)
    run.execution_event_summary["executed_execution_model_hash"] = "sha256:wrong"  # type: ignore[index]
    with pytest.raises(ExecutionEvidenceError, match="model_hash_mismatch"):
        validate_execution_evidence(
            run=run,
            timing=ExecutionTimingPolicy(
                fill_reference_policy="next_candle_open",
                allow_same_candle_close_fill=False,
            ),
            model=model,
        )


def test_zero_intent_run_allows_zero_execution_counts():
    from market_research.research.simulation_engine import (
        run_common_simulation_backtest,
    )
    from market_research.research_composition import (
        resolve_builtin_strategy as resolve_research_strategy,
    )
    from tests.test_common_simulation_engine import _dataset

    model = FixedBpsExecutionModel(0.001, 10)
    run = run_common_simulation_backtest(
        plugin=resolve_research_strategy("noop_baseline"),
        dataset=_dataset(),
        parameter_values={},
        fee_rate=0.001,
        slippage_bps=10,
        execution_model=model,
        execution_timing_policy=ExecutionTimingPolicy(),
        portfolio_policy=__import__(
            "market_research.research.experiment_manifest",
            fromlist=["legacy_research_portfolio_policy"],
        ).legacy_research_portfolio_policy(),
    )
    assert (
        validate_execution_evidence(
            run=run, timing=ExecutionTimingPolicy(), model=model
        )["status"]
        == "PASS"
    )


def test_persisted_fill_quote_knowledge_time_tampering_is_rejected() -> None:
    model = SpyModel()
    run = _run(model)
    request = replace(
        run.execution_requests[0],
        request_id="",
        fill_reference_policy="first_orderbook_after_decision",
        fill_reference_source="first_orderbook_after_decision",
        quote_ts=run.execution_requests[0].decision_ts,
        quote_available_at_ts=None,
    )
    fill = replace(
        run.fills[0],
        fill_id="",
        request_id=request.request_id,
        fill_reference_policy="first_orderbook_after_decision",
        fill_reference_source="first_orderbook_after_decision",
        quote_ts=run.fills[0].decision_ts,
        quote_available_at_ts=None,
    )
    corrupted = replace(run, execution_requests=(request,), fills=(fill,))

    with pytest.raises(
        ExecutionEvidenceError, match="orderbook_quote_knowledge_time_missing"
    ):
        validate_execution_evidence(
            run=corrupted,
            timing=ExecutionTimingPolicy(
                fill_reference_policy="next_candle_open",
                allow_same_candle_close_fill=False,
            ),
            model=model,
        )


def test_validation_bound_evidence_rejects_schema_downgrade_and_future_version() -> (
    None
):
    model = SpyModel()
    run = _run(model)
    timing = ExecutionTimingPolicy(
        fill_reference_policy="next_candle_open",
        allow_same_candle_close_fill=False,
    )
    evidence = run.execution_event_summary
    assert isinstance(evidence, dict)

    evidence["execution_evidence_schema_version"] = 1
    with pytest.raises(
        ExecutionEvidenceError,
        match="validation_bound_execution_evidence_requires_schema_version:3",
    ):
        validate_execution_evidence(run=run, timing=timing, model=model)

    evidence["execution_evidence_schema_version"] = 999
    with pytest.raises(
        ExecutionEvidenceError,
        match="unsupported_execution_evidence_schema_version:999",
    ):
        validate_execution_evidence(
            run=run, timing=timing, model=model, validation_bound=False
        )


def test_schema_two_evidence_is_explicitly_read_only() -> None:
    model = SpyModel()
    run = _run(model)
    evidence = run.execution_event_summary
    assert isinstance(evidence, dict)
    evidence["execution_evidence_schema_version"] = 2
    for field in (
        "decision_timeline_invariant_status",
        "causal_timeline_validator",
        "market_knowledge_time_policy",
        "market_knowledge_time_basis_counts",
        "market_knowledge_time_assumption_count",
    ):
        evidence.pop(field, None)

    result = validate_execution_evidence(
        run=run,
        timing=ExecutionTimingPolicy(
            fill_reference_policy="next_candle_open",
            allow_same_candle_close_fill=False,
        ),
        model=model,
        validation_bound=False,
    )

    assert result["status"] == "LEGACY_READ_ONLY"
