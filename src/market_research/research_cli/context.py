from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .environment import ResearchEnvironmentSummary
from market_research.paths import ResearchPathManager
from market_research.settings import ResearchSettings


@dataclass(slots=True)
class ResearchAppContext:
    settings: ResearchSettings
    paths: ResearchPathManager
    printer: Callable[[str], None] = print
    environment: ResearchEnvironmentSummary | None = None


def build_research_context() -> ResearchAppContext:
    settings = ResearchSettings.from_env()
    context = ResearchAppContext(
        settings=settings,
        paths=ResearchPathManager.from_settings(settings),
    )
    return ResearchAppContext(
        settings=context.settings,
        paths=context.paths,
        printer=context.printer,
        environment=ResearchEnvironmentSummary.from_settings(settings),
    )
