from __future__ import annotations

import json
import os
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generic, Iterator, TypeVar

from market_research.storage_io import write_jsonl_atomic

from .hashing import canonical_json_bytes, content_hash_payload, sha256_prefixed


_EXPECTED_STREAM_HASH_UNSET = object()
_CHAIN_FIELDS = frozenset({"sequence", "prior_hash", "row_hash"})
_MutationResult = TypeVar("_MutationResult")


@dataclass(frozen=True, slots=True)
class HashChainSnapshot:
    """Detached, validated rows visible to one locked stream mutation."""

    rows: tuple[dict[str, Any], ...]
    stream_hash: str | None


@dataclass(frozen=True, slots=True)
class HashChainValidationSnapshot:
    """Detached rows and validation metadata from one locked file generation."""

    rows: tuple[dict[str, Any], ...]
    status: str
    reasons: tuple[str, ...]
    row_count: int
    stream_hash: str | None

    def as_validation(self) -> dict[str, Any]:
        """Return the compatibility validation result without exposing rows."""

        return {
            "status": self.status,
            "reasons": list(self.reasons),
            "row_count": self.row_count,
            "stream_hash": self.stream_hash,
        }


@dataclass(frozen=True, slots=True)
class HashChainMutation(Generic[_MutationResult]):
    """Result returned by an atomic stream mutation."""

    value: _MutationResult
    appended_rows: tuple[dict[str, Any], ...]


def append_hash_chained_jsonl(
    *,
    store: Any,
    path: Path,
    payload: dict[str, Any],
    label: str,
    expected_stream_hash: str | None | object = _EXPECTED_STREAM_HASH_UNSET,
) -> dict[str, Any]:
    with _locked(path):
        rows = _read_rows(path)
        validation = _validate_rows(rows=rows, label=label)
        if validation["status"] != "PASS":
            raise ValueError(f"hash_chain_invalid:{','.join(validation['reasons'])}")
        if (
            expected_stream_hash is not _EXPECTED_STREAM_HASH_UNSET
            and validation["stream_hash"] != expected_stream_hash
        ):
            raise ValueError("hash_chain_concurrent_update")
        material = {
            **payload,
            "sequence": int(validation["row_count"]),
            "prior_hash": validation["stream_hash"],
        }
        row = {
            **material,
            "row_hash": sha256_prefixed(
                content_hash_payload(material), label=f"{label}_row"
            ),
        }
        store.append_jsonl(path, row)
        return row


