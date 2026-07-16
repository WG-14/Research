from pathlib import Path

import pytest

import market_research.storage_io as storage_io
from market_research.storage_io import (
    append_jsonl,
    write_json_atomic_create_or_verify,
)


def test_append_jsonl_fsyncs_record_and_new_directory_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "audit.jsonl"
    durability_calls: list[str] = []
    monkeypatch.setattr(
        storage_io.os,
        "fsync",
        lambda _fd: durability_calls.append("file"),
    )
    monkeypatch.setattr(
        storage_io,
        "_fsync_parent_directory",
        lambda _path: durability_calls.append("directory"),
    )

    append_jsonl(target, {"event_id": "one"})

    assert durability_calls == ["file", "directory"]
    assert target.read_bytes().endswith(b"\n")

    durability_calls.clear()
    append_jsonl(target, {"event_id": "two"})
    assert durability_calls == ["file"]
    assert len(target.read_text(encoding="utf-8").splitlines()) == 2


def test_create_or_verify_accepts_only_its_exact_canonical_projection(
    tmp_path: Path,
) -> None:
    target = tmp_path / "approval.json"
    payload = {"artifact_type": "strategy_research_approval", "value": 1}

    assert write_json_atomic_create_or_verify(target, payload) is True
    prior = target.read_bytes()
    assert write_json_atomic_create_or_verify(target, payload) is False
    assert target.read_bytes() == prior

    target.write_text(
        '{"artifact_type":"strategy_research_approval","value":1}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="atomic_json_target_conflict"):
        write_json_atomic_create_or_verify(target, payload)


def test_create_or_verify_rejects_existing_symlink_without_following_it(
    tmp_path: Path,
) -> None:
    real_target = tmp_path / "real.json"
    real_target.write_text("sentinel\n", encoding="utf-8")
    link = tmp_path / "approval.json"
    link.symlink_to(real_target)

    with pytest.raises(ValueError, match="atomic_json_target_conflict"):
        write_json_atomic_create_or_verify(link, {"value": 1})

    assert real_target.read_text(encoding="utf-8") == "sentinel\n"
