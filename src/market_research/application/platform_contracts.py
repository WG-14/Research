"""Stable filesystem and configuration contracts for platform adapters.

Web and Operations use this module instead of importing Research implementation
modules directly.  The facade intentionally exposes only repository-external
path configuration and crash-safe publication primitives shared by the three
distributions.
"""

from market_research.paths import ResearchPathError, ResearchPathManager
from market_research.settings import ResearchSettings, ResearchSettingsError
from market_research.storage_io import (
    append_jsonl,
    write_json_atomic,
    write_json_atomic_create_or_verify,
)

__all__ = [
    "ResearchPathError",
    "ResearchPathManager",
    "ResearchSettings",
    "ResearchSettingsError",
    "append_jsonl",
    "write_json_atomic",
    "write_json_atomic_create_or_verify",
]
