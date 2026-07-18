from __future__ import annotations

import os
import stat
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "internal_web"
OPERATIONS = ROOT / "services" / "research_operations"


def _toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_uv_workspace_has_one_lock_and_explicit_package_members() -> None:
    root_project = _toml(ROOT / "pyproject.toml")
    assert root_project["tool"]["uv"]["workspace"]["members"] == [
        "apps/internal_web",
        "services/research_operations",
    ]

    assert (ROOT / "uv.lock").is_file()
    assert not (WEB / "uv.lock").exists()
    assert not (OPERATIONS / "uv.lock").exists()

    locked = _toml(ROOT / "uv.lock")
    packages = {package["name"]: package for package in locked["package"]}
    assert packages["market-research"]["source"] == {"editable": "."}
    assert packages["market-research-internal-web"]["source"] == {
        "editable": "apps/internal_web"
    }
    assert packages["research-operations"]["source"] == {
        "editable": "services/research_operations"
    }


def test_workspace_dependencies_do_not_use_machine_or_sibling_paths() -> None:
    web_project = _toml(WEB / "pyproject.toml")
    operations_project = _toml(OPERATIONS / "pyproject.toml")

    assert web_project["tool"]["uv"]["sources"] == {
        "market-research": {"workspace": True}
    }
    assert operations_project["tool"]["uv"]["sources"] == {
        "market-research": {"workspace": True},
        "market-research-internal-web": {"workspace": True},
    }

    root_dependencies = set(_toml(ROOT / "pyproject.toml")["project"]["dependencies"])
    assert not any("django" in dependency.lower() for dependency in root_dependencies)


def test_container_has_one_canonical_monorepo_dockerfile() -> None:
    dockerfile = OPERATIONS / "Dockerfile"
    content = dockerfile.read_text(encoding="utf-8")

    assert dockerfile.is_file()
    assert not (OPERATIONS / "deploy" / "Dockerfile").exists()
    assert "ARG UV_PYTHON_IMAGE" in content
    assert "COPY . /opt/Research" in content
    assert "uv sync --frozen --all-packages --no-dev --no-editable" in content
    assert "RESEARCH_OPS_SOURCE_ROOT=/opt/Research" in content
    for legacy_reference in (
        "COPY Research ",
        "COPY ResearchOperations ",
        "/home/vorac",
        "../Research",
    ):
        assert legacy_reference not in content


def test_docker_context_excludes_runtime_and_secret_material() -> None:
    patterns = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert {
        ".git",
        ".venv*",
        ".env",
        ".env.*",
        "**/runtime.env",
        "**/secrets/**",
        "**/pki/**",
        "**/*.key",
        "**/*.pem",
        "data/**",
        "snapshots/**",
        "deploy/systemd/rendered/**",
        "**/*.sqlite",
        "**/*.dump",
        "**/*.backup",
    } <= patterns


def test_build_image_script_uses_root_context_and_digest_pinned_uv_image(
    tmp_path: Path,
) -> None:
    fake_docker = tmp_path / "docker"
    log = tmp_path / "docker.log"
    fake_docker.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$DOCKER_LOG"\n'
        "case \"$1\" in image) printf 'example@sha256:%064d\\n' 0 ;; esac\n",
        encoding="utf-8",
    )
    fake_docker.chmod(fake_docker.stat().st_mode | stat.S_IXUSR)
    script = OPERATIONS / "scripts" / "build-image.sh"
    environment = {
        **os.environ,
        "DOCKER_LOG": str(log),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "UV_PYTHON_IMAGE": "registry.invalid/uv@sha256:" + "a" * 64,
        "OUTPUT_IMAGE": "research-platform:test",
    }

    subprocess.run(["sh", str(script)], env=environment, check=True)
    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    assert f"--file {OPERATIONS / 'Dockerfile'}" in calls[0]
    assert calls[0].endswith(str(ROOT))
    assert "UV_PYTHON_IMAGE=registry.invalid/uv@sha256:" + "a" * 64 in calls[0]
    assert calls[1].startswith("image inspect ")
    assert "{{.Id}}" in calls[1]

    environment["UV_PYTHON_IMAGE"] = "registry.invalid/uv:latest"
    rejected = subprocess.run(["sh", str(script)], env=environment, check=False)
    assert rejected.returncode == 64
    assert log.read_text(encoding="utf-8").splitlines() == calls


