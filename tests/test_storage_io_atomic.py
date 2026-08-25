import os
import stat
from pathlib import Path

import pytest

import market_research.storage_io as storage_io
from market_research.storage_io import (
    ATOMIC_PUBLICATION_MODE_ENV,
    append_authority_jsonl,
    append_jsonl,
    ensure_directory,
    open_lock_file,
    read_authority_text,
    write_json_atomic,
    write_json_atomic_create_or_verify,
    write_jsonl_atomic,
    write_text_atomic,
)


def test_process_lock_is_private_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ATOMIC_PUBLICATION_MODE_ENV, raising=False)
    path = tmp_path / "registry.lock"

    descriptor = open_lock_file(path)
    os.close(descriptor)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_process_lock_is_cross_uid_writable_only_in_shared_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ATOMIC_PUBLICATION_MODE_ENV, "0640")
    path = tmp_path / "registry.lock"

    first = open_lock_file(path)
    second = open_lock_file(path)
    os.close(second)
    os.close(first)

    assert stat.S_IMODE(path.stat().st_mode) == 0o660
    assert path.stat().st_gid == path.parent.stat().st_gid


def test_process_lock_rejects_existing_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ATOMIC_PUBLICATION_MODE_ENV, "0640")
    real = tmp_path / "real.lock"
    real.write_text("sentinel", encoding="utf-8")
    link = tmp_path / "registry.lock"
    link.symlink_to(real)

    with pytest.raises(ValueError, match="process_lock_access_invalid"):
        open_lock_file(link)

    assert real.read_text(encoding="utf-8") == "sentinel"


def test_process_lock_rejects_hardlinked_inode(tmp_path: Path) -> None:
    real = tmp_path / "real.lock"
    descriptor = open_lock_file(real)
    os.close(descriptor)
    alias = tmp_path / "registry.lock"
    alias.hardlink_to(real)

    with pytest.raises(ValueError, match="process_lock_access_invalid"):
        open_lock_file(real)


def test_append_jsonl_is_private_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ATOMIC_PUBLICATION_MODE_ENV, raising=False)
    target = tmp_path / "audit.jsonl"

    append_jsonl(target, {"event_id": "one"})

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_append_jsonl_shared_profile_is_exactly_group_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ATOMIC_PUBLICATION_MODE_ENV, "0640")
    target = tmp_path / "audit.jsonl"

    append_jsonl(target, {"event_id": "one"})
    append_jsonl(target, {"event_id": "two"})

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_authority_jsonl_shared_profile_is_exactly_group_writable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ATOMIC_PUBLICATION_MODE_ENV, "0640")
    target = tmp_path / "authority.jsonl"

    append_authority_jsonl(target, {"event_id": "reserved"})
    append_authority_jsonl(target, {"event_id": "activated"})

    assert stat.S_IMODE(target.stat().st_mode) == 0o660
    assert target.stat().st_gid == target.parent.stat().st_gid


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o664, 0o666])
def test_authority_jsonl_shared_profile_rejects_noncanonical_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
) -> None:
    monkeypatch.setenv(ATOMIC_PUBLICATION_MODE_ENV, "0640")
    target = tmp_path / "authority.jsonl"
    target.write_text("sentinel\n", encoding="utf-8")
    target.chmod(mode)

    with pytest.raises(ValueError, match="append_jsonl_access_mode_invalid"):
        append_authority_jsonl(target, {"event_id": "must-not-append"})

    assert target.read_text(encoding="utf-8") == "sentinel\n"


def test_authority_jsonl_rejects_hardlinked_inode(tmp_path: Path) -> None:
    target = tmp_path / "authority.jsonl"
    append_authority_jsonl(target, {"event_id": "reserved"})
    alias = tmp_path / "authority-alias.jsonl"
    alias.hardlink_to(target)

    with pytest.raises(ValueError, match="append_jsonl_access_mode_invalid"):
        append_authority_jsonl(target, {"event_id": "must-not-append"})


