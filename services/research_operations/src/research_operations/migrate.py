"""Minimal checksummed migration runner with a PostgreSQL advisory lock."""

from __future__ import annotations

import hashlib
from contextlib import suppress
from dataclasses import dataclass
from importlib import resources

from psycopg import sql

from .database import SCHEMA, connection
from .errors import MigrationDriftError

_MIGRATION_LOCK_ID = 8_217_316_011_029_271


@dataclass(frozen=True, slots=True)
class MigrationResult:
    applied: tuple[str, ...]
    already_applied: tuple[str, ...]


def apply_migrations(dsn: str | None = None) -> MigrationResult:
    root = resources.files("research_operations.migrations")
    migration_files = sorted(
        (item for item in root.iterdir() if item.name.endswith(".sql")),
        key=lambda item: item.name,
    )
    applied: list[str] = []
    existing: list[str] = []
    with connection(dsn) as conn:
        conn.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {} ").format(sql.Identifier(SCHEMA))
        )
        conn.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {}.migration_history (
                    name varchar(255) PRIMARY KEY,
                    content_hash varchar(64) NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            ).format(sql.Identifier(SCHEMA))
        )
        conn.commit()
        conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_ID,))
        try:
            for migration_file in migration_files:
                name = migration_file.name
                payload = migration_file.read_bytes()
                content_hash = hashlib.sha256(payload).hexdigest()
                row = conn.execute(
                    "SELECT content_hash FROM research_ops.migration_history "
                    "WHERE name = %s",
                    (name,),
                ).fetchone()
                if row is not None:
                    if row[0] != content_hash:
                        raise MigrationDriftError(f"migration_content_changed:{name}")
                    existing.append(name)
                    continue
                conn.execute(payload.decode("utf-8"))
                conn.execute(
                    """
                    INSERT INTO research_ops.migration_history(name, content_hash)
                    VALUES (%s, %s)
                    """,
                    (name, content_hash),
                )
                conn.commit()
                applied.append(name)
        except BaseException:
            # A failed DDL statement leaves PostgreSQL's transaction aborted.
            # Roll it back before attempting the session-level unlock; never
            # replace the authoritative migration error with a cleanup error.
            # Closing the connection in the outer context remains the final
            # lock-release guarantee if either cleanup operation cannot run.
            with suppress(Exception):
                conn.rollback()
                conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_ID,))
                conn.commit()
            raise
        else:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_ID,))
            conn.commit()
    return MigrationResult(tuple(applied), tuple(existing))


__all__ = ["MigrationResult", "apply_migrations"]
