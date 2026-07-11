from __future__ import annotations

import tomllib
from pathlib import Path


def test_distribution_metadata_and_console_entry_point() -> None:
    payload = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert payload["project"]["name"] == "bithumb-research"
    assert payload["project"]["version"] == "0.1.0"
    assert payload["project"]["scripts"] == {
        "bithumb-research": "bithumb_research.research_bootstrap:run_cli"
    }
    assert not (Path("src") / ("bithumb_" + "bo" + "t")).exists()
