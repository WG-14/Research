from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from tests.research_sma_success_fixture import create_success_fixture


ROOT = Path(__file__).resolve().parents[1]


def _run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str] | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert completed.returncode == 0, (
        f"command failed: {command!r}\nstdout:\n{completed.stdout[-4000:]}"
        f"\nstderr:\n{completed.stderr[-4000:]}"
    )
    return completed


def _cold_environment(root: Path, venv: Path) -> dict[str, str]:
    state = root / "state"
    home = root / "home"
    for path in (
        root / "cwd",
        home,
        state / "data",
        state / "artifacts",
        state / "reports",
        state / "cache",
        state / "identity",
    ):
        path.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "PATH": os.pathsep.join((str(venv / "bin"), "/usr/bin", "/bin")),
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "",
        "PYTHONHASHSEED": "0",
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "LC_NUMERIC": "C",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "BLIS_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
        "RESEARCH_DATA_ROOT": str(state / "data"),
        "RESEARCH_ARTIFACT_ROOT": str(state / "artifacts"),
        "RESEARCH_REPORT_ROOT": str(state / "reports"),
        "RESEARCH_CACHE_ROOT": str(state / "cache"),
        "RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH": str(
            state / "identity" / "experiment-identities.jsonl"
        ),
        "RESEARCH_FINAL_HOLDOUT_REGISTRY_PATH": str(
            state / "identity" / "final-holdout.jsonl"
        ),
    }


