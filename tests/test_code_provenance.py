from pathlib import Path
import subprocess

from market_research.research.code_provenance import collect_code_provenance


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
    assert provenance["dependency_contract_hash"].startswith("sha256:")
    assert provenance["code_provenance_hash"].startswith("sha256:")
