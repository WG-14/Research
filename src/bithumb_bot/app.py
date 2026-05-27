"""Compatibility shim for historical ``bithumb_bot.app`` imports."""

from __future__ import annotations

from typing import Any

from .cli.main import main


def __getattr__(name: str) -> Any:
    from . import app_impl

    return getattr(app_impl, name)


def legacy_main(argv: list[str] | None = None) -> int:
    return main(argv)
