from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from market_research.market_ids import MarketCodeError, parse_market_id
from market_research.research.execution_calibration_contract import ExecutionCalibrationThresholds
from market_research.research.intervals import interval_to_milliseconds, interval_to_minutes


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "market_research"
FORBIDDEN_IMPORTS = {
    "httpx",
    "requests",
    "aiohttp",
    "urllib.request",
    "websockets",
    "socket",
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
FORBIDDEN_RUNTIME_DEPENDENCIES = {"httpx", "requests", "aiohttp", "websockets"}


def _source_violations(package: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if path.stem in FORBIDDEN_REMOTE_NAMES:
            violations.append(f"forbidden remote module name: {path}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in FORBIDDEN_IMPORTS:
                        violations.append(f"forbidden import {alias.name!r} in {path}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module in FORBIDDEN_IMPORTS:
                    violations.append(f"forbidden import {module!r} in {path}")
                if module == "urllib" and any(alias.name == "request" for alias in node.names):
                    violations.append(f"forbidden import 'urllib.request' in {path}")
                if module == "http" and any(alias.name == "client" for alias in node.names):
                    violations.append(f"forbidden import 'http.client' in {path}")
            elif isinstance(node, ast.Attribute):
                dotted_name = _dotted_name(node)
                if dotted_name in {"urllib.request", "http.client"}:
                    violations.append(f"forbidden network module reference {dotted_name!r} in {path}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in FORBIDDEN_REMOTE_NAMES:
                    violations.append(f"forbidden remote symbol {node.name!r} in {path}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.lower().startswith(FORBIDDEN_URL_PREFIXES):
                    violations.append(f"forbidden URL literal in {path}")
                for table in FORBIDDEN_SQL_TABLES:
                    if _contains_sql_identifier(node.value, table):
                        violations.append(f"forbidden operational SQL table {table!r} in {path}")
    return violations


def _contains_sql_identifier(value: str, identifier: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(identifier)}(?![A-Za-z0-9_])", value) is not None


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
    project = tomllib.loads(pyproject_path.read_text(encoding="utf-8")).get("project", {})
    return {
        re.split(r"[<>=!~;\[ ]", str(item), maxsplit=1)[0].strip().lower()
        for item in project.get("dependencies", [])
    }


def test_offline_package_has_no_public_api_modules() -> None:
    assert not list(PACKAGE.glob("public_api*.py"))


def test_offline_boundary_rejects_network_and_operational_reentry(tmp_path: Path) -> None:
    assert _source_violations(PACKAGE) == []
    assert not (FORBIDDEN_RUNTIME_DEPENDENCIES & _runtime_dependencies(ROOT / "pyproject.toml"))

    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "network.py").write_text(
        "import requests\nimport urllib\nURL = 'https://example.test'\nprobe = urllib.request\ndef fetch_remote(): pass\n",
        encoding="utf-8",
    )
    (fixture / "database.py").write_text("SQL = 'SELECT * FROM orders'\n", encoding="utf-8")
    violations = _source_violations(fixture)
    assert any("forbidden import" in item for item in violations)
    assert any("URL literal" in item for item in violations)
    assert any("network module reference" in item for item in violations)
    assert any("remote symbol" in item for item in violations)
    assert any("operational SQL table" in item for item in violations)

    fake_pyproject = tmp_path / "pyproject.toml"
    fake_pyproject.write_text("[project]\ndependencies = ['aiohttp>=1']\n", encoding="utf-8")
    assert "aiohttp" in _runtime_dependencies(fake_pyproject)


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


def test_execution_calibration_threshold_contract_is_immutable_and_imports_no_runtime_io() -> None:
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
