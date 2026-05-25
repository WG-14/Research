from __future__ import annotations

import json
from pathlib import Path
import os
import sqlite3

import pytest

from bithumb_bot import config
from bithumb_bot import engine as engine_module
from bithumb_bot.config import settings
from bithumb_bot.db_core import ensure_db
from bithumb_bot.engine import compute_signal
from bithumb_bot.profile_cli import cmd_replay_decision
from bithumb_bot.runtime_sma_snapshot import build_sma_with_filter_replay_bundle
from bithumb_bot.research.strategy_registry import (
    ResearchStrategyRegistryError,
    resolve_research_strategy_plugin,
)
from bithumb_bot.strategy.sma import SmaWithFilterStrategy
from bithumb_bot.strategy import create_strategy, list_strategies
from bithumb_bot.strategy.base import StrategyDecision


def test_registry_default_strategy_available() -> None:
    assert "sma_cross" in list_strategies()
    assert "sma_with_filter" in list_strategies()


def test_compute_signal_uses_default_strategy_name_from_settings(tmp_path) -> None:
    old_db_path = settings.DB_PATH
    old_strategy_name = settings.STRATEGY_NAME
    old_env_db_path = os.environ.get("DB_PATH")

    db_path = str(tmp_path / "strategy_default.sqlite")
    os.environ["DB_PATH"] = db_path
    object.__setattr__(settings, "DB_PATH", db_path)
    object.__setattr__(settings, "STRATEGY_NAME", "sma_with_filter")

    conn = ensure_db()
    base_ts = 1_700_000_000_000
    try:
        closes = [10.0 + 0.2 * idx for idx in range(40)]
        for idx, close in enumerate(closes):
            ts = base_ts + idx * 60_000
            conn.execute(
                """
                INSERT OR REPLACE INTO candles(ts, pair, interval, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, settings.PAIR, settings.INTERVAL, close, close, close, close, 1.0),
            )
        conn.commit()

        result = compute_signal(conn, 2, 3)
    finally:
        conn.close()
        object.__setattr__(settings, "DB_PATH", old_db_path)
        object.__setattr__(settings, "STRATEGY_NAME", old_strategy_name)
        if old_env_db_path is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = old_env_db_path

    assert result is not None
    assert result["signal"] in {"BUY", "SELL", "HOLD"}
    assert result["strategy"] == "sma_with_filter"
    assert "reason" in result


def test_compute_signal_routes_sma_with_filter_through_snapshot_orchestration(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_db_path = settings.DB_PATH
    old_strategy_name = settings.STRATEGY_NAME
    old_env_db_path = os.environ.get("DB_PATH")
    calls: list[str] = []

    def _snapshot_orchestration(conn, strategy, *, through_ts_ms=None, normalizer=None):
        calls.append(strategy.name)
        return StrategyDecision(
            signal="HOLD",
            reason="test snapshot orchestration",
            context={"strategy": strategy.name},
        )

    monkeypatch.setattr(
        engine_module,
        "decide_sma_with_filter_snapshot_from_db",
        _snapshot_orchestration,
    )

    db_path = str(tmp_path / "strategy_snapshot_route.sqlite")
    os.environ["DB_PATH"] = db_path
    object.__setattr__(settings, "DB_PATH", db_path)
    object.__setattr__(settings, "STRATEGY_NAME", "sma_with_filter")

    conn = ensure_db()
    base_ts = 1_700_000_000_000
    try:
        closes = [10.0 + 0.2 * idx for idx in range(40)]
        for idx, close in enumerate(closes):
            ts = base_ts + idx * 60_000
            conn.execute(
                """
                INSERT OR REPLACE INTO candles(ts, pair, interval, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, settings.PAIR, settings.INTERVAL, close, close, close, close, 1.0),
            )
        conn.commit()

        result = compute_signal(conn, 2, 3)
    finally:
        conn.close()
        object.__setattr__(settings, "DB_PATH", old_db_path)
        object.__setattr__(settings, "STRATEGY_NAME", old_strategy_name)
        if old_env_db_path is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = old_env_db_path

    assert result is not None
    assert calls == ["sma_with_filter"]


