from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from market_research.application.adapter_contracts import (
    ManifestValidationError,
    parse_manifest_with_registry,
)
from market_research.research_composition import builtin_strategy_registry

from .admission import validate_raw_manifest_admission
from .audit import append_web_audit_event, record_web_audit_event
from .jobs import EnqueueResult, enqueue_research_job
from .models import ManifestUpload, ResearchJob
from .models import ImportedDecisionReport
from .report_imports import (
    HistoricalReportImportResult,
    import_historical_decision_report,
)
from .security import (
    actor_snapshot,
    normalize_display_filename,
    validate_manifest_reference_paths,
)
from .storage import publish_manifest_bytes


ALLOWED_MANIFEST_CONTENT_TYPES = frozenset(
    {"application/json", "text/json", "text/plain"}
)
UPLOAD_ERROR_MESSAGES = {
    "upload_filename_invalid": "파일 이름을 확인해 주세요.",
    "upload_filename_must_not_contain_path": "파일 이름에 경로를 포함할 수 없습니다.",
    "manifest_upload_requires_json_extension": "JSON 파일(.json)을 선택해 주세요.",
    "manifest_upload_content_type_not_allowed": "JSON 내용으로 인식되는 파일만 등록할 수 있습니다.",
    "manifest_upload_too_large": "파일이 2 MB 크기 제한을 넘었습니다.",
    "manifest_upload_empty": "빈 파일은 등록할 수 없습니다.",
    "manifest_upload_json_invalid": "JSON 형식을 해석할 수 없습니다.",
    "manifest_upload_must_be_object": "manifest의 최상위 내용은 JSON 객체여야 합니다.",
    "manifest_contract_invalid": "Research Semantics v2 manifest 규칙을 충족하지 못했습니다.",
    "manifest_local_reference_must_be_absolute": "manifest의 데이터 참조를 서버 정책에 맞게 준비해 주세요.",
    "path_outside_configured_root": "허용된 데이터 저장소 밖의 참조는 사용할 수 없습니다.",
    "path_symlink_component_rejected": "심볼릭 링크를 통한 데이터 참조는 사용할 수 없습니다.",
    "manifest_admission_candidate_limit_exceeded": "후보 조합 수가 웹 실행 한도를 넘었습니다.",
    "manifest_admission_scenario_limit_exceeded": "실행 시나리오 수가 웹 실행 한도를 넘었습니다.",
    "manifest_admission_work_unit_limit_exceeded": "전체 연구 작업량이 웹 실행 한도를 넘었습니다.",
}


class ManifestExperimentConflict(ValidationError):
    def __init__(self) -> None:
        super().__init__("manifest_experiment_id_conflict")


@dataclass(frozen=True, slots=True)
class ValidatedManifestUpload:
    display_name: str
    content: bytes
    content_hash: str
    manifest_hash: str
    size_bytes: int
    experiment_id: str
    strategy_name: str


def validate_manifest_upload(upload: Any) -> ValidatedManifestUpload:
    display_name = normalize_display_filename(getattr(upload, "name", ""))
    if not display_name.lower().endswith(".json"):
        raise ValidationError("manifest_upload_requires_json_extension")
    content_type = str(getattr(upload, "content_type", "") or "").lower()
    if content_type not in ALLOWED_MANIFEST_CONTENT_TYPES:
        raise ValidationError("manifest_upload_content_type_not_allowed")
    limit = int(settings.INTERNAL_WEB_MAX_MANIFEST_BYTES)
    chunks: list[bytes] = []
    observed = 0
    for chunk in upload.chunks():
        observed += len(chunk)
        if observed > limit:
            raise ValidationError("manifest_upload_too_large")
        chunks.append(bytes(chunk))
    content = b"".join(chunks)
    if not content:
        raise ValidationError("manifest_upload_empty")
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("manifest_upload_json_invalid") from exc
    if not isinstance(payload, dict):
        raise ValidationError("manifest_upload_must_be_object")
    validate_raw_manifest_admission(payload)
    validate_manifest_reference_paths(
        payload,
        data_root=settings.RESEARCH_PATHS.data_root,
    )
    try:
        manifest = parse_manifest_with_registry(
            payload,
            registry=builtin_strategy_registry(),
        )
    except (ManifestValidationError, ValueError) as exc:
        raise ValidationError(f"manifest_contract_invalid:{exc}") from exc
    return ValidatedManifestUpload(
        display_name=display_name,
        content=content,
        content_hash="sha256:" + hashlib.sha256(content).hexdigest(),
        manifest_hash=manifest.manifest_hash(),
        size_bytes=len(content),
        experiment_id=manifest.experiment_id,
        strategy_name=manifest.strategy_name,
    )


