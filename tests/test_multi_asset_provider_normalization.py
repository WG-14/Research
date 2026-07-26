from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from market_research.research.hashing import sha256_prefixed
from market_research.research.market_calendar_contract import (
    MARKET_CALENDAR_SCHEMA_VERSION,
    CalendarException,
    MarketCalendarAuthority,
    WeeklySessionRule,
)
from market_research.research.multi_asset.data import DataLayer
from market_research.research.multi_asset.normalization import (
    CalendarRegistry,
    CorporateActionAdjustment,
    EpochScaledReciprocalAdapter,
    IsoLocalDirectAdapter,
    MissingValueSemantics,
    ProviderNormalizationError,
    ProviderNormalizationPolicy,
    ProviderNormalizationService,
    ProviderRow,
    QuoteConvention,
    UnitDefinition,
    UnitRegistry,
    derive_normalized_value,
)


def _hash(value: str) -> str:
    return sha256_prefixed(value, label="provider-normalization-test")


def _calendar(
    *,
    calendar_id: str = "cal_continuous_test",
    timezone_name: str = "UTC",
    session: bool = False,
) -> MarketCalendarAuthority:
    weekly = (
        (
            WeeklySessionRule(
                weekday=4,
                open_local="09:30",
                close_local="16:00",
            ),
        )
        if session
        else ()
    )
    exceptions = (
        (
            CalendarException(
                exception_id="calex_earlyclose_test",
                local_date="2026-01-02",
                kind="early_close",
                reason="reviewed early close",
                published_at="2025-12-01T00:00:00+00:00",
                observed_at="2025-12-01T00:01:00+00:00",
                source_content_hash=_hash("exception"),
                close_local="13:00",
            ),
        )
        if session
        else ()
    )
    return MarketCalendarAuthority(
        schema_version=MARKET_CALENDAR_SCHEMA_VERSION,
        calendar_id=calendar_id,
        calendar_version_id=f"calv_{calendar_id[4:]}_v1",
        version=1,
        market_mode="session" if session else "continuous_24x7",
        timezone_name=timezone_name,
        tzdb_version="2026a",
        dst_transition_policy=(
            "iana_tzdb_reject_ambiguous_or_nonexistent_local_time"
        ),
        valid_from="2025-01-01",
        valid_to="2027-12-31",
        source_uri=f"/tmp/codex-gap-closure/{calendar_id}.json",
        source_content_hash=_hash(calendar_id),
        source_schema_hash=_hash("calendar-schema"),
        published_at="2025-01-01T00:00:00+00:00",
        observed_at="2025-01-01T00:01:00+00:00",
        weekly_sessions=weekly,
        exceptions=exceptions,
    )


def _unit_registry() -> UnitRegistry:
    return UnitRegistry(
        registry_id="units.research.v1",
        version="v1",
        definitions=(
            UnitDefinition(
                unit_id="USD_per_share",
                dimension="price",
                canonical_unit_id="USD_per_share",
                scale_to_canonical=Decimal("1"),
                version="v1",
                source_hash=_hash("usd-per-share"),
            ),
            UnitDefinition(
                unit_id="lot",
                dimension="quantity",
                canonical_unit_id="share",
                scale_to_canonical=Decimal("10"),
                version="v1",
                source_hash=_hash("lot"),
            ),
            UnitDefinition(
                unit_id="share",
                dimension="quantity",
                canonical_unit_id="share",
                scale_to_canonical=Decimal("1"),
                version="v1",
                source_hash=_hash("share"),
            ),
            UnitDefinition(
                unit_id="share_per_USD",
                dimension="price",
                canonical_unit_id="USD_per_share",
                scale_to_canonical=Decimal("1"),
                version="v1",
                source_hash=_hash("share-per-usd"),
            ),
        ),
    )


def _service(
    calendar: MarketCalendarAuthority | None = None,
) -> ProviderNormalizationService:
    selected = calendar or _calendar()
    return ProviderNormalizationService(
        calendar_registry=CalendarRegistry(
            registry_id="calendars.research.v1",
            version="v1",
            calendars=(selected,),
        ),
        unit_registry=_unit_registry(),
        adapters=(
            EpochScaledReciprocalAdapter(),
            IsoLocalDirectAdapter(),
        ),
    )


