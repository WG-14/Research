from __future__ import annotations
from types import SimpleNamespace
from market_research.research.cli import _required_runtime_db_path
import pytest


def test_frozen_candle_run_does_not_require_database_path() -> None:
    paths = SimpleNamespace(require_database_path=lambda: (_ for _ in ()).throw(AssertionError("opened")))
    manifest = SimpleNamespace(dataset=SimpleNamespace(source="frozen_sqlite_candles", top_of_book=None, depth=None))
    assert _required_runtime_db_path(SimpleNamespace(paths=paths), manifest) is None


def test_sqlite_candle_source_requires_database_path() -> None:
    paths = SimpleNamespace(require_database_path=lambda: "/tmp/data.sqlite")
    manifest = SimpleNamespace(dataset=SimpleNamespace(source="sqlite_candles", top_of_book=None, depth=None))
    assert _required_runtime_db_path(SimpleNamespace(paths=paths), manifest) == "/tmp/data.sqlite"


def test_immutable_execution_evidence_locators_do_not_require_runtime_database() -> None:
    paths = SimpleNamespace(
        require_database_path=lambda: (_ for _ in ()).throw(AssertionError("runtime database opened"))
    )
    locator = {
        "type": "content_addressed_local",
        "path": "/external/top.sqlite",
        "artifact_content_hash": "sha256:" + "a" * 64,
    }
    manifest = SimpleNamespace(
        dataset=SimpleNamespace(
            source="frozen_sqlite_candles",
            top_of_book=SimpleNamespace(
                source="sqlite_orderbook_top_snapshots",
                locator=locator,
            ),
            depth=None,
        ),
        execution_timing=SimpleNamespace(
            depth_required=False,
            min_execution_reality_level_for_validation=None,
        ),
        execution_model=SimpleNamespace(scenarios=()),
    )

    assert _required_runtime_db_path(SimpleNamespace(paths=paths), manifest) is None


@pytest.mark.parametrize(
    ("top_of_book", "depth", "timing", "scenarios", "role"),
    (
        (SimpleNamespace(source="sqlite_orderbook_top_snapshots"), None, SimpleNamespace(depth_required=False, min_execution_reality_level_for_validation=None), (), "top_of_book"),
        (None, SimpleNamespace(source="orderbook_depth_levels"), SimpleNamespace(depth_required=False, min_execution_reality_level_for_validation=None), (), "depth"),
        (None, None, SimpleNamespace(depth_required=True, min_execution_reality_level_for_validation=None), (), "depth"),
        (None, None, SimpleNamespace(depth_required=False, min_execution_reality_level_for_validation="l2_depth_walk_no_queue"), (), "depth"),
        (None, None, SimpleNamespace(depth_required=False, min_execution_reality_level_for_validation=None), (SimpleNamespace(type="depth_walk"),), "depth"),
    ),
)
def test_missing_runtime_context_identifies_source_capability_and_role(top_of_book, depth, timing, scenarios, role) -> None:
    paths = SimpleNamespace(require_database_path=lambda: (_ for _ in ()).throw(ValueError("missing")))
    manifest = SimpleNamespace(
        dataset=SimpleNamespace(source="frozen_sqlite_candles", top_of_book=top_of_book, depth=depth),
        execution_timing=timing,
        execution_model=SimpleNamespace(scenarios=scenarios),
    )
    with pytest.raises(ValueError, match=f"capability=runtime_db:role={role}"):
        _required_runtime_db_path(SimpleNamespace(paths=paths), manifest)
