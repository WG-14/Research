from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .hashing import content_hash_payload, sha256_prefixed


_EXPECTED_STREAM_HASH_UNSET = object()


def append_hash_chained_jsonl(
    *,
    store: Any,
    path: Path,
    payload: dict[str, Any],
    label: str,
    expected_stream_hash: str | None | object = _EXPECTED_STREAM_HASH_UNSET,
) -> dict[str, Any]:
    with _locked(path):
        validation = validate_hash_chained_jsonl(path=path, label=label)
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


def validate_hash_chained_jsonl(*, path: Path, label: str) -> dict[str, Any]:
    rows = _read_rows(path)
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
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
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
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:
            pass
        yield
    finally:
        try:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            except ImportError:
                pass
        finally:
            os.close(fd)
