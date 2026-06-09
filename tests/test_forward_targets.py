from __future__ import annotations

import pytest

from bithumb_bot.research.dataset_snapshot import Candle
from bithumb_bot.research.forward_targets import (
    PATH_START_NEXT_CANDLE_AFTER_SIGNAL_CLOSE,
    build_horizon_durations,
    build_forward_target_window,
    compute_forward_target,
)


def _candles() -> tuple[Candle, ...]:
    return (
        Candle(ts=0, open=10.0, high=11.0, low=9.0, close=10.0, volume=1.0),
        Candle(ts=1, open=12.0, high=13.0, low=11.0, close=12.5, volume=1.0),
        Candle(ts=2, open=13.0, high=16.0, low=10.0, close=15.0, volume=1.0),
        Candle(ts=3, open=15.0, high=17.0, low=8.0, close=14.0, volume=1.0),
        Candle(ts=4, open=14.0, high=99.0, low=1.0, close=13.0, volume=1.0),
    )


def _signal_close_contamination_candles() -> tuple[Candle, ...]:
    return (
        Candle(ts=0, open=100.0, high=999.0, low=1.0, close=100.0, volume=1.0),
        Candle(ts=1, open=101.0, high=110.0, low=95.0, close=105.0, volume=1.0),
        Candle(ts=2, open=105.0, high=108.0, low=100.0, close=104.0, volume=1.0),
    )


def test_forward_target_next_open_uses_next_candle_open_as_entry() -> None:
    target = compute_forward_target(candles=_candles(), index=0, horizon_steps=2)

    assert target is not None
    assert target.entry_ts == 1
    assert target.entry_price_index == 1
    assert target.entry_price == 12.0


def test_forward_target_signal_close_uses_signal_candle_close_as_entry() -> None:
    target = compute_forward_target(
        candles=_candles(),
        index=0,
        horizon_steps=2,
        entry_price_mode="signal_close",
    )

    assert target is not None
    assert target.entry_ts == 0
    assert target.entry_price_index == 0
    assert target.entry_price == 10.0


def test_forward_target_computes_mfe_from_highs_within_horizon() -> None:
    target = compute_forward_target(candles=_candles(), index=0, horizon_steps=2)

    assert target is not None
    assert target.mfe == pytest.approx((16.0 / 12.0) - 1.0)


def test_forward_target_computes_mae_from_lows_within_horizon() -> None:
    target = compute_forward_target(candles=_candles(), index=0, horizon_steps=2)

    assert target is not None
    assert target.mae == pytest.approx((10.0 / 12.0) - 1.0)


def test_forward_target_window_separates_entry_price_index_from_path_start_index() -> None:
    window = build_forward_target_window(
        candles=_signal_close_contamination_candles(),
        index=0,
        horizon_steps=1,
        entry_price_mode="signal_close",
    )

    assert window is not None
    assert window.signal_index == 0
    assert window.entry_price_index == 0
    assert window.path_start_index == 1
    assert window.exit_index == 1


def test_signal_close_path_start_policy_is_not_entry_index() -> None:
    target = compute_forward_target(
        candles=_signal_close_contamination_candles(),
        index=0,
        horizon_steps=1,
        entry_price_mode="signal_close",
    )

    assert target is not None
    assert target.entry_price_index == 0
    assert target.path_start_index == 1
    assert target.path_start_policy == PATH_START_NEXT_CANDLE_AFTER_SIGNAL_CLOSE


def test_next_open_entry_price_index_and_path_start_index_match() -> None:
    target = compute_forward_target(candles=_signal_close_contamination_candles(), index=0, horizon_steps=1)

    assert target is not None
    assert target.entry_price_index == 1
    assert target.path_start_index == 1


def test_signal_close_mfe_does_not_use_signal_candle_high() -> None:
    target = compute_forward_target(
        candles=_signal_close_contamination_candles(),
        index=0,
        horizon_steps=1,
        entry_price_mode="signal_close",
    )

    assert target is not None
    assert target.mfe == pytest.approx((110.0 / 100.0) - 1.0)
    assert target.mfe != pytest.approx((999.0 / 100.0) - 1.0)


