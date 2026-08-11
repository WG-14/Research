from __future__ import annotations

import json
import os
import secrets
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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


def _manager_and_key(tmp_path: Path) -> tuple[ResearchPathManager, bytes, Path]:
    research_root = tmp_path / "research-state"
    trust_root = tmp_path / "identity-trust"
    trust_root.mkdir(parents=True)
    key = secrets.token_bytes(32)
    key_path = trust_root / "verifier.key"
    key_path.write_bytes(key)
    key_path.chmod(0o600)
    trust_path = trust_root / "trust-store.json"
    trust_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "keys": [
                    {
                        "issuer": "identity-issuer",
                        "key_id": "key-1",
                        "algorithm": "hmac-sha256",
                        "key_path": str(key_path.resolve()),
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
        ),
        project_root=Path.cwd(),
    )
    return manager, key, key_path


def _assertion(
    *,
    key: bytes,
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
        key=key,
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
    signature_tampered["signature"] = "hmac-sha256:" + "0" * 64
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

    wrong_key = _assertion(key=secrets.token_bytes(32))
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


def test_assertion_and_secret_key_paths_stay_outside_research_artifacts(
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
    assert key.hex() not in serialized
    assert base64.b64encode(key).decode("ascii") not in serialized
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
def test_principal_assertion_rejects_group_or_world_readable_key(
    tmp_path: Path,
) -> None:
    manager, key, key_path = _manager_and_key(tmp_path)
    assertion = _assertion(key=key)
    key_path.chmod(0o640)

    with pytest.raises(PrincipalAssertionError, match="permissions_too_open"):
        verify_principal_assertion(
            assertion=assertion,
            expected_scope=_scope(),
            trust_store_path=manager.settings.independent_verifier_trust_store_path,
            manager=manager,
        )
