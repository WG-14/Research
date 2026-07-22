from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from typing import Any, cast

import pytest

from market_research.research.derivatives.common import (
    DerivativeResearchError,
    InstrumentKind,
)
from market_research.research.derivatives.portfolio import (
    PORTFOLIO_EXPOSURE_AGGREGATION_POLICY_HASH,
    ExpiryConcentration,
    ExposureGroup,
    PortfolioExposureSnapshot,
    PositionEvidenceHashes,
    PositionExposure,
    StressPnL,
    stress_set_hash,
)
from market_research.research.hashing import sha256_prefixed


AS_OF = "2026-01-02T00:00:00Z"
CAPITAL_STARTED = "2026-01-01T00:00:00Z"


def _digest(label: str) -> str:
    return sha256_prefixed({"fixture": label}, label="portfolio_test_fixture")


def _stress(
    crash_pnl: str,
    rally_pnl: str,
    *,
    crash_hash: str | None = None,
) -> tuple[StressPnL, ...]:
    return (
        StressPnL(
            scenario_id="CRASH",
            pnl=Decimal(crash_pnl),
            scenario_hash=crash_hash or _digest("scenario.crash"),
        ),
        StressPnL(
            scenario_id="RALLY",
            pnl=Decimal(rally_pnl),
            scenario_hash=_digest("scenario.rally"),
        ),
    )


def _position(
    *,
    position_id: str,
    instrument_id: str,
    kind: InstrumentKind,
    underlying_id: str,
    currency: str,
    expiry_at: str | None,
    multiplier: str,
    delta: str,
    gamma: str = "0",
    vega: str = "0",
    theta: str = "0",
    rho: str = "0",
    premium: str = "0",
    margin: str = "0",
    collateral: str = "0",
    crash_pnl: str = "0",
    rally_pnl: str = "0",
    stress: tuple[StressPnL, ...] | None = None,
) -> PositionExposure:
    stress_values = stress or _stress(crash_pnl, rally_pnl)
    evidence = PositionEvidenceHashes(
        dataset_hash=_digest(f"{position_id}.dataset"),
        instrument_hash=_digest(f"{position_id}.instrument"),
        valuation_hash=_digest(f"{position_id}.valuation"),
        stress_hash=stress_set_hash(stress_values),
    )
    return PositionExposure(
        position_id=position_id,
        instrument_id=instrument_id,
        instrument_kind=kind,
        underlying_id=underlying_id,
        currency=currency,
        as_of=AS_OF,
        capital_use_started_at=CAPITAL_STARTED,
        expiry_at=expiry_at,
        multiplier=Decimal(multiplier),
        delta=Decimal(delta),
        gamma=Decimal(gamma),
        vega=Decimal(vega),
        theta=Decimal(theta),
        rho=Decimal(rho),
        premium=Decimal(premium),
        margin_required=Decimal(margin),
        collateral_cash=Decimal(collateral),
        capital_use_seconds=Decimal("86400"),
        stress_pnl=stress_values,
        evidence_hashes=evidence,
    )


@pytest.fixture
def mixed_positions() -> tuple[PositionExposure, ...]:
    spot = _position(
        position_id="spot.krw",
        instrument_id="KRW-SPOT",
        kind=InstrumentKind.SPOT,
        underlying_id="KRW-ASSET",
        currency="KRW",
        expiry_at=None,
        multiplier="1",
        delta="1000",
        crash_pnl="-100",
        rally_pnl="60",
    )
    future = _position(
        position_id="future.usd",
        instrument_id="ESM26",
        kind=InstrumentKind.FUTURE,
        underlying_id="SPX",
        currency="USD",
        expiry_at="2026-06-19T20:00:00Z",
        multiplier="50",
        delta="50",
        theta="-1",
        rho="2",
        margin="50",
        collateral="25",
        crash_pnl="-30",
        rally_pnl="25",
    )
    option = _position(
        position_id="option.usd",
        instrument_id="SPX-202603-C-5000",
        kind=InstrumentKind.OPTION,
        underlying_id="SPX",
        currency="USD",
        expiry_at="2026-03-20T20:00:00Z",
        multiplier="100",
        delta="-10",
        gamma="3",
        vega="7",
        theta="-2",
        rho="1",
        premium="-5",
        margin="10",
        collateral="10",
        crash_pnl="10",
        rally_pnl="-15",
    )
    return (spot, future, option)


