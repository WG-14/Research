from __future__ import annotations

import hashlib
import importlib.metadata
import subprocess
from pathlib import Path
from typing import Any

from .hashing import sha256_prefixed


CODE_PROVENANCE_SCHEMA_VERSION = 3
_ROOT_FILES = ("pyproject.toml", "uv.lock")
REPOSITORY_DEPENDENCY_CONTRACT_BASIS = (
    "repository_contract_files_and_resolved_installed_distributions"
)
INSTALLED_DEPENDENCY_CONTRACT_BASIS = "resolved_installed_distributions"
RESOLVED_DEPENDENCY_CONTENT_IDENTITY_BASIS = (
    "normalized_record_and_installed_file_hashes_v1"
)
_IGNORED_INSTALLED_FILE_NAMES = frozenset(
    {"INSTALLER", "RECORD", "REQUESTED", "direct_url.json", "uv_cache.json"}
)


class CodeProvenanceError(RuntimeError):
    """Installed code or dependency content cannot be fingerprinted safely."""


def collect_code_provenance(project_root: str | Path) -> dict[str, Any]:
    """Fingerprint executable code, version control, and dependencies.

    A source checkout binds its ``src`` tree and checked-in dependency contract.
    An installed distribution has neither of those paths, so it binds the
    imported package bytes and resolved distribution content identities
    instead. A missing checkout must never collapse to the hash of an empty
    file list.
    """
    root = Path(project_root).resolve()
    repository_source_root = root / "src"
    if repository_source_root.is_dir():
        source_layout = "repository_src"
        source_base = root
        source_candidates = repository_source_root.rglob("*.py")
    else:
        source_layout = "installed_distribution"
        package_root = Path(__file__).resolve().parents[1]
        source_base = package_root.parent
        source_candidates = package_root.rglob("*.py")
    source_files = sorted(
        [path for path in source_candidates if path.is_file()],
        key=lambda path: path.relative_to(source_base).as_posix(),
    )
    source_rows = [_file_evidence(source_base, path) for path in source_files]
    lock_rows = [
        _file_evidence(root, root / name)
        for name in _ROOT_FILES
        if (root / name).is_file()
    ]
    resolved_distribution_rows = _installed_distribution_rows()
    declared_dependency_contract_hash = (
        sha256_prefixed(lock_rows, label="declared_dependency_contract")
        if lock_rows
        else None
    )
    resolved_dependency_contract_hash = sha256_prefixed(
        resolved_distribution_rows,
        label="resolved_dependency_contract",
    )
    if lock_rows:
        dependency_basis = REPOSITORY_DEPENDENCY_CONTRACT_BASIS
        dependency_contract_files = [str(row["path"]) for row in lock_rows]
    else:
        dependency_basis = INSTALLED_DEPENDENCY_CONTRACT_BASIS
        dependency_contract_files = [
            str(row["name"]) for row in resolved_distribution_rows
        ]
    dependency_contract_hash = combined_dependency_contract_hash(
        basis=dependency_basis,
        declared_dependency_contract_hash=declared_dependency_contract_hash,
        resolved_dependency_contract_hash=resolved_dependency_contract_hash,
    )
    git_root_present = (root / ".git").exists()
    commit = _git_text(root, "rev-parse", "HEAD") if git_root_present else None
    status = (
        _git_text(root, "status", "--porcelain=v1", "--untracked-files=all")
        if git_root_present
        else None
    )
    diff = (
        _git_bytes(root, "diff", "--binary", "HEAD", "--") if git_root_present else None
    )
    payload: dict[str, Any] = {
        "schema_version": CODE_PROVENANCE_SCHEMA_VERSION,
        "source_layout": source_layout,
        "git_commit": commit or "unknown",
        "git_available": commit is not None and status is not None and diff is not None,
        "git_dirty": bool(status) if status is not None else None,
        "git_status_hash": _bytes_hash(status.encode("utf-8"))
        if status is not None
        else None,
        "git_diff_hash": _bytes_hash(diff) if diff is not None else None,
        "source_tree_hash": sha256_prefixed(
            source_rows, label="repository_source_tree"
        ),
        "source_file_count": len(source_rows),
        "declared_dependency_contract_hash": declared_dependency_contract_hash,
        "resolved_dependency_contract_hash": resolved_dependency_contract_hash,
        "resolved_dependency_content_identity_basis": (
            RESOLVED_DEPENDENCY_CONTENT_IDENTITY_BASIS
        ),
        "dependency_contract_hash": dependency_contract_hash,
        "dependency_contract_basis": dependency_basis,
        "dependency_contract_files": dependency_contract_files,
        "resolved_dependency_distribution_identities": [
            dict(row) for row in resolved_distribution_rows
        ],
        "resolved_dependency_distributions": [
            row["name"] for row in resolved_distribution_rows
        ],
    }
    payload["code_provenance_hash"] = sha256_prefixed(payload, label="code_provenance")
    return payload


