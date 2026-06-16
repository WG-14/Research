from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
import subprocess
import shutil
from pathlib import Path


@contextmanager
def _repo_artifact_check_lock():
    lock_path = Path(os.environ.get("PYTEST_DEBUG_TEMPROOT", "/tmp")) / "bithumb-bot-repo-artifact-check.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _run_artifact_check_unlocked() -> subprocess.CompletedProcess[str]:
    script = Path("scripts/check_repo_runtime_artifacts.sh")
    return subprocess.run(["bash", script.as_posix()], capture_output=True, text=True, check=False)


def test_repo_runtime_artifact_check_script_passes_on_clean_tree() -> None:
    with _repo_artifact_check_lock():
        proc = _run_artifact_check_unlocked()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "no repo-local runtime/test artifacts" in (proc.stdout + proc.stderr)


def test_repo_runtime_artifact_check_rejects_repo_local_pytest_workspace() -> None:
    sentinel = Path(".tmp/pytest")
    with _repo_artifact_check_lock():
        try:
            sentinel.mkdir(parents=True, exist_ok=True)
            proc = _run_artifact_check_unlocked()
            assert proc.returncode != 0
            assert ".tmp/pytest" in (proc.stdout + proc.stderr)
        finally:
            shutil.rmtree(Path(".tmp"), ignore_errors=True)


def test_repo_runtime_artifact_check_rejects_generated_research_artifacts() -> None:
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
    with _repo_artifact_check_lock():
        try:
            for path in sentinels:
                path.mkdir(parents=True, exist_ok=True)
            Path("decisions.jsonl").write_text('{"x":1}\n', encoding="utf-8")
            Path("data/paper/derived/research/candidate_events.jsonl").write_text('{"x":1}\n', encoding="utf-8")
            proc = _run_artifact_check_unlocked()
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
    fixture = Path("tests/fixtures/runtime_artifact_policy_allowed.jsonl")
    with _repo_artifact_check_lock():
        try:
            fixture.write_text('{"fixture":true}\n', encoding="utf-8")
            proc = _run_artifact_check_unlocked()
            assert proc.returncode == 0, proc.stdout + proc.stderr
        finally:
            fixture.unlink(missing_ok=True)


def test_repo_runtime_artifact_check_rejects_generated_jsonl_under_fixtures_and_examples() -> None:
    generated = [
        Path("tests/fixtures/decisions.jsonl"),
        Path("examples/executions.jsonl"),
        Path("examples/research/candidate_events.jsonl"),
    ]
    with _repo_artifact_check_lock():
        try:
            for path in generated:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"generated":true}\n', encoding="utf-8")
            proc = _run_artifact_check_unlocked()
            output = proc.stdout + proc.stderr
            assert proc.returncode != 0
            for path in generated:
                assert path.as_posix() in output
        finally:
            for path in generated:
                path.unlink(missing_ok=True)


def test_repo_runtime_artifact_check_allows_narrow_static_example_jsonl() -> None:
    fixture = Path("examples/research/static_fixture_allowed.jsonl")
    with _repo_artifact_check_lock():
        try:
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text('{"fixture":true}\n', encoding="utf-8")
            proc = _run_artifact_check_unlocked()
            assert proc.returncode == 0, proc.stdout + proc.stderr
        finally:
            fixture.unlink(missing_ok=True)
