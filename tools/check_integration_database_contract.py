#!/usr/bin/env python3
"""Fail closed when Web migrations and Operations tests target different DBs."""

from __future__ import annotations

import os
import sys
from urllib.parse import parse_qs, unquote, urlsplit


def _required(environment: dict[str, str], key: str) -> str:
    value = environment.get(key, "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def validate_integration_database_contract(environment: dict[str, str]) -> None:
    raw_url = _required(environment, "RESEARCH_OPS_TEST_DATABASE_URL")
    parsed = urlsplit(raw_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("RESEARCH_OPS_TEST_DATABASE_URL must use PostgreSQL")
    if parsed.fragment or not parsed.hostname or parsed.username is None:
        raise ValueError("RESEARCH_OPS_TEST_DATABASE_URL is incomplete")
    try:
        port = parsed.port or 5432
    except ValueError as exc:
        raise ValueError("RESEARCH_OPS_TEST_DATABASE_URL port is invalid") from exc
    database = unquote(parsed.path.removeprefix("/"))
    if not database or "/" in database:
        raise ValueError("RESEARCH_OPS_TEST_DATABASE_URL database is invalid")
    password = parsed.password
    if password is None:
        raise ValueError("RESEARCH_OPS_TEST_DATABASE_URL password is required")
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    if set(query) - {"sslmode"} or any(len(values) != 1 for values in query.values()):
        raise ValueError("RESEARCH_OPS_TEST_DATABASE_URL query is unsupported")
    url_sslmode = query.get("sslmode", ["prefer"])[0]
    expected = {
        "database": _required(environment, "INTERNAL_WEB_DATABASE_NAME"),
        "user": _required(environment, "INTERNAL_WEB_DATABASE_USER"),
        "password": _required(environment, "INTERNAL_WEB_DATABASE_PASSWORD"),
        "host": _required(environment, "INTERNAL_WEB_DATABASE_HOST"),
        "port": _required(environment, "INTERNAL_WEB_DATABASE_PORT"),
        "sslmode": _required(environment, "INTERNAL_WEB_DATABASE_SSLMODE"),
    }
    actual = {
        "database": database,
        "user": unquote(parsed.username),
        "password": unquote(password),
        "host": parsed.hostname,
        "port": str(port),
        "sslmode": url_sslmode,
    }
    mismatches = sorted(key for key in expected if expected[key] != actual[key])
    if mismatches:
        raise ValueError(
            "Web and Operations integration database settings differ: "
            + ",".join(mismatches)
        )


def main() -> int:
    try:
        validate_integration_database_contract(dict(os.environ))
    except ValueError as exc:
        print(f"integration database contract error: {exc}", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