def test_cross_product_snapshot_aggregates_exact_exposures_and_evidence(
    mixed_positions: tuple[PositionExposure, ...],
) -> None:
    snapshot = PortfolioExposureSnapshot(
        snapshot_id="mixed.exposure",
        as_of=AS_OF,
        positions=mixed_positions,
    )

    assert snapshot.currencies == ("KRW", "USD")
    assert snapshot.aggregation_policy_hash == (
        PORTFOLIO_EXPOSURE_AGGREGATION_POLICY_HASH
    )
    usd = snapshot.currency_total("USD")
    assert usd.group is ExposureGroup.CURRENCY
    assert usd.position_count == 2
    assert usd.delta == Decimal("40")
    assert usd.gamma == Decimal("3")
    assert usd.vega == Decimal("7")
    assert usd.theta == Decimal("-3")
    assert usd.rho == Decimal("3")
    assert usd.premium == Decimal("-5")
    assert usd.margin_required == Decimal("60")
    assert usd.collateral_cash == Decimal("35")
    assert usd.capital_use_seconds == Decimal("172800")
    assert snapshot.total_premium_by_currency == (
        ("KRW", Decimal("0")),
        ("USD", Decimal("-5")),
    )
    assert snapshot.total_margin_by_currency == (
        ("KRW", Decimal("0")),
        ("USD", Decimal("60")),
    )
    assert snapshot.total_collateral_by_currency == (
        ("KRW", Decimal("0")),
        ("USD", Decimal("35")),
    )
    assert snapshot.total_capital_use_seconds == Decimal("259200")

    spx = [
        item
        for item in snapshot.underlying_exposure
        if item.group_value == "SPX" and item.currency == "USD"
    ]
    assert len(spx) == 1
    assert spx[0].delta == Decimal("40")
    assert len(snapshot.expiry_exposure) == 2
    assert {item.group_value for item in snapshot.expiry_exposure} == {
        "2026-03-20T20:00:00Z",
        "2026-06-19T20:00:00Z",
    }
    assert snapshot.stress_total("CRASH", "USD") == Decimal("-20")
    assert snapshot.stress_total("CRASH", "KRW") == Decimal("-100")

    payload = cast(dict[str, Any], snapshot.as_dict())
    assert payload["content_hash"] == snapshot.content_hash
    assert payload["positions"][0]["evidence_hashes"]["dataset_hash"].startswith(
        "sha256:"
    )
    rebuilt = PortfolioExposureSnapshot(
        snapshot_id="mixed.exposure",
        as_of=AS_OF,
        positions=tuple(reversed(mixed_positions)),
    )
    assert rebuilt.content_hash == snapshot.content_hash


def test_multi_currency_monetary_and_stress_totals_fail_closed_without_fx(
    mixed_positions: tuple[PositionExposure, ...],
) -> None:
    snapshot = PortfolioExposureSnapshot(
        snapshot_id="fx.fail.safe",
        as_of=AS_OF,
        positions=mixed_positions,
    )

    with pytest.raises(
        DerivativeResearchError,
        match="portfolio_snapshot_fx_conversion_required",
    ):
        snapshot.monetary_total("premium")
    with pytest.raises(
        DerivativeResearchError,
        match="portfolio_snapshot_fx_conversion_required",
    ):
        snapshot.stress_total("CRASH")
    assert snapshot.monetary_total("premium", "USD") == Decimal("-5")
    assert snapshot.monetary_total("collateral_cash", "KRW") == Decimal("0")


def test_expiry_concentration_is_per_currency_and_has_explicit_basis(
    mixed_positions: tuple[PositionExposure, ...],
) -> None:
    snapshot = PortfolioExposureSnapshot(
        snapshot_id="expiry.concentration",
        as_of=AS_OF,
        positions=mixed_positions,
    )
    by_currency = {
        item.currency: item for item in snapshot.expiry_concentration_by_currency
    }

    usd = by_currency["USD"]
    assert usd.available
    assert usd.total_capital_basis == Decimal("100")
    assert tuple(item.capital_basis for item in usd.shares) == (
        Decimal("25"),
        Decimal("75"),
    )
    assert tuple(item.share for item in usd.shares) == (
        Decimal("0.25"),
        Decimal("0.75"),
    )
    assert usd.maximum_share == Decimal("0.75")
    assert usd.herfindahl_index == Decimal("0.6250")

    krw = by_currency["KRW"]
    assert not krw.available
    assert krw.unavailable_reason == "no_expiring_positions"
    assert krw.total_capital_basis == Decimal("0")


