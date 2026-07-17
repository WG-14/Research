from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "src" / "market_research"
WEB = ROOT / "apps" / "internal_web" / "src"
OPERATIONS = ROOT / "services" / "research_operations" / "src"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def _python_imports(root: Path) -> list[tuple[Path, str]]:
    return [
        (path, imported)
        for path in sorted(root.rglob("*.py"))
        for imported in sorted(_imports(path))
    ]


def test_core_never_depends_on_web_or_operations() -> None:
    forbidden = ("django", "portal", "market_research_web", "research_operations")
    violations = [
        (path.relative_to(ROOT).as_posix(), imported)
        for path, imported in _python_imports(CORE)
        if imported in forbidden
        or imported.startswith(tuple(name + "." for name in forbidden))
    ]

    assert violations == []


def test_web_uses_public_core_application_or_composition_contracts() -> None:
    violations = [
        (path.relative_to(ROOT).as_posix(), imported)
        for path, imported in _python_imports(WEB)
        if imported == "market_research.research"
        or imported.startswith("market_research.research.")
        or imported == "market_research.research_cli"
        or imported.startswith("market_research.research_cli.")
    ]

    assert violations == []


def test_operations_uses_web_and_core_facades_only() -> None:
    violations = [
        (path.relative_to(ROOT).as_posix(), imported)
        for path, imported in _python_imports(OPERATIONS)
        if imported == "portal"
        or imported.startswith("portal.")
        or imported == "market_research.research"
        or imported.startswith("market_research.research.")
        or imported == "market_research.research_cli"
        or imported.startswith("market_research.research_cli.")
        or imported == "market_research.research_composition"
        or imported.startswith("market_research.research_composition.")
    ]

    assert violations == []


def test_operated_capability_issuer_is_imported_only_by_operations() -> None:
    issuer_functions = {"_issue_operated_execution_capability"}
    violations: list[str] = []
    source_roots = (CORE, WEB, OPERATIONS)
    for source_root in source_roots:
        for path in sorted(source_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imports_issuer = any(
                isinstance(node, ast.ImportFrom)
                and any(alias.name in issuer_functions for alias in node.names)
                for node in ast.walk(tree)
            )
            if imports_issuer and not path.is_relative_to(OPERATIONS):
                violations.append(path.relative_to(ROOT).as_posix())

    assert violations == []


def test_core_capability_seam_has_no_operational_credential_ownership() -> None:
    core_seam = (CORE / "application" / "cli_execution.py").read_text(encoding="utf-8")
    operations_issuer = (
        OPERATIONS / "research_operations" / "execution_capability.py"
    ).read_text(encoding="utf-8")
    operational_markers = {
        '"/run/credentials"',
        '"CREDENTIALS_DIRECTORY"',
        '"operated-execution.key"',
        '"research-ops"',
        "_create_operated_execution_proof",
        "_load_systemd_worker_credential",
        "_operated_execution_proof",
        "import pwd",
        "os.open(",
        "stat.S_",
    }

    assert [marker for marker in operational_markers if marker in core_seam] == []
    assert all(marker in operations_issuer for marker in operational_markers)


def test_monorepo_sources_contain_no_tracked_runtime_secret_or_link() -> None:
    forbidden_suffixes = {
        ".backup",
        ".cer",
        ".crt",
        ".db",
        ".dump",
        ".htpasswd",
        ".key",
        ".p12",
        ".pem",
        ".pfx",
        ".sqlite",
        ".sqlite3",
    }
    violations: list[str] = []
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "apps", "services", "src"],
        cwd=ROOT,
    ).split(b"\0")
    for raw_path in tracked:
        if not raw_path:
            continue
        relative = Path(raw_path.decode("utf-8"))
        path = ROOT / relative
        if path.is_symlink():
            violations.append(relative.as_posix() + ":symlink")
        if not path.is_file() or path.name.endswith(".example"):
            continue
        if path.name == ".env" or path.suffix.lower() in forbidden_suffixes:
            violations.append(relative.as_posix())
        if {"pki", "secrets"} & set(relative.parts):
            violations.append(relative.as_posix())

    assert violations == []
