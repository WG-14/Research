from __future__ import annotations

from django.urls import path

from . import views


app_name = "portal"
urlpatterns = [
    path("login/", views.PortalLoginView.as_view(), name="login"),
    path("logout/", views.PortalLogoutView.as_view(), name="logout"),
    path("", views.dashboard, name="dashboard"),
    path("research/new/", views.manifest_upload, name="manifest-upload"),
    path("manifests/<uuid:pk>/", views.manifest_detail, name="manifest-detail"),
    path(
        "manifests/<uuid:pk>/preflight/",
        views.manifest_preflight,
        name="manifest-preflight",
    ),
    path("jobs/", views.job_list, name="job-list"),
    path("jobs/<uuid:pk>/", views.job_detail, name="job-detail"),
    path("jobs/<uuid:pk>/status/", views.job_status, name="job-status"),
    path("jobs/<uuid:pk>/cancel/", views.job_cancel, name="job-cancel"),
    path(
        "jobs/<uuid:pk>/validate/",
        views.job_submit_validation,
        name="job-submit-validation",
    ),
    path("jobs/<uuid:pk>/download/", views.job_download, name="job-download"),
    path("reports/", views.report_list, name="report-list"),
    path("reports/compare/", views.report_compare, name="report-compare"),
    path("review/", views.review_queue, name="review-queue"),
    path("review/<uuid:pk>/", views.review_detail, name="review-detail"),
    path("review/<uuid:pk>/record/", views.review_record, name="review-record"),
    path("review/<uuid:pk>/approve/", views.review_approve, name="review-approve"),
]