def _policy(
    *,
    provider_id: str,
    adapter: str,
    calendar: MarketCalendarAuthority | None = None,
) -> ProviderNormalizationPolicy:
    selected = calendar or _calendar()
    reciprocal = adapter == "reciprocal"
    return ProviderNormalizationPolicy(
        policy_id=f"normalize.{provider_id}.v1",
        version="v1",
        provider_id=provider_id,
        instrument_id="inst_equity_primary",
        provider_symbol="ACME",
        calendar_id=selected.calendar_id,
        exchange_session_id="REGULAR",
        source_timezone=selected.timezone_name,
        source_price_unit=(
            "share_per_USD" if reciprocal else "USD_per_share"
        ),
        target_price_unit="USD_per_share",
        source_quantity_unit="lot" if reciprocal else "share",
        target_quantity_unit="share",
        currency="USD",
        contract_multiplier=Decimal("1"),
        price_scale=Decimal("0.01") if reciprocal else Decimal("1"),
        quantity_scale=Decimal("1"),
        quote_convention=(
            QuoteConvention.RECIPROCAL
            if reciprocal
            else QuoteConvention.DIRECT
        ),
        corporate_action_adjustment=CorporateActionAdjustment.RAW,
        missing_value_semantics=MissingValueSemantics.REJECT_ROW,
    )


def _iso_row(
    *,
    revision: int = 1,
    supersedes_hash: str | None = None,
    price: str = "100",
    ingested_at: str = "2026-01-02T10:00:03+00:00",
) -> ProviderRow:
    return ProviderRow(
        row_id="row.acme.20260102.1000",
        provider_id="provider.iso",
        provider_version="v1",
        schema_id="iso.quote.v1",
        source_object_id="object.iso.20260102",
        collection_batch_id="batch.iso.20260102",
        source_artifact_hash=_hash("iso-artifact"),
        source_schema_hash=_hash("iso-schema"),
        ingested_at=ingested_at,
        revision=revision,
        supersedes_hash=supersedes_hash,
        fields={
            "symbol": "ACME",
            "local_timestamp": "2026-01-02T10:00:00",
            "published_at": "2026-01-02T10:00:01+00:00",
            "available_at": "2026-01-02T10:00:02+00:00",
            "session_id": "REGULAR",
            "price": price,
            "quantity": "5",
        },
    )


def _epoch_row() -> ProviderRow:
    def millis(value: str) -> str:
        parsed = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        return str(int(parsed.timestamp() * 1000))

    return ProviderRow(
        row_id="row.acme.epoch.20260102.1000",
        provider_id="provider.epoch",
        provider_version="v7",
        schema_id="epoch.inverse.v7",
        source_object_id="object.epoch.20260102",
        collection_batch_id="batch.epoch.20260102",
        source_artifact_hash=_hash("epoch-artifact"),
        source_schema_hash=_hash("epoch-schema"),
        ingested_at="2026-01-02T10:00:03+00:00",
        fields={
            "ticker": "ACME",
            "event_ms": millis("2026-01-02T10:00:00"),
            "publication_ms": millis("2026-01-02T10:00:01"),
            "availability_ms": millis("2026-01-02T10:00:02"),
            "session_code": "REGULAR",
            "reciprocal_price_scaled": "1",
            "size_lots": "0.5",
        },
    )


def test_two_provider_conventions_normalize_to_same_economic_meaning() -> None:
    service = _service()
    direct = service.normalize(
        _iso_row(),
        policy=_policy(provider_id="provider.iso", adapter="direct"),
        adapter_id="provider.iso-local-direct",
    )
    reciprocal = service.normalize(
        _epoch_row(),
        policy=_policy(provider_id="provider.epoch", adapter="reciprocal"),
        adapter_id="provider.epoch-scaled-reciprocal",
    )

    economic_fields = (
        "event_at",
        "trading_date",
        "session_id",
        "instrument_id",
        "price",
        "quantity",
        "currency",
        "price_unit",
        "quantity_unit",
        "contract_multiplier",
    )
    for field in economic_fields:
        assert direct.normalized_record.payload[field] == (
            reciprocal.normalized_record.payload[field]
        )
    assert direct.normalized_record.payload["price"] == "100"
    assert direct.normalized_record.payload["quantity"] == "5"
    assert direct.store.trace_to_raw(
        direct.normalized_record.record_hash()
    ) == (direct.raw_record,)
    assert direct.receipt.raw_record_hash == direct.raw_record.record_hash()
    assert direct.receipt.normalized_record_hash == (
        direct.normalized_record.record_hash()
    )


def test_late_correction_preserves_raw_and_normalized_revision_history() -> None:
    service = _service()
    original = service.normalize(
        _iso_row(),
        policy=_policy(provider_id="provider.iso", adapter="direct"),
        adapter_id="provider.iso-local-direct",
    )
    correction = _iso_row(
        revision=2,
        supersedes_hash=original.raw_record.record_hash(),
        price="101",
        ingested_at="2026-01-02T11:00:03+00:00",
    )
    correction = replace(
        correction,
        fields={
            **correction.fields,
            "published_at": "2026-01-02T11:00:01+00:00",
            "available_at": "2026-01-02T11:00:02+00:00",
        },
    )
    revised = service.normalize(
        correction,
        policy=_policy(provider_id="provider.iso", adapter="direct"),
        adapter_id="provider.iso-local-direct",
        store=original.store,
    )

    before = revised.store.query_as_of(
        event_as_of="2026-01-02T10:00:00+00:00",
        knowledge_as_of="2026-01-02T10:59:59+00:00",
        layer=DataLayer.NORMALIZED,
    )
    after = revised.store.query_as_of(
        event_as_of="2026-01-02T10:00:00+00:00",
        knowledge_as_of="2026-01-02T11:00:03+00:00",
        layer=DataLayer.NORMALIZED,
    )
    assert [item.payload["price"] for item in before] == ["100"]
    assert [item.payload["price"] for item in after] == ["101"]
    assert [
        item.version
        for item in revised.store.correction_history(
            revised.normalized_record.record_id,
            layer=DataLayer.NORMALIZED,
        )
    ] == [1, 2]


