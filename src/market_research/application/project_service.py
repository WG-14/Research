"""UI-neutral application service for authoritative research projects."""

from __future__ import annotations

from dataclasses import dataclass

from market_research.paths import ResearchPathManager
from market_research.research.research_project import (
    ResearchProject,
    ResearchProjectAuthorizationError,
    ResearchProjectMember,
    ResearchProjectMutation,
    ResearchProjectObjectKind,
    ResearchProjectObjectRef,
    ResearchProjectPermission,
    ResearchProjectRole,
    ResearchProjectStatus,
    attach_research_project_reference,
    create_or_verify_research_project,
    get_research_project,
    has_research_project_permission,
    impacted_research_projects,
    replace_research_project_members,
    require_research_project_permission,
    research_project_namespaces,
    revise_research_project,
    search_research_projects,
    transition_research_project,
)

from .authorization import ensure_capability_authorized
from .contracts import (
    ActorContext,
    ArtifactReference,
    ResearchProjectCreateRequest,
    ResearchProjectImpactQueryRequest,
    ResearchProjectMembersRequest,
    ResearchProjectMutationResult,
    ResearchProjectQueryRequest,
    ResearchProjectQueryResult,
    ResearchProjectReferenceRequest,
    ResearchProjectRevisionRequest,
    ResearchProjectSearchRequest,
    ResearchProjectSearchResult,
    ResearchProjectTransitionRequest,
    ResearchProjectWorkspaceRequest,
    ResearchProjectWorkspaceResult,
    ResultStatus,
)


