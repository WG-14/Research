from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from django.core.exceptions import ValidationError


SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
SENSITIVE_KEY_FRAGMENTS = (
    "secret",
    "password",
    "token",
    "cookie",
    "authorization",
    "api_key",
    "apikey",
)

RESEARCH_VIEWER_GROUP = "research_viewer"
RESEARCH_RUNNER_GROUP = "research_runner"
RESEARCH_REVIEWER_GROUP = "research_reviewer"
RESEARCH_APPROVER_GROUP = "research_approver"
RESEARCH_ADMIN_GROUP = "research_admin"
RBAC_GROUPS = (
    RESEARCH_VIEWER_GROUP,
    RESEARCH_RUNNER_GROUP,
    RESEARCH_REVIEWER_GROUP,
    RESEARCH_APPROVER_GROUP,
    RESEARCH_ADMIN_GROUP,
)

_APPLICATION_PERMISSION_MAP = {
    "portal.view_researchjob": {"research.view"},
    "portal.view_manifestupload": {"research.view"},
    "portal.submit_research_job": {"research.execute", "research.view"},
    "portal.view_all_research_jobs": {"research.view"},
    "portal.view_all_research_manifests": {"research.view"},
    "portal.record_research_review": {"research.review.record", "research.view"},
    "portal.approve_research_candidate": {"research.approve", "research.view"},
    "portal.manage_research_web": {
        "research.audit.verify",
        "research.governance.transition",
        "research.registry.validate",
        "research.reproduce",
        "research.view",
    },
}


def validate_sha256(value: str, *, field: str = "hash") -> str:
    normalized = str(value or "").strip()
    if not SHA256_PATTERN.fullmatch(normalized):
        raise ValidationError(f"{field}_must_be_sha256")
    return normalized


def normalize_display_filename(value: str) -> str:
    name = str(value or "").strip()
    if not name or len(name) > 255:
        raise ValidationError("upload_filename_invalid")
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise ValidationError("upload_filename_must_not_contain_path")
    if any(ord(character) < 32 for character in name):
        raise ValidationError("upload_filename_contains_control_character")
    return name


def validate_relative_artifact_path(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or "\\" in raw or "\x00" in raw:
        raise ValidationError("artifact_ref_path_invalid")
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise ValidationError("artifact_ref_path_must_be_relative")
    if any(part in {"", ".", ".."} for part in posix.parts):
        raise ValidationError("artifact_ref_path_traversal")
    return posix.as_posix()


def ensure_path_within_root(path: Path, root: Path) -> Path:
    resolved_root = root.expanduser().resolve()
    resolved = path.expanduser().resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValidationError("path_outside_configured_root") from exc
    return resolved


def reject_symlink_components(path: Path) -> None:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise ValidationError("path_must_be_absolute")
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValidationError("path_symlink_component_rejected")


def validate_manifest_reference_paths(
    payload: dict[str, Any], *, data_root: Path
) -> None:
    """Confine every manifest-controlled local locator to the data root."""

    def walk(value: Any, *, parent_key: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized_key = str(key)
                path_authority = (
                    normalized_key == "artifact_manifest_uri"
                    or (normalized_key == "path" and parent_key == "locator")
                    or (normalized_key == "uri" and parent_key == "artifact")
                    or normalized_key == "source_uri"
                )
                if path_authority and isinstance(item, str):
                    candidate = Path(item).expanduser()
                    if normalized_key == "source_uri" and not candidate.is_absolute():
                        walk(item, parent_key=normalized_key)
                        continue
                    if not candidate.is_absolute():
                        raise ValidationError(
                            "manifest_local_reference_must_be_absolute"
                        )
                    ensure_path_within_root(candidate, data_root)
                    reject_symlink_components(candidate)
                walk(item, parent_key=normalized_key)
        elif isinstance(value, list):
            for item in value:
                walk(item, parent_key=parent_key)

    walk(payload)


def reject_paths_in_job_payload(value: Any) -> None:
    """Job payloads may contain opaque refs, hashes and ids, never server paths."""

    if isinstance(value, dict):
        for key, item in value.items():
            if any(
                fragment in str(key).lower() for fragment in SENSITIVE_KEY_FRAGMENTS
            ):
                raise ValidationError("job_payload_contains_sensitive_key")
            reject_paths_in_job_payload(item)
        return
    if isinstance(value, list):
        for item in value:
            reject_paths_in_job_payload(item)
        return
    if isinstance(value, str):
        text = value.strip()
        if Path(text).is_absolute() or PureWindowsPath(text).is_absolute():
            raise ValidationError("job_payload_contains_absolute_path")


def sanitize_audit_details(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key)
            if any(
                fragment in normalized.lower() for fragment in SENSITIVE_KEY_FRAGMENTS
            ):
                sanitized[normalized] = "<redacted>"
            else:
                sanitized[normalized] = sanitize_audit_details(item)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [sanitize_audit_details(item) for item in value]
    if isinstance(value, Path):
        return "<redacted-path>"
    if isinstance(value, str):
        stripped = value.strip()
        if (
            Path(stripped).is_absolute()
            or PureWindowsPath(stripped).is_absolute()
            or stripped.lower().startswith(("file:", "sqlite:", "duckdb:"))
        ):
            return "<redacted-path>"
        return value[:2048]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2048]


def actor_snapshot(user: Any) -> tuple[str, list[str], list[str]]:
    if not getattr(user, "is_authenticated", False):
        raise ValidationError("authenticated_actor_required")
    roles = sorted(user.groups.values_list("name", flat=True))
    permissions = {str(item) for item in user.get_all_permissions()}
    for (
        django_permission,
        application_permissions,
    ) in _APPLICATION_PERMISSION_MAP.items():
        if django_permission in permissions:
            permissions.update(application_permissions)
    return str(user.pk), roles, sorted(permissions)


def can_view_all_jobs(user: Any) -> bool:
    return bool(
        getattr(user, "is_authenticated", False)
        and user.has_perm("portal.view_all_research_jobs")
    )
