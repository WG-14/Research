from __future__ import annotations

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.uploadhandler import StopUpload
from django.db import IntegrityError, transaction

from portal.admission import validate_raw_manifest_admission
from portal.forms import (
    ManifestExperimentConflict,
    ManifestUploadForm,
    validate_manifest_upload,
)
from portal.models import ManifestUpload
from portal.storage import resolve_artifact_ref
from portal.upload_handlers import BoundedManifestUploadHandler


def test_validated_manifest_upload_is_immutable_and_repository_external(
    runner_user,
    manifest_bytes: bytes,
) -> None:
    upload = SimpleUploadedFile(
        "research-manifest.json",
        manifest_bytes,
        content_type="application/json",
    )
    form = ManifestUploadForm(
        data={"display_name": "장기 SMA 검증"},
        files={"manifest_file": upload},
    )

    record, created = form.save(owner=runner_user, correlation_id=uuid.uuid4())

    assert created is True
    assert record.display_name == "장기 SMA 검증"
    assert record.storage_ref.startswith("data:_internal_web/manifests/")
    assert resolve_artifact_ref(record.storage_ref).read_bytes() == manifest_bytes
    record.display_name = "changed.json"
    with pytest.raises(ValidationError, match="manifest_upload_is_immutable"):
        record.save()
    with pytest.raises(ValidationError, match="manifest_upload_is_immutable"):
        record.delete()


def test_manifest_upload_reuses_same_owner_content(runner_user, manifest_bytes: bytes) -> None:
    def build_form() -> ManifestUploadForm:
        return ManifestUploadForm(
            files={
                "manifest_file": SimpleUploadedFile(
                    "same.json",
                    manifest_bytes,
                    content_type="application/json",
                )
            }
        )

    first, first_created = build_form().save(
        owner=runner_user,
        correlation_id=uuid.uuid4(),
    )
    second, second_created = build_form().save(
        owner=runner_user,
        correlation_id=uuid.uuid4(),
    )

    assert first_created is True
    assert second_created is False
    assert second.pk == first.pk


def test_manifest_experiment_id_is_global_and_conflicting_upload_fails_closed(
    runner_user,
    manifest_bytes: bytes,
) -> None:
    first_form = ManifestUploadForm(
        files={
            "manifest_file": SimpleUploadedFile(
                "first.json",
                manifest_bytes,
                content_type="application/json",
            )
        }
    )
    first, created = first_form.save(
        owner=runner_user,
        correlation_id=uuid.uuid4(),
    )
    assert created is True

    other = get_user_model().objects.create_user(
        username=f"runner-{uuid.uuid4().hex}",
        password="test-password",
    )
    other.groups.add(Group.objects.get(name="research_runner"))
    conflicting_payload = json.loads(manifest_bytes)
    conflicting_payload["hypothesis"] = "different manifest, same experiment identity"
    conflicting_bytes = json.dumps(conflicting_payload, sort_keys=True).encode("utf-8")
    conflicting_form = ManifestUploadForm(
        files={
            "manifest_file": SimpleUploadedFile(
                "conflicting.json",
                conflicting_bytes,
                content_type="application/json",
            )
        }
    )

    with pytest.raises(ManifestExperimentConflict) as raised:
        conflicting_form.save(owner=other, correlation_id=uuid.uuid4())

    assert raised.value.messages == ["manifest_experiment_id_conflict"]
    assert ManifestUpload.objects.count() == 1
    assert ManifestUpload.objects.get().pk == first.pk


def test_manifest_experiment_id_has_a_database_uniqueness_boundary(
    runner_user,
    manifest_record,
) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        ManifestUpload.objects.create(
            owner=runner_user,
            display_name="duplicate.json",
            storage_ref="data:_internal_web/manifests/duplicate.json",
            content_hash=f"sha256:{'3' * 64}",
            manifest_hash=f"sha256:{'4' * 64}",
            size_bytes=64,
            experiment_id=manifest_record.experiment_id,
            strategy_name="sma_with_filter",
        )


