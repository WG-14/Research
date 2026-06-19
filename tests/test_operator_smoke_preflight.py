from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from bithumb_bot.config import LiveModeValidationError, settings
from bithumb_bot.db_core import ensure_db
from bithumb_bot.operator_smoke_preflight import validate_operator_smoke_preflight


def _live_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for key, dirname in {
        "ENV_ROOT": "envroot",
        "RUN_ROOT": "runroot",
        "DATA_ROOT": "dataroot",
        "LOG_ROOT": "logroot",
        "BACKUP_ROOT": "backuproot",
    }.items():
        monkeypatch.setenv(key, str(tmp_path / dirname))
    db_path = tmp_path / "dataroot" / "live" / "trades" / "live.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    return db_path


def _live_settings(db_path: Path, **overrides):
    base = replace(
        settings,
        MODE="live",
        LIVE_DRY_RUN=False,
        LIVE_REAL_ORDER_ARMED=True,
        KILL_SWITCH=False,
        DB_PATH=str(db_path),
        PAIR="KRW-BTC",
        BITHUMB_API_KEY="key",
        BITHUMB_API_SECRET="x" * 64,
    )
    return replace(base, **overrides)


def test_operator_smoke_preflight_allows_live_armed_without_approved_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = _live_roots(monkeypatch, tmp_path)
    conn = ensure_db(str(db_path))
    try:
        validate_operator_smoke_preflight(
            cfg=_live_settings(db_path, APPROVED_STRATEGY_PROFILE_PATH=""),
            conn=conn,
            market="KRW-BTC",
            market_preflight=lambda _cfg: None,
        )
    finally:
        conn.close()


def test_operator_smoke_preflight_rejects_live_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = _live_roots(monkeypatch, tmp_path)
    conn = ensure_db(str(db_path))
    try:
        with pytest.raises(LiveModeValidationError, match="LIVE_DRY_RUN=false"):
            validate_operator_smoke_preflight(
                cfg=_live_settings(db_path, LIVE_DRY_RUN=True, LIVE_REAL_ORDER_ARMED=False),
                conn=conn,
                market="KRW-BTC",
                market_preflight=lambda _cfg: None,
            )
    finally:
        conn.close()


def test_operator_smoke_preflight_rejects_unarmed_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = _live_roots(monkeypatch, tmp_path)
    conn = ensure_db(str(db_path))
    try:
        with pytest.raises(LiveModeValidationError, match="LIVE_REAL_ORDER_ARMED=true"):
            validate_operator_smoke_preflight(
                cfg=_live_settings(db_path, LIVE_REAL_ORDER_ARMED=False),
                conn=conn,
                market="KRW-BTC",
                market_preflight=lambda _cfg: None,
            )
    finally:
        conn.close()


def test_operator_smoke_preflight_rejects_kill_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = _live_roots(monkeypatch, tmp_path)
    conn = ensure_db(str(db_path))
    try:
        with pytest.raises(LiveModeValidationError, match="KILL_SWITCH=true"):
            validate_operator_smoke_preflight(
                cfg=_live_settings(db_path, KILL_SWITCH=True),
                conn=conn,
                market="KRW-BTC",
                market_preflight=lambda _cfg: None,
            )
    finally:
        conn.close()
