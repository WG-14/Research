from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .approved_profile import compute_file_content_hash
from .storage_io import write_json_atomic


BUNDLE_MANIFEST_NAME = "evidence_bundle.json"


class EvidenceBundleError(ValueError):
    pass


def _safe_relative_path(value: str) -> Path:
    rel = Path(str(value))
    if rel.is_absolute() or ".." in rel.parts:
        raise EvidenceBundleError("evidence_bundle_path_escape")
    return rel


def create_evidence_bundle(*, bundle_root: str | Path, artifacts: dict[str, str | Path]) -> dict[str, Any]:
    root = Path(bundle_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, str]] = []
    for role, source in sorted(artifacts.items()):
        src = Path(source).expanduser().resolve()
        rel = Path("artifacts") / role / src.name
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        entries.append(
            {
                "role": str(role),
                "path": rel.as_posix(),
                "content_hash": compute_file_content_hash(dst),
            }
        )
    manifest = {
        "schema_version": 1,
        "artifact_type": "portable_evidence_bundle",
        "artifacts": entries,
    }
    write_json_atomic(root / BUNDLE_MANIFEST_NAME, manifest)
    return manifest


def verify_evidence_bundle(bundle_root: str | Path) -> dict[str, Any]:
    root = Path(bundle_root).expanduser().resolve()
    manifest_path = root / BUNDLE_MANIFEST_NAME
    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict):
        raise EvidenceBundleError("evidence_bundle_manifest_not_object")
    verified: list[dict[str, str]] = []
    for item in manifest.get("artifacts") or []:
        if not isinstance(item, dict):
            raise EvidenceBundleError("evidence_bundle_artifact_not_object")
        rel = _safe_relative_path(str(item.get("path") or ""))
        path = (root / rel).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise EvidenceBundleError("evidence_bundle_path_escape") from exc
        expected = str(item.get("content_hash") or "")
        if not expected.startswith("sha256:"):
            raise EvidenceBundleError("evidence_bundle_missing_artifact_hash")
        if not path.exists():
            raise EvidenceBundleError("evidence_bundle_artifact_missing")
        actual = compute_file_content_hash(path)
        if actual != expected:
            raise EvidenceBundleError("evidence_bundle_artifact_hash_mismatch")
        verified.append({"role": str(item.get("role") or ""), "path": str(path), "content_hash": actual})
    result = dict(manifest)
    result["verified_artifacts"] = verified
    return result


def bundle_artifact_path(bundle_root: str | Path, *, role: str) -> Path:
    manifest = verify_evidence_bundle(bundle_root)
    for item in manifest.get("verified_artifacts") or []:
        if str(item.get("role") or "") == role:
            return Path(str(item["path"]))
    raise EvidenceBundleError(f"evidence_bundle_role_missing:{role}")


def cmd_evidence_bundle_create(*, bundle_root: str, promotion_path: str) -> int:
    manifest = create_evidence_bundle(bundle_root=bundle_root, artifacts={"promotion": promotion_path})
    print(json.dumps(manifest, sort_keys=True, ensure_ascii=False))
    return 0


def cmd_evidence_bundle_verify(*, bundle_root: str) -> int:
    manifest = verify_evidence_bundle(bundle_root)
    print(json.dumps({"ok": True, "artifact_count": len(manifest.get("verified_artifacts") or [])}, sort_keys=True))
    return 0
