from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs" / "platform-completeness-review.md"
AREA_CONTRACTS = {
    "R": (5, 6),
    "D": (6, 8),
    "L": (4, 5),
    "DA": (7, 11),
    "P": (8, 9),
    "E": (7, 9),
    "BT": (10, 13),
    "V": (6, 11),
    "S": (6, 8),
    "M": (5, 8),
    "MON": (5, 7),
    "K": (4, 7),
    "UX": (4, 8),
    "SEC": (4, 7),
    "OPS": (5, 10),
    "T": (8, 15),
    "A": (6, 11),
}


def _decision_scores(payload: str) -> dict[str, int | None]:
    section = payload.split("## Final criterion decisions (iteration 15/15)", 1)[
        1
    ].split("### Final score", 1)[0]
    values: dict[str, int | None] = {}
    for criterion_id, raw_score in re.findall(
        r"\b([A-Z]+-[0-9]{2})=(N/A|[0-5])\b", section
    ):
        assert criterion_id not in values
        values[criterion_id] = None if raw_score == "N/A" else int(raw_score)
    return values


def test_final_review_has_one_decision_for_every_normalized_criterion() -> None:
    payload = REPORT.read_text(encoding="utf-8")
    scores = _decision_scores(payload)
    expected = {
        f"{prefix}-{number:02d}"
        for prefix, (_weight, count) in AREA_CONTRACTS.items()
        for number in range(1, count + 1)
    }

    assert scores.keys() == expected
    assert len(scores) == 153
    assert all(score is not None for score in scores.values())
    assert scores["A-10"] == 5
    assert all(scores[f"M-{number:02d}"] == 0 for number in range(1, 9))
    assert all(scores[f"MON-{number:02d}"] == 0 for number in range(1, 8))
    assert "**INCOMPLETE.**" in payload


def test_final_review_score_arithmetic_is_literal_and_exact() -> None:
    payload = REPORT.read_text(encoding="utf-8")
    scores = _decision_scores(payload)
    weighted_total = 0.0
    for prefix, (weight, count) in AREA_CONTRACTS.items():
        area_values = [
            scores[f"{prefix}-{number:02d}"] for number in range(1, count + 1)
        ]
        assert all(value is not None for value in area_values)
        weighted_total += (
            (sum(value for value in area_values if value is not None) / count)
            / 5.0
            * weight
        )

    advertised = re.search(
        r"\| \*\*Literal rubric total\*\* .*?\*\*([0-9]+\.[0-9]{2}) / 100\*\*",
        payload,
    )
    assert advertised is not None
    assert float(advertised.group(1)) == pytest.approx(weighted_total, abs=0.005)
    assert weighted_total < 100.0
    assert "supported-scope score" not in payload


def test_all_blockers_are_decided_and_external_e5_is_not_overclaimed() -> None:
    payload = REPORT.read_text(encoding="utf-8")
    section = payload.split("## Blocking-condition decision", 1)[1].split(
        "## Representative end-to-end trace", 1
    )[0]

    assert {f"B-{number:02d}" for number in range(1, 9)} == set(
        re.findall(r"\bB-[0-9]{2}\b", section)
    )
    assert "B-08 unverified recovery | **OPEN**" in section
    assert "actual local E4 restore" in section
    assert "does not supply E5" in section