@dataclass(frozen=True, slots=True)
class ResearchProjectApplicationService:
    """Apply global and project-scoped authorization to the project authority."""

    paths: ResearchPathManager

    def create(
        self,
        request: ResearchProjectCreateRequest,
    ) -> ResearchProjectMutationResult:
        ensure_capability_authorized("research-project-create", request.actor)
        actor = _required_actor(request.actor)
        if actor.actor_id != request.owner_id:
            raise ResearchProjectAuthorizationError(
                "research_project_owner_actor_mismatch"
            )
        project = ResearchProject.create(
            project_id=request.project_id,
            title=request.title,
            research_question=request.research_question,
            owner_id=request.owner_id,
            asset_classes=request.asset_classes,
            markets=request.markets,
            members=tuple(
                ResearchProjectMember(
                    actor_id=member.actor_id,
                    role=ResearchProjectRole(member.role),
                )
                for member in request.members
            ),
            recorded_at=request.recorded_at,
            status_reason=request.reason,
        )
        mutation = create_or_verify_research_project(
            manager=self.paths,
            project=project,
            actor_id=actor.actor_id,
            event_id=request.event_id,
            recorded_at=request.recorded_at,
            reason=request.reason,
        )
        namespaces = research_project_namespaces(
            self.paths,
            mutation.project.project_id,
        )
        namespaces.ensure()
        return _mutation_result(
            capability_id="research-project-create",
            request_id=request.request_id,
            mutation=mutation,
            compute_root=str(namespaces.compute_root.resolve()),
            cache_root=str(namespaces.cache_root.resolve()),
        )

    def replace_members(
        self,
        request: ResearchProjectMembersRequest,
    ) -> ResearchProjectMutationResult:
        ensure_capability_authorized(
            "research-project-manage-members",
            request.actor,
        )
        actor = _required_actor(request.actor)
        mutation = replace_research_project_members(
            manager=self.paths,
            project_id=request.project_id,
            actor_id=actor.actor_id,
            expected_version=request.expected_version,
            members=tuple(
                ResearchProjectMember(
                    actor_id=member.actor_id,
                    role=ResearchProjectRole(member.role),
                )
                for member in request.members
            ),
            event_id=request.event_id,
            recorded_at=request.recorded_at,
            reason=request.reason,
        )
        return _mutation_result(
            capability_id="research-project-manage-members",
            request_id=request.request_id,
            mutation=mutation,
        )

    def revise(
        self,
        request: ResearchProjectRevisionRequest,
    ) -> ResearchProjectMutationResult:
        ensure_capability_authorized("research-project-revise", request.actor)
        actor = _required_actor(request.actor)
        mutation = revise_research_project(
            manager=self.paths,
            project_id=request.project_id,
            actor_id=actor.actor_id,
            expected_version=request.expected_version,
            title=request.title,
            research_question=request.research_question,
            asset_classes=request.asset_classes,
            markets=request.markets,
            event_id=request.event_id,
            recorded_at=request.recorded_at,
            reason=request.reason,
        )
        return _mutation_result(
            capability_id="research-project-revise",
            request_id=request.request_id,
            mutation=mutation,
        )

    def attach_reference(
        self,
        request: ResearchProjectReferenceRequest,
    ) -> ResearchProjectMutationResult:
        ensure_capability_authorized(
            "research-project-attach-reference",
            request.actor,
        )
        actor = _required_actor(request.actor)
        mutation = attach_research_project_reference(
            manager=self.paths,
            project_id=request.project_id,
            actor_id=actor.actor_id,
            expected_version=request.expected_version,
            reference=ResearchProjectObjectRef(
                project_id=request.reference.project_id,
                kind=ResearchProjectObjectKind(request.reference.kind),
                object_id=request.reference.object_id,
                version=request.reference.version,
                content_hash=request.reference.content_hash,
            ),
            event_id=request.event_id,
            recorded_at=request.recorded_at,
            reason=request.reason,
        )
        return _mutation_result(
            capability_id="research-project-attach-reference",
            request_id=request.request_id,
            mutation=mutation,
        )

    def transition(
        self,
        request: ResearchProjectTransitionRequest,
    ) -> ResearchProjectMutationResult:
        ensure_capability_authorized(
            "research-project-transition",
            request.actor,
        )
        actor = _required_actor(request.actor)
        mutation = transition_research_project(
            manager=self.paths,
            project_id=request.project_id,
            actor_id=actor.actor_id,
            expected_version=request.expected_version,
            to_status=ResearchProjectStatus(request.to_status),
            event_id=request.event_id,
            recorded_at=request.recorded_at,
            reason=request.reason,
            superseded_by_project_id=request.superseded_by_project_id,
        )
        return _mutation_result(
            capability_id="research-project-transition",
            request_id=request.request_id,
            mutation=mutation,
        )

    def get(
        self,
        request: ResearchProjectQueryRequest,
    ) -> ResearchProjectQueryResult:
        ensure_capability_authorized("research-project-get", request.actor)
        actor = _required_actor(request.actor)
        project = get_research_project(self.paths, request.project_id)
        require_research_project_permission(
            project,
            actor_id=actor.actor_id,
            permission=ResearchProjectPermission.VIEW,
        )
        return ResearchProjectQueryResult(
            capability_id="research-project-get",
            request_id=request.request_id,
            status=ResultStatus.SUCCEEDED,
            exit_code=0,
            content_hash=project.content_hash,
            project=project.as_dict(),
        )

    def search(
        self,
        request: ResearchProjectSearchRequest,
    ) -> ResearchProjectSearchResult:
        ensure_capability_authorized("research-project-search", request.actor)
        actor = _required_actor(request.actor)
        candidates = search_research_projects(
            self.paths,
            query=request.query,
            statuses=(
                None
                if not request.statuses
                else frozenset(
                    ResearchProjectStatus(status) for status in request.statuses
                )
            ),
            asset_classes=request.asset_classes,
            markets=request.markets,
            include_archived=request.include_archived,
        )
        projects = tuple(
            project
            for project in candidates
            if has_research_project_permission(
                project,
                actor_id=actor.actor_id,
                permission=ResearchProjectPermission.VIEW,
            )
        )
        return ResearchProjectSearchResult(
            capability_id="research-project-search",
            request_id=request.request_id,
            status=ResultStatus.SUCCEEDED,
            exit_code=0,
            projects=tuple(project.as_dict() for project in projects),
            total=len(projects),
        )

    def impacted_projects(
        self,
        request: ResearchProjectImpactQueryRequest,
    ) -> ResearchProjectSearchResult:
        ensure_capability_authorized(
            "research-project-impacted-objects",
            request.actor,
        )
        actor = _required_actor(request.actor)
        candidates = impacted_research_projects(
            self.paths,
            kind=ResearchProjectObjectKind(request.kind),
            object_id=request.object_id,
            version=request.version,
            content_hash=request.content_hash,
            include_archived=request.include_archived,
        )
        projects = tuple(
            project
            for project in candidates
            if has_research_project_permission(
                project,
                actor_id=actor.actor_id,
                permission=ResearchProjectPermission.VIEW,
            )
        )
        return ResearchProjectSearchResult(
            capability_id="research-project-impacted-objects",
            request_id=request.request_id,
            status=ResultStatus.SUCCEEDED,
            exit_code=0,
            projects=tuple(project.as_dict() for project in projects),
            total=len(projects),
        )

    def workspace(
        self,
        request: ResearchProjectWorkspaceRequest,
    ) -> ResearchProjectWorkspaceResult:
        ensure_capability_authorized(
            "research-project-workspace",
            request.actor,
        )
        actor = _required_actor(request.actor)
        project = get_research_project(self.paths, request.project_id)
        require_research_project_permission(
            project,
            actor_id=actor.actor_id,
            permission=ResearchProjectPermission.USE_COMPUTE,
        )
        namespaces = research_project_namespaces(self.paths, project.project_id)
        namespaces.ensure()
        return ResearchProjectWorkspaceResult(
            capability_id="research-project-workspace",
            request_id=request.request_id,
            status=ResultStatus.SUCCEEDED,
            exit_code=0,
            content_hash=project.content_hash,
            project_id=project.project_id,
            project_version=project.version,
            compute_root=str(namespaces.compute_root.resolve()),
            cache_root=str(namespaces.cache_root.resolve()),
        )


def _required_actor(actor: ActorContext | None) -> ActorContext:
    if actor is None:  # Authorization always rejects this first.
        raise ResearchProjectAuthorizationError("research_project_actor_required")
    return actor


def _mutation_result(
    *,
    capability_id: str,
    request_id: str | None,
    mutation: ResearchProjectMutation,
    compute_root: str | None = None,
    cache_root: str | None = None,
) -> ResearchProjectMutationResult:
    row_hash = str(mutation.registry_row["row_hash"])
    return ResearchProjectMutationResult(
        capability_id=capability_id,
        request_id=request_id,
        status=ResultStatus.SUCCEEDED,
        exit_code=0,
        content_hash=mutation.project.content_hash,
        artifacts=(
            ArtifactReference(
                kind="research_project_registry_event",
                uri=str(mutation.registry_path),
                content_hash=row_hash,
            ),
        ),
        project=mutation.project.as_dict(),
        registry_path=str(mutation.registry_path),
        registry_row_hash=row_hash,
        event_created=mutation.created,
        compute_root=compute_root,
        cache_root=cache_root,
    )


__all__ = ["ResearchProjectApplicationService"]
