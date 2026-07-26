from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from market_research.research.hashing import sha256_prefixed
from market_research.research.instrument_kinds import InstrumentKind
from market_research.research.market_calendar_contract import (
    MARKET_CALENDAR_SCHEMA_VERSION,
    MarketCalendarAuthority,
)
from market_research.research.multi_asset.data import (
    AppendOnlyBitemporalStore,
    BitemporalRecord,
    DataLayer,
    DataLineage,
    DerivedLayerMetadata,
    derived_input_snapshot_hash,
    MultiAssetDataError,
    NormalizedLayerMetadata,
    ObservationClocks,
    RawLayerMetadata,
)
from market_research.research.multi_asset.domain import (
    ContractSpecification,
    EconomicUnderlying,
    EffectivePeriod,
    Instrument,
    InstrumentRegistry,
    InstrumentRelationship,
    InstrumentRelationshipType,
    InstrumentTradingStatus,
    Issuer,
    LifecycleEvent,
    LifecycleEventType,
    Listing,
    ProductMasterError,
    ProductMasterHistory,
    ProductMasterRevision,
    SettlementType,
    SourceReference,
    SymbolAlias,
)
from market_research.research.multi_asset.market_state import (
    BorrowQuote,
    CurvePoint,
    DividendYieldAssumption,
    FXQuote,
    FundingRateQuote,
    FuturesContractQuote,
    FuturesCurveState,
    LiquidityQuote,
    MarketDataQuality,
    MarketState,
    MarketStateError,
    ObservationMetadata,
    OptionAnalyticsMark,
    OptionChainState,
    OptionContractQuote,
    OptionRight,
    QuoteCondition,
    RateQuote,
    SpotQuote,
    VolatilityPoint,
    VolatilitySurface,
    YieldCurve,
)


def _hash(char: str) -> str:
    return "sha256:" + char * 64


def _period() -> EffectivePeriod:
    return EffectivePeriod("2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00")


def _source(
    char: str = "a", *, observed_at: str = "2025-12-01T00:00:00+00:00"
) -> SourceReference:
    return SourceReference(
        source_id="reviewed_product_master",
        source_version="v1",
        content_hash=_hash(char),
        observed_at=observed_at,
        source_uri="/var/lib/market-research-inputs/product-master-v1.json",
    )


def _registry() -> InstrumentRegistry:
    period = _period()
    source = _source()
    underlying = EconomicUnderlying(
        underlying_id="underlying_btc",
        name="Bitcoin economic underlying",
        asset_class="digital_asset",
        unit="coin",
        currency="KRW",
        validity=period,
        source=source,
    )
    issuer = Issuer(
        issuer_id="issuer_reviewed_exchange",
        legal_name="Reviewed Research Issuer",
        jurisdiction="KR",
        validity=period,
        source=source,
    )
    spot = Instrument(
        instrument_id="inst_btc_spot",
        kind=InstrumentKind.SPOT,
        name="BTC spot reference",
        economic_underlying_id=underlying.underlying_id,
        issuer_id=issuer.issuer_id,
        currency="KRW",
        unit="coin",
        validity=period,
        source=source,
        primary_listing_id="listing_btc_spot_xoff",
    )
    future = Instrument(
        instrument_id="inst_btc_future_dec26",
        kind=InstrumentKind.FUTURE,
        name="BTC December 2026 future",
        economic_underlying_id=underlying.underlying_id,
        issuer_id=issuer.issuer_id,
        currency="KRW",
        unit="contract",
        validity=period,
        source=source,
    )
    option = Instrument(
        instrument_id="inst_btc_option_dec26",
        kind=InstrumentKind.OPTION,
        name="Option delivering BTC December 2026 future",
        economic_underlying_id=underlying.underlying_id,
        issuer_id=issuer.issuer_id,
        currency="KRW",
        unit="contract",
        validity=period,
        source=source,
    )
    future_specification = ContractSpecification(
        contract_specification_id="contract_spec_btc_future_dec26",
        instrument_id=future.instrument_id,
        contract_multiplier=Decimal("1"),
        contract_unit="coin",
        settlement_type=SettlementType.CASH,
        settlement_currency="KRW",
        expiry_at="2026-12-18T08:00:00+00:00",
        last_trade_at="2026-12-18T07:55:00+00:00",
        minimum_tick=Decimal("0.01"),
        tick_value=Decimal("0.01"),
        trading_currency="KRW",
        calendar_id="calendar_xoff",
        lifecycle_rule_id="future.cash.expiry.v1",
        validity=period,
        source=source,
    )
    option_specification = ContractSpecification(
        contract_specification_id="contract_spec_btc_option_dec26",
        instrument_id=option.instrument_id,
        contract_multiplier=Decimal("1"),
        contract_unit="future_contract",
        settlement_type=SettlementType.PHYSICAL,
        settlement_currency="KRW",
        expiry_at="2026-11-20T08:00:00+00:00",
        last_trade_at="2026-11-20T07:55:00+00:00",
        exercise_style="EUROPEAN",
        minimum_tick=Decimal("0.01"),
        tick_value=Decimal("0.01"),
        trading_currency="KRW",
        calendar_id="calendar_xoff",
        lifecycle_rule_id="option.european.physical.v1",
        validity=period,
        source=source,
    )
    relationships = (
        InstrumentRelationship(
            relationship_id="rel_future_underlying",
            source_instrument_id=future.instrument_id,
            target_instrument_id=spot.instrument_id,
            relationship_type=InstrumentRelationshipType.FUTURE_UNDERLYING,
            validity=period,
            source=source,
        ),
        InstrumentRelationship(
            relationship_id="rel_option_underlying",
            source_instrument_id=option.instrument_id,
            target_instrument_id=future.instrument_id,
            relationship_type=InstrumentRelationshipType.OPTION_UNDERLYING,
            validity=period,
            source=source,
        ),
        InstrumentRelationship(
            relationship_id="rel_option_delivers_future",
            source_instrument_id=option.instrument_id,
            target_instrument_id=future.instrument_id,
            relationship_type=(InstrumentRelationshipType.FUTURE_OPTION_DELIVERABLE),
            quantity_ratio=Decimal("1"),
            validity=period,
            source=source,
        ),
    )
    listing = Listing(
        listing_id="listing_btc_spot_xoff",
        instrument_id=spot.instrument_id,
        venue_mic="XOFF",
        symbol="KRW-BTC",
        trading_currency="KRW",
        price_unit="KRW_per_coin",
        quantity_unit="coin",
        calendar_id="calendar_xoff",
        validity=period,
        source=source,
        market_segment="OFF_EXCHANGE_REFERENCE",
        session_id="CONTINUOUS",
        lot_size=Decimal("0.0001"),
    )
    alias = SymbolAlias(
        alias_id="alias_btc_vendor",
        instrument_id=spot.instrument_id,
        listing_id=listing.listing_id,
        provider_id="prepared_vendor",
        symbol="XBTKRW",
        validity=period,
        source=source,
    )
    event = LifecycleEvent(
        event_id="event_future_expiry",
        instrument_id=future.instrument_id,
        event_type=LifecycleEventType.EXPIRY,
        effective_at="2026-12-18T08:00:00+00:00",
        knowledge_at="2026-11-02T00:00:00+00:00",
        contract_specification_id=(future_specification.contract_specification_id),
        validity=period,
        source=_source("b", observed_at="2026-11-02T00:00:00+00:00"),
    )
    return InstrumentRegistry(
        economic_underlyings=(underlying,),
        issuers=(issuer,),
        instruments=(spot, future, option),
        listings=(listing,),
        contract_specifications=(future_specification, option_specification),
        symbol_aliases=(alias,),
        lifecycle_events=(event,),
        relationships=relationships,
    )


