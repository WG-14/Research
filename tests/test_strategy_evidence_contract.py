from __future__ import annotations

import pytest

from bithumb_bot.research.strategy_registry import (
    ResearchStrategyPlugin,
    StrategyRuntimeCapabilities,
)
from bithumb_bot.research.strategy_spec import StrategySpec
from bithumb_bot.strategy_decision_service import StrategyDecisionService, StrategyEvaluationRequest
from bithumb_bot.strategy_evidence_contract import DecisionEvidenceContract
from bithumb_bot.strategy_policy_contract import (
    ExecutionConstraintSnapshot,
    PositionSnapshot,
    StrategyDecisionV2,
)


HASH = "sha256:" + "a" * 64


class _ContractPolicy:
    name = "contract_canary"

    def decide_snapshot(self, **_kwargs: object) -> StrategyDecisionV2:
        position = PositionSnapshot(in_position=False, entry_allowed=True, exit_allowed=False)
        return StrategyDecisionV2(
            strategy_name=self.name,
            raw_signal="HOLD",
            raw_reason="unit",
            entry_signal="HOLD",
            entry_reason="unit",
            exit_signal="HOLD",
            exit_reason="unit",
            final_signal="HOLD",
            final_reason="unit",
            blocked_filters=(),
            entry_blocked=False,
            entry_block_reason=None,
            exit_rule=None,
            exit_evaluations=(),
            protective_exit_overrode_entry=False,
            exit_filter_suppression_prevented=False,
            position_snapshot=position,
            execution_intent=None,
            entry_decision=None,
            trace={},
            policy_hash=HASH,
            policy_contract_hash=HASH,
            policy_input_hash=HASH,
            policy_decision_hash=HASH,
        )


def test_decision_evidence_contract_one_of_groups_are_hash_bound_and_normalized() -> None:
    contract = DecisionEvidenceContract(
        required_live_real_order_one_of_field_groups=(
            ("z_payload_hash", "z_hash"),
            ("a_hash", "a_payload_hash"),
        ),
    )

    assert contract.required_live_real_order_one_of_field_groups == (
        ("a_hash", "a_payload_hash"),
        ("z_hash", "z_payload_hash"),
    )
    assert contract.payload_without_hash()["required_live_real_order_one_of_field_groups"] == [
        ["a_hash", "a_payload_hash"],
        ["z_hash", "z_payload_hash"],
    ]
    assert contract.as_dict()["contract_hash"] == contract.contract_hash()
    assert "required_live_real_order_one_of_field_groups" in contract.as_dict()


@pytest.mark.parametrize(
    "groups",
    [
        ((),),
        (("only_one_field",),),
        (("duplicate", "duplicate"),),
        (("valid", ""),),
    ],
)
def test_decision_evidence_contract_rejects_invalid_one_of_groups(
    groups: tuple[tuple[str, ...], ...],
) -> None:
    with pytest.raises(ValueError, match="decision_evidence_required"):
        DecisionEvidenceContract(required_live_real_order_one_of_field_groups=groups)


def test_live_real_order_one_of_group_is_enforced_by_contract() -> None:
    request = _live_real_order_request(
        contract=DecisionEvidenceContract(
            required_live_real_order_fields=("decision_input_bundle_hash",),
            required_live_real_order_one_of_field_groups=(
                ("fee_authority_hash", "fee_authority_payload_hash"),
            ),
        ),
        provenance={"decision_input_bundle_hash": HASH},
    )

    with pytest.raises(ValueError) as exc:
        StrategyDecisionService().evaluate(request)

    assert "one_of(fee_authority_hash|fee_authority_payload_hash)" in str(exc.value)


def test_live_real_order_one_of_group_accepts_any_declared_member() -> None:
    request = _live_real_order_request(
        contract=DecisionEvidenceContract(
            required_live_real_order_fields=("decision_input_bundle_hash",),
            required_live_real_order_one_of_field_groups=(
                ("fee_authority_hash", "fee_authority_payload_hash"),
            ),
        ),
        provenance={
            "decision_input_bundle_hash": HASH,
            "fee_authority_payload_hash": HASH,
        },
    )

    result = StrategyDecisionService().evaluate(request)

    assert result.provenance["fee_authority_payload_hash"] == HASH


