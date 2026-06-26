from __future__ import annotations

import sqlite3

from bithumb_bot.db_core import ensure_db
from bithumb_bot.h74_cycle_state import ensure_h74_cycle_schema


def test_ensure_db_creates_h74_cycle_state_table(tmp_path) -> None:
    conn = ensure_db(str(tmp_path / "h74-schema.sqlite"))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='h74_cycle_state'"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None


def test_h74_cycle_schema_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    ensure_h74_cycle_schema(conn)
    ensure_h74_cycle_schema(conn)

    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='h74_cycle_state'"
    ).fetchone()
    assert row is not None
