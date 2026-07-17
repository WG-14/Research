#!/usr/bin/env python3
"""Render the native Nginx site without shell interpolation."""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import stat
import tempfile
from pathlib import Path

_DNS = re.compile(
    r"(?=^.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_TOKEN = "@@EMPLOYEE_SERVER_NAME@@"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--server-name", required=True)
    args = parser.parse_args()
    if (
        not args.template.is_absolute()
        or not args.output.is_absolute()
        or not _DNS.fullmatch(args.server_name)
        or args.server_name.endswith(".example")
    ):
        raise SystemExit("nginx_render_invalid:argument")
    try:
        template_status = args.template.lstat()
        if stat.S_ISLNK(template_status.st_mode) or not stat.S_ISREG(
            template_status.st_mode
        ):
            raise SystemExit("nginx_render_invalid:template")
        if args.output.is_symlink() or not args.output.parent.is_dir():
            raise SystemExit("nginx_render_invalid:output")
        source = args.template.read_text(encoding="utf-8")
    except OSError as error:
        raise SystemExit("nginx_render_invalid:path") from error
    if source.count(_TOKEN) != 4:
        raise SystemExit("nginx_render_invalid:template_contract")
    payload = source.replace(_TOKEN, args.server_name).encode()
    descriptor, temporary = tempfile.mkstemp(
        dir=args.output.parent, prefix=".research-operations.", suffix=".conf"
    )
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, args.output)
        directory = os.open(args.output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
