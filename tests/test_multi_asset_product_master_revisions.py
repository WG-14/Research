from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from market_research.research.hashing import sha256_prefixed
from market_research.research.instrument_kinds import InstrumentKind
from market_research.research.multi_asset.domain import (
    CompositeDeliverable,
    DeliverableComponent,
    EconomicUnderlying,
    EffectivePeriod,
    Instrument,
    InstrumentRegistry,
    Issuer,
    IssuerIdentifierNamespace,
    IssuerIdentifierRevision,
    Listing,
    ProductMasterError,
    SourceReference,
    SymbolAlias,
)


def _hash(value: str) -> str:
    return sha256_prefixed(value, label="product-master-revision-test")


def _source(value: str, observed_at: str) -> SourceReference:
    return SourceReference(
        source_id=f"source.{value}",
        source_version="v1",
        content_hash=_hash(value),
        observed_at=observed_at,
    )


def _registry() -> InstrumentRegistry:
    entire = EffectivePeriod(
        "2020-01-01T00:00:00+00:00",
        "2030-01-01T00:00:00+00:00",
    )
    source = _source("master", "2019-12-01T00:00:00+00:00")
    issuer = Issuer(
        issuer_id="issuer.acme",
        legal_name="Acme Holdings",
        jurisdiction="US",
        validity=entire,
        source=source,
    )
    underlying = EconomicUnderlying(
        underlying_id="underlying.acme.enterprise",
        name="Acme enterprise value",
        asset_class="equity",
        unit="share",
        validity=entire,
        source=source,
        currency="USD",
    )
    instrument = Instrument(
        instrument_id="instrument.acme.common",
        kind=InstrumentKind.EQUITY,
        name="Acme common stock",
        economic_underlying_id=underlying.underlying_id,
        issuer_id=issuer.issuer_id,
        currency="USD",
        unit="share",
        validity=entire,
        source=source,
    )
    xnys = Listing(
        listing_id="listing.acme.xnys",
        instrument_id=instrument.instrument_id,
        venue_mic="XNYS",
        symbol="ACME",
        trading_currency="USD",
        price_unit="USD_per_share",
        quantity_unit="share",
        calendar_id="cal_xnys_v1",
        validity=entire,
        source=source,
    )
    xnas = replace(
        xnys,
        listing_id="listing.acme.xnas",
        venue_mic="XNAS",
        calendar_id="cal_xnas_v1",
    )
    identifier_v1 = IssuerIdentifierRevision(
        identifier_id="issuer-id.acme.lei",
        revision=1,
        issuer_id=issuer.issuer_id,
        namespace=IssuerIdentifierNamespace.LEI,
        value="549300OLDACME000001",
        validity=entire,
        knowledge_at="2020-01-01T00:00:00+00:00",
        source=_source("lei-v1", "2019-12-31T00:00:00+00:00"),
        jurisdiction="US",
    )
    identifier_v2 = IssuerIdentifierRevision(
        identifier_id=identifier_v1.identifier_id,
        revision=2,
        issuer_id=issuer.issuer_id,
        namespace=IssuerIdentifierNamespace.LEI,
        value="549300NEWACME000002",
        validity=entire,
        knowledge_at="2024-01-01T00:00:00+00:00",
        source=_source("lei-v2", "2024-01-01T00:00:00+00:00"),
        jurisdiction="US",
        supersedes_hash=identifier_v1.revision_hash(),
        correction_reason="registry correction retained append-only",
    )
    aliases = (
        SymbolAlias(
            alias_id="alias.acme.xnys",
            instrument_id=instrument.instrument_id,
            provider_id="provider.reference",
            symbol="ACME",
            listing_id=xnys.listing_id,
            validity=entire,
            source=source,
        ),
        SymbolAlias(
            alias_id="alias.acme.xnas",
            instrument_id=instrument.instrument_id,
            provider_id="provider.reference",
            symbol="ACME",
            listing_id=xnas.listing_id,
            validity=entire,
            source=source,
        ),
    )
    deliverable = CompositeDeliverable(
        deliverable_id="deliverable.acme.reorganization",
        source_instrument_id=instrument.instrument_id,
        components=(
            DeliverableComponent(
                component_id="component.cash",
                target_instrument_id=None,
                quantity=Decimal("0"),
                cash_amount=Decimal("12.50"),
                cash_currency="USD",
            ),
            DeliverableComponent(
                component_id="component.stock",
                target_instrument_id=instrument.instrument_id,
                quantity=Decimal("0.75"),
            ),
        ),
        validity=entire,
        knowledge_at="2023-01-01T00:00:00+00:00",
        source=_source("deliverable", "2023-01-01T00:00:00+00:00"),
    )
    return InstrumentRegistry(
        economic_underlyings=(underlying,),
        issuers=(issuer,),
        issuer_identifier_revisions=(identifier_v1, identifier_v2),
        instruments=(instrument,),
        listings=(xnas, xnys),
        symbol_aliases=aliases,
        composite_deliverables=(deliverable,),
    )


