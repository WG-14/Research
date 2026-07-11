from __future__ import annotations

from dataclasses import dataclass

from .settings import ResearchSettings


@dataclass(frozen=True, slots=True)
class ResearchEnvironmentSummary:
    """Research-only configuration provenance safe to include in reports."""

    db_path_configured: bool
    data_root: str
    artifact_root: str
    settings_source: str = "RESEARCH_*"

    @classmethod
    def from_settings(cls, settings: ResearchSettings) -> "ResearchEnvironmentSummary":
        return cls(
            db_path_configured=settings.db_path is not None,
            data_root=str(settings.data_root),
            artifact_root=str(settings.artifact_root),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "settings_source": self.settings_source,
            "db_path_configured": self.db_path_configured,
            "data_root": self.data_root,
            "artifact_root": self.artifact_root,
        }
