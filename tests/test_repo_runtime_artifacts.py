from __future__ import annotations

import subprocess
import shutil
from pathlib import Path


def test_repo_runtime_artifact_check_script_passes_on_clean_tree() -> None:
    script = Path("scripts/check_repo_runtime_artifacts.sh")
    proc = subprocess.run(["bash", script.as_posix()], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "no repo-local runtime/test artifacts" in (proc.stdout + proc.stderr)


def test_repo_runtime_artifact_check_rejects_repo_local_pytest_workspace() -> None:
    script = Path("scripts/check_repo_runtime_artifacts.sh")
    sentinel = Path(".tmp/pytest")
    try:
        sentinel.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(["bash", script.as_posix()], capture_output=True, text=True, check=False)
        assert proc.returncode != 0
        assert ".tmp/pytest" in (proc.stdout + proc.stderr)
    finally:
        shutil.rmtree(Path(".tmp"), ignore_errors=True)


def test_repo_runtime_artifact_check_rejects_generated_research_artifacts() -> None:
    script = Path("scripts/check_repo_runtime_artifacts.sh")
    sentinels = [
        Path("derived/research"),
        Path("reports/research"),
        Path("data/paper/derived/research"),
        Path("data/paper/reports/research"),
        Path("pytest-debug"),
        Path("traces"),
        Path("candidate_results"),
        Path("candidate_failures"),
    ]
    try:
        for path in sentinels:
            path.mkdir(parents=True, exist_ok=True)
        Path("decisions.jsonl").write_text('{"x":1}\n', encoding="utf-8")
        Path("data/paper/derived/research/candidate_events.jsonl").write_text('{"x":1}\n', encoding="utf-8")
        proc = subprocess.run(["bash", script.as_posix()], capture_output=True, text=True, check=False)
        output = proc.stdout + proc.stderr
        assert proc.returncode != 0
        for path in sentinels:
            assert path.as_posix() in output
        assert "decisions.jsonl" in output
        assert "data/paper/derived/research" in output
    finally:
        for path in sentinels:
            shutil.rmtree(path, ignore_errors=True)
        Path("decisions.jsonl").unlink(missing_ok=True)


def test_repo_runtime_artifact_check_allows_explicit_fixture_jsonl() -> None:
    script = Path("scripts/check_repo_runtime_artifacts.sh")
    fixture = Path("tests/fixtures/runtime_artifact_policy_allowed.jsonl")
    try:
        fixture.write_text('{"fixture":true}\n', encoding="utf-8")
        proc = subprocess.run(["bash", script.as_posix()], capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stdout + proc.stderr
    finally:
        fixture.unlink(missing_ok=True)
