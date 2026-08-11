from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast

import pytest
from pydantic import ValidationError

from market_research.application import (
    ActorContext,
    ResearchProjectApplicationService,
    ResearchProjectCreateRequest,
    ResearchProjectImpactQueryRequest,
    ResearchProjectMemberInput,
    ResearchProjectMembersRequest,
    ResearchProjectObjectRefInput,
    ResearchProjectQueryRequest,
    ResearchProjectReferenceRequest,
    ResearchProjectRevisionRequest,
    ResearchProjectSearchRequest,
    ResearchProjectTransitionRequest,
    ResearchProjectWorkspaceRequest,
    get_capability,
)
from market_research.paths import ResearchPathError, ResearchPathManager
from market_research.research.research_project import (
    ResearchProject,
    ResearchProjectAuthorizationError,
    ResearchProjectConflictError,
    ResearchProjectIntegrityError,
    ResearchProjectNotFoundError,
    ResearchProjectObjectKind,
    ResearchProjectObjectRef,
    attach_research_project_reference,
    get_research_project,
    research_project_history,
    research_project_namespaces,
    research_project_registry_path,
    validate_research_project_registry,
)
from market_research.settings import ResearchSettings
from market_research.storage_io import ATOMIC_PUBLICATION_MODE_ENV


_BASE_TIME = datetime(2026, 7, 29, 0, 0, tzinfo=timezone.utc)
_ProjectStatusInput: TypeAlias = Literal[
    "ACTIVE",
    "CHALLENGED",
    "SUPERSEDED",
    "DEPRECATED",
    "REJECTED",
    "ARCHIVED",
]
_ProjectObjectKindInput: TypeAlias = Literal[
    "HYPOTHESIS",
    "DATASET",
    "CODE",
    "EXPERIMENT",
    "RESULT",
    "VERIFICATION",
    "REVIEW",
    "PACKAGE",
]


def _time(index: int) -> str:
    return (_BASE_TIME + timedelta(minutes=index)).isoformat()


def _paths(tmp_path: Path, *, project_root: Path | None = None) -> ResearchPathManager:
    return ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "data",
            artifact_root=tmp_path / "artifacts",
            report_root=tmp_path / "reports",
            cache_root=tmp_path / "cache",
            db_path=None,
            max_workers=1,
            random_seed=0,
        ),
        project_root=project_root or Path.cwd(),
    )


def _actor(actor_id: str, permission: str) -> ActorContext:
    return ActorContext(
        actor_id=actor_id,
        permissions=frozenset({permission}),
        source="web",
    )


def _members(prefix: str = "a") -> tuple[ResearchProjectMemberInput, ...]:
    return (
        ResearchProjectMemberInput(actor_id=f"owner-{prefix}", role="OWNER"),
        ResearchProjectMemberInput(
            actor_id=f"researcher-{prefix}",
            role="RESEARCHER",
        ),
        ResearchProjectMemberInput(
            actor_id=f"steward-{prefix}",
            role="DATA_STEWARD",
        ),
        ResearchProjectMemberInput(
            actor_id=f"validator-{prefix}",
            role="VALIDATOR",
        ),
        ResearchProjectMemberInput(
            actor_id=f"reviewer-{prefix}",
            role="REVIEWER",
        ),
        ResearchProjectMemberInput(
            actor_id=f"publisher-{prefix}",
            role="PUBLISHER",
        ),
        ResearchProjectMemberInput(actor_id=f"viewer-{prefix}", role="VIEWER"),
    )


def _create_request(
    project_id: str,
    *,
    prefix: str = "a",
    event_id: str | None = None,
    recorded_at: str = _time(0),
    title: str = "Cross-sectional liquidity research",
) -> ResearchProjectCreateRequest:
    return ResearchProjectCreateRequest(
        project_id=project_id,
        event_id=event_id or f"create-{project_id}",
        recorded_at=recorded_at,
        reason="Create the governed research workspace",
        title=title,
        research_question="Does a liquidity signal survive causal validation?",
        owner_id=f"owner-{prefix}",
        asset_classes=("EQUITY", "FUTURE"),
        markets=("KRX", "NYSE"),
        members=_members(prefix),
        actor=_actor(f"owner-{prefix}", "research.project.manage"),
    )


def _create(
    service: ResearchProjectApplicationService,
    project_id: str,
    *,
    prefix: str = "a",
    recorded_at: str = _time(0),
) -> dict[str, Any]:
    return service.create(
        _create_request(
            project_id,
            prefix=prefix,
            recorded_at=recorded_at,
        )
    ).project


