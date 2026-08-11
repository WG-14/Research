from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "deploy" / "native" / "bin" / "backup_evidence.py"


def _load_backup_evidence() -> ModuleType:
    spec = importlib.util.spec_from_file_location("native_backup_evidence", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _status(*, uid: int = 100, gid: int = 200, mode: int = 0o640) -> object:
    return SimpleNamespace(
        st_uid=uid,
        st_gid=gid,
        st_mode=stat.S_IFREG | mode,
    )


def _receipt_payload() -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "status": "VERIFIED",
            "backup_id": "11111111-2222-4333-8444-555555555555",
            "target_id": "approved-vault",
            "encrypted": True,
            "encryption": "kms-envelope",
            "encryption_key_id": "kms-key-version-7",
            "manifest_hash": "sha256:" + "a" * 64,
            "remote_object_digest": "sha256:" + "b" * 64,
            "remote_object_version": "immutable-version-1",
            "uploaded_at": "2026-08-01T00:00:00Z",
            "receipt_signature": "base64:c2lnbmF0dXJl",
        },
        sort_keys=True,
    ).encode("ascii")


def test_cross_principal_reader_accepts_exact_owner_group_and_0640() -> None:
    module = _load_backup_evidence()

    module.validate_offsite_receipt_access(
        _status(),
        expected_owner_uid=100,
        expected_group_gid=200,
        reader_uid=300,
        reader_group_ids=frozenset({200}),
    )


@pytest.mark.parametrize(
    ("uid", "gid", "mode", "reader_uid", "reader_groups"),
    [
        (101, 200, 0o640, 300, frozenset({200})),
        (100, 201, 0o640, 300, frozenset({200})),
        (100, 200, 0o600, 300, frozenset({200})),
        (100, 200, 0o400, 300, frozenset({200})),
        (100, 200, 0o620, 300, frozenset({200})),
        (100, 200, 0o660, 300, frozenset({200})),
        (100, 200, 0o644, 300, frozenset({200})),
        (100, 200, 0o640, 300, frozenset({201})),
    ],
)
def test_cross_principal_reader_rejects_identity_mode_and_membership_drift(
    uid: int,
    gid: int,
    mode: int,
    reader_uid: int,
    reader_groups: frozenset[int],
) -> None:
    module = _load_backup_evidence()

    with pytest.raises(module.EvidenceError, match="receipt_file"):
        module.validate_offsite_receipt_access(
            _status(uid=uid, gid=gid, mode=mode),
            expected_owner_uid=100,
            expected_group_gid=200,
            reader_uid=reader_uid,
            reader_group_ids=reader_groups,
        )


def test_owner_private_receipt_is_local_only_and_shared_contract_requires_0640() -> (
    None
):
    module = _load_backup_evidence()
    private_status = _status(mode=0o600)

    module.validate_offsite_receipt_access(
        private_status,
        expected_owner_uid=100,
        expected_group_gid=200,
        reader_uid=100,
        reader_group_ids=frozenset({200}),
    )
    with pytest.raises(module.EvidenceError, match="receipt_file"):
        module.validate_offsite_receipt_access(
            private_status,
            expected_owner_uid=100,
            expected_group_gid=200,
            reader_uid=100,
            reader_group_ids=frozenset({200}),
            require_group_readable=True,
        )


def test_read_offsite_receipt_uses_explicit_cross_principal_trust_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_backup_evidence()
    monkeypatch.setattr(
        module,
        "_read_file_with_status",
        lambda *_args, **_kwargs: (_receipt_payload(), _status()),
    )
    monkeypatch.setattr(module.os, "geteuid", lambda: 300)
    monkeypatch.setattr(module.os, "getegid", lambda: 201)
    monkeypatch.setattr(module.os, "getgroups", lambda: [200])

    receipt = module.read_offsite_receipt(
        Path("/srv/research-offsite-receipts/receipt.json"),
        expected_owner_uid=100,
        expected_group_gid=200,
    )

    assert receipt["status"] == "VERIFIED"


def test_read_offsite_receipt_rejects_wrong_explicit_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_backup_evidence()
    monkeypatch.setattr(
        module,
        "_read_file_with_status",
        lambda *_args, **_kwargs: (_receipt_payload(), _status()),
    )
    monkeypatch.setattr(module.os, "geteuid", lambda: 300)
    monkeypatch.setattr(module.os, "getegid", lambda: 201)
    monkeypatch.setattr(module.os, "getgroups", lambda: [200])

    with pytest.raises(module.EvidenceError, match="receipt_file"):
        module.read_offsite_receipt(
            Path("/srv/research-offsite-receipts/receipt.json"),
            expected_owner_uid=999,
            expected_group_gid=200,
        )