def test_operated_shared_authority_requires_kernel_append_only_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o2770)
    target = tmp_path / "authority.jsonl"
    target.write_text('{"event_id":"reserved"}\n', encoding="utf-8")
    target.chmod(0o660)
    monkeypatch.setenv(ATOMIC_PUBLICATION_MODE_ENV, "0640")
    monkeypatch.setenv("RESEARCH_RUNTIME_PROFILE", "operated")
    monkeypatch.setattr(storage_io, "_linux_file_flags", lambda _fd: 0)

    with pytest.raises(ValueError, match="kernel_append_only_required"):
        append_authority_jsonl(
            target,
            {"event_id": "must-not-append"},
            require_kernel_append_only=True,
        )
    with pytest.raises(ValueError, match="kernel_append_only_required"):
        read_authority_text(target, require_kernel_append_only=True)
    assert target.read_text(encoding="utf-8") == '{"event_id":"reserved"}\n'


def test_operated_shared_authority_accepts_attested_kernel_append_only_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o2770)
    target = tmp_path / "authority.jsonl"
    target.write_text('{"event_id":"reserved"}\n', encoding="utf-8")
    target.chmod(0o660)
    monkeypatch.setenv(ATOMIC_PUBLICATION_MODE_ENV, "0640")
    monkeypatch.setenv("RESEARCH_RUNTIME_PROFILE", "operated")
    monkeypatch.setattr(
        storage_io,
        "_linux_file_flags",
        lambda _fd: storage_io._LINUX_FS_APPEND_FL,
    )

    append_authority_jsonl(
        target,
        {"event_id": "activated"},
        require_kernel_append_only=True,
    )

    assert read_authority_text(target, require_kernel_append_only=True) == (
        '{"event_id":"reserved"}\n' '{"event_id":"activated"}\n'
    )


def test_operated_shared_generic_authority_does_not_inherit_holdout_append_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o2770)
    target = tmp_path / "generic-authority.jsonl"
    target.write_text('{"event_id":"reserved"}\n', encoding="utf-8")
    target.chmod(0o660)
    monkeypatch.setenv(ATOMIC_PUBLICATION_MODE_ENV, "0640")
    monkeypatch.setenv("RESEARCH_RUNTIME_PROFILE", "operated")
    monkeypatch.setattr(storage_io, "_linux_file_flags", lambda _fd: 0)

    append_authority_jsonl(target, {"event_id": "completed"})

    assert read_authority_text(target) == (
        '{"event_id":"reserved"}\n' '{"event_id":"completed"}\n'
    )


def test_append_jsonl_rejects_existing_file_with_wrong_access_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "audit.jsonl"
    target.write_text("sentinel\n", encoding="utf-8")
    target.chmod(0o600)
    monkeypatch.setenv(ATOMIC_PUBLICATION_MODE_ENV, "0640")

    with pytest.raises(ValueError, match="append_jsonl_access_mode_invalid"):
        append_jsonl(target, {"event_id": "must-not-append"})

    assert target.read_text(encoding="utf-8") == "sentinel\n"


def test_append_jsonl_rejects_symlink_without_mutating_target(
    tmp_path: Path,
) -> None:
    real_target = tmp_path / "real.jsonl"
    real_target.write_text("sentinel\n", encoding="utf-8")
    link = tmp_path / "audit.jsonl"
    link.symlink_to(real_target)

    with pytest.raises(OSError):
        append_jsonl(link, {"event_id": "must-not-append"})

    assert real_target.read_text(encoding="utf-8") == "sentinel\n"


def test_shared_directory_creation_is_setgid_and_group_writable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = tmp_path / "boundary"
    boundary.mkdir()
    target = boundary / "project" / "compute"
    monkeypatch.setenv(ATOMIC_PUBLICATION_MODE_ENV, "0640")

    ensure_directory(target, require_shared_mode=True)

    for directory in (target.parent, target):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o2770
        assert directory.stat().st_gid == boundary.stat().st_gid


def test_shared_directory_contract_rejects_existing_weak_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "project"
    target.mkdir(mode=0o750)
    monkeypatch.setenv(ATOMIC_PUBLICATION_MODE_ENV, "0640")

    with pytest.raises(ValueError, match="atomic_publication_directory_invalid"):
        ensure_directory(target, require_shared_mode=True)

    assert stat.S_IMODE(target.stat().st_mode) == 0o750