def test_issuer_identifier_correction_is_bitemporal_and_not_overwritten() -> None:
    registry = _registry()
    old = registry.resolve_issuer_identifier(
        namespace=IssuerIdentifierNamespace.LEI,
        value="549300OLDACME000001",
        jurisdiction="US",
        as_of="2023-06-01T00:00:00+00:00",
        knowledge_at="2023-06-01T00:00:00+00:00",
    )
    new = registry.resolve_issuer_identifier(
        namespace=IssuerIdentifierNamespace.LEI,
        value="549300NEWACME000002",
        jurisdiction="US",
        as_of="2024-06-01T00:00:00+00:00",
        knowledge_at="2024-06-01T00:00:00+00:00",
    )
    assert old.issuer_id == new.issuer_id == "issuer.acme"
    with pytest.raises(
        ProductMasterError, match="issuer_identifier_not_unique_as_of"
    ):
        registry.resolve_issuer_identifier(
            namespace=IssuerIdentifierNamespace.LEI,
            value="549300OLDACME000001",
            jurisdiction="US",
            as_of="2024-06-01T00:00:00+00:00",
            knowledge_at="2024-06-01T00:00:00+00:00",
        )


def test_same_provider_symbol_requires_market_qualification() -> None:
    registry = _registry()
    with pytest.raises(ProductMasterError, match="symbol_alias_not_unique_as_of"):
        registry.resolve_symbol(
            provider_id="provider.reference",
            symbol="ACME",
            as_of="2025-01-01T00:00:00+00:00",
        )
    xnys = registry.resolve_symbol(
        provider_id="provider.reference",
        symbol="ACME",
        venue_mic="XNYS",
        as_of="2025-01-01T00:00:00+00:00",
    )
    xnas = registry.resolve_symbol(
        provider_id="provider.reference",
        symbol="ACME",
        venue_mic="XNAS",
        as_of="2025-01-01T00:00:00+00:00",
    )
    assert xnys.instrument_id == xnas.instrument_id == "instrument.acme.common"


def test_namespace_collision_and_broken_revision_chain_fail_closed() -> None:
    registry = _registry()
    identifier = registry.issuer_identifier_revisions[0]
    conflicting_issuer = replace(
        registry.issuers[0],
        issuer_id="issuer.other",
        legal_name="Other Holdings",
    )
    conflicting_identifier = replace(
        identifier,
        identifier_id="issuer-id.other.lei",
        issuer_id=conflicting_issuer.issuer_id,
    )
    with pytest.raises(
        ProductMasterError, match="issuer_identifier_namespace_collision"
    ):
        replace(
            registry,
            issuers=(*registry.issuers, conflicting_issuer),
            issuer_identifier_revisions=(
                *registry.issuer_identifier_revisions,
                conflicting_identifier,
            ),
        )

    with pytest.raises(
        ProductMasterError, match="issuer_identifier_revision_chain_broken"
    ):
        replace(
            registry,
            issuer_identifier_revisions=(
                registry.issuer_identifier_revisions[0],
                replace(
                    registry.issuer_identifier_revisions[1],
                    supersedes_hash=_hash("forged"),
                ),
            ),
        )


def test_composite_deliverable_is_hash_bound_and_resolved_by_knowledge_time() -> None:
    registry = _registry()
    assert (
        registry.composite_deliverable_as_of(
            "instrument.acme.common",
            "2024-01-01T00:00:00+00:00",
        )
        == registry.composite_deliverables[0]
    )
    assert (
        registry.composite_deliverable_as_of(
            "instrument.acme.common",
            "2022-01-01T00:00:00+00:00",
        )
        is None
    )
    assert registry.composite_deliverables[0].deliverable_hash().startswith(
        "sha256:"
    )
