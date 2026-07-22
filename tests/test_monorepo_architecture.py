from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "src" / "market_research"
WEB = ROOT / "apps" / "internal_web" / "src"
OPERATIONS = ROOT / "services" / "research_operations" / "src"
ARCHITECTURE = ROOT / "docs" / "architecture-boundaries.json"
_DOTTED_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


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


def _architecture() -> dict[str, object]:
    return cast(dict[str, object], json.loads(ARCHITECTURE.read_text(encoding="utf-8")))


def _dependency_rule(payload: dict[str, object], name: str) -> tuple[str, ...]:
    rules = payload["dependency_rules"]
    assert isinstance(rules, dict)
    value = rules[name]
    assert isinstance(value, list)
    result = tuple(str(item) for item in value)
    assert result
    assert len(result) == len(set(result))
    assert all(_DOTTED_MODULE.fullmatch(item) for item in result)
    return result


def _import_roots(payload: dict[str, object], distribution: str) -> tuple[str, ...]:
    distributions = payload["distributions"]
    assert isinstance(distributions, dict)
    metadata = distributions[distribution]
    assert isinstance(metadata, dict)
    roots = metadata["python_import_roots"]
    assert isinstance(roots, list)
    result = tuple(str(item) for item in roots)
    assert result
    assert len(result) == len(set(result))
    assert all(_DOTTED_MODULE.fullmatch(item) for item in result)
    return result


def _matches_prefix(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _belongs_to(module: str, roots: tuple[str, ...]) -> bool:
    return any(_matches_prefix(module, root) for root in roots)


def _public_contract_modules(payload: dict[str, object]) -> tuple[str, ...]:
    contracts = payload["public_adapter_contracts"]
    assert isinstance(contracts, list)
    assert len(contracts) == len(set(str(item) for item in contracts))
    modules: list[str] = []
    for raw_path in contracts:
        path = ROOT / str(raw_path)
        assert path.is_file()
        assert path.suffix == ".py"
        if path.is_relative_to(CORE):
            parts = ("market_research", *path.relative_to(CORE).with_suffix("").parts)
        elif path.is_relative_to(WEB):
            parts = path.relative_to(WEB).with_suffix("").parts
        elif path.is_relative_to(OPERATIONS):
            parts = path.relative_to(OPERATIONS).with_suffix("").parts
        else:
            raise AssertionError(
                f"public contract is outside a distribution: {raw_path}"
            )
        if parts[-1] == "__init__":
            parts = parts[:-1]
        module = ".".join(parts)
        assert _DOTTED_MODULE.fullmatch(module)
        modules.append(module)
    assert len(modules) == len(set(modules))
    return tuple(modules)


def _assert_exact_allowlist(
    *,
    imports: list[tuple[Path, str]],
    owned_roots: tuple[str, ...],
    allowed_modules: tuple[str, ...],
) -> None:
    cross_distribution = [
        (path, imported)
        for path, imported in imports
        if _belongs_to(imported, owned_roots)
    ]
    violations = [
        (path.relative_to(ROOT).as_posix(), imported)
        for path, imported in cross_distribution
        if imported not in allowed_modules
    ]
    used = {
        module
        for module in allowed_modules
        if any(imported == module for _, imported in cross_distribution)
    }

    assert violations == []
    assert set(allowed_modules) - used == set()


def test_architecture_manifest_defines_executable_import_namespaces() -> None:
    payload = _architecture()
    assert payload["schema_version"] == 7
    all_roots = tuple(
        root
        for distribution in ("research", "web", "operations")
        for root in _import_roots(payload, distribution)
    )
    assert len(all_roots) == len(set(all_roots))
    public_modules = _public_contract_modules(payload)

    target_roots_by_rule = {
        "web_core_access_allowed_through": _import_roots(payload, "research"),
        "operations_core_access_allowed_through": _import_roots(payload, "research"),
        "operations_web_access_allowed_through": _import_roots(payload, "web"),
    }
    for rule, target_roots in target_roots_by_rule.items():
        allowed = _dependency_rule(payload, rule)
        assert all(_belongs_to(prefix, target_roots) for prefix in allowed)
        assert set(allowed) <= set(public_modules)


def test_core_never_depends_on_web_or_operations() -> None:
    payload = _architecture()
    forbidden = _dependency_rule(payload, "research_distribution_forbids")
    violations = [
        (path.relative_to(ROOT).as_posix(), imported)
        for path, imported in _python_imports(CORE)
        if _belongs_to(imported, forbidden)
    ]

    assert violations == []


def test_web_uses_public_core_application_or_composition_contracts() -> None:
    payload = _architecture()
    _assert_exact_allowlist(
        imports=_python_imports(WEB),
        owned_roots=_import_roots(payload, "research"),
        allowed_modules=_dependency_rule(payload, "web_core_access_allowed_through"),
    )


def test_operations_uses_web_and_core_facades_only() -> None:
    payload = _architecture()
    imports = _python_imports(OPERATIONS)
    _assert_exact_allowlist(
        imports=imports,
        owned_roots=_import_roots(payload, "research"),
        allowed_modules=_dependency_rule(
            payload, "operations_core_access_allowed_through"
        ),
    )
    _assert_exact_allowlist(
        imports=imports,
        owned_roots=_import_roots(payload, "web"),
        allowed_modules=_dependency_rule(
            payload, "operations_web_access_allowed_through"
        ),
    )


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
