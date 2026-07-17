from __future__ import annotations

import tomllib
from pathlib import Path


def test_distribution_metadata_and_console_entry_point() -> None:
    payload = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert payload["project"]["name"] == "market-research"
    assert payload["project"]["version"] == "0.1.0"
    assert payload["project"]["scripts"] == {
        "market-research": "market_research.research_bootstrap:run_cli"
    }


def test_distribution_probe_uses_explicit_builtin_composition() -> None:
    workflow = Path(".github/workflows/research-ci.yml").read_text(encoding="utf-8")

    assert (
        "from market_research.research_composition import builtin_strategy_registry"
        in workflow
    )
    assert "list_research_strategies(registry=builtin_strategy_registry())" in workflow
