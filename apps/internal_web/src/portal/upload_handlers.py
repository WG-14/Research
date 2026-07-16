from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.files.uploadhandler import FileUploadHandler, StopUpload


class BoundedManifestUploadHandler(FileUploadHandler):
    """Stop oversized multipart file bodies before Django stores them."""

    def __init__(self, request: Any | None = None) -> None:
        super().__init__(request)
        self.observed_bytes = 0

    def receive_data_chunk(self, raw_data: bytes, start: int) -> bytes:
        self.observed_bytes += len(raw_data)
        if self.observed_bytes > int(settings.INTERNAL_WEB_MAX_MANIFEST_BYTES):
            raise StopUpload(connection_reset=True)
        return raw_data

    def file_complete(self, file_size: int) -> None:
        return None
