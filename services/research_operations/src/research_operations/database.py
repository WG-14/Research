"""PostgreSQL connection boundary for operational state."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg import Connection

from .errors import ConfigurationError, MaintenanceFenceActive

DATABASE_URL_ENV = "RESEARCH_OPS_DATABASE_URL"
SCHEMA = "research_ops"
RUNTIME_CONTROL_ADVISORY_LOCK_ID = 7_641_922_806_173_411


def database_url(environ: Mapping[str, str] | None = None) -> str:
    environment = os.environ if environ is None else environ
    value = environment.get(DATABASE_URL_ENV, "")
    if not value or not value.strip():
        raise ConfigurationError(f"{DATABASE_URL_ENV} is required")
    dsn = value.strip()
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ConfigurationError(f"{DATABASE_URL_ENV} must use PostgreSQL")
    if not parsed.hostname or not parsed.path.strip("/"):
        raise ConfigurationError(f"{DATABASE_URL_ENV} requires host and database")
    return dsn


@contextmanager
def connection(
    dsn: str | None = None,
    *,
    autocommit: bool = False,
    connect_timeout: int = 5,
    session_read_only: bool | None = None,
) -> Iterator[Connection[Any]]:
    resolved = dsn or database_url()
    connect_options: dict[str, Any] = {
        "autocommit": autocommit,
        "connect_timeout": connect_timeout,
        "application_name": "research-operations",
    }
    if session_read_only is not None:
        connect_options["options"] = "-c default_transaction_read_only=" + (
            "on" if session_read_only else "off"
        )
    conn = psycopg.connect(resolved, **connect_options)
    try:
        statement_timeout = _bounded_timeout(
            "RESEARCH_OPS_DATABASE_STATEMENT_TIMEOUT_MS", 30_000, 1_000, 300_000
        )
        lock_timeout = _bounded_timeout(
            "RESEARCH_OPS_DATABASE_LOCK_TIMEOUT_MS", 5_000, 100, 60_000
        )
        idle_timeout = _bounded_timeout(
            "RESEARCH_OPS_DATABASE_IDLE_TRANSACTION_TIMEOUT_MS",
            30_000,
            1_000,
            300_000,
        )
        with conn.cursor() as cursor:
            cursor.execute("SET TIME ZONE 'UTC'")
            cursor.execute(
                """
                SELECT set_config('statement_timeout', %s, false),
                       set_config('lock_timeout', %s, false),
                       set_config('idle_in_transaction_session_timeout', %s, false)
                """,
                (
                    f"{statement_timeout}ms",
                    f"{lock_timeout}ms",
                    f"{idle_timeout}ms",
                ),
            )
        yield conn
        if not autocommit:
            conn.commit()
    except BaseException:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def _bounded_timeout(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    if not raw.isascii() or not raw.isdecimal():
        raise ConfigurationError(f"{name} must be an integer")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} is outside the supported range")
    return value


def verify_postgresql(dsn: str | None = None) -> dict[str, Any]:
    with connection(dsn) as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT current_database(), current_user, "
            "current_setting('server_version_num')"
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("postgresql_identity_query_returned_no_row")
        database, user, version = row
    return {"database": database, "user": user, "server_version_num": int(version)}


def assert_claim_admission_open(conn: Connection[Any]) -> None:
    """Fail closed while backup fencing or quarantine blocks new claims."""

    conn.execute(
        "SELECT pg_advisory_xact_lock_shared(%s)",
        (RUNTIME_CONTROL_ADVISORY_LOCK_ID,),
    )
    row = conn.execute(
        """
        SELECT claim_admission_open, integrity_quarantine
        FROM research_ops.runtime_control
        WHERE singleton_id = 1
        """
    ).fetchone()
    if row is None or row[0] is not True or row[1] is True:
        raise MaintenanceFenceActive("operational_claim_admission_closed")


def assert_mutation_admission_open(conn: Connection[Any]) -> None:
    """Reject new experiment executions during draining and sealed fences."""

    conn.execute(
        "SELECT pg_advisory_xact_lock_shared(%s)",
        (RUNTIME_CONTROL_ADVISORY_LOCK_ID,),
    )
    row = conn.execute(
        """
        SELECT mutation_admission_open, integrity_quarantine
        FROM research_ops.runtime_control
        WHERE singleton_id = 1
        """
    ).fetchone()
    if row is None or row[0] is not True or row[1] is True:
        raise MaintenanceFenceActive("operational_mutation_admission_closed")


__all__ = [
    "DATABASE_URL_ENV",
    "RUNTIME_CONTROL_ADVISORY_LOCK_ID",
    "SCHEMA",
    "assert_claim_admission_open",
    "assert_mutation_admission_open",
    "connection",
    "database_url",
    "verify_postgresql",
]
