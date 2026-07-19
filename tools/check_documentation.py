#!/usr/bin/env python3
"""Fail when repository documentation or documented commands drift.

The checker is intentionally dependency-free so CI and release workstations can
run it before installing the platform. External URLs and in-document anchors
are outside its authority; repository-local links must resolve inside the
checkout. Required operator documents and command references must remain
present, and retired sibling-repository spellings may not reappear.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MARKDOWN_LINK = re.compile(
    r"!?\[[^\]]*\]\((?P<target><[^>]+>|[^)\s]+)(?:\s+['\"][^)]*['\"])?\)"
)
_EXTERNAL_SCHEMES = (
    "http://",
    "https://",
    "mailto:",
    "app://",
    "data:",
)
_DOCUMENTATION_ROOTS = (
    Path("README.md"),
    Path("AGENTS.md"),
    Path("docs"),
    Path("apps/internal_web"),
    Path("services/research_operations"),
)
_REQUIRED_DOCUMENTS = (
    Path("README.md"),
    Path("docs/dataset_artifact_legacy_policy.md"),
    Path("docs/internal-web-architecture.md"),
    Path("docs/investment-research-platform.md"),
    Path("docs/research-platform-completeness-review.md"),
    Path("docs/research-platform-evaluation-matrix.json"),
    Path("docs/research-data-dictionary.md"),
    Path("services/research_operations/README.md"),
    Path("services/research_operations/docs/runbook.md"),
)
_REQUIRED_COMMAND_REFERENCES = {
    Path("README.md"): (
        "scripts/platform test-all",
        "scripts/platform lint",
        "scripts/platform typecheck",
        "scripts/platform compile",
        "scripts/platform docs-check",
        "scripts/platform verify-complete",
        "scripts/platform backup-restore-drill",
        "scripts/platform research research-reproduce-run",
        "docs/research-data-dictionary.md",
    ),
}
_FORBIDDEN_LEGACY_REFERENCES = (
    "/home/vorac/work/ResearchOperations",
    "ResearchOperations/",
    "../ResearchOperations",
)


@dataclass(frozen=True, slots=True)
class BrokenLink:
    source: Path
    line: int
    target: str
    reason: str

    def render(self) -> str:
        return f"{self.source.as_posix()}:{self.line}: {self.reason}: {self.target}"


def repository_markdown_files(root: Path = PROJECT_ROOT) -> tuple[Path, ...]:
    """Return the documentation surface, including newly added Markdown.

    Relying on ``git ls-files`` made the checker blind to a new report until it
    was staged. Restricting discovery to the owned documentation roots keeps
    virtual environments and generated artifacts out without that blind spot.
    """

    values: set[Path] = set()
    for relative in _DOCUMENTATION_ROOTS:
        candidate = root / relative
        if candidate.is_file() and candidate.suffix.casefold() == ".md":
            values.add(candidate)
        elif candidate.is_dir():
            values.update(
                path
                for path in candidate.rglob("*.md")
                if path.is_file()
                and not any(
                    part.startswith(".") or part in {"node_modules", "__pycache__"}
                    for part in path.relative_to(candidate).parts[:-1]
                )
            )
    return tuple(sorted(values))


def tracked_markdown_files(root: Path = PROJECT_ROOT) -> tuple[Path, ...]:
    """Compatibility alias for callers of the original checker API."""

    return repository_markdown_files(root)


def check_markdown_file(path: Path, *, root: Path = PROJECT_ROOT) -> list[BrokenLink]:
    root = root.resolve()
    source = path.resolve()
    try:
        source_label = source.relative_to(root)
    except ValueError:
        source_label = source
    failures: list[BrokenLink] = []
    for line_number, line in enumerate(
        source.read_text(encoding="utf-8").splitlines(), start=1
    ):
        for match in _MARKDOWN_LINK.finditer(line):
            raw_target = match.group("target").strip("<>")
            if (
                not raw_target
                or raw_target.startswith("#")
                or raw_target.lower().startswith(_EXTERNAL_SCHEMES)
            ):
                continue
            target_without_anchor = unquote(raw_target.split("#", 1)[0])
            if not target_without_anchor:
                continue
            candidate = (
                root / target_without_anchor.lstrip("/")
                if target_without_anchor.startswith("/")
                else source.parent / target_without_anchor
            ).resolve(strict=False)
            try:
                candidate.relative_to(root)
            except ValueError:
                failures.append(
                    BrokenLink(
                        source=source_label,
                        line=line_number,
                        target=raw_target,
                        reason="local_link_escapes_repository",
                    )
                )
                continue
            if not candidate.exists():
                failures.append(
                    BrokenLink(
                        source=source_label,
                        line=line_number,
                        target=raw_target,
                        reason="local_link_target_missing",
                    )
                )
    return failures


def check_documentation(
    paths: tuple[Path, ...] | None = None,
    *,
    root: Path = PROJECT_ROOT,
) -> tuple[BrokenLink, ...]:
    candidates = paths if paths is not None else repository_markdown_files(root)
    failures = [
        failure
        for path in candidates
        for failure in check_markdown_file(path, root=root)
    ]
    if paths is not None:
        return tuple(failures)

    for relative in _REQUIRED_DOCUMENTS:
        if not (root / relative).is_file():
            failures.append(
                BrokenLink(
                    relative, 0, relative.as_posix(), "required_document_missing"
                )
            )
    for relative, snippets in _REQUIRED_COMMAND_REFERENCES.items():
        document = root / relative
        payload = document.read_text(encoding="utf-8") if document.is_file() else ""
        for snippet in snippets:
            if snippet not in payload:
                failures.append(
                    BrokenLink(
                        relative,
                        0,
                        snippet,
                        "required_command_reference_missing",
                    )
                )
    for path in candidates:
        payload = path.read_text(encoding="utf-8")
        source = path.resolve().relative_to(root.resolve())
        for forbidden in _FORBIDDEN_LEGACY_REFERENCES:
            if forbidden in payload:
                failures.append(
                    BrokenLink(source, 0, forbidden, "forbidden_legacy_reference")
                )
    return tuple(failures)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check repository documentation links and required contracts."
    )
    parser.add_argument("paths", nargs="*", help="Optional Markdown files to check")
    args = parser.parse_args(argv)
    paths = tuple(Path(value) for value in args.paths) if args.paths else None
    failures = check_documentation(paths)
    for failure in failures:
        print(failure.render())
    if failures:
        print(f"documentation check failed: {len(failures)} broken local link(s)")
        return 1
    print("documentation check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
