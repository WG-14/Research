from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from bithumb_bot.cli.commands import runtime as runtime_commands
from bithumb_bot.h74_pre_submit_evidence import (
    H74PreSubmitEvidenceError,
    build_h74_pre_submit_evidence_bundle,
    require_pre_submit_bundle_hash,
)
from bithumb_bot.storage_io import write_json_atomic
from tests.test_h74_authority_env_alignment import _settings
from tests.test_h74_source_variant_authority import _source, _variant


def _bundle(authority: dict[str, object], *, start: int = 0, end: int = 24, flat: bool = True, min_free: int = 1) -> dict[str, object]:
    return build_h74_pre_submit_evidence_bundle(
        authority_payload=authority,
        settings_obj=_settings(start, end),
        env_hash="sha256:" + "6" * 64,
        risk_baseline_certificate_hash="sha256:" + "7" * 64,
        db_snapshot_hash="sha256:" + "8" * 64,
        starting_broker_position={"qty": 0},
        starting_local_position={"qty": 0},
        flat_start_proof={"flat": flat},
        disk_capacity_path="/tmp",
        min_free_bytes=min_free,
    )


def test_pre_submit_bundle_requires_authority_env_match() -> None:
    with pytest.raises(Exception, match="MISMATCH|runtime_mismatch"):
        _bundle(_source(), start=0, end=24)


def test_pre_submit_bundle_requires_flat_start() -> None:
    with pytest.raises(H74PreSubmitEvidenceError, match="flat_start_required"):
        _bundle(_variant(), flat=False)


def test_pre_submit_bundle_requires_disk_capacity() -> None:
    with pytest.raises(H74PreSubmitEvidenceError, match="disk_capacity_insufficient"):
        _bundle(_variant(), min_free=10**30)


def test_pre_submit_bundle_records_effective_behavior_parameters() -> None:
    payload = _bundle(_variant())
    assert payload["effective_behavior_parameters"]["DAILY_PARTICIPATION_WINDOW_START_HOUR_KST"] == 0
    assert payload["variant_overrides"]["DAILY_PARTICIPATION_WINDOW_END_HOUR_KST"] == 24
    assert payload["pre_submit_evidence_hash"].startswith("sha256:")


def test_probe_run_requires_pre_submit_bundle_hash() -> None:
    with pytest.raises(H74PreSubmitEvidenceError, match="required"):
        require_pre_submit_bundle_hash({})
    require_pre_submit_bundle_hash(_bundle(_variant()))


def test_probe_submit_path_requires_pre_submit_bundle_hash() -> None:
    messages: list[str] = []
    args = SimpleNamespace(pre_submit_evidence="", probe_run_id="probe-1")
    context = SimpleNamespace(settings=_settings(0, 24), printer=messages.append)

    rc = runtime_commands._h74_no_window_probe(args, context)

    assert rc == 1
    assert "h74_no_window_probe_pre_submit_evidence_hash_required" in messages[0]


def test_probe_submit_path_rejects_mismatched_bundle(tmp_path: Path) -> None:
    payload = _bundle(_variant())
    payload["flat_start_proof"]["flat"] = False
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(payload), encoding="utf-8")
    args = SimpleNamespace(
        pre_submit_evidence=str(bundle_path),
        probe_run_id="probe-1",
        db=str(tmp_path / "probe.sqlite"),
        pair="KRW-BTC",
        min_executable_qty=0.0,
        restore_authority="",
    )
    context = SimpleNamespace(settings=_settings(0, 24), printer=lambda _message: None)

    with pytest.raises(H74PreSubmitEvidenceError, match="hash_mismatch|flat_start"):
        runtime_commands._h74_no_window_probe(args, context)


def test_probe_startup_log_records_pre_submit_evidence_hash(tmp_path: Path, monkeypatch) -> None:
    for key in ("ENV_ROOT", "RUN_ROOT", "DATA_ROOT", "LOG_ROOT", "BACKUP_ROOT"):
        monkeypatch.setenv(key, str(tmp_path / key.lower()))
    monkeypatch.setenv("MODE", "live")
    db_path = tmp_path / "probe.sqlite"
    db_path.touch()
    bundle = _bundle(_variant())
    bundle_path = tmp_path / "bundle.json"
    write_json_atomic(bundle_path, bundle)
    messages: list[str] = []
    args = SimpleNamespace(
        pre_submit_evidence=str(bundle_path),
        probe_run_id="probe-1",
        db=str(db_path),
        pair="KRW-BTC",
        min_executable_qty=0.0,
        restore_authority="",
    )
    context = SimpleNamespace(settings=_settings(0, 24), printer=messages.append)

    rc = runtime_commands._h74_no_window_probe(args, context)

    assert rc == 1
    startup_path = tmp_path / "data_root" / "live" / "reports" / "h74_no_window_probe_startup" / "h74_no_window_probe_startup_"
    written = list((tmp_path / "data_root" / "live" / "reports" / "h74_no_window_probe_startup").glob("*.json"))
    assert written
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["pre_submit_evidence_hash"] == bundle["pre_submit_evidence_hash"]
    assert "submit_path_entry=external_run_wrapper_required" not in "".join(messages)
    assert startup_path
