from __future__ import annotations

from pathlib import Path

from scripts.check_strategy_pr_workload_guard import (
    REQUIRED_AUTHORING_DOC_TOKENS,
    REQUIRED_PR_TEMPLATE_TOKENS,
    main,
    missing_tokens,
    validate_strategy_pr_evidence,
)


def test_pr_template_contains_strategy_workload_delta_guard() -> None:
    path = Path(".github/pull_request_template.md")

    assert missing_tokens(path, REQUIRED_PR_TEMPLATE_TOKENS) == []


def test_strategy_authoring_docs_keep_workload_delta_and_level_guidance() -> None:
    path = Path("docs/strategy-plugin-authoring.md")

    assert missing_tokens(path, REQUIRED_AUTHORING_DOC_TOKENS) == []


def test_guard_accepts_valid_level_1_builtin_strategy_evidence() -> None:
    evidence = """
    Strategy Level: level_1_research_only
    Registration Path: builtin_manifest
    Built-in Reason: official_example
    Level contract helper or equivalent focused test: assert_research_only_contract
    Built-in manifest: src/bithumb_bot/strategy_plugins/builtin_manifest.py
    Inventory Evidence: strategy-plugin-inventory --json checked
    no default-fast workload delta
    """

    assert validate_strategy_pr_evidence(
        changed_files=(
            "src/bithumb_bot/strategy_plugins/new_research.py",
            "src/bithumb_bot/strategy_plugins/builtin_manifest.py",
            "tests/test_new_research.py",
        ),
        evidence_text=evidence,
    ) == []


def test_guard_accepts_valid_level_2_external_entry_point_evidence() -> None:
    evidence = """
    Strategy Level: level_2_replay_compatible
    Registration Path: external_entry_point
    Entry Point Group: bithumb_bot.strategy_plugins
    Level contract helper or equivalent focused test: assert_replay_compatible_contract
    External registration: bithumb_bot.strategy_plugins
    Inventory Evidence: strategy-plugin-inventory --json checked
    no default-fast workload delta
    """

    assert validate_strategy_pr_evidence(
        changed_files=("src/bithumb_bot/strategy_plugins/new_replay.py",),
        evidence_text=evidence,
    ) == []


def test_guard_accepts_valid_level_3_builtin_strategy_evidence() -> None:
    evidence = """
    Strategy Level: level_3_promotion_grade
    Registration Path: builtin_manifest
    Built-in Reason: canary
    Level contract helper or equivalent focused test: assert_live_eligible_contract
    Built-in manifest: src/bithumb_bot/strategy_plugins/builtin_manifest.py
    Inventory Evidence: strategy-plugin-inventory --json checked
    no default-fast workload delta
    """

    assert validate_strategy_pr_evidence(
        changed_files=(
            "src/bithumb_bot/strategy_plugins/new_live.py",
            "src/bithumb_bot/strategy_plugins/builtin_manifest.py",
        ),
        evidence_text=evidence,
    ) == []


def test_guard_rejects_strategy_plugin_without_level_contract_or_registration() -> None:
    violations = validate_strategy_pr_evidence(
        changed_files=("src/bithumb_bot/strategy_plugins/new_plugin.py",),
        evidence_text="no default-fast workload delta",
    )

    assert "strategy changes require strategy Level declaration" in violations
    assert "strategy plugin changes require built-in manifest or external entry-point evidence" in violations
    assert "strategy plugin changes require Registration Path evidence" in violations
    assert "strategy plugin changes require inventory evidence" in violations


def test_guard_rejects_strategy_plugin_with_level_but_without_registration() -> None:
    violations = validate_strategy_pr_evidence(
        changed_files=("src/bithumb_bot/strategy_plugins/new_plugin.py",),
        evidence_text=(
            "Strategy Level: level_1_research_only\n"
            "Registration Path: builtin_manifest\n"
            "Level contract helper or equivalent focused test: assert_research_only_contract\n"
            "Inventory Evidence: strategy-plugin-inventory --json checked\n"
            "no default-fast workload delta\n"
        ),
    )

    assert violations == [
        "strategy plugin changes require built-in manifest or external entry-point evidence"
    ]


def test_guard_enforces_level_specific_contract_helpers() -> None:
    assert validate_strategy_pr_evidence(
        changed_files=("src/bithumb_bot/strategy_plugins/new_replay.py",),
        evidence_text=(
            "Strategy Level: level_2_replay_compatible\n"
            "bithumb_bot.strategy_plugins\n"
            "Inventory Evidence: strategy-plugin-inventory --json checked\n"
        ),
    ) == ["level_2_replay_compatible requires contract helper or equivalent focused test"]

    assert validate_strategy_pr_evidence(
        changed_files=("src/bithumb_bot/strategy_plugins/new_live.py",),
        evidence_text=(
            "Strategy Level: level_3_promotion_grade\n"
            "equivalent focused runtime/live gate coverage\n"
            "bithumb_bot.strategy_plugins\n"
            "Inventory Evidence: strategy-plugin-inventory --json checked\n"
        ),
    ) == []


def test_guard_requires_architecture_marker_for_core_changes() -> None:
    violations = validate_strategy_pr_evidence(
        changed_files=("src/bithumb_bot/runtime_strategy_set.py",),
        evidence_text="Strategy Level: not_strategy_related",
    )

    assert "core runtime/research changes require architecture review marker" in violations