def test_product_master_resolves_alias_and_typed_future_option_deliverable() -> None:
    registry = _registry()
    as_of = "2026-06-01T00:00:00+00:00"

    assert registry.schema_version == 3
    assert (
        registry.resolve_symbol(
            provider_id="prepared_vendor", symbol="XBTKRW", as_of=as_of
        ).instrument_id
        == "inst_btc_spot"
    )
    deliverable = registry.relationship_targets(
        source_instrument_id="inst_btc_option_dec26",
        relationship_type=InstrumentRelationshipType.FUTURE_OPTION_DELIVERABLE,
        as_of=as_of,
    )
    assert [item.kind for item in deliverable] == [InstrumentKind.FUTURE]
    assert registry.contract_hash().startswith("sha256:")
    assert (
        registry.tradable_instrument_as_of("inst_btc_spot", as_of)
        == registry.instruments[0]
    )
    suspended = replace(
        registry.instruments[0],
        trading_status=InstrumentTradingStatus.SUSPENDED,
    )
    assert (
        replace(
            registry,
            instruments=(suspended, *registry.instruments[1:]),
        ).tradable_instrument_as_of(suspended.instrument_id, as_of)
        is None
    )
    assert (
        replace(
            registry,
            listings=(),
            symbol_aliases=(),
            instruments=(
                replace(registry.instruments[0], primary_listing_id=None),
                *registry.instruments[1:],
            ),
        ).tradable_instrument_as_of("inst_btc_spot", as_of)
        is None
    )

    reordered = InstrumentRegistry(
        economic_underlyings=registry.economic_underlyings,
        issuers=registry.issuers,
        instruments=tuple(reversed(registry.instruments)),
        listings=registry.listings,
        contract_specifications=tuple(reversed(registry.contract_specifications)),
        symbol_aliases=registry.symbol_aliases,
        lifecycle_events=registry.lifecycle_events,
        relationships=tuple(reversed(registry.relationships)),
    )
    assert reordered.contract_hash() == registry.contract_hash()


def test_product_master_rejects_incomplete_derivative_economic_terms() -> None:
    registry = _registry()
    incomplete_future = replace(
        registry.contract_specifications[0],
        minimum_tick=None,
        tick_value=None,
    )

    with pytest.raises(
        ProductMasterError,
        match="derivative_contract_specification_economic_terms_required",
    ):
        replace(
            registry,
            contract_specifications=(
                incomplete_future,
                registry.contract_specifications[1],
            ),
        )
    with pytest.raises(
        ProductMasterError,
        match="option_contract_specification_exercise_style_required",
    ):
        replace(
            registry,
            contract_specifications=(
                registry.contract_specifications[0],
                replace(
                    registry.contract_specifications[1],
                    exercise_style=None,
                ),
            ),
        )
    with pytest.raises(
        ProductMasterError,
        match="derivative_underlying_relationship_required",
    ):
        replace(
            registry,
            relationships=tuple(
                item
                for item in registry.relationships
                if item.source_instrument_id != "inst_btc_future_dec26"
            ),
        )


