from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class RuntimeStateStore:
    snapshot_reader: Callable[[], object]

    def snapshot(self) -> object:
        return self.snapshot_reader()


__all__ = ["RuntimeStateStore"]
