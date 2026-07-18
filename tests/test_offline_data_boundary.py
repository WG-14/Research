from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from market_research.market_ids import MarketCodeError, parse_market_id
from market_research.research.data_plane import _configured_db_path
from market_research.research.execution_calibration_contract import (
    ExecutionCalibrationThresholds,
)
from market_research.research.intervals import (
    interval_to_milliseconds,
    interval_to_minutes,
)


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "market_research"
FORBIDDEN_NETWORK_ROOTS = {
    "httpx",
    "requests",
    "aiohttp",
    "websockets",
    "urllib3",
    "socket",
    "httpcore",
    "ccxt",
}
FORBIDDEN_STDLIB_NETWORK_MODULES = {
    "urllib.request",
    "http.client",
}
FORBIDDEN_URL_PREFIXES = ("http://", "https://", "ws://", "wss://")
FORBIDDEN_REMOTE_NAMES = {
    "PublicApi",
    "RemoteMarketCatalog",
    "MarketCatalogClient",
    "fetch_remote",
    "source_probe",
    "retry_missing_candles",
}
FORBIDDEN_SQL_TABLES = {
    "orders",
    "order_events",
    "fills",
    "strategy_decisions",
    "execution_quality_events",
}
FORBIDDEN_RUNTIME_DEPENDENCIES = {
    "httpx",
    "requests",
    "aiohttp",
    "websockets",
    "urllib3",
    "httpcore",
    "ccxt",
}