@pytest.mark.parametrize(
    ("name", "content_type", "content"),
    [
        ("manifest.txt", "application/json", b"{}"),
        ("manifest.json", "application/octet-stream", b"{}"),
        ("manifest.json", "application/json", b"not-json"),
    ],
)
def test_manifest_upload_rejects_bad_file_contract(
    name: str,
    content_type: str,
    content: bytes,
) -> None:
    upload = SimpleUploadedFile(name, content, content_type=content_type)
    with pytest.raises(ValidationError):
        validate_manifest_upload(upload)


def test_manifest_form_exposes_a_safe_localized_error() -> None:
    form = ManifestUploadForm(
        files={
            "manifest_file": SimpleUploadedFile(
                "bad.json",
                b"not-json",
                content_type="application/json",
            )
        }
    )

    assert form.is_valid() is False
    assert form.errors["manifest_file"] == ["JSON 형식을 해석할 수 없습니다."]


def test_manifest_upload_enforces_two_mib_limit(settings) -> None:
    settings.INTERNAL_WEB_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
    upload = SimpleUploadedFile(
        "too-large.json",
        b" " * (settings.INTERNAL_WEB_MAX_MANIFEST_BYTES + 1),
        content_type="application/json",
    )
    with pytest.raises(ValidationError, match="manifest_upload_too_large"):
        validate_manifest_upload(upload)


@pytest.mark.parametrize(
    ("payload", "setting_name", "limit", "error_code"),
    [
        (
            {"parameter_space": {"p": [1, 2, 3]}, "cost_model": {"slippage_bps": [1]}},
            "INTERNAL_WEB_MAX_PARAMETER_CANDIDATES",
            2,
            "manifest_admission_candidate_limit_exceeded",
        ),
        (
            {
                "parameter_space": {"p": [1]},
                "execution_model": {
                    "fee_rate": [0.0, 0.1],
                    "slippage_bps": [1, 2],
                },
            },
            "INTERNAL_WEB_MAX_EXECUTION_SCENARIOS",
            3,
            "manifest_admission_scenario_limit_exceeded",
        ),
        (
            {
                "parameter_space": {"p": [1, 2]},
                "execution_model": {"scenarios": [{}, {}]},
            },
            "INTERNAL_WEB_MAX_WORK_UNITS",
            3,
            "manifest_admission_work_unit_limit_exceeded",
        ),
    ],
)
def test_raw_manifest_admission_rejects_combinatorial_work_before_parsing(
    settings,
    payload,
    setting_name: str,
    limit: int,
    error_code: str,
) -> None:
    setattr(settings, setting_name, limit)
    with pytest.raises(ValidationError, match=error_code):
        validate_raw_manifest_admission(payload)


def test_manifest_upload_applies_raw_admission_before_core_parsing(
    settings,
    manifest_bytes: bytes,
) -> None:
    settings.INTERNAL_WEB_MAX_PARAMETER_CANDIDATES = 2
    payload = json.loads(manifest_bytes)
    payload["parameter_space"] = {"intentionally_unparsed": [1, 2, 3]}
    upload = SimpleUploadedFile(
        "too-many-candidates.json",
        json.dumps(payload).encode("utf-8"),
        content_type="application/json",
    )

    with pytest.raises(
        ValidationError,
        match="manifest_admission_candidate_limit_exceeded",
    ):
        validate_manifest_upload(upload)


def test_streaming_upload_handler_stops_before_oversized_file_is_stored(settings) -> None:
    settings.INTERNAL_WEB_MAX_MANIFEST_BYTES = 8
    handler = BoundedManifestUploadHandler()

    assert handler.receive_data_chunk(b"1234", 0) == b"1234"
    with pytest.raises(StopUpload):
        handler.receive_data_chunk(b"56789", 4)