def test_product_master_preserves_pre_correction_value_by_knowledge_time() -> None:
    registry = _registry()
    as_of = "2026-06-01T00:00:00+00:00"
    before_revision = "2026-06-30T23:59:59+00:00"
    revision_known = "2026-07-01T00:00:00+00:00"
    late_source = replace(
        _source("c", observed_at=revision_known),
        source_version="v2",
    )

    revised_spot = replace(registry.instruments[0], source=late_source)
    revised_listing = replace(registry.listings[0], source=late_source)
    revised_alias = replace(registry.symbol_aliases[0], source=late_source)
    revised_future_specification = replace(
        registry.contract_specifications[0],
        source=late_source,
    )
    revised_future_relationship = replace(
        registry.relationships[0],
        source=late_source,
    )
    revised = replace(
        registry,
        instruments=(revised_spot, *registry.instruments[1:]),
        listings=(revised_listing,),
        contract_specifications=(
            revised_future_specification,
            registry.contract_specifications[1],
        ),
        symbol_aliases=(revised_alias,),
        relationships=(
            revised_future_relationship,
            *registry.relationships[1:],
        ),
    )
    original_revision = ProductMasterRevision(
        revision_id="product_master.v1",
        recorded_at="2025-12-01T00:00:00+00:00",
        registry=registry,
    )
    corrected_revision = ProductMasterRevision(
        revision_id="product_master.v2",
        recorded_at=revision_known,
        registry=revised,
        supersedes_hash=original_revision.revision_hash(),
        revision_reason="late_vendor_identifier_correction",
    )
    history = ProductMasterHistory((original_revision, corrected_revision))

    assert (
        history.instrument_as_of(
            revised_spot.instrument_id,
            as_of,
            knowledge_at=before_revision,
        )
        == registry.instruments[0]
    )
    assert (
        history.resolve_symbol(
            provider_id="prepared_vendor",
            symbol="XBTKRW",
            as_of=as_of,
            knowledge_at=before_revision,
        )
        == registry.instruments[0]
    )
    assert history.relationship_targets(
        source_instrument_id="inst_btc_future_dec26",
        relationship_type=InstrumentRelationshipType.FUTURE_UNDERLYING,
        as_of=as_of,
        knowledge_at=before_revision,
    ) == (registry.instruments[0],)
    assert (
        history.contract_specification_as_of(
            "inst_btc_future_dec26",
            as_of,
            knowledge_at=before_revision,
        )
        == registry.contract_specifications[0]
    )
    assert history.revision_as_known(before_revision) == original_revision
    assert history.contract_hash_as_known(before_revision) != (
        history.contract_hash_as_known(revision_known)
    )

    assert (
        history.instrument_as_of(
            revised_spot.instrument_id,
            as_of,
            knowledge_at=revision_known,
        )
        == revised_spot
    )
    assert (
        history.resolve_symbol(
            provider_id="prepared_vendor",
            symbol="XBTKRW",
            as_of=as_of,
            knowledge_at=revision_known,
        )
        == revised_spot
    )
    assert history.relationship_targets(
        source_instrument_id="inst_btc_future_dec26",
        relationship_type=InstrumentRelationshipType.FUTURE_UNDERLYING,
        as_of=as_of,
        knowledge_at=revision_known,
    ) == (revised_spot,)
    assert (
        history.contract_specification_as_of(
            "inst_btc_future_dec26",
            as_of,
            knowledge_at=revision_known,
        )
        == revised_future_specification
    )
    assert history.revision_as_known(revision_known) == corrected_revision
    assert history.contract_hash().startswith("sha256:")


def test_product_master_revision_history_rejects_a_broken_chain() -> None:
    first = ProductMasterRevision(
        revision_id="product_master.v1",
        recorded_at="2025-12-01T00:00:00+00:00",
        registry=_registry(),
    )
    broken = ProductMasterRevision(
        revision_id="product_master.v2",
        recorded_at="2026-07-01T00:00:00+00:00",
        registry=_registry(),
        supersedes_hash=_hash("0"),
        revision_reason="invalid_chain_for_test",
    )

    with pytest.raises(ProductMasterError, match="revision_chain_broken"):
        ProductMasterHistory((first, broken))


def test_product_master_rejects_wrong_future_option_deliverable_endpoints() -> None:
    registry = _registry()
    wrong = replace(
        registry.relationships[-1],
        source_instrument_id="inst_btc_future_dec26",
        target_instrument_id="inst_btc_option_dec26",
    )

    with pytest.raises(ProductMasterError, match="relationship_endpoint_kind_invalid"):
        replace(registry, relationships=registry.relationships[:-1] + (wrong,))


def test_product_master_enforces_validity_references_and_alias_uniqueness() -> None:
    registry = _registry()
    orphan = replace(registry.listings[0], instrument_id="inst_missing_reference")
    with pytest.raises(
        ProductMasterError, match="listing_instrument_reference_invalid"
    ):
        replace(registry, listings=(orphan,))

    ambiguous = replace(
        registry.symbol_aliases[0],
        alias_id="alias_btc_vendor_duplicate",
        instrument_id="inst_btc_future_dec26",
        listing_id=None,
    )
    with pytest.raises(ProductMasterError, match="symbol_alias_ambiguous"):
        replace(
            registry,
            symbol_aliases=registry.symbol_aliases + (ambiguous,),
        )


def test_lifecycle_events_are_filtered_by_knowledge_time() -> None:
    registry = _registry()

    assert registry.lifecycle_events_known_at("2026-10-31T23:59:59+00:00") == ()
    assert registry.lifecycle_events_known_at("2026-11-01T00:00:00+00:00") == ()
    assert [
        item.event_id
        for item in registry.lifecycle_events_known_at("2026-11-02T00:00:00+00:00")
    ] == ["event_future_expiry"]


def test_lifecycle_event_retains_economic_terms_and_required_dates() -> None:
    registry = _registry()
    dividend = LifecycleEvent(
        event_id="event_spot_cash_distribution",
        instrument_id="inst_btc_spot",
        event_type=LifecycleEventType.CASH_DISTRIBUTION,
        effective_at="2026-07-03T00:00:00+00:00",
        knowledge_at="2026-06-02T00:00:00+00:00",
        announced_at="2026-06-01T00:00:00+00:00",
        record_at="2026-07-02T00:00:00+00:00",
        payment_at="2026-07-03T00:00:00+00:00",
        cash_amount=Decimal("1.25"),
        currency="KRW",
        validity=_period(),
        source=_source("9", observed_at="2026-06-02T00:00:00+00:00"),
    )
    with_dividend = replace(
        registry,
        lifecycle_events=registry.lifecycle_events + (dividend,),
    )

    payload = next(
        item
        for item in with_dividend.as_dict()["lifecycle_events"]
        if item["event_id"] == dividend.event_id
    )
    assert payload["announced_at"] == "2026-06-01T00:00:00+00:00"
    assert payload["cash_amount"] == "1.25"
    assert payload["currency"] == "KRW"

    with pytest.raises(ProductMasterError, match="amount_required"):
        replace(dividend, cash_amount=None, currency=None)

    with pytest.raises(
        ProductMasterError,
        match="knowledge_before_source_observed",
    ):
        replace(
            dividend,
            knowledge_at="2026-06-01T00:00:00+00:00",
        )

    with pytest.raises(
        ProductMasterError,
        match="knowledge_before_announcement",
    ):
        replace(
            dividend,
            source=_source("8", observed_at="2026-05-01T00:00:00+00:00"),
            knowledge_at="2026-05-15T00:00:00+00:00",
        )


def _lineage(
    char: str,
    *,
    upstream: tuple[str, ...] = (),
    transformed: bool = False,
) -> DataLineage:
    return DataLineage(
        source_id="prepared_dataset",
        source_version="snapshot_v1",
        source_artifact_hash=_hash(char),
        source_schema_hash=_hash("f"),
        upstream_record_hashes=upstream,
        transformation_id="normalize_ohlcv" if transformed else None,
        transformation_version="v1" if transformed else None,
        parameters_hash=_hash("e") if transformed else None,
    )


