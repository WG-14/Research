from __future__ import annotations

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DERIVATIVE_ROOT = PROJECT_ROOT / "src" / "market_research" / "research" / "derivatives"
FORBIDDEN_IMPORT_ROOTS = {
    "aiohttp",
    "ccxt",
    "django",
    "httpx",
    "market_research_web",
    "portal",
    "requests",
    "research_operations",
    "socket",
    "websocket",
    "websockets",
}
FORBIDDEN_MARKET_RESEARCH_PREFIXES = (
    "market_research.broker",
    "market_research.exchange",
    "market_research.live",
    "market_research.order_submission",
    "market_research.trading",
)


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)
    return tuple(imported)


def test_derivative_research_has_no_network_live_or_operational_import_path() -> None:
    violations: list[str] = []
    for path in sorted(DERIVATIVE_ROOT.glob("*.py")):
        for imported in _imports(path):
            root = imported.split(".", 1)[0]
            if root in FORBIDDEN_IMPORT_ROOTS or imported.startswith(
                FORBIDDEN_MARKET_RESEARCH_PREFIXES
            ):
                violations.append(f"{path.name}:{imported}")

    assert violations == []


def test_derivative_research_never_references_the_separate_operation_repository() -> (
    None
):
    forbidden = "/home/vorac/work/" + "Operation"
    offenders = [
        path.name
        for path in sorted(DERIVATIVE_ROOT.glob("*.py"))
        if forbidden in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
