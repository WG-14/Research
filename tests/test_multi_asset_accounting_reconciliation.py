from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import cast

import pytest

from market_research.research.hashing import canonical_json_bytes, sha256_prefixed
from market_research.research.multi_asset.accounting import (
    REPORT_ANALYSIS_OBJECT_NAMES,
    AccountingReconciliationError,
    FxRevaluationReceipt,
    LedgerPnlReconciliation,
    PitFxObservation,
    ReportLedgerReconciliation,
    ReportPnlSummary,
    encode_report_payload,
    report_payload_hash,
)
from market_research.research.multi_asset.portfolio import (
    AssetClass,
    CashDelta,
    ExternalFlowConversionEvidence,
    PortfolioAccountingError,
    UnifiedPortfolioLedger,
    funding_event,
    mark_event,
    trade_event,
)


OPENED_AT = "2025-01-01T00:00:00Z"
MIDPOINT_AT = "2025-01-01T12:00:00Z"
CLOSED_AT = "2025-01-02T00:00:00Z"


def _hash(label: str) -> str:
    return sha256_prefixed({"test_evidence": label})


def _empty_ledger(ledger_id: str) -> UnifiedPortfolioLedger:
    return UnifiedPortfolioLedger.open(ledger_id=ledger_id, base_currency="USD")


def _fx(
    *,
    observation_id: str,
    observed_at: str,
    rate: str,
    source_hash: str | None = None,
) -> PitFxObservation:
    return PitFxObservation(
        observation_id=observation_id,
        currency="EUR",
        base_currency="USD",
        observed_at=observed_at,
        rate=Decimal(rate),
        source_hash=(
            source_hash
            if source_hash is not None
            else _hash(f"source:{observation_id}")
        ),
    )


def _funding_evidence(
    *,
    observed_at: str,
    rate: str,
    source_hash: str,
) -> tuple[ExternalFlowConversionEvidence, ...]:
    return (
        ExternalFlowConversionEvidence(
            currency="EUR",
            base_currency="USD",
            observed_at=observed_at,
            fx_rate=Decimal(rate),
            source_hash=source_hash,
        ),
    )


def _usd_projection() -> tuple[UnifiedPortfolioLedger, UnifiedPortfolioLedger]:
    opening = _empty_ledger("ledger.usd")
    closing = opening.publish_many(
        (
            funding_event(
                event_id="funding.usd",
                occurred_at=OPENED_AT,
                cash_deltas=(CashDelta("USD", Decimal("1000")),),
            ),
            trade_event(
                event_id="trade.usd",
                occurred_at=MIDPOINT_AT,
                asset_class=AssetClass.SPOT,
                instrument_id="ASSET.USD",
                currency="USD",
                quantity_delta=Decimal("1"),
                price=Decimal("100"),
            ),
            mark_event(
                event_id="mark.usd",
                occurred_at=CLOSED_AT,
                asset_class=AssetClass.SPOT,
                instrument_id="ASSET.USD",
                currency="USD",
                mark_price=Decimal("110"),
            ),
        )
    )
    return opening, closing


def _eur_projection(
    *,
    with_midpoint_withdrawal: bool = False,
) -> tuple[
    UnifiedPortfolioLedger,
    UnifiedPortfolioLedger,
    tuple[PitFxObservation, ...],
]:
    opening = _empty_ledger("ledger.eur")
    opening_source = _hash("eur-fx-open")
    midpoint_source = _hash("eur-fx-midpoint")
    drafts = [
        funding_event(
            event_id="funding.eur",
            occurred_at=OPENED_AT,
            cash_deltas=(CashDelta("EUR", Decimal("100")),),
            conversion_evidence=_funding_evidence(
                observed_at=OPENED_AT,
                rate="1.10",
                source_hash=opening_source,
            ),
        )
    ]
    if with_midpoint_withdrawal:
        drafts.append(
            funding_event(
                event_id="withdrawal.eur",
                occurred_at=MIDPOINT_AT,
                cash_deltas=(CashDelta("EUR", Decimal("-50")),),
                conversion_evidence=_funding_evidence(
                    observed_at=MIDPOINT_AT,
                    rate="1.15",
                    source_hash=midpoint_source,
                ),
            )
        )
    closing = opening.publish_many(tuple(drafts))
    observations = [
        _fx(
            observation_id="eur-open",
            observed_at=OPENED_AT,
            rate="1.10",
            source_hash=opening_source,
        )
    ]
    if with_midpoint_withdrawal:
        observations.append(
            _fx(
                observation_id="eur-midpoint",
                observed_at=MIDPOINT_AT,
                rate="1.15",
                source_hash=midpoint_source,
            )
        )
    observations.append(
        _fx(
            observation_id="eur-close",
            observed_at=CLOSED_AT,
            rate="1.20",
            source_hash=_hash("eur-fx-close"),
        )
    )
    return opening, closing, tuple(observations)