def _clocks(prefix: str = "10") -> ObservationClocks:
    return ObservationClocks(
        event_at="2026-01-02T09:00:00+00:00",
        knowledge_at=f"2026-01-02T{prefix}:00:00+00:00",
        revision_at=f"2026-01-02T{prefix}:00:00+00:00",
        received_at=f"2026-01-02T{prefix}:01:00+00:00",
        ingested_at=f"2026-01-02T{prefix}:02:00+00:00",
    )


def _raw_metadata(
    payload: dict[str, object],
    *,
    provider_record_id: str = "provider.btc.20260102.v1",
    quality_flags: tuple[str, ...] = (),
) -> RawLayerMetadata:
    return RawLayerMetadata(
        provider_record_id=provider_record_id,
        provider_symbol="XBTKRW",
        source_object_id="object.btc.daily.20260102",
        collection_batch_id="batch.reviewed.20260102",
        original_schema_id="provider.ohlcv.v1",
        provider_version="v1",
        payload_checksum=sha256_prefixed(
            payload,
            label="multi_asset_data_payload",
        ),
        quality_flags=quality_flags,
    )


def _normalized_metadata(
    *,
    quality_flags: tuple[str, ...] = (),
) -> NormalizedLayerMetadata:
    return NormalizedLayerMetadata(
        internal_instrument_id="inst_btc_spot",
        source_timezone="Asia/Seoul",
        target_timezone="UTC",
        source_price_unit="KRW_per_coin",
        target_price_unit="KRW_per_coin",
        source_quantity_unit="coin",
        target_quantity_unit="coin",
        currency="KRW",
        exchange_session_id="session.krx.day",
        provider_priority=1,
        duplicate_resolution="UNIQUE",
        unit_conversion_id="identity.krw.coin.v1",
        missing_value=False,
        outlier=False,
        revised=False,
        quality_flags=quality_flags,
    )


def _derived_metadata(
    input_snapshot_hash: str,
    *,
    quality_flags: tuple[str, ...] = (),
) -> DerivedLayerMetadata:
    return DerivedLayerMetadata(
        model_id="simple.return",
        model_version="v1",
        input_snapshot_hash=input_snapshot_hash,
        generated_at="2026-01-02T12:00:00+00:00",
        code_version="commit.test",
        code_hash=_hash("7"),
        quality_flags=quality_flags,
    )


def _raw_record(
    payload: dict[str, object] | None = None,
    *,
    quality_flags: tuple[str, ...] = (),
) -> BitemporalRecord:
    record_payload = payload or {"close": "100", "currency": "KRW"}
    return BitemporalRecord(
        record_id="raw.btc.20260102",
        version=1,
        layer=DataLayer.RAW,
        instrument_id="inst_btc_spot",
        data_kind="ohlcv",
        clocks=_clocks(),
        payload=record_payload,
        lineage=_lineage("a"),
        layer_metadata=_raw_metadata(
            record_payload,
            quality_flags=quality_flags,
        ),
    )


def test_three_layer_store_is_immutable_and_hash_binds_lineage() -> None:
    mutable_payload: dict[str, object] = {"close": "100", "currency": "KRW"}
    raw = _raw_record(mutable_payload)
    empty = AppendOnlyBitemporalStore()
    raw_store = empty.append(raw)
    normalized = BitemporalRecord(
        record_id="normalized.btc.20260102",
        version=1,
        layer=DataLayer.NORMALIZED,
        instrument_id=raw.instrument_id,
        data_kind="ohlcv",
        clocks=_clocks("11"),
        payload={"close": Decimal("100.00"), "currency": "KRW"},
        lineage=_lineage("b", upstream=(raw.record_hash(),), transformed=True),
        layer_metadata=_normalized_metadata(),
    )
    normalized_store = raw_store.append(normalized)
    derived = BitemporalRecord(
        record_id="derived.btc.return.20260102",
        version=1,
        layer=DataLayer.DERIVED,
        instrument_id=raw.instrument_id,
        data_kind="simple_return",
        clocks=_clocks("12"),
        payload={"return": "0.01", "unit": "decimal_return"},
        lineage=_lineage("c", upstream=(normalized.record_hash(),), transformed=True),
        layer_metadata=_derived_metadata(normalized.record_hash()),
    )
    complete = normalized_store.append(derived)

    assert complete.schema_version == 3
    mutable_payload["close"] = "999"
    assert raw.payload["close"] == "100"
    assert empty.records == ()
    assert len(raw_store.records) == 1
    assert [item.layer for item in complete.records] == [
        DataLayer.RAW,
        DataLayer.NORMALIZED,
        DataLayer.DERIVED,
    ]
    complete.verify_content_hash(complete.content_hash())
    assert complete.content_hash().startswith("sha256:")
    with pytest.raises(FrozenInstanceError):
        raw.version = 2  # type: ignore[misc]
    with pytest.raises(
        MultiAssetDataError,
        match="derived_record_known_before_generation",
    ):
        replace(
            derived,
            layer_metadata=replace(
                derived.layer_metadata,
                generated_at="2026-01-02T12:03:00+00:00",
            ),
        )
    derived_before_upstream = replace(
        derived,
        record_id="derived.btc.return.before.upstream",
        layer_metadata=replace(
            derived.layer_metadata,
            generated_at="2026-01-02T11:01:00+00:00",
        ),
    )
    with pytest.raises(
        MultiAssetDataError,
        match="derived_record_generated_before_upstream_available",
    ):
        normalized_store.append(derived_before_upstream)


def test_processed_record_cannot_predate_upstream_availability() -> None:
    raw = _raw_record()
    store = AppendOnlyBitemporalStore().append(raw)
    normalized = BitemporalRecord(
        record_id="normalized.btc.early",
        version=1,
        layer=DataLayer.NORMALIZED,
        instrument_id=raw.instrument_id,
        data_kind=raw.data_kind,
        clocks=_clocks("09"),
        payload={"close": "100", "currency": "KRW"},
        lineage=_lineage(
            "4",
            upstream=(raw.record_hash(),),
            transformed=True,
        ),
        layer_metadata=_normalized_metadata(),
    )

    with pytest.raises(
        MultiAssetDataError,
        match="processed_record_known_before_upstream_available",
    ):
        store.append(normalized)


