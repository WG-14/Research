from __future__ import annotations

import sqlite3
import warnings

from . import db_core


_DEPRECATION_MESSAGE = (
    "bithumb_bot.db_schema is deprecated and no longer owns DB DDL. "
    "Use bithumb_bot.db_core.ensure_db() for canonical schema creation, migration, and validation."
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    warnings.warn(_DEPRECATION_MESSAGE, DeprecationWarning, stacklevel=2)
    db_core.ensure_schema(conn)


def init_portfolio(conn: sqlite3.Connection, start_cash: float | None = None) -> None:
    warnings.warn(_DEPRECATION_MESSAGE, DeprecationWarning, stacklevel=2)
    db_core.ensure_schema(conn)
    if start_cash is None:
        db_core.init_portfolio(conn)
        return

    row = conn.execute("SELECT id FROM portfolio WHERE id=1").fetchone()
    if row is not None:
        return
    cash = db_core.normalize_cash_amount(float(start_cash))
    had_tx = conn.in_transaction
    conn.execute(
        """
        INSERT INTO portfolio(
            id, cash_krw, asset_qty, cash_available, cash_locked, asset_available, asset_locked
        ) VALUES (1, ?, 0.0, ?, 0.0, 0.0, 0.0)
        """,
        (cash, cash),
    )
    if not had_tx:
        conn.commit()
