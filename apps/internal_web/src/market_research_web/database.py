"""Pure database settings boundary for the internal web adapter.

This module describes connection settings only.  Selecting PostgreSQL does not
install a driver, provision a server, or make it a supported production
database; those remain integration gates outside this repository.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any


DATABASE_ENGINE_ENV = "INTERNAL_WEB_DATABASE_ENGINE"
POSTGRESQL_SSLMODES = frozenset(
    {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
)


def build_database_settings(
    *,
    sqlite_path: str | Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build one fail-closed Django database configuration.

    SQLite is the compatibility default for local development.  PostgreSQL is
    an explicit integration profile whose connection fields must all be
    supplied by the caller's environment.
    """

    environment = os.environ if environ is None else environ
    raw_engine = environment.get(DATABASE_ENGINE_ENV, "sqlite")
    engine = str(raw_engine).strip().lower()
    if engine == "sqlite":
        return _sqlite_settings(sqlite_path)
    if engine == "postgresql":
        return _postgresql_settings(environment)
    raise RuntimeError(
        f"{DATABASE_ENGINE_ENV} must be one of: postgresql, sqlite"
    )


def _sqlite_settings(sqlite_path: str | Path) -> dict[str, dict[str, Any]]:
    path = Path(sqlite_path).expanduser()
    if not path.is_absolute():
        raise RuntimeError("internal web SQLite database path must be absolute")
    return {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": path,
            "OPTIONS": {"timeout": 30},
            "ATOMIC_REQUESTS": True,
            # Isolate local and CI test processes from the configured state DB.
            "TEST": {"NAME": None},
        }
    }


def _postgresql_settings(
    environment: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    name = _required_text(environment, "INTERNAL_WEB_DATABASE_NAME")
    user = _required_text(environment, "INTERNAL_WEB_DATABASE_USER")
    password = _required_secret(environment, "INTERNAL_WEB_DATABASE_PASSWORD")
    host = _required_text(environment, "INTERNAL_WEB_DATABASE_HOST")
    port = _required_port(environment, "INTERNAL_WEB_DATABASE_PORT")
    sslmode = str(environment.get("INTERNAL_WEB_DATABASE_SSLMODE", "require"))
    if sslmode not in POSTGRESQL_SSLMODES:
        allowed = ", ".join(sorted(POSTGRESQL_SSLMODES))
        raise RuntimeError(
            f"INTERNAL_WEB_DATABASE_SSLMODE must be one of: {allowed}"
        )
    connect_timeout = _bounded_integer(
        environment,
        "INTERNAL_WEB_DATABASE_CONNECT_TIMEOUT_SECONDS",
        default=5,
        minimum=1,
        maximum=60,
    )
    statement_timeout = _bounded_integer(
        environment,
        "INTERNAL_WEB_DATABASE_STATEMENT_TIMEOUT_MS",
        default=30_000,
        minimum=1_000,
        maximum=300_000,
    )
    lock_timeout = _bounded_integer(
        environment,
        "INTERNAL_WEB_DATABASE_LOCK_TIMEOUT_MS",
        default=5_000,
        minimum=100,
        maximum=60_000,
    )
    idle_transaction_timeout = _bounded_integer(
        environment,
        "INTERNAL_WEB_DATABASE_IDLE_TRANSACTION_TIMEOUT_MS",
        default=30_000,
        minimum=1_000,
        maximum=300_000,
    )
    connection_max_age = _bounded_integer(
        environment,
        "INTERNAL_WEB_DATABASE_CONN_MAX_AGE_SECONDS",
        default=60,
        minimum=0,
        maximum=3_600,
    )
    application_name = str(
        environment.get(
            "INTERNAL_WEB_DATABASE_APPLICATION_NAME",
            "market-research-web",
        )
    ).strip()
    if (
        not application_name
        or len(application_name) > 63
        or not all(character.isascii() and (character.isalnum() or character in "-_.") for character in application_name)
    ):
        raise RuntimeError(
            "INTERNAL_WEB_DATABASE_APPLICATION_NAME must contain only ASCII "
            "letters, digits, '.', '-' or '_' and be at most 63 characters"
        )
    options: dict[str, Any] = {
        "sslmode": sslmode,
        "connect_timeout": connect_timeout,
        "application_name": application_name,
        # Bound lock waits and abandoned transactions at the server session,
        # rather than relying on a request or worker process to remain alive.
        "options": " ".join(
            (
                f"-c statement_timeout={statement_timeout}",
                f"-c lock_timeout={lock_timeout}",
                f"-c idle_in_transaction_session_timeout={idle_transaction_timeout}",
                "-c timezone=UTC",
                "-c client_encoding=UTF8",
            )
        ),
    }
    ssl_root_certificate = environment.get("INTERNAL_WEB_DATABASE_SSLROOTCERT")
    if ssl_root_certificate is not None:
        certificate_path = Path(ssl_root_certificate).expanduser()
        if not certificate_path.is_absolute():
            raise RuntimeError(
                "INTERNAL_WEB_DATABASE_SSLROOTCERT must be an absolute path"
            )
        options["sslrootcert"] = str(certificate_path)
    return {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": name,
            "USER": user,
            "PASSWORD": password,
            "HOST": host,
            "PORT": port,
            "OPTIONS": options,
            "ATOMIC_REQUESTS": True,
            "CONN_MAX_AGE": connection_max_age,
            "CONN_HEALTH_CHECKS": True,
        }
    }


def _required_text(environment: Mapping[str, str], key: str) -> str:
    value = environment.get(key)
    if value is None or not value.strip():
        raise RuntimeError(f"{key} is required for PostgreSQL")
    return value.strip()


def _required_secret(environment: Mapping[str, str], key: str) -> str:
    value = environment.get(key)
    if value is None or not value.strip():
        raise RuntimeError(f"{key} is required for PostgreSQL")
    return value


def _required_port(environment: Mapping[str, str], key: str) -> str:
    value = environment.get(key)
    if value is None or not value or not value.isascii() or not value.isdecimal():
        raise RuntimeError(f"{key} must be an ASCII TCP port for PostgreSQL")
    port = int(value)
    if not 1 <= port <= 65535:
        raise RuntimeError(f"{key} must be between 1 and 65535")
    return str(port)


def _bounded_integer(
    environment: Mapping[str, str],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = environment.get(key)
    if raw is None:
        value = default
    elif not raw or not raw.isascii() or not raw.isdecimal():
        raise RuntimeError(f"{key} must be an ASCII integer")
    else:
        value = int(raw)
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{key} must be between {minimum} and {maximum}")
    return value


__all__ = [
    "DATABASE_ENGINE_ENV",
    "POSTGRESQL_SSLMODES",
    "build_database_settings",
]