def _transition(
    service: ResearchProjectApplicationService,
    *,
    project_id: str,
    prefix: str,
    actor_role: str,
    version: int,
    status: _ProjectStatusInput,
    index: int,
    superseded_by: str | None = None,
) -> dict[str, Any]:
    actor_id = f"{actor_role}-{prefix}"
    result = service.transition(
        ResearchProjectTransitionRequest(
            project_id=project_id,
            event_id=f"{project_id}-{status.lower()}-{index}",
            recorded_at=_time(index),
            reason=f"Move {project_id} to {status}",
            expected_version=version,
            to_status=status,
            superseded_by_project_id=superseded_by,
            actor=_actor(actor_id, "research.project.manage"),
        )
    )
    return result.project


def _version(project: dict[str, Any]) -> int:
    value = project["version"]
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def test_project_schema_registry_idempotence_tamper_and_external_namespaces(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    service = ResearchProjectApplicationService(paths)
    request = _create_request("project-liquidity")

    first = service.create(request)
    repeated = service.create(request)

    assert first.event_created is True
    assert repeated.event_created is False
    assert repeated.registry_row_hash == first.registry_row_hash
    assert first.project["schema_version"] == 1
    assert first.project["version"] == 1
    assert first.project["status"] == "DRAFT"
    assert first.compute_root is not None
    assert first.cache_root is not None
    compute_root = Path(first.compute_root)
    cache_root = Path(first.cache_root)
    assert compute_root.is_dir()
    assert cache_root.is_dir()
    assert not paths.is_within(compute_root, paths.project_root)
    assert not paths.is_within(cache_root, paths.project_root)
    registry = research_project_registry_path(paths)
    assert len(registry.read_text(encoding="utf-8").splitlines()) == 1
    assert validate_research_project_registry(paths)["status"] == "PASS"

    project = ResearchProject.from_dict(first.project)
    assert project.content_hash == first.content_hash
    unknown = dict(first.project)
    unknown["unknown"] = True
    with pytest.raises(ValueError, match="research_project_fields_invalid"):
        ResearchProject.from_dict(unknown)
    changed_hash = dict(first.project)
    changed_hash["title"] = "Changed without rehashing"
    with pytest.raises(
        ResearchProjectIntegrityError,
        match="research_project_content_hash_mismatch",
    ):
        ResearchProject.from_dict(changed_hash)

    with pytest.raises(
        ResearchProjectConflictError,
        match="research_project_event_id_conflict",
    ):
        service.create(
            _create_request(
                "project-liquidity",
                title="Conflicting replay",
            )
        )
    with pytest.raises(
        ResearchProjectConflictError,
        match="research_project_duplicate",
    ):
        service.create(
            _create_request(
                "project-liquidity",
                event_id="create-project-liquidity-again",
                recorded_at=_time(1),
            )
        )

    view = service.get(
        ResearchProjectQueryRequest(
            project_id="project-liquidity",
            actor=_actor("viewer-a", "research.project.view"),
        )
    )
    assert view.project["content_hash"] == first.content_hash
    assert (
        get_capability("research-project-create").service_id
        == "ResearchProjectApplicationService.create"
    )

    rows = [
        json.loads(line) for line in registry.read_text(encoding="utf-8").splitlines()
    ]
    rows[0]["project"]["title"] = "tampered"
    registry.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    validation = validate_research_project_registry(paths)
    assert validation["status"] == "FAIL"
    with pytest.raises(ResearchProjectIntegrityError):
        get_research_project(paths, "project-liquidity")


def test_shared_project_namespaces_are_setgid_and_group_writable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    for root in (paths.settings.artifact_root, paths.settings.cache_root):
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o2770)
    monkeypatch.setenv(ATOMIC_PUBLICATION_MODE_ENV, "0640")

    created = ResearchProjectApplicationService(paths).create(
        _create_request("project-shared")
    )

    assert created.compute_root is not None
    assert created.cache_root is not None
    for namespace in (Path(created.compute_root), Path(created.cache_root)):
        assert stat.S_IMODE(namespace.stat().st_mode) == 0o2770
    published = [path for path in paths.artifact_root.rglob("*") if path.is_file()]
    assert published
    for path in published:
        expected_mode = 0o660 if path.suffix == ".lock" else 0o640
        assert stat.S_IMODE(path.stat().st_mode) == expected_mode
        assert path.stat().st_gid == path.parent.stat().st_gid


