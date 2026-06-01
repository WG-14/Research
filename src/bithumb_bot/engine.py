from __future__ import annotations

from .config import settings
from .runtime.app_container import create_default_runtime_app

__all__ = ["run_loop", "settings"]


def run_loop() -> None:
    create_default_runtime_app(settings).runner.run_forever()
