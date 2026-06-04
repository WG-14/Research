#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = PROJECT_ROOT / "docs" / "config-reference.md"
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bithumb_bot.config_spec import CONFIG_SCHEMA_VERSION, ENV_SPECS, config_spec_hash  # noqa: E402


def render_config_reference() -> str:
    lines = [
        "# Configuration Reference",
        "",
        "This file is generated from `src/bithumb_bot/config_spec.py`.",
        f"Schema version: `{CONFIG_SCHEMA_VERSION}`",
        f"Spec hash: `{config_spec_hash()}`",
        "",
        "| Name | Type | Scope | Default | Live required | Secret | Deprecated/Ignored | Safety | Validation | Description |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for spec in sorted(ENV_SPECS, key=lambda item: item.name):
        deprecated = []
        if spec.deprecated:
            deprecated.append("deprecated")
        if spec.ignored:
            deprecated.append("ignored")
        default = spec.default or (f"<{spec.default_resolver}>" if spec.default_resolver else "")
        validation_parts = []
        if spec.validation_kind:
            validation_parts.append(spec.validation_kind)
        if spec.min_live_bytes is not None:
            validation_parts.append(f"min_live_bytes={spec.min_live_bytes}")
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{spec.name}`",
                    spec.value_type,
                    spec.mode_scope,
                    f"`{default}`" if default else "",
                    "yes" if spec.required_in_live else "no",
                    "yes" if spec.secret else "no",
                    ", ".join(deprecated) or "no",
                    spec.safety_tier,
                    ", ".join(validation_parts) or "",
                    spec.description.replace("|", "/"),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate or verify docs/config-reference.md.")
    parser.add_argument("--check", action="store_true", help="Fail if the checked-in file is out of date.")
    args = parser.parse_args(argv)

    rendered = render_config_reference()
    if args.check:
        current = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else ""
        if current != rendered:
            print("docs/config-reference.md is out of sync; run tools/generate_config_docs.py", file=sys.stderr)
            return 1
        print("config reference is in sync")
        return 0
    DOC_PATH.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"wrote {DOC_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