def _reconcile(
    opening: UnifiedPortfolioLedger,
    closing: UnifiedPortfolioLedger,
    observations: tuple[PitFxObservation, ...] = (),
) -> LedgerPnlReconciliation:
    return LedgerPnlReconciliation.from_ledger_projection(
        reconciliation_id="ledger-reconciliation-1",
        opening_ledger=opening,
        closing_ledger=closing,
        opened_at=OPENED_AT,
        closed_at=CLOSED_AT,
        fx_observations=observations,
    )


def _parse_report(ledger: LedgerPnlReconciliation) -> ReportPnlSummary:
    payload = encode_report_payload(report_id="published-report-1", ledger=ledger)
    return ReportPnlSummary.from_json(
        payload,
        expected_payload_hash=report_payload_hash(payload),
    )


def _payload_object(payload: bytes) -> dict[str, object]:
    return cast(dict[str, object], json.loads(payload))


def test_usd_projection_factory_reconciles_ledger_and_report() -> None:
    opening, closing = _usd_projection()

    ledger = _reconcile(opening, closing)

    assert ledger.fx_revaluation.currency_universe == ("USD",)
    assert ledger.fx_revaluation.intervals == ()
    assert ledger.fx_translation_pnl == Decimal("0")
    assert ledger.opening_nav == Decimal("0")
    assert ledger.external_cash_flow == Decimal("1000")
    assert ledger.closing_nav == Decimal("1010")
    assert ledger.ledger_event_pnl == Decimal("10")
    assert ledger.unrealized_pnl_change == Decimal("10")
    assert ledger.nav_identity_error == Decimal("0")
    assert ledger.attribution_identity_error == Decimal("0")
    assert ledger.fx_revaluation.exposure_ledger_hash == closing.content_hash

    report = _parse_report(ledger)
    receipt = ReportLedgerReconciliation(
        reconciliation_id="report-ledger-usd",
        ledger=ledger,
        report=report,
    )
    assert report.report_rows() == ledger.report_rows()
    assert report.analysis_object_hashes() == ledger.analysis_object_hashes()
    assert receipt.content_hash.startswith("sha256:")


def test_eur_100_at_110_then_120_separates_principal_and_fx_pnl() -> None:
    opening, closing, observations = _eur_projection()

    ledger = _reconcile(opening, closing, observations)

    assert ledger.external_cash_flow == Decimal("110.00")
    assert ledger.closing_nav == Decimal("120.00")
    assert ledger.fx_translation_pnl == Decimal("10.00")
    assert ledger.ledger_event_pnl == Decimal("10.00")
    assert ledger.realized_pnl == Decimal("0")
    assert ledger.unrealized_pnl_change == Decimal("0")
    assert ledger.attribution_pnl == Decimal("10.00")
    assert ledger.fx_revaluation.intervals[0].exposure == Decimal("100")
    assert (
        ledger.fx_revaluation.intervals[0].exposure_source_hash
        == closing.replay().content_hash
    )
    ReportLedgerReconciliation(
        reconciliation_id="report-ledger-eur",
        ledger=ledger,
        report=_parse_report(ledger),
    )


def test_exposure_change_requires_event_time_pit_observation() -> None:
    opening, closing, complete_observations = _eur_projection(
        with_midpoint_withdrawal=True
    )
    gapped_observations = (
        complete_observations[0],
        complete_observations[-1],
    )

    with pytest.raises(
        AccountingReconciliationError,
        match="pit_fx_event_time_observation_missing:EUR",
    ):
        _reconcile(opening, closing, gapped_observations)

    ledger = _reconcile(opening, closing, complete_observations)
    assert [item.exposure for item in ledger.fx_revaluation.intervals] == [
        Decimal("100"),
        Decimal("50"),
    ]
    assert ledger.external_cash_flow == Decimal("52.50")
    assert ledger.fx_translation_pnl == Decimal("7.50")
    assert ledger.closing_nav == Decimal("60.00")
    assert ledger.ledger_event_pnl == Decimal("7.50")


def test_duplicate_pit_timestamp_rejects_overlapping_interval_source() -> None:
    opening, closing, observations = _eur_projection()
    duplicate = replace(
        observations[0],
        observation_id="eur-open-duplicate",
    )

    with pytest.raises(
        AccountingReconciliationError,
        match="pit_fx_timestamp_duplicate",
    ):
        _reconcile(opening, closing, (observations[0], duplicate, observations[1]))