def test_bitemporal_query_excludes_later_correction_and_preserves_history() -> None:
    original = _raw_record()
    first = AppendOnlyBitemporalStore().append(original)
    correction_payload = {"close": "101", "currency": "KRW"}
    correction = BitemporalRecord(
        record_id=original.record_id,
        version=2,
        layer=original.layer,
        instrument_id=original.instrument_id,
        data_kind=original.data_kind,
        clocks=_clocks("13"),
        payload=correction_payload,
        lineage=_lineage("d"),
        layer_metadata=_raw_metadata(
            correction_payload,
            provider_record_id="provider.btc.20260102.v2",
            quality_flags=("provider_correction",),
        ),
        supersedes_hash=original.record_hash(),
        correction_reason="reviewed vendor correction",
    )
    corrected = first.append(correction)

    before = corrected.query_as_of(
        event_as_of="2026-01-02T23:59:59+00:00",
        knowledge_as_of="2026-01-02T12:59:59+00:00",
    )
    after = corrected.query_as_of(
        event_as_of="2026-01-02T23:59:59+00:00",
        knowledge_as_of="2026-01-02T13:02:00+00:00",
    )
    assert [(item.version, item.payload["close"]) for item in before] == [(1, "100")]
    assert [(item.version, item.payload["close"]) for item in after] == [(2, "101")]
    assert [
        item.version for item in corrected.correction_history(original.record_id)
    ] == [1, 2]
    assert first.correction_history(original.record_id) == (original,)


def test_data_store_rejects_missing_lineage_and_non_offline_source() -> None:
    raw = _raw_record()
    with pytest.raises(
        MultiAssetDataError,
        match="raw_record_transformation_forbidden",
    ):
        replace(
            raw,
            lineage=_lineage("1", transformed=True),
        )
    with pytest.raises(
        MultiAssetDataError,
        match="normalized_metadata.currency_invalid_currency",
    ):
        replace(
            _normalized_metadata(),
            currency="usd",
        )
    missing_upstream = BitemporalRecord(
        record_id="normalized.btc.missing",
        version=1,
        layer=DataLayer.NORMALIZED,
        instrument_id=raw.instrument_id,
        data_kind="ohlcv",
        clocks=_clocks("11"),
        payload={"close": "100"},
        lineage=_lineage("b", upstream=(_hash("9"),), transformed=True),
        layer_metadata=_normalized_metadata(),
    )
    with pytest.raises(MultiAssetDataError, match="lineage_upstream_record_missing"):
        AppendOnlyBitemporalStore().append(missing_upstream)

    with pytest.raises(
        MultiAssetDataError, match="network_source_collection_forbidden"
    ):
        replace(_lineage("a"), source_mode="NETWORK_COLLECTION")

    with pytest.raises(MultiAssetDataError, match="clocks_received_before_knowledge"):
        ObservationClocks(
            event_at="2026-01-01T00:00:00+00:00",
            knowledge_at="2026-01-02T00:00:00+00:00",
            revision_at="2026-01-02T00:00:00+00:00",
            received_at="2026-01-01T23:59:59+00:00",
            ingested_at="2026-01-02T00:00:01+00:00",
        )


def test_layer_metadata_lineage_and_quality_propagate_in_both_directions() -> None:
    raw = _raw_record(quality_flags=("provider_stale",))
    normalized = BitemporalRecord(
        record_id="normalized.btc.quality",
        version=1,
        layer=DataLayer.NORMALIZED,
        instrument_id=raw.instrument_id,
        data_kind="ohlcv",
        clocks=_clocks("11"),
        payload={"close": "100", "currency": "KRW"},
        lineage=_lineage("b", upstream=(raw.record_hash(),), transformed=True),
        layer_metadata=_normalized_metadata(quality_flags=("estimated_session",)),
    )
    derived = BitemporalRecord(
        record_id="derived.btc.quality",
        version=1,
        layer=DataLayer.DERIVED,
        instrument_id=raw.instrument_id,
        data_kind="simple_return",
        clocks=_clocks("12"),
        payload={"return": "0.01", "unit": "decimal_return"},
        lineage=_lineage("c", upstream=(normalized.record_hash(),), transformed=True),
        layer_metadata=_derived_metadata(
            normalized.record_hash(),
            quality_flags=("model_fallback",),
        ),
    )
    store = AppendOnlyBitemporalStore().append(raw).append(normalized).append(derived)

    assert store.lineage_ancestors(derived.record_hash()) == (raw, normalized)
    assert store.lineage_descendants(raw.record_hash()) == (normalized, derived)
    assert store.trace_to_raw(derived.record_hash()) == (raw,)
    assert store.propagated_quality_flags(derived.record_hash()) == (
        "estimated_session",
        "model_fallback",
        "provider_stale",
    )


def test_layer_metadata_rejects_wrong_layer_and_raw_checksum() -> None:
    payload = {"close": "100", "currency": "KRW"}
    with pytest.raises(
        MultiAssetDataError,
        match="raw_record_layer_metadata_mismatch",
    ):
        BitemporalRecord(
            record_id="raw.btc.wrong.metadata",
            version=1,
            layer=DataLayer.RAW,
            instrument_id="inst_btc_spot",
            data_kind="ohlcv",
            clocks=_clocks(),
            payload=payload,
            lineage=_lineage("a"),
            layer_metadata=_normalized_metadata(),
        )
    with pytest.raises(
        MultiAssetDataError,
        match="raw_record_payload_checksum_mismatch",
    ):
        BitemporalRecord(
            record_id="raw.btc.bad.checksum",
            version=1,
            layer=DataLayer.RAW,
            instrument_id="inst_btc_spot",
            data_kind="ohlcv",
            clocks=_clocks(),
            payload=payload,
            lineage=_lineage("a"),
            layer_metadata=replace(
                _raw_metadata(payload),
                payload_checksum=_hash("8"),
            ),
        )
    with pytest.raises(
        MultiAssetDataError,
        match="source_timezone_unknown",
    ):
        replace(
            _normalized_metadata(),
            source_timezone="Mars/Olympus",
        )


