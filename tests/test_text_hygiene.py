from __future__ import annotations

from pathlib import Path

from tools.check_text_hygiene import (
    _allows_user_work_language,
    _is_sha256_shell_glob,
)


ROOT = Path(__file__).resolve().parents[1]


def test_user_work_language_allowance_is_limited_to_internal_web() -> None:
    assert _allows_user_work_language(
        ROOT / "apps/internal_web/src/portal/templates/portal/base.html"
    )
    assert _allows_user_work_language(
        ROOT / "apps/internal_web/tests/test_accessibility_contract.py"
    )
    assert not _allows_user_work_language(ROOT / "src/market_research/settings.py")
    assert not _allows_user_work_language(ROOT / "docs/internal-web-architecture.md")


def test_sha256_shell_glob_allowance_is_exact_and_path_scoped() -> None:
    script = ROOT / "services/research_operations/scripts/build-image.sh"
    digest_glob = next(
        line
        for line in script.read_text(encoding="utf-8").splitlines()
        if 'case "$UV_PYTHON_IMAGE" in *@sha256:' in line
    )

    assert digest_glob.count("?") == 64
    assert _is_sha256_shell_glob(script, digest_glob)
    assert not _is_sha256_shell_glob(script, digest_glob.replace("?", "", 1))
    assert not _is_sha256_shell_glob(ROOT / "scripts/platform", digest_glob)
