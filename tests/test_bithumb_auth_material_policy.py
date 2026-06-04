from __future__ import annotations

import ast
from pathlib import Path

import pytest

from bithumb_bot.config_spec import (
    JWT_HS256_MIN_SECRET_BYTES,
    JWT_HS256_SECRET_VALIDATION_KIND,
    SPEC_BY_NAME,
)
from tests.support.live_auth import TEST_BITHUMB_API_SECRET


pytestmark = pytest.mark.fast_regression

TESTS_ROOT = Path(__file__).resolve().parent

SHORT_SECRET_ALLOWLIST = {
    (
        "test_bithumb_private_api.py",
        "test_private_api_rejects_short_secret_before_jwt_signing",
        "api_secret",
        "s",
    ),
}


def _call_name(node: ast.Call) -> str:
    parts: list[str] = []
    current: ast.AST = node.func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _literal_arg(node: ast.Call, index: int) -> str | None:
    if len(node.args) <= index:
        return None
    arg = node.args[index]
    return arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None


def _short_secret_literal(value: str) -> bool:
    return bool(value) and len(value.encode("utf-8")) < JWT_HS256_MIN_SECRET_BYTES


def _offenders_in_function(path: Path, function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    offenders: list[str] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue

        call_name = _call_name(node)
        candidates: list[tuple[str, str]] = []
        if call_name.endswith("BithumbPrivateAPI"):
            for keyword in node.keywords:
                if keyword.arg == "api_secret" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                    candidates.append(("api_secret", keyword.value.value))
        elif call_name.endswith("object.__setattr__"):
            key = _literal_arg(node, 1)
            value = _literal_arg(node, 2)
            if key == "BITHUMB_API_SECRET" and value is not None:
                candidates.append(("BITHUMB_API_SECRET", value))
        elif call_name.endswith(".setenv"):
            key = _literal_arg(node, 0)
            value = _literal_arg(node, 1)
            if key == "BITHUMB_API_SECRET" and value is not None:
                candidates.append(("BITHUMB_API_SECRET", value))

        for context, value in candidates:
            if not _short_secret_literal(value):
                continue
            allow_key = (path.name, function.name, context, value)
            if allow_key in SHORT_SECRET_ALLOWLIST:
                continue
            offenders.append(
                f"{path.relative_to(TESTS_ROOT)}::{function.name} uses short Bithumb JWT secret "
                f"in {context} literal with actual_bytes={len(value.encode('utf-8'))}"
            )
    return offenders


def test_tests_do_not_use_short_bithumb_jwt_secrets() -> None:
    offenders: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                offenders.extend(_offenders_in_function(path, node))

    assert offenders == []


def test_shared_bithumb_test_secret_satisfies_hs256_policy() -> None:
    assert len(TEST_BITHUMB_API_SECRET.encode("utf-8")) >= JWT_HS256_MIN_SECRET_BYTES


def test_config_spec_declares_bithumb_jwt_secret_quality_policy() -> None:
    spec = SPEC_BY_NAME["BITHUMB_API_SECRET"]
    assert spec.validation_kind == JWT_HS256_SECRET_VALIDATION_KIND
    assert spec.min_live_bytes == JWT_HS256_MIN_SECRET_BYTES
