from __future__ import annotations

from django.contrib import admin
from django.urls import include, path


urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("portal.urls")),
]

handler403 = "portal.views.permission_denied"
handler404 = "portal.views.not_found"
handler500 = "portal.views.server_error"
