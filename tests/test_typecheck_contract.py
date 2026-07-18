from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "typecheck_fixtures" / "invalid_cross_distribution.py"


def test_strict_typecheck_configuration_covers_each_distribution() -> None:
    payload = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    web_payload = tomllib.loads(
        (ROOT / "apps/internal_web/pyproject.toml").read_text(encoding="utf-8")
    )
    mypy = payload["tool"]["mypy"]

    assert mypy["strict"] is True
    assert mypy["disallow_any_unimported"] is True
    assert mypy["plugins"] == ["mypy_django_plugin.main"]
    assert mypy["mypy_path"] == [
        "src",
        "apps/internal_web/src",
        "services/research_operations/src",
    ]
    assert "exclude" not in mypy
    assert "disable_error_code" not in mypy
    assert "ignore_errors" not in mypy
    assert "ignore_missing_imports" not in mypy

    development = payload["dependency-groups"]["dev"]
    web_development = web_payload["dependency-groups"]["dev"]
    assert any(item.startswith("mypy>=1.18,<2") for item in development)
    assert not any(item.startswith("django-stubs") for item in development)
    assert any(
        item.startswith("django-stubs[compatible-mypy]>=5.2,<5.3")
        for item in web_development
    )


def test_platform_and_ci_enforce_the_strict_type_gate() -> None:
    platform = (ROOT / "scripts" / "platform").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "research-ci.yml").read_text(
        encoding="utf-8"
    )

    assert "typecheck)" in platform
    assert "-p market_research" in platform
    assert "-p market_research_web -p portal" in platform
    assert "-p research_operations" in platform
    assert platform.count("--no-incremental --config-file pyproject.toml") == 3
    assert "- name: Type-check all production distributions" in workflow
    assert "run: scripts/platform typecheck" in workflow


def test_negative_fixture_is_rejected_by_mypy(tmp_path: Path) -> None:
    external = tmp_path / "external"
    environment = os.environ.copy()
    environment.update(
        {
            "INTERNAL_WEB_SECRET_KEY": "negative-typecheck-fixture-only",
            "RESEARCH_DATA_ROOT": str(external / "datasets"),
            "RESEARCH_ARTIFACT_ROOT": str(external / "artifacts"),
            "RESEARCH_REPORT_ROOT": str(external / "reports"),
            "RESEARCH_CACHE_ROOT": str(external / "cache"),
            "RESEARCH_OPS_SOURCE_ROOT": str(ROOT),
        }
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--no-incremental",
            "--config-file",
            str(ROOT / "pyproject.toml"),
            str(FIXTURE),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 1, output
    fixture_diagnostic = "tests/typecheck_fixtures/invalid_cross_distribution.py"
    assert output.count(fixture_diagnostic) >= 3, output
    assert output.count("[arg-type]") >= 3, output
    assert "Success: no issues found" not in output