def test_guard_rejects_accidental_default_fast_matrix_expansion() -> None:
    violations = validate_strategy_pr_evidence(
        changed_files=("src/bithumb_bot/strategy_plugins/new_plugin.py",),
        evidence_text=(
            "Strategy Level: level_1_research_only\n"
            "Registration Path: builtin_manifest\n"
            "Built-in Reason: canary\n"
            "assert_research_only_contract\n"
            "builtin_manifest.py\n"
            "Inventory Evidence: strategy-plugin-inventory --json checked\n"
            "full default-fast research matrices added\n"
        ),
    )

    assert "default-fast research matrix expansion is not allowed" in violations


def test_guard_requires_inventory_evidence_for_strategy_plugin_changes() -> None:
    violations = validate_strategy_pr_evidence(
        changed_files=("src/bithumb_bot/strategy_plugins/new_plugin.py",),
        evidence_text=(
            "Strategy Level: level_1_research_only\n"
            "Registration Path: external_entry_point\n"
            "Entry Point Group: bithumb_bot.strategy_plugins\n"
            "assert_research_only_contract\n"
            "no default-fast workload delta\n"
        ),
    )

    assert violations == ["strategy plugin changes require inventory evidence"]


def test_guard_rejects_strategy_plugin_marked_not_strategy_related() -> None:
    violations = validate_strategy_pr_evidence(
        changed_files=("src/bithumb_bot/strategy_plugins/new_plugin.py",),
        evidence_text=(
            "Strategy Level: not_strategy_related\n"
            "Registration Path: external_entry_point\n"
            "Entry Point Group: bithumb_bot.strategy_plugins\n"
            "Inventory Evidence: strategy-plugin-inventory --json checked\n"
            "no default-fast workload delta\n"
        ),
    )

    assert "strategy changes cannot be marked not_strategy_related" in violations


def test_guard_requires_builtin_reason_for_builtin_strategy() -> None:
    violations = validate_strategy_pr_evidence(
        changed_files=(
            "src/bithumb_bot/strategy_plugins/new_strategy.py",
            "src/bithumb_bot/strategy_plugins/builtin_manifest.py",
        ),
        evidence_text=(
            "Strategy Level: level_1_research_only\n"
            "Registration Path: builtin_manifest\n"
            "assert_research_only_contract\n"
            "Inventory Evidence: strategy-plugin-inventory --json checked\n"
            "no default-fast workload delta\n"
        ),
    )

    assert "built-in strategy changes require valid Built-in Reason" in violations


def test_guard_accepts_external_strategy_without_builtin_manifest() -> None:
    violations = validate_strategy_pr_evidence(
        changed_files=("src/bithumb_bot/strategy_plugins/new_external.py",),
        evidence_text=(
            "Strategy Level: level_2_replay_compatible\n"
            "Registration Path: external_entry_point\n"
            "Entry Point Group: bithumb_bot.strategy_plugins\n"
            "assert_replay_compatible_contract\n"
            "Inventory Evidence: strategy-plugin-inventory --json checked\n"
            "no default-fast workload delta\n"
        ),
    )

    assert violations == []


def test_guard_rejects_external_strategy_that_edits_builtin_manifest_without_reason() -> None:
    violations = validate_strategy_pr_evidence(
        changed_files=(
            "src/bithumb_bot/strategy_plugins/new_external.py",
            "src/bithumb_bot/strategy_plugins/builtin_manifest.py",
        ),
        evidence_text=(
            "Strategy Level: level_2_replay_compatible\n"
            "Registration Path: external_entry_point\n"
            "Entry Point Group: bithumb_bot.strategy_plugins\n"
            "assert_replay_compatible_contract\n"
            "Inventory Evidence: strategy-plugin-inventory --json checked\n"
            "no default-fast workload delta\n"
        ),
    )

    assert "external entry-point strategy changes must not edit built-in manifest" in violations
    assert "built-in strategy changes require valid Built-in Reason" in violations


def test_guard_rejects_invalid_builtin_reason() -> None:
    violations = validate_strategy_pr_evidence(
        changed_files=(
            "src/bithumb_bot/strategy_plugins/new_strategy.py",
            "src/bithumb_bot/strategy_plugins/builtin_manifest.py",
        ),
        evidence_text=(
            "Strategy Level: level_1_research_only\n"
            "Registration Path: builtin_manifest\n"
            "Built-in Reason: random_experiment\n"
            "assert_research_only_contract\n"
            "Inventory Evidence: strategy-plugin-inventory --json checked\n"
            "no default-fast workload delta\n"
        ),
    )

    assert "built-in strategy changes require valid Built-in Reason" in violations


def test_guard_cli_requires_real_evidence_for_explicit_changed_files(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_strategy_pr_workload_guard.py",
            "--require-diff-aware",
            "--changed-file",
            "src/bithumb_bot/strategy_plugins/new_plugin.py",
        ],
    )

    assert main() == 1
    captured = capsys.readouterr()
    assert "strategy changes require strategy Level declaration" in captured.err


def test_guard_cli_local_no_metadata_reports_diff_aware_skip(monkeypatch, capsys) -> None:
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    monkeypatch.delenv("STRATEGY_PR_CHANGED_FILES", raising=False)
    monkeypatch.delenv("STRATEGY_PR_EVIDENCE_TEXT", raising=False)
    monkeypatch.setattr("sys.argv", ["check_strategy_pr_workload_guard.py"])

    assert main() == 0
    captured = capsys.readouterr()
    assert "static docs/templates ok" in captured.out
    assert "diff-aware evidence skipped" in captured.out
