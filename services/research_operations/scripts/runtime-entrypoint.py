#!/usr/bin/env python3
"""Inject file-mounted secrets into one child without printing them."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _secret(environment_name: str) -> str:
    raw = os.environ.get(environment_name, "")
    path = Path(raw)
    if not raw or not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise SystemExit(f"configuration_error:{environment_name}")
    value = path.read_text(encoding="utf-8").rstrip("\r\n")
    if not value or "\x00" in value:
        raise SystemExit(f"configuration_error:{environment_name}")
    return value


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("configuration_error:child_command_required")
    role = os.environ.get("RESEARCH_OPS_DATABASE_ROLE", "runtime")
    user_key = {
        "owner": "POSTGRES_OWNER_USER",
        "runtime": "POSTGRES_RUNTIME_USER",
        "diagnostics": "POSTGRES_DIAGNOSTICS_USER",
        "validator": "POSTGRES_VALIDATOR_USER",
        "backup": "POSTGRES_BACKUP_USER",
    }.get(role)
    secret_key = {
        "owner": "POSTGRES_OWNER_PASSWORD_FILE",
        "runtime": "POSTGRES_RUNTIME_PASSWORD_FILE",
        "diagnostics": "POSTGRES_DIAGNOSTICS_PASSWORD_FILE",
        "validator": "POSTGRES_VALIDATOR_PASSWORD_FILE",
        "backup": "POSTGRES_BACKUP_PASSWORD_FILE",
    }.get(role)
    if user_key is None or secret_key is None:
        raise SystemExit("configuration_error:database_role_invalid")
    user = os.environ.get(user_key, "")
    if not _IDENTIFIER.fullmatch(user):
        raise SystemExit("configuration_error:database_user_invalid")
    password = _secret(secret_key)
    host = os.environ.get("INTERNAL_WEB_DATABASE_HOST", "")
    port = os.environ.get("INTERNAL_WEB_DATABASE_PORT", "")
    database = os.environ.get("INTERNAL_WEB_DATABASE_NAME", "")
    if not host or not port.isdecimal() or not database:
        raise SystemExit("configuration_error:database_endpoint_invalid")
    ca_path = os.environ.get("INTERNAL_WEB_DATABASE_SSLROOTCERT", "")
    if not Path(ca_path).is_absolute():
        raise SystemExit("configuration_error:database_ca_invalid")
    timeout_values = []
    for key, default, minimum, maximum, setting in (
        (
            "RESEARCH_OPS_DATABASE_STATEMENT_TIMEOUT_MS",
            "30000",
            1000,
            300000,
            "statement_timeout",
        ),
        ("RESEARCH_OPS_DATABASE_LOCK_TIMEOUT_MS", "5000", 100, 60000, "lock_timeout"),
        (
            "RESEARCH_OPS_DATABASE_IDLE_TRANSACTION_TIMEOUT_MS",
            "30000",
            1000,
            300000,
            "idle_in_transaction_session_timeout",
        ),
    ):
        raw = os.environ.get(key, default)
        if (
            not raw.isascii()
            or not raw.isdecimal()
            or not minimum <= int(raw) <= maximum
        ):
            raise SystemExit(f"configuration_error:{key}")
        timeout_values.extend(("-c", f"{setting}={raw}"))
    options = " ".join(timeout_values + ["-c", "timezone=UTC"])
    os.environ["RESEARCH_OPS_DATABASE_URL"] = (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@"
        f"{host}:{port}/{quote(database, safe='')}?"
        f"sslmode=verify-full&sslrootcert={quote(ca_path, safe='/')}"
        f"&options={quote(options, safe='')}"
    )
    os.environ["PGHOST"] = host
    os.environ["PGPORT"] = port
    os.environ["PGDATABASE"] = database
    os.environ["PGUSER"] = user
    os.environ["PGPASSWORD"] = password
    os.environ["PGSSLMODE"] = "verify-full"
    os.environ["PGSSLROOTCERT"] = ca_path
    if role in {"owner", "runtime", "validator", "backup"}:
        os.environ["INTERNAL_WEB_DATABASE_USER"] = user
        os.environ["INTERNAL_WEB_DATABASE_PASSWORD"] = password
    django_secret_file = os.environ.get("DJANGO_SECRET_KEY_FILE")
    if django_secret_file:
        os.environ["INTERNAL_WEB_SECRET_KEY"] = _secret("DJANGO_SECRET_KEY_FILE")
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