def test_release_build_and_native_install_are_provenance_bound_wheels() -> None:
    root_project = _toml(ROOT / "pyproject.toml")
    web_project = _toml(WEB / "pyproject.toml")
    assert root_project["tool"]["setuptools"]["package-data"] == {
        "market_research": [
            "_build_provenance.json",
            "builtin_strategies/*.strategy.json",
        ]
    }
    assert web_project["tool"]["setuptools"]["package-data"]["market_research_web"] == [
        "_build_provenance.json"
    ]
    operations_ignore = (OPERATIONS / ".gitignore").read_text(encoding="utf-8")
    assert "runtime.env" in operations_ignore
    assert "*.pem" in operations_ignore

    platform = (ROOT / "scripts" / "platform").read_text(encoding="utf-8")
    assert "tools/build_release_artifacts.py" in platform
    assert "tools/install_release.py" in platform
    assert "uv build --all-packages --out-dir dist/platform" not in platform
    assert ': "${PYTHONHASHSEED:=0}"' in platform
    assert "uv run --frozen --package market-research market-research" in platform
    for thread_variable in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "BLIS_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        assert f"{thread_variable}=1" in platform
        assert f"${{{thread_variable}:=" not in platform
    installer = (ROOT / "tools" / "install_release.py").read_text(encoding="utf-8")
    assert '"--no-emit-workspace"' in installer
    assert '"--no-deps"' in installer
    assert '"--require-hashes"' in installer
    assert "ensure_clean_checkout" in installer
    verifier = (ROOT / "tools" / "verify_installed_release.py").read_text(
        encoding="utf-8"
    )
    assert "installed_distribution_not_direct_wheel" in verifier
    assert "installed_source_digest_mismatch" in verifier
    assert "installed_research_provenance_invalid" in verifier

    native = (OPERATIONS / "deploy" / "native" / "README.md").read_text(
        encoding="utf-8"
    )
    assert "scripts/platform install-release" in native
    assert "three exact manifest-bound wheels with `--no-deps`" in native
    assert "`pip install -e`" in native
    assert "`uv sync`" in native


def test_platform_launcher_overrides_hostile_numeric_thread_environment(
    tmp_path: Path,
) -> None:
    fake_uv = tmp_path / "uv"
    environment_log = tmp_path / "environment.log"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$PYTHONHASHSEED,$OMP_NUM_THREADS,"
        "$OPENBLAS_NUM_THREADS,$MKL_NUM_THREADS,$NUMEXPR_NUM_THREADS,"
        '$BLIS_NUM_THREADS,$VECLIB_MAXIMUM_THREADS" > "$PLATFORM_ENV_LOG"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(fake_uv.stat().st_mode | stat.S_IXUSR)
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "PLATFORM_ENV_LOG": str(environment_log),
        "PYTHONHASHSEED": "17",
        "OMP_NUM_THREADS": "8",
        "OPENBLAS_NUM_THREADS": "8",
        "MKL_NUM_THREADS": "8",
        "NUMEXPR_NUM_THREADS": "8",
        "BLIS_NUM_THREADS": "8",
        "VECLIB_MAXIMUM_THREADS": "8",
    }

    subprocess.run(
        ["sh", str(ROOT / "scripts" / "platform"), "docs-check"],
        env=environment,
        check=True,
    )

    assert environment_log.read_text(encoding="utf-8").strip() == ("17,1,1,1,1,1,1")


def test_platform_research_launcher_uses_frozen_workspace_and_forwards_args(
    tmp_path: Path,
) -> None:
    fake_uv = tmp_path / "uv"
    argv_log = tmp_path / "argv.log"
    fake_uv.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" > "$PLATFORM_ARGV_LOG"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(fake_uv.stat().st_mode | stat.S_IXUSR)
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "PLATFORM_ARGV_LOG": str(argv_log),
    }

    subprocess.run(
        [
            "sh",
            str(ROOT / "scripts" / "platform"),
            "research",
            "research-reproduce-run",
            "--manifest",
            "/abs/manifest.json",
            "--receipt",
            "/abs/receipt.json",
        ],
        env=environment,
        check=True,
    )

    assert argv_log.read_text(encoding="utf-8").strip() == (
        "run --frozen --package market-research market-research "
        "research-reproduce-run --manifest /abs/manifest.json "
        "--receipt /abs/receipt.json"
    )