def test_shared_directory_contract_rejects_symlink_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_target = tmp_path / "real"
    real_target.mkdir(mode=0o770)
    real_target.chmod(0o2770)
    link = tmp_path / "project"
    link.symlink_to(real_target, target_is_directory=True)
    monkeypatch.setenv(ATOMIC_PUBLICATION_MODE_ENV, "0640")

    with pytest.raises(ValueError, match="atomic_publication_directory_invalid"):
        ensure_directory(link, require_shared_mode=True)


def test_shared_directory_contract_rejects_symlinked_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "redirect").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv(ATOMIC_PUBLICATION_MODE_ENV, "0640")

    with pytest.raises(ValueError, match="atomic_publication_directory_invalid"):
        ensure_directory(
            root / "redirect" / "project",
            trusted_root=root,
        )

    assert not (outside / "project").exists()


def test_shared_directory_contract_rejects_wrong_existing_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    target = root / "project"
    root.mkdir()
    target.mkdir()
    target.chmod(0o2770)
    original_lstat = Path.lstat

    def shifted_group(path: Path) -> os.stat_result:
        status = original_lstat(path)
        if path == target:
            values = list(status)
            values[5] = status.st_gid + 1
            return os.stat_result(values)
        return status

    monkeypatch.setattr(Path, "lstat", shifted_group)
    monkeypatch.setenv(ATOMIC_PUBLICATION_MODE_ENV, "0640")

    with pytest.raises(
        ValueError,
        match="atomic_publication_directory_group_invalid",
    ):
        ensure_directory(target, trusted_root=root)


def test_private_directory_creation_does_not_force_shared_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ATOMIC_PUBLICATION_MODE_ENV, raising=False)
    target = tmp_path / "project" / "compute"

    ensure_directory(target, require_shared_mode=True)

    assert stat.S_IMODE(target.stat().st_mode) != 0o2770


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

    assert durability_calls == ["file", "file", "directory"]
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


@pytest.mark.parametrize(
    "publish",
    [
        lambda path: write_text_atomic(path, "complete\n"),
        lambda path: write_json_atomic(path, {"complete": True}),
        lambda path: write_json_atomic_create_or_verify(path, {"complete": True}),
        lambda path: write_jsonl_atomic(path, ({"complete": True},)),
    ],
)
def test_atomic_publication_is_private_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publish,
) -> None:
    monkeypatch.delenv(ATOMIC_PUBLICATION_MODE_ENV, raising=False)
    target = tmp_path / "result"

    publish(target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "publish",
    [
        lambda path: write_text_atomic(path, "complete\n"),
        lambda path: write_json_atomic(path, {"complete": True}),
        lambda path: write_json_atomic_create_or_verify(path, {"complete": True}),
        lambda path: write_jsonl_atomic(path, ({"complete": True},)),
    ],
)
def test_qualified_shared_atomic_publication_is_exactly_group_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publish,
) -> None:
    monkeypatch.setenv(ATOMIC_PUBLICATION_MODE_ENV, "0640")
    target = tmp_path / "result"

    publish(target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


@pytest.mark.parametrize("configured", ["", "640", "0644", "0660", "0777"])
def test_atomic_publication_rejects_unapproved_environment_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
) -> None:
    monkeypatch.setenv(ATOMIC_PUBLICATION_MODE_ENV, configured)
    target = tmp_path / "result"

    with pytest.raises(ValueError, match="atomic_publication_mode_invalid"):
        write_text_atomic(target, "must-not-publish\n")

    assert not target.exists()


def test_create_or_verify_does_not_remode_an_existing_immutable_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "result.json"
    payload = {"complete": True}
    monkeypatch.delenv(ATOMIC_PUBLICATION_MODE_ENV, raising=False)
    assert write_json_atomic_create_or_verify(target, payload) is True
    assert stat.S_IMODE(target.stat().st_mode) == 0o600

    monkeypatch.setenv(ATOMIC_PUBLICATION_MODE_ENV, "0640")
    assert write_json_atomic_create_or_verify(target, payload) is False
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_atomic_publication_rejects_unapproved_explicit_mode(tmp_path: Path) -> None:
    target = tmp_path / "result"

    with pytest.raises(ValueError, match="atomic_publication_mode_invalid"):
        write_text_atomic(target, "must-not-publish\n", mode=0o644)

    assert not target.exists()
