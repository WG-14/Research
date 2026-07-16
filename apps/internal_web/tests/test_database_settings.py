from __future__ import annotations

from pathlib import Path

import pytest

from market_research_web.database import (
    POSTGRESQL_SSLMODES,
    build_database_settings,
)


def _postgresql_environment(**overrides: str) -> dict[str, str]:
    environment = {
        "INTERNAL_WEB_DATABASE_ENGINE": "postgresql",
        "INTERNAL_WEB_DATABASE_NAME": "research_web",
        "INTERNAL_WEB_DATABASE_USER": "research_app",
        "INTERNAL_WEB_DATABASE_PASSWORD": "secret with spaces ",
        "INTERNAL_WEB_DATABASE_HOST": "database.internal",
        "INTERNAL_WEB_DATABASE_PORT": "5432",
    }
    environment.update(overrides)
    return environment


def _default_postgresql_options(*, sslmode: str = "require") -> dict[str, object]:
    return {
        "sslmode": sslmode,
        "connect_timeout": 5,
        "application_name": "market-research-web",
        "options": (
            "-c statement_timeout=30000 -c lock_timeout=5000 "
            "-c idle_in_transaction_session_timeout=30000 "
            "-c timezone=UTC -c client_encoding=UTF8"
        ),
    }


def test_sqlite_is_the_existing_atomic_development_default(tmp_path: Path) -> None:
    database_path = (tmp_path / "operations.sqlite3").resolve()

    settings = build_database_settings(sqlite_path=database_path, environ={})

    assert settings == {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": database_path,
            "OPTIONS": {"timeout": 30},
            "ATOMIC_REQUESTS": True,
            "TEST": {"NAME": None},
        }
    }


def test_postgresql_requires_explicit_fields_and_defaults_to_required_tls(
    tmp_path: Path,
) -> None:
    settings = build_database_settings(
        sqlite_path=tmp_path / "unused.sqlite3",
        environ=_postgresql_environment(),
    )

    assert settings == {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "research_web",
            "USER": "research_app",
            "PASSWORD": "secret with spaces ",
            "HOST": "database.internal",
            "PORT": "5432",
            "OPTIONS": _default_postgresql_options(),
            "ATOMIC_REQUESTS": True,
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
        }
    }


@pytest.mark.parametrize(
    "missing_key",
    (
        "INTERNAL_WEB_DATABASE_NAME",
        "INTERNAL_WEB_DATABASE_USER",
        "INTERNAL_WEB_DATABASE_PASSWORD",
        "INTERNAL_WEB_DATABASE_HOST",
        "INTERNAL_WEB_DATABASE_PORT",
    ),
)
def test_postgresql_rejects_each_missing_required_field(
    tmp_path: Path,
    missing_key: str,
) -> None:
    environment = _postgresql_environment()
    environment.pop(missing_key)

    with pytest.raises(RuntimeError, match=missing_key):
        build_database_settings(
            sqlite_path=tmp_path / "unused.sqlite3",
            environ=environment,
        )


def test_unknown_database_engine_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="INTERNAL_WEB_DATABASE_ENGINE"):
        build_database_settings(
            sqlite_path=tmp_path / "unused.sqlite3",
            environ={"INTERNAL_WEB_DATABASE_ENGINE": "mysql"},
        )


@pytest.mark.parametrize("sslmode", sorted(POSTGRESQL_SSLMODES))
def test_postgresql_accepts_only_allowlisted_sslmode(
    tmp_path: Path,
    sslmode: str,
) -> None:
    settings = build_database_settings(
        sqlite_path=tmp_path / "unused.sqlite3",
        environ=_postgresql_environment(
            INTERNAL_WEB_DATABASE_SSLMODE=sslmode,
        ),
    )
    assert settings["default"]["OPTIONS"] == _default_postgresql_options(
        sslmode=sslmode
    )


@pytest.mark.parametrize("sslmode", ("", "true", "verify_ca", "REQUIRED"))
def test_postgresql_rejects_unknown_sslmode(
    tmp_path: Path,
    sslmode: str,
) -> None:
    with pytest.raises(RuntimeError, match="INTERNAL_WEB_DATABASE_SSLMODE"):
        build_database_settings(
            sqlite_path=tmp_path / "unused.sqlite3",
            environ=_postgresql_environment(
                INTERNAL_WEB_DATABASE_SSLMODE=sslmode,
            ),
        )


@pytest.mark.parametrize("port", ("", "0", "65536", "+5432", " 5432", "５４３２"))
def test_postgresql_rejects_invalid_port(tmp_path: Path, port: str) -> None:
    with pytest.raises(RuntimeError, match="INTERNAL_WEB_DATABASE_PORT"):
        build_database_settings(
            sqlite_path=tmp_path / "unused.sqlite3",
            environ=_postgresql_environment(INTERNAL_WEB_DATABASE_PORT=port),
        )


def test_postgresql_applies_bounded_session_and_connection_controls(
    tmp_path: Path,
) -> None:
    settings = build_database_settings(
        sqlite_path=tmp_path / "unused.sqlite3",
        environ=_postgresql_environment(
            INTERNAL_WEB_DATABASE_CONNECT_TIMEOUT_SECONDS="9",
            INTERNAL_WEB_DATABASE_STATEMENT_TIMEOUT_MS="45000",
            INTERNAL_WEB_DATABASE_LOCK_TIMEOUT_MS="2500",
            INTERNAL_WEB_DATABASE_IDLE_TRANSACTION_TIMEOUT_MS="12000",
            INTERNAL_WEB_DATABASE_CONN_MAX_AGE_SECONDS="120",
            INTERNAL_WEB_DATABASE_APPLICATION_NAME="research_web.integration",
            INTERNAL_WEB_DATABASE_SSLROOTCERT="/etc/research-web/ca.pem",
        ),
    )["default"]

    assert settings["CONN_MAX_AGE"] == 120
    assert settings["CONN_HEALTH_CHECKS"] is True
    assert settings["OPTIONS"] == {
        "sslmode": "require",
        "connect_timeout": 9,
        "application_name": "research_web.integration",
        "options": (
            "-c statement_timeout=45000 -c lock_timeout=2500 "
            "-c idle_in_transaction_session_timeout=12000 "
            "-c timezone=UTC -c client_encoding=UTF8"
        ),
        "sslrootcert": "/etc/research-web/ca.pem",
    }


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("INTERNAL_WEB_DATABASE_CONNECT_TIMEOUT_SECONDS", "0"),
        ("INTERNAL_WEB_DATABASE_STATEMENT_TIMEOUT_MS", "999"),
        ("INTERNAL_WEB_DATABASE_LOCK_TIMEOUT_MS", "60001"),
        ("INTERNAL_WEB_DATABASE_IDLE_TRANSACTION_TIMEOUT_MS", "oops"),
        ("INTERNAL_WEB_DATABASE_CONN_MAX_AGE_SECONDS", "3601"),
        ("INTERNAL_WEB_DATABASE_APPLICATION_NAME", "unsafe name"),
        ("INTERNAL_WEB_DATABASE_SSLROOTCERT", "relative/ca.pem"),
    ),
)
def test_postgresql_rejects_unsafe_operational_connection_controls(
    tmp_path: Path,
    key: str,
    value: str,
) -> None:
    with pytest.raises(RuntimeError, match=key):
        build_database_settings(
            sqlite_path=tmp_path / "unused.sqlite3",
            environ=_postgresql_environment(**{key: value}),
        )
