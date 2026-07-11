from __future__ import annotations

from typing import Any


class LazyOperationalConfigValue:
    """Resolve an operational config value only for legacy research callers."""

    def __init__(self, attribute: str) -> None:
        self._attribute = attribute

    def _resolve(self) -> Any:
        from bithumb_bot import config

        return getattr(config, self._attribute)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)


PATH_MANAGER: Any = LazyOperationalConfigValue("PATH_MANAGER")
PROJECT_ROOT: Any = LazyOperationalConfigValue("PROJECT_ROOT")
settings: Any = LazyOperationalConfigValue("settings")
