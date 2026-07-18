from __future__ import annotations

import sqlite3
from pathlib import Path

from market_research.research.dataset_freeze import CANONICAL_CANDLES_TABLE_DDL
from market_research.research.datasets.schema_dictionary import (
    canonical_data_fields,
    data_dictionary_payload,
)
from tools.check_dataset_dictionary import dictionary_is_current


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_data_dictionary_has_every_required_semantic_attribute() -> None:
    payload = data_dictionary_payload()

    assert payload["schema_version"] == 1
    assert str(payload["content_hash"]).startswith("sha256:")
    fields = payload["fields"]
    assert isinstance(fields, list)
    assert len(fields) == len(canonical_data_fields())
    required = {
        "dataset",
        "name",
        "type",
        "unit",
        "meaning",
        "nullable",
        "valid_range",
        "generation_method",
        "available_at",
        "provider",
        "change_history",
        "owner_module",
    }
    for field in fields:
        assert isinstance(field, dict)
        assert set(field) == required
        assert field["change_history"]
        assert all(
            set(change) == {"version", "effective_date", "description"}
            for change in field["change_history"]
        )


def test_candle_dictionary_matches_the_canonical_sqlite_schema() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(CANONICAL_CANDLES_TABLE_DDL)
        actual = {
            str(row[1]): (str(row[2]), not bool(row[3]))
            for row in connection.execute("PRAGMA table_info(candles)").fetchall()
        }
    finally:
        connection.close()

    fields = {
        field.name: field
        for field in canonical_data_fields()
        if field.dataset == "frozen_sqlite_candles"
    }
    assert set(fields) == set(actual)
    for name, (storage_type, nullable) in actual.items():
        assert storage_type in fields[name].type
        assert fields[name].nullable is nullable


def test_published_data_dictionary_is_generated_from_code() -> None:
    assert dictionary_is_current(
        REPOSITORY_ROOT / "docs/generated/research-data-dictionary.json"
    )
