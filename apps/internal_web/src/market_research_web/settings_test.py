from __future__ import annotations

import os

os.environ.setdefault(
    "INTERNAL_WEB_SECRET_KEY",
    "test-only-not-for-production-0123456789abcdef",
)

from .settings import *  # noqa: F403,E402


PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_HSTS_SECONDS = 0
# Preserve the legacy single-file fixture surface unless a segmented audit
# test opts in explicitly.  Production settings use bounded segments.
INTERNAL_WEB_AUDIT_SEGMENT_ROWS = 0
