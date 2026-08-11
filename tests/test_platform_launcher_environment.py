from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _fake_uv(tmp_path: Path) -> Path:
    executable = tmp_path / "bin" / "uv"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        "#!/bin/sh\n"
        "printf 'TMPDIR=%s\\n' \"$TMPDIR\"\n"
        "printf 'TEMP=%s\\n' \"$TEMP\"\n"
        "printf 'TMP=%s\\n' \"$TMP\"\n"
        "printf 'RESEARCH_DATA_ROOT=%s\\n' \"$RESEARCH_DATA_ROOT\"\n"
        "printf 'RESEARCH_ARTIFACT_ROOT=%s\\n' \"$RESEARCH_ARTIFACT_ROOT\"\n"
        "printf 'RESEARCH_REPORT_ROOT=%s\\n' \"$RESEARCH_REPORT_ROOT\"\n"
        "printf 'RESEARCH_CACHE_ROOT=%s\\n' \"$RESEARCH_CACHE_ROOT\"\n"
        "printf 'RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH=%s\\n' "
        '"$RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH"\n'
        "printf 'XDG_STATE_HOME=%s\\n' \"$XDG_STATE_HOME\"\n"
        "printf 'RESEARCH_OPS_TEST_DATABASE_URL=%s\\n' "
        '"${RESEARCH_OPS_TEST_DATABASE_URL:-}"\n'
        "printf 'ARGV=%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable.parent


def _launcher_environment(fake_bin: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("TMPDIR", None)
    environment.pop("RESEARCH_TEST_TMPDIR", None)
    environment["TEMP"] = "/mnt/c/inherited-windows-temp"
    environment["TMP"] = "/mnt/c/inherited-windows-temp"
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    return environment


def test_platform_test_launcher_uses_linux_tmp_when_wsl_inherits_windows_temp(
    tmp_path: Path,
) -> None:
    environment = _launcher_environment(_fake_uv(tmp_path))

    completed = subprocess.run(
        [str(ROOT / "scripts" / "platform"), "test-core"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "TMPDIR=/tmp\n" in completed.stdout
    assert "TEMP=/tmp\n" in completed.stdout
    assert "TMP=/tmp\n" in completed.stdout
    assert "ARGV=run --package market-research pytest tests\n" in completed.stdout


def test_platform_test_launcher_rejects_missing_explicit_temp_root(
    tmp_path: Path,
) -> None:
    environment = _launcher_environment(_fake_uv(tmp_path))
    missing = tmp_path / "missing-test-root"
    environment["RESEARCH_TEST_TMPDIR"] = str(missing)

    completed = subprocess.run(
        [str(ROOT / "scripts" / "platform"), "test-core"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 78
    assert f"test temp directory does not exist: {missing}" in completed.stderr
    assert "ARGV=" not in completed.stdout


def test_platform_test_launcher_rejects_repository_internal_temp_root(
    tmp_path: Path,
) -> None:
    environment = _launcher_environment(_fake_uv(tmp_path))
    repository_link = tmp_path / "repository-link"
    try:
        repository_link.symlink_to(ROOT, target_is_directory=True)
    except OSError as error:
        raise AssertionError(f"test requires directory symlinks: {error}") from error
    environment["RESEARCH_TEST_TMPDIR"] = str(repository_link)

    completed = subprocess.run(
        [str(ROOT / "scripts" / "platform"), "test-core"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 78
    assert (
        f"test temp directory must be outside the repository: {repository_link}"
        in completed.stderr
    )
    assert "ARGV=" not in completed.stdout


def _environment_value(output: str, key: str) -> str:
    prefix = f"{key}="
    values = [
        line.removeprefix(prefix)
        for line in output.splitlines()
        if line.startswith(prefix)
    ]
    assert values
    assert len(set(values)) == 1
    return values[0]


def test_platform_test_launcher_replaces_user_state_with_fresh_external_roots(
    tmp_path: Path,
) -> None:
    environment = _launcher_environment(_fake_uv(tmp_path))
    caller_state = tmp_path / "caller-state"
    environment.update(
        {
            "RESEARCH_DATA_ROOT": str(caller_state / "datasets"),
            "RESEARCH_ARTIFACT_ROOT": str(caller_state / "artifacts"),
            "RESEARCH_REPORT_ROOT": str(caller_state / "reports"),
            "RESEARCH_CACHE_ROOT": str(caller_state / "cache"),
            "RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH": str(
                caller_state / "identity.jsonl"
            ),
            "XDG_STATE_HOME": str(caller_state / "xdg-state"),
        }
    )

    first = subprocess.run(
        [str(ROOT / "scripts" / "platform"), "test-core"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    second = subprocess.run(
        [str(ROOT / "scripts" / "platform"), "test-core"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_data = Path(_environment_value(first.stdout, "RESEARCH_DATA_ROOT"))
    second_data = Path(_environment_value(second.stdout, "RESEARCH_DATA_ROOT"))
    first_root = first_data.parent
    assert first_root.is_absolute()
    assert not first_root.is_relative_to(ROOT.resolve())
    assert first_root != caller_state
    assert second_data.parent != first_root
    assert Path(_environment_value(first.stdout, "RESEARCH_ARTIFACT_ROOT")) == (
        first_root / "artifacts"
    )
    assert Path(_environment_value(first.stdout, "RESEARCH_REPORT_ROOT")) == (
        first_root / "reports"
    )
    assert Path(_environment_value(first.stdout, "RESEARCH_CACHE_ROOT")) == (
        first_root / "cache"
    )
    assert (
        Path(
            _environment_value(
                first.stdout, "RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH"
            )
        )
        == first_root / "identity" / "experiment-identities.jsonl"
    )
    assert Path(_environment_value(first.stdout, "XDG_STATE_HOME")) == (
        first_root / "xdg-state"
    )
    assert not first_root.exists()
    assert not second_data.parent.exists()


def test_platform_test_all_uses_one_fresh_environment_for_all_distributions(
    tmp_path: Path,
) -> None:
    environment = _launcher_environment(_fake_uv(tmp_path))

    completed = subprocess.run(
        [str(ROOT / "scripts" / "platform"), "test-all"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.count("ARGV=") == 3
    assert "ARGV=run --package market-research pytest tests\n" in completed.stdout
    assert (
        "ARGV=run --package market-research-internal-web pytest "
        "apps/internal_web/tests\n"
    ) in completed.stdout
    assert (
        "ARGV=run --package research-operations pytest "
        "services/research_operations/tests\n"
    ) in completed.stdout
    isolated_root = Path(
        _environment_value(completed.stdout, "RESEARCH_DATA_ROOT")
    ).parent
    assert not isolated_root.exists()


def test_platform_integration_launcher_preserves_database_contract(
    tmp_path: Path,
) -> None:
    environment = _launcher_environment(_fake_uv(tmp_path))
    database_url = "postgresql://tester:secret@localhost/research_contract"
    environment["RESEARCH_OPS_TEST_DATABASE_URL"] = database_url
    environment["INTERNAL_WEB_DATABASE_ENGINE"] = "postgresql"

    completed = subprocess.run(
        [str(ROOT / "scripts" / "platform"), "test-integration"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert (
        completed.stdout.count(f"RESEARCH_OPS_TEST_DATABASE_URL={database_url}\n") == 2
    )
    assert completed.stdout.count("ARGV=") == 2
