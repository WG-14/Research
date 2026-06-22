from __future__ import annotations

import sqlite3

from bithumb_bot.exchange_submit_diagnostics import (
    BROKER_LOOKUP_UNAVAILABLE,
    EXCHANGE_REJECTED,
    SUBMITTED_NO_FILL,
    SUBMIT_NOT_REACHED,
    classify_exchange_submit_reachability,
    diagnose_exchange_submit_reachability,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE orders (
            client_order_id TEXT PRIMARY KEY,
            side TEXT,
            status TEXT,
            exchange_order_id TEXT,
            created_ts INTEGER,
            last_error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE order_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_order_id TEXT,
            event_type TEXT,
            event_ts INTEGER,
            exchange_order_id_obtained INTEGER,
            submission_reason_code TEXT
        )
        """
    )
    return conn


def test_submit_not_reached_when_local_submit_is_absent() -> None:
    result = classify_exchange_submit_reachability(
        local_order={
            "client_order_id": "cid-1",
            "status": "INTENT_CREATED",
            "exchange_order_id": "",
        },
        order_events=[{"event_type": "intent_created", "client_order_id": "cid-1"}],
        broker_recent_orders=[],
    )

    assert result["reason_code"] == SUBMIT_NOT_REACHED
    assert result["exchange_submit_reached"] is False


def test_exchange_rejected_classified_from_broker_recent_order() -> None:
    result = classify_exchange_submit_reachability(
        local_order={
            "client_order_id": "cid-2",
            "status": "SUBMITTED",
            "exchange_order_id": "ex-2",
        },
        order_events=[{"event_type": "submit_started", "client_order_id": "cid-2"}],
        broker_recent_orders=[
            {
                "client_order_id": "cid-2",
                "exchange_order_id": "ex-2",
                "status": "REJECTED",
                "qty_filled": 0.0,
            }
        ],
    )

    assert result["reason_code"] == EXCHANGE_REJECTED
    assert result["exchange_submit_reached"] is True
    assert result["matched_by"] == "broker_recent_orders"


def test_submitted_no_fill_classified_from_recent_order() -> None:
    result = classify_exchange_submit_reachability(
        local_order={
            "client_order_id": "cid-3",
            "status": "SUBMITTED",
            "exchange_order_id": "ex-3",
        },
        order_events=[{"event_type": "submit_started", "client_order_id": "cid-3"}],
        broker_recent_orders=[
            {
                "client_order_id": "cid-3",
                "exchange_order_id": "ex-3",
                "status": "NEW",
                "qty_filled": 0.0,
            }
        ],
    )

    assert result["reason_code"] == SUBMITTED_NO_FILL
    assert result["exchange_submit_reached"] is True


def test_diagnosis_reports_pre_submit_blocked_when_no_order_row() -> None:
    result = classify_exchange_submit_reachability(
        local_order=None,
        order_events=[],
        broker_recent_orders=[],
    )

    assert result["reason_code"] == SUBMIT_NOT_REACHED
    assert result["exchange_submit_reached"] is False


def test_diagnosis_reports_exchange_reject_when_broker_order_rejected() -> None:
    result = classify_exchange_submit_reachability(
        local_order={
            "client_order_id": "cid-4",
            "status": "SUBMITTED",
            "exchange_order_id": "ex-4",
        },
        order_events=[{"event_type": "submit_attempt_recorded", "client_order_id": "cid-4"}],
        broker_recent_orders=[
            {
                "client_order_id": "cid-4",
                "exchange_order_id": "ex-4",
                "status": "FAILED",
                "qty_filled": 0,
            }
        ],
    )

    assert result["reason_code"] == EXCHANGE_REJECTED
    assert result["matched_by"] == "broker_recent_orders"


def test_diagnosis_matches_local_order_to_broker_recent_order() -> None:
    result = classify_exchange_submit_reachability(
        local_order={
            "client_order_id": "cid-5",
            "status": "SUBMITTED",
            "exchange_order_id": "ex-5",
        },
        order_events=[{"event_type": "submit_attempt_recorded", "client_order_id": "cid-5"}],
        broker_recent_orders=[
            {
                "client_order_id": "cid-5",
                "exchange_order_id": "ex-5",
                "status": "NEW",
                "filled_qty": 0,
            }
        ],
    )

    assert result["reason_code"] == SUBMITTED_NO_FILL
    assert result["exchange_order_id"] == "ex-5"
    assert result["matched_by"] == "broker_recent_orders"


def test_diagnosis_reports_lookup_unavailable_when_broker_recent_orders_fail() -> None:
    result = classify_exchange_submit_reachability(
        local_order={
            "client_order_id": "cid-6",
            "status": "SUBMITTED",
            "exchange_order_id": "ex-6",
        },
        order_events=[{"event_type": "submit_attempt_recorded", "client_order_id": "cid-6"}],
        broker_recent_orders=[],
        broker_lookup_available=False,
        broker_lookup_error="credentials_missing",
    )

    assert result["reason_code"] == BROKER_LOOKUP_UNAVAILABLE
    assert result["exchange_submit_reached"] is False


def test_read_only_diagnosis_compares_local_rows_and_broker_recent_orders() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO orders(client_order_id, side, status, exchange_order_id, created_ts) VALUES (?, ?, ?, ?, ?)",
        ("cid-db", "BUY", "SUBMITTED", "ex-db", 1),
    )
    conn.execute(
        "INSERT INTO order_events(client_order_id, event_type, event_ts) VALUES (?, ?, ?)",
        ("cid-db", "submit_started", 2),
    )

    result = diagnose_exchange_submit_reachability(
        conn,
        client_order_id="cid-db",
        broker_recent_orders=[
            {
                "client_order_id": "cid-db",
                "exchange_order_id": "ex-db",
                "status": "NEW",
                "qty_filled": 0.0,
            }
        ],
    )

    assert result["reason_code"] == SUBMITTED_NO_FILL
    assert result["matched_by"] == "broker_recent_orders"
