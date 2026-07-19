from __future__ import annotations

from django.urls import path

from . import api_views, views


app_name = "portal"
urlpatterns = [
    path("api/v1/openapi.json", api_views.openapi_document, name="api-openapi"),
    path("api/v1/jobs/", api_views.job_list, name="api-job-list"),
    path(
        "api/v1/manifests/<uuid:manifest_id>/jobs/",
        api_views.job_submit,
        name="api-job-submit",
    ),
    path(
        "api/v1/jobs/<uuid:job_id>/",
        api_views.job_detail,
        name="api-job-detail",
    ),
    path(
        "api/v1/jobs/<uuid:job_id>/cancel/",
        api_views.job_cancel,
        name="api-job-cancel",
    ),
    path(
        "api/v1/research/lineage/",
        api_views.research_lineage_list,
        name="api-research-lineage-list",
    ),
    path(
        "api/v1/research/lineage/<str:record_type>/<str:logical_id>/<str:version>/",
        api_views.research_lineage_detail,
        name="api-research-lineage-detail",
    ),
    path(
        "api/v1/research/validation-decisions/",
        api_views.validation_decision_list,
        name="api-validation-decision-list",
    ),
    path(
        "api/v1/research/validation-decisions/<str:logical_id>/<str:version>/",
        api_views.validation_decision_detail,
        name="api-validation-decision-detail",
    ),
    path(
        "api/v1/research/prospective/",
        api_views.prospective_validation_list,
        name="api-prospective-validation-list",
    ),
    path(
        "api/v1/research/prospective/<str:logical_id>/<str:version>/",
        api_views.prospective_validation_detail,
        name="api-prospective-validation-detail",
    ),
    path(
        "api/v1/research/datasets/",
        api_views.dataset_artifact_list,
        name="api-dataset-artifact-list",
    ),
    path(
        "api/v1/research/datasets/<str:logical_id>/<str:version>/",
        api_views.dataset_artifact_detail,
        name="api-dataset-artifact-detail",
    ),
    path(
        "api/v1/research/features/",
        api_views.feature_definition_list,
        name="api-feature-definition-list",
    ),
    path(
        "api/v1/research/features/<str:logical_id>/<str:version>/",
        api_views.feature_definition_detail,
        name="api-feature-definition-detail",
    ),
    # Keep the fixed diff route before the dynamic package identity routes.
    path(
        "api/v1/research/packages/diff/",
        api_views.research_package_diff_view,
        name="api-research-package-diff",
    ),
    path(
        "api/v1/research/packages/",
        api_views.research_package_list,
        name="api-research-package-list",
    ),
    path(
        "api/v1/research/packages/<str:logical_id>/<str:version>/lineage/",
        api_views.research_package_lineage_view,
        name="api-research-package-lineage",
    ),
    path(
        "api/v1/research/packages/<str:logical_id>/<str:version>/",
        api_views.research_package_detail,
        name="api-research-package-detail",
    ),
    path("login/", views.PortalLoginView.as_view(), name="login"),
    path("logout/", views.PortalLogoutView.as_view(), name="logout"),
    path("", views.dashboard, name="dashboard"),
    path("research/", views.research_explorer, name="research-explorer"),
    path(
        "research/<str:section>/<str:logical_id>/<str:version>/",
        views.research_explorer_detail,
        name="research-explorer-detail",
    ),
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
    path("reports/import/", views.report_import, name="report-import"),
    path("reports/compare/", views.report_compare, name="report-compare"),
    path("review/", views.review_queue, name="review-queue"),
    path("review/<uuid:pk>/", views.review_detail, name="review-detail"),
    path("review/<uuid:pk>/record/", views.review_record, name="review-record"),
    path("review/<uuid:pk>/approve/", views.review_approve, name="review-approve"),
]
