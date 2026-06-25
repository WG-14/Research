from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from bithumb_bot import operator_commands
from bithumb_bot.live_dry_run_isolation import LiveDryRunIsolationError, validate_live_dry_run_state_isolation


def test_cmd_live_dry_run_calls_startup_isolation_gate(monkeypatch) -> None:
    called = {"value": False}

    class GateCalled(RuntimeError):
        pass

    def _gate(_settings):
        called["value"] = True
        raise GateCalled("gate-called")

    monkeypatch.setattr("bithumb_bot.live_dry_run_isolation.validate_live_dry_run_state_isolation", _gate)

    with pytest.raises(GateCalled):
        operator_commands.cmd_live_dry_run()

    assert called["value"] is True


def test_live_dry_run_refuses_live_sqlite_without_copy() -> None:
    cfg = SimpleNamespace(MODE="live", LIVE_DRY_RUN=True, DB_PATH="/var/lib/bithumb-bot/data/live/trades/live.sqlite")
    with pytest.raises(LiveDryRunIsolationError, match="refuses_direct_live_sqlite"):
        validate_live_dry_run_state_isolation(cfg)


def test_live_dry_run_does_not_modify_live_target_position_state() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE target_position_state(pair TEXT)")
    before = conn.execute("SELECT COUNT(*) FROM target_position_state").fetchone()[0]
    cfg = SimpleNamespace(MODE="live", LIVE_DRY_RUN=True, DB_PATH="/var/lib/bithumb-bot/data/live/trades/live.sqlite")
    with pytest.raises(LiveDryRunIsolationError):
        validate_live_dry_run_state_isolation(cfg)
    after = conn.execute("SELECT COUNT(*) FROM target_position_state").fetchone()[0]
    assert after == before


def test_live_dry_run_does_not_leave_live_h74_virtual_open() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE strategy_virtual_target_state(lifecycle_state TEXT)")
    cfg = SimpleNamespace(MODE="live", LIVE_DRY_RUN=True, DB_PATH="/var/lib/bithumb-bot/data/live/trades/live.sqlite")
    with pytest.raises(LiveDryRunIsolationError):
        validate_live_dry_run_state_isolation(cfg)
    assert conn.execute("SELECT COUNT(*) FROM strategy_virtual_target_state WHERE lifecycle_state='virtual_open'").fetchone()[0] == 0


def test_live_dry_run_artifacts_are_namespaced() -> None:
    cfg = SimpleNamespace(MODE="live", LIVE_DRY_RUN=True, DB_PATH="/var/lib/bithumb-bot/data/live/reports/dryrun.sqlite")
    validate_live_dry_run_state_isolation(cfg)
    assert "/reports/" in cfg.DB_PATH
