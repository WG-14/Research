from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .settings import ResearchSettings


class ResearchPathError(ValueError):
    pass


def _safe_parts(*parts: str) -> tuple[str, ...]:
    normalized = tuple(str(part).strip() for part in parts)
    if not normalized or any(not part or Path(part).is_absolute() or part in {".", ".."} for part in normalized):
        raise ResearchPathError("research output path parts must be non-empty relative names")
    if any("/" in part or "\\" in part for part in normalized):
        raise ResearchPathError("research output path parts must not contain path separators")
    return normalized


@dataclass(frozen=True, slots=True)
class ResearchPathManager:
    """Repository-external paths for datasets and disposable research outputs."""

    settings: ResearchSettings
    project_root: Path

    @classmethod
    def from_settings(cls, settings: ResearchSettings, project_root: Path | None = None) -> "ResearchPathManager":
        return cls(settings=settings, project_root=(project_root or Path.cwd()).expanduser().resolve())

    @property
    def data_root(self) -> Path:
        return self.settings.data_root

    @property
    def artifact_root(self) -> Path:
        return self.settings.artifact_root

    @property
    def report_root(self) -> Path:
        return self.settings.report_root

    @property
    def cache_root(self) -> Path:
        return self.settings.cache_root

    @property
    def db_path(self) -> Path | None:
        return self.settings.db_path

    def ensure_roots(self) -> None:
        for root in (self.data_root, self.artifact_root, self.report_root, self.cache_root):
            root.mkdir(parents=True, exist_ok=True)

    def require_database_path(self) -> Path:
        if self.db_path is None:
            raise ResearchPathError("RESEARCH_DB_PATH is required for this command")
        return self.db_path

    def data_dir(self) -> Path:
        """Compatibility root for existing research artifact writers.

        Existing research writers use ``data_dir()/derived`` and
        ``data_dir()/reports``. Returning the research artifact root preserves
        those relative locations while keeping them outside operational
        paper/live storage.
        """

        return self.artifact_root

    def notification_events_path(self) -> Path:
        return self.report_root / "notifications" / "notification_events.jsonl"

    def ensure_parent_dir(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)

    def dataset_path(self, *parts: str) -> Path:
        return self.data_root.joinpath(*_safe_parts(*parts))

    def artifact_path(self, *parts: str) -> Path:
        return self.artifact_root.joinpath(*_safe_parts(*parts))

    def report_path(self, *parts: str) -> Path:
        return self.report_root.joinpath(*_safe_parts(*parts))

    def cache_path(self, *parts: str) -> Path:
        return self.cache_root.joinpath(*_safe_parts(*parts))
