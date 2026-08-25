from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from market_research.paths import ResearchPathManager
from market_research.research.principal_assertion import (
    IndependentVerificationAssertionScope,
    PrincipalAssertion,
    PrincipalAssertionError,
    issue_principal_assertion,
    load_principal_assertion,
    verify_principal_assertion,
)
from market_research.settings import ResearchSettings


def _hash(char: str) -> str:
    return "sha256:" + char * 64


def _scope(
    *,
    verification_id: str = "verification-1",
) -> IndependentVerificationAssertionScope:
    return IndependentVerificationAssertionScope(
        verification_id=verification_id,
        verification_version="1",
        experiment_id="experiment-1",
        research_version=_hash("1"),
        source_report_hash=_hash("2"),
        baseline_receipt_hash=_hash("3"),
    )


def _manager_and_key(
    tmp_path: Path,
) -> tuple[ResearchPathManager, Ed25519PrivateKey, Path]:
    research_root = tmp_path / "research-state"
    trust_root = tmp_path / "identity-trust"
    trust_root.mkdir(parents=True)
    key = Ed25519PrivateKey.generate()
    key_path = trust_root / "verifier.ed25519.pub"
    key_path.write_bytes(
        b"ed25519:" + base64.b64encode(key.public_key().public_bytes_raw()) + b"\n"
    )
    key_path.chmod(0o644)
    key_hash = "sha256:" + hashlib.sha256(key_path.read_bytes()).hexdigest()
    current = datetime.now(timezone.utc)
    trust_path = trust_root / "trust-store.json"
    trust_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_type": "independent_verifier_trust_store",
                "authority_id": "identity-issuer",
                "issued_at": (current - timedelta(days=1))
                .isoformat()
                .replace("+00:00", "Z"),
                "expires_at": (current + timedelta(days=30))
                .isoformat()
                .replace("+00:00", "Z"),
                "keys": [
                    {
                        "key_id": "key-1",
                        "algorithm": "ed25519",
                        "public_key_path": str(key_path.resolve()),
                        "public_key_content_hash": key_hash,
                        "valid_from": (current - timedelta(days=2))
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "valid_until": (current + timedelta(days=20))
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "revoked_at": None,
                        "revocation_reason": "",
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    manager = ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=research_root / "data",
            artifact_root=research_root / "artifacts",
            report_root=research_root / "reports",
            cache_root=research_root / "cache",
            db_path=research_root / "input.sqlite",
            max_workers=1,
            random_seed=0,
            independent_verifier_trust_store_path=trust_path.resolve(),
            independent_verifier_trust_store_hash=(
                "sha256:" + hashlib.sha256(trust_path.read_bytes()).hexdigest()
            ),
        ),
        project_root=Path.cwd(),
    )
    return manager, key, key_path


def _assertion(
    *,
    key: Ed25519PrivateKey,
    scope: IndependentVerificationAssertionScope | None = None,
    subject: str = "principal-7",
    roles: tuple[str, ...] = ("independent_verifier",),
    authenticated_at: datetime | None = None,
    expires_at: datetime | None = None,
    nonce: str = "nonce-1",
) -> PrincipalAssertion:
    now = datetime.now(timezone.utc)
    return issue_principal_assertion(
        issuer="identity-issuer",
        key_id="key-1",
        subject=subject,
        roles=roles,
        authenticated_at=(authenticated_at or now - timedelta(seconds=1)).isoformat(),
        expires_at=(expires_at or now + timedelta(minutes=5)).isoformat(),
        nonce=nonce,
        scope=scope or _scope(),
        private_key=key,
    )


