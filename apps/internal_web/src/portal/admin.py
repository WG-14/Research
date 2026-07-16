from __future__ import annotations

from django.contrib import admin

from .models import ManifestUpload, ResearchJob


@admin.register(ManifestUpload)
class ManifestUploadAdmin(admin.ModelAdmin):
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

    def has_add_permission(self, request):  # type: ignore[no-untyped-def]
        return False

    def has_change_permission(self, request, obj=None):  # type: ignore[no-untyped-def]
        return False

    def has_delete_permission(self, request, obj=None):  # type: ignore[no-untyped-def]
        return False


@admin.register(ResearchJob)
class ResearchJobAdmin(admin.ModelAdmin):
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
    readonly_fields = tuple(
        field.name for field in ResearchJob._meta.fields
    )

    def has_add_permission(self, request):  # type: ignore[no-untyped-def]
        return False

    def has_change_permission(self, request, obj=None):  # type: ignore[no-untyped-def]
        return False

    def has_delete_permission(self, request, obj=None):  # type: ignore[no-untyped-def]
        return False

