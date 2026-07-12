from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ResearchSettingsError(ValueError):
    pass


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_root() -> Path:
    state_home = os.getenv("XDG_STATE_HOME", "").strip()
    if state_home:
        return (Path(state_home).expanduser() / "market-research").resolve()
    return (Path.home() / ".local" / "state" / "market-research").resolve()


def _external_absolute_path(key: str, value: str | None, default: Path) -> Path:
    raw = (value or "").strip()
    path = Path(raw).expanduser() if raw else default
    if raw and not path.is_absolute():
        raise ResearchSettingsError(f"{key} must be an absolute path")
    resolved = path.resolve()
    if _RepositoryBoundary.is_within_repository(resolved):
        raise ResearchSettingsError(f"{key} must be outside the repository: {resolved}")
    return resolved


class _RepositoryBoundary:
    @staticmethod
    def is_within_repository(path: Path) -> bool:
        try:
            path.relative_to(_project_root())
            return True
        except ValueError:
            return False


def _optional_external_absolute_path(key: str, value: str | None) -> Path | None:
    raw = (value or "").strip()
    return None if not raw else _external_absolute_path(key, raw, Path(raw))


def _positive_int(key: str, value: str | None, default: int) -> int:
    raw = (value or "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ResearchSettingsError(f"{key} must be an integer") from exc
    if parsed <= 0:
        raise ResearchSettingsError(f"{key} must be positive")
    return parsed


def _int(key: str, value: str | None, default: int) -> int:
    raw = (value or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ResearchSettingsError(f"{key} must be an integer") from exc


@dataclass(frozen=True, slots=True)
class ResearchSettings:
    """Research-only configuration resolved after CLI argument parsing."""

    data_root: Path
    artifact_root: Path
    report_root: Path
    cache_root: Path
    db_path: Path | None
    max_workers: int
    random_seed: int

    @classmethod
    def from_env(cls) -> "ResearchSettings":
        root = _default_root()
        artifact_root = _external_absolute_path("RESEARCH_ARTIFACT_ROOT", os.getenv("RESEARCH_ARTIFACT_ROOT"), root / "artifacts")
        return cls(
            data_root=_external_absolute_path("RESEARCH_DATA_ROOT", os.getenv("RESEARCH_DATA_ROOT"), root / "datasets"),
            artifact_root=artifact_root,
            report_root=_external_absolute_path("RESEARCH_REPORT_ROOT", os.getenv("RESEARCH_REPORT_ROOT"), root / "reports"),
            cache_root=_external_absolute_path("RESEARCH_CACHE_ROOT", os.getenv("RESEARCH_CACHE_ROOT"), root / "cache"),
            db_path=_optional_external_absolute_path("RESEARCH_DB_PATH", os.getenv("RESEARCH_DB_PATH")),
            max_workers=_positive_int("RESEARCH_MAX_WORKERS", os.getenv("RESEARCH_MAX_WORKERS"), 1),
            random_seed=_int("RESEARCH_RANDOM_SEED", os.getenv("RESEARCH_RANDOM_SEED"), 0),
        )
