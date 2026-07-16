from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.core.exceptions import ValidationError

from portal.security import (
    normalize_display_filename,
    reject_paths_in_job_payload,
    sanitize_audit_details,
    validate_manifest_reference_paths,
)
from portal.execution import (
    ResearchJobDispatcher,
    _safe_application_result_projection,
)
from portal.presenters import redact_server_topology
from portal.storage import (
    SafeArtifactRef,
    make_artifact_ref,
    publish_manifest_bytes,
    read_verified_manifest_bytes,
    resolve_artifact_ref,
    verify_result_artifact,
)
from portal.worker import PublicJobError


def test_safe_artifact_refs_reject_absolute_and_traversal_paths() -> None:
    for value in ("report:/etc/passwd", "report:../secret.json", "C:/secret.json"):
        with pytest.raises(ValidationError):
            SafeArtifactRef.parse(value)


def test_safe_artifact_resolution_rejects_symlink(tmp_path: Path) -> None:
    root = settings.RESEARCH_PATHS.report_root
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = root / "_internal_web" / "unsafe-link.json"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    link.symlink_to(outside)
    try:
        with pytest.raises(ValidationError):
            resolve_artifact_ref("report:_internal_web/unsafe-link.json")
    finally:
        link.unlink(missing_ok=True)


def test_content_addressed_manifest_publication_is_idempotent() -> None:
    content = b'{"schema_version":1}'
    content_hash = "sha256:" + hashlib.sha256(content).hexdigest()

    first = publish_manifest_bytes(content=content, content_hash=content_hash)
    second = publish_manifest_bytes(content=content, content_hash=content_hash)

    assert first == second
    assert resolve_artifact_ref(first).read_bytes() == content


def test_manifest_reads_are_bounded_and_verify_recorded_metadata(monkeypatch) -> None:
    monkeypatch.setattr(settings, "INTERNAL_WEB_MAX_MANIFEST_BYTES", 64)
    path = (
        settings.RESEARCH_PATHS.data_root
        / "_internal_web"
        / "manifest-reader-tests"
        / f"{uuid.uuid4().hex}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    content = b"{}"
    path.write_bytes(content)
    record = SimpleNamespace(
        storage_ref=str(make_artifact_ref("data", path)),
        content_hash="sha256:" + hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )
    try:
        assert read_verified_manifest_bytes(record) == content

        path.write_bytes(b"[]")
        with pytest.raises(ValidationError, match="manifest_content_hash_mismatch"):
            read_verified_manifest_bytes(record)

        path.write_bytes(b"x" * 65)
        with pytest.raises(
            ValidationError,
            match="manifest_content_too_large_to_verify",
        ):
            read_verified_manifest_bytes(record)
    finally:
        path.unlink(missing_ok=True)


def test_preflight_error_projection_drops_embedded_paths_and_raw_details() -> None:
    private_path = "/srv/private/candles.sqlite"
    projected = _safe_application_result_projection(
        {
            "status": "FAILED",
            "errors": [
                {
                    "code": "research_io_error",
                    "message": f"unable to open input at '{private_path}'",
                    "details": {"exception_type": "FileNotFoundError"},
                    "retryable": True,
                }
            ],
        }
    )

    assert projected["errors"] == [
        {
            "code": "RESEARCH_INPUT_UNAVAILABLE",
            "message": "A required research input is unavailable.",
        }
    ]
    assert private_path not in json.dumps(projected)


def test_result_verification_reads_only_the_configured_maximum(monkeypatch) -> None:
    monkeypatch.setattr(settings, "INTERNAL_WEB_MAX_RESULT_BYTES", 8)
    path = (
        settings.RESEARCH_PATHS.report_root
        / "_internal_web"
        / "bounded-result-tests"
        / f"{uuid.uuid4().hex}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"oversized":true}')
    try:
        with pytest.raises(
            ValidationError,
            match="result_artifact_too_large_to_verify",
        ):
            verify_result_artifact(
                make_artifact_ref("report", path),
                expected_hash=f"sha256:{'a' * 64}",
            )
    finally:
        path.unlink(missing_ok=True)


def test_dispatch_rechecks_raw_manifest_admission_after_hash_verification(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "INTERNAL_WEB_MAX_PARAMETER_CANDIDATES", 2)
    payload = {
        "parameter_space": {"p": [1, 2, 3]},
        "cost_model": {"slippage_bps": [1]},
    }
    content = json.dumps(payload).encode("utf-8")
    path = (
        settings.RESEARCH_PATHS.data_root
        / "_internal_web"
        / "manifest-admission-tests"
        / f"{uuid.uuid4().hex}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    manifest = SimpleNamespace(
        storage_ref=str(make_artifact_ref("data", path)),
        content_hash="sha256:" + hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )
    try:
        with pytest.raises(PublicJobError) as caught:
            ResearchJobDispatcher._verified_manifest_path(
                SimpleNamespace(manifest=manifest)
            )
        assert caught.value.error_code == "RESEARCH_REQUEST_INVALID"
    finally:
        path.unlink(missing_ok=True)


def test_untrusted_names_paths_and_sensitive_options_are_rejected() -> None:
    with pytest.raises(ValidationError):
        normalize_display_filename("../../manifest.json")
    with pytest.raises(ValidationError):
        reject_paths_in_job_payload({"output": "/tmp/result.json"})
    with pytest.raises(ValidationError):
        reject_paths_in_job_payload({"api_token": "secret"})
    with pytest.raises(ValidationError):
        validate_manifest_reference_paths(
            {"dataset": {"locator": {"path": "/etc/passwd"}}},
            data_root=settings.RESEARCH_PATHS.data_root,
        )


def test_audit_projection_redacts_server_paths_and_secrets() -> None:
    assert sanitize_audit_details(
        {
            "password": "never-log",
            "path": "/srv/private/result.json",
            "locator": "sqlite:///srv/private/candles.sqlite",
        }
    ) == {
        "password": "<redacted>",
        "path": "<redacted-path>",
        "locator": "<redacted-path>",
    }
    assert redact_server_topology(
        {"locator": "file:///srv/private/result.json"}
    ) == {"locator": "<server-managed>"}
