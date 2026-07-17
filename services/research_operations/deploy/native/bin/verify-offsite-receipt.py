#!/usr/bin/env python3
"""Verify the site hook's detached off-site export receipt."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn

from backup_evidence import (
    EvidenceError,
    read_offsite_receipt,
    verify_offsite_receipt,
)

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _fail(code: str) -> NoReturn:
    raise SystemExit(f"offsite_receipt_invalid:{code}")


def _manifest_hash(path: Path) -> str:
    try:
        link_status = path.lstat()
        status = path.stat()
        if (
            stat.S_ISLNK(link_status.st_mode)
            or not stat.S_ISREG(status.st_mode)
            or status.st_size < 1
            or status.st_size > 2 * 1024 * 1024
        ):
            _fail("manifest")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            payload = handle.read(2 * 1024 * 1024 + 1)
    except OSError:
        _fail("manifest")
    if not payload or len(payload) > 2 * 1024 * 1024:
        _fail("manifest")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--backup-directory", type=Path, required=True)
    parser.add_argument("--backup-id", required=True)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--encryption", choices=("age", "kms-envelope"), required=True)
    parser.add_argument("--encryption-key-id", required=True)
    parser.add_argument("--verification-public-key", type=Path, required=True)
    args = parser.parse_args()
    if not _UUID.fullmatch(args.backup_id):
        _fail("backup_id")
    if not args.receipt.is_absolute() or not args.backup_directory.is_absolute():
        _fail("absolute_path")
    if args.backup_directory.name != args.backup_id:
        _fail("backup_directory")
    try:
        link_status = args.backup_directory.lstat()
        status = args.backup_directory.stat()
        if (
            stat.S_ISLNK(link_status.st_mode)
            or not stat.S_ISDIR(status.st_mode)
            or args.backup_directory.resolve(strict=True)
            != args.backup_directory.absolute()
        ):
            _fail("backup_directory")
    except OSError:
        _fail("backup_directory")

    try:
        receipt = read_offsite_receipt(args.receipt)
        verify_offsite_receipt(
            receipt,
            public_key=args.verification_public_key,
            backup_id=args.backup_id,
            manifest_hash=_manifest_hash(args.backup_directory / "manifest.json"),
            target_id=args.target_id,
            encryption=args.encryption,
            encryption_key_id=args.encryption_key_id,
            now=datetime.now(UTC),
            maximum_age=timedelta(days=1),
        )
    except EvidenceError as error:
        _fail(str(error))
    print(f"offsite_receipt_ok:{args.backup_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
