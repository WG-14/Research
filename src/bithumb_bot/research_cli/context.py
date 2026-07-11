from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .environment import ResearchEnvironmentSummary
from .notifier import DisabledResearchNotifier, ResearchNotifier
from .paths import ResearchPathManager
from .settings import ResearchSettings


@dataclass(slots=True)
class ResearchAppContext:
    settings: ResearchSettings
    paths: ResearchPathManager
    printer: Callable[[str], None] = print
    notifier: ResearchNotifier = field(default_factory=DisabledResearchNotifier)
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
        notifier=context.notifier,
        environment=ResearchEnvironmentSummary.from_settings(settings),
    )
