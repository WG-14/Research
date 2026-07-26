#!/usr/bin/env python3
"""Validate or regenerate the executable multi-asset responsibility map."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from market_research.research.multi_asset.architecture_manifest import (  # noqa: E402
    ArchitectureManifestError,
    load_multi_asset_architecture,
    render_embedded_responsibility_section,
    render_responsibility_document,
    validate_multi_asset_architecture,
)


_BEGIN = "<!-- BEGIN GENERATED MULTI-ASSET RESPONSIBILITY MAP -->"
_END = "<!-- END GENERATED MULTI-ASSET RESPONSIBILITY MAP -->"


def _atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_embedded_section(document: str, section: str) -> str:
    start = document.find(_BEGIN)
    end = document.find(_END)
    if start < 0 or end < start:
        raise ArchitectureManifestError("embedded_responsibility_markers_missing")
    return document[:start] + section + document[end + len(_END) :]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate authority, constructor, migration, research-only, and "
            "generated-document contracts for multi-asset research."
        )
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
    )
    parser.add_argument("--manifest-directory", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--write-generated",
        action="store_true",
        help="Regenerate both responsibility-map views before validation.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    project_root = args.project_root.resolve()
    try:
        architecture = load_multi_asset_architecture(args.manifest_directory)
        if args.write_generated:
            generated = project_root / architecture.boundaries.generated_document
            _atomic_write(generated, render_responsibility_document(architecture))
            main_doc = project_root / "docs" / "multi-asset-research.md"
            updated = _replace_embedded_section(
                main_doc.read_text(encoding="utf-8"),
                render_embedded_responsibility_section(architecture),
            )
            _atomic_write(main_doc, updated)
        report = validate_multi_asset_architecture(
            project_root,
            manifest_directory=args.manifest_directory,
        )
    except ArchitectureManifestError as exc:
        payload: dict[str, object] = {
            "schema_version": 1,
            "status": "FAIL",
            "finding_count": 1,
            "findings": [
                {
                    "code": "architecture_manifest_invalid",
                    "detail": str(exc),
                }
            ],
        }
        if args.json:
            print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        else:
            print(f"FAIL architecture_manifest_invalid: {exc}")
        return 1
    if args.json:
        print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
    else:
        print(
            f"{'PASS' if report.ok else 'FAIL'} "
            f"modules={len(report.discovered_modules)} "
            f"findings={len(report.findings)} "
            f"architecture={report.architecture_hash}"
        )
        for finding in report.findings:
            print(
                f"{finding.code}: {finding.module}:{finding.line}:"
                f"{finding.symbol}: {finding.detail}"
            )
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