def append_hash_chained_jsonl_idempotent(
    *,
    store: Any,
    path: Path,
    payload: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    """Append one event-id-bound row, or return its identical existing row.

    The lookup and append share the stream lock. This makes concurrent delivery
    of the same immutable event safe without implementing a retry scheduler or
    recovery workflow. Reusing an event ID for different payload material is a
    fail-closed integrity error.
    """

    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("hash_chain_event_id_required")
    if _CHAIN_FIELDS.intersection(payload):
        raise ValueError("hash_chain_payload_contains_reserved_field")

    with _locked(path):
        rows = _read_rows(path)
        validation = _validate_rows(rows=rows, label=label)
        if validation["status"] != "PASS":
            raise ValueError(f"hash_chain_invalid:{','.join(validation['reasons'])}")

        matches = [row for row in rows if row.get("event_id") == event_id]
        if len(matches) > 1:
            raise ValueError("hash_chain_duplicate_event_id")
        if matches:
            existing = matches[0]
            existing_payload = {
                key: value
                for key, value in existing.items()
                if key not in _CHAIN_FIELDS
            }
            if canonical_json_bytes(existing_payload) != canonical_json_bytes(payload):
                raise ValueError("hash_chain_event_id_conflict")
            return existing

        material = {
            **payload,
            "sequence": int(validation["row_count"]),
            "prior_hash": validation["stream_hash"],
        }
        row = {
            **material,
            "row_hash": sha256_prefixed(
                content_hash_payload(material), label=f"{label}_row"
            ),
        }
        store.append_jsonl(path, row)
        return row


def read_hash_chained_jsonl_snapshot(
    *,
    path: Path,
    label: str,
) -> HashChainValidationSnapshot:
    """Read and validate one stream generation while holding its writer lock."""

    with _locked(path):
        rows = _read_rows(path)
        validation = _validate_rows(rows=rows, label=label)
        return HashChainValidationSnapshot(
            rows=tuple(deepcopy(rows)),
            status=str(validation["status"]),
            reasons=tuple(str(reason) for reason in validation["reasons"]),
            row_count=int(validation["row_count"]),
            stream_hash=validation["stream_hash"],
        )


def verify_hash_chained_jsonl_event(
    *,
    path: Path,
    label: str,
    event_id: str,
    expected_payload: dict[str, Any],
) -> dict[str, Any]:
    """Return one exact event row from a single locked, validated snapshot."""

    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("hash_chain_event_id_required")
    if _CHAIN_FIELDS.intersection(expected_payload):
        raise ValueError("hash_chain_payload_contains_reserved_field")

    with _locked(path):
        rows = _read_rows(path)
        validation = _validate_rows(rows=rows, label=label)
        if validation["status"] != "PASS":
            raise ValueError(f"hash_chain_invalid:{','.join(validation['reasons'])}")
        matches = [row for row in rows if row.get("event_id") == event_id]
        if not matches:
            raise ValueError("hash_chain_event_id_missing")
        if len(matches) > 1:
            raise ValueError("hash_chain_duplicate_event_id")
        existing = matches[0]
        existing_payload = {
            key: value
            for key, value in existing.items()
            if key not in _CHAIN_FIELDS
        }
        if canonical_json_bytes(existing_payload) != canonical_json_bytes(
            expected_payload
        ):
            raise ValueError("hash_chain_event_id_conflict")
        return deepcopy(existing)


def mutate_hash_chained_jsonl_atomic(
    *,
    path: Path,
    label: str,
    mutation: Callable[
        [HashChainSnapshot, Callable[[dict[str, Any]], dict[str, Any]]],
        _MutationResult,
    ],
) -> HashChainMutation[_MutationResult]:
    """Validate, stage, and atomically publish a multi-row stream mutation.

    ``mutation`` runs while the stream lock is held. It may inspect the detached
    snapshot and call ``stage`` repeatedly; staged rows receive their final
    sequence, prior hash, and row hash immediately, so a later staged row can
    bind to an earlier row. No row is published if the callback raises.
    """

    with _locked(path):
        rows = _read_rows(path)
        validation = _validate_rows(rows=rows, label=label)
        if validation["status"] != "PASS":
            raise ValueError(
                f"hash_chain_invalid:{','.join(validation['reasons'])}"
            )
        staged: list[dict[str, Any]] = []

        def stage(payload: dict[str, Any]) -> dict[str, Any]:
            if _CHAIN_FIELDS.intersection(payload):
                raise ValueError("hash_chain_payload_contains_reserved_field")
            detached_payload = deepcopy(payload)
            prior_hash = (
                staged[-1]["row_hash"]
                if staged
                else validation["stream_hash"]
            )
            material = {
                **detached_payload,
                "sequence": len(rows) + len(staged),
                "prior_hash": prior_hash,
            }
            row = {
                **material,
                "row_hash": sha256_prefixed(
                    content_hash_payload(material),
                    label=f"{label}_row",
                ),
            }
            staged.append(row)
            return deepcopy(row)

        snapshot = HashChainSnapshot(
            rows=tuple(deepcopy(rows)),
            stream_hash=validation["stream_hash"],
        )
        value = mutation(snapshot, stage)
        if staged:
            complete_rows = [*rows, *staged]
            complete_validation = _validate_rows(rows=complete_rows, label=label)
            if complete_validation["status"] != "PASS":
                raise ValueError(
                    "hash_chain_staged_rows_invalid:"
                    + ",".join(complete_validation["reasons"])
                )
            write_jsonl_atomic(path, complete_rows)
        return HashChainMutation(
            value=value,
            appended_rows=tuple(deepcopy(staged)),
        )


def validate_hash_chained_jsonl(*, path: Path, label: str) -> dict[str, Any]:
    return read_hash_chained_jsonl_snapshot(
        path=path,
        label=label,
    ).as_validation()


def _validate_rows(*, rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    reasons: list[str] = []
    prior_hash: str | None = None
    for index, row in enumerate(rows):
        if row.get("sequence") != index:
            reasons.append(f"sequence_mismatch:{index}")
        if row.get("prior_hash") != prior_hash:
            reasons.append(f"prior_hash_mismatch:{index}")
        material = {key: value for key, value in row.items() if key != "row_hash"}
        expected = sha256_prefixed(
            content_hash_payload(material), label=f"{label}_row"
        )
        if row.get("row_hash") != expected:
            reasons.append(f"row_hash_mismatch:{index}")
        prior_hash = str(row.get("row_hash") or "")
    return {
        "status": "PASS" if not reasons else "FAIL",
        "reasons": sorted(set(reasons)),
        "row_count": len(rows),
        "stream_hash": prior_hash,
    }


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    content = path.read_text(encoding="utf-8")
    if content and not content.endswith("\n"):
        raise ValueError("hash_chain_unterminated_final_line")
    rows: list[dict[str, Any]] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("hash-chain row must be an object")
        rows.append(value)
    return rows


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_suffix(path.suffix + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    lock_module: Any | None = None
    try:
        try:
            import fcntl
        except ImportError as exc:
            raise RuntimeError("hash_chain_process_lock_unavailable") from exc
        lock_module = fcntl
        lock_module.flock(fd, lock_module.LOCK_EX)
        yield
    finally:
        try:
            if lock_module is not None:
                lock_module.flock(fd, lock_module.LOCK_UN)
        finally:
            os.close(fd)
