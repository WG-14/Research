from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from market_research.research.derivatives.options import TransactionSide
from market_research.research.hashing import sha256_prefixed
from market_research.research.multi_asset.option_analytics import (
    EuropeanBlackScholesModel,
    OptionAnalyticsError,
    OptionModelRegistry,
    SupplierAnalyticsObservation,
)
from market_research.research.multi_asset.public_option_profile import (
    PublicOptionInputProvenance,
    PublicOptionLifecycleKind,
    PublicOptionProfileError,
    PublicOptionProfileInput,
    build_public_t03_fixture_inputs,
    default_public_option_institutional_factory,
    run_public_option_profile,
)


OBSERVED_AT = "2026-01-02T12:00:00Z"
KNOWN_AT = "2026-01-02T12:00:02Z"
VALUATION_AT = "2026-01-02T12:00:10Z"


def _hash(token: str) -> str:
    return sha256_prefixed(token, label="public_t03_profile_test")


def _inputs() -> PublicOptionProfileInput:
    return build_public_t03_fixture_inputs(
        source_document_id="document.public.t03",
        source_document_hashes=(_hash("source-document"),),
        observation_at=OBSERVED_AT,
        knowledge_at=KNOWN_AT,
        valuation_at=VALUATION_AT,
    )


def test_public_t03_fixture_exact_overrides_preserve_source_economics() -> None:
    inputs = build_public_t03_fixture_inputs(
        source_document_id="document.public.t03.exact",
        source_document_hashes=(_hash("source-document-exact"),),
        observation_at=OBSERVED_AT,
        knowledge_at=KNOWN_AT,
        valuation_at=VALUATION_AT,
        repricing_observation_at="2026-03-01T11:59:50Z",
        repricing_knowledge_at="2026-03-01T11:59:58Z",
        repricing_valuation_at="2026-03-01T12:00:00Z",
        repricing_quotes=(
            (Decimal("13.1"), Decimal("13.3")),
            (Decimal("10.7"), Decimal("10.9")),
            (Decimal("4.6"), Decimal("4.8")),
        ),
        repricing_spot_price=Decimal("103"),
        settlement_observation_at="2026-07-01T12:00:10Z",
        settlement_knowledge_at="2026-07-01T12:00:15Z",
        settlement_spot_price=Decimal("112"),
        lifecycle_event_at="2026-07-01T12:00:20Z",
        contract_expiration_at="2026-07-01T12:00:10Z",
        contract_settlement_at="2026-07-01T13:00:10Z",
        contract_multiplier=Decimal("50"),
        contract_currency="EUR",
    )

    assert {item.expiration_at for item in inputs.contracts} == {"2026-07-01T12:00:10Z"}
    assert {item.settlement_at for item in inputs.contracts} == {"2026-07-01T13:00:10Z"}
    assert {item.multiplier for item in inputs.contracts} == {Decimal("50")}
    assert {item.currency for item in inputs.contracts} == {"EUR"}
    assert inputs.market_state.base_currency == "EUR"
    assert inputs.market_state.spots[0].currency == "EUR"

    repricings = tuple(
        sorted(
            inputs.repricing_inputs,
            key=lambda item: item.valuation_input.contract.strike,
        )
    )
    assert tuple(
        (item.provider_row.bid, item.provider_row.ask) for item in repricings
    ) == (
        (Decimal("13.1"), Decimal("13.3")),
        (Decimal("10.7"), Decimal("10.9")),
        (Decimal("4.6"), Decimal("4.8")),
    )
    assert {item.market_state.valuation_at for item in repricings} == {
        "2026-03-01T12:00:00+00:00"
    }
    assert {item.provider_row.observed_at for item in repricings} == {
        "2026-03-01T11:59:50Z"
    }
    assert {item.provider_row.available_at for item in repricings} == {
        "2026-03-01T11:59:58Z"
    }
    assert {item.valuation_input.spot_price for item in repricings} == {Decimal("103")}

    assert inputs.lifecycle.event_at == "2026-07-01T12:00:20Z"
    assert {item.settlement_at for item in inputs.lifecycle.settlement_inputs} == {
        "2026-07-01T12:00:10Z"
    }
    assert {
        item.availability.processed_at for item in inputs.lifecycle.settlement_inputs
    } == {"2026-07-01T12:00:15Z"}
    assert {item.spot_price for item in inputs.lifecycle.settlement_inputs} == {
        Decimal("112")
    }

    receipt = run_public_option_profile(
        receipt_id="receipt.public.t03.exact",
        inputs=inputs,
        factory=default_public_option_institutional_factory(),
    )
    receipt.require_valid()
    assert receipt.fill.contract.multiplier == Decimal("50")
    assert receipt.ledger.base_currency == "EUR"
    assert receipt.lifecycle_hash