def test_derived_metadata_must_bind_the_exact_upstream_record_set() -> None:
    raw = _raw_record()
    normalized = BitemporalRecord(
        record_id="normalized.btc.snapshot.binding",
        version=1,
        layer=DataLayer.NORMALIZED,
        instrument_id=raw.instrument_id,
        data_kind="ohlcv",
        clocks=_clocks("11"),
        payload={"close": "100", "currency": "KRW"},
        lineage=_lineage("b", upstream=(raw.record_hash(),), transformed=True),
        layer_metadata=_normalized_metadata(),
    )
    derived = BitemporalRecord(
        record_id="derived.btc.snapshot.binding",
        version=1,
        layer=DataLayer.DERIVED,
        instrument_id=raw.instrument_id,
        data_kind="simple_return",
        clocks=_clocks("12"),
        payload={"return": "0.01"},
        lineage=_lineage("c", upstream=(normalized.record_hash(),), transformed=True),
        layer_metadata=_derived_metadata(_hash("8")),
    )
    normalized_store = AppendOnlyBitemporalStore().append(raw).append(normalized)

    assert derived_input_snapshot_hash((normalized.record_hash(),)) == (
        normalized.record_hash()
    )
    with pytest.raises(
        MultiAssetDataError,
        match="derived_input_snapshot_hash_mismatch",
    ):
        normalized_store.append(derived)


def _metadata(
    *,
    quality: MarketDataQuality = MarketDataQuality.GOOD,
    observed_at: str = "2026-01-02T09:59:30+00:00",
    knowledge_at: str = "2026-01-02T09:59:31+00:00",
    max_age_seconds: int = 120,
) -> ObservationMetadata:
    return ObservationMetadata(
        observed_at=observed_at,
        knowledge_at=knowledge_at,
        source_hash=_hash("a"),
        calendar_id="calendar_xoff",
        max_age_seconds=max_age_seconds,
        quality=quality,
    )


def _market_state(*, include_fx: bool = True) -> MarketState:
    metadata = _metadata()
    btc = SpotQuote(
        instrument_id="inst_btc_spot",
        price=Decimal("100"),
        currency="USD",
        unit="USD_per_coin",
        metadata=metadata,
    )
    local_equity = SpotQuote(
        instrument_id="inst_local_equity",
        price=Decimal("50000"),
        currency="KRW",
        unit="KRW_per_share",
        metadata=metadata,
    )
    return MarketState(
        state_id="market_state_20260102_1000",
        valuation_at="2026-01-02T10:00:00+00:00",
        base_currency="KRW",
        calendar_ids=("calendar_xoff",),
        spots=(btc, local_equity),
        curves=(
            YieldCurve(
                curve_id="curve_usd_ois",
                currency="USD",
                curve_type="ois",
                points=(
                    CurvePoint(365, Decimal("0.04")),
                    CurvePoint(30, Decimal("0.03")),
                ),
                metadata=metadata,
            ),
        ),
        volatility_surfaces=(
            VolatilitySurface(
                surface_id="surface_btc",
                underlying_instrument_id=btc.instrument_id,
                quote_currency="USD",
                points=(
                    VolatilityPoint(
                        "2026-06-30T00:00:00+00:00",
                        Decimal("100"),
                        Decimal("0.50"),
                    ),
                ),
                metadata=metadata,
            ),
        ),
        rates=(
            RateQuote(
                rate_id="rate_usd_overnight",
                currency="USD",
                tenor_days=1,
                rate=Decimal("0.03"),
                metadata=metadata,
            ),
        ),
        fx_quotes=(
            FXQuote(
                base_currency="USD",
                quote_currency="KRW",
                rate=Decimal("1300"),
                unit="KRW_per_USD",
                metadata=metadata,
            ),
        )
        if include_fx
        else (),
        borrow_quotes=(
            BorrowQuote(
                instrument_id=btc.instrument_id,
                currency="USD",
                annualized_rate=Decimal("0.02"),
                available_quantity=Decimal("10"),
                quantity_unit="coin",
                metadata=metadata,
            ),
        ),
        liquidity_quotes=(
            LiquidityQuote(
                instrument_id=btc.instrument_id,
                currency="USD",
                bid=Decimal("99"),
                ask=Decimal("101"),
                price_unit="USD_per_coin",
                depth_quantity=Decimal("5"),
                quantity_unit="coin",
                metadata=metadata,
            ),
        ),
    )


def _derivative_components() -> tuple[FuturesCurveState, OptionChainState]:
    metadata = _metadata()
    margin_hash = _hash("b")
    future = FuturesContractQuote(
        contract_id="inst_btc_future_dec26",
        underlying_instrument_id="inst_btc_spot",
        expiry_at="2026-12-18T08:00:00+00:00",
        currency="USD",
        price_unit="USD_per_coin",
        bid=Decimal("99"),
        ask=Decimal("101"),
        last=Decimal("100"),
        settlement=Decimal("100"),
        bid_size=Decimal("20"),
        ask_size=Decimal("20"),
        volume=Decimal("1000"),
        open_interest=Decimal("5000"),
        condition=QuoteCondition.OFFICIAL_SETTLEMENT,
        initial_margin_per_contract=Decimal("10"),
        collateral_per_contract=Decimal("5"),
        margin_model_hash=margin_hash,
        metadata=metadata,
    )
    curve = FuturesCurveState(
        curve_id="curve_btc_futures_20260102",
        underlying_instrument_id="inst_btc_spot",
        currency="USD",
        price_unit="USD_per_coin",
        contracts=(future,),
        metadata=metadata,
    )
    option_quote = OptionContractQuote(
        contract_id="inst_btc_option_dec26",
        underlying_instrument_id=future.contract_id,
        expiry_at="2026-11-20T08:00:00+00:00",
        right=OptionRight.CALL,
        strike=Decimal("100"),
        currency="USD",
        price_unit="USD_per_future_contract",
        bid=Decimal("4.5"),
        ask=Decimal("5.5"),
        last=Decimal("5"),
        settlement=None,
        bid_size=Decimal("30"),
        ask_size=Decimal("30"),
        volume=Decimal("100"),
        open_interest=Decimal("1000"),
        condition=QuoteCondition.NORMAL,
        metadata=metadata,
    )
    option_mark = OptionAnalyticsMark(
        contract_id=option_quote.contract_id,
        underlying_instrument_id=option_quote.underlying_instrument_id,
        expiry_at=option_quote.expiry_at,
        currency=option_quote.currency,
        price_unit=option_quote.price_unit,
        market_price=option_quote.midpoint,
        model_price=Decimal("5.2"),
        implied_volatility=Decimal("0.50"),
        delta=Decimal("0.50"),
        gamma=Decimal("0.20"),
        vega=Decimal("0.20"),
        theta=Decimal("-0.01"),
        rho=Decimal("0.05"),
        margin_per_contract=Decimal("2"),
        collateral_per_contract=Decimal("1"),
        model_hash=_hash("c"),
        model_specification_hash=_hash("d"),
        margin_model_hash=margin_hash,
        valuation_input_hash=_hash("e"),
        source_quote_hash=option_quote.content_hash,
        metadata=metadata,
    )
    chain = OptionChainState(
        chain_id="chain_btc_options_20260102",
        underlying_instrument_id=future.contract_id,
        currency="USD",
        price_unit="USD_per_future_contract",
        quotes=(option_quote,),
        analytics=(option_mark,),
        metadata=metadata,
    )
    return curve, chain


