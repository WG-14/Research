from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from market_research.research.hashing import sha256_prefixed
from market_research.research.multi_asset.futures_delivery import (
    FuturesSettlementMode,
)
from market_research.research.multi_asset.public_spot_futures_profile import (
    InputFixtureSemantics,
    PublicFuturesProfileInputs,
    PublicProfileError,
    PublicSpotProfileInputs,
    build_public_t01_inputs,
    build_public_t02_inputs,
    run_public_t01_spot_profile,
    run_public_t02_futures_profile,
)


def _hash(label: str) -> str:
    return sha256_prefixed(label, label="public-profile-focused-test")


def _spot_inputs() -> PublicSpotProfileInputs:
    return build_public_t01_inputs(
        source_document_id="document.public.t01",
        source_document_hashes=(_hash("t01-document"),),
        observation_at="2026-01-02T10:00:00+00:00",
        valuation_at="2026-01-20T10:00:00+00:00",
        knowledge_at="2026-01-20T10:00:00+00:00",
        instrument_id="instrument.public.acme",
        currency="USD",
        entry_price=Decimal("100"),
        quantity=Decimal("100"),
    )


def _futures_inputs(
    *,
    settlement_mode: FuturesSettlementMode = FuturesSettlementMode.CASH,
    quantity: Decimal = Decimal("2"),
) -> PublicFuturesProfileInputs:
    return build_public_t02_inputs(
        source_document_id="document.public.t02",
        source_document_hashes=(_hash("t02-document"),),
        observation_at="2026-01-01T10:00:00+00:00",
        valuation_at="2026-01-03T10:00:00+00:00",
        knowledge_at="2026-01-03T09:00:00+00:00",
        underlying_instrument_id="instrument.public.index",
        root_id="root.public.index",
        near_contract_id="future.public.near",
        selected_contract_id="future.public.selected",
        currency="USD",
        entry_price=Decimal("100"),
        final_price=Decimal("105"),
        multiplier=Decimal("50"),
        quantity=quantity,
        settlement_mode=settlement_mode,
    )


def test_t01_executes_source_owned_spot_institutional_path() -> None:
    inputs = _spot_inputs()
    first = run_public_t01_spot_profile(inputs)
    second = run_public_t01_spot_profile(inputs)

    assert first.fixture_semantics is (
        InputFixtureSemantics.EXTERNALLY_PREPARED_IMMUTABLE_SYNTHETIC_FIXTURE
    )
    assert first.resolved_instrument_id == "instrument.public.acme"
    assert len(first.normalization_receipt_hashes) == 2
    assert len(first.corporate_action_hashes) == 3
    assert dict(first.final_position_quantities) == {
        "instrument.public.acme.borrow": Decimal("-5"),
        "instrument.public.acme.subscribed": Decimal("10"),
        "instrument.public.acme.successor": Decimal("50"),
    }
    assert first.capacity_study_hash.startswith("sha256:")
    assert first.extended_exposure_hash.startswith("sha256:")
    assert first.economic_evidence_hash.startswith("sha256:")
    assert first.attribution_hash.startswith("sha256:")
    assert first.content_hash == second.content_hash
    assert first.as_dict()["content_hash"] == first.content_hash
    with pytest.raises(PublicProfileError, match="spot_receipt_factory_only"):
        replace(first, profile_id="forged.profile")


def test_t01_rejects_forged_identity_and_provider_economic_meaning() -> None:
    inputs = _spot_inputs()
    with pytest.raises(PublicProfileError, match="forged_instrument_preselection"):
        run_public_t01_spot_profile(
            replace(inputs, claimed_instrument_id="instrument.forged")
        )

    direct = inputs.provider_conventions[0]
    changed_row = replace(
        direct.row,
        fields={**direct.row.fields, "price": "101"},
    )
    conventions = (
        replace(direct, row=changed_row),
        inputs.provider_conventions[1],
    )
    with pytest.raises(PublicProfileError, match="provider_economic_meaning_mismatch"):
        run_public_t01_spot_profile(replace(inputs, provider_conventions=conventions))


@pytest.mark.parametrize(
    "settlement_mode",
    (FuturesSettlementMode.CASH, FuturesSettlementMode.PHYSICAL),
)
def test_t02_selects_actual_contract_and_runs_margin_delivery_and_stress(
    settlement_mode: FuturesSettlementMode,
) -> None:
    inputs = _futures_inputs(settlement_mode=settlement_mode)
    first = run_public_t02_futures_profile(inputs)
    second = run_public_t02_futures_profile(inputs)

    assert first.selected_contract_id == "future.public.selected"
    assert first.selected_contract_multiplier == Decimal("50")
    assert first.position_quantity == Decimal("2")
    assert first.prior_settlement_price == Decimal("100")
    assert first.final_settlement_price == Decimal("105")
    assert first.resolved_contract_version == 2
    assert first.variation_margin == Decimal("-100")
    expected_lifecycle_cash = (
        Decimal("500")
        if settlement_mode is FuturesSettlementMode.CASH
        else Decimal("-10155")
    )
    assert first.lifecycle_cash_delta == expected_lifecycle_cash
    assert first.final_realized_pnl == (("USD", Decimal("400")),)
    assert first.pre_settlement_ledger_hash != first.final_ledger_hash
    assert first.extended_exposure_hash.startswith("sha256:")
    assert first.economic_evidence_hash.startswith("sha256:")
    assert first.provenance_hash.startswith("sha256:")
    assert (first.ctd_decision_hash is not None) == (
        settlement_mode is FuturesSettlementMode.PHYSICAL
    )
    assert first.content_hash == second.content_hash
    with pytest.raises(PublicProfileError, match="futures_receipt_factory_only"):
        replace(first, profile_id="forged.profile")


def test_t02_preserves_short_position_direction() -> None:
    receipt = run_public_t02_futures_profile(_futures_inputs(quantity=Decimal("-2")))

    assert receipt.position_quantity == Decimal("-2")
    assert receipt.variation_margin == Decimal("-100")
    assert receipt.lifecycle_cash_delta == Decimal("-500")
    assert receipt.final_realized_pnl == (("USD", Decimal("-600")),)


def test_t02_rejects_preselection_multiplier_and_future_knowledge_bypasses() -> None:
    inputs = _futures_inputs()
    with pytest.raises(PublicProfileError, match="forged_contract_preselection"):
        run_public_t02_futures_profile(
            replace(
                inputs,
                claimed_selected_contract_id="future.public.near",
            )
        )
    with pytest.raises(PublicProfileError, match="forged_contract_multiplier"):
        run_public_t02_futures_profile(
            replace(inputs, claimed_contract_multiplier=Decimal("500"))
        )
    with pytest.raises(PublicProfileError, match="futures_future_knowledge"):
        run_public_t02_futures_profile(
            replace(
                inputs,
                knowledge_at="2026-01-04T10:00:00+00:00",
            )
        )

    selected_candidate = inputs.quote_candidates[1]
    forged_contract = replace(
        selected_candidate.contract,
        contract_multiplier=Decimal("51"),
        tick_value=Decimal("0.51"),
    )
    forged_candidates = (
        inputs.quote_candidates[0],
        replace(selected_candidate, contract=forged_contract),
    )
    with pytest.raises(
        PublicProfileError, match="candidate_contract_not_authoritative"
    ):
        run_public_t02_futures_profile(
            replace(inputs, quote_candidates=forged_candidates)
        )
