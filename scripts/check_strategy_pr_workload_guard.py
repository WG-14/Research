#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


REQUIRED_PR_TEMPLATE_TOKENS = (
    "default-fast workload delta",
    "estimated_strategy_runs",
    "research/nightly workload delta",
    "research_e2e",
    "audit_e2e",
    "walk_forward_e2e",
    "parallel_e2e",
    "research_kernel",
    "slow_research",
    "nightly",
    "memory_sensitive",
    "lower-level contract coverage",
    "no default-fast workload delta",
    "builtin_manifest.py",
    "bithumb_bot.strategy_plugins",
    "strategy-plugin-inventory --json",
    "list_research_strategy_plugins()",
    "resolve_research_strategy_plugin()",
    "common execution, risk, data, research, and runtime core paths remain strategy-neutral",
    "strategy level",
    "level_1_research_only",
    "level_2_replay_compatible",
    "level_3_promotion_grade",
    "not_strategy_related",
    "assert_research_only_contract",
    "assert_replay_compatible_contract",
    "assert_live_eligible_contract",
    "architecture_review_required",
    "architecture_review_complete",
)

REQUIRED_AUTHORING_DOC_TOKENS = (
    "Level 1",
    "Level 2",
    "Level 3",
    "estimated_strategy_runs",
    "research/nightly workload delta",
    "full default-fast research matrices",
    "lower-level contract",
    "builtin_manifest.py",
    "entry-point group",
    "STRATEGY_PLUGINS",
    "strategy-plugin-inventory --json",
    "list_research_strategy_plugins()",
    "resolve_research_strategy_plugin()",
    "level_1_research_only",
    "level_2_replay_compatible",
    "level_3_promotion_grade",
    "assert_research_only_contract",
    "assert_replay_compatible_contract",
    "assert_live_eligible_contract",
)

LEVEL_HELPERS = {
    "level_1_research_only": ("assert_research_only_contract",),
    "level_2_replay_compatible": ("assert_replay_compatible_contract",),
    "level_3_promotion_grade": (
        "assert_live_eligible_contract",
        "focused runtime/live gate coverage",
        "equivalent focused runtime/live gate coverage",
    ),
}
LEVEL_TOKENS = tuple(LEVEL_HELPERS) + ("not_strategy_related",)
CORE_PATH_PREFIXES = (
    "src/bithumb_bot/runtime_",
    "src/bithumb_bot/research/",
    "src/bithumb_bot/risk",
    "src/bithumb_bot/execution",
    "src/bithumb_bot/run_loop",
    "src/bithumb_bot/strategy_decision",
    "src/bithumb_bot/runtime_data_provider.py",
)
STRATEGY_PLUGIN_PREFIX = "src/bithumb_bot/strategy_plugins/"
BUILTIN_MANIFEST = "src/bithumb_bot/strategy_plugins/builtin_manifest.py"


def missing_tokens(path: Path, tokens: tuple[str, ...]) -> list[str]:
    text = path.read_text(encoding="utf-8").lower()
    return [token for token in tokens if token.lower() not in text]


def validate_strategy_pr_evidence(
    *,
    changed_files: tuple[str, ...],
    evidence_text: str,
) -> list[str]:
    text = evidence_text.lower()
    normalized_files = tuple(str(path).replace("\\", "/") for path in changed_files)
    violations: list[str] = []
    strategy_related = any(path.startswith(STRATEGY_PLUGIN_PREFIX) for path in normalized_files)
    core_related = any(path.startswith(prefix) for path in normalized_files for prefix in CORE_PATH_PREFIXES)
    if not normalized_files:
        return violations
    declared_levels = [level for level in LEVEL_TOKENS if level in text]
    if strategy_related and not declared_levels:
        violations.append("strategy changes require strategy Level declaration")
    if strategy_related and "not_strategy_related" in declared_levels:
        violations.append("strategy changes cannot be marked not_strategy_related")
    for level, helpers in LEVEL_HELPERS.items():
        if level in text and not any(helper in text for helper in helpers):
            violations.append(f"{level} requires contract helper or equivalent focused test")
    plugin_files = [
        path
        for path in normalized_files
        if path.startswith(STRATEGY_PLUGIN_PREFIX)
        and path.endswith(".py")
        and path != BUILTIN_MANIFEST
        and not path.endswith("_test.py")
    ]
    if plugin_files:
        has_builtin_manifest = BUILTIN_MANIFEST in normalized_files or "builtin_manifest.py" in text
        has_entry_point = "bithumb_bot.strategy_plugins" in text
        if not (has_builtin_manifest or has_entry_point):
            violations.append("strategy plugin changes require built-in manifest or external entry-point evidence")
    if core_related and not (
        "architecture_review_required" in text or "architecture_review_complete" in text
    ):
        violations.append("core runtime/research changes require architecture review marker")
    if "full default-fast research matrix" in text or "full default-fast research matrices added" in text:
        violations.append("default-fast research matrix expansion is not allowed")
    return violations


def _changed_files_from_args(args: argparse.Namespace, repo_root: Path) -> tuple[str, ...]:
    files = list(args.changed_file or ())
    if args.changed_files:
        files.extend(
            line.strip()
            for line in Path(args.changed_files).read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    if files:
        return tuple(files)
    return ()


def _evidence_text_from_args(args: argparse.Namespace, repo_root: Path) -> str:
    if args.evidence_text:
        return str(args.evidence_text)
    if args.evidence_file:
        return Path(args.evidence_file).read_text(encoding="utf-8")
    return (repo_root / ".github" / "pull_request_template.md").read_text(encoding="utf-8")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--changed-file", action="append")
    parser.add_argument("--changed-files")
    parser.add_argument("--evidence-file")
    parser.add_argument("--evidence-text")
    return parser.parse_args(argv)


def main() -> int:
    args = _parse_args(sys.argv[1:])
    repo_root = Path(__file__).resolve().parents[1]
    checks = {
        repo_root / ".github" / "pull_request_template.md": REQUIRED_PR_TEMPLATE_TOKENS,
        repo_root / "docs" / "strategy-plugin-authoring.md": REQUIRED_AUTHORING_DOC_TOKENS,
    }
    violations: list[str] = []
    for path, tokens in checks.items():
        if not path.exists():
            violations.append(f"{path.relative_to(repo_root)} missing")
            continue
        for token in missing_tokens(path, tokens):
            violations.append(f"{path.relative_to(repo_root)} missing required strategy workload guard text: {token}")
    changed_files = _changed_files_from_args(args, repo_root)
    evidence_text = _evidence_text_from_args(args, repo_root)
    violations.extend(
        validate_strategy_pr_evidence(
            changed_files=changed_files,
            evidence_text=evidence_text,
        )
    )
    if violations:
        print("strategy PR workload guard violations:", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1
    print("strategy PR workload guard: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