def combined_dependency_contract_hash(
    *,
    basis: str,
    declared_dependency_contract_hash: str | None,
    resolved_dependency_contract_hash: str,
) -> str:
    """Bind declared dependency intent to the environment that resolved it."""

    return sha256_prefixed(
        {
            "basis": basis,
            "declared_dependency_contract_hash": declared_dependency_contract_hash,
            "resolved_dependency_contract_hash": resolved_dependency_contract_hash,
        },
        label="combined_dependency_contract",
    )


def _file_evidence(root: Path, path: Path) -> dict[str, str]:
    return {
        "path": path.relative_to(root).as_posix(),
        "content_hash": _bytes_hash(path.read_bytes()),
    }


def _installed_distribution_rows() -> list[dict[str, object]]:
    identities: dict[tuple[str, str], set[tuple[str, int]]] = {}
    distributions_seen: set[tuple[str, str]] = set()
    content_cache: dict[Path, tuple[str, int]] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if not isinstance(name, str) or not name.strip():
            continue
        key = (name.strip().lower().replace("_", "-"), str(distribution.version))
        distributions_seen.add(key)
        identity = _installed_distribution_content_identity(
            distribution,
            content_cache=content_cache,
        )
        if identity is not None:
            identities.setdefault(key, set()).add(identity)
    missing = sorted(distributions_seen - set(identities))
    if missing:
        raise CodeProvenanceError(
            "installed_distribution_content_identity_empty:"
            + ",".join(f"{name}=={version}" for name, version in missing)
        )
    rows = [
        {
            "name": name,
            "version": version,
            "content_hash": content_hash,
            "file_count": file_count,
        }
        for (name, version), values in identities.items()
        for content_hash, file_count in values
    ]
    return sorted(
        rows,
        key=lambda row: (
            str(row["name"]),
            str(row["version"]),
            str(row["content_hash"]),
        ),
    )


def _installed_distribution_content_identity(
    distribution: importlib.metadata.Distribution,
    *,
    content_cache: dict[Path, tuple[str, int]],
) -> tuple[str, int] | None:
    files = distribution.files
    if files is None:
        raise CodeProvenanceError("installed_distribution_file_manifest_missing")
    package_paths = tuple(files)
    file_rows = _editable_distribution_source_rows(
        distribution,
        package_paths=package_paths,
        content_cache=content_cache,
    )
    for package_path in sorted(package_paths, key=lambda value: value.as_posix()):
        logical_path = package_path.as_posix()
        if _ignore_installed_distribution_file(logical_path):
            continue
        installed_path = Path(str(distribution.locate_file(package_path)))
        if not installed_path.is_file():
            raise CodeProvenanceError(
                f"installed_distribution_file_missing:{logical_path}"
            )
        cached = _installed_file_hash(
            installed_path,
            logical_path=logical_path,
            content_cache=content_cache,
        )
        recorded = package_path.hash
        file_rows.append(
            {
                "identity_role": "installed_file",
                "path": logical_path,
                "content_hash": cached[0],
                "size_bytes": cached[1],
                "recorded_hash": (
                    f"{recorded.mode}:{recorded.value}" if recorded else None
                ),
                "recorded_size_bytes": package_path.size,
                "is_symlink": installed_path.is_symlink(),
            }
        )
    if not file_rows:
        # Editable installs can leave duplicate legacy egg-info distributions
        # on sys.path. Their canonical dist-info sibling carries the actual
        # pointer, metadata, and source identity, so the empty duplicate is
        # ignored and the name/version group is checked after collection.
        return None
    return (
        sha256_prefixed(
            file_rows,
            label="installed_distribution_content_identity",
        ),
        len(file_rows),
    )


