from __future__ import annotations
from types import SimpleNamespace
from market_research.research.cli import _required_runtime_db_path


def test_frozen_candle_run_does_not_require_database_path() -> None:
    paths = SimpleNamespace(require_database_path=lambda: (_ for _ in ()).throw(AssertionError("opened")))
    manifest = SimpleNamespace(dataset=SimpleNamespace(source="frozen_sqlite_candles", top_of_book=None, depth=None))
    assert _required_runtime_db_path(SimpleNamespace(paths=paths), manifest) is None


def test_sqlite_candle_source_requires_database_path() -> None:
    paths = SimpleNamespace(require_database_path=lambda: "/tmp/data.sqlite")
    manifest = SimpleNamespace(dataset=SimpleNamespace(source="sqlite_candles", top_of_book=None, depth=None))
    assert _required_runtime_db_path(SimpleNamespace(paths=paths), manifest) == "/tmp/data.sqlite"
