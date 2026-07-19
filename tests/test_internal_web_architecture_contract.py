from __future__ import annotations

import ast
from dataclasses import asdict, fields
from pathlib import Path
import re
import runpy
import tomllib

import pytest

from market_research.paths import ResearchPathError, ResearchPathManager
from market_research.research.run_summary import (
    ResearchRunSummary,
    build_research_run_summary,
)
from market_research.research_cli.registry import command_registry
from market_research.settings import ResearchSettings, ResearchSettingsError


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "src" / "market_research"
ARCHITECTURE_DOC = ROOT / "docs" / "internal-web-architecture.md"

EXPECTED_GUI_POLICY = {
    "research-backtest": "cli_only",
    "research-walk-forward": "cli_only",
    "research-validate": "required",
    "research-readiness": "required",
    "research-freeze-dataset": "admin_only",
    "research-workload-estimate": "required",
    "research-batch": "cli_only",
    "research-forward-diagnostics": "cli_only",
    "research-verify-audit": "admin_only",
    "research-reproduce-run": "admin_only",
    "research-registry-inspect": "cli_only",
    "research-registry-validate": "admin_only",
    "research-mark-attempt-aborted": "cli_only",
    "research-export-strategy-package": "admin_only",
    "research-compare": "required",
    "research-render-report": "cli_only",
    "research-governance-transition": "admin_only",
    "research-record-human-review": "admin_only",
    "research-approve-strategy-candidate": "admin_only",
    "research-derivative-register": "cli_only",
    "research-derivative-replay": "cli_only",
    "research-derivative-diff": "cli_only",
}

FORBIDDEN_WEB_IMPORT_ROOTS = {
    "django",
    "flask",
    "fastapi",
    "starlette",
    "quart",
    "internal_web",
}
FORBIDDEN_ROOT_DEPENDENCIES = {
    "django",
    "celery",
    "redis",
    "gunicorn",
    "uvicorn",
    "channels",
    "whitenoise",
    "daphne",
}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _dependency_name(requirement: object) -> str:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", str(requirement))
    assert match is not None, requirement
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _policy_value(value: object) -> str:
    return str(getattr(value, "value", value))


def test_research_package_does_not_import_web_framework_or_internal_web() -> None:
    violations: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        for module in _imported_modules(path):
            root = module.split(".", 1)[0]
            if root in FORBIDDEN_WEB_IMPORT_ROOTS or module.startswith(
                "apps.internal_web"
            ):
                violations.append(f"{path.relative_to(ROOT)} imports {module}")
    assert violations == []


def test_root_distribution_has_no_web_runtime_dependencies() -> None:
    payload = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = list(payload.get("project", {}).get("dependencies", []))
    for group in payload.get("dependency-groups", {}).values():
        requirements.extend(group)
    dependencies = {_dependency_name(item) for item in requirements}
    violations = {
        name
        for name in dependencies
        if name in FORBIDDEN_ROOT_DEPENDENCIES or name.startswith("django-")
    }
    assert violations == set()


def test_every_cli_command_has_one_capability_gui_policy() -> None:
    from market_research.application.capabilities import capability_registry

    capabilities = capability_registry()
    by_command = {
        spec.cli_command: spec
        for spec in capabilities.values()
        if spec.cli_command is not None
    }
    assert set(by_command) == set(command_registry()) == set(EXPECTED_GUI_POLICY)
    assert {
        command: _policy_value(spec.gui_policy) for command, spec in by_command.items()
    } == EXPECTED_GUI_POLICY


def test_required_and_cli_only_capabilities_are_documented() -> None:
    text = ARCHITECTURE_DOC.read_text(encoding="utf-8")
    for command, policy in EXPECTED_GUI_POLICY.items():
        assert f"| `{command}` | `{policy}` |" in text
    assert "required capabilities require a GUI workflow contract" in text
    assert "CLI-only capabilities remain intentionally unavailable from the GUI" in text


def test_every_required_capability_has_an_explicit_web_workflow_contract() -> None:
    from market_research.application.capabilities import GuiPolicy, capability_registry

    namespace = runpy.run_path(
        str(ROOT / "apps/internal_web/src/portal/capability_routes.py")
    )
    workflows = namespace["WEB_CAPABILITY_WORKFLOWS"]
    required = {
        capability_id
        for capability_id, spec in capability_registry().items()
        if spec.gui_policy is GuiPolicy.REQUIRED
    }
    assert required <= set(workflows)
    assert set(workflows) <= set(capability_registry())
    assert all(
        capability_registry()[capability_id].gui_policy
        in {GuiPolicy.REQUIRED, GuiPolicy.ADMIN_ONLY}
        for capability_id in workflows
    )
    assert all(route and permission for route, permission in workflows.values())


@pytest.mark.parametrize(
    "key",
    (
        "RESEARCH_DATA_ROOT",
        "RESEARCH_ARTIFACT_ROOT",
        "RESEARCH_REPORT_ROOT",
        "RESEARCH_CACHE_ROOT",
        "RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH",
        "RESEARCH_DB_PATH",
    ),
)
def test_research_environment_rejects_relative_storage_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for name in (
        "RESEARCH_DATA_ROOT",
        "RESEARCH_ARTIFACT_ROOT",
        "RESEARCH_REPORT_ROOT",
        "RESEARCH_CACHE_ROOT",
        "RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH",
        "RESEARCH_DB_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(key, "relative/path")
    with pytest.raises(ResearchSettingsError, match="absolute path"):
        ResearchSettings.from_env()


def test_managed_path_segments_reject_absolute_and_traversal_input(
    tmp_path: Path,
) -> None:
    settings = ResearchSettings(
        data_root=tmp_path / "datasets",
        artifact_root=tmp_path / "artifacts",
        report_root=tmp_path / "reports",
        cache_root=tmp_path / "cache",
        db_path=None,
        max_workers=1,
        random_seed=0,
    )
    manager = ResearchPathManager.from_settings(settings, project_root=ROOT)

    for invalid in ("", ".", "..", "/absolute", "nested/path", "nested\\path"):
        with pytest.raises(ResearchPathError):
            manager.report_path(invalid)


def test_user_facing_run_summary_does_not_copy_absolute_artifact_paths() -> None:
    forbidden_field_fragments = ("path", "root", "uri")
    assert not [
        field.name
        for field in fields(ResearchRunSummary)
        if any(fragment in field.name for fragment in forbidden_field_fragments)
    ]
    report = {
        "artifact_paths": {
            "report_path": "/srv/market-research/reports/private.json",
            "derived_path": "/srv/market-research/artifacts/private.json",
        },
        "candidates": [],
    }
    projected = asdict(build_research_run_summary(report))
    assert "/srv/market-research" not in repr(projected)


def test_cli_environment_path_summary_does_not_cross_into_non_cli_package() -> None:
    violations: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if "research_cli" in path.parts:
            continue
        modules = _imported_modules(path)
        if "market_research.research_cli.environment" in modules:
            violations.append(str(path.relative_to(ROOT)))
    assert violations == []
