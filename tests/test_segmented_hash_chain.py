from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import market_research.research.segmented_hash_chain as segmented_module
from market_research.research.segmented_hash_chain import (
    append_segmented_hash_chained_jsonl_idempotent,
    read_segmented_hash_chain_full_snapshot,
    validate_segmented_hash_chain_incremental,
    verify_segmented_hash_chained_jsonl_event,
)


LABEL = "segmented_test_audit"


def _append(path: Path, index: int, *, segment_rows: int = 5) -> dict[str, object]:
    return append_segmented_hash_chained_jsonl_idempotent(
        path=path,
        payload={"event_id": f"event-{index}", "value": index},
        label=LABEL,
        max_segment_rows=segment_rows,
    )


def test_segment_rotation_preserves_global_chain_and_idempotency(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audit.jsonl"
    rows = [_append(path, index) for index in range(13)]

    repeated = _append(path, 7)
    assert repeated == rows[7]
    with pytest.raises(ValueError, match="segmented_hash_chain_event_id_conflict"):
        append_segmented_hash_chained_jsonl_idempotent(
            path=path,
            payload={"event_id": "event-7", "value": "different"},
            label=LABEL,
            max_segment_rows=5,
        )

    snapshot = read_segmented_hash_chain_full_snapshot(
        path=path,
        label=LABEL,
        max_segment_rows=5,
    )
    assert snapshot.status == "PASS"
    assert snapshot.row_count == 13
    assert snapshot.sealed_segment_count == 2
    assert [row["sequence"] for row in snapshot.rows] == list(range(13))
    assert snapshot.stream_hash == rows[-1]["row_hash"]


def test_incremental_validation_cost_is_bounded_by_two_segments(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bounded-audit.jsonl"
    for index in range(101):
        _append(path, index, segment_rows=10)

    incremental = validate_segmented_hash_chain_incremental(
        path=path,
        label=LABEL,
        max_segment_rows=10,
    )
    full = read_segmented_hash_chain_full_snapshot(
        path=path,
        label=LABEL,
        max_segment_rows=10,
    ).as_validation()

    assert incremental["status"] == full["status"] == "PASS"
    assert incremental["row_count"] == full["row_count"] == 101
    assert incremental["stream_hash"] == full["stream_hash"]
    assert incremental["validated_row_count"] == 11
    assert full["validated_row_count"] == 101


def test_full_validation_localizes_middle_segment_corruption(
    tmp_path: Path,
) -> None:
    path = tmp_path / "corrupt-audit.jsonl"
    for index in range(26):
        _append(path, index, segment_rows=5)
    middle = (
        path.with_name(f"{path.name}.segments") / "segments" / "segment-00000001.jsonl"
    )
    rows = [json.loads(line) for line in middle.read_text().splitlines()]
    rows[2]["value"] = "tampered"
    middle.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )

    # The bounded validator intentionally covers only the active and immediate
    # predecessor segments.  Scheduled full validation detects older damage.
    assert (
        validate_segmented_hash_chain_incremental(
            path=path, label=LABEL, max_segment_rows=5
        )["status"]
        == "PASS"
    )
    full = read_segmented_hash_chain_full_snapshot(
        path=path, label=LABEL, max_segment_rows=5
    )
    assert full.status == "FAIL"
    assert "content_hash_mismatch:1" in full.reasons[0]


@pytest.mark.parametrize("damage", ("checkpoint", "tail"))
def test_checkpoint_tamper_and_truncated_tail_fail_closed(
    tmp_path: Path,
    damage: str,
) -> None:
    path = tmp_path / f"{damage}-audit.jsonl"
    for index in range(3):
        _append(path, index, segment_rows=5)
    root = path.with_name(f"{path.name}.segments")
    if damage == "checkpoint":
        checkpoint = json.loads((root / "checkpoint.json").read_text())
        checkpoint["row_count"] = 999
        (root / "checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    else:
        segment = root / "segments" / "segment-00000000.jsonl"
        segment.write_bytes(segment.read_bytes().removesuffix(b"\n"))

    incremental = validate_segmented_hash_chain_incremental(
        path=path,
        label=LABEL,
        max_segment_rows=5,
    )
    assert incremental["status"] == "FAIL"


def test_fsynced_row_is_adopted_after_checkpoint_publish_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "interrupted-audit.jsonl"
    original_write = segmented_module._write_checkpoint
    calls = 0

    def interrupt_second_checkpoint(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated checkpoint publish interruption")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(
        segmented_module, "_write_checkpoint", interrupt_second_checkpoint
    )
    with pytest.raises(OSError, match="checkpoint publish interruption"):
        _append(path, 1)
    monkeypatch.setattr(segmented_module, "_write_checkpoint", original_write)

    recovered = _append(path, 1)
    snapshot = read_segmented_hash_chain_full_snapshot(
        path=path,
        label=LABEL,
        max_segment_rows=5,
    )
    assert recovered["event_id"] == "event-1"
    assert snapshot.status == "PASS"
    assert snapshot.row_count == 1


def test_concurrent_duplicate_delivery_converges_to_one_row(tmp_path: Path) -> None:
    path = tmp_path / "concurrent-audit.jsonl"

    def deliver(_index: int) -> dict[str, object]:
        return append_segmented_hash_chained_jsonl_idempotent(
            path=path,
            payload={"event_id": "same-event", "value": 1},
            label=LABEL,
            max_segment_rows=5,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        rows = list(executor.map(deliver, range(16)))

    assert len({row["row_hash"] for row in rows}) == 1
    verified = verify_segmented_hash_chained_jsonl_event(
        path=path,
        label=LABEL,
        max_segment_rows=5,
        event_id="same-event",
        expected_payload={"event_id": "same-event", "value": 1},
    )
    assert verified == rows[0]
    assert (
        read_segmented_hash_chain_full_snapshot(
            path=path, label=LABEL, max_segment_rows=5
        ).row_count
        == 1
    )