def test_shared_market_state_is_consistent_hash_stable_and_currency_aware() -> None:
    state = _market_state()
    state.require_usable()

    assert state.schema_version == 3
    assert state.spot_price("inst_btc_spot").price == Decimal("100")
    assert state.convert(
        Decimal("2"), from_currency="USD", to_currency="KRW"
    ) == Decimal("2600")
    reordered = replace(state, spots=tuple(reversed(state.spots)))
    assert reordered.state_hash() == state.state_hash()
    assert state.state_hash().startswith("sha256:")


def test_market_state_fx_order_is_canonical_and_reciprocals_fail_closed() -> None:
    state = _market_state()
    usd_krw = state.fx_quotes[0]
    eur_krw = FXQuote(
        base_currency="EUR",
        quote_currency="KRW",
        rate=Decimal("1500"),
        unit="KRW_per_EUR",
        metadata=_metadata(),
    )
    ordered = replace(state, fx_quotes=(eur_krw, usd_krw))
    reversed_state = replace(state, fx_quotes=(usd_krw, eur_krw))

    assert ordered.fx_quotes == reversed_state.fx_quotes
    assert ordered.state_hash() == reversed_state.state_hash()
    assert ordered.convert(
        Decimal("2"), from_currency="USD", to_currency="KRW"
    ) == reversed_state.convert(Decimal("2"), from_currency="USD", to_currency="KRW")

    inconsistent_inverse = FXQuote(
        base_currency="KRW",
        quote_currency="USD",
        rate=Decimal("0.001"),
        unit="USD_per_KRW",
        metadata=_metadata(),
    )
    for quotes in (
        (usd_krw, inconsistent_inverse),
        (inconsistent_inverse, usd_krw),
    ):
        with pytest.raises(
            MarketStateError,
            match="market_state_fx_reciprocal_pair_duplicate:KRW:USD",
        ):
            replace(state, fx_quotes=quotes)


def test_typed_derivative_state_is_hash_bound_order_stable_and_queryable() -> None:
    curve, chain = _derivative_components()
    state = replace(
        _market_state(),
        futures_curves=(curve,),
        option_chains=(chain,),
    )
    state.require_usable()

    future = state.futures_contract_quote("inst_btc_future_dec26")
    quote = state.option_contract_quote("inst_btc_option_dec26")
    analytics = state.option_analytics_mark("inst_btc_option_dec26")
    assert future.mark_price == Decimal("100")
    assert state.derivative_underlying_price(future.contract_id) == Decimal("100")
    assert quote.right is OptionRight.CALL
    assert quote.strike == Decimal("100")
    assert analytics.market_price == Decimal("5")
    assert analytics.model_price == Decimal("5.2")
    assert analytics.source_quote_hash == quote.content_hash
    assert state.futures_curve(curve.curve_id).content_hash == curve.content_hash
    assert state.option_chain(chain.chain_id).content_hash == chain.content_hash
    assert state.state_hash() != _market_state().state_hash()

    second_future = replace(
        future,
        contract_id="inst_btc_future_mar27",
        expiry_at="2027-03-19T08:00:00+00:00",
    )
    ordered_curve = replace(curve, contracts=(future, second_future))
    reversed_curve = replace(curve, contracts=(second_future, future))
    ordered = replace(state, futures_curves=(ordered_curve,))
    reversed_state = replace(state, futures_curves=(reversed_curve,))
    assert ordered.state_hash() == reversed_state.state_hash()


def test_typed_derivative_state_fails_closed_on_binding_time_and_duplicates() -> None:
    curve, chain = _derivative_components()
    state = _market_state()
    future = curve.contracts[0]
    option_quote = chain.quotes[0]
    analytics = chain.analytics[0]

    with pytest.raises(MarketStateError, match="futures_curve.contract_duplicate"):
        replace(curve, contracts=(future, future))
    with pytest.raises(MarketStateError, match="source_quote_hash_mismatch"):
        replace(
            chain,
            analytics=(replace(analytics, source_quote_hash=_hash("f")),),
        )
    with pytest.raises(MarketStateError, match="contract_mismatch"):
        replace(
            chain,
            quotes=(replace(option_quote, price_unit="USD_per_coin"),),
        )

    future_metadata = replace(
        _metadata(),
        knowledge_at="2026-01-02T10:00:01+00:00",
    )
    future_known_late = replace(future, metadata=future_metadata)
    curve_known_late = replace(
        curve,
        contracts=(future_known_late,),
        metadata=future_metadata,
    )
    with pytest.raises(MarketStateError, match="future_knowledge"):
        replace(state, futures_curves=(curve_known_late,))

    halted = replace(option_quote, condition=QuoteCondition.HALTED)
    halted_mark = replace(analytics, source_quote_hash=halted.content_hash)
    halted_chain = replace(chain, quotes=(halted,), analytics=(halted_mark,))
    halted_state = replace(
        state,
        futures_curves=(curve,),
        option_chains=(halted_chain,),
    )
    with pytest.raises(MarketStateError, match="unusable_quote_condition"):
        halted_state.require_usable()


