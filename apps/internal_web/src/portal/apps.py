from __future__ import annotations

from django.apps import AppConfig


class PortalConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "portal"
    verbose_name = "Market Research Portal"

    def ready(self) -> None:
        # Import exactly once through AppConfig so Django authentication
        # signals cannot be bypassed by choosing a different login surface.
        from . import auth_audit

        _ = auth_audit