def test_single_currency_boundary_and_immutable_contract() -> None:
    spot = _position(
        position_id="spot.only",
        instrument_id="SPOT-ONLY",
        kind=InstrumentKind.SPOT,
        underlying_id="SPOT-ONLY",
        currency="USD",
        expiry_at=None,
        multiplier="1",
        delta="0",
    )
    snapshot = PortfolioExposureSnapshot(
        snapshot_id="single.currency",
        as_of=AS_OF,
        positions=(spot,),
    )

    assert snapshot.monetary_total("premium") == Decimal("0")
    assert snapshot.stress_total("CRASH") == Decimal("0")
    assert snapshot.expiry_concentration_by_currency[0].unavailable_reason == (
        "no_expiring_positions"
    )
    with pytest.raises(FrozenInstanceError):
        spot.delta = Decimal("1")  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.content_hash = _digest("tampered")  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "value", "error"),
    [
        ("expiry_at", "2026-03-01T00:00:00Z", "portfolio_spot_expiry_forbidden"),
        ("multiplier", Decimal("2"), "portfolio_spot_multiplier_must_be_one"),
        ("premium", Decimal("1"), "portfolio_spot_premium_must_be_zero"),
        ("margin_required", Decimal("1"), "portfolio_spot_margin_must_be_zero"),
    ],
)
def test_spot_rejects_derivative_only_semantics(
    field_name: str,
    value: object,
    error: str,
) -> None:
    spot = _position(
        position_id="spot.rules",
        instrument_id="SPOT-RULES",
        kind=InstrumentKind.SPOT,
        underlying_id="SPOT-RULES",
        currency="USD",
        expiry_at=None,
        multiplier="1",
        delta="1",
    )
    with pytest.raises(DerivativeResearchError, match=error):
        cast(Any, replace)(spot, **{field_name: value})


def test_future_and_option_require_future_expiry_and_future_has_no_premium() -> None:
    future = _position(
        position_id="future.rules",
        instrument_id="FUTURE-RULES",
        kind=InstrumentKind.FUTURE,
        underlying_id="INDEX",
        currency="USD",
        expiry_at="2026-06-01T00:00:00Z",
        multiplier="10",
        delta="10",
    )
    option = _position(
        position_id="option.rules",
        instrument_id="OPTION-RULES",
        kind=InstrumentKind.OPTION,
        underlying_id="INDEX",
        currency="USD",
        expiry_at="2026-03-01T00:00:00Z",
        multiplier="100",
        delta="1",
    )

    with pytest.raises(
        DerivativeResearchError,
        match="portfolio_derivative_expiry_required",
    ):
        replace(future, expiry_at=None)
    with pytest.raises(
        DerivativeResearchError,
        match="portfolio_derivative_expiry_not_after_as_of",
    ):
        replace(option, expiry_at=AS_OF)
    with pytest.raises(
        DerivativeResearchError,
        match="portfolio_future_premium_must_be_zero",
    ):
        replace(future, premium=Decimal("0.01"))


@pytest.mark.parametrize(
    ("field_name", "value", "error"),
    [
        ("delta", 0.1, "portfolio_position.delta_must_be_decimal_text_or_integer"),
        ("gamma", Decimal("NaN"), "portfolio_position.gamma_non_finite"),
        ("vega", Decimal("Infinity"), "portfolio_position.vega_non_finite"),
        (
            "margin_required",
            Decimal("-0.01"),
            "portfolio_position.margin_required_must_be_non_negative",
        ),
        (
            "collateral_cash",
            Decimal("-0.01"),
            "portfolio_position.collateral_cash_must_be_non_negative",
        ),
        (
            "capital_use_seconds",
            Decimal("-1"),
            "portfolio_position.capital_use_seconds_must_be_non_negative",
        ),
    ],
)
def test_non_exact_non_finite_and_negative_values_are_rejected(
    field_name: str,
    value: object,
    error: str,
) -> None:
    position = _position(
        position_id="decimal.rules",
        instrument_id="DECIMAL-RULES",
        kind=InstrumentKind.OPTION,
        underlying_id="INDEX",
        currency="USD",
        expiry_at="2026-03-01T00:00:00Z",
        multiplier="100",
        delta="1",
    )
    with pytest.raises(DerivativeResearchError, match=error):
        cast(Any, replace)(position, **{field_name: value})


def test_time_order_and_exact_capital_seconds_are_enforced() -> None:
    position = _position(
        position_id="time.rules",
        instrument_id="TIME-RULES",
        kind=InstrumentKind.OPTION,
        underlying_id="INDEX",
        currency="USD",
        expiry_at="2026-03-01T00:00:00Z",
        multiplier="100",
        delta="1",
    )

    with pytest.raises(
        DerivativeResearchError,
        match="portfolio_position_capital_start_after_as_of",
    ):
        replace(position, capital_use_started_at="2026-01-03T00:00:00Z")
    with pytest.raises(
        DerivativeResearchError,
        match="portfolio_position_capital_seconds_mismatch",
    ):
        replace(position, capital_use_seconds=Decimal("86399.999999"))
    with pytest.raises(
        DerivativeResearchError,
        match="portfolio_snapshot_as_of_mismatch",
    ):
        PortfolioExposureSnapshot(
            snapshot_id="time.mismatch",
            as_of="2026-01-02T00:00:01Z",
            positions=(position,),
        )