def test_live_sma_with_filter_route_does_not_call_legacy_decide(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_db_path = settings.DB_PATH
    old_strategy_name = settings.STRATEGY_NAME
    old_env_db_path = os.environ.get("DB_PATH")

    def _fail_legacy_decide(*args, **kwargs):
        raise AssertionError("legacy Strategy.decide(conn) path called")

    def _fail_legacy_normalized_db(*args, **kwargs):
        raise AssertionError("legacy _decide_from_normalized_db path called")

    monkeypatch.setattr(SmaWithFilterStrategy, "decide", _fail_legacy_decide)
    monkeypatch.setattr(
        SmaWithFilterStrategy,
        "_decide_from_normalized_db",
        _fail_legacy_normalized_db,
    )

    db_path = str(tmp_path / "strategy_no_legacy_decide.sqlite")
    os.environ["DB_PATH"] = db_path
    object.__setattr__(settings, "DB_PATH", db_path)
    object.__setattr__(settings, "STRATEGY_NAME", "sma_with_filter")

    conn = ensure_db()
    base_ts = 1_700_000_300_000
    try:
        closes = [10.0 + 0.2 * idx for idx in range(40)]
        for idx, close in enumerate(closes):
            ts = base_ts + idx * 60_000
            conn.execute(
                """
                INSERT OR REPLACE INTO candles(ts, pair, interval, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, settings.PAIR, settings.INTERVAL, close, close, close, close, 1.0),
            )
        conn.commit()

        result = compute_signal(conn, 2, 3)
    finally:
        conn.close()
        object.__setattr__(settings, "DB_PATH", old_db_path)
        object.__setattr__(settings, "STRATEGY_NAME", old_strategy_name)
        if old_env_db_path is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = old_env_db_path

    assert result is not None
    assert result["strategy"] == "sma_with_filter"


def test_runtime_replay_bundle_contains_reproducibility_material(tmp_path) -> None:
    old_db_path = settings.DB_PATH
    old_env_db_path = os.environ.get("DB_PATH")

    db_path = str(tmp_path / "strategy_replay_bundle.sqlite")
    os.environ["DB_PATH"] = db_path
    object.__setattr__(settings, "DB_PATH", db_path)

    conn = ensure_db()
    base_ts = 1_700_000_400_000
    try:
        closes = [10.0 + 0.2 * idx for idx in range(40)]
        for idx, close in enumerate(closes):
            ts = base_ts + idx * 60_000
            conn.execute(
                """
                INSERT OR REPLACE INTO candles(ts, pair, interval, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, settings.PAIR, settings.INTERVAL, close, close, close, close, 1.0),
            )
        conn.commit()

        strategy = create_strategy(
            "sma_with_filter",
            short_n=2,
            long_n=3,
            pair=settings.PAIR,
            interval=settings.INTERVAL,
        )
        changes_before_replay = conn.total_changes
        bundle = build_sma_with_filter_replay_bundle(
            conn,
            strategy,
            through_ts_ms=base_ts + 39 * 60_000,
        )
        changes_after_replay = conn.total_changes
    finally:
        conn.close()
        object.__setattr__(settings, "DB_PATH", old_db_path)
        if old_env_db_path is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = old_env_db_path

    assert bundle is not None
    assert {
        "schema_version",
        "strategy",
        "through_ts_ms",
        "boundary_stages",
        "market_snapshot",
        "position_snapshot",
        "policy_config",
        "execution_constraint_snapshot",
        "policy_input_hash",
        "policy_decision_hash",
        "pure_policy_hash",
        "replay_fingerprint",
        "pure_policy_trace",
        "final_strategy_decision",
        "execution_decision_summary",
    }.issubset(bundle)
    assert changes_after_replay == changes_before_replay
    assert bundle["market_snapshot"]["candle_ts"] == base_ts + 39 * 60_000
    assert bundle["position_snapshot"] is not None
    assert bundle["policy_config"]["short_n"] == 2
    assert str(bundle["policy_input_hash"]).startswith("sha256:")
    assert str(bundle["policy_decision_hash"]).startswith("sha256:")
    assert str(bundle["pure_policy_hash"]).startswith("sha256:")
    assert isinstance(bundle["replay_fingerprint"], dict)
    assert bundle["replay_fingerprint"]["strategy_name"] == "sma_with_filter"
    assert bundle["replay_fingerprint"]["through_ts_ms"] == base_ts + 39 * 60_000
    assert bundle["pure_policy_trace"]
    assert bundle["final_strategy_decision"]["strategy"] == "sma_with_filter"
    assert bundle["execution_decision_summary"]["execution_engine"] in {"lot_native", "target_delta"}


def test_replay_decision_cli_outputs_single_read_only_replay_bundle(tmp_path, capsys) -> None:
    old_db_path = settings.DB_PATH
    old_env_db_path = os.environ.get("DB_PATH")

    db_path = str(tmp_path / "single_replay_decision.sqlite")
    os.environ["DB_PATH"] = db_path
    object.__setattr__(settings, "DB_PATH", db_path)

    conn = ensure_db()
    base_ts = 1_700_000_500_000
    try:
        for idx in range(40):
            close = 10.0 + 0.2 * idx
            ts = base_ts + idx * 60_000
            conn.execute(
                """
                INSERT OR REPLACE INTO candles(ts, pair, interval, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, settings.PAIR, settings.INTERVAL, close, close, close, close, 1.0),
            )
        conn.commit()
        changes_before_replay = conn.total_changes
    finally:
        conn.close()
        object.__setattr__(settings, "DB_PATH", old_db_path)
        if old_env_db_path is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = old_env_db_path

    rc = cmd_replay_decision(
        db_path=db_path,
        strategy_name="sma_with_filter",
        candle_ts=base_ts + 39 * 60_000,
        as_json=True,
    )
    stdout = capsys.readouterr().out
    out = json.loads(stdout[stdout.index('{\n  "bundle"') :])

    verify_conn = sqlite3.connect(db_path)
    try:
        changes_after_replay = verify_conn.total_changes
    finally:
        verify_conn.close()

    assert rc == 0
    assert out["ok"] is True
    assert out["command"] == "replay-decision"
    bundle = out["bundle"]
    assert bundle["schema_version"] == 1
    assert bundle["boundary_stages"]["snapshot_builder"] == (
        "runtime_sma_snapshot.decide_sma_with_filter_snapshot_from_db"
    )
    assert bundle["market_snapshot"]["candle_ts"] == base_ts + 39 * 60_000
    assert str(bundle["policy_input_hash"]).startswith("sha256:")
    assert str(bundle["policy_decision_hash"]).startswith("sha256:")
    assert str(bundle["pure_policy_hash"]).startswith("sha256:")
    assert bundle["final_strategy_decision"]["strategy"] == "sma_with_filter"
    assert "execution_decision_summary" in bundle
    assert changes_after_replay == 0
    assert changes_before_replay > 0


def test_compute_signal_allows_strategy_override_for_backtest_compatibility(tmp_path) -> None:
    old_db_path = settings.DB_PATH
    old_env_db_path = os.environ.get("DB_PATH")

    db_path = str(tmp_path / "strategy_override.sqlite")
    os.environ["DB_PATH"] = db_path
    object.__setattr__(settings, "DB_PATH", db_path)

    conn = ensure_db()
    base_ts = 1_700_000_100_000
    try:
        for idx, close in enumerate([10.0, 11.0, 12.0, 13.0, 14.0]):
            ts = base_ts + idx * 60_000
            conn.execute(
                """
                INSERT OR REPLACE INTO candles(ts, pair, interval, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, settings.PAIR, settings.INTERVAL, close, close, close, close, 1.0),
            )
        conn.commit()

        result = compute_signal(conn, 2, 3, strategy_name="sma_cross")
    finally:
        conn.close()
        object.__setattr__(settings, "DB_PATH", old_db_path)
        if old_env_db_path is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = old_env_db_path

    assert result is not None
    assert result["signal"] in {"BUY", "SELL", "HOLD"}
    assert result["strategy"] == "sma_cross"
    assert "reason" in result


def test_compute_signal_normalizes_strategy_override_name(tmp_path) -> None:
    old_db_path = settings.DB_PATH
    old_env_db_path = os.environ.get("DB_PATH")

    db_path = str(tmp_path / "strategy_override_normalized.sqlite")
    os.environ["DB_PATH"] = db_path
    object.__setattr__(settings, "DB_PATH", db_path)

    conn = ensure_db()
    base_ts = 1_700_000_200_000
    try:
        for idx, close in enumerate([10.0, 11.0, 12.0, 13.0, 14.0]):
            ts = base_ts + idx * 60_000
            conn.execute(
                """
                INSERT OR REPLACE INTO candles(ts, pair, interval, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, settings.PAIR, settings.INTERVAL, close, close, close, close, 1.0),
            )
        conn.commit()

        result = compute_signal(conn, 2, 3, strategy_name="  SMA_CROSS ")
    finally:
        conn.close()
        object.__setattr__(settings, "DB_PATH", old_db_path)
        if old_env_db_path is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = old_env_db_path

    assert result is not None
    assert result["strategy"] == "sma_cross"


def test_live_compute_signal_rejects_plain_sma_cross_override() -> None:
    old_mode = settings.MODE

    object.__setattr__(settings, "MODE", "live")
    try:
        with sqlite3.connect(":memory:") as conn:
            with pytest.raises(config.LiveModeValidationError) as exc:
                compute_signal(conn, 2, 3, strategy_name="sma_cross")
    finally:
        object.__setattr__(settings, "MODE", old_mode)

    assert "plain_sma_live_not_allowed" in str(exc.value)


def test_sma_cross_is_excluded_from_research_promotion_plugin_registry() -> None:
    with pytest.raises(ResearchStrategyRegistryError, match="unsupported research strategy: sma_cross"):
        resolve_research_strategy_plugin("sma_cross")


def test_registry_rejects_unknown_strategy_name() -> None:
    with pytest.raises(ValueError, match="unknown strategy"):
        create_strategy("does_not_exist")


def test_registry_can_create_filtered_sma_strategy() -> None:
    strategy = create_strategy("sma_with_filter", short_n=2, long_n=3)
    assert strategy.name == "sma_with_filter"


def test_engine_no_direct_sma_import() -> None:
    engine_source = Path("src/bithumb_bot/engine.py").read_text()
    assert "from .strategy.sma import" not in engine_source
