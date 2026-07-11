from __future__ import annotations

import sys


def run_cli() -> None:
    """Run the research-only CLI without loading the operational bootstrap."""

    from .research_cli.main import main

    raise SystemExit(main(sys.argv[1:]))
