#!/usr/bin/env python3
"""Produce a deterministic, non-destructive backup retention plan."""

from __future__ import annotations

import argparse
import json
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backup_evidence import (
    EvidenceError,
    read_offsite_receipt,
    verify_backup_directory,
    verify_offsite_receipt,
)

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _safe_directory(path: Path, code: str) -> None:
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path.absolute():
            raise OSError
        link_status = path.lstat()
        status = path.stat()
    except OSError as error:
        raise SystemExit(f"retention_invalid:{code}") from error
    if stat.S_ISLNK(link_status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise SystemExit(f"retention_invalid:{code}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--receipt-root", type=Path, required=True)
    parser.add_argument("--backup-verification-public-key", type=Path, required=True)
    parser.add_argument(
        "--offsite-receipt-verification-public-key",
        type=Path,
        required=True,
    )
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--encryption", choices=("age", "kms-envelope"), required=True)
    parser.add_argument("--encryption-key-id", required=True)
    parser.add_argument("--retention-days", type=int, required=True)
    parser.add_argument("--minimum-count", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true", required=True)
    args = parser.parse_args()
    if not all(
        path.is_absolute()
        for path in (
            args.backup_root,
            args.receipt_root,
            args.backup_verification_public_key,
            args.offsite_receipt_verification_public_key,
        )
    ):
        raise SystemExit("retention_invalid:absolute_path")
    if not 7 <= args.retention_days <= 3650 or not 2 <= args.minimum_count <= 1000:
        raise SystemExit("retention_invalid:policy")
    _safe_directory(args.backup_root, "backup_root")
    _safe_directory(args.receipt_root, "receipt_root")

    complete: list[tuple[float, str]] = []
    incomplete: list[str] = []
    held: list[str] = []
    for candidate in args.backup_root.iterdir():
        if not _UUID.fullmatch(candidate.name):
            continue
        try:
            if candidate.is_symlink() or not candidate.is_dir():
                incomplete.append(candidate.name)
                continue
            receipt = args.receipt_root / f"{candidate.name}.json"
            if (candidate / "LEGAL_HOLD").exists() or (
                args.receipt_root / f"{candidate.name}.LEGAL_HOLD"
            ).exists():
                held.append(candidate.name)
                continue
            manifest_hash, created_at = verify_backup_directory(
                candidate,
                public_key=args.backup_verification_public_key,
                backup_id=candidate.name,
            )
            offsite_receipt = read_offsite_receipt(receipt)
            verify_offsite_receipt(
                offsite_receipt,
                public_key=args.offsite_receipt_verification_public_key,
                backup_id=candidate.name,
                manifest_hash=manifest_hash,
                target_id=args.target_id,
                encryption=args.encryption,
                encryption_key_id=args.encryption_key_id,
                maximum_age=None,
            )
            complete.append((created_at.timestamp(), candidate.name))
        except (EvidenceError, OSError):
            incomplete.append(candidate.name)

    complete.sort(reverse=True)
    cutoff = datetime.now(UTC) - timedelta(days=args.retention_days)
    eligible = [
        backup_id
        for index, (modified, backup_id) in enumerate(complete)
        if index >= args.minimum_count
        and datetime.fromtimestamp(modified, UTC) < cutoff
    ]
    result = {
        "schema_version": 1,
        "mode": "dry-run",
        "retention_days": args.retention_days,
        "minimum_count": args.minimum_count,
        "complete_count": len(complete),
        "eligible_backup_ids": sorted(eligible),
        "legal_hold_backup_ids": sorted(held),
        "incomplete_backup_ids": sorted(incomplete),
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
