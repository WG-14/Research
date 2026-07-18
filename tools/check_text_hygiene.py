#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

TEXT_SUFFIXES = {
    ".cfg",
    ".conf",
    ".css",
    ".env",
    ".example",
    ".html",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".service",
    ".timer",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

TEXT_NAMES = {
    ".env.example",
    ".gitattributes",
    "AGENTS.md",
    "README.md",
}

HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")
REPLACEMENT_RE = re.compile("\ufffd")
QUESTION_RUN_RE = re.compile(r"\?{4,}")
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
KNOWN_MOJIBAKE_RE = re.compile(
    r"(?:"
    r"\?몄|\?ㅽ|됰Ŧ| ш끽|維|뱀땡|얩맪|棺|堉|袁|筌|獄|釉|轅|"
    r"怨|湲|섏|猷|뚯|쑉|媛|곴|컖|紐|낆|떆|"
    r"Ã|Â|ì|í|ê|ë|ð|占"
    r")"
)


@dataclass(frozen=True)
class Violation:
    path: Path
    line_no: int
    kind: str
    text: str

    def render(self) -> str:
        rel = self.path.as_posix()
        location = f"{rel}:{self.line_no}" if self.line_no else rel
        snippet = self.text.strip()
        if len(snippet) > 180:
            snippet = snippet[:177] + "..."
        return f"{location}: {self.kind}: {snippet}"


def tracked_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [
        PROJECT_ROOT / line for line in completed.stdout.splitlines() if line.strip()
    ]


def is_relevant_text_file(path: Path) -> bool:
    try:
        rel_name = path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        rel_name = path.as_posix()
    if path.name in TEXT_NAMES or rel_name in TEXT_NAMES:
        return True
    if path.suffix.lower() in TEXT_SUFFIXES:
        return True
    if ".env" in path.name:
        return True
    return False


def _is_allowed_technical_identifier(path: Path, line: str) -> bool:
    stripped = line.strip()
    if path.name == "check_text_hygiene.py" and stripped.startswith('r"'):
        # Regex fragments inside this scanner intentionally contain the byte-
        # pattern text that the scanner must detect elsewhere.
        return True
    return (
        "korean_name" in stripped
        and not HANGUL_RE.search(stripped)
        and not CJK_RE.search(stripped)
        and not QUESTION_RUN_RE.search(stripped)
        and not KNOWN_MOJIBAKE_RE.search(stripped)
    )


def _allows_user_work_language(path: Path) -> bool:
    """Allow the authenticated Web UI to use its users' working language."""

    try:
        relative = path.resolve().relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return False
    return relative.parts[:3] in {
        ("apps", "internal_web", "src"),
        ("apps", "internal_web", "tests"),
    }


def _is_sha256_shell_glob(path: Path, line: str) -> bool:
    """Recognize the deliberate 64-character digest glob in build-image.sh."""

    try:
        relative = path.resolve().relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return False
    stripped = line.strip()
    return (
        relative == Path("services/research_operations/scripts/build-image.sh")
        and stripped.startswith('case "$UV_PYTHON_IMAGE" in *@sha256:')
        and stripped.count("?") == 64
    )


def scan_file(path: Path) -> list[Violation]:
    if not is_relevant_text_file(path):
        return []
    try:
        rel = path.relative_to(PROJECT_ROOT)
    except ValueError:
        rel = path
    data = path.read_bytes()
    violations: list[Violation] = []
    if data.startswith(b"\xef\xbb\xbf"):
        violations.append(Violation(rel, 0, "utf8_bom", "file starts with UTF-8 BOM"))
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        violations.append(Violation(rel, 0, "utf8_decode_error", str(exc)))
        return violations

    for index, line in enumerate(text.splitlines(), start=1):
        if _is_allowed_technical_identifier(path, line):
            continue
        if HANGUL_RE.search(line) and not _allows_user_work_language(path):
            violations.append(Violation(rel, index, "hangul", line))
        if REPLACEMENT_RE.search(line):
            violations.append(Violation(rel, index, "replacement_character", line))
        if QUESTION_RUN_RE.search(line) and not _is_sha256_shell_glob(path, line):
            violations.append(Violation(rel, index, "suspicious_question_run", line))
        if KNOWN_MOJIBAKE_RE.search(line):
            violations.append(Violation(rel, index, "known_mojibake", line))
        if path.suffix == ".py" and CJK_RE.search(line):
            violations.append(Violation(rel, index, "cjk_in_python_text", line))
    return violations


def scan_paths(paths: list[Path] | None = None) -> list[Violation]:
    candidates = paths if paths is not None else tracked_files()
    violations: list[Violation] = []
    for path in candidates:
        full_path = path if path.is_absolute() else PROJECT_ROOT / path
        if full_path.is_file():
            violations.extend(scan_file(full_path))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check tracked text files for language/mojibake hygiene."
    )
    parser.add_argument(
        "paths", nargs="*", help="Optional paths to scan instead of all tracked files."
    )
    args = parser.parse_args(argv)

    paths = [Path(item) for item in args.paths] if args.paths else None
    violations = scan_paths(paths)
    if violations:
        for violation in violations:
            print(violation.render(), file=sys.stderr)
        print(f"text hygiene failed: {len(violations)} violation(s)", file=sys.stderr)
        return 1
    print("text hygiene passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
