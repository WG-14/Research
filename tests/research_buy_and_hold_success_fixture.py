from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


PRICES = (100.0, 110.0, 90.0, 120.0, 130.0)


def create_success_fixture(root: Path) -> tuple[Path, Path]:
    db_path = root / "candles.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE candles (pair TEXT, interval TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL)"
        )
        for day in ("2026-01-01", "2026-01-02"):
            base = int(
                datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()
                * 1000
            )
            for index, price in enumerate(PRICES):
                conn.execute(
                    "INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "KRW-BTC",
                        "1m",
                        base + index * 60_000,
                        price,
                        price,
                        price,
                        price,
                        1.0,
                    ),
                )
    manifest = {
        "experiment_id": "buy_and_hold_success_import_boundary",
        "hypothesis": "deterministic research-native buy and hold fixture",
        "strategy_name": "buy_and_hold_baseline",
        "research_classification": "research_only",
        "market": "KRW-BTC",
        "interval": "1m",
        "dataset": {
            "source": "sqlite_candles",
            "snapshot_id": "unit",
            "train": {"start": "2026-01-01", "end": "2026-01-01"},
            "validation": {"start": "2026-01-02", "end": "2026-01-02"},
        },
        "parameter_space": {
            "BUY_HOLD_BUY_INDEX": [1],
            "BUY_HOLD_DECISION_REASON": ["golden_buy_and_hold"],
        },
        "cost_model": {"fee_rate": 0.001, "slippage_bps": [10.0]},
        "portfolio_policy": {
            "schema_version": 1,
            "starting_cash_krw": 1_000_000,
            "quote_currency": "KRW",
            "initial_position_qty": 0.0,
            "cash_interest_policy": "zero",
            "position_sizing": {
                "type": "fractional_cash",
                "buy_fraction": 0.99,
                "sell_policy": "sell_all_available_position",
                "cash_buffer_policy": "retain_1_percent_before_fees",
                "min_order_krw": None,
                "max_order_krw": None,
                "rounding_policy": "engine_float_no_exchange_lot_rounding",
            },
            "source": "manifest",
        },
        "acceptance_gate": {
            "min_trade_count": 1,
            "max_mdd_pct": 100,
            "min_profit_factor": 0.1,
            "oos_return_must_be_positive": False,
            "parameter_stability_required": False,
            "walk_forward_required": False,
            "final_holdout_required_for_validation": False,
            "reject_open_position_at_end": False,
            "metrics_contract_required": False,
        },
        "research_run": {
            "execution": {
                "mode": "serial",
                "max_workers": 1,
                "process_start_method": "auto_safe",
                "work_unit": "candidate_scenario",
            }
        },
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return db_path, manifest_path
