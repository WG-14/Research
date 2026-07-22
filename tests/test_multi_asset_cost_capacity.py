from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from market_research.research.multi_asset.costs import (
    CalibratedImpactCostModel,
    ExecutionContext,
    ExecutionCostError,
    ExecutionSide,
    FillDisposition,
    LinearExecutionCostModel,
    LiquidityImpactCalibration,
    analyze_capacity,
)


SOURCE_HASH = "sha256:" + "a" * 64


def _calibration(**changes: object) -> LiquidityImpactCalibration:
    values: dict[str, object] = {
        "calibration_id": "btc-impact-2026-01-02",
        "instrument_id": "spot:btc-usd",
        "instrument_kind": "SPOT",
        "currency": "USD",
        "observed_at": "2026-01-02T15:59:00+00:00",
        "known_at": "2026-01-02T15:59:30+00:00",
        "capacity_quantity": Decimal("1000"),
        "daily_volatility": Decimal("0.02"),
        "half_spread_bps": Decimal("5"),
        "square_root_coefficient": Decimal("0.5"),
        "maximum_participation_rate": Decimal("0.5"),
        "source_hashes": (SOURCE_HASH,),
    }
    values.update(changes)
    return LiquidityImpactCalibration(**values)  # type: ignore[arg-type]


def _context(
    execution_id: str,
    *,
    requested: str,
    filled: str,
    observed_at: str = "2026-01-02T16:00:00+00:00",
) -> ExecutionContext:
    filled_quantity = Decimal(filled)
    return ExecutionContext(
        execution_id=execution_id,
        instrument_id="spot:btc-usd",
        instrument_kind="SPOT",
        currency="USD",
        side=ExecutionSide.BUY,
        requested_quantity=Decimal(requested),
        filled_quantity=filled_quantity,
        reference_price=Decimal("100"),
        execution_price=Decimal("100") if filled_quantity else None,
        observed_at=observed_at,
        capacity_quantity=Decimal("1000"),
        source_hashes=(SOURCE_HASH,),
    )


def test_calibrated_impact_is_nonlinear_and_hash_bound() -> None:
    model = CalibratedImpactCostModel(
        calibrations=(_calibration(),),
        base_model=LinearExecutionCostModel(commission_per_unit=Decimal("0.01")),
    )

    small = model.estimate(_context("small", requested="100", filled="100"))
    large = model.estimate(_context("large", requested="500", filled="500"))

    assert small.spread == Decimal("5")
    assert large.spread == Decimal("25")
    assert large.market_impact / Decimal("50000") > (
        small.market_impact / Decimal("10000")
    )
    assert small.commission == Decimal("1")
    assert model.content_hash.startswith("sha256:")


def test_capacity_sweep_finds_last_profitable_size_and_partial_fill() -> None:
    model = CalibratedImpactCostModel(calibrations=(_calibration(),))
    result = analyze_capacity(
        (
            _context("size-100", requested="100", filled="100"),
            _context("size-250", requested="250", filled="250"),
            _context("size-1000", requested="1000", filled="500"),
        ),
        model=model,
        gross_edge_bps=Decimal("60"),
    )

    assert result.points[0].net_edge > 0
    assert result.points[1].net_edge > 0
    assert result.points[2].net_edge < 0
    assert result.points[2].disposition is FillDisposition.PARTIAL
    assert result.points[2].fill_ratio == Decimal("0.5")
    assert result.maximum_profitable_filled_notional == Decimal("25000")
    assert result.content_hash.startswith("sha256:")


def test_impact_model_rejects_future_or_excess_participation_calibration() -> None:
    future_model = CalibratedImpactCostModel(
        calibrations=(_calibration(known_at="2026-01-02T16:00:01+00:00"),)
    )
    with pytest.raises(ExecutionCostError, match="future_calibration"):
        future_model.estimate(_context("future", requested="100", filled="100"))

    restrictive = CalibratedImpactCostModel(
        calibrations=(_calibration(maximum_participation_rate=Decimal("0.1")),)
    )
    with pytest.raises(ExecutionCostError, match="participation_limit"):
        restrictive.estimate(_context("large", requested="500", filled="500"))

    with pytest.raises(ExecutionCostError, match="dimension_mismatch"):
        restrictive.estimate(
            replace(
                _context("currency", requested="10", filled="10"),
                currency="EUR",
            )
        )