def _source_violations(package: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if path.stem in FORBIDDEN_REMOTE_NAMES:
            violations.append(f"forbidden remote module name: {path}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _module_root(alias.name) in FORBIDDEN_NETWORK_ROOTS:
                        violations.append(f"forbidden import {alias.name!r} in {path}")
                    if any(
                        _matches_module_or_submodule(alias.name, forbidden)
                        for forbidden in FORBIDDEN_STDLIB_NETWORK_MODULES
                    ):
                        violations.append(f"forbidden import {alias.name!r} in {path}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if _module_root(module) in FORBIDDEN_NETWORK_ROOTS:
                    violations.append(f"forbidden import {module!r} in {path}")
                if any(
                    _matches_module_or_submodule(module, forbidden)
                    for forbidden in FORBIDDEN_STDLIB_NETWORK_MODULES
                ):
                    violations.append(f"forbidden import {module!r} in {path}")
                if module == "urllib" and any(
                    alias.name == "request" for alias in node.names
                ):
                    violations.append(f"forbidden import 'urllib.request' in {path}")
                if module == "http" and any(
                    alias.name == "client" for alias in node.names
                ):
                    violations.append(f"forbidden import 'http.client' in {path}")
            elif isinstance(node, ast.Attribute):
                dotted_name = _dotted_name(node)
                if any(
                    _matches_module_or_submodule(dotted_name, forbidden)
                    for forbidden in FORBIDDEN_STDLIB_NETWORK_MODULES
                ):
                    violations.append(
                        f"forbidden network module reference {dotted_name!r} in {path}"
                    )
            elif isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                if node.name in FORBIDDEN_REMOTE_NAMES:
                    violations.append(
                        f"forbidden remote symbol {node.name!r} in {path}"
                    )
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if any(
                    prefix in node.value.lower() for prefix in FORBIDDEN_URL_PREFIXES
                ):
                    violations.append(f"forbidden URL literal in {path}")
                for table in FORBIDDEN_SQL_TABLES:
                    if _contains_sql_identifier(node.value, table):
                        violations.append(
                            f"forbidden operational SQL table {table!r} in {path}"
                        )
    return violations


def _module_root(module: str) -> str:
    return module.split(".", 1)[0]


def _matches_module_or_submodule(module: str, forbidden: str) -> bool:
    return module == forbidden or module.startswith(forbidden + ".")


def _contains_sql_identifier(value: str, identifier: str) -> bool:
    # A research result can legitimately describe simulated ``fills``.  The
    # forbidden boundary is an operational SQL table reference, not the word
    # appearing in an evidence/category label.  Require a SQL table clause so
    # the guard remains sensitive to SELECT/INSERT/UPDATE/DDL statements while
    # avoiding false positives from ordinary research vocabulary.
    optional_schema = r'(?:["`\[]?[A-Za-z0-9_]+["`\]]?\s*\.\s*)?'
    quoted_identifier = rf'["`\[]?{re.escape(identifier)}["`\]]?'
    return (
        re.search(
            rf"\b(?:from|join|into|update|table)\s+"
            rf"{optional_schema}{quoted_identifier}(?![A-Za-z0-9_])",
            value,
            flags=re.IGNORECASE,
        )
        is not None
    )


def _dotted_name(node: ast.Attribute) -> str:
    parts = [node.attr]
    current: ast.expr = node.value
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _runtime_dependencies(pyproject_path: Path) -> set[str]:
    project = tomllib.loads(pyproject_path.read_text(encoding="utf-8")).get(
        "project", {}
    )
    return {
        _normalize_dependency_name(match.group(1))
        for item in project.get("dependencies", [])
        if (match := re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", str(item)))
        is not None
    }


def _normalize_dependency_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_offline_package_has_no_public_api_modules() -> None:
    assert not list(PACKAGE.glob("public_api*.py"))


def test_offline_boundary_rejects_network_and_operational_reentry(
    tmp_path: Path,
) -> None:
    assert _source_violations(PACKAGE) == []
    assert not (
        FORBIDDEN_RUNTIME_DEPENDENCIES & _runtime_dependencies(ROOT / "pyproject.toml")
    )

    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "network.py").write_text(
        "import requests.sessions\nfrom aiohttp.client import ClientSession\n"
        "from websockets.client import connect\nimport httpx._client\n"
        "from urllib.parse import urlparse\nimport urllib\n"
        "import urllib.request as request\nrequest.urlopen('file-or-url')\n"
        "import http.client as client\nclient.HTTPConnection('example.test')\n"
        "from urllib.request import urlopen\nfrom http.client import HTTPConnection\n"
        "TEXT = 'remote endpoint: https://example.test/path'\n"
        "probe = urllib.request\ndef fetch_remote(): pass\n",
        encoding="utf-8",
    )
    (fixture / "database.py").write_text(
        "SQL = 'SELECT * FROM orders'\n", encoding="utf-8"
    )
    violations = _source_violations(fixture)
    assert any("requests.sessions" in item for item in violations)
    assert any("aiohttp.client" in item for item in violations)
    assert any("websockets.client" in item for item in violations)
    assert any("httpx._client" in item for item in violations)
    assert not any("urllib.parse" in item for item in violations)
    assert any("urllib.request" in item for item in violations)
    assert any("http.client" in item for item in violations)
    assert any("URL literal" in item for item in violations)
    assert any("network module reference" in item for item in violations)
    assert any("remote symbol" in item for item in violations)
    assert any("operational SQL table" in item for item in violations)

    fake_pyproject = tmp_path / "pyproject.toml"
    fake_pyproject.write_text(
        "[project]\ndependencies = [\n"
        "  'requests>=2',\n"
        "  'requests[socks]>=2',\n"
        "  'aiohttp; python_version >= \"3.12\"',\n"
        "  'httpx~=0.28',\n"
        "]\n",
        encoding="utf-8",
    )
    dependencies = _runtime_dependencies(fake_pyproject)
    assert {"requests", "aiohttp", "httpx"} <= dependencies


def test_configured_db_path_accepts_only_explicit_path_or_research_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    explicit_path = tmp_path / "explicit.sqlite"
    research_path = tmp_path / "research.sqlite"
    legacy_path = tmp_path / "legacy.sqlite"
    monkeypatch.setenv("RESEARCH_DB_PATH", str(research_path))
    monkeypatch.setenv("DB_PATH", str(legacy_path))

    assert _configured_db_path(explicit_path) == explicit_path.resolve()
    assert _configured_db_path(None) == research_path.resolve()

    monkeypatch.delenv("RESEARCH_DB_PATH")
    with pytest.raises(ValueError, match="RESEARCH_DB_PATH") as error:
        _configured_db_path(None)
    assert (
        str(error.value)
        == "db_path is required; set RESEARCH_DB_PATH for research commands"
    )


def test_market_id_and_interval_helpers_are_local_and_deterministic() -> None:
    assert parse_market_id("krw-btc") == "KRW-BTC"
    assert parse_market_id("usd-btc") == "USD-BTC"
    assert parse_market_id("usdt-btc") == "USDT-BTC"
    assert interval_to_minutes("15m") == 15
    assert interval_to_milliseconds("15m") == 900_000
    for invalid in ("BTC", "BTC_KRW", "", "KRW BTC"):
        with pytest.raises(MarketCodeError):
            parse_market_id(invalid)
    with pytest.raises(ValueError, match="unsupported minute interval"):
        interval_to_minutes("1h")


def test_execution_calibration_threshold_contract_is_immutable_and_imports_no_runtime_io() -> (
    None
):
    thresholds = ExecutionCalibrationThresholds()
    assert thresholds.min_sample == 30
    assert thresholds.max_p90_slippage_bps == 20.0
    assert thresholds.max_p95_full_fill_latency_ms == 3000.0
    assert thresholds.max_partial_fill_rate == 0.05
    assert thresholds.max_model_breach_rate == 0.10
    with pytest.raises(AttributeError):
        thresholds.min_sample = 1  # type: ignore[misc]

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import market_research.research.execution_calibration_contract; "
            "assert not {'sqlite3', 'httpx', 'requests', 'aiohttp', 'websockets'} & set(sys.modules)",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
