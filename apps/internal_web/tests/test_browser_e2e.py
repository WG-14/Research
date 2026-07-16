from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright

from market_research.research.hashing import content_hash_payload, sha256_prefixed
from portal.execution import ResearchJobDispatcher
from portal.models import ResearchJob
from portal.worker import run_worker_once


pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)


def test_browser_research_workflow_from_login_to_verified_download(
    noop_research_fixture,
    runner_user,
    live_server,
    tmp_path: Path,
) -> None:
    """Exercise the ordinary Windows-browser workflow against real Django pages.

    Research execution uses the same direct worker dispatcher as adapter code;
    no HTTP view invokes the engine and no CLI subprocess is involved.
    """

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
        expect(page.get_by_role("heading", name=f"{runner_user.username}님, 무엇을 확인할까요?")).to_be_visible()

        page.get_by_role("link", name="새 연구 시작").click()
        expect(page.get_by_role("heading", name="연구 정의 파일을 선택하세요")).to_be_visible()
        page.locator("input[type='file']").set_input_files(manifest_path)
        page.locator("input[name='display_name']").fill("브라우저 E2E 검증")
        page.get_by_role("button", name="파일 점검").click()
        expect(page.get_by_role("heading", name="브라우저 E2E 검증")).to_be_visible()
        expect(page.get_by_text("원본 해시 고정")).to_be_visible()

        page.get_by_role("button", name="사전 점검 시작").click()
        expect(page.get_by_text("대기 중", exact=True).first).to_be_visible()
        with ThreadPoolExecutor(max_workers=1) as executor:
            preflight = executor.submit(
                run_worker_once,
                ResearchJobDispatcher(),
                worker_id="browser-e2e",
            ).result()
        assert preflight is not None
        assert preflight.capability_id == ResearchJob.Capability.PREFLIGHT
        assert preflight.status == ResearchJob.Status.SUCCEEDED

        page.reload()
        expect(page.get_by_role("heading", name="사전 점검이 완료되었습니다")).to_be_visible()
        expect(page.get_by_text("해시 검증").locator("xpath=following-sibling::strong")).to_have_text(
            "확인됨"
        )
        page.get_by_role("button", name="검증 실행").click()
        expect(page.get_by_text("대기 중", exact=True).first).to_be_visible()

        with ThreadPoolExecutor(max_workers=1) as executor:
            validation = executor.submit(
                run_worker_once,
                ResearchJobDispatcher(),
                worker_id="browser-e2e",
            ).result()
        assert validation is not None
        assert validation.capability_id == ResearchJob.Capability.VALIDATE
        assert validation.status == ResearchJob.Status.SUCCEEDED, validation.error_code

        page.reload()
        expect(page.get_by_role("heading", name="연구 검증이 완료되었습니다")).to_be_visible()
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
