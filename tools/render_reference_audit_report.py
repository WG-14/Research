#!/usr/bin/env python3
"""Render honest human and machine reports for the canonical A--J audit.

Every execution claim in these reports is derived from the current,
hash-validated execution receipt.  Historical prose and retained paths are not
promoted into current PASS evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    from tools.reference_audit import (
        DEFAULT_MATRIX,
        DOMAIN_POINTS,
        AuditEvaluation,
        evaluate_matrix,
        load_matrix,
    )
except ModuleNotFoundError:  # direct ``python tools/...`` execution
    from reference_audit import (  # type: ignore[import-not-found,no-redef]
        DEFAULT_MATRIX,
        DOMAIN_POINTS,
        AuditEvaluation,
        evaluate_matrix,
        load_matrix,
    )


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = PROJECT_ROOT / "docs/investment-research-platform-audit-report.md"
RESULT_PATH = PROJECT_ROOT / "docs/investment-research-platform-audit-result.json"

_DOMAIN_NAMES = {
    "A": ("scope_boundary", "연구 범위와 경계"),
    "B": ("data", "데이터 정확성·시점성·품질"),
    "C": ("reproducibility", "재현성과 버전 관리"),
    "D": ("research_lifecycle", "가설·연구 생애주기·실험 설계"),
    "E": ("backtesting_simulation", "백테스트·체결·비용 시뮬레이션"),
    "F": ("validation", "통계·강건성·현실성 검증"),
    "G": ("review_governance", "독립 검증·리뷰·거버넌스"),
    "H": ("artifacts_knowledge", "산출물·계보·지식 관리"),
    "I": ("security_observability", "보안·권한·감사·관측성"),
    "J": ("architecture_usability", "아키텍처·사용성·협업·확장성"),
}
_IMPORTANCE_NAMES = {"C": "CRITICAL", "M": "MAJOR", "S": "SUPPORTING"}
_FINAL_QUESTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("사용 데이터의 당시 이용 가능 시점을 확인할 수 있는가?", ("B-03", "B-04", "B-14")),
    ("상장폐지와 데이터 수정 이력이 포함되는가?", ("B-05", "B-06", "E-05")),
    ("특정 결과에서 원천 데이터와 코드까지 역추적할 수 있는가?", ("B-14", "H-11")),
    ("다른 연구자가 수동 설명 없이 결과를 재현할 수 있는가?", ("C-06", "G-02")),
    ("주요 파라미터와 실패 실험이 보존되는가?", ("C-11", "C-12", "H-15")),
    ("탐색 데이터와 최종 검증 데이터가 분리되는가?", ("D-08", "D-09", "F-03")),
    ("미래정보와 생존편향을 자동 검사하는가?", ("B-06", "B-07", "E-02")),
    ("비용·시장충격·유동성·용량을 오프라인 평가하는가?", ("E-09", "E-16", "E-17")),
    ("기간·시장·종목 성과 집중을 탐지하는가?", ("F-08", "F-09", "F-13", "F-14")),
    ("통계적 유의성과 경제적 의미를 구분하는가?", ("F-17", "F-18")),
    ("별개의 검증자가 연구를 재현할 수 있는가?", ("G-01", "G-02", "G-03", "G-04")),
    ("제한사항과 실패 조건이 공식 산출물에 포함되는가?", ("H-08", "H-09")),
    ("기각된 연구를 검색하고 재사용할 수 있는가?", ("G-10", "H-15")),
    ("공식 연구 릴리스가 불변 버전으로 보존되는가?", ("C-17", "C-18", "H-10")),
    ("주문·포지션·실시간 리스크·운영 기능이 분리되는가?", ("A-02", "A-03", "A-04")),
)
_LIFECYCLE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("연구 생성", ("D-01",)),
    ("가설 등록", ("D-03",)),
    ("사전등록", ("D-06", "D-07")),
    ("데이터 선택", ("D-10", "B-12")),
    ("데이터 스냅샷", ("B-13",)),
    ("실험 실행", ("C-09", "C-10")),
    ("백테스트", ("E-01", "E-24")),
    ("검증", ("F-03", "F-04")),
    ("리뷰", ("G-05", "G-06")),
    ("독립 재현", ("G-02", "G-03", "G-04")),
    ("릴리스", ("C-17", "H-01")),
    ("검색·재사용", ("H-13", "H-15")),
)
_ANTI_PATTERNS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "AP-01 노트북 공동 저장소",
        ("C-07", "C-08"),
        "공식 결과가 수동 상태에 의존할 위험",
    ),
    ("AP-02 백테스트 성과 순위표", ("F-02", "F-07"), "선택 편향과 과적합 위험"),
    (
        "AP-03 변경 가능한 공용 데이터",
        ("B-01", "B-12"),
        "입력 변경으로 결과가 달라질 위험",
    ),
    ("AP-04 수동 데이터 수정", ("B-14", "B-16"), "계보가 끊기고 수정이 은폐될 위험"),
    ("AP-05 성공 연구만 보존", ("C-11", "G-10", "H-15"), "부정 결과와 시행 횟수 누락"),
    ("AP-06 검증 데이터 반복 사용", ("D-09", "F-03"), "홀드아웃 오염 위험"),
    ("AP-07 신호와 체결의 혼합", ("E-01", "E-07"), "연구 의미와 실행 가정 혼동"),
    ("AP-08 연구와 실거래 결합", ("A-02", "A-03", "A-04"), "연구 경계 위반"),
    ("AP-09 문서만 갖춘 가짜 완성도", ("C-06", "C-16"), "실행 증거 없는 과대 판정"),
    ("AP-10 외부 도구 이름만 나열", ("J-08", "J-09"), "실제 통합 계약 부재"),
)

# These are retained audit observations, including failures that were later
# fixed.  They are deliberately not promoted into the current execution
# receipt: only the receipt runner's exact clean target set can establish E4.
_VALIDATION_INCIDENTS: tuple[dict[str, object], ...] = (
    {
        "id": "VI-01",
        "phase": "baseline focused validation",
        "command": "initial 89-test critical selector with inherited Windows TEMP/TMP",
        "observed": "FileNotFoundError before collection; collected=0",
        "cause": "pytest capture used a Windows TEMP path whose directory disappeared",
        "resolution": "created a task-specific directory under /tmp and set TMPDIR, TMP, and TEMP explicitly",
        "final_result": "89 passed",
        "status": "RESOLVED",
    },
    {
        "id": "VI-02",
        "phase": "principal-bound independent verification",
        "command": "TMPDIR=/tmp/pytest-of-vorac .venv/bin/pytest -q tests/test_principal_assertion.py tests/test_independent_verification.py tests/test_research_governance.py tests/test_study_lifecycle.py",
        "observed": "53 passed, 1 failed: concurrent approvers produced conflicting assertion nonce/time bytes",
        "cause": "create-or-verify raced on semantically equal verification results with nondeterministic assertion issuance fields",
        "resolution": "made the test-only publication factory converge on the canonical result without weakening verified principal identity; nonce replay concurrency and secret-mode checks remained enabled",
        "final_result": "56 passed in 9.64s",
        "status": "RESOLVED",
    },
    {
        "id": "VI-03",
        "phase": "tool invocation",
        "command": "python - <<'PY' ...",
        "observed": "/bin/bash: python: command not found (exit 127)",
        "cause": "the workspace exposes its interpreter through the locked virtual environment, not an unqualified python executable",
        "resolution": "used .venv/bin/python (and the receipt runner records sys.executable)",
        "final_result": "corrected command succeeded",
        "status": "RESOLVED",
    },
    {
        "id": "VI-04",
        "phase": "architecture boundary regression",
        "command": "DJANGO_SETTINGS_MODULE=market_research_web.settings_test TMPDIR=/tmp/pytest-of-vorac .venv/bin/pytest -q tests/test_internal_web_architecture_contract.py tests/test_repository_research_only_boundary.py tests/test_research_cli_boundary.py apps/internal_web/tests/test_configuration_audit.py apps/internal_web/tests/test_authentication_audit_admin_boundary.py",
        "observed": "29 passed, 1 failed: ResearchProject GUI adapter was missing",
        "cause": "the new Core ResearchProject aggregate had no authenticated internal-web projection",
        "resolution": "added project adapter routes, views, templates, distinct create/manage permissions, capability checks, and workflow tests",
        "final_result": "47 boundary tests passed in 8.86s; project workflow 3 passed in 1.11s",
        "status": "RESOLVED",
    },
    {
        "id": "VI-05",
        "phase": "database immutability validation",
        "command": "pytest -q apps/internal_web/tests/test_audit_outbox.py apps/internal_web/tests/test_database_immutability_static.py",
        "observed": "17 passed, 1 skipped when the PostgreSQL-only raw SQL check shared the local target file",
        "cause": "a conditional external-database test contaminated the clean local E4 receipt inventory",
        "resolution": "split PostgreSQL integration into test_database_immutability_postgresql.py; the local receipt retains only skip-free audit and static trigger contracts",
        "final_result": "live trigger execution remains UNVERIFIED_EXTERNAL",
        "status": "LOCAL_RESOLVED_EXTERNAL_UNVERIFIED",
    },
    {
        "id": "VI-06",
        "phase": "native operations focused validation",
        "command": "focused three-test Core/native selector with inherited Windows TEMP/TMP",
        "observed": "3 failed from unavailable Windows temporary storage",
        "cause": "temporary-path configuration, not an operations contract failure",
        "resolution": "reran with a task-specific /tmp directory bound to TMPDIR, TMP, and TEMP",
        "final_result": "3 passed",
        "status": "RESOLVED",
    },
    {
        "id": "VI-07",
        "phase": "external PostgreSQL integration",
        "command": "PostgreSQL integration selectors for audit immutability and durable alert delivery",
        "observed": "server accepted connections, but no authorized test role/DSN or sudo credential was available",
        "cause": "external infrastructure authority was intentionally not assumed",
        "resolution": "kept conditional integration files outside the local E4 receipt and documented the exact external prerequisite",
        "final_result": "not executed",
        "status": "UNVERIFIED_EXTERNAL",
    },
    {
        "id": "VI-08",
        "phase": "adapter CLI deterministic focused validation",
        "command": "adapter CLI scoped selector (first run omitted PYTHONHASHSEED)",
        "observed": "10 passed, 5 failed because the deterministic environment contract failed closed",
        "cause": "PYTHONHASHSEED=0 was omitted from the first scoped command",
        "resolution": "set PYTHONHASHSEED=0 and reran only the five selectors reported as failures",
        "final_result": "5 passed in 196.61s",
        "status": "RESOLVED",
    },
    {
        "id": "VI-09",
        "phase": "emergency alert and migration focused validation",
        "command": "focused emergency-alert, native-deployment, and migration selector",
        "observed": "51 passed, 2 failed: expected condition order lagged database priority and operations migration inventory omitted 0011/0012",
        "cause": "test expectations were stale after fail-closed database priority and new migrations were added",
        "resolution": "updated the condition ordering and migration inventory, then reran only the two reported selectors",
        "final_result": "2 passed",
        "status": "RESOLVED",
    },
    {
        "id": "VI-10",
        "phase": "canonical audit output regeneration",
        "command": "pytest -q tests/test_reference_audit.py -k 'not canonical_human_and_machine_reports_are_current_and_honest'",
        "observed": "29 passed, 3 failed because checked-in matrix hashes and assessment surface were stale during parallel source edits",
        "cause": "generated audit outputs intentionally precede the final stable source surface and cannot truthfully be refreshed mid-edit",
        "resolution": "deferred matrix/result/report regeneration until source stabilization, regenerated the canonical outputs, and ran the complete reference-audit test file",
        "final_result": "36 passed in 10.28s after canonical regeneration",
        "status": "RESOLVED",
    },
    {
        "id": "VI-11",
        "phase": "adversarial receipt trust review",
        "command": "synthetically construct and rehash a claimed clean receipt, then call validate_receipt",
        "observed": "the unkeyed envelope can prove byte consistency but cannot authenticate test execution or the executor",
        "cause": "a repository-owned SHA-256 content hash is not a signature or independent CI attestation",
        "resolution": "renamed clean status to VALID_LOCAL_SELF_ATTESTED, exposed clean_local_run/trusted/trust_level and execution_authenticity_unverified, and reserved M5/independent claims for external evidence",
        "final_result": "synthetic receipts remain explicitly untrusted and cannot be described as authenticated execution",
        "status": "RESOLVED_WITH_TRUST_LIMIT",
    },
    {
        "id": "VI-12",
        "phase": "concurrent alert topology and native identity edit",
        "command": "pytest -q services/research_operations/tests/test_native_deployment.py services/research_operations/tests/test_service_alert_worker_unit.py",
        "observed": "46 passed, 3 failed: the alert unit dependency expectation was stale and two database-emergency tests let the unavailable-store error escape",
        "cause": "focused validation intersected an in-progress change that removed the alert worker's migration dependency and reordered the database-independent emergency path",
        "resolution": "preserved the failure, completed the alert dependency and database-independent emergency-fallback fixes, and reran the focused alert/native set",
        "final_result": "114 passed in the subsequent focused alert/native set; the exact three previously failing selectors then passed in 0.79s",
        "status": "RESOLVED",
    },
    {
        "id": "VI-13",
        "phase": "focused alert/native validation capture",
        "command": "focused alert/native pytest selector with a dedicated pytest temporary root",
        "observed": "the first invocation collected 0 tests and exited 1 because the dedicated pytest temporary root disappeared during output capture",
        "cause": "ephemeral temporary-directory loss during command capture, not a product assertion failure",
        "resolution": "created a fresh task-specific directory with mktemp, rebound TMPDIR, TMP, and TEMP, and immediately reran the same focused selector",
        "final_result": "114 passed on the immediate rerun",
        "status": "RESOLVED",
    },
    {
        "id": "VI-14",
        "phase": "native preflight strict static analysis",
        "command": "strict mypy and Ruff checks for services/research_operations/deploy/native/bin/preflight.py and related native helper sources",
        "observed": "strict mypy initially reported one error in _validate_runtime_files because qualification.get('roots') retained object/non-iterable type",
        "cause": "the parsed runtime qualification payload lacked explicit type narrowing before roots iteration",
        "resolution": "added fail-closed isinstance(roots, list) validation before iteration",
        "final_result": "strict mypy passed first for the preflight source and later for four native helper source files; Ruff passed",
        "status": "RESOLVED",
    },
    {
        "id": "VI-15",
        "phase": "native backup ACL static and focused validation",
        "command": "strict mypy for native backup helpers, followed by focused backup and storage-evidence pytest selectors",
        "observed": "the first strict mypy invocation lacked the native bin helper import path and reported a missing CompletedProcess generic type argument",
        "cause": "the static-analysis environment did not expose the native helper module path and one subprocess result annotation was incomplete",
        "resolution": "added the CompletedProcess type argument and reran strict mypy with the native bin directory on MYPYPATH",
        "final_result": "strict mypy passed; focused backup/storage evidence selectors passed in sets of 30 and 3",
        "status": "RESOLVED",
    },
    {
        "id": "VI-16",
        "phase": "shared JSONL and directory permission focused validation",
        "command": "focused 32-test selector for shared JSONL durability and directory permission contracts",
        "observed": "31 passed, 1 failed: test_append_jsonl_fsyncs_record_and_new_directory_entry expected one fsync while the new-file path performed two",
        "cause": "the test expectation lagged the intentional durability contract that fsyncs payload data and chmod metadata separately",
        "resolution": "updated the test expectation to require both fsync operations and reran the exact reported selector plus the related storage and project set",
        "final_result": "exact selector passed in 0.09s; storage_io and research_project set passed 32 tests in 1.73s; Ruff and strict mypy for two source files passed",
        "status": "RESOLVED",
    },
    {
        "id": "VI-17",
        "phase": "native backup and alert focused/static validation",
        "command": "focused native/backup/alert pytest and Ruff checks, followed by a chained strict mypy and systemd verification command",
        "observed": "125 focused tests passed in 2.97s and Ruff passed, then strict mypy reported three errors: one unresolved backup_evidence helper import and two incompatible preflight loop-variable assignments; the chained systemd verification did not run",
        "cause": "the strict-mypy environment omitted the native bin directory from MYPYPATH, while preflight also reused one loop variable across str and Optional[str] value types",
        "resolution": "split the preflight loop variable names by type and reran strict mypy with the native bin directory on MYPYPATH, then ran Ruff and systemd verification separately",
        "final_result": "strict mypy passed for 24 source files; Ruff passed; systemd-analyze verify passed all native units, timers, and target with exit 0 and no output",
        "status": "RESOLVED",
    },
    {
        "id": "VI-18",
        "phase": "storage lock and path focused validation environment",
        "command": "focused storage/lock/path pytest selector with DJANGO_SETTINGS_MODULE=market_research_web.test_settings, followed by chained static checks",
        "observed": "pytest initialization raised ImportError before collection because the configured module name did not exist; 0 tests ran and the chained static checks did not run",
        "cause": "the validation command used market_research_web.test_settings instead of the repository's market_research_web.settings_test module",
        "resolution": "corrected DJANGO_SETTINGS_MODULE and reran the focused storage/project/source-archive/web-storage selector and static checks",
        "final_result": "52 focused tests passed in 7.30s; Ruff passed; strict mypy passed for 13 source files",
        "status": "RESOLVED",
    },
    {
        "id": "VI-19",
        "phase": "path lock and source-archive focused validation",
        "command": "focused 44-test path/lock/source selector",
        "observed": "42 passed, 2 failed: symlink-escape negative tests expected outside_configured_root but received the earlier and more specific symlink_component_forbidden code",
        "cause": "negative-test expectations lagged the new fail-closed path guard that rejects a symlink component before evaluating the resolved-root boundary",
        "resolution": "updated both negative tests to require symlink_component_forbidden and reran the exact two reported selectors, followed by the focused set and static checks",
        "final_result": "exact two selectors passed in 0.50s; path/lock/source set passed 44 tests in 8.91s; Ruff and strict mypy for three source files passed",
        "status": "RESOLVED",
    },
    {
        "id": "VI-20",
        "phase": "retention and off-site protocol test authoring",
        "command": "new focused retention/off-site protocol selector",
        "observed": "the initial two-selector run had 1 passed and 1 failed because a nested fixture string encoded a newline incorrectly",
        "cause": "test-fixture string escaping error, not a retention or backup protocol implementation failure",
        "resolution": "corrected the nested newline escape and reran the exact reported selector",
        "final_result": "exact selector passed; commit/resume protocol set passed 3 tests in 1.03s; broader retention, signed-receipt staging, ordering, systemd/shell, and operations-surface set passed 6 tests in 2.82s",
        "status": "RESOLVED",
    },
    {
        "id": "VI-21",
        "phase": "retention and off-site protocol ordering test authoring",
        "command": "new static ordering selector for deferred staging, receipt verification, and atomic finalization/resume",
        "observed": "one integrated selector failed because the assertion selected the first textual occurrence from the resume branch instead of the intended finalization occurrence",
        "cause": "the static test used first-occurrence lookup for a repeated protocol marker",
        "resolution": "changed the assertion to use the final occurrence with rindex and reran the exact reported selector",
        "final_result": "exact selector passed; related focused sets passed 3 tests in 1.03s and 6 tests in 2.82s; Ruff, strict mypy for two executables, and sh -n for two scripts passed",
        "status": "RESOLVED",
    },
    {
        "id": "VI-22",
        "phase": "Nginx proxy identity focused validation capture",
        "command": "dedicated Nginx/proxy focused pytest selector and native configuration/static checks",
        "observed": "the first focused pytest rerun encountered one environment error when a pytest capture temporary file disappeared",
        "cause": "ephemeral pytest capture storage loss, not a proxy identity or Nginx assertion failure",
        "resolution": "disabled pytest capture and reran the same related focused selector, then completed the configuration and static checks",
        "final_result": "6 related tests passed; Ruff lint/format, strict mypy, default-Nginx-plus-drop-in systemd-analyze verify, host-substituted nginx -t, and scoped diff-check passed; systemd-sysusers dry-run passed within the tests",
        "status": "RESOLVED",
    },
    {
        "id": "VI-23",
        "phase": "broad focused validation",
        "command": "broad focused pytest selector with PYTHONHASHSEED=0 but without the complete deterministic numerical thread environment",
        "observed": "419 passed, 7 failed in 730.08s: six strict-reproduction checks rejected missing OMP, OPENBLAS, MKL, NUMEXPR, BLIS, and VECLIB thread limits; one E2E date-boundary fixture generated frozen_at on 2026-08-01 after a fixed start_at at 2026-08-01T00:00:00Z",
        "cause": "the validation command omitted six required numerical-library thread variables, and one test fixture coupled a generated timestamp to a fixed same-day boundary",
        "resolution": "reran the exact six reproduction selectors with the full deterministic environment and limited the E2E change to making the brittle fixture internally ordered before rerunning its exact selector on a frozen source tree",
        "final_result": "the exact six strict-reproduction selectors passed in 296.69s; the corrected date-boundary E2E selector passed in 540.07s",
        "status": "RESOLVED",
    },
    {
        "id": "VI-24",
        "phase": "date-boundary reproduction E2E source stability",
        "command": "exact patched date-boundary E2E selector with a fresh temporary directory, complete deterministic environment, and pytest capture disabled",
        "observed": "the first exact rerun reached reproduction after 532.15s and failed closed with DRIFT because Ruff concurrently formatted 30 shared-worktree files, changing bound source, archive, and dependency hashes",
        "cause": "shared-worktree source mutation during the long-running reproduction, not a product or fixture defect",
        "resolution": "froze all source edits and reran the exact selector from a fresh temporary directory under the complete deterministic environment",
        "final_result": "1 passed in 540.07s (0:09:00)",
        "status": "RESOLVED",
    },
    {
        "id": "VI-25",
        "phase": "local self-attested receipt execution",
        "command": ".venv/bin/python tools/reference_audit_receipt.py",
        "observed": "the receipt runner exited 1 before test collection because Path(sys.executable).resolve() dereferenced the virtualenv launcher to /usr/bin/python3.12, whose environment had no pytest module",
        "cause": "canonicalizing the interpreter with filesystem symlink resolution discarded the active virtualenv launcher semantics",
        "resolution": "changed interpreter canonicalization to an absolute path that preserves the virtualenv symlink and added a synthetic launcher-symlink regression test",
        "final_result": "the launcher regression test passed in 0.59s and the corrected child interpreter executed the subsequent 852-test receipt session; separate failures are recorded as VI-26 and VI-27",
        "status": "RESOLVED",
    },
    {
        "id": "VI-26",
        "phase": "receipt parallel walk-forward integration",
        "command": "receipt-owned exact audit pytest run across 84 evidence files",
        "observed": "850 passed, 2 failed in 1594.50s; test_parallel_frozen_walk_forward_without_db failed when multiprocessing forkserver could not bind an AF_UNIX socket because the nested receipt temporary path exceeded the platform limit",
        "cause": "the receipt runner nested a long descriptive temporary directory beneath caller TMPDIR and then exposed that path to multiprocessing",
        "resolution": "made the receipt runner own a short, securely created mra-* POSIX temporary root and added a regression test that rejects dependence on a long caller TMPDIR, then reran the exact parallel selector",
        "final_result": "the two exact failing selectors plus the short-temp regression passed as a three-test set in 43.64s",
        "status": "RESOLVED",
    },
    {
        "id": "VI-27",
        "phase": "web-to-Core architecture allowlist",
        "command": "receipt-owned exact audit pytest run across 84 evidence files",
        "observed": "850 passed, 2 failed in 1594.50s; test_web_uses_public_core_application_or_composition_contracts found portal/project_views.py importing market_research.research.research_project directly",
        "cause": "the new project UI consumed project authority types from an internal research module instead of the published application package boundary",
        "resolution": "published the required project adapter contract names through market_research.application and changed the web adapter to import only that allowed boundary, then reran the exact architecture selector",
        "final_result": "the two exact failing selectors plus the short-temp regression passed as a three-test set in 43.64s",
        "status": "RESOLVED",
    },
    {
        "id": "VI-28",
        "phase": "single full monorepo pytest environment isolation",
        "command": "one deterministic pytest invocation over tests, apps/internal_web/tests, and services/research_operations/tests",
        "observed": "2085 passed, 40 skipped, 4 failed in 2253.81s; test_manifest_upload_reuses_same_owner_content reached the persistent user-state web_audit.jsonl and rejected its pre-existing access mode",
        "cause": "the combined validation command supplied deterministic thread and temporary-directory variables but omitted repository-external RESEARCH_DATA_ROOT, RESEARCH_ARTIFACT_ROOT, RESEARCH_REPORT_ROOT, and RESEARCH_CACHE_ROOT isolation",
        "resolution": "preserved the full-suite failure and limited the rerun to the exact selector under fresh repository-external research roots",
        "final_result": "the four exact full-suite failure selectors passed together in 3.79s",
        "status": "RESOLVED",
    },
    {
        "id": "VI-29",
        "phase": "single full monorepo pytest legacy multi-asset audit",
        "command": "one deterministic pytest invocation over tests, apps/internal_web/tests, and services/research_operations/tests",
        "observed": "2085 passed, 40 skipped, 4 failed in 2253.81s; test_final_audit_report_is_complete_consistent_and_current found checked-in multi-asset result bytes stale against the changed source snapshot",
        "cause": "the legacy generated multi-asset result, report, and criterion-evidence files preceded the final source changes",
        "resolution": "regenerated all three files with their canonical renderer and reran only the reported currentness selector",
        "final_result": "the four exact full-suite failure selectors passed together in 3.79s",
        "status": "RESOLVED",
    },
    {
        "id": "VI-30",
        "phase": "single full monorepo pytest release migration inventory",
        "command": "one deterministic pytest invocation over tests, apps/internal_web/tests, and services/research_operations/tests",
        "observed": "2085 passed, 40 skipped, 4 failed in 2253.81s; test_release_manifest_binds_every_distribution_and_migration expected web migration 0010/count 10 while the packaged inventory correctly ended at 0012_project_permissions_rbac/count 12",
        "cause": "the release and prior-upgrade test expectations lagged web migrations 0011 and 0012",
        "resolution": "updated the expected current web migration head and count without changing release-manifest semantics and reran the exact local release selector; live prior-release PostgreSQL execution remains externally unverified",
        "final_result": "the four exact full-suite failure selectors passed together in 3.79s",
        "status": "RESOLVED",
    },
    {
        "id": "VI-31",
        "phase": "single full monorepo pytest legacy completeness evidence hashes",
        "command": "one deterministic pytest invocation over tests, apps/internal_web/tests, and services/research_operations/tests",
        "observed": "2085 passed, 40 skipped, 4 failed in 2253.81s; test_default_matrix_records_ten_honest_reassessments found stale blocker evidence hashes in the checked-in full-scope matrix",
        "cause": "the legacy completeness matrix evidence digests preceded the final source and test changes",
        "resolution": "refreshed the matrix through its canonical assessment updater and reran only the reported hash-binding selector",
        "final_result": "the four exact full-suite failure selectors passed together in 3.79s",
        "status": "RESOLVED",
    },
    {
        "id": "VI-32",
        "phase": "canonical receipt currentness preflight",
        "command": "scripts/platform verify-complete --json",
        "observed": "verification exited 1 and classified the checked-in receipt as STALE; clean_local_run=false, trust=NONE, and score_cap=75",
        "cause": "the receipt recorded the active virtualenv launcher as .venv/bin/python while the platform command reached the same environment through .venv/bin/python3 and compared the lexical aliases literally",
        "resolution": "normalized only python/python<major>/python<major.minor> launchers inside the active virtualenv to its lexical bin/python authority and isolated every platform test command in a fresh repository-external state root",
        "final_result": "focused launcher and receipt alias/foreign-interpreter contract tests passed; the final current-surface receipt is reported separately by the canonical receipt field",
        "status": "RESOLVED",
    },
    {
        "id": "VI-33",
        "phase": "focused adversarial validation environment",
        "command": "TMPDIR=/tmp TEMP=/tmp TMP=/tmp PYTHONHASHSEED=0 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 BLIS_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 DJANGO_SETTINGS_MODULE=market_research_web.settings_test uv run --frozen --no-sync --package research-operations pytest -q tests/test_research_reproduction_cli.py",
        "observed": "one reproduction selector group failed closed when PYTHONHASHSEED was omitted, and a separate capture attempt lost its inherited temporary path",
        "cause": "the ad-hoc command did not initially satisfy the repository deterministic-environment contract and inherited unstable cross-platform temporary storage",
        "resolution": "set the complete deterministic thread/hash environment, rebound TMPDIR/TMP/TEMP to /tmp, and reran only the reported selectors",
        "final_result": "the exact reproduction selectors and the surrounding focused adversarial groups passed",
        "status": "RESOLVED",
    },
    {
        "id": "VI-34",
        "phase": "corporate-action causal adversarial review",
        "command": "PYTHONHASHSEED=0 TMPDIR=/tmp pytest -q tests/test_corporate_action_dataset_materialization.py",
        "observed": "static full-split adjustment changed a January 1 BUY into HOLD before future actions were effective; later probes found future action versions and persistent halt state could alter or bypass historical admission",
        "cause": "the official loader materialized a manifest-global backward-adjusted tuple before CausalMarketView and initially scanned action history without a split-bounded causal state projection",
        "resolution": "made official strategy snapshots raw-only, selected versions at the bounded known-at authority, scrubbed whole-split evidence from causal views, and failed closed for unsupported in-period or already-active lifecycle/accounting states",
        "final_result": "focused corporate-action causal, future-correction, adjusted-scale, lifecycle-state, and raw suffix invariance tests passed; economic split/dividend/delisting ledger support remains explicitly partial",
        "status": "RESOLVED_WITH_SCOPE_LIMIT",
    },
    {
        "id": "VI-35",
        "phase": "stochastic future-suffix adversarial review",
        "command": "PYTHONHASHSEED=0 TMPDIR=/tmp pytest -q tests/test_future_suffix_invariance.py tests/test_benchmark_suite.py tests/test_validation_stress_suite_contract.py tests/test_corporate_action_dataset_materialization.py",
        "observed": "full manifest/dataset identity changed prior partial fills and returns, and hashing the whole candle stream changed earlier random-entry indices when only a future candle value changed",
        "cause": "behavioral RNG was coupled to full evidence identity and outcome-bearing suffix content instead of a causal request/domain authority",
        "resolution": "separated full reproducibility evidence hashes from request-scoped execution RNG, bounded component RNGs to their actual causal inputs, and removed future candle values from random-entry seed material while retaining the sampling-domain length",
        "final_result": "deterministic and stochastic source-hash, date-range, row/value, and corporate-correction suffix regressions passed",
        "status": "RESOLVED",
    },
    {
        "id": "VI-36",
        "phase": "validation-experiment terminal-gate adversarial review",
        "command": "PYTHONHASHSEED=0 TMPDIR=/tmp pytest -q tests/test_validation_experiment_bundle.py tests/test_application_contracts_and_capabilities.py tests/test_research_cli_boundary.py",
        "observed": "the initial lower-level bundle was unreachable from official callers, trusted internally inconsistent rehashed payloads, and allowed component evidence to be rewrapped under a different manifest/candidate",
        "cause": "terminal bindings and unkeyed hashes were checked without strict typed reconstruction or a component-native experiment scope, while the policy remained optional for legacy compatibility",
        "resolution": "wired an external absolute-path bundle through CLI/request/service, strictly reconstructed component results, bound every envelope to manifest/dataset/temporal-plan/candidate scope, and then derived a non-optional capability/policy from manifest research_classification with an explicit non-promotable legacy marker",
        "final_result": "focused official-path and adversarial rehash/rebind/field-strip/policy-downgrade tests passed; validated_candidate requires all four components and complete field-family removal now fails closed. Native calculation and source bindings remain externally prepared, unkeyed, and not replayed from observations, so the evidence remains M3",
        "status": "RESOLVED_WITH_SCOPE_LIMIT",
    },
    {
        "id": "VI-37",
        "phase": "audit-evidence honesty adversarial review",
        "command": "TMPDIR=/tmp TEMP=/tmp TMP=/tmp PYTHONHASHSEED=0 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 BLIS_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 uv run --frozen --no-sync --package market-research pytest -q tests/test_reference_audit.py::test_self_attested_receipt_never_claims_authenticated_verification tests/test_reference_audit.py::test_assessment_history_requires_sequence_and_final_binding tests/test_reference_audit.py::test_generated_result_preserves_validation_incidents_and_external_pg_gap",
        "observed": "the generator retrospectively assigned maturity to five logical phases without retained per-phase surfaces, described M3 rows as lacking a receipt even when a clean receipt existed, printed a bare VERIFIED architecture label, and rated fail-closed corporate-action admission above its economic capability",
        "cause": "reporting mechanics conflated logical work phases, test execution, local self-attestation, and complete criterion semantics",
        "resolution": "assigned M0/MISSING to unretained historical phases, made receipt language state-dependent, renamed local architecture coverage, narrowed fatal-gate wording, and conservatively reduced B-08/E-05 to M2 and C-05 to M3",
        "final_result": "the exact three structural honesty selectors passed; that phase-6 assessment recorded current-surface maturity only once and left missing historical snapshots explicit rather than reconstructed. The later phase-7 assessment retains phase 6 byte-for-byte before recording subsequent implementation evidence",
        "status": "RESOLVED_WITH_CONSERVATIVE_DOWNGRADE",
    },
    {
        "id": "VI-38",
        "phase": "release and distribution package build",
        "command": "scripts/platform build; uv build --all-packages --out-dir <repository-external-temp>",
        "observed": "the release build failed closed with release_checkout_not_clean because the audited workspace contains the preserved implementation changes",
        "cause": "the canonical release builder intentionally requires a clean checkout so a dirty source surface cannot be represented as a release artifact",
        "resolution": "preserved the workspace changes and ran the non-release package builder into a repository-external temporary directory; this validates packaging only and is not release evidence",
        "final_result": "all three distributions produced both wheel and source archives (6 artifacts total); the canonical release build remains blocked until the audited changes are intentionally committed in a clean checkout",
        "status": "LOCAL_PACKAGE_BUILD_PASS_RELEASE_BUILD_BLOCKED",
    },
    {
        "id": "VI-39",
        "phase": "single final full monorepo pytest reviewed example hash",
        "command": "TMPDIR=/tmp TEMP=/tmp TMP=/tmp PYTHONHASHSEED=0 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 BLIS_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 DJANGO_SETTINGS_MODULE=market_research_web.settings_test uv run --frozen --no-sync --package research-operations pytest -q tests apps/internal_web/tests services/research_operations/tests",
        "observed": "2147 passed, 40 skipped, 1 failed in 2157.81s; test_schema_two_example_manifest_hash_is_reviewed retained the pre-change manifest hash after the reviewed Monte Carlo seed-policy literal changed",
        "cause": "the example manifest correctly adopted the bounded closed-trade-stream seed contract, but its explicit reviewed canonical-hash assertion was not refreshed with that semantic change",
        "resolution": "updated only the reviewed example hash to the canonical hash produced by the changed schema-2 example and reran the exact reported failure selector",
        "final_result": "tests/test_research_semantics_v2_contract.py::test_schema_two_example_manifest_hash_is_reviewed passed in 1.25s; repository policy prohibits a second broad run after the single full invocation",
        "status": "RESOLVED",
    },
    {
        "id": "VI-40",
        "phase": "canonical attachment provenance reassessment",
        "command": "sha256sum <current instruction attachment> <current rubric attachment> docs/investment-research-platform-audit-instructions.md docs/investment-research-platform-audit-rubric.md; targeted Ruff and strict mypy for the canonical audit tools",
        "observed": "the matrix and three validators claimed an older aef6... rubric hash as the current titled attachment, while the actual current attachment was ce507...; the 28cd... repository rubric was also described as a normalized copy even though it is a reviewed semantic inventory derived from an older attachment variant",
        "cause": "canonical-source constants and copy-role metadata were carried forward without comparing them to the exact attachment bytes supplied for the current audit",
        "resolution": "rebound all canonical-source and receipt authorities to ce507..., retained the exact current instruction hash 26871..., and relabeled repository files as reviewed semantic copies without claiming a byte-normalization transform",
        "final_result": "the four exact byte hashes were recorded; no old aef6... authority remains in audit tools/tests; targeted Ruff and strict mypy passed, with final generated-currentness verified after source freeze",
        "status": "RESOLVED",
    },
    {
        "id": "VI-41",
        "phase": "retained audit-history static typing",
        "command": "uv run --frozen --no-sync --package market-research mypy tools/reference_audit.py tools/update_reference_audit.py tools/reference_audit_receipt.py tools/render_reference_audit_report.py",
        "observed": "the first targeted mypy run reported one optional-value access in the retained-snapshot history branch",
        "cause": "the runtime guard narrowed the retained path but did not also make the optional retained payload explicit to the type checker",
        "resolution": "added the missing retained-payload guard before reading its assessment surface and reran the same strict static check",
        "final_result": "strict mypy passed for all four canonical audit source files",
        "status": "RESOLVED",
    },
    {
        "id": "VI-42",
        "phase": "retained audit-history validator authoring",
        "command": "uv run --frozen --no-sync --package market-research ruff check tools/reference_audit.py",
        "observed": "the first lint parse reported an invalid if-expression where a newly split receipt condition omitted its boolean conjunction",
        "cause": "a local patching error while separating current-surface receipt checks from retained-snapshot receipt checks",
        "resolution": "restored the missing conjunction and reran Ruff plus strict mypy over both audit evaluator/generator sources",
        "final_result": "Ruff passed and strict mypy passed for both audit evaluator/generator files; scoped git diff-check passed",
        "status": "RESOLVED",
    },
    {
        "id": "VI-43",
        "phase": "manifest-authoritative validation focused capture",
        "command": ".venv/bin/pytest -q tests/test_validation_experiment_bundle.py -x",
        "observed": "pytest ran no tests because its inherited capture temporary file disappeared and tmpfile.truncate raised FileNotFoundError",
        "cause": "unstable inherited pytest capture storage, not a collected validation-capability assertion",
        "resolution": "disabled capture, assigned a dedicated temporary root, and reran the focused capability, gate, application-boundary, and native-experiment files",
        "final_result": "52 focused tests passed; targeted Ruff and strict mypy passed after the final compatibility correction",
        "status": "RESOLVED",
    },
    {
        "id": "VI-44",
        "phase": "validation integration shared-state isolation",
        "command": "TMPDIR=/home/vorac/work/Research/.pytest_tmp .venv/bin/pytest -q -s tests/test_validation_admission_integration.py tests/test_research_standard_authority_integration.py tests/test_frozen_dataset_multi_split_integration.py -x; followed by the exact final-holdout selector",
        "observed": "the repository-local shared temporary root first produced knowledge_registry_invalid and then process_lock_access_invalid before the intended capability assertions",
        "cause": "the ad-hoc broader checks reused mutable repository-local registry/lock state instead of an isolated repository-external test environment",
        "resolution": "reran the two exact reported integration selectors with a fresh /tmp root plus isolated data, artifact, report, cache, identity-registry, and XDG state paths",
        "final_result": "the isolated run passed the lock path but exposed the separate empty-policy bundle compatibility regression recorded as VI-46; both exact selectors passed after that correction",
        "status": "RESOLVED",
    },
    {
        "id": "VI-45",
        "phase": "independent validation rerun orchestration",
        "command": "fresh /tmp validation script with an EXIT trap that recursively removed its generated temporary directory",
        "observed": "the command runner rejected the script before execution because its safety policy does not permit rm -f style cleanup",
        "cause": "the diagnostic wrapper included unnecessary destructive cleanup even though leaving the unique /tmp directory was safe",
        "resolution": "removed the cleanup trap and reran the same focused test/static sequence in a new unique /tmp directory",
        "final_result": "52 focused tests passed in the independent rerun; no repository test executed in the rejected attempt",
        "status": "RESOLVED",
    },
    {
        "id": "VI-46",
        "phase": "research-only validation capability compatibility",
        "command": "isolated exact selectors for validation admission and final-holdout-once integration, followed by the 52-test validation capability group",
        "observed": "1 integration test failed and 1 passed: a research_only manifest with no required experiment and no supplied output still tried to build an empty bundle from a minimal mocked selection report, then rejected its absent dataset snapshot hash",
        "cause": "the new non-optional capability correctly existed for every current result, but bundle construction did not distinguish an empty authoritative policy from a candidate-validation policy requiring evidence",
        "resolution": "kept the capability/policy marker in every current result while skipping bundle construction only when required_components is empty and no experiment outputs are supplied",
        "final_result": "the two exact integration selectors passed in 29.16s; the 52 focused validation tests, targeted Ruff, and strict mypy all passed",
        "status": "RESOLVED",
    },
    {
        "id": "VI-47",
        "phase": "serial versus process-parallel stochastic determinism",
        "command": "PYTHONHASHSEED=0 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 BLIS_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 TMPDIR=/tmp/research-c05.C5b5af .venv/bin/pytest -q tests/test_frozen_dataset_multi_split_integration.py::test_serial_and_process_parallel_stochastic_backtest_are_causally_equivalent",
        "observed": "the first focused run failed closed because PYTHONHASHSEED was not an explicit integer; a second run with only PYTHONHASHSEED=0 failed because OMP_NUM_THREADS was not fixed at 1",
        "cause": "the two ad-hoc invocations did not initially reproduce the complete strict deterministic environment established by scripts/platform",
        "resolution": "fixed Python hash state plus all six supported numerical-library thread variables, then reran the exact official serial/process-parallel selector",
        "final_result": "1 passed in 54.53s; serial 1-worker and real process-parallel 2-worker runs produced exactly equal derived seeds, decisions, fills, ledgers, metrics v1/v2, equity and behavior hashes while retaining mode-specific manifest/artifact/work-unit/PID evidence",
        "status": "RESOLVED",
    },
    {
        "id": "VI-48",
        "phase": "validation experiment semantic-authority adversarial review",
        "command": "deterministic focused validation suite plus self-consistent manual rehash/rebind probes against terminal candidate selection, component scopes, native source hashes, and caller-selected experiment policies",
        "observed": "field stripping, explicit legacy promotion, stale-scope reuse and internal-result forgery fail closed, but a terminal candidate need only be a member rather than a nested winner; a caller can create a fresh self-consistent scope/source hash; and falsification thresholds, factor model and provider tolerances are not manifest-authoritative",
        "cause": "the manifest-derived capability names the four required component classes but does not authenticate calculation provenance or fully specify each component's semantic policy and selected-winner derivation",
        "resolution": "preserved the working downgrade/strip/rebind protections, documented the remaining attacks in the criterion gaps and remediation, and retained F-05/F-12/F-16/F-21 at M3 instead of claiming authoritative statistical validation",
        "final_result": "the exact adversarial protection selectors and focused validation suite passed; the winner, producer-authentication and policy-authority gaps remain open capability limits and are not represented as resolved implementation",
        "status": "OPEN_CAPABILITY_GAP_M3",
    },
    {
        "id": "VI-49",
        "phase": "corporate-action portfolio-plan integration race",
        "command": "TMPDIR=/tmp/research-c05.C5b5af .venv/bin/pytest -q tests/test_corporate_action_dataset_materialization.py --junitxml=/tmp/research-c05.C5b5af/corporate-junit.xml",
        "observed": "19 failed and 6 passed in the shared intermediate state; every failure was TypeError: build_corporate_action_portfolio_plan() missing required keyword-only argument quantity_step at dataset_snapshot.py:695",
        "cause": "the plan builder gained a quantity-step accounting authority before its official dataset-snapshot call site was updated in the concurrently shared implementation",
        "resolution": "passed the manifest instrument quantity step through the official materialization call site and reran the complete corporate-action test file after all related changes settled",
        "final_result": "the final exact corporate-action file passed 33/33 in 20.18s and the 15-file accounting/lineage focused group passed 127/127 in 69.07s",
        "status": "RESOLVED",
    },
    {
        "id": "VI-50",
        "phase": "corporate terminal event after final market observation",
        "command": "pytest -q tests/test_corporate_action_dataset_materialization.py::test_terminal_recovery_after_last_candle_drains_to_declared_split_end",
        "observed": "a cash delisting effective after the last candle but before the declared split end remained unapplied, leaving cash 10000, quantity 9000, basis 990000, no closed trade and a stale-price equity mark",
        "cause": "the simulation consumed corporate actions only inside the candle loop and had no bounded post-market terminal drain",
        "resolution": "drained only cash-terminal actions through the split-end-exclusive horizon, closed quantity and basis, recorded realized cash recovery and emitted a synthetic terminal equity mark; non-terminal economic actions without a later raw mark fail closed",
        "final_result": "the exact regression passed with cash/equity 505000, quantity and basis zero, one closed trade, terminal non-tradability and hash-bound application evidence",
        "status": "RESOLVED",
    },
    {
        "id": "VI-51",
        "phase": "same-time corporate entitlement ordering",
        "command": "pytest -q tests/test_corporate_action_dataset_materialization.py::test_same_timestamp_entitlement_never_depends_on_event_id_order",
        "observed": "a ratio-2 split and cash dividend 5 at the same effective/observed timestamp paid either 90000 or 45000 solely according to lexical event ID order",
        "cause": "the contract defined action-versus-fill precedence but no reviewed entitlement basis or priority between simultaneous quantity-changing and cash events",
        "resolution": "rejected distinct event IDs at the same effective timestamp unless an explicit future precedence contract can represent their entitlement semantics",
        "final_result": "both reversed-ID parameter cases passed by failing closed with same_timestamp_event_ordering_terms_required, so arbitrary identity no longer changes economics",
        "status": "RESOLVED",
    },
    {
        "id": "VI-52",
        "phase": "mixed cash and replacement-stock merger accounting",
        "command": "pytest -q tests/test_corporate_action_dataset_materialization.py::test_mixed_cash_and_replacement_merger_remains_fail_closed",
        "observed": "an ETF merger containing cash_amount=55 and a replacement instrument was admitted, paid the cash leg and zeroed quantity/basis while silently discarding the stock leg",
        "cause": "the single-instrument portfolio ledger can represent cash terminal recovery but cannot carry a replacement-instrument position",
        "resolution": "limited terminal merger support to cash-only terms and rejected every non-null replacement instrument as an unsupported stock conversion",
        "final_result": "the exact mixed-consideration regression passed with stock_merger_conversion_unsupported and no partial settlement",
        "status": "RESOLVED",
    },
    {
        "id": "VI-53",
        "phase": "sub-millisecond corporate knowledge-time boundary",
        "command": "pytest -q tests/test_corporate_action_dataset_materialization.py::test_sub_millisecond_late_observation_cannot_floor_into_effective_boundary tests/test_corporate_action_dataset_materialization.py::test_nonempty_action_set_requires_valid_hash_bound_known_at",
        "observed": "an observation 0.5ms after effective time could floor to the same integer millisecond and bypass the late-observation boundary; a sub-ms known-at authority had the same ambiguous resolution",
        "cause": "the contract accepted ISO timestamps finer than the millisecond simulation engine and converted them with truncating integer arithmetic",
        "resolution": "required exact millisecond alignment for event and manifest corporate-action authority timestamps before any causal comparison or materialization",
        "final_result": "the event and known-at regressions passed by rejecting ambiguous timestamps with explicit millisecond-alignment errors",
        "status": "RESOLVED",
    },
    {
        "id": "VI-54",
        "phase": "corporate focused-test selector correction",
        "command": "TMPDIR=/tmp .venv/bin/pytest -q -s tests/test_corporate_action_contract.py tests/test_corporate_action_dataset_materialization.py tests/test_portfolio_accounting_properties.py tests/test_execution_evidence.py tests/test_backtest_lineage.py",
        "observed": "pytest exited before collection with file or directory not found: tests/test_corporate_action_contract.py; three requested filenames did not exist and no tests ran",
        "cause": "the ad-hoc focused command inferred test filenames from production module names instead of resolving the repository's actual test inventory",
        "resolution": "resolved selectors with rg --files, used the existing corporate, instrument, point-in-time, ledger, engine, evidence and lineage test files, and recorded the invalid command rather than counting it as validation",
        "final_result": "the corrected 15-file exact group passed 127/127 in 69.07s; the nonexistent-file invocation contributes no test evidence",
        "status": "RESOLVED",
    },
    {
        "id": "VI-55",
        "phase": "validated-candidate external experiment bundle production E2E",
        "command": ".venv/bin/pytest -q -s --basetemp=/tmp/research-e2e-final-frozen-20260811 tests/test_strategy_extension_production_e2e.py::test_validated_new_strategy_reaches_authoritative_package_and_reproduction -x",
        "observed": "the original official E2E failed after 128.54s because validated_candidate now required four experiment components but supplied no bundle. Successive honest preparer runs then failed at 4m15s for nested-winner/candidate-hash rebinding, at 271.55s for raw-float versus 12-digit wire replay, and at 397.88s with reproduction DRIFT after source was intentionally hardened during the run",
        "cause": "the pre-existing fixture lacked a pre-holdout native experiment preparation path; candidate logical identity included path/runtime-derived aliases; nested eligibility did not bind the terminal candidate; serialized semantic replay used different numeric arithmetic; and one diagnostic run correctly detected a mid-run source change",
        "resolution": "added a test-only external preparer over the complete manifest candidate grid, a manifest-declared stability-conditioned nested metric, four native typed outputs, distinct frozen provider inputs, portable logical candidate binding and exact canonical wire-domain replay. The CLI gate remains fail-closed and receives the immutable external bundle through its official request/service path",
        "final_result": "the frozen-source rerun passed in 570.46s with identical start/end source hash 2158f2b3877c902be870d2df8dd6f7dd26037cf6741f5466e5188447fd799f60; all four components and every outer-fold winner bound to terminal candidate_9c407479, then selection reproduction, terminal verification, approval, package, reproduction and prospective lifecycle passed. This is bespoke synthetic M3 evidence, not production-native auto-generation, authenticated calculation provenance, real second-vendor evidence, or sandbox bundle-ref plumbing",
        "status": "RESOLVED_WITH_SCOPE_LIMIT",
    },
    {
        "id": "VI-56",
        "phase": "post-E2E focused validation capture storage",
        "command": "pytest focused application-contract and validation-pipeline files with default capture, followed by the same selectors with -s and a unique native /tmp basetemp",
        "observed": "the capture-enabled attempt lost its inherited pytest temporary file and ran no tests in 0.67s with FileNotFoundError",
        "cause": "the ad-hoc process inherited unstable cross-platform capture storage; no product assertion had been collected",
        "resolution": "disabled capture, allocated a unique native /tmp basetemp, and reran exactly the two intended files without broadening the selector",
        "final_result": "13 focused application-contract and validation-pipeline tests passed in 0.62s; the failed capture attempt contributes no execution evidence",
        "status": "RESOLVED",
    },
    {
        "id": "VI-57",
        "phase": "final validation and audit static-typing scope",
        "command": ".venv/bin/mypy <12 validation/audit implementation paths> tests/test_validation_experiments.py tests/test_validation_experiment_bundle.py tests/test_candidate_semantic_binding.py tests/test_application_contracts_and_capabilities.py tests/test_validation_pipeline_gate.py tests/test_strategy_extension_production_e2e.py tests/test_reference_audit.py",
        "observed": "the first ad-hoc invocation reported 87 errors in 8 test/helper files while checking 19 paths in 13.35s; the errors were strict annotations in tests and transitively followed test helpers, not implementation diagnostics",
        "cause": "the command mixed the repository's configured implementation typecheck surface with pytest modules that are intentionally outside the strict mypy package targets",
        "resolution": "preserved the failed invocation, reran mypy over the 12 changed implementation/tool paths, and separately executed the canonical scripts/platform typecheck across all four configured package targets",
        "final_result": "the 12-path focused mypy run passed with no issues in 8.37s and scripts/platform typecheck passed for 271, 54, 21 and 6 source files; no test module was reclassified as typed evidence",
        "status": "RESOLVED",
    },
    {
        "id": "VI-58",
        "phase": "retained-history iteration-generalized negative tests",
        "command": "deterministic repository-external pytest run of tests/test_reference_audit.py after preserving iteration 6 and generating the current iteration-7 matrix",
        "observed": "39 passed and 2 failed in 15.01s; both failures expected assessment_history_6 receipt findings even though the forged claim was correctly reported against the new current assessment_history_7 entry",
        "cause": "two receipt-negative tests hardcoded the former current iteration number, and the report schema assertions also encoded exactly one retained snapshot rather than the validated contiguous retained-history sequence",
        "resolution": "derived expected finding IDs from assessment.iteration and generalized machine-result/history assertions to logical phases 1-5, every contiguous retained snapshot from 6 through current-1, and one current-surface final entry",
        "final_result": "the exact full reference-audit test file passed 41/41 in 14.52s after the iteration-7 matrix was retained and the iteration-8 current surface was generated",
        "status": "RESOLVED",
    },
    {
        "id": "VI-59",
        "phase": "single repository-wide Research, Internal Web and Operations test run",
        "command": "env -u RESEARCH_OPS_DATABASE_URL -u RESEARCH_OPS_TEST_DATABASE_URL -u PLAYWRIGHT_BROWSERS_PATH TMPDIR=/tmp/research-final-full.HWjrEE/tmp TMP=/tmp/research-final-full.HWjrEE/tmp TEMP=/tmp/research-final-full.HWjrEE/tmp PYTHONHASHSEED=0 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 BLIS_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 DJANGO_SETTINGS_MODULE=market_research_web.settings_test INTERNAL_WEB_SECRET_KEY=codex-final-audit-test-key RESEARCH_DATA_ROOT=/tmp/research-final-full.HWjrEE/data RESEARCH_ARTIFACT_ROOT=/tmp/research-final-full.HWjrEE/artifacts RESEARCH_REPORT_ROOT=/tmp/research-final-full.HWjrEE/reports RESEARCH_CACHE_ROOT=/tmp/research-final-full.HWjrEE/cache RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH=/tmp/research-final-full.HWjrEE/identity/experiment-identities.jsonl XDG_STATE_HOME=/tmp/research-final-full.HWjrEE/xdg RESEARCH_OPS_SOURCE_ROOT=/home/vorac/work/Research uv run --frozen --no-sync --package research-operations pytest -q --basetemp=/tmp/research-final-full.HWjrEE/pytest tests apps/internal_web/tests services/research_operations/tests",
        "observed": "the one authorized full invocation completed in 2592.47s with 2173 passed, 40 skipped and 12 failed. Eleven failures rejected legacy approval/package fixtures with validated_research_result_validation_experiment_capability_missing; the remaining failure reported stale generated multi-asset audit outputs",
        "cause": "the new mandatory validated-candidate capability correctly failed closed, but eleven test-only validated-result fixtures had not been migrated to carry the four-component hash-bound experiment bundle. The multi-asset audit source snapshot had also changed since its last generated output",
        "resolution": "kept the production gate and validated_candidate classification unchanged, attached a complete synthetic manifest/dataset/temporal/candidate-bound experiment capability and PASS bundle only to the approval/package fixtures, and regenerated the three legacy multi-asset audit outputs with their official renderer",
        "final_result": "all eleven exact validation-capability failure selectors passed (one preliminary selector plus the remaining ten in 18.49s); the exact multi-asset currentness selector passed, its renderer --check and scripts/platform verify-multi-asset-audit-result both returned VALID. Per policy, the repository-wide invocation was not repeated",
        "status": "RESOLVED",
    },
)

_EXTERNAL_VALIDATION_CLAIMS: tuple[str, ...] = (
    "UNVERIFIED_EXTERNAL — apps/internal_web/tests/test_database_immutability_postgresql.py requires an isolated PostgreSQL role/DSN with migration authority; raw UPDATE/DELETE/TRUNCATE trigger behavior was not executed locally.",
    "UNVERIFIED_EXTERNAL — services/research_operations/tests/test_service_alert_postgresql.py requires an authorized PostgreSQL test role/DSN; durable lease, retry, restart and tamper behavior was not executed against live PostgreSQL.",
    "UNVERIFIED_EXTERNAL — the native systemd profile still needs deployment-host evidence for root:root 0600 credential sources, actual LoadCredential projection, PKI, HTTPS alert delivery, off-site backup, and RPO/RTO recovery.",
    "UNVERIFIED_EXTERNAL — apps/internal_web/tests/test_browser_e2e.py needs the live PostgreSQL browser profile and installed Chromium system prerequisites; local adapter workflow tests do not substitute for that E5 evidence.",
    "UNVERIFIED_EXTERNAL — the live PostgreSQL prior-release upgrade case in services/research_operations/tests/test_prior_release_upgrade.py needs an authorized disposable database role/DSN and was excluded from the local E4 receipt.",
    "UNVERIFIED_EXTERNAL — actual organization IdP/HSM principal-key issuance, rotation and revocation were not available; local tests validate the trust-store assertion contract only.",
)


def _md(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def _rank(row: dict[str, Any]) -> int:
    return int(str(row["maturity"]).removeprefix("M"))


def _is_locally_verified(row: dict[str, Any]) -> bool:
    return row["status"] in {"VERIFIED", "VERIFIED_LOCAL_SELF_ATTESTED"}


def _criteria_by_id(matrix: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row["id"]): row for row in matrix["criteria"]}


def _receipt_summary(
    matrix: dict[str, Any], evaluation: AuditEvaluation
) -> dict[str, object]:
    declared = matrix["assessment"]["execution_receipt"]
    return {
        "path": declared["path"],
        "status": evaluation.execution_receipt_status,
        "clean_local_run": evaluation.execution_receipt_clean_local_run,
        "trusted": evaluation.execution_receipt_trusted,
        "trust_level": evaluation.execution_receipt_trust_level,
        "content_sha256": evaluation.execution_receipt_content_sha256,
        "required_target_count": declared["required_target_count"],
        "tests_passed": evaluation.execution_receipt_tests_passed,
        "findings": list(evaluation.execution_receipt_findings),
    }


def _question_result(
    *,
    number: int,
    question: str,
    criterion_ids: tuple[str, ...],
    criteria: dict[str, dict[str, Any]],
    receipt_valid: bool,
) -> dict[str, object]:
    rows = [criteria[criterion_id] for criterion_id in criterion_ids]
    statuses = [str(row["status"]) for row in rows]
    ranks = [_rank(row) for row in rows]
    locally_verified = {"VERIFIED", "VERIFIED_LOCAL_SELF_ATTESTED"}
    if receipt_valid and all(status in locally_verified for status in statuses):
        answer = "YES"
    elif statuses and all(
        status in {"MISSING", "DOCUMENTATION_ONLY", "PLACEHOLDER"}
        for status in statuses
    ):
        answer = "NO"
    elif max(ranks, default=0) <= 1:
        answer = "NO"
    elif all(status == "UNVERIFIED_EXTERNAL" for status in statuses):
        answer = "UNVERIFIED"
    else:
        answer = "PARTIAL"
    evidence = []
    for row in rows:
        objective = row["objective_evidence"][0]
        evidence.append(
            f"{row['id']}={row['status']} ({objective['path']}; {objective['test']})"
        )
    explanation = (
        f"{question} 관련 {', '.join(criterion_ids)}의 현재 판정은 "
        f"{', '.join(statuses)}이다. 실행 영수증 상태는 "
        f"{'LOCAL_CLEAN_SELF_ATTESTED' if receipt_valid else '유효하지 않음'}이며, "
        "로컬 clean run이 없으면 YES로 승격하지 않는다. YES도 독립 M5 "
        "attestation을 의미하지 않는다."
    )
    return {
        "number": number,
        "answer": answer,
        "evidence": evidence,
        "explanation": explanation,
    }


def _priority(row: dict[str, Any]) -> str:
    rank = _rank(row)
    if row["importance"] == "C" and rank <= 1:
        return "P0"
    if row["importance"] == "C":
        return "P1"
    if row["importance"] == "M":
        return "P2"
    return "P3"


def _top_gaps(matrix: dict[str, Any]) -> list[dict[str, object]]:
    gaps: list[dict[str, object]] = []
    for gate in matrix["fatal_gates"]:
        if gate["status"] == "PASS":
            continue
        test = str(gate["verification_method"]).rsplit(" ", 1)[-1]
        gaps.append(
            {
                "priority": "P0",
                "criterion_ids": [gate["id"]],
                "title": f"{gate['id']} {gate['title']}",
                "why_it_matters": gate["impact"],
                "required_implementation": [gate["required_remediation"]],
                "required_tests": [test],
                "definition_of_done": [
                    "현재 assessment surface에 결속된 실행 증거로 PASS가 확인된다."
                ],
            }
        )
    remaining = [row for row in matrix["criteria"] if row["status"] != "VERIFIED"]
    remaining.sort(
        key=lambda row: (
            {"C": 0, "M": 1, "S": 2}[row["importance"]],
            _rank(row),
            row["id"],
        )
    )
    for row in remaining:
        evidence_tests = sorted(
            {str(item["test"]) for item in row["objective_evidence"]}
        )
        gaps.append(
            {
                "priority": _priority(row),
                "criterion_ids": [row["id"]],
                "title": row["title"],
                "why_it_matters": row["gap"],
                "required_implementation": [row["required_remediation"]],
                "required_tests": evidence_tests,
                "definition_of_done": [row["completion_condition"]],
            }
        )
        if len(gaps) == 20:
            break
    return gaps


def _unverified_claims(
    matrix: dict[str, Any], evaluation: AuditEvaluation
) -> list[str]:
    claims = (
        [
            (
                "현재 A--J 실행 영수증이 유효하지 않다: "
                f"status={evaluation.execution_receipt_status}; "
                f"findings={','.join(evaluation.execution_receipt_findings) or 'none'}."
            )
        ]
        if not evaluation.execution_receipt_clean_local_run
        else []
    )
    if (
        evaluation.execution_receipt_clean_local_run
        and not evaluation.execution_receipt_trusted
    ):
        claims.append(
            "execution_authenticity_unverified — clean local run receipt is "
            "unkeyed and self-attested; it supports local M4 execution only, "
            "not authenticated CI or independent M5 evidence."
        )
    claims.extend(
        f"{gate['id']} {gate['title']}: {gate['status']} — {gate['evidence']}"
        for gate in matrix["fatal_gates"]
        if gate["status"] != "PASS"
    )
    claims.extend(
        f"{row['id']} {row['title']}: {row['status']} — {row['gap']}"
        for row in matrix["criteria"]
        if row["status"] == "UNVERIFIED_EXTERNAL"
    )
    claims.extend(_EXTERNAL_VALIDATION_CLAIMS)
    return claims


def _machine_result(
    matrix: dict[str, Any], evaluation: AuditEvaluation
) -> dict[str, Any]:
    criteria_index = _criteria_by_id(matrix)
    receipt = _receipt_summary(matrix, evaluation)
    questions = [
        _question_result(
            number=index,
            question=question,
            criterion_ids=criterion_ids,
            criteria=criteria_index,
            receipt_valid=evaluation.execution_receipt_clean_local_run,
        )
        for index, (question, criterion_ids) in enumerate(_FINAL_QUESTIONS, start=1)
    ]
    commands = [str(incident["command"]) for incident in _VALIDATION_INCIDENTS]
    if evaluation.execution_receipt_clean_local_run:
        commands.append(
            ".venv/bin/python -m pytest -q <receipt.pytest.targets>; "
            f"tests_passed={evaluation.execution_receipt_tests_passed}; "
            f"receipt_sha256={evaluation.execution_receipt_content_sha256}"
        )
    receipt_auth_reason = (
        "local self-attested receipt의 execution authenticity가 검증되지 않았고"
        if evaluation.execution_receipt_clean_local_run
        else "현재 source surface에 결속된 clean-PASS 실행 receipt가 없고"
    )
    result_criteria = [
        {
            "id": row["id"],
            "importance": _IMPORTANCE_NAMES[row["importance"]],
            "maturity": row["maturity"],
            "status": row["status"],
            "evidence": row["objective_evidence"],
            "gap": row["gap"],
            "required_remediation": row["required_remediation"],
        }
        for row in matrix["criteria"]
    ]
    return {
        "verdict": evaluation.verdict,
        "is_complete_against_reference": evaluation.complete,
        "overall_score": round(evaluation.score, 4),
        "raw_weighted_score": round(evaluation.raw_score, 4),
        "score_cap": evaluation.score_cap,
        "canonical_source": matrix["canonical_source"],
        "audit_history": {
            "session_id": matrix["canonical_source"]["audit_session_id"],
            "iteration_count": matrix["assessment"]["iteration"],
            "current_surface_iteration": matrix["assessment"]["iteration"],
            "retained_snapshot_iterations": [
                entry["iteration"]
                for entry in matrix["criteria"][0]["assessment_history"]
                if entry["history_kind"] == "retained_assessment_snapshot"
            ],
            "semantics": matrix["assessment"]["history_semantics"],
            "phases": [
                {
                    "iteration": entry["iteration"],
                    "phase": entry["phase"],
                    "history_kind": entry["history_kind"],
                    "evidence_scope": entry["evidence_scope"],
                }
                for entry in matrix["criteria"][0]["assessment_history"]
            ],
        },
        "repository": {
            "root": str(PROJECT_ROOT),
            "commit": matrix["assessment"]["repository_commit"],
            "commit_role": matrix["assessment"]["repository_commit_role"],
            "branch": matrix["assessment"]["repository_branch"],
            "dirty": not matrix["assessment"]["worktree_was_clean"],
            "assessment_surface": matrix["assessment"]["assessment_surface"],
            "primary_languages": ["Python", "SQL", "Shell", "HTML", "JavaScript"],
            "entrypoints": [
                "scripts/platform",
                "market-research",
                "Django internal web adapter",
                "research-operations",
            ],
            "test_commands": [
                ".venv/bin/python tools/reference_audit_receipt.py",
                ".venv/bin/python tools/reference_audit.py --validate-structure",
            ],
        },
        "execution_receipt": receipt,
        "fatal_gates": [
            {
                "id": row["id"],
                "status": row["status"],
                "evidence": [row["evidence"]],
                "verification_method": row["verification_method"],
                "impact": row["impact"],
                "mitigation_possible": row["mitigation_possible"],
                "required_remediation": row["required_remediation"],
            }
            for row in matrix["fatal_gates"]
        ],
        "domain_scores": {
            _DOMAIN_NAMES[domain][0]: {
                "max": DOMAIN_POINTS[domain],
                "score": round(evaluation.domain_scores[domain], 4),
            }
            for domain in DOMAIN_POINTS
        },
        "criteria": result_criteria,
        "final_questions": questions,
        "top_gaps": _top_gaps(matrix),
        "unverified_external_dependencies": _unverified_claims(matrix, evaluation),
        "commands_executed": commands,
        "tests_failed": [dict(incident) for incident in _VALIDATION_INCIDENTS],
        "final_reasoning": (
            f"현재 점수는 {evaluation.score:.4f}/100이고 판정은 "
            f"{evaluation.verdict}이다. 실행 영수증 상태는 "
            f"{evaluation.execution_receipt_status}, trust="
            f"{evaluation.execution_receipt_trust_level}이며, "
            f"실패 게이트는 {', '.join(evaluation.fatal_failures) or '없음'}, "
            f"미검증 게이트는 {', '.join(evaluation.fatal_unverified) or '없음'}이다. "
            f"{receipt_auth_reason}, 모든 기준 authenticated VERIFIED, Critical M4+, "
            "95점 이상 및 모든 fatal gate PASS가 동시에 충족되지 않아 "
            "COMPLETE로 판정하지 않는다."
        ),
    }


def _domain_rows(matrix: dict[str, Any], domain: str) -> list[dict[str, Any]]:
    return [row for row in matrix["criteria"] if row["domain"] == domain]


def _render_report(
    matrix: dict[str, Any], evaluation: AuditEvaluation, result: dict[str, Any]
) -> str:
    criteria = matrix["criteria"]
    receipt = result["execution_receipt"]
    verified_count = sum(row["status"] == "VERIFIED" for row in criteria)
    local_verified_count = sum(_is_locally_verified(row) for row in criteria)
    critical_pct = (
        evaluation.critical_m4_or_higher / evaluation.critical_count * 100
        if evaluation.critical_count
        else 0.0
    )
    execution_state = (
        f"LOCAL YES — {receipt['tests_passed']} tests, self-attested receipt `{receipt['content_sha256']}`; execution authenticity unverified"
        if receipt["clean_local_run"]
        else f"UNVERIFIED — receipt {receipt['status']}"
    )
    retained_iterations = [
        entry["iteration"]
        for entry in criteria[0]["assessment_history"]
        if entry["history_kind"] == "retained_assessment_snapshot"
    ]
    lines: list[str] = [
        "# 투자 연구 전용 플랫폼 완전성 감사 — A–J 기준 보고서",
        "",
        f"기준 원문 SHA-256: `{matrix['canonical_source']['sha256']}`",
        f"실행 지시 SHA-256: `{matrix['canonical_source']['instruction_sha256']}`",
        f"평가 identity: source surface `{matrix['assessment']['assessment_surface']['sha256']}`",
        (
            "Git provenance: generation base "
            f"`{matrix['assessment']['repository_commit']}` "
            f"({matrix['assessment']['repository_commit_role']})"
        ),
        "",
        "## 13.1 Executive Verdict",
        "",
        "| 항목 | 결과 |",
        "| --- | --- |",
        f"| 최종 판정 | {evaluation.verdict} |",
        (
            f"| 총점 | {evaluation.score:.4f}/100 "
            f"(raw {evaluation.raw_score:.4f}, cap {evaluation.score_cap:.0f}) |"
        ),
        (
            "| 감사 session / iteration | "
            f"`{matrix['canonical_source']['audit_session_id']}` / "
            f"{matrix['assessment']['iteration']} "
            f"(retained={retained_iterations}; current="
            f"{matrix['assessment']['iteration']}) |"
        ),
        f"| 완전 충족(authenticated VERIFIED) 기준 수 | {verified_count}/{len(criteria)} |",
        f"| 로컬 M4 포함 판정 기준 수 | {local_verified_count}/{len(criteria)} |",
        (
            f"| 치명적 결함 수 | {len(evaluation.fatal_failures)} "
            f"({', '.join(evaluation.fatal_failures) or '없음'}) |"
        ),
        (
            f"| 미검증 치명 게이트 수 | {len(evaluation.fatal_unverified)} "
            f"({', '.join(evaluation.fatal_unverified) or '없음'}) |"
        ),
        (
            f"| Critical 기준 통과율 | {evaluation.critical_m4_or_higher}/"
            f"{evaluation.critical_count} ({critical_pct:.1f}%) |"
        ),
        f"| 종단 간 재현 성공 여부 | {execution_state} |",
        f"| 반복 기록 의미 | {_md(matrix['assessment']['history_semantics'])} |",
        (
            "| 시점 정확성 검증 여부 | "
            f"{'현재 local self-attested 영수증에 결속됨' if receipt['clean_local_run'] else 'UNVERIFIED'} |"
        ),
        (
            "| 독립 검증 가능 여부 | "
            f"{'PARTIAL' if receipt['clean_local_run'] else 'UNVERIFIED'} |"
        ),
        "| 연구·실거래 경계 준수 여부 | local receipt와 경계 테스트로 판정하되 execution authenticity는 별도 미검증 |",
        "",
        (
            f"현재 판정은 `{evaluation.verdict}`이며 COMPLETE가 아니다. "
            f"점수는 {evaluation.score:.4f}점이고 fatal failure는 "
            f"{', '.join(evaluation.fatal_failures) or '없음'}이다. "
            f"현재 해시 결합 실행 영수증은 `{receipt['status']}` 상태이고 "
            f"trust는 `{receipt['trust_level']}`이다. local clean run이면 M4 실행 "
            "근거로 보존하지만 unkeyed persisted receipt만으로 실행 주체를 인증하거나 "
            "M5/독립 증거로 간주하지 않는다. 모든 과거 실행 서술과 외부 절대 경로는 현재 증거에서 "
            "제외했다. source surface가 감사 identity이고 Git SHA는 생성 시점의 "
            "provenance만 기록한다. 완전 판정에는 trusted attestation, 184개 authenticated VERIFIED, "
            "Critical M4+, 95점 이상, 12개 fatal gate PASS가 모두 필요하다."
        ),
        "",
        "## 13.2 Repository Profile",
        "",
        "| 항목 | 값 |",
        "| --- | --- |",
        "| 레포 이름 | market-research platform monorepo |",
        f"| 커밋 SHA | `{matrix['assessment']['repository_commit']}` (generation base) |",
        f"| 브랜치 | `{matrix['assessment']['repository_branch']}` |",
        "| 기술 스택 | Python, SQL, Shell, HTML, JavaScript |",
        "| 주요 실행 진입점 | `scripts/platform`, `market-research`, internal web, research operations |",
        "| 테스트 프레임워크 | pytest |",
        "| 데이터 저장 기술 | repository-external immutable datasets/artifacts; SQLite; operations PostgreSQL |",
        "| 실험 추적 기술 | 매트릭스의 C·D·H 증거 경로 참조 |",
        "| 오케스트레이션 기술 | offline research workflow 및 operations adapter |",
        "| UI/API/CLI | internal web adapter와 research CLI |",
        "| 외부 서비스 의존성 | externally prepared immutable datasets 및 operations infrastructure |",
        "| 감사 시 검증하지 못한 인프라 | 실행 영수증과 fatal gate에서 UNVERIFIED/FAIL로 표시된 범위 |",
        "",
        "## 13.3 Evidence Summary",
        "",
        "| 구성요소 | 상태 | 핵심 증거 | 실행 검증 | 주요 공백 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for domain in DOMAIN_POINTS:
        rows = _domain_rows(matrix, domain)
        paths = sorted(
            {str(item["path"]) for row in rows for item in row["objective_evidence"]}
        )
        tests = sorted(
            {str(item["test"]) for row in rows for item in row["objective_evidence"]}
        )
        domain_status = (
            "VERIFIED_LOCAL_SELF_ATTESTED"
            if rows and all(_is_locally_verified(row) for row in rows)
            else "INCOMPLETE"
        )
        gaps = [str(row["gap"]) for row in rows if row["status"] != "VERIFIED"]
        lines.append(
            f"| {domain}. {_md(_DOMAIN_NAMES[domain][1])} | {domain_status} | "
            f"{_md('; '.join(paths[:4]))} | receipt={receipt['status']}; "
            f"{len(tests)} test file targets | {_md(gaps[0] if gaps else '없음')} |"
        )
    lines.extend(
        [
            "",
            "## 13.4 Fatal Gate Results",
            "",
            "| 게이트 | 판정 | 증거 | 영향 | 필수 조치 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for gate in matrix["fatal_gates"]:
        lines.append(
            f"| {gate['id']} {_md(gate['title'])} | {gate['status']} | "
            f"{_md(gate['evidence'])} | {_md(gate['impact'])} | "
            f"{_md(gate['required_remediation'])} |"
        )
    lines.extend(
        [
            "",
            "## 13.5 Domain Scores",
            "",
            "| 영역 | 배점 | 획득 점수 | 핵심 강점 | 핵심 공백 |",
            "| --- | ---: | ---: | --- | --- |",
        ]
    )
    for domain in DOMAIN_POINTS:
        rows = _domain_rows(matrix, domain)
        strongest = max(rows, key=_rank)
        weakest = min(rows, key=_rank)
        lines.append(
            f"| {domain}. {_md(_DOMAIN_NAMES[domain][1])} | "
            f"{DOMAIN_POINTS[domain]:.0f} | "
            f"{evaluation.domain_scores[domain]:.4f} | "
            f"{strongest['id']} {strongest['maturity']} | "
            f"{weakest['id']} {weakest['maturity']}: {_md(weakest['gap'])} |"
        )
    lines.extend(
        [
            "",
            "영역별로 Critical=3, Major=2, Supporting=1 가중치와 M0=0, M1=.10, "
            "M2=.40, M3=.65, M4=.85, M5=1.0 배율을 적용해 영역 배점에 비례 "
            "환산하고 합산했다. 현재 평가의 총점 상한은 "
            f"{evaluation.score_cap:.0f}점이다. clean local receipt는 M4를 "
            "VERIFIED_LOCAL_SELF_ATTESTED로만 부여하며 gate PASS도 local "
            "execution 판정이다. authenticated VERIFIED/M5와 COMPLETE에는 "
            "trusted external attestation이 별도로 필요하다.",
            "",
            "## 13.6 Criterion-Level Matrix",
            "",
            "| 기준 ID | 중요도 | 성숙도 | 판정 | 증거 | 누락·위험 | 수정 요구사항 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in criteria:
        evidence = "; ".join(
            f"{item['path']}::{item['symbol_or_lines']}; {item['test']}; "
            f"{item['result']}"
            for item in row["objective_evidence"]
        )
        lines.append(
            f"| {row['id']} | {_IMPORTANCE_NAMES[row['importance']]} | "
            f"{row['maturity']} | {row['status']} | {_md(evidence)} | "
            f"{_md(row['gap'])} | {_md(row['required_remediation'])} |"
        )
    lines.extend(
        [
            "",
            "## 13.7 Architecture Coverage Map",
            "",
            "| 이상적 구성요소 | 실제 구현 위치 | 상태 | 통합 수준 | 테스트 | 비고 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for domain in DOMAIN_POINTS:
        rows = _domain_rows(matrix, domain)
        paths = sorted(
            {str(item["path"]) for row in rows for item in row["objective_evidence"]}
        )
        tests = sorted(
            {str(item["test"]) for row in rows for item in row["objective_evidence"]}
        )
        m4 = sum(_rank(row) >= 4 for row in rows)
        lines.append(
            f"| {domain}. {_md(_DOMAIN_NAMES[domain][1])} | "
            f"{_md('; '.join(paths[:5]))} | "
            f"{'LOCAL_M4_COMPLETE' if m4 == len(rows) else 'INCOMPLETE'} | "
            f"M4+ {m4}/{len(rows)} | {_md('; '.join(tests[:5]))} | "
            f"execution receipt={receipt['status']} |"
        )
    lines.extend(
        [
            "",
            "## 13.8 Research Lifecycle Walkthrough",
            "",
            (
                "현재 receipt가 clean local run일 때만 아래 테스트 파일을 local M4 "
                "실행 근거로 인정한다. self-attested receipt는 실행 주체를 인증하지 "
                "않으므로 하나의 독립 M5 샘플 연구가 전 단계를 완주했다고 주장하지 않는다."
            ),
            "",
            "| 단계 | 실제 파일·계약 | 판정 | 실행 결과 |",
            "| --- | --- | --- | --- |",
        ]
    )
    criteria_index = _criteria_by_id(matrix)
    for stage, criterion_ids in _LIFECYCLE:
        rows = [criteria_index[item] for item in criterion_ids]
        paths = sorted(
            {str(item["path"]) for row in rows for item in row["objective_evidence"]}
        )
        statuses = ", ".join(f"{row['id']}={row['status']}" for row in rows)
        lines.append(
            f"| {stage} | {_md('; '.join(paths))} | {statuses} | "
            f"{'LOCAL-RECEIPT-BOUND PASS' if receipt['clean_local_run'] else 'UNVERIFIED'} |"
        )
    lineage_rows = [criteria_index[item] for item in ("B-14", "H-04", "H-11")]
    lineage_evidence = [
        str(item["path"]) for row in lineage_rows for item in row["objective_evidence"]
    ]
    lines.extend(
        [
            "",
            "## 13.9 Data Lineage Walkthrough",
            "",
            (
                "현재 local self-attested receipt에서 계약 수준의 trace 테스트를 "
                "실행했지만 실행 주체가 인증된 independent M5 trace로 보지는 않는다. "
                "추적 경로는 다음과 같다:"
                if receipt["clean_local_run"]
                else (
                    "현재 source surface에 결속된 clean-PASS receipt가 없어 실제 "
                    "지표에서 원천 데이터까지의 runtime trace를 완료로 보고하지 "
                    "않는다. 계약 수준의 추적 후보는 다음과 같다:"
                )
            ),
            "",
            "```text",
            "보고 지표",
            "→ 연구 산출물/검증 보고서",
            "→ experiment/parameter/execution-assumption binding",
            "→ dataset manifest/content hash",
            "→ externally prepared immutable dataset",
            "```",
            "",
            f"검사 대상: `{_md('`, `'.join(sorted(set(lineage_evidence))))}`. "
            "B-14/H-04/H-11이 모두 local M4 이상이고 동일 surface의 실행 receipt가 "
            "clean일 때만 local 양방향 계보 성공으로 판정하며 independent M5는 별도다.",
            "",
            "## 13.10 Reproducibility Walkthrough",
            "",
            f"canonical execution receipt: `{receipt['path']}`",
            f"status: `{receipt['status']}`",
            f"clean local run: `{receipt['clean_local_run']}`",
            f"trusted execution attestation: `{receipt['trusted']}`",
            f"trust level: `{receipt['trust_level']}`",
            f"content SHA-256: `{receipt['content_sha256']}`",
            f"required targets: `{receipt['required_target_count']}`",
            f"tests passed: `{receipt['tests_passed']}`",
            "",
        ]
    )
    if result["commands_executed"]:
        lines.extend(
            ["감사에서 실행하거나 시도한 명령:", ""]
            + [f"- {_md(command)}" for command in result["commands_executed"]]
        )
    if not receipt["clean_local_run"]:
        lines.extend(
            [
                "",
                "현재 surface에 결속된 clean-PASS 실행 영수증은 없다. "
                "`.venv/bin/python tools/reference_audit_receipt.py`가 정확한 target을 직접 "
                "실행해 영수증을 생성하기 전에는 과거 명령·시간·성공 선언을 현재 "
                "재현 증거로 기록하지 않는다.",
                "",
                "영수증 검증 결과:",
                "",
            ]
            + [f"- {_md(finding)}" for finding in receipt["findings"]]
        )
    lines.extend(
        [
            "",
            "검증 incident (해결된 실패도 숨기지 않고 보존):",
            "",
            "| ID | 단계 | 최초 관측 | 원인 | 조치·최종 상태 | 판정 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for incident in result["tests_failed"]:
        lines.append(
            f"| {_md(incident['id'])} | {_md(incident['phase'])} | "
            f"{_md(incident['observed'])} | {_md(incident['cause'])} | "
            f"{_md(incident['resolution'])}; {_md(incident['final_result'])} | "
            f"{_md(incident['status'])} |"
        )
    lines.extend(
        [
            "",
            "## 13.11 Anti-Pattern Findings",
            "",
            "| 안티패턴 | 탐지 여부 | 심각도 | 증거 | 영향 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for name, criterion_ids, impact in _ANTI_PATTERNS:
        rows = [criteria_index[item] for item in criterion_ids]
        cleared = all(_is_locally_verified(row) for row in rows)
        state = (
            "NOT DETECTED" if cleared and receipt["clean_local_run"] else "NOT CLEARED"
        )
        severity = (
            "CRITICAL" if any(row["importance"] == "C" for row in rows) else "MAJOR"
        )
        evidence = "; ".join(f"{row['id']}={row['status']}" for row in rows)
        lines.append(
            f"| {_md(name)} | {state} | {severity} | {_md(evidence)} | {_md(impact)} |"
        )
    lines.extend(["", "## 13.12 Unverified Claims", ""])
    unverified = result["unverified_external_dependencies"]
    if unverified:
        lines.extend(f"- {_md(item)}" for item in unverified)
    else:
        lines.append("- 없음")
    lines.extend(["", "## 13.13 Top Gaps", ""])
    for gap in result["top_gaps"]:
        lines.extend(
            [
                f"### {gap['priority']} — {_md(gap['title'])}",
                "",
                f"- 관련 기준: {', '.join(gap['criterion_ids'])}",
                f"- 현재 상태: {_md(gap['why_it_matters'])}",
                f"- 왜 중요한가: {_md(gap['why_it_matters'])}",
                f"- 필요한 구현: {_md('; '.join(gap['required_implementation']))}",
                f"- 필요한 테스트: {_md('; '.join(gap['required_tests']))}",
                f"- 완료 판정 조건: {_md('; '.join(gap['definition_of_done']))}",
                "",
            ]
        )
    lines.extend(["## 13.14 Remediation Roadmap", ""])
    for priority, title in (
        ("P0", "치명적 결함 제거"),
        ("P1", "연구 신뢰성 핵심"),
        ("P2", "플랫폼 완성도"),
        ("P3", "확장성과 사용성"),
    ):
        lines.extend([f"### {priority} — {title}", ""])
        priority_gaps = [
            gap for gap in result["top_gaps"] if gap["priority"] == priority
        ]
        if not priority_gaps:
            lines.extend(["- 현재 상위 20개 gap에 해당 항목 없음", ""])
            continue
        for gap in priority_gaps:
            item_id = gap["criterion_ids"][0]
            if item_id.startswith("FG-"):
                gate = next(
                    row for row in matrix["fatal_gates"] if row["id"] == item_id
                )
                module = str(gate["verification_method"]).rsplit(" ", 1)[-1]
            else:
                module = str(criteria_index[item_id]["objective_evidence"][0]["path"])
            lines.extend(
                [
                    f"#### {_md(gap['title'])}",
                    "",
                    f"- 구현 대상: {_md('; '.join(gap['required_implementation']))}",
                    f"- 예상 변경 모듈: `{_md(module)}`",
                    f"- 필수 테스트: {_md('; '.join(gap['required_tests']))}",
                    f"- 의존성: {', '.join(gap['criterion_ids'])}",
                    f"- 완료 기준: {_md('; '.join(gap['definition_of_done']))}",
                    "",
                ]
            )
    lines.extend(
        [
            "## 13.15 Final 15 Questions",
            "",
            "| 번호 | 답 | 근거 | 설명 |",
            "| ---: | --- | --- | --- |",
        ]
    )
    for question in result["final_questions"]:
        lines.append(
            f"| {question['number']} | {question['answer']} | "
            f"{_md('; '.join(question['evidence']))} | "
            f"{_md(question['explanation'])} |"
        )
    blockers = [
        f"실패 fatal gate: {', '.join(evaluation.fatal_failures)}"
        if evaluation.fatal_failures
        else "",
        f"미검증 fatal gate: {', '.join(evaluation.fatal_unverified)}"
        if evaluation.fatal_unverified
        else "",
        f"execution receipt={receipt['status']}"
        if not receipt["clean_local_run"]
        else "",
        "execution_authenticity_unverified: local receipt is self-attested"
        if receipt["clean_local_run"] and not receipt["trusted"]
        else "",
        f"VERIFIED={verified_count}/{len(criteria)}"
        if verified_count != len(criteria)
        else "",
        f"Critical M4+={evaluation.critical_m4_or_higher}/{evaluation.critical_count}"
        if evaluation.critical_m4_or_higher != evaluation.critical_count
        else "",
        f"score={evaluation.score:.4f}<95" if evaluation.score < 95 else "",
    ]
    blockers = [item for item in blockers if item]
    lines.extend(
        [
            "",
            "## 13.16 Final Conclusion",
            "",
            f"결론: {'YES' if evaluation.complete else 'NO'}",
            "",
            "핵심 이유:",
            "",
            f"1. canonical identity는 A–J 184개 기준과 source surface `{matrix['assessment']['assessment_surface']['sha256']}`에 결속된다.",
            f"2. 현재 계산 점수는 {evaluation.score:.4f}/100이며 판정은 {evaluation.verdict}이다.",
            f"3. 현재 execution receipt 상태는 {receipt['status']}이다.",
            f"4. receipt trust는 {receipt['trust_level']}이며 authenticated execution attestation은 {receipt['trusted']}이다. fatal failures는 {', '.join(evaluation.fatal_failures) or '없음'}, unverified gates는 {', '.join(evaluation.fatal_unverified) or '없음'}이다.",
            f"5. authenticated VERIFIED 기준은 {verified_count}/{len(criteria)}, local M4 포함은 {local_verified_count}/{len(criteria)}이며 Critical M4+는 {evaluation.critical_m4_or_higher}/{evaluation.critical_count}이다.",
            "",
            "완전 판정을 막는 조건:",
            "",
        ]
    )
    lines.extend(f"- {_md(item)}" for item in blockers)
    lines.extend(
        [
            "",
            "완전 판정을 받기 위한 최소 필수 수정:",
            "",
            "- FG-06을 포함한 모든 fatal gate를 실제 독립 증거로 PASS 처리",
            "- 현재 source surface와 정확한 test file hash에 결속된 clean-PASS receipt 생성",
            "- 모든 Critical을 M4 이상 및 모든 184개 기준을 VERIFIED로 승격",
            "- raw/capped score 95 이상과 evaluator findings 0을 동시에 달성",
            "",
            "## 기계 판독 가능한 JSON 결과",
            "",
            "```json",
            json.dumps(result, ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _first_json_difference(
    actual: object, expected: object, *, path: str = "$"
) -> str | None:
    if type(actual) is not type(expected):
        return (
            f"{path}:type actual={type(actual).__name__} "
            f"expected={type(expected).__name__}"
        )
    if isinstance(actual, dict) and isinstance(expected, dict):
        for key in sorted(set(actual) | set(expected)):
            child = f"{path}.{key}"
            if key not in actual:
                return f"{child}:missing"
            if key not in expected:
                return f"{child}:unexpected"
            difference = _first_json_difference(actual[key], expected[key], path=child)
            if difference:
                return difference
        return None
    if isinstance(actual, list) and isinstance(expected, list):
        if len(actual) != len(expected):
            return f"{path}:length actual={len(actual)} expected={len(expected)}"
        for index, (actual_item, expected_item) in enumerate(zip(actual, expected)):
            difference = _first_json_difference(
                actual_item, expected_item, path=f"{path}[{index}]"
            )
            if difference:
                return difference
        return None
    if actual != expected:
        return f"{path}:actual={repr(actual)[:120]} expected={repr(expected)[:120]}"
    return None


def _first_text_difference(actual: str, expected: str) -> str | None:
    actual_lines = actual.splitlines()
    expected_lines = expected.splitlines()
    for number, (actual_line, expected_line) in enumerate(
        zip(actual_lines, expected_lines), start=1
    ):
        if actual_line != expected_line:
            return (
                f"line={number}:actual={actual_line[:120]!r} "
                f"expected={expected_line[:120]!r}"
            )
    if len(actual_lines) != len(expected_lines):
        return f"line_count actual={len(actual_lines)} expected={len(expected_lines)}"
    return None


def _atomic_write(path: Path, text: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    matrix = load_matrix(DEFAULT_MATRIX)
    evaluation = evaluate_matrix(DEFAULT_MATRIX)
    if evaluation.findings:
        for finding in evaluation.findings:
            print(f"INVALID: {finding}", file=sys.stderr)
        return 2
    result = _machine_result(matrix, evaluation)
    result_text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    report_text = _render_report(matrix, evaluation, result)
    if args.check:
        if not RESULT_PATH.is_file():
            print(
                f"STALE: {RESULT_PATH.relative_to(PROJECT_ROOT)}:missing",
                file=sys.stderr,
            )
            return 1
        try:
            actual_result = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            print(
                f"STALE: {RESULT_PATH.relative_to(PROJECT_ROOT)}:invalid_json:"
                f"{type(error).__name__}",
                file=sys.stderr,
            )
            return 1
        difference = _first_json_difference(actual_result, result)
        if difference:
            print(
                f"STALE: {RESULT_PATH.relative_to(PROJECT_ROOT)}:{difference}",
                file=sys.stderr,
            )
            return 1
        if not REPORT_PATH.is_file():
            print(
                f"STALE: {REPORT_PATH.relative_to(PROJECT_ROOT)}:missing",
                file=sys.stderr,
            )
            return 1
        actual_report = REPORT_PATH.read_text(encoding="utf-8")
        difference = _first_text_difference(actual_report, report_text)
        if difference:
            print(
                f"STALE: {REPORT_PATH.relative_to(PROJECT_ROOT)}:{difference}",
                file=sys.stderr,
            )
            return 1
        print("VALID: canonical A--J audit report and machine result are current")
        return 0
    _atomic_write(RESULT_PATH, result_text)
    _atomic_write(REPORT_PATH, report_text)
    print(
        "WROTE: "
        f"{RESULT_PATH.relative_to(PROJECT_ROOT)}, "
        f"{REPORT_PATH.relative_to(PROJECT_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
