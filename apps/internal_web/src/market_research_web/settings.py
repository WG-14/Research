from __future__ import annotations

import os
from pathlib import Path

from market_research.paths import ResearchPathManager
from market_research.settings import ResearchSettings

from .database import build_database_settings


def _bool_env(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be an explicit boolean")


def _csv_env(name: str, *, default: tuple[str, ...] = ()) -> list[str]:
    value = os.getenv(name)
    if value is None:
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


def _absolute_csv_paths_env(name: str) -> tuple[Path, ...]:
    paths: list[Path] = []
    for item in _csv_env(name):
        candidate = Path(item)
        if (
            not candidate.is_absolute()
            or "\x00" in item
            or any(part in {".", ".."} for part in candidate.parts)
        ):
            raise RuntimeError(f"{name} must contain only absolute paths")
        if candidate == Path(candidate.anchor):
            raise RuntimeError(f"{name} must not allowlist a filesystem root")
        if candidate not in paths:
            paths.append(candidate)
    return tuple(paths)


def _positive_int_env(name: str, *, default: int) -> int:
    value = os.getenv(name)
    try:
        parsed = default if value is None else int(value.strip())
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return parsed


def _bounded_positive_int_env(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = os.getenv(name)
    if value is None:
        parsed = default
    elif not value or not value.isascii() or not value.isdecimal():
        raise RuntimeError(f"{name} must be an ASCII integer")
    else:
        parsed = int(value)
    if parsed < minimum or parsed > maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _repository_external_path_env(name: str, *, default: Path) -> Path:
    raw = os.getenv(name)
    path = default if raw is None else Path(raw).expanduser()
    if not path.is_absolute():
        raise RuntimeError(f"{name} must be an absolute path")
    resolved = path.resolve(strict=False)
    if resolved.is_relative_to(REPOSITORY_ROOT.resolve()):
        raise RuntimeError(f"{name} must be outside the repository")
    return resolved


def _source_root_env() -> Path:
    raw = os.getenv("RESEARCH_OPS_SOURCE_ROOT")
    if raw is None:
        return Path(__file__).resolve().parents[4]
    path = Path(raw).expanduser()
    if not path.is_absolute() or path == Path(path.anchor):
        raise RuntimeError("RESEARCH_OPS_SOURCE_ROOT must be an absolute non-root path")
    return path.resolve(strict=False)


BASE_DIR = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = _source_root_env()
RESEARCH_SETTINGS = ResearchSettings.from_env()
RESEARCH_PATHS = ResearchPathManager.from_settings(
    RESEARCH_SETTINGS,
    project_root=REPOSITORY_ROOT,
)

INTERNAL_WEB_STATE_ROOT = RESEARCH_PATHS.artifact_path("_internal_web")
INTERNAL_WEB_DATABASE_PATH = RESEARCH_PATHS.artifact_path(
    "_internal_web", "operations.sqlite3"
)
INTERNAL_WEB_MANIFEST_ROOT = RESEARCH_PATHS.dataset_path("_internal_web", "manifests")
INTERNAL_WEB_AUDIT_PATH = RESEARCH_PATHS.artifact_path(
    "_internal_web", "audit", "web_audit.jsonl"
)
INTERNAL_WEB_REPORT_IMPORT_ROOTS = _absolute_csv_paths_env(
    "INTERNAL_WEB_REPORT_IMPORT_ROOTS"
)
INTERNAL_WEB_STATE_ROOT.mkdir(parents=True, exist_ok=True)

SECRET_KEY = os.getenv("INTERNAL_WEB_SECRET_KEY", "").strip()
if not SECRET_KEY:
    raise RuntimeError("INTERNAL_WEB_SECRET_KEY is required")

DEBUG = False
ALLOWED_HOSTS = _csv_env(
    "INTERNAL_WEB_ALLOWED_HOSTS",
    default=("localhost", "127.0.0.1", "[::1]", "testserver"),
)
CSRF_TRUSTED_ORIGINS = _csv_env("INTERNAL_WEB_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "portal.apps.PortalConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "market_research_web.middleware.CorrelationIdMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "market_research_web.middleware.ResponseSecurityHeadersMiddleware",
]

ROOT_URLCONF = "market_research_web.urls"
WSGI_APPLICATION = "market_research_web.wsgi.application"
ASGI_APPLICATION = "market_research_web.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

DATABASES = build_database_settings(sqlite_path=INTERNAL_WEB_DATABASE_PATH)

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ko-kr"
TIME_ZONE = os.getenv("INTERNAL_WEB_TIME_ZONE", "Asia/Seoul")
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STATIC_URL = "/static/"
STATIC_ROOT = _repository_external_path_env(
    "INTERNAL_WEB_STATIC_ROOT",
    default=RESEARCH_PATHS.artifact_path("_internal_web", "static"),
)
LOGIN_URL = "portal:login"
LOGIN_REDIRECT_URL = "portal:dashboard"
LOGOUT_REDIRECT_URL = "portal:login"

FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 3 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FILES = 1
INTERNAL_WEB_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
INTERNAL_WEB_MAX_RESULT_BYTES = 64 * 1024 * 1024
INTERNAL_WEB_MAX_PARAMETER_CANDIDATES = _positive_int_env(
    "INTERNAL_WEB_MAX_PARAMETER_CANDIDATES",
    default=4096,
)
INTERNAL_WEB_MAX_EXECUTION_SCENARIOS = _positive_int_env(
    "INTERNAL_WEB_MAX_EXECUTION_SCENARIOS",
    default=32,
)
INTERNAL_WEB_MAX_WORK_UNITS = _positive_int_env(
    "INTERNAL_WEB_MAX_WORK_UNITS",
    default=32768,
)
INTERNAL_WEB_LOGIN_FAILURE_LIMIT = _bounded_positive_int_env(
    "INTERNAL_WEB_LOGIN_FAILURE_LIMIT",
    default=5,
    minimum=1,
    maximum=100,
)
INTERNAL_WEB_LOGIN_FAILURE_WINDOW_SECONDS = _bounded_positive_int_env(
    "INTERNAL_WEB_LOGIN_FAILURE_WINDOW_SECONDS",
    default=900,
    minimum=60,
    maximum=86400,
)
INTERNAL_WEB_LOGIN_BLOCK_SECONDS = _bounded_positive_int_env(
    "INTERNAL_WEB_LOGIN_BLOCK_SECONDS",
    default=900,
    minimum=60,
    maximum=86400,
)
INTERNAL_WEB_JOB_LEASE_SECONDS = 120
INTERNAL_WEB_AUDIT_SEGMENT_ROWS = _bounded_positive_int_env(
    "INTERNAL_WEB_AUDIT_SEGMENT_ROWS",
    default=10_000,
    minimum=2,
    maximum=1_000_000,
)
FILE_UPLOAD_HANDLERS = [
    "portal.upload_handlers.BoundedManifestUploadHandler",
    "django.core.files.uploadhandler.MemoryFileUploadHandler",
    "django.core.files.uploadhandler.TemporaryFileUploadHandler",
]

SESSION_COOKIE_HTTPONLY = True
SECURE_BROWSER_COOKIES = _bool_env("INTERNAL_WEB_SECURE_COOKIES", default=True)
SESSION_COOKIE_SECURE = SECURE_BROWSER_COOKIES
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = SECURE_BROWSER_COOKIES
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_SSL_REDIRECT = _bool_env("INTERNAL_WEB_SECURE_SSL_REDIRECT", default=True)
SECURE_HSTS_SECONDS = int(os.getenv("INTERNAL_WEB_HSTS_SECONDS", "3600"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = _bool_env(
    "INTERNAL_WEB_HSTS_INCLUDE_SUBDOMAINS", default=False
)
SECURE_HSTS_PRELOAD = _bool_env("INTERNAL_WEB_HSTS_PRELOAD", default=False)
# Private DNS names are not eligible for the public browser preload list.  A
# deliberate false setting therefore silences only that inapplicable deploy
# warning; HSTS itself, HTTPS redirect, and secure cookies stay enforced.
SILENCED_SYSTEM_CHECKS = [] if SECURE_HSTS_PRELOAD else ["security.W021"]
X_FRAME_OPTIONS = "DENY"

if _bool_env("INTERNAL_WEB_TRUST_X_FORWARDED_PROTO", default=False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