def test_market_state_fails_closed_on_fx_units_calendars_and_staleness() -> None:
    with pytest.raises(MarketStateError, match="market_state_base_fx_missing"):
        _market_state(include_fx=False)

    with pytest.raises(MarketStateError, match="fx_unit_mismatch"):
        FXQuote(
            base_currency="USD",
            quote_currency="KRW",
            rate=Decimal("1300"),
            unit="USD_per_KRW",
            metadata=_metadata(),
        )

    stale_metadata = _metadata(
        observed_at="2026-01-02T09:00:00+00:00",
        knowledge_at="2026-01-02T09:00:01+00:00",
        max_age_seconds=60,
    )
    with pytest.raises(MarketStateError, match="staleness_quality_mismatch"):
        replace(
            _market_state(),
            spots=(replace(_market_state().spots[1], metadata=stale_metadata),),
            curves=(),
            volatility_surfaces=(),
            rates=(),
            fx_quotes=(),
            borrow_quotes=(),
            liquidity_quotes=(),
        )

    explicitly_stale = replace(stale_metadata, quality=MarketDataQuality.STALE)
    stale_state = replace(
        _market_state(),
        spots=(replace(_market_state().spots[1], metadata=explicitly_stale),),
        curves=(),
        volatility_surfaces=(),
        rates=(),
        fx_quotes=(),
        borrow_quotes=(),
        liquidity_quotes=(),
    )
    with pytest.raises(MarketStateError, match="market_state_unusable_quality"):
        stale_state.require_usable()


def test_market_state_rejects_cross_component_currency_or_calendar_mismatch() -> None:
    state = _market_state()
    bad_liquidity = replace(state.liquidity_quotes[0], price_unit="KRW_per_coin")
    with pytest.raises(MarketStateError, match="spot_liquidity_mismatch"):
        replace(state, liquidity_quotes=(bad_liquidity,))

    foreign_calendar = replace(_metadata(), calendar_id="calendar_unregistered")
    with pytest.raises(MarketStateError, match="unregistered_calendar"):
        replace(state, rates=(replace(state.rates[0], metadata=foreign_calendar),))

    futures_curve, _option_chain = _derivative_components()
    cross_currency_curve = replace(
        futures_curve,
        currency="KRW",
        contracts=tuple(
            replace(item, currency="KRW") for item in futures_curve.contracts
        ),
    )
    with pytest.raises(
        MarketStateError,
        match="futures_underlying_dimension_mismatch",
    ):
        replace(
            state,
            futures_curves=(cross_currency_curve,),
        )

    other_calendar_metadata = replace(
        _metadata(),
        calendar_id="calendar_other",
    )
    funding = FundingRateQuote(
        funding_rate_id="funding_btc_wrong_calendar",
        instrument_id=futures_curve.contracts[0].contract_id,
        currency="USD",
        annualized_rate=Decimal("0.01"),
        settlement_at="2026-01-02T16:00:00+00:00",
        interval_hours=8,
        metadata=other_calendar_metadata,
    )
    with pytest.raises(
        MarketStateError,
        match="funding_dimension_mismatch",
    ):
        replace(
            state,
            calendar_ids=("calendar_other", "calendar_xoff"),
            futures_curves=(futures_curve,),
            funding_rates=(funding,),
        )


def test_market_state_binds_calendar_carry_and_forward_diagnostics() -> None:
    metadata = replace(_metadata(), calendar_id="cal_xoffmarket")
    calendar = MarketCalendarAuthority(
        schema_version=MARKET_CALENDAR_SCHEMA_VERSION,
        calendar_id="cal_xoffmarket",
        calendar_version_id="calv_xoffmarket_v1",
        version=1,
        market_mode="continuous_24x7",
        timezone_name="UTC",
        tzdb_version="2026a",
        dst_transition_policy=("iana_tzdb_reject_ambiguous_or_nonexistent_local_time"),
        valid_from="2026-01-01",
        valid_to="2026-12-31",
        source_uri="/var/lib/market-research-inputs/calendar-xoff-v1.json",
        source_content_hash=_hash("1"),
        source_schema_hash=_hash("2"),
        published_at="2025-12-01T00:00:00+00:00",
        observed_at="2025-12-01T00:01:00+00:00",
        weekly_sessions=(),
        exceptions=(),
    )
    curve, _ = _derivative_components()
    future = replace(curve.contracts[0], metadata=metadata)
    curve = replace(curve, contracts=(future,), metadata=metadata)
    spot = SpotQuote(
        instrument_id="inst_btc_spot",
        price=Decimal("100"),
        currency="USD",
        unit="USD_per_coin",
        metadata=metadata,
    )
    state = MarketState(
        state_id="market_state_carry_20260102",
        valuation_at="2026-01-02T10:00:00+00:00",
        base_currency="USD",
        calendar_ids=(calendar.calendar_id,),
        spots=(spot,),
        rates=(
            RateQuote(
                rate_id="rate_usd_annual",
                currency="USD",
                tenor_days=365,
                rate=Decimal("0.05"),
                metadata=metadata,
            ),
        ),
        calendar_authorities=(calendar,),
        funding_rates=(
            FundingRateQuote(
                funding_rate_id="funding_btc_future_dec26",
                instrument_id=future.contract_id,
                currency="USD",
                annualized_rate=Decimal("0.02"),
                settlement_at="2026-01-02T16:00:00+00:00",
                interval_hours=8,
                metadata=metadata,
            ),
        ),
        dividend_yields=(
            DividendYieldAssumption(
                assumption_id="dividend_btc_zero_proxy",
                underlying_instrument_id=spot.instrument_id,
                currency="USD",
                annualized_yield=Decimal("0.01"),
                model_hash=_hash("3"),
                metadata=metadata,
            ),
        ),
        futures_curves=(curve,),
    )

    state.require_usable()
    diagnostic = state.forward_consistency_diagnostics(
        relative_tolerance=Decimal("0.05")
    )[0]
    assert diagnostic["contract_id"] == future.contract_id
    assert diagnostic["method"] == "simple_carry_actual_365"
    assert diagnostic["passed"] is True
    assert state.as_dict()["calendar_authorities"][0]["contract_hash"] == (
        calendar.contract_hash()
    )

    with pytest.raises(
        MarketStateError,
        match="calendar_authority_set_mismatch",
    ):
        replace(state, calendar_ids=("cal_xoffmarket", "cal_secondmarket"))
