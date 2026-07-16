"""User-safe projections of authoritative job artifacts."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath
from typing import Any

from market_research.research.hashing import content_hash_payload, sha256_prefixed

from .models import ResearchJob
from .storage import verify_result_artifact


SAFE_ERROR_MESSAGES = {
    "CANCELLED_BEFORE_START": "실행 전에 취소되었습니다.",
    "CANCELLED_BY_REQUEST": "요청에 따라 안전한 단계에서 취소되었습니다.",
    "WORKER_LEASE_EXPIRED": "작업 실행기가 중단되어 결과를 완료로 처리하지 않았습니다.",
    "MANIFEST_INVALID": "연구 정의 파일이 현재 연구 계약을 통과하지 못했습니다.",
    "MANIFEST_CONTENT_HASH_MISMATCH": "등록 후 입력 파일의 무결성이 달라졌습니다.",
    "RESEARCH_INPUT_UNAVAILABLE": "필요한 입력 데이터 또는 저장소를 사용할 수 없습니다.",
    "RESEARCH_REQUEST_INVALID": "연구 요청의 입력값을 다시 확인해 주세요.",
    "RESEARCH_VALIDATION_FAILED": "연구 검증 규칙을 완료하지 못했습니다.",
    "VALIDATION_RUN_FAILED": "검증 실행 중 안전 규칙이 작업을 중단했습니다.",
    "RESULT_CONTRACT_INVALID": "생성된 결과의 해시 또는 형식 검증에 실패했습니다.",
    "UNEXPECTED_WORKER_ERROR": "예상하지 못한 오류로 작업을 안전하게 중단했습니다.",
    "APPLICATION_PERMISSION_DENIED": "요청 당시 권한 기록이 현재 기능 계약을 충족하지 못했습니다.",
}


def safe_error_message(job: ResearchJob) -> str:
    return SAFE_ERROR_MESSAGES.get(
        job.error_code,
        "작업을 완료하지 못했습니다. 오류 코드와 문의 ID를 관리자에게 전달해 주세요.",
    )


def load_safe_result(job: ResearchJob) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a compact template summary and a path-redacted download payload."""

    if job.status != ResearchJob.Status.SUCCEEDED or not job.result_ref:
        return {}, {}
    payload = verify_result_artifact(job.result_ref, expected_hash=job.result_hash)
    safe_payload = redact_server_topology(payload)
    if payload.get("report_kind") == "internal_web_preflight":
        readiness = payload.get("readiness") or {}
        workload = payload.get("workload") or {}
        readiness_report = readiness.get("report") or {}
        estimate = workload.get("estimate") or {}
        passed = payload.get("status") == "PASS"
        summary = {
            "title": "사전 점검이 완료되었습니다",
            "summary": (
                "데이터 준비 상태와 예상 작업량을 확인했습니다."
                if passed
                else "준비 상태의 보완 항목을 확인한 뒤 다시 점검해 주세요."
            ),
            "final_status": "실행 가능" if passed else "보완 필요",
            "selection": f"작업 단위 {estimate.get('work_unit_count', '—')}개",
            "warning_count": len(readiness_report.get("next_actions") or ()),
            "integrity_status": "확인됨",
            "manifest_hash": payload.get("manifest_hash"),
            "dataset_hash": "사전 점검 후 실행 시 확정",
            "execution_contract_hash": readiness_report.get(
                "execution_capability_contract_hash"
            ),
            "next_action": "validation" if passed else None,
        }
        return summary, safe_payload

    warnings = payload.get("warning_codes") or payload.get("warnings") or ()
    summary = {
        "title": "연구 검증이 완료되었습니다",
        "summary": "연구 엔진이 생성한 최종 상태와 재현성 근거입니다.",
        "final_status": payload.get("end_to_end_validation_result")
        or payload.get("status")
        or "완료",
        "selection": payload.get("selected_candidate_id") or "선택 없음",
        "warning_count": len(warnings) if isinstance(warnings, (list, tuple)) else 0,
        "integrity_status": "확인됨",
        "manifest_hash": payload.get("manifest_hash"),
        "dataset_hash": payload.get("dataset_hash")
        or payload.get("dataset_artifact_manifest_hash")
        or _nested(payload, "dataset_evidence", "content_hash"),
        "execution_contract_hash": payload.get("execution_contract_hash")
        or payload.get("compiled_contract_hash")
        or _nested(payload, "execution_evidence", "content_hash"),
        "next_action": "review"
        if payload.get("end_to_end_validation_result") == "PASS"
        else None,
    }
    return summary, safe_payload


def build_safe_download_payload(
    job: ResearchJob,
    safe_result: dict[str, Any],
) -> dict[str, Any]:
    """Bind a redacted projection to its verified source with its own hash."""

    projection = dict(safe_result)
    source_embedded_hash = projection.pop("content_hash", None)
    if source_embedded_hash != job.result_hash:
        raise ValueError("safe_download_source_hash_mismatch")
    document: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "internal_web_redacted_result_projection",
        "source_job_id": str(job.pk),
        "source_capability_id": job.capability_id,
        "source_result_hash": job.result_hash,
        "result": projection,
    }
    document["content_hash"] = sha256_prefixed(content_hash_payload(document))
    return document


def redact_server_topology(value: Any, *, key: str = "") -> Any:
    lowered = key.lower()
    if lowered.endswith(("path", "uri")) or lowered in {"db_path", "manifest_path"}:
        return "<server-managed>"
    if isinstance(value, dict):
        return {
            str(item_key): redact_server_topology(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_server_topology(item) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if (
            Path(stripped).is_absolute()
            or PureWindowsPath(stripped).is_absolute()
            or stripped.lower().startswith(("file:", "sqlite:", "duckdb:"))
        ):
            return "<server-managed>"
        return value
    return value


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    value: Any = payload
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value