def test_public_t03_fixture_defaults_equal_explicit_legacy_values() -> None:
    implicit = _inputs()
    explicit = build_public_t03_fixture_inputs(
        source_document_id="document.public.t03",
        source_document_hashes=(_hash("source-document"),),
        observation_at=OBSERVED_AT,
        knowledge_at=KNOWN_AT,
        valuation_at=VALUATION_AT,
        repricing_observation_at="2026-01-03T12:00:00Z",
        repricing_knowledge_at="2026-01-03T12:00:08Z",
        repricing_valuation_at="2026-01-03T12:00:10Z",
        repricing_quotes=(
            (Decimal("12.3"), Decimal("12.5")),
            (Decimal("10.3"), Decimal("10.5")),
            (Decimal("4.3"), Decimal("4.5")),
        ),
        repricing_spot_price=Decimal("101"),
        settlement_observation_at="2026-07-01T12:00:10Z",
        settlement_knowledge_at="2026-07-01T12:00:20Z",
        settlement_spot_price=Decimal("105"),
        lifecycle_event_at="2026-07-01T13:00:10Z",
        contract_expiration_at="2026-07-01T12:00:10Z",
        contract_settlement_at="2026-07-01T13:00:10Z",
        contract_multiplier=Decimal("100"),
        contract_currency="USD",
    )

    assert explicit == implicit
    assert explicit.content_hash == implicit.content_hash


def test_public_t03_fixture_rejects_override_length_and_clock_mismatches() -> None:
    with pytest.raises(
        PublicOptionProfileError,
        match="repricing_quote_grid_invalid",
    ):
        build_public_t03_fixture_inputs(
            source_document_id="document.public.t03.invalid",
            source_document_hashes=(_hash("source-document-invalid"),),
            observation_at=OBSERVED_AT,
            knowledge_at=KNOWN_AT,
            valuation_at=VALUATION_AT,
            repricing_quotes=((Decimal("1"), Decimal("2")),),
        )

    with pytest.raises(
        PublicOptionProfileError,
        match="repricing_clock_order_invalid",
    ):
        build_public_t03_fixture_inputs(
            source_document_id="document.public.t03.invalid",
            source_document_hashes=(_hash("source-document-invalid"),),
            observation_at=OBSERVED_AT,
            knowledge_at=KNOWN_AT,
            valuation_at=VALUATION_AT,
            repricing_observation_at="2026-01-03T12:00:09Z",
            repricing_knowledge_at="2026-01-03T12:00:08Z",
        )

    with pytest.raises(
        PublicOptionProfileError,
        match="settlement_clock_order_invalid",
    ):
        build_public_t03_fixture_inputs(
            source_document_id="document.public.t03.invalid",
            source_document_hashes=(_hash("source-document-invalid"),),
            observation_at=OBSERVED_AT,
            knowledge_at=KNOWN_AT,
            valuation_at=VALUATION_AT,
            settlement_knowledge_at="2026-07-01T13:00:20Z",
            lifecycle_event_at="2026-07-01T13:00:10Z",
        )

    with pytest.raises(
        PublicOptionProfileError,
        match="contract_clock_order_invalid",
    ):
        build_public_t03_fixture_inputs(
            source_document_id="document.public.t03.invalid",
            source_document_hashes=(_hash("source-document-invalid"),),
            observation_at=OBSERVED_AT,
            knowledge_at=KNOWN_AT,
            valuation_at=VALUATION_AT,
            contract_expiration_at=VALUATION_AT,
        )


def test_public_t03_runs_source_owned_path_and_returns_factory_receipt() -> None:
    inputs = _inputs()
    factory = default_public_option_institutional_factory()
    assert factory == default_public_option_institutional_factory(
        fill_side=TransactionSide.BUY
    )

    first = run_public_option_profile(
        receipt_id="receipt.public.t03",
        inputs=inputs,
        factory=factory,
    )
    second = run_public_option_profile(
        receipt_id="receipt.public.t03",
        inputs=inputs,
        factory=factory,
    )

    assert first == second
    assert first.content_hash == second.content_hash
    assert first.provenance is (
        PublicOptionInputProvenance.EXTERNALLY_PREPARED_IMMUTABLE_SYNTHETIC_FIXTURE
    )
    assert first.selected_contract_id == "option.public.t03.call.100"
    assert len(first.selection_decision.eligible_contract_ids) >= 2
    assert first.executed_side is TransactionSide.BUY
    assert first.fill.side is TransactionSide.BUY
    assert first.filled_quantity == Decimal("1")
    assert first.fill.contract.contract_id == first.selected_contract_id
    assert first.surface.repair_count > 0
    assert any(not item.passed for item in first.surface.pre_repair_diagnostics)
    assert all(item.passed for item in first.surface.post_repair_diagnostics)
    assert first.raw_provider_row_hashes
    assert first.normalized_quote_hashes
    assert first.forward_receipt_hashes
    assert first.repricing_analytics_receipts
    assert first.attribution.reconciled
    assert first.attribution.actual_pnl == first.attribution_actual_pnl
    assert first.ledger_event_count == len(first.ledger.events)
    assert not first.ledger.replay().positions
    assert {
        "black_scholes_result_hash",
        "black_76_result_hash",
        "american_crr_result_hash",
        "american_richardson_result_hash",
        "asian_result_hash",
        "asian_path_grid_variant_hash",
    } <= dict(first.model_conformance_hashes).keys()
    assert {
        "STATIC_ARBITRAGE_REPAIRED",
        "SELF_IV_AND_GREEKS_DERIVED",
        "AMERICAN_MODEL_BRANCH_VALIDATED",
        "ASIAN_EXOTIC_MODEL_BRANCH_VALIDATED",
        "CONTRACT_SELECTED_FROM_COMPETING_CHAIN",
        "INTERMEDIATE_REPRICING_BOUND",
        "ATTRIBUTION_RECONCILED",
        "LIFECYCLE_LEDGER_RECONCILED",
    } <= set(first.quality_flags)
    assert first.as_dict()["content_hash"] == first.content_hash
    first.require_valid()

    with pytest.raises(
        PublicOptionProfileError,
        match="requires_institutional_factory",
    ):
        replace(first, receipt_id="receipt.public.t03.forged")