def test_live_real_order_scalar_contract_fields_still_fail_closed() -> None:
    request = _live_real_order_request(
        contract=DecisionEvidenceContract(
            required_live_real_order_fields=("decision_input_bundle_hash",),
            required_live_real_order_one_of_field_groups=(("custom_hash", "custom_payload_hash"),),
        ),
        provenance={"custom_hash": HASH},
    )

    with pytest.raises(ValueError) as exc:
        StrategyDecisionService().evaluate(request)

    assert "decision_input_bundle_hash" in str(exc.value)
    assert "custom_hash" not in str(exc.value)


def test_non_sma_live_dry_run_contract_missing_promotion_evidence_fails_closed() -> None:
    request = _live_real_order_request(
        contract=DecisionEvidenceContract(
            required_promotion_provenance_fields=("non_sma_contract_hash",),
        ),
        provenance={},
    )
    request = StrategyEvaluationRequest(
        **{
            **request.__dict__,
            "mode": "live_dry_run",
        }
    )

    with pytest.raises(ValueError) as exc:
        StrategyDecisionService().evaluate(request)

    assert "strategy_evaluation_required_provenance_missing:contract_canary" in str(exc.value)
    assert "non_sma_contract_hash" in str(exc.value)


def test_non_sma_live_dry_run_contract_complete_promotion_evidence_passes() -> None:
    request = _live_real_order_request(
        contract=DecisionEvidenceContract(
            required_promotion_provenance_fields=("non_sma_contract_hash",),
        ),
        provenance={"non_sma_contract_hash": HASH},
    )
    request = StrategyEvaluationRequest(
        **{
            **request.__dict__,
            "mode": "live_dry_run",
        }
    )

    result = StrategyDecisionService().evaluate(request)

    assert result.provenance["non_sma_contract_hash"] == HASH


def test_live_real_order_plugin_rejects_incomplete_decision_evidence_contract() -> None:
    with pytest.raises(ValueError) as exc:
        ResearchStrategyPlugin(
            name="contract_canary",
            version="v1",
            spec=_spec(),
            required_data=("candles",),
            optional_data=(),
            runner=_runner,
            runtime_replay_builder=None,
            runtime_parameter_adapter=None,
            decision_contract_version="contract_canary.v1",
            diagnostics_namespace="contract_canary",
            research_event_builder=_research_events,
            runtime_decision_adapter_factory=lambda: object(),
            policy_assembly_factory=lambda: object(),
            runtime_capabilities=StrategyRuntimeCapabilities(
                promotion_runtime_decisions_supported=True,
                runtime_replay_supported=False,
                live_dry_run_allowed=True,
                live_real_order_allowed=True,
                approved_profile_required=True,
                fail_closed_reason="unit",
            ),
            decision_evidence_contract=DecisionEvidenceContract(),
        )

    message = str(exc.value)
    assert "strategy_live_real_order_decision_evidence_contract_incomplete:contract_canary" in message
    assert "snapshot_projector_contract" in message
    assert "one_of(fee_authority_hash|fee_authority_payload_hash)" in message
    assert "one_of(order_rules_hash|order_rules_payload_hash)" in message


def _live_real_order_request(
    *,
    contract: DecisionEvidenceContract,
    provenance: dict[str, object],
) -> StrategyEvaluationRequest:
    policy = _ContractPolicy()
    return StrategyEvaluationRequest(
        strategy_name=policy.name,
        strategy_instance_id="unit",
        mode="live_real_order",
        strategy_policy=policy,
        market_snapshot={"price": 1.0},
        position_snapshot=PositionSnapshot(in_position=False, entry_allowed=True, exit_allowed=False),
        strategy_config={},
        execution_constraints=ExecutionConstraintSnapshot(fee_rate_for_decision=0.0),
        exit_policy_config=None,
        rule_sources={},
        approved_profile_hash=HASH,
        runtime_contract_hash=HASH,
        plugin_contract_hash=HASH,
        request_hash=HASH,
        provenance={
            "strategy_parameters_hash": HASH,
            **provenance,
        },
        decision_evidence_contract=contract,
    )


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_name="contract_canary",
        strategy_version="v1",
        accepted_parameter_names=(),
        required_parameter_names=(),
        behavior_affecting_parameter_names=(),
        metadata_only_parameter_names=(),
        research_only_parameter_names=(),
        default_parameters={},
        decision_contract_version="contract_canary.v1",
        required_data=("candles",),
        optional_data=(),
        exit_policy_schema={"schema_version": 1},
    )


def _research_events(**_kwargs: object) -> tuple[object, ...]:
    return ()


def _runner(*_args: object, **_kwargs: object) -> object:
    return object()
