from __future__ import annotations

import sys

from .application.cli_execution import is_operated_runtime


def run_cli() -> None:
    """Run the research-only CLI without loading the operational bootstrap."""

    if is_operated_runtime():
        sys.stderr.write(
            "market-research: direct CLI execution is disabled in the operated "
            "runtime profile; submit work through the authorized Operations service\n"
        )
        raise SystemExit(78)

    from .research_cli.main import main

    raise SystemExit(main(sys.argv[1:]))
