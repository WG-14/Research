from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from market_research.research.multi_asset.expression import Direction
from market_research.research.multi_asset.public_integrated_profile import (
    PublicIntegratedProfileError,
    PublicIntegratedProfileReceipt,
    PublicT04Inputs,
    build_public_t04_fixture_inputs,
    build_public_t04_inputs,
    run_public_integrated_profile,
)


_SOURCE_HASH = "sha256:" + ("7" * 64)


def _inputs() -> PublicT04Inputs:
    return build_public_t04_fixture_inputs(
        source_document_id="institutional.t04.conformance",
        source_document_hashes=(_SOURCE_HASH,),
        opened_at="2026-01-02T12:00:00Z",
        closed_at="2026-01-02T13:00:00Z",
    )


def _rebuild(
    inputs: PublicT04Inputs,
    *,
    research: object | None = None,
    execution: object | None = None,
    stress: object | None = None,
) -> PublicT04Inputs:
    return build_public_t04_inputs(
        profile_id=inputs.profile_id,
        opened_at=inputs.opened_at,
        closed_at=inputs.closed_at,
        provenance=inputs.provenance,
        research=inputs.research if research is None else research,  # type: ignore[arg-type]
        execution=(
            inputs.execution if execution is None else execution  # type: ignore[arg-type]
        ),
        accounting=inputs.accounting,
        risk=inputs.risk,
        stress=inputs.stress if stress is None else stress,  # type: ignore[arg-type]
    )


def test_public_t04_profile_executes_source_owned_institutional_path() -> None:
    inputs = _inputs()
    first = run_public_integrated_profile(inputs)
    second = run_public_integrated_profile(inputs)

    assert first.content_hash == second.content_hash
    assert first.selected_quantities == (
        ("T04.OPTION.A", 1),
        ("T04.OPTION.B", 1),
    )
    assert first.selected_directions == (
        ("T04.OPTION.A", "LONG"),
        ("T04.OPTION.B", "LONG"),
    )
    assert first.final_position_quantities == (("T04.OPTION.B", Decimal("1.5")),)
    report_rows = dict(first.report_pnl_rows)
    assert report_rows["nav_identity_error"] == Decimal("0")
    assert report_rows["attribution_identity_error"] == Decimal("0")
    assert report_rows["closing_nav"] > Decimal("0")
    assert dict(first.exposure_totals)["gross_notional"] > Decimal("0")
    assert first.breaches and first.breaches[0].startswith("DRAWDOWN:")
    assert {
        "advanced_accounting",
        "collateral_waterfall",
        "constrained_scenarios",
        "dynamic_execution",
        "extended_exposure",
        "final_ledger",
        "funding_fx",
        "generated_path",
        "ledger_reconciliation",
        "lifecycle_chain",
        "optimization",
        "path_stress",
        "portfolio_exposure",
        "report_reconciliation",
    }.issubset(dict(first.component_hashes))
    assert {
        "LIFECYCLE:HEDGE",
        "LIFECYCLE:REBALANCE",
        "LIFECYCLE:ROLL_EXPIRY",
        "LIFECYCLE:PARTIAL_UNWIND",
        "SEQUENTIAL_PARTIAL_UNWIND",
        "DYNAMIC_RETRY_FILLED",
        "INDEPENDENT_LEDGER_REPORT_RECONCILIATION",
    }.issubset(first.actions)
    assert "EXTERNALLY_PREPARED_IMMUTABLE_SYNTHETIC_FIXTURE" in first.quality_flags
    assert first.as_dict()["content_hash"] == first.content_hash


def test_public_t04_canonicalizes_higher_order_policy_source_hashes() -> None:
    inputs = build_public_t04_fixture_inputs(
        source_document_id="hash.order.regression",
        source_document_hashes=("sha256:" + ("2" * 64),),
        opened_at="2026-01-02T12:00:00Z",
        closed_at="2026-01-02T13:00:00Z",
    )
    policy = inputs.risk.higher_order_policy

    assert policy.source_hash < policy.content_hash
    assert run_public_integrated_profile(inputs).content_hash.startswith("sha256:")


def test_public_t04_rejects_infeasible_joint_optimizer() -> None:
    inputs = _inputs()
    constraints = replace(
        inputs.research.optimization_constraints,
        margin_limit=Decimal("0"),
    )
    research = replace(
        inputs.research,
        optimization_constraints=constraints,
    )

    with pytest.raises(
        PublicIntegratedProfileError,
        match="public_t04_optimizer_infeasible:.*MARGIN_LIMIT_EXCEEDED",
    ):
        run_public_integrated_profile(_rebuild(inputs, research=research))