def _payload(completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
    lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    assert lines, completed.stdout[-4000:]
    value = json.loads(lines[-1])
    assert isinstance(value, dict)
    return value


def test_noneditable_wheel_build_verify_and_two_cold_replays(
    tmp_path: Path,
) -> None:
    """Exercise the production CLI without checkout imports or preexisting state."""

    uv = shutil.which("uv")
    assert uv is not None
    source_input = tmp_path / "external-prepared-input"
    source_input.mkdir()
    _db_path, manifest_path = create_success_fixture(source_input)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact_manifest_path = Path(
        manifest["dataset"]["artifact_manifest_uri"]
    )
    artifact_manifest = json.loads(
        artifact_manifest_path.read_text(encoding="utf-8")
    )
    dataset_path = Path(artifact_manifest["artifact"]["uri"])

    wheels = tmp_path / "wheels"
    wheels.mkdir()
    _run(
        [
            uv,
            "build",
            "--offline",
            "--wheel",
            "--package",
            "market-research",
            "--out-dir",
            str(wheels),
            "--no-create-gitignore",
        ],
        cwd=ROOT,
    )
    wheel_rows = list(wheels.glob("market_research-*.whl"))
    assert len(wheel_rows) == 1
    wheel = wheel_rows[0]
    constraints = tmp_path / "locked-runtime-requirements.txt"
    _run(
        [
            uv,
            "export",
            "--offline",
            "--frozen",
            "--package",
            "market-research",
            "--no-dev",
            "--no-hashes",
            "--no-emit-project",
            "--output-file",
            str(constraints),
        ],
        cwd=ROOT,
    )
    venv = tmp_path / "venv"
    _run([uv, "venv", "--offline", "--python", sys.executable, str(venv)], cwd=tmp_path)
    _run(
        [
            uv,
            "pip",
            "install",
            "--offline",
            "--python",
            str(venv / "bin" / "python"),
            "--constraints",
            str(constraints),
            str(wheel),
        ],
        cwd=tmp_path,
    )
    python = venv / "bin" / "python"
    cli = venv / "bin" / "market-research"
    import_probe = _run(
        [
            str(python),
            "-I",
            "-c",
                (
                    "import importlib.metadata,json,market_research,pathlib,sys;"
                    "d=importlib.metadata.distribution('market-research');"
                    "u=json.loads(d.read_text('direct_url.json') or '{}');"
                    "print(json.dumps({'editable': u.get('dir_info', {}).get("
                    "'editable') is True, 'module_origin': str(pathlib.Path("
                    "market_research.__file__).resolve()), 'prefix': sys.prefix}))"
                ),
        ],
        cwd=tmp_path,
    )
    import_identity = json.loads(import_probe.stdout)
    assert import_identity["editable"] is False
    assert Path(import_identity["module_origin"]).is_relative_to(venv)
    assert import_identity["prefix"] == str(venv)
    assert str(ROOT) not in import_identity["module_origin"]

    baseline_root = tmp_path / "baseline"
    baseline_environment = _cold_environment(baseline_root, venv)
    baseline = _run(
        [str(cli), "research-backtest", "--manifest", str(manifest_path)],
        cwd=baseline_root / "cwd",
        environment=baseline_environment,
    )
    assert "\"status\": \"success\"" in baseline.stdout
    report_root = (
        baseline_root
        / "state"
        / "reports"
        / "research"
        / "sma_success_import_boundary"
    )
    result_path = report_root / "backtest_report.json"
    receipt_path = report_root / "reproduction_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    strict = receipt["stable_fingerprint"]["strict_environment"]
    assert strict["source_layout"] == "installed_distribution"
    assert strict["dependency_contract_basis"] == "resolved_installed_distributions"
    assert not any((baseline_root / "cwd").iterdir())
    assert not any((baseline_root / "home").iterdir())
    assert not any((baseline_root / "state" / "cache").iterdir())

    package_root = tmp_path / "package-command"
    package_environment = _cold_environment(package_root, venv)
    package_path = tmp_path / "published" / "sample.mrpkg"
    package = _payload(
        _run(
            [
                str(cli),
                "research-build-portable-package",
                "--result",
                str(result_path),
                "--manifest",
                str(manifest_path),
                "--receipt",
                str(receipt_path),
                "--dataset-mode",
                "external_content_addressed",
                "--out",
                str(package_path),
            ],
            cwd=package_root / "cwd",
            environment=package_environment,
        )
    )
    assert package["replay_eligibility"] == "INSTALLED_WHEEL_COLD_REPLAY_ELIGIBLE"
    assert package["publication_status"] == "NON_PROMOTABLE_RESEARCH_ONLY_PACKAGE"
    with zipfile.ZipFile(package_path, mode="r") as archive:
        assert "dataset/candles.sqlite" not in archive.namelist()

    verification_root = tmp_path / "verification-command"
    verification_environment = _cold_environment(verification_root, venv)
    verification_path = tmp_path / "receipts" / "verification.json"
    verification = _payload(
        _run(
            [
                str(cli),
                "research-verify-portable-package",
                "--package",
                str(package_path),
                "--external-artifact-manifest",
                str(artifact_manifest_path),
                "--external-dataset",
                str(dataset_path),
                "--out",
                str(verification_path),
            ],
            cwd=verification_root / "cwd",
            environment=verification_environment,
        )
    )
    assert verification["status"] == "PASS"

    reproduction_root = tmp_path / "reproduction-command"
    reproduction_environment = _cold_environment(reproduction_root, venv)
    reproduction_path = tmp_path / "receipts" / "reproduction.json"
    reproduction = _payload(
        _run(
            [
                str(cli),
                "research-reproduce-portable-package",
                "--package",
                str(package_path),
                "--external-artifact-manifest",
                str(artifact_manifest_path),
                "--external-dataset",
                str(dataset_path),
                "--workspace",
                str(tmp_path / "replay-workspace"),
                "--out",
                str(reproduction_path),
            ],
            cwd=reproduction_root / "cwd",
            environment=reproduction_environment,
        )
    )
    assert reproduction["status"] == "PASS"
    assert reproduction["cold_run_count"] == 2
    assert len(set(reproduction["actual_fingerprint_hashes"])) == 1
    assert reproduction["actual_fingerprint_hashes"] == [
        reproduction["expected_fingerprint_hash"],
        reproduction["expected_fingerprint_hash"],
    ]
    assert reproduction["source_tree_used"] is False
    assert reproduction["pythonpath_used"] is False
    assert reproduction["cache_preexisting"] is False
    assert reproduction["python_isolated_mode"] is True
    assert len(reproduction["interpreter_probes"]) == 2
    for probe in reproduction["interpreter_probes"]:
        assert probe["distribution_owned_module"] is True
        assert probe["empty_cwd_precondition"] is True
        assert probe["empty_home_precondition"] is True
        assert probe["empty_cache_precondition"] is True
        assert Path(probe["module_origin"]).is_relative_to(venv)
        assert str(ROOT) not in probe["module_origin"]
