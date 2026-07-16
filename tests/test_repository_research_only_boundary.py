from __future__ import annotations

import ast
import importlib
import json
import pkgutil
from pathlib import Path

import market_research

from market_research.research_composition import list_builtin_strategies as list_research_strategies


ROOT = Path(__file__).resolve().parents[1]


def test_repository_has_only_research_entrypoints_and_environment_template() -> None:
    assert not any((ROOT / name).exists() for name in ("main.py", "backtest.py", "backtest2.py"))
    env_keys = {
        line.split("=", 1)[0]
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }
    assert env_keys == {
        "RESEARCH_DATA_ROOT",
        "RESEARCH_ARTIFACT_ROOT",
        "RESEARCH_REPORT_ROOT",
        "RESEARCH_CACHE_ROOT",
        "RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH",
        "RESEARCH_DB_PATH",
        "RESEARCH_MAX_WORKERS",
        "RESEARCH_RANDOM_SEED",
    }


def test_no_operational_files_or_legacy_path_manager_remain() -> None:
    forbidden = (
        "scripts/check_live_runtime.sh",
        "scripts/collect_live_snapshot.sh",
        "scripts/healthcheck.py",
        "scripts/repair_zero_price_sell_ledger.py",
        "scripts/backup_sqlite.sh",
        "tools/cleanup_open_order.py",
        "tools/make_open_order.py",
        "tools/oms_smoke.py",
        "tests/operator",
    )
    assert not any((ROOT / path).exists() for path in forbidden)
    paths_source = (ROOT / "src/market_research/paths.py").read_text(encoding="utf-8")
    assert "class PathManager:" not in paths_source
    assert "class Path" + "Config:" not in paths_source


def test_architecture_paths_exist_and_strategy_catalog_is_exact() -> None:
    boundaries = json.loads((ROOT / "docs/architecture-boundaries.json").read_text(encoding="utf-8"))
    for section in ("entrypoints", "research_core", "offline_data_helpers"):
        assert all((ROOT / path).exists() for path in boundaries[section])
    required_forbidden_domains = {
        "private_exchange_access",
        "account_connected_runtime",
        "order_submission",
        "account_access",
        "state_repair",
        "reviewed_account_profile",
        "network_market_data_collection",
        "operational_order_fill_database_ingestion",
        "exchange_raw_order_semantics_inference",
        "retry_backfill_source_probe",
    }
    assert required_forbidden_domains <= set(boundaries["forbidden_domains"])
    assert {plugin.name for plugin in list_research_strategies()} == {
        "sma_with_filter",
        "buy_and_hold_baseline",
        "noop_baseline",
        "threshold_research_only",
    }


def test_package_source_does_not_read_legacy_db_path_environment_variable() -> None:
    package = ROOT / "src" / "market_research"
    legacy_reads: list[Path] = []
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            function = node.func
            is_getenv = (
                isinstance(function, ast.Attribute)
                and function.attr == "getenv"
                and isinstance(function.value, ast.Name)
                and function.value.id == "os"
            )
            is_environ_get = (
                isinstance(function, ast.Attribute)
                and function.attr == "get"
                and isinstance(function.value, ast.Attribute)
                and function.value.attr == "environ"
                and isinstance(function.value.value, ast.Name)
                and function.value.value.id == "os"
            )
            if (
                (is_getenv or is_environ_get)
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "DB_PATH"
            ):
                legacy_reads.append(path)
    assert legacy_reads == []


def test_every_package_module_imports_without_side_effects() -> None:
    for module in pkgutil.walk_packages(market_research.__path__, market_research.__name__ + "."):
        importlib.import_module(module.name)