def test_project_paths_reject_symlink_escape_from_configured_roots(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    paths.artifact_root.mkdir()
    (paths.artifact_root / "derived").symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(ResearchPathError, match="symlink_component_forbidden"):
        research_project_namespaces(paths, "project-escape")


def test_project_registry_rejects_symlink_escape_from_artifact_root(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    paths.artifact_root.mkdir()
    (paths.artifact_root / "reports").symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(ResearchPathError, match="symlink_component_forbidden"):
        research_project_registry_path(paths)


def test_project_namespace_rejects_symlink_alias_within_artifact_root(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    paths.artifact_root.mkdir()
    aliased = paths.artifact_root / "aliased"
    aliased.mkdir()
    (paths.artifact_root / "derived").symlink_to(
        aliased,
        target_is_directory=True,
    )

    with pytest.raises(ResearchPathError, match="symlink_component_forbidden"):
        research_project_namespaces(paths, "project-alias")


def test_role_scoped_lineage_search_reverse_impact_and_cross_project_denial(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    service = ResearchProjectApplicationService(paths)
    project = _create(service, "project-a", prefix="a")
    _create(service, "project-b", prefix="b", recorded_at=_time(1))
    version = _version(project)

    links: tuple[tuple[_ProjectObjectKindInput, str, str], ...] = (
        ("HYPOTHESIS", "hypothesis-a", "researcher-a"),
        ("DATASET", "dataset-a", "steward-a"),
        ("CODE", "code-a", "researcher-a"),
        ("EXPERIMENT", "experiment-a", "researcher-a"),
        ("RESULT", "result-a", "researcher-a"),
        ("VERIFICATION", "verification-a", "validator-a"),
        ("REVIEW", "review-a", "reviewer-a"),
        ("PACKAGE", "package-a", "publisher-a"),
    )
    latest: dict[str, Any] = project
    for index, (kind, object_id, actor_id) in enumerate(links, start=2):
        result = service.attach_reference(
            ResearchProjectReferenceRequest(
                project_id="project-a",
                event_id=f"attach-{object_id}",
                recorded_at=_time(index),
                reason=f"Bind immutable {kind.lower()} evidence",
                expected_version=version,
                reference=ResearchProjectObjectRefInput(
                    project_id="project-a",
                    kind=kind,
                    object_id=object_id,
                    version="v1",
                    content_hash="sha256:" + f"{index:064x}",
                ),
                actor=_actor(actor_id, "research.project.write"),
            )
        )
        latest = result.project
        version = _version(latest)

    assert version == 9
    object_refs = cast(list[dict[str, Any]], latest["object_refs"])
    assert {ref["kind"] for ref in object_refs} == {
        kind for kind, _object_id, _actor_id in links
    }
    assert len(research_project_history(paths, "project-a")) == 9

    impact = service.impacted_projects(
        ResearchProjectImpactQueryRequest(
            kind="DATASET",
            object_id="dataset-a",
            version="v1",
            actor=_actor("viewer-a", "research.project.view"),
        )
    )
    assert impact.total == 1
    assert impact.projects[0]["project_id"] == "project-a"
    hidden = service.impacted_projects(
        ResearchProjectImpactQueryRequest(
            kind="DATASET",
            object_id="dataset-a",
            actor=_actor("owner-b", "research.project.view"),
        )
    )
    assert hidden.total == 0
    search = service.search(
        ResearchProjectSearchRequest(
            query="liquidity",
            asset_classes=frozenset({"EQUITY"}),
            actor=_actor("viewer-a", "research.project.view"),
        )
    )
    assert search.total == 1
    assert search.projects[0]["project_id"] == "project-a"

    with pytest.raises(
        ResearchProjectAuthorizationError,
        match="project.link.verification",
    ):
        service.attach_reference(
            ResearchProjectReferenceRequest(
                project_id="project-a",
                event_id="researcher-cannot-verify",
                recorded_at=_time(20),
                reason="Invalid role escalation",
                expected_version=version,
                reference=ResearchProjectObjectRefInput(
                    project_id="project-a",
                    kind="VERIFICATION",
                    object_id="verification-by-researcher",
                    version="v1",
                    content_hash="sha256:" + "a" * 64,
                ),
                actor=_actor("researcher-a", "research.project.write"),
            )
        )

    with pytest.raises(ResearchProjectAuthorizationError):
        service.attach_reference(
            ResearchProjectReferenceRequest(
                project_id="project-b",
                event_id="cross-project-actor",
                recorded_at=_time(21),
                reason="Actor belongs only to another project",
                expected_version=1,
                reference=ResearchProjectObjectRefInput(
                    project_id="project-b",
                    kind="CODE",
                    object_id="code-cross-project",
                    version="v1",
                    content_hash="sha256:" + "b" * 64,
                ),
                actor=_actor("researcher-a", "research.project.write"),
            )
        )

    with pytest.raises(
        ValidationError,
        match="research_project_cross_project_ref_forbidden",
    ):
        ResearchProjectReferenceRequest(
            project_id="project-b",
            event_id="cross-project-ref",
            recorded_at=_time(22),
            reason="Reference scope mismatch",
            expected_version=1,
            reference=ResearchProjectObjectRefInput(
                project_id="project-a",
                kind="CODE",
                object_id="code-cross-project",
                version="v1",
                content_hash="sha256:" + "c" * 64,
            ),
            actor=_actor("researcher-b", "research.project.write"),
        )

    exact_ref = ResearchProjectObjectRef(
        project_id="project-a",
        kind=ResearchProjectObjectKind.DATASET,
        object_id="dataset-a",
        version="v1",
        content_hash="sha256:" + f"{3:064x}",
    )
    with pytest.raises(
        ResearchProjectConflictError,
        match="research_project_reference_duplicate",
    ):
        attach_research_project_reference(
            manager=paths,
            project_id="project-a",
            actor_id="steward-a",
            expected_version=version,
            reference=exact_ref,
            event_id="duplicate-dataset-ref",
            recorded_at=_time(23),
            reason="Duplicate reference must fail",
        )

    researcher_workspace = service.workspace(
        ResearchProjectWorkspaceRequest(
            project_id="project-a",
            actor=_actor("researcher-a", "research.project.compute"),
        )
    )
    owner_b_workspace = service.workspace(
        ResearchProjectWorkspaceRequest(
            project_id="project-b",
            actor=_actor("owner-b", "research.project.compute"),
        )
    )
    assert researcher_workspace.compute_root != owner_b_workspace.compute_root
    assert researcher_workspace.cache_root != owner_b_workspace.cache_root
    with pytest.raises(
        ResearchProjectAuthorizationError,
        match="project.compute.use",
    ):
        service.workspace(
            ResearchProjectWorkspaceRequest(
                project_id="project-a",
                actor=_actor("viewer-a", "research.project.compute"),
            )
        )


def test_project_revision_membership_version_and_read_acl(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    service = ResearchProjectApplicationService(paths)
    _create(service, "project-versioned", prefix="v")

    revised = service.revise(
        ResearchProjectRevisionRequest(
            project_id="project-versioned",
            event_id="revise-project-versioned",
            recorded_at=_time(1),
            reason="Narrow the preregistered market scope",
            expected_version=1,
            title="Revised liquidity research",
            research_question="Does the signal survive in liquid KRX equities?",
            asset_classes=("EQUITY",),
            markets=("KRX",),
            actor=_actor("owner-v", "research.project.manage"),
        )
    )
    assert revised.project["version"] == 2
    assert revised.project["markets"] == ["KRX"]

    with pytest.raises(
        ResearchProjectAuthorizationError,
        match="project.members.manage",
    ):
        service.replace_members(
            ResearchProjectMembersRequest(
                project_id="project-versioned",
                event_id="researcher-replaces-members",
                recorded_at=_time(2),
                reason="Unauthorized membership mutation",
                expected_version=2,
                members=_members("v"),
                actor=_actor("researcher-v", "research.project.manage"),
            )
        )

    replacement_members = (
        *tuple(member for member in _members("v") if member.role != "VIEWER"),
        ResearchProjectMemberInput(actor_id="viewer-v2", role="VIEWER"),
    )
    members_result = service.replace_members(
        ResearchProjectMembersRequest(
            project_id="project-versioned",
            event_id="replace-project-versioned-members",
            recorded_at=_time(3),
            reason="Rotate the read-only project member",
            expected_version=2,
            members=replacement_members,
            actor=_actor("owner-v", "research.project.manage"),
        )
    )
    assert members_result.project["version"] == 3

    with pytest.raises(
        ResearchProjectAuthorizationError,
        match="project.view",
    ):
        service.get(
            ResearchProjectQueryRequest(
                project_id="project-versioned",
                actor=_actor("viewer-v", "research.project.view"),
            )
        )
    current = service.get(
        ResearchProjectQueryRequest(
            project_id="project-versioned",
            actor=_actor("viewer-v2", "research.project.view"),
        )
    )
    assert current.project["version"] == 3

    with pytest.raises(
        ResearchProjectConflictError,
        match="research_project_version_conflict",
    ):
        service.revise(
            ResearchProjectRevisionRequest(
                project_id="project-versioned",
                event_id="stale-project-revision",
                recorded_at=_time(4),
                reason="Stale revision must fail",
                expected_version=1,
                title="Stale title",
                research_question="Stale question",
                asset_classes=("EQUITY",),
                markets=("KRX",),
                actor=_actor("owner-v", "research.project.manage"),
            )
        )

    invalid_members = tuple(
        member for member in replacement_members if member.role != "OWNER"
    )
    with pytest.raises(ValueError, match="owner_membership_invalid"):
        service.replace_members(
            ResearchProjectMembersRequest(
                project_id="project-versioned",
                event_id="remove-project-owner",
                recorded_at=_time(5),
                reason="Owner removal must fail",
                expected_version=3,
                members=invalid_members,
                actor=_actor("owner-v", "research.project.manage"),
            )
        )


def test_project_lifecycle_covers_challenge_supersede_deprecate_reject_archive(
    tmp_path: Path,
) -> None:
    service = ResearchProjectApplicationService(_paths(tmp_path))

    _create(service, "project-challenged", prefix="c")
    current = _transition(
        service,
        project_id="project-challenged",
        prefix="c",
        actor_role="owner",
        version=1,
        status="ACTIVE",
        index=1,
    )
    with pytest.raises(
        ResearchProjectAuthorizationError,
        match="project.transition",
    ):
        _transition(
            service,
            project_id="project-challenged",
            prefix="c",
            actor_role="researcher",
            version=_version(current),
            status="CHALLENGED",
            index=2,
        )
    current = _transition(
        service,
        project_id="project-challenged",
        prefix="c",
        actor_role="reviewer",
        version=_version(current),
        status="CHALLENGED",
        index=3,
    )
    current = _transition(
        service,
        project_id="project-challenged",
        prefix="c",
        actor_role="owner",
        version=_version(current),
        status="ACTIVE",
        index=4,
    )
    current = _transition(
        service,
        project_id="project-challenged",
        prefix="c",
        actor_role="reviewer",
        version=_version(current),
        status="DEPRECATED",
        index=5,
    )
    with pytest.raises(ValueError, match="status_transition_forbidden"):
        _transition(
            service,
            project_id="project-challenged",
            prefix="c",
            actor_role="owner",
            version=_version(current),
            status="ACTIVE",
            index=6,
        )
    archived = _transition(
        service,
        project_id="project-challenged",
        prefix="c",
        actor_role="owner",
        version=_version(current),
        status="ARCHIVED",
        index=7,
    )
    assert archived["status"] == "ARCHIVED"

    _create(
        service,
        "project-successor",
        prefix="s",
        recorded_at=_time(8),
    )
    _create(
        service,
        "project-superseded",
        prefix="u",
        recorded_at=_time(9),
    )
    superseded = _transition(
        service,
        project_id="project-superseded",
        prefix="u",
        actor_role="owner",
        version=1,
        status="ACTIVE",
        index=10,
    )
    with pytest.raises(
        ResearchProjectNotFoundError,
        match="research_project_superseded_target_not_found",
    ):
        _transition(
            service,
            project_id="project-superseded",
            prefix="u",
            actor_role="owner",
            version=_version(superseded),
            status="SUPERSEDED",
            index=11,
            superseded_by="project-missing-successor",
        )
    superseded = _transition(
        service,
        project_id="project-superseded",
        prefix="u",
        actor_role="owner",
        version=_version(superseded),
        status="SUPERSEDED",
        index=11,
        superseded_by="project-successor",
    )
    assert superseded["superseded_by_project_id"] == "project-successor"
    superseded = _transition(
        service,
        project_id="project-superseded",
        prefix="u",
        actor_role="owner",
        version=_version(superseded),
        status="ARCHIVED",
        index=12,
    )
    assert superseded["status"] == "ARCHIVED"

    _create(
        service,
        "project-rejected",
        prefix="r",
        recorded_at=_time(13),
    )
    rejected = _transition(
        service,
        project_id="project-rejected",
        prefix="r",
        actor_role="reviewer",
        version=1,
        status="REJECTED",
        index=14,
    )
    assert rejected["status"] == "REJECTED"
    rejected = _transition(
        service,
        project_id="project-rejected",
        prefix="r",
        actor_role="owner",
        version=_version(rejected),
        status="ARCHIVED",
        index=15,
    )
    assert rejected["status"] == "ARCHIVED"


def test_project_registry_rejects_repository_local_runtime_state(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = _paths(repository_root, project_root=repository_root)
    service = ResearchProjectApplicationService(paths)

    with pytest.raises(
        ResearchPathError,
        match="research_project_registry_must_be_repository_external",
    ):
        service.create(_create_request("project-local"))

    assert not (repository_root / "artifacts").exists()