def _ignore_installed_distribution_file(logical_path: str) -> bool:
    path = Path(logical_path)
    return (
        logical_path.startswith("../")
        or "/__pycache__/" in f"/{logical_path}"
        or path.suffix in {".pyc", ".pyo"}
        or path.name in _IGNORED_INSTALLED_FILE_NAMES
        or (path.suffix == ".pth" and "editable" in path.name)
    )


def _editable_distribution_source_rows(
    distribution: importlib.metadata.Distribution,
    *,
    package_paths: tuple[importlib.metadata.PackagePath, ...],
    content_cache: dict[Path, tuple[str, int]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    editable_paths = [
        package_path
        for package_path in package_paths
        if package_path.suffix == ".pth" and "editable" in package_path.name
    ]
    for pointer_index, package_path in enumerate(
        sorted(editable_paths, key=lambda value: value.as_posix())
    ):
        pointer_path = Path(str(distribution.locate_file(package_path)))
        try:
            pointer_lines = pointer_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise CodeProvenanceError(
                f"editable_distribution_pointer_unreadable:{package_path.as_posix()}"
            ) from exc
        source_roots = [
            Path(line.strip())
            for line in pointer_lines
            if line.strip()
            and not line.lstrip().startswith("#")
            and not line.lstrip().startswith("import ")
        ]
        if not source_roots or any(
            not source_root.is_absolute() or not source_root.is_dir()
            for source_root in source_roots
        ):
            raise CodeProvenanceError(
                f"editable_distribution_pointer_unsupported:{package_path.as_posix()}"
            )
        rows.append(
            {
                "identity_role": "editable_pointer",
                "path": package_path.name,
                "pointer_index": pointer_index,
                "pointer_kind": "absolute_source_directory",
                "source_root_count": len(source_roots),
            }
        )
        for source_index, source_root in enumerate(source_roots):
            resolved_source_root = source_root.resolve(strict=True)
            source_files = sorted(
                (
                    path
                    for path in resolved_source_root.rglob("*")
                    if _include_editable_source_file(path, resolved_source_root)
                ),
                key=lambda path: path.relative_to(resolved_source_root).as_posix(),
            )
            if not source_files:
                raise CodeProvenanceError(
                    f"editable_distribution_source_empty:{package_path.as_posix()}"
                )
            for source_path in source_files:
                relative_path = source_path.relative_to(resolved_source_root).as_posix()
                cached = _installed_file_hash(
                    source_path,
                    logical_path=relative_path,
                    content_cache=content_cache,
                )
                rows.append(
                    {
                        "identity_role": "editable_source_file",
                        "path": f"editable_source_{source_index}/{relative_path}",
                        "content_hash": cached[0],
                        "size_bytes": cached[1],
                        "is_symlink": False,
                    }
                )
    return rows


def _include_editable_source_file(path: Path, source_root: Path) -> bool:
    relative = path.relative_to(source_root)
    if path.is_symlink():
        raise CodeProvenanceError(
            f"editable_distribution_source_symlink_rejected:{relative.as_posix()}"
        )
    return (
        path.is_file()
        and "__pycache__" not in relative.parts
        and not any(
            part.endswith((".egg-info", ".dist-info")) for part in relative.parts
        )
        and path.suffix not in {".pyc", ".pyo"}
    )


def _installed_file_hash(
    path: Path,
    *,
    logical_path: str,
    content_cache: dict[Path, tuple[str, int]],
) -> tuple[str, int]:
    try:
        resolved_path = path.resolve(strict=True)
    except OSError as exc:
        raise CodeProvenanceError(
            f"installed_distribution_file_unreadable:{logical_path}"
        ) from exc
    cached = content_cache.get(resolved_path)
    if cached is None:
        try:
            content = resolved_path.read_bytes()
        except OSError as exc:
            raise CodeProvenanceError(
                f"installed_distribution_file_unreadable:{logical_path}"
            ) from exc
        cached = (_bytes_hash(content), len(content))
        content_cache[resolved_path] = cached
    return cached


def _git_text(root: Path, *args: str) -> str | None:
    raw = _git_bytes(root, *args)
    return raw.decode("utf-8", errors="strict").strip() if raw is not None else None


def _git_bytes(root: Path, *args: str) -> bytes | None:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=root,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None


def _bytes_hash(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()