@pytest.mark.parametrize(
    ("fill_side", "expected_position_quantity"),
    (
        (TransactionSide.BUY, Decimal("1")),
        (TransactionSide.SELL, Decimal("-1")),
    ),
)
def test_public_t03_binds_execution_direction_through_receipt(
    fill_side: TransactionSide,
    expected_position_quantity: Decimal,
) -> None:
    factory = default_public_option_institutional_factory(fill_side=fill_side)
    receipt = run_public_option_profile(
        receipt_id=f"receipt.public.t03.{fill_side.value.lower()}",
        inputs=_inputs(),
        factory=factory,
    )

    assert receipt.executed_side is fill_side
    assert receipt.fill.side is fill_side
    assert receipt.attribution.position_quantity == expected_position_quantity
    assert receipt.as_dict()["executed_side"] == fill_side.value
    assert receipt.factory_hash == factory.content_hash
    assert not receipt.ledger.replay().positions
    receipt.require_valid()


def test_public_t03_factory_hash_binds_execution_direction() -> None:
    buy = default_public_option_institutional_factory()
    explicit_buy = default_public_option_institutional_factory(
        fill_side=TransactionSide.BUY
    )
    sell = default_public_option_institutional_factory(fill_side=TransactionSide.SELL)

    assert buy == explicit_buy
    assert buy.content_hash == explicit_buy.content_hash
    assert sell.content_hash != buy.content_hash


def test_public_t03_rejects_tampered_supplier_analytics() -> None:
    inputs = _inputs()
    tampered = SupplierAnalyticsObservation(
        provider_id="supplier.untrusted",
        contract_id="option.public.t03.call.100",
        observed_at=OBSERVED_AT,
        source_hash=_hash("tampered-supplier"),
        implied_volatility=Decimal("4"),
        delta=Decimal("-1"),
        gamma=Decimal("1"),
    )

    with pytest.raises(OptionAnalyticsError, match="tolerance_exceeded"):
        run_public_option_profile(
            receipt_id="receipt.public.t03.tampered",
            inputs=replace(inputs, supplier_observations=(tampered,)),
            factory=default_public_option_institutional_factory(),
        )


def test_public_t03_rejects_unrepairable_static_arbitrage() -> None:
    inputs = _inputs()
    factory = default_public_option_institutional_factory()
    fail_closed = replace(
        factory,
        surface_policy=replace(
            factory.surface_policy,
            maximum_repair_price_residual=Decimal("0"),
        ),
    )

    with pytest.raises(OptionAnalyticsError, match="repair_residual_exceeded"):
        run_public_option_profile(
            receipt_id="receipt.public.t03.unrepairable",
            inputs=inputs,
            factory=fail_closed,
        )


def test_public_t03_rejects_missing_required_model_branch() -> None:
    inputs = _inputs()
    factory = replace(
        default_public_option_institutional_factory(),
        model_registry=OptionModelRegistry(
            (EuropeanBlackScholesModel(),),
            registry_version="public_t03_incomplete_registry_test",
        ),
    )

    with pytest.raises(OptionAnalyticsError, match="registry_resolution_failed"):
        run_public_option_profile(
            receipt_id="receipt.public.t03.unsupported-model",
            inputs=inputs,
            factory=factory,
        )


def test_public_t03_rejects_unsupported_trading_lifecycle() -> None:
    inputs = _inputs()
    unsupported = replace(
        inputs,
        lifecycle=replace(
            inputs.lifecycle,
            kind=PublicOptionLifecycleKind.EARLY_EXERCISE,
        ),
    )

    with pytest.raises(PublicOptionProfileError, match="lifecycle_unsupported"):
        run_public_option_profile(
            receipt_id="receipt.public.t03.unsupported-lifecycle",
            inputs=unsupported,
            factory=default_public_option_institutional_factory(),
        )
