#!/usr/bin/env python3
from __future__ import annotations

import argparse
import tarfile
from pathlib import Path, PurePosixPath


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    if not args.archive.is_absolute() or not args.archive.is_file():
        raise SystemExit("archive_invalid")
    if not args.destination.is_absolute() or not args.destination.is_dir():
        raise SystemExit("destination_invalid")
    with tarfile.open(args.archive, "r:*") as bundle:
        members = bundle.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise SystemExit("archive_path_invalid")
            if not (member.isdir() or member.isfile()):
                raise SystemExit("archive_member_type_invalid")
        bundle.extractall(args.destination, members=members, filter="data")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
