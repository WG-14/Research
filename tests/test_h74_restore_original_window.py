from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from bithumb_bot.cli.commands import runtime as runtime_commands
from bithumb_bot.h74_observation import H74ObservationAuthorityError
from bithumb_bot.h74_pre_submit_evidence import build_h74_pre_submit_evidence_bundle
from bithumb_bot.h74_restore_check import verify_h74_restore_original_window
from bithumb_bot.storage_io import write_json_atomic
from tests.test_h74_authority_env_alignment import _settings
from tests.test_h74_source_variant_authority import _source, _variant


def test_restore_check_passes_for_source_authority_and_9_11_env() -> None:
    result = verify_h74_restore_original_window(
        authority_payload=_source(),
        settings_obj=_settings(9, 11),
        env_hash="sha256:" + "1" * 64,
    )
    assert result["status"] == "PASS"
    assert result["source_authority_hash"].startswith("sha256:")
    assert result["effective_behavior_parameter_hash"].startswith("sha256:")


def test_restore_check_rejects_no_window_authority_path() -> None:
    with pytest.raises(H74ObservationAuthorityError, match="requires_source_authority"):
        verify_h74_restore_original_window(
            authority_payload=_variant(),
            settings_obj=_settings(0, 24),
            env_hash="sha256:" + "1" * 64,
        )


def test_restore_check_rejects_env_0_24() -> None:
    with pytest.raises(H74ObservationAuthorityError):
        verify_h74_restore_original_window(
            authority_payload=_source(),
            settings_obj=_settings(0, 24),
            env_hash="sha256:" + "1" * 64,
        )


def test_restore_check_rejects_non_window_behavior_mismatch() -> None:
    cfg = _settings(9, 11)
    cfg.SMA_LONG = 99
    with pytest.raises(H74ObservationAuthorityError, match="SMA_LONG|runtime_mismatch"):
        verify_h74_restore_original_window(
            authority_payload=_source(),
            settings_obj=cfg,
            env_hash="sha256:" + "1" * 64,
        )


