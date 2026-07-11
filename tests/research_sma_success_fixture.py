from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


PRICES = (10.0, 9.0, 8.0, 9.0, 11.0, 12.0, 10.0, 8.0, 7.0, 8.0, 10.0, 12.0)


def create_success_fixture(root: Path) -> tuple[Path, Path]:
    db_path = root / "candles.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE candles (pair TEXT, interval TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL)")
        for day in ("2026-01-01", "2026-01-02"):
            base = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)
            for index, price in enumerate(PRICES):
                conn.execute("INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ("KRW-BTC", "1m", base + index * 60_000, price, price, price, price, 1.0))
    manifest = {
        "experiment_id": "sma_success_import_boundary",
        "hypothesis": "deterministic SMA research kernel fixture",
        "strategy_name": "sma_with_filter",
        "research_classification": "research_only",
        "market": "KRW-BTC",
        "interval": "1m",
        "dataset": {"source": "sqlite_candles", "snapshot_id": "unit", "train": {"start": "2026-01-01", "end": "2026-01-01"}, "validation": {"start": "2026-01-02", "end": "2026-01-02"}},
        "parameter_space": {"SMA_SHORT": [2], "SMA_LONG": [3], "SMA_FILTER_GAP_MIN_RATIO": [0.0], "SMA_FILTER_VOL_MIN_RANGE_RATIO": [0.0], "SMA_FILTER_OVEREXT_MAX_RETURN_RATIO": [1.0], "SMA_COST_EDGE_ENABLED": [False], "SMA_MARKET_REGIME_ENABLED": [False], "ENTRY_EDGE_BUFFER_RATIO": [0.0], "STRATEGY_MIN_EXPECTED_EDGE_RATIO": [0.0], "LIVE_FEE_RATE_ESTIMATE": [0.0], "STRATEGY_EXIT_RULES": ["stop_loss,opposite_cross,max_holding_time"], "STRATEGY_EXIT_STOP_LOSS_RATIO": [0.01], "STRATEGY_EXIT_MAX_HOLDING_MIN": [0], "STRATEGY_EXIT_MIN_TAKE_PROFIT_RATIO": [0.0], "STRATEGY_EXIT_SMALL_LOSS_TOLERANCE_RATIO": [0.0]},
        "cost_model": {"fee_rate": 0.001, "slippage_bps": [10]},
        "portfolio_policy": {"schema_version": 1, "starting_cash_krw": 1_000_000, "quote_currency": "KRW", "initial_position_qty": 0.0, "cash_interest_policy": "zero", "position_sizing": {"type": "fractional_cash", "buy_fraction": 0.99, "sell_policy": "sell_all_available_position", "cash_buffer_policy": "retain_1_percent_before_fees", "min_order_krw": None, "max_order_krw": None, "rounding_policy": "engine_float_no_exchange_lot_rounding"}, "source": "manifest"},
        "acceptance_gate": {"min_trade_count": 1, "max_mdd_pct": 100, "min_profit_factor": 0.1, "oos_return_must_be_positive": False, "parameter_stability_required": False, "walk_forward_required": False, "final_holdout_required_for_validation": False, "reject_open_position_at_end": False, "metrics_contract_required": False},
        "research_run": {"execution": {"mode": "serial", "max_workers": 1, "process_start_method": "auto_safe", "work_unit": "candidate_scenario"}},
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return db_path, manifest_path
