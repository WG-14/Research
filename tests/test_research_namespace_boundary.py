from __future__ import annotations

import importlib.util
import subprocess
import sys


def test_new_package_namespace_imports() -> None:
    import bithumb_research

    assert bithumb_research is not None


def test_old_package_namespace_does_not_exist() -> None:
    assert importlib.util.find_spec("bithumb" + "_bot") is None


def test_cli_subprocess_uses_only_new_namespace() -> None:
    script = """
import sys
from bithumb_research.research_cli.main import build_parser

build_parser()
assert "bithumb" + "_bot" not in sys.modules
assert any(name.startswith("bithumb_research") for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
