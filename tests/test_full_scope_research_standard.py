from __future__ import annotations

from dataclasses import replace

import pytest

from market_research.research.derivatives.common import InstrumentKind
from market_research.research.research_standard import (
    CompetingHypothesis,
    ExpectedDirection,
    HypothesisRelation,
    HypothesisVersion,
    Mechanism,
    NullHypothesis,
    Observation,
    ResearchQuestion,
    ResearchStandardError,
    ResearchStatus,
    ResearchTransition,
    verify_hypothesis_successor,
)


def _hash(token: str) -> str:
    return "sha256:" + token * 64


def _hypothesis(*, version: int = 1, parent: tuple[str, ...] = ()) -> HypothesisVersion:
    return HypothesisVersion(
        hypothesis_id="hyp_derivative_basis_001",
        version=version,
        relation=(
            HypothesisRelation.ORIGINAL
            if version == 1
            else HypothesisRelation.REVISED_AFTER_FALSIFICATION
        ),
        parent_version_hashes=parent,
        research_question_hash=_hash("1"),
        claim="A persistent observable basis predicts subsequent roll return.",
        expected_direction=ExpectedDirection.NEGATIVE,
        target_ids=("fut_root_kospi200",),
        conditions=("liquid_front_and_next_contracts",),
        outcome_variables=("future_roll_return",),
        prediction_horizon="five_trading_days",
        mechanism=Mechanism(
            mechanism_id="mechanism_inventory_pressure_v1",
            version=1,
            causal_chain=("inventory pressure", "basis", "roll return"),
            assumptions=("quotes are synchronous",),
            observable_implications=("basis widens before negative roll",),
        ),
        null_hypothesis=NullHypothesis(
            null_hypothesis_id="null_basis_no_information",
            statement="Basis has no conditional predictive information.",
            rejection_metric="mean_forward_roll_return",
            rejection_threshold="two_sided_p_below_0.05_and_net_edge_positive",
        ),
        competing_hypotheses=(
            CompetingHypothesis(
                competing_hypothesis_id="competing_liquidity_premium",
                statement="Observed return is compensation for illiquidity.",
                differentiating_predictions=("effect vanishes after spread control",),
            ),
        ),
        confounders=("contract liquidity", "seasonality"),
        falsification_conditions=("cost-adjusted effect is non-positive",),
        required_dataset_kinds=("futures_chain", "spot_reference", "rates"),
        created_by="researcher_001",
        created_at=f"2026-01-0{version}T00:00:00+00:00",
        preregistration_hash=_hash("2"),
    )


def test_complete_observation_question_and_hypothesis_contracts_are_hash_bound() -> (
    None
):
    observation = Observation(
        observation_id="obs_basis_001",
        version=1,
        observed_at="2026-01-01T00:00:00+00:00",
        recorded_at="2026-01-01T01:00:00+00:00",
        target_ids=("fut_root_kospi200",),
        dataset_snapshot_hashes=(_hash("3"),),
        available_information_hash=_hash("4"),
        statement="Front-month basis widened repeatedly before roll.",
        researcher_interpretation="Inventory pressure may explain the pattern.",
        uncertainty="The pattern may be driven by a holiday liquidity regime.",
        attachment_hashes=(_hash("5"),),
        linked_question_ids=("rq_basis_001",),
        linked_hypothesis_ids=("hyp_derivative_basis_001",),
        created_by="researcher_001",
    )
    question = ResearchQuestion(
        research_question_id="rq_basis_001",
        version=1,
        title="Does basis predict roll return?",
        description="Test the mechanism without future chain information.",
        target_market="XKRX",
        target_instrument_types=(InstrumentKind.FUTURE,),
        research_horizon="five_trading_days",
        research_scope="KOSPI200 listed futures",
        created_by="researcher_001",
        created_at="2026-01-01T02:00:00+00:00",
        status=ResearchStatus.STRUCTURED,
        observation_hashes=(observation.content_hash,),
    )
    hypothesis = replace(_hypothesis(), research_question_hash=question.content_hash)

    assert observation.fact_status == "UNVERIFIED_OBSERVATION"
    assert question.target_instrument_types == (InstrumentKind.FUTURE,)
    assert hypothesis.mechanism.content_hash.startswith("sha256:")
    assert hypothesis.content_hash == hypothesis.as_dict()["content_hash"]


def test_hypothesis_versions_are_immutable_and_parent_bound() -> None:
    original = _hypothesis()
    successor = _hypothesis(version=2, parent=(original.content_hash,))
    verify_hypothesis_successor(original, successor)

    with pytest.raises(ResearchStandardError, match="parent_hash_missing"):
        verify_hypothesis_successor(
            original,
            replace(successor, parent_version_hashes=(_hash("9"),)),
        )


def test_lifecycle_requires_evidence_and_rejects_invalid_shortcuts() -> None:
    transition = ResearchTransition(
        subject_id="hyp_derivative_basis_001",
        from_status=ResearchStatus.EXPLORATORY,
        to_status=ResearchStatus.PREREGISTERED,
        evidence_hashes=(_hash("1"), _hash("2"), _hash("3")),
        recorded_at="2026-01-03T00:00:00+00:00",
        actor_id="reviewer_001",
    )
    assert transition.content_hash.startswith("sha256:")

    with pytest.raises(ResearchStandardError, match="transition_not_allowed"):
        ResearchTransition(
            subject_id="hyp_derivative_basis_001",
            from_status=ResearchStatus.IDEA,
            to_status=ResearchStatus.VALIDATED,
            evidence_hashes=(_hash("1"), _hash("2")),
            recorded_at="2026-01-03T00:00:00+00:00",
            actor_id="reviewer_001",
        )

    with pytest.raises(ResearchStandardError, match="requires_dataset_metric"):
        ResearchTransition(
            subject_id="hyp_derivative_basis_001",
            from_status=ResearchStatus.EXPLORATORY,
            to_status=ResearchStatus.PREREGISTERED,
            evidence_hashes=(_hash("1"),),
            recorded_at="2026-01-03T00:00:00+00:00",
            actor_id="reviewer_001",
        )