def test_public_t04_rejects_forged_receipt_and_result_injection() -> None:
    inputs = _inputs()
    receipt = run_public_integrated_profile(inputs)

    with pytest.raises(
        PublicIntegratedProfileError,
        match="public_t04_receipt_requires_factory",
    ):
        PublicIntegratedProfileReceipt(
            profile_id=receipt.profile_id,
            inputs_hash=receipt.inputs_hash,
            provenance_hash=receipt.provenance_hash,
            selected_instruments=receipt.selected_instruments,
            selected_directions=receipt.selected_directions,
            selected_quantities=receipt.selected_quantities,
            final_position_quantities=receipt.final_position_quantities,
            closing_ledger_hash=receipt.closing_ledger_hash,
            report_pnl_rows=receipt.report_pnl_rows,
            exposure_totals=receipt.exposure_totals,
            component_hashes=receipt.component_hashes,
            breaches=receipt.breaches,
            actions=receipt.actions,
            quality_flags=receipt.quality_flags,
        )

    common = {
        "profile_id": inputs.profile_id,
        "opened_at": inputs.opened_at,
        "closed_at": inputs.closed_at,
        "provenance": inputs.provenance,
        "research": inputs.research,
        "execution": inputs.execution,
        "accounting": inputs.accounting,
        "risk": inputs.risk,
        "stress": inputs.stress,
    }
    with pytest.raises(
        PublicIntegratedProfileError,
        match="caller_preselected_quantities_forbidden",
    ):
        build_public_t04_inputs(
            **common,  # type: ignore[arg-type]
            caller_preselected_quantities={"T04.OPTION.A": 1},
        )
    with pytest.raises(
        PublicIntegratedProfileError,
        match="caller_precomputed_receipts_forbidden",
    ):
        build_public_t04_inputs(
            **common,  # type: ignore[arg-type]
            caller_receipts=(receipt,),
        )


def test_public_t04_preserves_long_short_economic_directions() -> None:
    inputs = build_public_t04_fixture_inputs(
        source_document_id="institutional.t04.long.short",
        source_document_hashes=(_SOURCE_HASH,),
        opened_at="2026-01-02T12:00:00Z",
        closed_at="2026-01-02T13:00:00Z",
        leg_directions=(Direction.LONG, Direction.SHORT),
    )

    receipt = run_public_integrated_profile(inputs)

    assert receipt.selected_directions == (
        ("T04.OPTION.A", "LONG"),
        ("T04.OPTION.B", "SHORT"),
    )
    assert receipt.selected_quantities == (
        ("T04.OPTION.A", 1),
        ("T04.OPTION.B", 1),
    )


def test_public_t04_rejects_inconsistent_interleg_market_move() -> None:
    inputs = _inputs()
    first_attempt = inputs.execution.attempts[0]
    move = first_attempt.interleg_moves[0]
    inconsistent_move = replace(
        move,
        after_price=move.after_price + Decimal("1"),
    )
    execution = replace(
        inputs.execution,
        attempts=(
            replace(
                first_attempt,
                interleg_moves=(inconsistent_move,),
            ),
            inputs.execution.attempts[1],
        ),
    )

    with pytest.raises(
        PublicIntegratedProfileError,
        match="public_t04_interleg_move_after_quote_inconsistent",
    ):
        run_public_integrated_profile(_rebuild(inputs, execution=execution))


def test_public_t04_rejects_inconsistent_generated_shock() -> None:
    inputs = _inputs()
    inconsistent_observation = replace(
        inputs.stress.observations[0],
        price_returns=(("UNKNOWN.LEG", Decimal("-0.10")),),
    )
    stress = replace(
        inputs.stress,
        observations=(
            inconsistent_observation,
            inputs.stress.observations[1],
        ),
    )

    with pytest.raises(
        PublicIntegratedProfileError,
        match="scenario_price_target_not_held:UNKNOWN.LEG",
    ):
        run_public_integrated_profile(_rebuild(inputs, stress=stress))


def test_public_t04_rejects_missing_partial_retry_evidence() -> None:
    inputs = _inputs()
    first_attempt = inputs.execution.attempts[0]
    fully_liquid_second_quote = replace(
        first_attempt.quotes[1],
        ask_size=Decimal("10"),
    )
    execution = replace(
        inputs.execution,
        attempts=(
            replace(
                first_attempt,
                quotes=(
                    first_attempt.quotes[0],
                    fully_liquid_second_quote,
                ),
            ),
            inputs.execution.attempts[1],
        ),
    )

    with pytest.raises(
        PublicIntegratedProfileError,
        match="public_t04_sequential_partial_retry_not_observed",
    ):
        run_public_integrated_profile(_rebuild(inputs, execution=execution))