def test_signal_close_mae_does_not_use_signal_candle_low() -> None:
    target = compute_forward_target(
        candles=_signal_close_contamination_candles(),
        index=0,
        horizon_steps=1,
        entry_price_mode="signal_close",
    )

    assert target is not None
    assert target.mae == pytest.approx((95.0 / 100.0) - 1.0)
    assert target.mae != pytest.approx((1.0 / 100.0) - 1.0)


def test_signal_close_mfe_excludes_signal_candle_high() -> None:
    baseline = list(_signal_close_contamination_candles())
    changed_signal_high = list(baseline)
    changed_signal_high[0] = Candle(ts=0, open=100.0, high=5000.0, low=1.0, close=100.0, volume=1.0)

    first = compute_forward_target(candles=tuple(baseline), index=0, horizon_steps=1, entry_price_mode="signal_close")
    second = compute_forward_target(candles=tuple(changed_signal_high), index=0, horizon_steps=1, entry_price_mode="signal_close")

    assert first is not None
    assert second is not None
    assert first.mfe == second.mfe


def test_signal_close_mae_excludes_signal_candle_low() -> None:
    baseline = list(_signal_close_contamination_candles())
    changed_signal_low = list(baseline)
    changed_signal_low[0] = Candle(ts=0, open=100.0, high=999.0, low=0.01, close=100.0, volume=1.0)

    first = compute_forward_target(candles=tuple(baseline), index=0, horizon_steps=1, entry_price_mode="signal_close")
    second = compute_forward_target(candles=tuple(changed_signal_low), index=0, horizon_steps=1, entry_price_mode="signal_close")

    assert first is not None
    assert second is not None
    assert first.mae == second.mae


def test_signal_close_records_path_policy() -> None:
    target = compute_forward_target(
        candles=_signal_close_contamination_candles(),
        index=0,
        horizon_steps=1,
        entry_price_mode="signal_close",
    )

    assert target is not None
    assert target.path_start_policy == "next_candle_after_signal_close"
    assert target.intrabar_included is False
    assert target.mfe_mae_basis == "ohlc_future_candles_only"


def test_signal_close_target_records_intrabar_policy() -> None:
    target = compute_forward_target(
        candles=_signal_close_contamination_candles(),
        index=0,
        horizon_steps=1,
        entry_price_mode="signal_close",
    )

    assert target is not None
    assert target.intrabar_included is False


def test_next_open_horizon_1_uses_next_candle_close_as_exit() -> None:
    target = compute_forward_target(candles=_signal_close_contamination_candles(), index=0, horizon_steps=1)

    assert target is not None
    assert target.exit_index == 1
    assert target.exit_ts == 1
    assert target.exit_price == 105.0
    assert target.gross_forward_return == pytest.approx((105.0 / 101.0) - 1.0)


def test_signal_close_horizon_1_exit_semantics_are_documented() -> None:
    target = compute_forward_target(
        candles=_signal_close_contamination_candles(),
        index=0,
        horizon_steps=1,
        entry_price_mode="signal_close",
    )

    assert target is not None
    assert target.exit_index == 1
    assert target.exit_ts == 1
    assert target.exit_price == 105.0
    assert target.gross_forward_return == pytest.approx((105.0 / 100.0) - 1.0)


def test_forward_target_skips_when_horizon_exceeds_available_candles() -> None:
    assert compute_forward_target(candles=_candles(), index=3, horizon_steps=2) is None


def test_forward_target_rejects_unknown_entry_price_mode() -> None:
    with pytest.raises(ValueError, match="unknown entry_price_mode"):
        compute_forward_target(candles=_candles(), index=0, horizon_steps=1, entry_price_mode="unknown")


def test_horizon_duration_uses_interval_and_preserves_candle_label() -> None:
    durations = build_horizon_durations(interval="5m", horizon_steps=(5,))

    assert durations[0].horizon_steps == 5
    assert durations[0].horizon_label == "5c"
    assert durations[0].horizon_duration_ms == 1_500_000
    assert durations[0].horizon_duration_label == "25m"