class ManifestUploadForm(forms.Form):
    display_name = forms.CharField(
        label="화면에 표시할 이름",
        max_length=255,
        required=False,
        help_text="비워 두면 업로드한 파일 이름을 사용합니다.",
    )
    manifest_file = forms.FileField(
        label="연구 정의 파일",
        help_text="JSON 형식, 최대 2 MiB",
    )

    def clean_display_name(self) -> str:
        value = str(self.cleaned_data.get("display_name") or "").strip()
        if not value:
            return ""
        try:
            return normalize_display_filename(value)
        except ValidationError as exc:
            raise forms.ValidationError(
                "화면에 표시할 이름에는 경로나 제어 문자를 사용할 수 없습니다."
            ) from exc

    def clean_manifest_file(self) -> Any:
        upload = self.cleaned_data["manifest_file"]
        try:
            self.validated_upload = validate_manifest_upload(upload)
        except ValidationError as exc:
            raw = str(exc.messages[0] if exc.messages else "")
            code = raw.split(":", 1)[0]
            raise forms.ValidationError(
                UPLOAD_ERROR_MESSAGES.get(
                    code,
                    "파일을 검증하지 못했습니다. manifest 형식과 데이터 참조를 확인해 주세요.",
                )
            ) from exc
        return upload

    def save(
        self,
        *,
        owner: Any,
        correlation_id: str | uuid.UUID,
    ) -> tuple[ManifestUpload, bool]:
        if not self.is_valid():
            raise ValueError("cannot_save_invalid_manifest_upload_form")
        if not owner.has_perm("portal.upload_research_manifest"):
            raise PermissionDenied("research_manifest_upload_permission_required")
        validated = self.validated_upload
        display_name = self.cleaned_data["display_name"] or validated.display_name
        actor_id, _roles, _permissions = actor_snapshot(owner)
        existing = ManifestUpload.objects.filter(
            experiment_id=validated.experiment_id
        ).first()
        if existing is not None:
            if (
                existing.owner_id != owner.pk
                or existing.content_hash != validated.content_hash
            ):
                raise ManifestExperimentConflict()
            record, created = existing, False
            append_web_audit_event(
                action="research_manifest_reused",
                actor_id=actor_id,
                object_type="manifest_upload",
                object_id=str(record.pk),
                correlation_id=str(correlation_id),
                details={
                    "content_hash": record.content_hash,
                    "manifest_hash": record.manifest_hash,
                    "experiment_id": record.experiment_id,
                },
            )
            return record, created
        else:
            storage_ref = publish_manifest_bytes(
                content=validated.content,
                content_hash=validated.content_hash,
            )
            try:
                with transaction.atomic():
                    record, created = ManifestUpload.objects.get_or_create(
                        experiment_id=validated.experiment_id,
                        defaults={
                            "owner": owner,
                            "display_name": display_name,
                            "storage_ref": str(storage_ref),
                            "content_hash": validated.content_hash,
                            "manifest_hash": validated.manifest_hash,
                            "size_bytes": validated.size_bytes,
                            "strategy_name": validated.strategy_name,
                        },
                    )
                    if not created and (
                        record.owner_id != owner.pk
                        or record.content_hash != validated.content_hash
                    ):
                        raise ManifestExperimentConflict()
                    record_web_audit_event(
                        action=(
                            "research_manifest_uploaded"
                            if created
                            else "research_manifest_reused"
                        ),
                        actor_id=actor_id,
                        object_type="manifest_upload",
                        object_id=str(record.pk),
                        correlation_id=str(correlation_id),
                        details={
                            "content_hash": record.content_hash,
                            "manifest_hash": record.manifest_hash,
                            "experiment_id": record.experiment_id,
                        },
                    )
            except IntegrityError as exc:
                raise ManifestExperimentConflict() from exc
        return record, created


