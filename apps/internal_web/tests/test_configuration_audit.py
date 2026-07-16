from __future__ import annotations

import json
import tomllib
import uuid
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import Group

from portal.audit import append_web_audit_event, validate_web_audit
from portal.capability_routes import WEB_CAPABILITY_WORKFLOWS
from portal.security import RBAC_GROUPS
from portal.urls import urlpatterns
from market_research_web.settings import (
    _bool_env,
    _bounded_positive_int_env,
    _source_root_env,
)


@pytest.mark.django_db
def test_rbac_groups_are_seeded_by_migration() -> None:
    assert set(
        Group.objects.filter(name__in=RBAC_GROUPS).values_list("name", flat=True)
    ) == set(RBAC_GROUPS)
    runner_permissions = set(
        Group.objects.get(name="research_runner").permissions.values_list(
            "codename", flat=True
        )
    )
    assert {"upload_research_manifest", "submit_research_job"} <= runner_permissions
    assert "approve_research_candidate" not in runner_permissions


def test_database_and_research_state_are_repository_external() -> None:
    repository_root = settings.REPOSITORY_ROOT.resolve()
    database = settings.DATABASES["default"]
    if database["ENGINE"] == "django.db.backends.sqlite3":
        database_name = str(database["NAME"])
        # Django replaces the configured external SQLite path with an isolated
        # shared-memory URI while tests are running.
        if database_name.startswith("file:memorydb_"):
            assert "mode=memory" in database_name
        else:
            assert not Path(database_name).resolve().is_relative_to(repository_root)
    else:
        # A PostgreSQL database name is an identifier, not a filesystem path.
        assert database["ENGINE"] == "django.db.backends.postgresql"
    for path in (
        settings.RESEARCH_PATHS.data_root,
        settings.RESEARCH_PATHS.artifact_root,
        settings.RESEARCH_PATHS.report_root,
        settings.RESEARCH_PATHS.cache_root,
        settings.STATIC_ROOT,
    ):
        assert not path.resolve().is_relative_to(repository_root)
    assert settings.DEBUG is False


def test_security_boolean_environment_values_fail_closed(monkeypatch) -> None:
    monkeypatch.delenv("INTERNAL_WEB_SECURITY_PROBE", raising=False)
    assert _bool_env("INTERNAL_WEB_SECURITY_PROBE", default=True) is True
    monkeypatch.setenv("INTERNAL_WEB_SECURITY_PROBE", "false")
    assert _bool_env("INTERNAL_WEB_SECURITY_PROBE", default=True) is False
    monkeypatch.setenv("INTERNAL_WEB_SECURITY_PROBE", "treu")
    with pytest.raises(RuntimeError, match="must be an explicit boolean"):
        _bool_env("INTERNAL_WEB_SECURITY_PROBE", default=True)


def test_installed_web_uses_explicit_source_root(monkeypatch, tmp_path: Path) -> None:
    source_root = tmp_path / "Research"
    monkeypatch.setenv("RESEARCH_OPS_SOURCE_ROOT", str(source_root))
    assert _source_root_env() == source_root.resolve()

    for invalid in ("relative/source", "/"):
        monkeypatch.setenv("RESEARCH_OPS_SOURCE_ROOT", invalid)
        with pytest.raises(RuntimeError, match="absolute non-root"):
            _source_root_env()


def test_login_throttle_integer_environment_values_are_strict_and_bounded(
    monkeypatch,
) -> None:
    name = "INTERNAL_WEB_LOGIN_THROTTLE_PROBE"
    monkeypatch.delenv(name, raising=False)
    assert _bounded_positive_int_env(name, default=5, minimum=1, maximum=100) == 5
    monkeypatch.setenv(name, "12")
    assert _bounded_positive_int_env(name, default=5, minimum=1, maximum=100) == 12
    for invalid in (" 12", "+12", "1.0", "１２", "", "0", "101"):
        monkeypatch.setenv(name, invalid)
        with pytest.raises(RuntimeError):
            _bounded_positive_int_env(name, default=5, minimum=1, maximum=100)


def test_hash_chained_audit_detects_tampering(tmp_path: Path, settings) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "web-audit.jsonl"
    append_web_audit_event(
        action="test_event",
        actor_id="actor",
        object_type="fixture",
        object_id="one",
        correlation_id=str(uuid.uuid4()),
        details={"password": "hidden", "server_path": "/srv/result.json"},
    )
    validation = validate_web_audit()
    assert validation["status"] == "PASS"
    row = json.loads(settings.INTERNAL_WEB_AUDIT_PATH.read_text(encoding="utf-8"))
    assert row["details"] == {
        "password": "<redacted>",
        "server_path": "<redacted-path>",
    }

    row["action"] = "tampered"
    settings.INTERNAL_WEB_AUDIT_PATH.write_text(
        json.dumps(row) + "\n", encoding="utf-8"
    )
    assert validate_web_audit()["status"] == "FAIL"


def test_correlation_and_browser_security_headers(client) -> None:
    first = client.get("/__missing__")
    second = client.get("/__missing__", HTTP_X_CORRELATION_ID="untrusted")

    assert uuid.UUID(first.headers["X-Correlation-ID"])
    assert uuid.UUID(second.headers["X-Correlation-ID"])
    assert second.headers["X-Correlation-ID"] != "untrusted"
    assert second.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in second.headers["Content-Security-Policy"]


def test_required_capability_workflow_routes_exist() -> None:
    route_names = {pattern.name for pattern in urlpatterns}
    assert {
        route_name for route_name, _permission in WEB_CAPABILITY_WORKFLOWS.values()
    } <= route_names


def test_distribution_declares_runtime_templates_and_static_assets() -> None:
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    package_data = project["tool"]["setuptools"]["package-data"]["portal"]

    assert set(package_data) == {
        "templates/portal/*.html",
        "templates/registration/*.html",
        "static/portal/*",
    }