def test_duplicate_positions_and_instruments_cannot_be_double_counted() -> None:
    first = _position(
        position_id="duplicate.one",
        instrument_id="DUPLICATE-INSTRUMENT",
        kind=InstrumentKind.FUTURE,
        underlying_id="INDEX",
        currency="USD",
        expiry_at="2026-06-01T00:00:00Z",
        multiplier="10",
        delta="1",
    )
    second = replace(first, position_id="duplicate.two")

    with pytest.raises(
        DerivativeResearchError,
        match="portfolio_snapshot_position_duplicate",
    ):
        PortfolioExposureSnapshot(
            snapshot_id="duplicate.position",
            as_of=AS_OF,
            positions=(first, first),
        )
    with pytest.raises(
        DerivativeResearchError,
        match="portfolio_snapshot_instrument_duplicate",
    ):
        PortfolioExposureSnapshot(
            snapshot_id="duplicate.instrument",
            as_of=AS_OF,
            positions=(first, second),
        )


def test_stress_scenario_completeness_and_hash_binding_are_fail_closed() -> None:
    first = _position(
        position_id="stress.one",
        instrument_id="STRESS-ONE",
        kind=InstrumentKind.FUTURE,
        underlying_id="INDEX",
        currency="USD",
        expiry_at="2026-06-01T00:00:00Z",
        multiplier="10",
        delta="1",
    )
    changed_definition = _stress(
        "0",
        "0",
        crash_hash=_digest("scenario.crash.changed"),
    )
    second = _position(
        position_id="stress.two",
        instrument_id="STRESS-TWO",
        kind=InstrumentKind.OPTION,
        underlying_id="INDEX",
        currency="USD",
        expiry_at="2026-03-01T00:00:00Z",
        multiplier="100",
        delta="1",
        stress=changed_definition,
    )

    with pytest.raises(
        DerivativeResearchError,
        match="portfolio_snapshot_stress_scenario_set_mismatch",
    ):
        PortfolioExposureSnapshot(
            snapshot_id="stress.mismatch",
            as_of=AS_OF,
            positions=(first, second),
        )
    bad_evidence = replace(
        first.evidence_hashes,
        stress_hash=_digest("wrong.stress.set"),
    )
    with pytest.raises(
        DerivativeResearchError,
        match="portfolio_stress_evidence_hash_mismatch",
    ):
        replace(first, evidence_hashes=bad_evidence)
    with pytest.raises(
        DerivativeResearchError,
        match="portfolio_snapshot_aggregation_policy_unsupported",
    ):
        PortfolioExposureSnapshot(
            snapshot_id="policy.mismatch",
            as_of=AS_OF,
            positions=(first,),
            aggregation_policy_hash=_digest("unsupported.policy"),
        )


def test_invalid_duplicate_and_mutable_evidence_inputs_are_rejected() -> None:
    shared_hash = _digest("shared")
    with pytest.raises(
        DerivativeResearchError,
        match="portfolio_evidence_hash_duplicate",
    ):
        PositionEvidenceHashes(
            dataset_hash=shared_hash,
            instrument_hash=shared_hash,
            valuation_hash=_digest("valuation"),
            stress_hash=_digest("stress"),
        )
    with pytest.raises(
        DerivativeResearchError,
        match="portfolio_evidence.dataset_hash_invalid_hash",
    ):
        PositionEvidenceHashes(
            dataset_hash="not-a-hash",
            instrument_hash=_digest("instrument"),
            valuation_hash=_digest("valuation"),
            stress_hash=_digest("stress"),
        )

    position = _position(
        position_id="mutable.rules",
        instrument_id="MUTABLE-RULES",
        kind=InstrumentKind.OPTION,
        underlying_id="INDEX",
        currency="USD",
        expiry_at="2026-03-01T00:00:00Z",
        multiplier="100",
        delta="1",
    )
    mutable_stress = cast(Any, list(position.stress_pnl))
    with pytest.raises(
        DerivativeResearchError,
        match="portfolio_stress_tuple_required",
    ):
        replace(position, stress_pnl=mutable_stress)
    mutable_positions = cast(Any, [position])
    with pytest.raises(
        DerivativeResearchError,
        match="portfolio_snapshot_positions_tuple_required",
    ):
        PortfolioExposureSnapshot(
            snapshot_id="mutable.positions",
            as_of=AS_OF,
            positions=mutable_positions,
        )


def test_expiry_concentration_constructor_rejects_inconsistent_zero_basis() -> None:
    with pytest.raises(
        DerivativeResearchError,
        match="portfolio_expiry_concentration_unavailable_mismatch",
    ):
        ExpiryConcentration(
            currency="USD",
            total_capital_basis=Decimal("0"),
            shares=(),
            maximum_share=None,
            herfindahl_index=None,
            unavailable_reason=None,
        )
