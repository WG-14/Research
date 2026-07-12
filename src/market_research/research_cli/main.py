from __future__ import annotations

import argparse

from .context import ResearchAppContext, build_research_context
from .registry import ResearchCommandSpec, command_registry


def build_parser(registry: dict[str, ResearchCommandSpec] | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market-research")
    subparsers = parser.add_subparsers(dest="cmd", required=False)
    for spec in sorted((registry or command_registry()).values(), key=lambda item: item.name):
        spec.register_parser(subparsers)
    return parser


def main(argv: list[str] | None = None, context: ResearchAppContext | None = None) -> int:
    registry = dict(command_registry())
    args = build_parser(registry).parse_args(argv)
    command = getattr(args, "cmd", None)
    if command is None:
        return 0
    app_context = context or build_research_context()
    result = registry[command].handler(args, app_context)
    return 0 if result is None else int(result)