def test_caller_cannot_self_certify_fx_or_ledger_receipts() -> None:
    opening, closing = _usd_projection()
    valid = _reconcile(opening, closing)

    with pytest.raises(
        AccountingReconciliationError,
        match="fx_receipt_requires_ledger_projection_factory",
    ):
        FxRevaluationReceipt(
            receipt_id="self-certified",
            base_currency="USD",
            opened_at=OPENED_AT,
            closed_at=CLOSED_AT,
            currency_universe=("USD",),
            intervals=(),
            exposure_ledger_hash=closing.content_hash,
            opening_ledger_hash=opening.content_hash,
            opening_snapshot_hash=opening.replay().content_hash,
            closing_snapshot_hash=closing.replay().content_hash,
            fx_observation_hashes=(),
        )

    with pytest.raises(
        AccountingReconciliationError,
        match="ledger_reconciliation_requires_projection_factory",
    ):
        replace(valid, closing_nav=Decimal("999999"))


def test_tampered_mark_event_hash_is_rejected_before_reconciliation() -> None:
    opening, closing = _usd_projection()
    mark = closing.events[-1]
    object.__setattr__(mark, "mark_price", Decimal("999"))

    with pytest.raises(
        PortfolioAccountingError,
        match="portfolio_ledger_event_hash_mismatch",
    ):
        _reconcile(opening, closing)


def test_funding_conversion_must_match_pit_source_and_rate() -> None:
    opening, closing, observations = _eur_projection()
    conflicting_open = replace(
        observations[0],
        rate=Decimal("1.11"),
    )

    with pytest.raises(
        AccountingReconciliationError,
        match="funding_conversion_pit_fx_mismatch",
    ):
        _reconcile(opening, closing, (conflicting_open, observations[1]))


def test_report_payload_numeric_tampering_fails_bound_hash() -> None:
    opening, closing = _usd_projection()
    ledger = _reconcile(opening, closing)
    payload = encode_report_payload(report_id="published-report-1", ledger=ledger)
    expected_hash = report_payload_hash(payload)
    document = _payload_object(payload)
    rows = cast(dict[str, object], document["pnl_rows"])
    rows["closing_nav"] = "1011"
    tampered_payload = canonical_json_bytes(document)

    with pytest.raises(AccountingReconciliationError, match="payload_hash_mismatch"):
        ReportPnlSummary.from_json(
            tampered_payload,
            expected_payload_hash=expected_hash,
        )


def test_self_consistent_false_report_numbers_fail_ledger_comparison() -> None:
    opening, closing = _usd_projection()
    ledger = _reconcile(opening, closing)
    payload = encode_report_payload(report_id="published-report-1", ledger=ledger)
    document = _payload_object(payload)
    rows = cast(dict[str, object], document["pnl_rows"])
    rows["closing_nav"] = "1011"
    rows["ledger_pnl"] = "11"
    rows["realized_pnl"] = "1"
    rows["attribution_pnl"] = "11"
    forged_payload = canonical_json_bytes(document)
    forged_report = ReportPnlSummary.from_json(
        forged_payload,
        expected_payload_hash=report_payload_hash(forged_payload),
    )

    with pytest.raises(AccountingReconciliationError, match="numeric_rows_mismatch"):
        ReportLedgerReconciliation(
            reconciliation_id="forged-numeric-report",
            ledger=ledger,
            report=forged_report,
        )


def test_report_analysis_hash_tampering_fails_ledger_comparison() -> None:
    opening, closing = _usd_projection()
    ledger = _reconcile(opening, closing)
    payload = encode_report_payload(report_id="published-report-1", ledger=ledger)
    document = _payload_object(payload)
    hashes = cast(dict[str, object], document["analysis_object_hashes"])
    hashes["costs"] = _hash("forged-cost-analysis")
    forged_payload = canonical_json_bytes(document)
    forged_report = ReportPnlSummary.from_json(
        forged_payload,
        expected_payload_hash=report_payload_hash(forged_payload),
    )

    with pytest.raises(
        AccountingReconciliationError,
        match="analysis_hashes_mismatch",
    ):
        ReportLedgerReconciliation(
            reconciliation_id="forged-analysis-report",
            ledger=ledger,
            report=forged_report,
        )


def test_missing_report_or_pit_hash_fails_closed() -> None:
    opening, closing = _usd_projection()
    ledger = _reconcile(opening, closing)
    payload = encode_report_payload(report_id="published-report-1", ledger=ledger)
    document = _payload_object(payload)
    hashes = cast(dict[str, object], document["analysis_object_hashes"])
    del hashes[REPORT_ANALYSIS_OBJECT_NAMES[0]]
    incomplete_payload = canonical_json_bytes(document)

    with pytest.raises(
        AccountingReconciliationError,
        match="analysis_object_hashes_schema_invalid",
    ):
        ReportPnlSummary.from_json(
            incomplete_payload,
            expected_payload_hash=report_payload_hash(incomplete_payload),
        )

    with pytest.raises(AccountingReconciliationError, match="source_hash_invalid"):
        _fx(
            observation_id="missing-source-hash",
            observed_at=OPENED_AT,
            rate="1.10",
            source_hash="",
        )
