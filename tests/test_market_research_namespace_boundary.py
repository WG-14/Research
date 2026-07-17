from __future__ import annotations

import subprocess
import sys


def test_new_package_namespace_imports() -> None:
    import market_research

    assert market_research is not None


def test_cli_subprocess_uses_only_new_namespace() -> None:
    script = """
import sys
from market_research.research_cli.main import build_parser

build_parser()
assert any(name.startswith("market_research") for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
