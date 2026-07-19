from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from market_research.research.derivatives.common import (
    QualityDecision,
    QualityResult,
    RunType,
)
from market_research.research.derivatives.evidence import (
    DerivativeEvidenceError,
    DerivativeProductKind,
    ProductChainEvidence,
)
from market_research.research.derivatives.options import OptionChainSnapshot
from tests.test_futures_derivative_research import _market_fixture
from tests.test_options_derivative_research import (
    _contract,
    _hash,
    _quote,
)


def test_futures_chain_evidence_is_derived_from_actual_typed_chain() -> None:
    _near, _deferred, chain, _later = _market_fixture()

    evidence = ProductChainEvidence.from_futures_chain(chain)
    restored = ProductChainEvidence.from_dict(evidence.as_dict())

    assert restored == evidence
    assert evidence.product_kind is DerivativeProductKind.FUTURE
    assert evidence.source_chain_hash == chain.content_hash
    assert evidence.chain_payload == chain.as_dict()
    assert evidence.universe_ids == tuple(
        contract.contract_id for contract in chain.contracts
    )
    evidence.admit(RunType.CONFIRMATORY)


def test_option_chain_evidence_binds_series_membership_and_quality() -> None:
    observed_at = "2026-03-10T16:00:00Z"
    contract = _contract("OPT.C100", strike="100")
    quote = _quote(contract, bid="4", ask="5", as_of=observed_at)
    chain = OptionChainSnapshot(
        chain_snapshot_id="chain.option.evidence",
        underlying_id=contract.underlying_id,
        knowledge_time=observed_at,
        underlying_price=Decimal("101"),
        contracts=(contract,),
        quotes=(quote,),
        source_manifest_hashes=(_hash("a"),),
        quality_results=(
            QualityResult(
                check_id="option-chain-complete",
                check_version="v1",
                decision=QualityDecision.PASS,
            ),
        ),
    )

    evidence = ProductChainEvidence.from_option_chain(chain)

    assert evidence.product_kind is DerivativeProductKind.OPTION
    assert evidence.source_chain_hash == chain.content_hash
    assert evidence.chain_payload == chain.as_dict()
    assert evidence.knowledge_time == chain.knowledge_time
    assert evidence.universe_ids == (contract.contract_id,)
    assert evidence.ref().authority == "derivative_chain_snapshot"
    evidence.admit(RunType.CONFIRMATORY)


def test_full_chain_payload_tamper_is_recomputed_and_rejected() -> None:
    _near, _deferred, chain, _later = _market_fixture()
    payload = deepcopy(ProductChainEvidence.from_futures_chain(chain).as_dict())
    chain_payload = payload["chain_payload"]
    assert isinstance(chain_payload, dict)
    contracts = chain_payload["contracts"]
    assert isinstance(contracts, list)
    first_contract = contracts[0]
    assert isinstance(first_contract, dict)
    first_contract["contract_id"] = "FUT.TAMPERED"

    with pytest.raises(
        DerivativeEvidenceError,
        match="product_chain_payload_hash_mismatch",
    ):
        ProductChainEvidence.from_dict(payload)
