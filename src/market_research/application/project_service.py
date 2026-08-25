"""UI-neutral application service for authoritative research projects."""

from __future__ import annotations

from dataclasses import dataclass

from market_research.paths import ResearchPathManager
from market_research.research.research_project import (
    ResearchProject,
    ResearchProjectAuthorizationError,
    ResearchProjectConflictError,
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
    resolve_research_project_object,
    research_project_namespaces,
    revise_research_project,
    search_research_projects,
    transition_research_project,
)
from market_research.research.hypothesis_contract import (
    HypothesisSpec,
    parse_hypothesis_spec,
)
from market_research.research.governance import governance_registry_path
from market_research.research.research_standard import (
    PreregisteredResearchDesign,
    ResearchPhase,
    ResearchPhaseWindow,
)
from market_research.research.study_lifecycle import (
    preregister_study,
    record_study_stage,
    study_preregistration_registry_path,
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
    ResearchStudyLifecycleResult,
    ResearchStudyPreregistrationRequest,
    ResearchStudyStageRequest,
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
            investment_horizon=request.investment_horizon,
            expected_phenomenon=request.expected_phenomenon,
            economic_explanation=request.economic_explanation,
            prior_research_relationship=request.prior_research_relationship,
            required_data=request.required_data,
            expected_challenges=request.expected_challenges,
            similar_research_assessment=request.similar_research_assessment,
            similar_research_refs=tuple(
                _domain_reference(reference)
                for reference in request.similar_research_refs
            ),
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
            investment_horizon=request.investment_horizon,
            expected_phenomenon=request.expected_phenomenon,
            economic_explanation=request.economic_explanation,
            prior_research_relationship=request.prior_research_relationship,
            required_data=request.required_data,
            expected_challenges=request.expected_challenges,
            similar_research_assessment=request.similar_research_assessment,
            similar_research_refs=tuple(
                _domain_reference(reference)
                for reference in request.similar_research_refs
            ),
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
            "research-project-record-study-stage",
            request.actor,
        )
        actor = _required_actor(request.actor)
        mutation = attach_research_project_reference(
            manager=self.paths,
            project_id=request.project_id,
            actor_id=actor.actor_id,
            expected_version=request.expected_version,
            reference=_domain_reference(request.reference),
            event_id=request.event_id,
            recorded_at=request.recorded_at,
            reason=request.reason,
        )
        return _mutation_result(
            capability_id="research-project-attach-reference",
            request_id=request.request_id,
            mutation=mutation,
        )

    def record_study_stage(
        self,
        request: ResearchStudyStageRequest,
    ) -> ResearchStudyLifecycleResult:
        ensure_capability_authorized(
            "research-project-attach-reference",
            request.actor,
        )
        actor = _required_actor(request.actor)
        project, hypothesis = self._project_hypothesis(
            project_id=request.project_id,
            expected_project_version=request.expected_project_version,
            hypothesis_object_id=request.hypothesis_object_id,
            hypothesis_version=request.hypothesis_version,
            actor_id=actor.actor_id,
        )
        transition = record_study_stage(
            manager=self.paths,
            hypothesis=hypothesis,
            to_state=request.to_state,
            actor_id=actor.actor_id,
            recorded_at=request.recorded_at,
            reason=request.reason,
            exploration_evidence_hash=request.exploration_evidence_hash,
        )
        return ResearchStudyLifecycleResult(
            capability_id="research-project-record-study-stage",
            request_id=request.request_id,
            status=ResultStatus.SUCCEEDED,
            exit_code=0,
            content_hash=str(transition["row_hash"]),
            artifacts=(
                ArtifactReference(
                    kind="study_lifecycle_transition",
                    uri=str(governance_registry_path(self.paths).resolve()),
                    content_hash=str(transition["row_hash"]),
                ),
            ),
            project_id=project.project_id,
            hypothesis_id=hypothesis.hypothesis_id,
            hypothesis_version=hypothesis.version,
            state=request.to_state,
            transition_row_hash=str(transition["row_hash"]),
        )

    def preregister_study(
        self,
        request: ResearchStudyPreregistrationRequest,
    ) -> ResearchStudyLifecycleResult:
        ensure_capability_authorized(
            "research-project-preregister-study",
            request.actor,
        )
        actor = _required_actor(request.actor)
        project, hypothesis = self._project_hypothesis(
            project_id=request.project_id,
            expected_project_version=request.expected_project_version,
            hypothesis_object_id=request.hypothesis_object_id,
            hypothesis_version=request.hypothesis_version,
            actor_id=actor.actor_id,
        )
        if (
            hypothesis.pre_registered_at != request.recorded_at
            or hypothesis.actor_id != actor.actor_id
            or hypothesis.registration_evidence_hash is None
        ):
            raise ResearchProjectAuthorizationError(
                "study_preregistration_actor_or_timestamp_mismatch"
            )
        design = PreregisteredResearchDesign(
            registration_id=request.registration_id,
            version=request.registration_version,
            manifest_hash=request.manifest_hash,
            hypothesis_contract_hash=hypothesis.contract_hash(),
            registered_by=actor.actor_id,
            registered_at=request.recorded_at,
            sample_starts_at=request.sample_starts_at,
            sample_ends_at=request.sample_ends_at,
            universe=request.universe,
            exclusion_criteria=request.exclusion_criteria,
            variable_definitions=request.variable_definitions,
            target_variable=request.target_variable,
            portfolio_construction=request.portfolio_construction,
            rebalancing_policy=request.rebalancing_policy,
            primary_metrics=request.primary_metrics,
            cost_assumptions=request.cost_assumptions,
            phase_windows=tuple(
                ResearchPhaseWindow(
                    phase=ResearchPhase(window.phase),
                    starts_at=window.starts_at,
                    ends_at=window.ends_at,
                )
                for window in request.phase_windows
            ),
            rejection_criteria=request.rejection_criteria,
            data_suitability_evidence_hash=(request.data_suitability_evidence_hash),
            signal_definition_hash=request.signal_definition_hash,
            external_registration_evidence_hash=(hypothesis.registration_evidence_hash),
        )
        result = preregister_study(
            manager=self.paths,
            hypothesis=hypothesis,
            design=design,
            reason=request.reason,
        )
        transition = result["transition"]
        preregistration = result["preregistration"]
        return ResearchStudyLifecycleResult(
            capability_id="research-project-preregister-study",
            request_id=request.request_id,
            status=ResultStatus.SUCCEEDED,
            exit_code=0,
            content_hash=design.content_hash,
            artifacts=(
                ArtifactReference(
                    kind="study_preregistration",
                    uri=str(study_preregistration_registry_path(self.paths).resolve()),
                    content_hash=str(preregistration["row_hash"]),
                ),
                ArtifactReference(
                    kind="study_lifecycle_transition",
                    uri=str(governance_registry_path(self.paths).resolve()),
                    content_hash=str(transition["row_hash"]),
                ),
            ),
            project_id=project.project_id,
            hypothesis_id=hypothesis.hypothesis_id,
            hypothesis_version=hypothesis.version,
            state="PREREGISTERED",
            transition_row_hash=str(transition["row_hash"]),
            preregistration_row_hash=str(preregistration["row_hash"]),
            preregistration_hash=design.content_hash,
        )

    def _project_hypothesis(
        self,
        *,
        project_id: str,
        expected_project_version: int,
        hypothesis_object_id: str,
        hypothesis_version: str,
        actor_id: str,
    ) -> tuple[ResearchProject, HypothesisSpec]:
        project = get_research_project(self.paths, project_id)
        if project.version != expected_project_version:
            raise ResearchProjectConflictError("research_project_version_conflict")
        require_research_project_permission(
            project,
            actor_id=actor_id,
            permission=ResearchProjectPermission.LINK_HYPOTHESIS,
        )
        references = [
            reference
            for reference in project.object_refs
            if reference.kind is ResearchProjectObjectKind.HYPOTHESIS
            and reference.object_id == hypothesis_object_id
            and reference.version == hypothesis_version
        ]
        if len(references) != 1:
            raise ResearchProjectAuthorizationError(
                "research_project_hypothesis_reference_missing"
            )
        payload = resolve_research_project_object(
            manager=self.paths,
            reference=references[0],
        )
        hypothesis = parse_hypothesis_spec(dict(payload))
        if (
            hypothesis.hypothesis_id != hypothesis_object_id
            or hypothesis.version != hypothesis_version
        ):
            raise ResearchProjectAuthorizationError(
                "research_project_hypothesis_identity_mismatch"
            )
        return project, hypothesis

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


def _domain_reference(reference: object) -> ResearchProjectObjectRef:
    return ResearchProjectObjectRef(
        project_id=str(getattr(reference, "project_id")),
        kind=ResearchProjectObjectKind(str(getattr(reference, "kind"))),
        object_id=str(getattr(reference, "object_id")),
        version=str(getattr(reference, "version")),
        content_hash=str(getattr(reference, "content_hash")),
        artifact_uri=str(getattr(reference, "artifact_uri")),
        artifact_file_hash=str(getattr(reference, "artifact_file_hash")),
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
