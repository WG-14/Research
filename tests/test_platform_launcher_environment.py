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
