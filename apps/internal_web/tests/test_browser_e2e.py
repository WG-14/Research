from __future__ import annotations

import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from django.db import close_old_connections, connections
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright
from psycopg.conninfo import make_conninfo

from market_research.research.hashing import content_hash_payload, sha256_prefixed
from portal.execution import ResearchJobDispatcher
from portal.models import ResearchJob
from research_operations.admission import ExperimentAdmissionStore
from research_operations.migrate import apply_migrations
from research_operations.outbox import OutboxStore
from research_operations.research_job_worker import (
    ResearchJobWorker,
    ResearchJobWorkerSettings,
)


pytestmark = [
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
    pytest.mark.skipif(
        connections["default"].vendor != "postgresql",
        reason="browser E2E requires the live PostgreSQL test profile",
    ),
]


def _operations_dsn() -> str:
    database = connections["default"].settings_dict
    values = {
        "dbname": str(database["NAME"]),
        "user": str(database.get("USER") or ""),
        "password": str(database.get("PASSWORD") or ""),
        "host": str(database.get("HOST") or ""),
        "port": str(database.get("PORT") or ""),
    }
    return make_conninfo(**{key: value for key, value in values.items() if value})


@pytest.fixture
def operations_worker_dsn(live_server) -> Iterator[str]:
    """Install then remove the non-Django schema inside this test database."""

    del live_server
    database_name = str(connections["default"].settings_dict["NAME"])
    if not database_name.startswith("test_"):
        raise RuntimeError("browser_e2e_refuses_non_test_database")
    dsn = _operations_dsn()
    apply_migrations(dsn)
    yield dsn
    close_old_connections()
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS research_ops CASCADE")


def _run_worker_in_fresh_connection(
    dispatcher: ResearchJobDispatcher,
    dsn: str,
) -> ResearchJob | None:
    close_old_connections()
    try:
        job_id = (
            ResearchJob.objects.filter(status=ResearchJob.Status.QUEUED)
            .order_by("created_at")
            .values_list("pk", flat=True)
            .first()
        )
        if job_id is None:
            return None
        worker = ResearchJobWorker(
            admissions=ExperimentAdmissionStore(dsn),
            settings=ResearchJobWorkerSettings(
                worker_id="research-job:browser-e2e",
                poll_interval=0.05,
                admission_lease_seconds=30,
            ),
            dispatcher=dispatcher,
            heartbeat_store=OutboxStore(dsn),
        )
        assert worker.run_one()
        return ResearchJob.objects.get(pk=job_id)
    finally:
        connections.close_all()


def test_browser_research_workflow_from_login_to_verified_download(
    noop_research_fixture,
    runner_user,
    live_server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operations_worker_dsn: str,
) -> None:
    """Exercise the ordinary Windows-browser workflow against real Django pages.

    Research execution traverses the PostgreSQL Operations admission, lease,
    fencing and result-receipt worker path. No HTTP view invokes the engine and
    no CLI subprocess is involved.
    """

    monkeypatch.setenv("RESEARCH_OPS_GIT_SHA", "1" * 40)
    monkeypatch.setenv("RESEARCH_OPS_RELEASE_ID", "browser-e2e")
    monkeypatch.setenv("RESEARCH_OPS_BUILD_DIGEST", "sha256:" + "2" * 64)
    monkeypatch.setenv("RESEARCH_OPS_RELEASE_BUNDLE_DIGEST", "sha256:" + "3" * 64)
    monkeypatch.delenv("RESEARCH_RUNTIME_PROFILE", raising=False)
    _paths, manifest_path = noop_research_fixture
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:
            message = str(exc)
            if "shared libraries" in message or "Executable doesn't exist" in message:
                if os.getenv("INTERNAL_WEB_REQUIRE_BROWSER_E2E") == "1":
                    raise
                pytest.skip(
                    "Playwright Chromium prerequisites are unavailable; run "
                    "`python -m playwright install --with-deps chromium`."
                )
            raise
        page = browser.new_page(viewport={"width": 1440, "height": 960})

        page.goto(f"{live_server.url}/")
        expect(page.get_by_role("heading", name="사내 계정으로 로그인")).to_be_visible()
        page.locator("input[name='username']").fill(runner_user.username)
        page.locator("input[name='password']").fill("test-password")
        page.get_by_role("button", name="로그인").click()
        expect(
            page.get_by_role(
                "heading", name=f"{runner_user.username}님, 무엇을 확인할까요?"
            )
        ).to_be_visible()

        page.get_by_role("link", name="새 연구 시작").click()
        expect(
            page.get_by_role("heading", name="연구 정의 파일을 선택하세요")
        ).to_be_visible()
        page.locator("input[type='file']").set_input_files(manifest_path)
        page.locator("input[name='display_name']").fill("브라우저 E2E 검증")
        page.get_by_role("button", name="파일 점검").click()
        expect(page.get_by_role("heading", name="브라우저 E2E 검증")).to_be_visible()
        expect(page.get_by_text("원본 해시 고정")).to_be_visible()

        page.get_by_role("button", name="사전 점검 시작").click()
        expect(page.get_by_text("대기 중", exact=True).first).to_be_visible()
        with ThreadPoolExecutor(max_workers=1) as executor:
            preflight = executor.submit(
                _run_worker_in_fresh_connection,
                ResearchJobDispatcher(),
                operations_worker_dsn,
            ).result()
        assert preflight is not None
        assert preflight.capability_id == ResearchJob.Capability.PREFLIGHT
        assert preflight.status == ResearchJob.Status.SUCCEEDED

        page.reload()
        expect(
            page.get_by_role("heading", name="사전 점검이 완료되었습니다")
        ).to_be_visible()
        expect(
            page.get_by_text("해시 검증").locator("xpath=following-sibling::strong")
        ).to_have_text("확인됨")
        page.get_by_role("button", name="검증 실행").click()
        expect(page.get_by_text("대기 중", exact=True).first).to_be_visible()

        with ThreadPoolExecutor(max_workers=1) as executor:
            validation = executor.submit(
                _run_worker_in_fresh_connection,
                ResearchJobDispatcher(),
                operations_worker_dsn,
            ).result()
        assert validation is not None
        assert validation.capability_id == ResearchJob.Capability.VALIDATE
        assert validation.status == ResearchJob.Status.SUCCEEDED, validation.error_code

        page.reload()
        expect(
            page.get_by_role("heading", name="연구 검증이 완료되었습니다")
        ).to_be_visible()
        page.get_by_text("고급 근거 보기 · 실행 ID와 hash").click()
        expect(page.get_by_text(validation.result_hash, exact=True)).to_be_visible()
        with page.expect_download() as download_info:
            page.get_by_role("link", name="검증 가능한 안전 사본").click()
        download_path = tmp_path / "browser-result.json"
        download_info.value.save_as(download_path)
        downloaded = json.loads(download_path.read_text(encoding="utf-8"))
        assert downloaded["source_result_hash"] == validation.result_hash
        without_projection_hash = {
            key: value for key, value in downloaded.items() if key != "content_hash"
        }
        assert downloaded["content_hash"] == sha256_prefixed(
            content_hash_payload(without_projection_hash)
        )
        assert "server-managed" in json.dumps(downloaded, ensure_ascii=False)

        browser.close()