def _pass_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE strategy_decisions(id INTEGER PRIMARY KEY, probe_run_id TEXT, pair TEXT, signal TEXT);
        CREATE TABLE execution_plan(id INTEGER PRIMARY KEY, probe_run_id TEXT, pair TEXT, side TEXT, submit_expected INTEGER);
        CREATE TABLE orders(id INTEGER PRIMARY KEY, probe_run_id TEXT, pair TEXT, client_order_id TEXT, side TEXT);
        CREATE TABLE order_events(id INTEGER PRIMARY KEY, probe_run_id TEXT, pair TEXT, client_order_id TEXT, side TEXT, event_type TEXT, exception_class TEXT);
        CREATE TABLE fills(id INTEGER PRIMARY KEY, probe_run_id TEXT, pair TEXT, client_order_id TEXT, side TEXT);
        CREATE TABLE trades(id INTEGER PRIMARY KEY, probe_run_id TEXT, pair TEXT, client_order_id TEXT, side TEXT);
        CREATE TABLE open_position_lots(id INTEGER PRIMARY KEY, probe_run_id TEXT, pair TEXT);
        CREATE TABLE trade_lifecycles(id INTEGER PRIMARY KEY, probe_run_id TEXT, pair TEXT);
        CREATE TABLE portfolio(id INTEGER PRIMARY KEY, probe_run_id TEXT, pair TEXT, asset_qty REAL);
        INSERT INTO strategy_decisions(probe_run_id, pair, signal) VALUES('probe-1', 'KRW-BTC', 'BUY');
        INSERT INTO strategy_decisions(probe_run_id, pair, signal) VALUES('probe-1', 'KRW-BTC', 'SELL');
        INSERT INTO execution_plan(probe_run_id, pair, side, submit_expected) VALUES('probe-1', 'KRW-BTC', 'BUY', 1);
        INSERT INTO execution_plan(probe_run_id, pair, side, submit_expected) VALUES('probe-1', 'KRW-BTC', 'SELL', 1);
        INSERT INTO orders(probe_run_id, pair, client_order_id, side) VALUES('probe-1', 'KRW-BTC', 'buy-1', 'BUY');
        INSERT INTO orders(probe_run_id, pair, client_order_id, side) VALUES('probe-1', 'KRW-BTC', 'sell-1', 'SELL');
        INSERT INTO order_events(probe_run_id, pair, client_order_id, side, event_type, exception_class) VALUES('probe-1', 'KRW-BTC', 'buy-1', 'BUY', 'submit', '');
        INSERT INTO order_events(probe_run_id, pair, client_order_id, side, event_type, exception_class) VALUES('probe-1', 'KRW-BTC', 'sell-1', 'SELL', 'submit', '');
        INSERT INTO fills(probe_run_id, pair, client_order_id, side) VALUES('probe-1', 'KRW-BTC', 'buy-1', 'BUY');
        INSERT INTO fills(probe_run_id, pair, client_order_id, side) VALUES('probe-1', 'KRW-BTC', 'sell-1', 'SELL');
        INSERT INTO trades(probe_run_id, pair, client_order_id, side) VALUES('probe-1', 'KRW-BTC', 'buy-1', 'BUY');
        INSERT INTO trades(probe_run_id, pair, client_order_id, side) VALUES('probe-1', 'KRW-BTC', 'sell-1', 'SELL');
        INSERT INTO open_position_lots(probe_run_id, pair) VALUES('probe-1', 'KRW-BTC');
        INSERT INTO trade_lifecycles(probe_run_id, pair) VALUES('probe-1', 'KRW-BTC');
        INSERT INTO portfolio(probe_run_id, pair, asset_qty) VALUES('probe-1', 'KRW-BTC', 0);
        """
    )
    conn.commit()
    conn.close()


def _bundle_path(path: Path) -> Path:
    bundle = build_h74_pre_submit_evidence_bundle(
        authority_payload=_variant(),
        settings_obj=_settings(0, 24),
        env_hash="sha256:" + "6" * 64,
        risk_baseline_certificate_hash="sha256:" + "7" * 64,
        db_snapshot_hash="sha256:" + "8" * 64,
        starting_broker_position={"qty": 0},
        starting_local_position={"qty": 0},
        flat_start_proof={"flat": True},
        disk_capacity_path="/tmp",
    )
    write_json_atomic(path, bundle)
    return path


def _set_roots(monkeypatch, tmp_path: Path) -> None:
    for key in ("ENV_ROOT", "RUN_ROOT", "DATA_ROOT", "LOG_ROOT", "BACKUP_ROOT"):
        monkeypatch.setenv(key, str(tmp_path / key.lower()))
    monkeypatch.setenv("MODE", "live")


def test_post_probe_restore_check_runs_after_probe_pass(tmp_path: Path, monkeypatch) -> None:
    _set_roots(monkeypatch, tmp_path)
    db_path = tmp_path / "probe.sqlite"
    _pass_db(db_path)
    source_path = tmp_path / "source.json"
    write_json_atomic(source_path, _source())
    args = SimpleNamespace(
        pre_submit_evidence=str(_bundle_path(tmp_path / "bundle.json")),
        probe_run_id="probe-1",
        db=str(db_path),
        pair="KRW-BTC",
        min_executable_qty=0.0,
        restore_authority=str(source_path),
    )
    messages: list[str] = []
    rc = runtime_commands._h74_no_window_probe(args, SimpleNamespace(settings=_settings(9, 11), printer=messages.append))

    assert rc == 0
    assert "restore_artifact=" in messages[-1]


def test_post_probe_restore_rejects_no_window_authority_path(tmp_path: Path, monkeypatch) -> None:
    _set_roots(monkeypatch, tmp_path)
    db_path = tmp_path / "probe.sqlite"
    _pass_db(db_path)
    variant_path = tmp_path / "variant.json"
    write_json_atomic(variant_path, _variant())
    args = SimpleNamespace(
        pre_submit_evidence=str(_bundle_path(tmp_path / "bundle.json")),
        probe_run_id="probe-1",
        db=str(db_path),
        pair="KRW-BTC",
        min_executable_qty=0.0,
        restore_authority=str(variant_path),
    )

    with pytest.raises(H74ObservationAuthorityError, match="requires_source_authority"):
        runtime_commands._h74_no_window_probe(args, SimpleNamespace(settings=_settings(0, 24), printer=lambda _message: None))


def test_restore_artifact_is_written_with_hashes(tmp_path: Path, monkeypatch) -> None:
    _set_roots(monkeypatch, tmp_path)
    db_path = tmp_path / "probe.sqlite"
    _pass_db(db_path)
    source_path = tmp_path / "source.json"
    write_json_atomic(source_path, _source())
    args = SimpleNamespace(
        pre_submit_evidence=str(_bundle_path(tmp_path / "bundle.json")),
        probe_run_id="probe-1",
        db=str(db_path),
        pair="KRW-BTC",
        min_executable_qty=0.0,
        restore_authority=str(source_path),
    )

    rc = runtime_commands._h74_no_window_probe(args, SimpleNamespace(settings=_settings(9, 11), printer=lambda _message: None))

    assert rc == 0
    written = list((tmp_path / "data_root" / "live" / "reports" / "h74_restore_original_window_check").glob("*.json"))
    assert written
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["source_authority_hash"].startswith("sha256:")
    assert payload["env_hash"].startswith("sha256:")
    assert payload["effective_behavior_parameter_hash"].startswith("sha256:")
    assert payload["restore_check_hash"].startswith("sha256:")
