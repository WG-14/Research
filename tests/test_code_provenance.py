from pathlib import Path
import subprocess
from types import SimpleNamespace

import market_research.research.code_provenance as code_provenance_module
from market_research.research.code_provenance import (
    CODE_PROVENANCE_SCHEMA_VERSION,
    REPOSITORY_DEPENDENCY_CONTRACT_BASIS,
    RESOLVED_DEPENDENCY_CONTENT_IDENTITY_BASIS,
    collect_code_provenance,
)


def test_source_and_dependency_changes_alter_code_provenance(tmp_path: Path) -> None:
    source = tmp_path / "src" / "package" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='fixture'\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    first = collect_code_provenance(tmp_path)
    source.write_text("VALUE = 2\n", encoding="utf-8")
    second = collect_code_provenance(tmp_path)
    (tmp_path / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    third = collect_code_provenance(tmp_path)

    assert first["source_tree_hash"] != second["source_tree_hash"]
    assert second["dependency_contract_hash"] != third["dependency_contract_hash"]
    assert (
        first["code_provenance_hash"]
        != second["code_provenance_hash"]
        != third["code_provenance_hash"]
    )
    assert first["git_available"] is False


def test_repository_provenance_binds_resolved_version_with_unchanged_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "src" / "package" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='fixture'\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        code_provenance_module,
        "_installed_distribution_rows",
        lambda: [
            {
                "name": "resolved-package",
                "version": "1.0",
                "content_hash": "sha256:" + "1" * 64,
                "file_count": 1,
            }
        ],
    )

    first = collect_code_provenance(tmp_path)
    monkeypatch.setattr(
        code_provenance_module,
        "_installed_distribution_rows",
        lambda: [
            {
                "name": "resolved-package",
                "version": "2.0",
                "content_hash": "sha256:" + "1" * 64,
                "file_count": 1,
            }
        ],
    )
    second = collect_code_provenance(tmp_path)

    assert first["dependency_contract_basis"] == REPOSITORY_DEPENDENCY_CONTRACT_BASIS
    assert (
        first["declared_dependency_contract_hash"]
        == second["declared_dependency_contract_hash"]
    )
    assert (
        first["resolved_dependency_contract_hash"]
        != second["resolved_dependency_contract_hash"]
    )
    assert (
        first["resolved_dependency_distribution_identities"]
        != second["resolved_dependency_distribution_identities"]
    )
    assert first["dependency_contract_hash"] != second["dependency_contract_hash"]
    assert first["code_provenance_hash"] != second["code_provenance_hash"]


def test_repository_provenance_binds_same_version_installed_content(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "src" / "package" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='fixture'\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    installed_file = tmp_path / "installed" / "dependency" / "module.py"
    installed_file.parent.mkdir(parents=True)
    installed_file.write_text("VALUE = 1\n", encoding="utf-8")

    class FakePackagePath:
        hash = SimpleNamespace(mode="sha256", value="recorded-hash")
        size = len("VALUE = 1\n")
        suffix = ".py"
        name = "module.py"

        @staticmethod
        def as_posix() -> str:
            return "dependency/module.py"

    class FakeDistribution:
        metadata = {"Name": "same-version-dependency"}
        version = "1.0"
        files = (FakePackagePath(),)

        @staticmethod
        def locate_file(_package_path) -> Path:
            return installed_file

    monkeypatch.setattr(
        code_provenance_module.importlib.metadata,
        "distributions",
        lambda: [FakeDistribution()],
    )

    first = collect_code_provenance(tmp_path)
    installed_file.write_text("VALUE = 2\n", encoding="utf-8")
    second = collect_code_provenance(tmp_path)

    assert (
        first["resolved_dependency_content_identity_basis"]
        == RESOLVED_DEPENDENCY_CONTENT_IDENTITY_BASIS
    )
    assert (
        first["declared_dependency_contract_hash"]
        == second["declared_dependency_contract_hash"]
    )
    assert (
        first["resolved_dependency_contract_hash"]
        != second["resolved_dependency_contract_hash"]
    )
    assert first["dependency_contract_hash"] != second["dependency_contract_hash"]
    assert first["code_provenance_hash"] != second["code_provenance_hash"]


def test_git_provenance_distinguishes_clean_and_dirty_worktrees(tmp_path: Path) -> None:
    source = tmp_path / "src" / "package" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='fixture'\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Research Test",
            "-c",
            "user.email=research-test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=tmp_path,
        check=True,
    )

    clean = collect_code_provenance(tmp_path)
    source.write_text("VALUE = 2\n", encoding="utf-8")
    dirty = collect_code_provenance(tmp_path)

    assert clean["git_available"] is True
    assert clean["git_dirty"] is False
    assert dirty["git_available"] is True
    assert dirty["git_dirty"] is True
    assert clean["git_status_hash"] != dirty["git_status_hash"]
    assert clean["git_diff_hash"] != dirty["git_diff_hash"]
    assert clean["code_provenance_hash"] != dirty["code_provenance_hash"]


def test_repository_provenance_records_actual_git_and_source_state() -> None:
    root = Path(__file__).resolve().parents[1]
    provenance = collect_code_provenance(root)
    actual_git_status = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        text=True,
    ).strip()

    assert len(provenance["git_commit"]) == 40
    assert all(char in "0123456789abcdef" for char in provenance["git_commit"])
    assert provenance["git_dirty"] is bool(actual_git_status)
    assert provenance["git_diff_hash"].startswith("sha256:")
    assert provenance["source_tree_hash"].startswith("sha256:")
    assert provenance["declared_dependency_contract_hash"].startswith("sha256:")
    assert provenance["resolved_dependency_contract_hash"].startswith("sha256:")
    assert provenance["resolved_dependency_distribution_identities"]
    assert all(
        set(row) == {"name", "version", "content_hash", "file_count"}
        for row in provenance["resolved_dependency_distribution_identities"]
    )
    assert (
        provenance["resolved_dependency_content_identity_basis"]
        == RESOLVED_DEPENDENCY_CONTENT_IDENTITY_BASIS
    )
    assert provenance["dependency_contract_hash"].startswith("sha256:")
    assert provenance["code_provenance_hash"].startswith("sha256:")


def test_installed_distribution_fallback_never_hashes_empty_code_or_dependencies(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime-without-source-checkout"
    runtime_root.mkdir()

    provenance = collect_code_provenance(runtime_root)

    assert provenance["schema_version"] == CODE_PROVENANCE_SCHEMA_VERSION
    assert provenance["source_layout"] == "installed_distribution"
    assert provenance["source_file_count"] > 0
    assert provenance["source_tree_hash"].startswith("sha256:")
    assert provenance["dependency_contract_basis"] == (
        "resolved_installed_distributions"
    )
    assert provenance["dependency_contract_files"]
    assert provenance["declared_dependency_contract_hash"] is None
    assert provenance["resolved_dependency_contract_hash"].startswith("sha256:")
    assert provenance["resolved_dependency_distribution_identities"]
    assert (
        provenance["resolved_dependency_content_identity_basis"]
        == RESOLVED_DEPENDENCY_CONTENT_IDENTITY_BASIS
    )
    assert provenance["dependency_contract_hash"].startswith("sha256:")
    assert provenance["git_available"] is False
    assert provenance["git_commit"] == "unknown"
