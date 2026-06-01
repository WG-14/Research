from __future__ import annotations

from pathlib import Path

from tools.check_text_hygiene import scan_file, scan_paths


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_text_hygiene_checker_detects_known_violation_classes(tmp_path: Path) -> None:
    hangul_sample = bytes.fromhex("ed959cea b880".replace(" ", "")).decode("utf-8")
    mojibake_sample = bytes.fromhex("c383").decode("utf-8")
    replacement_sample = chr(0xFFFD)
    question_run = "?" * 4
    cases = {
        "bom.py": b"\xef\xbb\xbffrom __future__ import annotations\n",
        "hangul.py": f"message = 'operator text {hangul_sample}'\n".encode(),
        "replacement.py": f"message = 'broken {replacement_sample} text'\n".encode(),
        "questions.py": f"message = 'broken {question_run} text'\n".encode(),
        "mojibake.py": f"message = '{mojibake_sample}'\n".encode(),
    }
    kinds_by_file = {}
    for name, data in cases.items():
        path = _write(tmp_path, name, data)
        kinds_by_file[name] = {violation.kind for violation in scan_file(path)}

    assert "utf8_bom" in kinds_by_file["bom.py"]
    assert "hangul" in kinds_by_file["hangul.py"]
    assert "replacement_character" in kinds_by_file["replacement.py"]
    assert "suspicious_question_run" in kinds_by_file["questions.py"]
    assert "known_mojibake" in kinds_by_file["mojibake.py"]


def test_repository_text_hygiene_passes() -> None:
    assert scan_paths() == []
