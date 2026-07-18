#!/usr/bin/env python3
"""Verify or regenerate the published research data dictionary."""

from __future__ import annotations

import argparse
from pathlib import Path

from market_research.research.datasets.schema_dictionary import (
    render_data_dictionary_json,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PUBLISHED_DICTIONARY = PROJECT_ROOT / "docs/generated/research-data-dictionary.json"


def dictionary_is_current(path: Path = PUBLISHED_DICTIONARY) -> bool:
    try:
        actual = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return actual == render_data_dictionary_json()


def write_dictionary(path: Path = PUBLISHED_DICTIONARY) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_data_dictionary_json(), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the generated research data dictionary for drift."
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Regenerate docs/generated/research-data-dictionary.json.",
    )
    args = parser.parse_args(argv)
    if args.write:
        write_dictionary()
        print(f"wrote {PUBLISHED_DICTIONARY.relative_to(PROJECT_ROOT)}")
        return 0
    if not dictionary_is_current():
        print(
            "research data dictionary is missing or stale; run "
            "tools/check_dataset_dictionary.py --write"
        )
        return 1
    print("research data dictionary check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
