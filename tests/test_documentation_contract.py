from __future__ import annotations

from pathlib import Path

from tools.check_documentation import (
    check_documentation,
    check_markdown_file,
    repository_markdown_files,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_tracked_markdown_local_links_resolve() -> None:
    assert check_documentation(root=REPOSITORY_ROOT) == ()


def test_documentation_checker_rejects_missing_and_escaping_links(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    docs = root / "docs"
    docs.mkdir(parents=True)
    source = docs / "guide.md"
    source.write_text(
        "\n".join(
            (
                "[missing](missing.md)",
                "[escape](../../outside.md)",
                "[external](https://example.invalid/document)",
                "[anchor](#local-heading)",
            )
        ),
        encoding="utf-8",
    )

    failures = check_markdown_file(source, root=root)

    assert [failure.reason for failure in failures] == [
        "local_link_target_missing",
        "local_link_escapes_repository",
    ]


def test_documentation_discovery_includes_untracked_markdown(tmp_path: Path) -> None:
    report = tmp_path / "docs" / "new-review.md"
    report.parent.mkdir(parents=True)
    report.write_text("# New review\n", encoding="utf-8")

    assert repository_markdown_files(tmp_path) == (report,)


def test_documentation_checker_requires_operator_docs_and_commands(
    tmp_path: Path,
) -> None:
    failures = check_documentation(root=tmp_path)

    assert "required_document_missing" in {failure.reason for failure in failures}

    for relative in (
        "docs/dataset_artifact_legacy_policy.md",
        "docs/derivative-research.md",
        "docs/internal-web-architecture.md",
        "docs/investment-research-platform.md",
        "docs/research-platform-completeness-review.md",
        "docs/research-platform-evaluation-matrix.json",
        "docs/research-platform-full-scope-evaluation-matrix.json",
        "docs/research-platform-full-scope-review.md",
        "docs/research-data-dictionary.md",
        "services/research_operations/README.md",
        "services/research_operations/docs/runbook.md",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Required\n", encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text(
        "\n".join(
            (
                "scripts/platform test-all",
                "scripts/platform lint",
                "scripts/platform typecheck",
                "scripts/platform compile",
                "scripts/platform docs-check",
                "scripts/platform verify-complete",
                "scripts/platform backup-restore-drill",
                "scripts/platform research research-reproduce-run",
                "docs/research-data-dictionary.md",
                "../ResearchOperations",
            )
        ),
        encoding="utf-8",
    )

    failures = check_documentation(root=tmp_path)

    assert [failure.reason for failure in failures] == ["forbidden_legacy_reference"]