def test_calendar_early_close_dst_units_and_arrival_order_fail_closed() -> None:
    calendar = _calendar(
        calendar_id="cal_newyork_session",
        timezone_name="America/New_York",
        session=True,
    )
    service = _service(calendar)
    policy = _policy(
        provider_id="provider.iso",
        adapter="direct",
        calendar=calendar,
    )
    after_close = replace(
        _iso_row(),
        fields={
            **_iso_row().fields,
            "local_timestamp": "2026-01-02T14:00:00",
            "published_at": "2026-01-02T19:00:01+00:00",
            "available_at": "2026-01-02T19:00:02+00:00",
        },
        ingested_at="2026-01-02T19:00:03+00:00",
    )
    with pytest.raises(
        ProviderNormalizationError, match="provider_event_outside_session"
    ):
        service.normalize(
            after_close,
            policy=policy,
            adapter_id="provider.iso-local-direct",
        )

    dst_ambiguous = replace(
        after_close,
        fields={
            **after_close.fields,
            "local_timestamp": "2026-11-01T01:30:00",
            "published_at": "2026-11-01T06:30:01+00:00",
            "available_at": "2026-11-01T06:30:02+00:00",
        },
        ingested_at="2026-11-01T06:30:03+00:00",
    )
    with pytest.raises(
        ProviderNormalizationError,
        match="dst_ambiguous_or_nonexistent",
    ):
        service.normalize(
            dst_ambiguous,
            policy=policy,
            adapter_id="provider.iso-local-direct",
        )

    with pytest.raises(ProviderNormalizationError, match="unit_dimension_mismatch"):
        _unit_registry().convert(
            Decimal("1"), source="share", target="USD_per_share"
        )

    later = replace(
        _iso_row(),
        row_id="row.acme.later",
        ingested_at="2026-01-02T12:00:00+00:00",
    )
    earlier = replace(
        _iso_row(),
        row_id="row.acme.earlier",
        ingested_at="2026-01-02T11:00:00+00:00",
    )
    with pytest.raises(
        ProviderNormalizationError, match="out_of_order_arrival"
    ):
        _service().normalize_many(
            (later, earlier),
            policies={"provider.iso": _policy(
                provider_id="provider.iso", adapter="direct"
            )},
            adapter_ids={"provider.iso": "provider.iso-local-direct"},
        )


def test_derived_record_binds_code_policy_inputs_and_reverse_lineage() -> None:
    service = _service()
    normalized = service.normalize(
        _iso_row(),
        policy=_policy(provider_id="provider.iso", adapter="direct"),
        adapter_id="provider.iso-local-direct",
    )
    complete, derived = derive_normalized_value(
        normalized.store,
        record_id="derived.acme.simple.return",
        instrument_id="inst_equity_primary",
        data_kind="simple_return",
        generated_at="2026-01-02T10:00:04+00:00",
        model_id="simple.return",
        model_version="v1",
        code_version="commit.test",
        code_hash=_hash("code"),
        policy_hash=_hash("return-policy"),
        upstream_record_hashes=(normalized.normalized_record.record_hash(),),
        payload={"return": "0.01", "unit": "decimal_return"},
        quality_flags=("fixture_only",),
    )

    assert complete.trace_to_raw(derived.record_hash()) == (
        normalized.raw_record,
    )
    assert complete.lineage_descendants(
        normalized.raw_record.record_hash()
    ) == (normalized.normalized_record, derived)
    assert complete.propagated_quality_flags(derived.record_hash()) == (
        "fixture_only",
    )
    with pytest.raises(ProviderNormalizationError, match="raw_upstream_forbidden"):
        derive_normalized_value(
            normalized.store,
            record_id="derived.acme.bad",
            instrument_id="inst_equity_primary",
            data_kind="bad",
            generated_at="2026-01-02T10:00:04+00:00",
            model_id="bad.model",
            model_version="v1",
            code_version="commit.test",
            code_hash=_hash("code"),
            policy_hash=_hash("bad-policy"),
            upstream_record_hashes=(normalized.raw_record.record_hash(),),
            payload={"value": "1"},
        )
