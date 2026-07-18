from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import admin
from django.contrib.admin.exceptions import NotRegistered
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.http import HttpRequest

from .models import ManifestUpload, ResearchJob


if TYPE_CHECKING:

    class _ManifestUploadAdminBase(admin.ModelAdmin[ManifestUpload]):
        pass

    class _ResearchJobAdminBase(admin.ModelAdmin[ResearchJob]):
        pass

else:

    class _ManifestUploadAdminBase(admin.ModelAdmin):
        pass

    class _ResearchJobAdminBase(admin.ModelAdmin):
        pass


# Identity and role lifecycle is external to this web adapter and requires a
# separate approval authority.  Leaving Django's default auth registrations in
# place would let any superuser grant research_approver outside that boundary.
for identity_model in (get_user_model(), Group, Permission):
    try:
        admin.site.unregister(identity_model)
    except NotRegistered:
        pass


@admin.register(ManifestUpload)
class ManifestUploadAdmin(_ManifestUploadAdminBase):
    list_display = (
        "experiment_id",
        "strategy_name",
        "owner",
        "content_hash",
        "created_at",
    )
    search_fields = ("experiment_id", "strategy_name", "content_hash")
    list_filter = ("strategy_name", "created_at")
    readonly_fields = (
        "id",
        "owner",
        "display_name",
        "storage_ref",
        "content_hash",
        "manifest_hash",
        "size_bytes",
        "experiment_id",
        "strategy_name",
        "created_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: ManifestUpload | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: ManifestUpload | None = None,
    ) -> bool:
        return False


@admin.register(ResearchJob)
class ResearchJobAdmin(_ResearchJobAdminBase):
    list_display = (
        "id",
        "capability_id",
        "status",
        "owner",
        "progress_stage",
        "created_at",
        "finished_at",
    )
    search_fields = ("id", "run_id", "request_hash", "result_hash", "error_code")
    list_filter = ("status", "capability_id", "created_at")
    readonly_fields = tuple(field.name for field in ResearchJob._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: ResearchJob | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: ResearchJob | None = None,
    ) -> bool:
        return False
