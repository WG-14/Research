"""Crash-safe segmented JSONL hash chains with bounded append validation.

The legacy single-file hash chain remains available for compatibility.  This
module is intended for long-lived append-only audit streams: each append reads
only the active bounded segment and an atomic checkpoint.  Sealed segments are
immutable and linked by immutable metadata records.  A full validator remains
available for incident response and backup verification.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from market_research.storage_io import (
    append_jsonl,
    write_json_atomic,
    write_json_atomic_create_or_verify,
)

from .hashing import canonical_json_bytes, content_hash_payload, sha256_prefixed


_CHAIN_FIELDS = frozenset({"sequence", "prior_hash", "row_hash"})
_CHECKPOINT_VERSION = 1


@dataclass(frozen=True, slots=True)
class SegmentedHashChainSnapshot:
    rows: tuple[dict[str, Any], ...]
    status: str
    reasons: tuple[str, ...]
    row_count: int
    stream_hash: str | None
    sealed_segment_count: int
    active_segment_row_count: int
    validated_row_count: int
    validation_scope: str

    def as_validation(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reasons": list(self.reasons),
            "row_count": self.row_count,
            "stream_hash": self.stream_hash,
            "sealed_segment_count": self.sealed_segment_count,
            "active_segment_row_count": self.active_segment_row_count,
            "validated_row_count": self.validated_row_count,
            "validation_scope": self.validation_scope,
        }


@dataclass(frozen=True, slots=True)
class _Layout:
    root: Path
    checkpoint: Path
    segments: Path
    metadata: Path
    receipts: Path
    lock: Path


def append_segmented_hash_chained_jsonl_idempotent(
    *,
    path: Path,
    payload: dict[str, Any],
    label: str,
    max_segment_rows: int,
) -> dict[str, Any]:
    """Append once while validating at most one bounded active segment."""

    _validate_configuration(label=label, max_segment_rows=max_segment_rows)
    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("segmented_hash_chain_event_id_required")
    if _CHAIN_FIELDS.intersection(payload):
        raise ValueError("segmented_hash_chain_payload_contains_reserved_field")
    detached_payload = deepcopy(payload)
    layout = _layout(path)
    with _locked(layout):
        checkpoint, active_rows = _load_for_append(
            layout=layout,
            label=label,
            max_segment_rows=max_segment_rows,
        )
        receipt = _read_receipt(layout, event_id)
        if receipt is not None:
            return _verify_receipt(
                layout=layout,
                checkpoint=checkpoint,
                active_rows=active_rows,
                receipt=receipt,
                event_id=event_id,
                expected_payload=detached_payload,
                label=label,
            )

        active_matches = [row for row in active_rows if row.get("event_id") == event_id]
        if len(active_matches) > 1:
            raise ValueError("segmented_hash_chain_duplicate_event_id")
        if active_matches:
            existing = active_matches[0]
            _assert_payload_matches(existing, detached_payload)
            _publish_receipt(
                layout=layout,
                row=existing,
                segment_number=int(checkpoint["active_segment"]),
                payload=detached_payload,
                label=label,
            )
            return deepcopy(existing)

        if len(active_rows) == max_segment_rows:
            checkpoint = _seal_active_segment(
                layout=layout,
                checkpoint=checkpoint,
                active_rows=active_rows,
                label=label,
                max_segment_rows=max_segment_rows,
            )
            active_rows = []

        material = {
            **detached_payload,
            "sequence": int(checkpoint["row_count"]),
            "prior_hash": checkpoint["stream_hash"],
        }
        row = {
            **material,
            "row_hash": sha256_prefixed(
                content_hash_payload(material),
                label=f"{label}_row",
            ),
        }
        segment_path = _segment_path(layout, int(checkpoint["active_segment"]))
        append_jsonl(segment_path, row)
        _publish_receipt(
            layout=layout,
            row=row,
            segment_number=int(checkpoint["active_segment"]),
            payload=detached_payload,
            label=label,
        )
        checkpoint = {
            **checkpoint,
            "row_count": int(checkpoint["row_count"]) + 1,
            "stream_hash": row["row_hash"],
            "active_row_count": int(checkpoint["active_row_count"]) + 1,
        }
        _write_checkpoint(layout, checkpoint, label=label)
        return deepcopy(row)


def verify_segmented_hash_chained_jsonl_event(
    *,
    path: Path,
    label: str,
    max_segment_rows: int,
    event_id: str,
    expected_payload: dict[str, Any],
) -> dict[str, Any]:
    _validate_configuration(label=label, max_segment_rows=max_segment_rows)
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("segmented_hash_chain_event_id_required")
    if _CHAIN_FIELDS.intersection(expected_payload):
        raise ValueError("segmented_hash_chain_payload_contains_reserved_field")
    layout = _layout(path)
    with _locked(layout):
        checkpoint, active_rows = _load_for_append(
            layout=layout,
            label=label,
            max_segment_rows=max_segment_rows,
        )
        receipt = _read_receipt(layout, event_id)
        if receipt is None:
            matches = [row for row in active_rows if row.get("event_id") == event_id]
            if not matches:
                raise ValueError("segmented_hash_chain_event_id_missing")
            if len(matches) > 1:
                raise ValueError("segmented_hash_chain_duplicate_event_id")
            _assert_payload_matches(matches[0], expected_payload)
            return deepcopy(matches[0])
        return _verify_receipt(
            layout=layout,
            checkpoint=checkpoint,
            active_rows=active_rows,
            receipt=receipt,
            event_id=event_id,
            expected_payload=expected_payload,
            label=label,
        )


def validate_segmented_hash_chain_incremental(
    *,
    path: Path,
    label: str,
    max_segment_rows: int,
) -> dict[str, Any]:
    """Validate the checkpoint, last sealed segment, and active segment."""

    _validate_configuration(label=label, max_segment_rows=max_segment_rows)
    layout = _layout(path)
    if not layout.root.exists():
        return _empty_snapshot(scope="incremental").as_validation()
    try:
        with _locked(layout):
            checkpoint = _read_checkpoint(
                layout,
                label=label,
                max_segment_rows=max_segment_rows,
            )
            active_rows = _read_rows(
                _segment_path(layout, int(checkpoint["active_segment"]))
            )
            _validate_rows_or_raise(
                rows=active_rows,
                start_sequence=int(checkpoint["active_start_sequence"]),
                prior_hash=checkpoint["active_prior_hash"],
                label=label,
            )
            _assert_active_matches_checkpoint(checkpoint, active_rows)
            validated_count = len(active_rows)
            if int(checkpoint["sealed_segment_count"]) > 0:
                segment_number = int(checkpoint["sealed_segment_count"]) - 1
                metadata = _read_metadata(layout, segment_number, label=label)
                if metadata["metadata_hash"] != checkpoint["sealed_metadata_hash"]:
                    raise ValueError("segmented_hash_chain_metadata_head_mismatch")
                sealed_rows = _read_and_validate_sealed_segment(
                    layout=layout,
                    metadata=metadata,
                    label=label,
                )
                validated_count += len(sealed_rows)
            snapshot = SegmentedHashChainSnapshot(
                rows=tuple(deepcopy(active_rows)),
                status="PASS",
                reasons=(),
                row_count=int(checkpoint["row_count"]),
                stream_hash=checkpoint["stream_hash"],
                sealed_segment_count=int(checkpoint["sealed_segment_count"]),
                active_segment_row_count=len(active_rows),
                validated_row_count=validated_count,
                validation_scope="incremental",
            )
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return _failed_snapshot(scope="incremental", reason=str(exc)).as_validation()
    return snapshot.as_validation()


def read_segmented_hash_chain_full_snapshot(
    *,
    path: Path,
    label: str,
    max_segment_rows: int,
) -> SegmentedHashChainSnapshot:
    """Validate every immutable segment and return all rows for reconciliation."""

    _validate_configuration(label=label, max_segment_rows=max_segment_rows)
    layout = _layout(path)
    if not layout.root.exists():
        return _empty_snapshot(scope="full")
    try:
        with _locked(layout):
            checkpoint = _read_checkpoint(
                layout,
                label=label,
                max_segment_rows=max_segment_rows,
            )
            all_rows: list[dict[str, Any]] = []
            prior_metadata_hash: str | None = None
            for segment_number in range(int(checkpoint["sealed_segment_count"])):
                metadata = _read_metadata(layout, segment_number, label=label)
                if metadata["prior_metadata_hash"] != prior_metadata_hash:
                    raise ValueError(
                        f"segmented_hash_chain_metadata_prior_mismatch:{segment_number}"
                    )
                sealed_rows = _read_and_validate_sealed_segment(
                    layout=layout,
                    metadata=metadata,
                    label=label,
                )
                if len(sealed_rows) != max_segment_rows:
                    raise ValueError(
                        f"segmented_hash_chain_sealed_row_count_invalid:{segment_number}"
                    )
                all_rows.extend(sealed_rows)
                prior_metadata_hash = str(metadata["metadata_hash"])
            if prior_metadata_hash != checkpoint["sealed_metadata_hash"]:
                raise ValueError("segmented_hash_chain_metadata_head_mismatch")

            active_rows = _read_rows(
                _segment_path(layout, int(checkpoint["active_segment"]))
            )
            _validate_rows_or_raise(
                rows=active_rows,
                start_sequence=int(checkpoint["active_start_sequence"]),
                prior_hash=checkpoint["active_prior_hash"],
                label=label,
            )
            _assert_active_matches_checkpoint(checkpoint, active_rows)
            all_rows.extend(active_rows)
            if len(all_rows) != int(checkpoint["row_count"]):
                raise ValueError("segmented_hash_chain_total_row_count_mismatch")
            if _terminal_hash(all_rows, None) != checkpoint["stream_hash"]:
                raise ValueError("segmented_hash_chain_terminal_hash_mismatch")
            event_ids: set[str] = set()
            for row in all_rows:
                event_id = row.get("event_id")
                if not isinstance(event_id, str) or not event_id:
                    raise ValueError("segmented_hash_chain_event_id_required")
                if event_id in event_ids:
                    raise ValueError("segmented_hash_chain_duplicate_event_id")
                event_ids.add(event_id)
                receipt = _read_receipt(layout, event_id)
                if receipt is None:
                    raise ValueError("segmented_hash_chain_receipt_missing")
                _validate_receipt(receipt, label=label)
                if (
                    receipt["row_hash"] != row["row_hash"]
                    or int(receipt["sequence"]) != int(row["sequence"])
                ):
                    raise ValueError("segmented_hash_chain_receipt_binding_invalid")
            return SegmentedHashChainSnapshot(
                rows=tuple(deepcopy(all_rows)),
                status="PASS",
                reasons=(),
                row_count=len(all_rows),
                stream_hash=checkpoint["stream_hash"],
                sealed_segment_count=int(checkpoint["sealed_segment_count"]),
                active_segment_row_count=len(active_rows),
                validated_row_count=len(all_rows),
                validation_scope="full",
            )
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return _failed_snapshot(scope="full", reason=str(exc))


def _load_for_append(
    *,
    layout: _Layout,
    label: str,
    max_segment_rows: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    layout.segments.mkdir(parents=True, exist_ok=True)
    layout.metadata.mkdir(parents=True, exist_ok=True)
    layout.receipts.mkdir(parents=True, exist_ok=True)
    if not layout.checkpoint.exists():
        if any(layout.segments.iterdir()) or any(layout.metadata.iterdir()):
            raise ValueError("segmented_hash_chain_checkpoint_missing")
        checkpoint = _initial_checkpoint(label=label, max_segment_rows=max_segment_rows)
        _write_checkpoint(layout, checkpoint, label=label)
    checkpoint = _read_checkpoint(
        layout,
        label=label,
        max_segment_rows=max_segment_rows,
    )
    active_segment = int(checkpoint["active_segment"])
    active_rows = _read_rows(_segment_path(layout, active_segment))
    if len(active_rows) > max_segment_rows:
        raise ValueError("segmented_hash_chain_active_segment_overflow")
    _validate_rows_or_raise(
        rows=active_rows,
        start_sequence=int(checkpoint["active_start_sequence"]),
        prior_hash=checkpoint["active_prior_hash"],
        label=label,
    )
    checkpoint_count = int(checkpoint["active_row_count"])
    if checkpoint_count > len(active_rows):
        raise ValueError("segmented_hash_chain_checkpoint_ahead_of_segment")
    prefix_hash = _terminal_hash(
        active_rows[:checkpoint_count], checkpoint["active_prior_hash"]
    )
    if prefix_hash != checkpoint["stream_hash"]:
        raise ValueError("segmented_hash_chain_checkpoint_terminal_mismatch")
    if len(active_rows) > checkpoint_count:
        # An append was fsynced before its checkpoint publish.  Only a valid
        # contiguous suffix can be adopted, and receipts are recreated with
        # immutable create-or-verify semantics.
        for row in active_rows[checkpoint_count:]:
            payload = _row_payload(row)
            _publish_receipt(
                layout=layout,
                row=row,
                segment_number=active_segment,
                payload=payload,
                label=label,
            )
        checkpoint = {
            **checkpoint,
            "active_row_count": len(active_rows),
            "row_count": int(checkpoint["active_start_sequence"]) + len(active_rows),
            "stream_hash": _terminal_hash(
                active_rows, checkpoint["active_prior_hash"]
            ),
        }
        _write_checkpoint(layout, checkpoint, label=label)
    return checkpoint, active_rows


def _seal_active_segment(
    *,
    layout: _Layout,
    checkpoint: dict[str, Any],
    active_rows: list[dict[str, Any]],
    label: str,
    max_segment_rows: int,
) -> dict[str, Any]:
    if len(active_rows) != max_segment_rows:
        raise ValueError("segmented_hash_chain_rotation_requires_full_segment")
    segment_number = int(checkpoint["active_segment"])
    segment_path = _segment_path(layout, segment_number)
    for row in active_rows:
        _publish_receipt(
            layout=layout,
            row=row,
            segment_number=segment_number,
            payload=_row_payload(row),
            label=label,
        )
    material = {
        "schema_version": _CHECKPOINT_VERSION,
        "label": label,
        "segment_number": segment_number,
        "start_sequence": int(checkpoint["active_start_sequence"]),
        "row_count": len(active_rows),
        "prior_hash": checkpoint["active_prior_hash"],
        "terminal_hash": _terminal_hash(active_rows, checkpoint["active_prior_hash"]),
        "content_hash": _file_hash(segment_path),
        "prior_metadata_hash": checkpoint["sealed_metadata_hash"],
    }
    metadata = {
        **material,
        "metadata_hash": sha256_prefixed(
            content_hash_payload(material), label=f"{label}_segment_metadata"
        ),
    }
    try:
        write_json_atomic_create_or_verify(
            _metadata_path(layout, segment_number), metadata
        )
    except ValueError as exc:
        raise ValueError("segmented_hash_chain_metadata_conflict") from exc
    advanced = {
        **checkpoint,
        "sealed_segment_count": int(checkpoint["sealed_segment_count"]) + 1,
        "sealed_metadata_hash": metadata["metadata_hash"],
        "active_segment": segment_number + 1,
        "active_start_sequence": int(checkpoint["row_count"]),
        "active_prior_hash": checkpoint["stream_hash"],
        "active_row_count": 0,
    }
    _write_checkpoint(layout, advanced, label=label)
    return advanced


def _verify_receipt(
    *,
    layout: _Layout,
    checkpoint: dict[str, Any],
    active_rows: list[dict[str, Any]],
    receipt: dict[str, Any],
    event_id: str,
    expected_payload: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    _validate_receipt(receipt, label=label)
    if receipt.get("event_id") != event_id:
        raise ValueError("segmented_hash_chain_receipt_binding_invalid")
    expected_payload_hash = _payload_hash(expected_payload, label=label)
    if receipt.get("payload_hash") != expected_payload_hash:
        raise ValueError("segmented_hash_chain_event_id_conflict")
    segment_number = int(receipt["segment_number"])
    if segment_number == int(checkpoint["active_segment"]):
        rows = active_rows
    elif 0 <= segment_number < int(checkpoint["sealed_segment_count"]):
        metadata = _read_metadata(layout, segment_number, label=label)
        rows = _read_and_validate_sealed_segment(
            layout=layout,
            metadata=metadata,
            label=label,
        )
    else:
        raise ValueError("segmented_hash_chain_receipt_segment_invalid")
    matches = [row for row in rows if row.get("event_id") == event_id]
    if not matches:
        raise ValueError("segmented_hash_chain_event_id_missing")
    if len(matches) > 1:
        raise ValueError("segmented_hash_chain_duplicate_event_id")
    row = matches[0]
    _assert_payload_matches(row, expected_payload)
    if (
        row.get("row_hash") != receipt.get("row_hash")
        or row.get("sequence") != receipt.get("sequence")
    ):
        raise ValueError("segmented_hash_chain_receipt_binding_invalid")
    return deepcopy(row)


def _read_and_validate_sealed_segment(
    *, layout: _Layout, metadata: dict[str, Any], label: str
) -> list[dict[str, Any]]:
    segment_number = int(metadata["segment_number"])
    segment_path = _segment_path(layout, segment_number)
    if _file_hash(segment_path) != metadata["content_hash"]:
        raise ValueError(f"segmented_hash_chain_content_hash_mismatch:{segment_number}")
    rows = _read_rows(segment_path)
    _validate_rows_or_raise(
        rows=rows,
        start_sequence=int(metadata["start_sequence"]),
        prior_hash=metadata["prior_hash"],
        label=label,
    )
    if (
        len(rows) != int(metadata["row_count"])
        or _terminal_hash(rows, metadata["prior_hash"])
        != metadata["terminal_hash"]
    ):
        raise ValueError(f"segmented_hash_chain_metadata_binding_invalid:{segment_number}")
    return rows


def _validate_rows_or_raise(
    *,
    rows: list[dict[str, Any]],
    start_sequence: int,
    prior_hash: str | None,
    label: str,
) -> None:
    expected_prior = prior_hash
    for offset, row in enumerate(rows):
        expected_sequence = start_sequence + offset
        if row.get("sequence") != expected_sequence:
            raise ValueError(
                f"segmented_hash_chain_sequence_mismatch:{expected_sequence}"
            )
        if row.get("prior_hash") != expected_prior:
            raise ValueError(
                f"segmented_hash_chain_prior_hash_mismatch:{expected_sequence}"
            )
        material = {key: value for key, value in row.items() if key != "row_hash"}
        expected_hash = sha256_prefixed(
            content_hash_payload(material), label=f"{label}_row"
        )
        if row.get("row_hash") != expected_hash:
            raise ValueError(
                f"segmented_hash_chain_row_hash_mismatch:{expected_sequence}"
            )
        expected_prior = str(row["row_hash"])


def _assert_active_matches_checkpoint(
    checkpoint: dict[str, Any], rows: list[dict[str, Any]]
) -> None:
    if len(rows) != int(checkpoint["active_row_count"]):
        raise ValueError("segmented_hash_chain_checkpoint_row_count_mismatch")
    if int(checkpoint["active_start_sequence"]) + len(rows) != int(
        checkpoint["row_count"]
    ):
        raise ValueError("segmented_hash_chain_checkpoint_total_mismatch")
    if _terminal_hash(rows, checkpoint["active_prior_hash"]) != checkpoint[
        "stream_hash"
    ]:
        raise ValueError("segmented_hash_chain_checkpoint_terminal_mismatch")


def _read_checkpoint(
    layout: _Layout, *, label: str, max_segment_rows: int
) -> dict[str, Any]:
    if not layout.checkpoint.exists():
        raise ValueError("segmented_hash_chain_checkpoint_missing")
    value = _read_json_object(layout.checkpoint)
    supplied_hash = value.get("checkpoint_hash")
    material = {key: item for key, item in value.items() if key != "checkpoint_hash"}
    expected_hash = sha256_prefixed(
        content_hash_payload(material), label=f"{label}_segment_checkpoint"
    )
    if supplied_hash != expected_hash:
        raise ValueError("segmented_hash_chain_checkpoint_hash_mismatch")
    if (
        value.get("schema_version") != _CHECKPOINT_VERSION
        or value.get("label") != label
        or value.get("max_segment_rows") != max_segment_rows
    ):
        raise ValueError("segmented_hash_chain_checkpoint_configuration_mismatch")
    integer_fields = (
        "row_count",
        "sealed_segment_count",
        "active_segment",
        "active_start_sequence",
        "active_row_count",
    )
    if any(
        not isinstance(value.get(field), int) or int(value[field]) < 0
        for field in integer_fields
    ):
        raise ValueError("segmented_hash_chain_checkpoint_shape_invalid")
    if value["active_segment"] != value["sealed_segment_count"]:
        raise ValueError("segmented_hash_chain_checkpoint_segment_mismatch")
    return value


def _write_checkpoint(
    layout: _Layout, checkpoint: dict[str, Any], *, label: str
) -> None:
    material = {
        key: value for key, value in checkpoint.items() if key != "checkpoint_hash"
    }
    value = {
        **material,
        "checkpoint_hash": sha256_prefixed(
            content_hash_payload(material), label=f"{label}_segment_checkpoint"
        ),
    }
    write_json_atomic(layout.checkpoint, value)


def _read_metadata(
    layout: _Layout, segment_number: int, *, label: str
) -> dict[str, Any]:
    value = _read_json_object(_metadata_path(layout, segment_number))
    supplied_hash = value.get("metadata_hash")
    material = {key: item for key, item in value.items() if key != "metadata_hash"}
    expected_hash = sha256_prefixed(
        content_hash_payload(material), label=f"{label}_segment_metadata"
    )
    if supplied_hash != expected_hash or value.get("segment_number") != segment_number:
        raise ValueError(f"segmented_hash_chain_metadata_hash_mismatch:{segment_number}")
    return value


def _publish_receipt(
    *,
    layout: _Layout,
    row: dict[str, Any],
    segment_number: int,
    payload: dict[str, Any],
    label: str,
) -> None:
    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("segmented_hash_chain_event_id_required")
    material = {
        "schema_version": _CHECKPOINT_VERSION,
        "event_id": event_id,
        "payload_hash": _payload_hash(payload, label=label),
        "segment_number": segment_number,
        "sequence": int(row["sequence"]),
        "row_hash": str(row["row_hash"]),
    }
    receipt = {
        **material,
        "receipt_hash": sha256_prefixed(
            content_hash_payload(material), label=f"{label}_segment_receipt"
        ),
    }
    try:
        write_json_atomic_create_or_verify(_receipt_path(layout, event_id), receipt)
    except ValueError as exc:
        existing = _read_receipt(layout, event_id)
        if existing is not None:
            try:
                _validate_receipt(existing, label=label)
            except ValueError:
                raise ValueError("segmented_hash_chain_receipt_conflict") from exc
            if existing.get("payload_hash") != material["payload_hash"]:
                raise ValueError("segmented_hash_chain_event_id_conflict") from exc
        raise ValueError("segmented_hash_chain_receipt_conflict") from exc


def _validate_receipt(receipt: dict[str, Any], *, label: str) -> None:
    supplied_hash = receipt.get("receipt_hash")
    material = {key: value for key, value in receipt.items() if key != "receipt_hash"}
    expected_hash = sha256_prefixed(
        content_hash_payload(material), label=f"{label}_segment_receipt"
    )
    if supplied_hash != expected_hash:
        raise ValueError("segmented_hash_chain_receipt_hash_mismatch")


def _read_receipt(layout: _Layout, event_id: str) -> dict[str, Any] | None:
    path = _receipt_path(layout, event_id)
    return _read_json_object(path) if path.exists() else None


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    content = path.read_text(encoding="utf-8")
    if content and not content.endswith("\n"):
        raise ValueError("segmented_hash_chain_unterminated_final_line")
    rows: list[dict[str, Any]] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("segmented_hash_chain_row_not_object")
        rows.append(value)
    return rows


def _read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("segmented_hash_chain_json_not_object")
    return value


def _assert_payload_matches(row: dict[str, Any], payload: dict[str, Any]) -> None:
    if canonical_json_bytes(_row_payload(row)) != canonical_json_bytes(payload):
        raise ValueError("segmented_hash_chain_event_id_conflict")


def _row_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in _CHAIN_FIELDS}


def _payload_hash(payload: dict[str, Any], *, label: str) -> str:
    return sha256_prefixed(content_hash_payload(payload), label=f"{label}_payload")


def _terminal_hash(
    rows: list[dict[str, Any]], prior_hash: str | None
) -> str | None:
    return str(rows[-1]["row_hash"]) if rows else prior_hash


def _file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _initial_checkpoint(*, label: str, max_segment_rows: int) -> dict[str, Any]:
    return {
        "schema_version": _CHECKPOINT_VERSION,
        "label": label,
        "max_segment_rows": max_segment_rows,
        "row_count": 0,
        "stream_hash": None,
        "sealed_segment_count": 0,
        "sealed_metadata_hash": None,
        "active_segment": 0,
        "active_start_sequence": 0,
        "active_prior_hash": None,
        "active_row_count": 0,
    }


def _layout(path: Path) -> _Layout:
    logical_path = Path(path)
    root = logical_path.with_name(f"{logical_path.name}.segments")
    return _Layout(
        root=root,
        checkpoint=root / "checkpoint.json",
        segments=root / "segments",
        metadata=root / "metadata",
        receipts=root / "receipts",
        lock=root.with_suffix(root.suffix + ".lock"),
    )


def _segment_path(layout: _Layout, segment_number: int) -> Path:
    return layout.segments / f"segment-{segment_number:08d}.jsonl"


def _metadata_path(layout: _Layout, segment_number: int) -> Path:
    return layout.metadata / f"segment-{segment_number:08d}.json"


def _receipt_path(layout: _Layout, event_id: str) -> Path:
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
    return layout.receipts / digest[:2] / f"{digest}.json"


def _validate_configuration(*, label: str, max_segment_rows: int) -> None:
    if not isinstance(label, str) or not label.strip():
        raise ValueError("segmented_hash_chain_label_required")
    if not isinstance(max_segment_rows, int) or not 2 <= max_segment_rows <= 1_000_000:
        raise ValueError("segmented_hash_chain_max_segment_rows_invalid")


def _empty_snapshot(*, scope: str) -> SegmentedHashChainSnapshot:
    return SegmentedHashChainSnapshot(
        rows=(),
        status="PASS",
        reasons=(),
        row_count=0,
        stream_hash=None,
        sealed_segment_count=0,
        active_segment_row_count=0,
        validated_row_count=0,
        validation_scope=scope,
    )


def _failed_snapshot(*, scope: str, reason: str) -> SegmentedHashChainSnapshot:
    return SegmentedHashChainSnapshot(
        rows=(),
        status="FAIL",
        reasons=(reason or "segmented_hash_chain_validation_error",),
        row_count=0,
        stream_hash=None,
        sealed_segment_count=0,
        active_segment_row_count=0,
        validated_row_count=0,
        validation_scope=scope,
    )


@contextmanager
def _locked(layout: _Layout) -> Iterator[None]:
    layout.root.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(layout.lock, os.O_CREAT | os.O_RDWR, 0o600)
    lock_module: Any | None = None
    try:
        try:
            import fcntl
        except ImportError as exc:
            raise RuntimeError("segmented_hash_chain_process_lock_unavailable") from exc
        lock_module = fcntl
        lock_module.flock(fd, lock_module.LOCK_EX)
        yield
    finally:
        try:
            if lock_module is not None:
                lock_module.flock(fd, lock_module.LOCK_UN)
        finally:
            os.close(fd)


__all__ = [
    "SegmentedHashChainSnapshot",
    "append_segmented_hash_chained_jsonl_idempotent",
    "read_segmented_hash_chain_full_snapshot",
    "validate_segmented_hash_chain_incremental",
    "verify_segmented_hash_chained_jsonl_event",
]
