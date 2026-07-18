#!/usr/bin/env python3
"""Generate or verify the internal-web OpenAPI and persisted-schema contracts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATED_CONTRACTS = {
    Path("docs/generated/internal-web-openapi.json"): "openapi",
    Path("docs/generated/internal-web-persisted-schema.json"): "persisted-schema",
}


def _configure_django() -> None:
    contract_root = Path("/tmp/market-research-web-contract-docs")
    defaults = {
        "DJANGO_SETTINGS_MODULE": "market_research_web.settings_test",
        "INTERNAL_WEB_SECRET_KEY": "contract-check-only-not-for-runtime-0123456789",
        "RESEARCH_DATA_ROOT": str(contract_root / "datasets"),
        "RESEARCH_ARTIFACT_ROOT": str(contract_root / "artifacts"),
        "RESEARCH_REPORT_ROOT": str(contract_root / "reports"),
        "RESEARCH_CACHE_ROOT": str(contract_root / "cache"),
        "RESEARCH_DB_PATH": str(contract_root / "research.sqlite3"),
        "RESEARCH_OPS_SOURCE_ROOT": str(PROJECT_ROOT),
    }
    for name, value in defaults.items():
        os.environ.setdefault(name, value)
    import django

    django.setup()


def _documents() -> dict[Path, dict[str, object]]:
    _configure_django()
    from portal.api_contract import (
        build_openapi_document,
        build_persisted_schema_document,
    )

    return {
        PROJECT_ROOT / "docs/generated/internal-web-openapi.json": (
            build_openapi_document()
        ),
        PROJECT_ROOT / "docs/generated/internal-web-persisted-schema.json": (
            build_persisted_schema_document()
        ),
    }


def _canonical_bytes(value: dict[str, object]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def write_contracts() -> tuple[Path, ...]:
    written: list[Path] = []
    for path, document in _documents().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_canonical_bytes(document))
        written.append(path)
    return tuple(written)


def check_contracts() -> tuple[str, ...]:
    failures: list[str] = []
    for path, document in _documents().items():
        expected = _canonical_bytes(document)
        if not path.is_file():
            failures.append(
                f"generated_contract_missing:{path.relative_to(PROJECT_ROOT)}"
            )
            continue
        if path.read_bytes() != expected:
            failures.append(
                f"generated_contract_drift:{path.relative_to(PROJECT_ROOT)}"
            )
    return tuple(failures)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check generated internal-web API and data schema contracts."
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Regenerate the committed machine-readable contracts.",
    )
    args = parser.parse_args(argv)
    if args.write:
        for path in write_contracts():
            print(f"wrote {path.relative_to(PROJECT_ROOT)}")
        return 0
    failures = check_contracts()
    for failure in failures:
        print(failure)
    if failures:
        print(
            "internal-web contract check failed; regenerate with "
            "tools/check_internal_web_contracts.py --write"
        )
        return 1
    print("internal-web generated contract check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
