from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .paths import ResearchPathManager
from .settings import ResearchSettings


@dataclass(slots=True)
class ResearchAppContext:
    settings: ResearchSettings
    paths: ResearchPathManager
    printer: Callable[[str], None] = print


def build_research_context() -> ResearchAppContext:
    settings = ResearchSettings.from_env()
    return ResearchAppContext(
        settings=settings,
        paths=ResearchPathManager.from_settings(settings),
    )