class HistoricalDecisionReportImportForm(forms.Form):
    source_path = forms.CharField(
        label="허용된 CLI 보고서 절대 경로",
        max_length=4096,
        widget=forms.PasswordInput(render_value=False),
    )
    expected_report_hash = forms.CharField(
        label="예상 보고서 hash",
        max_length=71,
    )
    expected_manifest_hash = forms.CharField(
        label="예상 manifest hash",
        max_length=71,
    )
    expected_experiment_id = forms.CharField(
        label="예상 experiment ID",
        max_length=255,
    )
    expected_run_id = forms.CharField(
        label="예상 run ID",
        max_length=255,
    )
    expected_dataset_snapshot_id = forms.CharField(
        label="예상 dataset snapshot ID",
        max_length=255,
    )
    expected_dataset_content_hash = forms.CharField(
        label="예상 dataset content hash",
        max_length=71,
    )
    code_revision = forms.RegexField(
        label="실행 코드 revision",
        regex=r"^[0-9a-f]{7,64}$",
        max_length=64,
    )
    owner = forms.ModelChoiceField(
        label="보고서 소유자",
        queryset=get_user_model().objects.none(),
    )
    visibility = forms.ChoiceField(
        label="공개 범위",
        choices=ImportedDecisionReport.Visibility.choices,
    )

    def __init__(self, *args: Any, operator: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.operator = operator
        self.fields["owner"].queryset = (
            get_user_model().objects.filter(is_active=True).order_by("username", "pk")
        )

    def save(
        self,
        *,
        correlation_id: str | uuid.UUID,
    ) -> HistoricalReportImportResult:
        if not self.is_valid():
            raise ValueError("cannot_save_invalid_historical_report_import_form")
        return import_historical_decision_report(
            actor=self.operator,
            owner=self.cleaned_data["owner"],
            source_path=self.cleaned_data["source_path"],
            expected_report_hash=self.cleaned_data["expected_report_hash"],
            expected_manifest_hash=self.cleaned_data["expected_manifest_hash"],
            expected_experiment_id=self.cleaned_data["expected_experiment_id"],
            expected_run_id=self.cleaned_data["expected_run_id"],
            expected_dataset_snapshot_id=(
                self.cleaned_data["expected_dataset_snapshot_id"]
            ),
            expected_dataset_content_hash=(
                self.cleaned_data["expected_dataset_content_hash"]
            ),
            code_revision=self.cleaned_data["code_revision"],
            visibility=self.cleaned_data["visibility"],
            correlation_id=str(correlation_id),
        )


class ResearchJobSubmissionForm(forms.Form):
    manifest = forms.ModelChoiceField(queryset=ManifestUpload.objects.none())
    capability_id = forms.ChoiceField(choices=ResearchJob.Capability.choices)
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args: Any, owner: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.owner = owner
        queryset = ManifestUpload.objects.all()
        if not owner.has_perm("portal.view_all_research_manifests"):
            queryset = queryset.filter(owner=owner)
        self.fields["manifest"].queryset = queryset.order_by("-created_at")

    def save(
        self,
        *,
        correlation_id: str | uuid.UUID,
        options: dict[str, Any] | None = None,
    ) -> EnqueueResult:
        if not self.is_valid():
            raise ValueError("cannot_save_invalid_job_submission_form")
        return enqueue_research_job(
            owner=self.owner,
            manifest=self.cleaned_data["manifest"],
            capability_id=self.cleaned_data["capability_id"],
            idempotency_key=str(self.cleaned_data["idempotency_key"]),
            options=options,
            correlation_id=correlation_id,
        )


class HumanReviewForm(forms.Form):
    decision = forms.ChoiceField(
        label="검토 의견",
        choices=(
            ("CHANGES_REQUESTED", "변경 요청"),
            ("REJECTED", "승인 대상 아님"),
        ),
    )
    rationale = forms.CharField(
        label="판단 근거",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 5}),
    )
    requirement_id = forms.CharField(
        label="요구사항 ID",
        max_length=255,
        required=False,
        help_text="변경 요청일 때만 입력합니다. 예: REQ-COST-1",
    )
    change_description = forms.CharField(
        label="필요한 변경",
        max_length=4000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    verification_condition = forms.CharField(
        label="완료 확인 기준",
        max_length=4000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean()
        change_fields = (
            "requirement_id",
            "change_description",
            "verification_condition",
        )
        values = tuple(str(cleaned.get(field) or "").strip() for field in change_fields)
        if cleaned.get("decision") == "CHANGES_REQUESTED":
            if not all(values):
                raise forms.ValidationError(
                    "변경 요청에는 요구사항 ID, 변경 내용, 완료 확인 기준이 모두 필요합니다."
                )
        elif any(values):
            raise forms.ValidationError(
                "승인 대상 아님 의견에는 변경 요구사항을 함께 기록할 수 없습니다."
            )
        return cleaned


class CandidateApprovalForm(forms.Form):
    approval_request_id = forms.UUIDField(
        widget=forms.HiddenInput,
        initial=uuid.uuid4,
    )
    rationale = forms.CharField(
        label="최종 승인 근거",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 5}),
    )
    resolved_requirement_ids = forms.CharField(
        label="해결한 요구사항 ID",
        required=False,
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="여러 ID는 쉼표 또는 줄바꿈으로 구분합니다.",
    )
    password = forms.CharField(
        label="현재 비밀번호 재확인",
        strip=False,
        widget=forms.PasswordInput,
    )
    confirm = forms.BooleanField(
        label="현재 결과 hash와 승인 영향 범위를 확인했습니다.",
    )

    def clean_resolved_requirement_ids(self) -> tuple[str, ...]:
        raw = str(self.cleaned_data.get("resolved_requirement_ids") or "")
        normalized = raw.replace(",", "\n")
        values = tuple(item.strip() for item in normalized.splitlines() if item.strip())
        if len(values) != len(set(values)):
            raise forms.ValidationError("해결한 요구사항 ID를 중복 입력할 수 없습니다.")
        return values