def _trust_payload(manager: ResearchPathManager) -> dict[str, object]:
    path = manager.settings.independent_verifier_trust_store_path
    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _replace_trust(
    manager: ResearchPathManager,
    payload: dict[str, object],
) -> ResearchPathManager:
    path = manager.settings.independent_verifier_trust_store_path
    assert path is not None
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return ResearchPathManager.from_settings(
        replace(
            manager.settings,
            independent_verifier_trust_store_hash=(
                "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            ),
        ),
        project_root=manager.project_root,
    )


def test_principal_assertion_authenticates_exact_scope(tmp_path: Path) -> None:
    manager, key, _ = _manager_and_key(tmp_path)
    assertion = _assertion(key=key)

    assert (
        verify_principal_assertion(
            assertion=assertion,
            expected_scope=_scope(),
            trust_store_path=manager.settings.independent_verifier_trust_store_path,
            manager=manager,
        )
        == assertion
    )

    with pytest.raises(PrincipalAssertionError, match="scope_mismatch"):
        verify_principal_assertion(
            assertion=assertion,
            expected_scope=_scope(verification_id="verification-2"),
            trust_store_path=manager.settings.independent_verifier_trust_store_path,
            manager=manager,
        )


def test_principal_assertion_rejects_payload_and_signature_tampering(
    tmp_path: Path,
) -> None:
    manager, key, _ = _manager_and_key(tmp_path)
    assertion = _assertion(key=key)
    tampered_payload = assertion.as_dict()
    tampered_payload["subject"] = "principal-8"
    with pytest.raises(PrincipalAssertionError, match="content_hash_mismatch"):
        PrincipalAssertion.from_dict(tampered_payload)

    signature_tampered = assertion.as_dict()
    signature_tampered["signature"] = "ed25519:" + base64.b64encode(
        b"\0" * 64
    ).decode("ascii")
    parsed = PrincipalAssertion.from_dict(signature_tampered)
    with pytest.raises(PrincipalAssertionError, match="signature_invalid"):
        verify_principal_assertion(
            assertion=parsed,
            expected_scope=_scope(),
            trust_store_path=manager.settings.independent_verifier_trust_store_path,
            manager=manager,
        )


def test_principal_assertion_rejects_expiry_wrong_key_and_missing_role(
    tmp_path: Path,
) -> None:
    manager, key, _ = _manager_and_key(tmp_path)
    now = datetime.now(timezone.utc)
    expired = _assertion(
        key=key,
        authenticated_at=now - timedelta(minutes=2),
        expires_at=now - timedelta(minutes=1),
    )
    with pytest.raises(PrincipalAssertionError, match="expired"):
        verify_principal_assertion(
            assertion=expired,
            expected_scope=_scope(),
            trust_store_path=manager.settings.independent_verifier_trust_store_path,
            manager=manager,
            now=now,
        )

    wrong_key = _assertion(key=Ed25519PrivateKey.generate())
    with pytest.raises(PrincipalAssertionError, match="signature_invalid"):
        verify_principal_assertion(
            assertion=wrong_key,
            expected_scope=_scope(),
            trust_store_path=manager.settings.independent_verifier_trust_store_path,
            manager=manager,
        )

    wrong_role = _assertion(key=key, roles=("research_reviewer",))
    with pytest.raises(PrincipalAssertionError, match="role_required"):
        verify_principal_assertion(
            assertion=wrong_role,
            expected_scope=_scope(),
            trust_store_path=manager.settings.independent_verifier_trust_store_path,
            manager=manager,
        )


def test_assertion_and_public_key_paths_stay_outside_research_artifacts(
    tmp_path: Path,
) -> None:
    manager, key, key_path = _manager_and_key(tmp_path)
    assertion = _assertion(key=key)
    assertion_path = tmp_path / "identity-trust" / "assertion.json"
    assertion_path.write_text(
        json.dumps(assertion.as_dict(), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    serialized = assertion_path.read_text(encoding="utf-8")

    assert load_principal_assertion(assertion_path, manager=manager) == assertion
    assert base64.b64encode(key.private_bytes_raw()).decode("ascii") not in serialized
    assert str(key_path) not in serialized
    assert not any(
        ResearchPathManager.is_within(key_path, root)
        for root in (
            manager.project_root,
            manager.data_root,
            manager.artifact_root,
            manager.report_root,
            manager.cache_root,
        )
    )

    with pytest.raises(PrincipalAssertionError, match="must_be_external"):
        load_principal_assertion(
            manager.project_root / "pyproject.toml",
            manager=manager,
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX key-mode contract")
def test_principal_assertion_rejects_group_or_world_writable_public_key(
    tmp_path: Path,
) -> None:
    manager, key, key_path = _manager_and_key(tmp_path)
    assertion = _assertion(key=key)
    key_path.chmod(0o664)

    with pytest.raises(PrincipalAssertionError, match="permissions_too_open"):
        verify_principal_assertion(
            assertion=assertion,
            expected_scope=_scope(),
            trust_store_path=manager.settings.independent_verifier_trust_store_path,
            manager=manager,
        )


def test_legacy_schema_and_algorithm_downgrades_are_explicitly_rejected(
    tmp_path: Path,
) -> None:
    _manager, key, _key_path = _manager_and_key(tmp_path)
    assertion = _assertion(key=key)
    legacy = assertion.as_dict()
    legacy["schema_version"] = 1
    legacy["algorithm"] = "hmac-sha256"
    legacy["signature"] = "hmac-sha256:" + "0" * 64
    with pytest.raises(PrincipalAssertionError, match="schema_unsupported"):
        PrincipalAssertion.from_dict(legacy)

    downgraded = assertion.as_dict()
    downgraded["algorithm"] = "hmac-sha256"
    with pytest.raises(PrincipalAssertionError, match="algorithm_invalid"):
        PrincipalAssertion.from_dict(downgraded)


def test_trust_store_expiry_future_key_and_revocation_fail_closed(
    tmp_path: Path,
) -> None:
    manager, key, _key_path = _manager_and_key(tmp_path)
    assertion = _assertion(key=key)
    payload = _trust_payload(manager)
    payload["issued_at"] = "2019-01-01T00:00:00Z"
    payload["expires_at"] = "2020-01-02T00:00:00Z"
    expired_manager = _replace_trust(manager, payload)
    with pytest.raises(PrincipalAssertionError, match="trust_store_expired"):
        verify_principal_assertion(
            assertion=assertion,
            expected_scope=_scope(),
            trust_store_path=expired_manager.settings.independent_verifier_trust_store_path,
            manager=expired_manager,
        )

    manager, key, _key_path = _manager_and_key(tmp_path / "future")
    assertion = _assertion(key=key)
    payload = _trust_payload(manager)
    key_entry = payload["keys"][0]
    key_entry["valid_from"] = "2098-01-01T00:00:00Z"
    key_entry["valid_until"] = "2099-01-01T00:00:00Z"
    future_manager = _replace_trust(manager, payload)
    with pytest.raises(PrincipalAssertionError, match="outside_validity"):
        verify_principal_assertion(
            assertion=assertion,
            expected_scope=_scope(),
            trust_store_path=future_manager.settings.independent_verifier_trust_store_path,
            manager=future_manager,
        )

    manager, key, _key_path = _manager_and_key(tmp_path / "revoked")
    assertion = _assertion(key=key)
    payload = _trust_payload(manager)
    key_entry = payload["keys"][0]
    key_entry["revoked_at"] = (
        (datetime.now(timezone.utc) - timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z")
    )
    key_entry["revocation_reason"] = "issuer compromise"
    revoked_manager = _replace_trust(manager, payload)
    with pytest.raises(PrincipalAssertionError, match="key_revoked"):
        verify_principal_assertion(
            assertion=assertion,
            expected_scope=_scope(),
            trust_store_path=revoked_manager.settings.independent_verifier_trust_store_path,
            manager=revoked_manager,
        )


def test_caller_override_key_substitution_and_duplicate_ids_are_rejected(
    tmp_path: Path,
) -> None:
    manager, key, key_path = _manager_and_key(tmp_path)
    assertion = _assertion(key=key)
    with pytest.raises(PrincipalAssertionError, match="caller_override"):
        verify_principal_assertion(
            assertion=assertion,
            expected_scope=_scope(),
            trust_store_path=key_path,
            manager=manager,
        )

    key_path.write_bytes(
        b"ed25519:"
        + base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw())
        + b"\n"
    )
    with pytest.raises(PrincipalAssertionError, match="content_hash_mismatch"):
        verify_principal_assertion(
            assertion=assertion,
            expected_scope=_scope(),
            trust_store_path=manager.settings.independent_verifier_trust_store_path,
            manager=manager,
        )

    manager, key, _key_path = _manager_and_key(tmp_path / "duplicate")
    assertion = _assertion(key=key)
    payload = _trust_payload(manager)
    payload["keys"].append(dict(payload["keys"][0]))
    duplicate_manager = _replace_trust(manager, payload)
    with pytest.raises(PrincipalAssertionError, match="key_id_duplicate"):
        verify_principal_assertion(
            assertion=assertion,
            expected_scope=_scope(),
            trust_store_path=duplicate_manager.settings.independent_verifier_trust_store_path,
            manager=duplicate_manager,
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX link contract")
@pytest.mark.parametrize("target", ("trust_store", "public_key"))
def test_trust_authority_rejects_hardlinks(
    tmp_path: Path,
    target: str,
) -> None:
    manager, key, key_path = _manager_and_key(tmp_path)
    assertion = _assertion(key=key)
    trust_path = manager.settings.independent_verifier_trust_store_path
    assert trust_path is not None
    attacked_path = trust_path if target == "trust_store" else key_path
    os.link(attacked_path, attacked_path.with_suffix(attacked_path.suffix + ".alias"))
    with pytest.raises(PrincipalAssertionError, match="single_link"):
        verify_principal_assertion(
            assertion=assertion,
            expected_scope=_scope(),
            trust_store_path=trust_path,
            manager=manager,
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-follow contract")
def test_trust_authority_rejects_symlinked_public_key(tmp_path: Path) -> None:
    manager, key, key_path = _manager_and_key(tmp_path)
    assertion = _assertion(key=key)
    real_path = key_path.with_name("real-public-key")
    key_path.rename(real_path)
    key_path.symlink_to(real_path)
    with pytest.raises(PrincipalAssertionError, match="unavailable_or_symlinked"):
        verify_principal_assertion(
            assertion=assertion,
            expected_scope=_scope(),
            trust_store_path=manager.settings.independent_verifier_trust_store_path,
            manager=manager,
        )


def test_trust_store_mutation_during_descriptor_read_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from market_research.research import principal_assertion as module

    manager, key, _key_path = _manager_and_key(tmp_path)
    assertion = _assertion(key=key)
    trust_path = manager.settings.independent_verifier_trust_store_path
    assert trust_path is not None
    original_read = module.os.read
    mutated = False

    def racing_read(descriptor: int, count: int) -> bytes:
        nonlocal mutated
        chunk = original_read(descriptor, count)
        if not mutated:
            mutated = True
            with trust_path.open("ab") as stream:
                stream.write(b" ")
                stream.flush()
                os.fsync(stream.fileno())
        return chunk

    monkeypatch.setattr(module.os, "read", racing_read)
    with pytest.raises(PrincipalAssertionError, match="changed_during_verification"):
        verify_principal_assertion(
            assertion=assertion,
            expected_scope=_scope(),
            trust_store_path=trust_path,
            manager=manager,
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX assertion link contract")
@pytest.mark.parametrize("link_kind", ("hardlink", "symlink"))
def test_assertion_loader_rejects_link_aliases(
    tmp_path: Path,
    link_kind: str,
) -> None:
    manager, key, _key_path = _manager_and_key(tmp_path)
    assertion_path = tmp_path / "identity-trust" / "assertion.json"
    assertion_path.write_text(
        json.dumps(_assertion(key=key).as_dict(), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    alias = assertion_path.with_name("assertion-alias.json")
    if link_kind == "hardlink":
        os.link(assertion_path, alias)
        attacked = assertion_path
        expected = "single_link"
    else:
        alias.symlink_to(assertion_path)
        attacked = alias
        expected = "unavailable_or_symlinked"
    with pytest.raises(PrincipalAssertionError, match=expected):
        load_principal_assertion(attacked, manager=manager)
