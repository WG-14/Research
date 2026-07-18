from __future__ import annotations

from copy import deepcopy

from market_research.research.hashing import sha256_prefixed


def hypothesis_spec_v2(
    *,
    hypothesis_id: str = "sma-uptrend-edge",
    version: str = "1.0.0",
    hypothesis_text: str = (
        "SMA crossovers have positive conditional expectancy after costs."
    ),
    phenomenon: str = "SMA crossovers have positive conditional expectancy.",
    mechanism: str = "Trend persistence delays price adjustment after crossovers.",
    experiment_family_id: str = "sma-uptrend-family",
    market: str = "KRW-BTC",
    interval: str = "1m",
    registration_status: str = "unregistered",
    pre_registered_at: str | None = None,
    registration_evidence_hash: str | None = None,
    competing_hypotheses: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    observation = {
        "schema_version": 1,
        "observation_id": "obs-sma-crossover-conditional-return",
        "version": "1.0.0",
        "statement": (
            "Closed-candle samples show clustered positive returns after some "
            "SMA crossovers."
        ),
        "actor_id": "researcher-a",
        "observed_at": "2025-12-01T00:00:00+00:00",
        "recorded_at": "2025-12-02T00:00:00+00:00",
        "market": market,
        "interval": interval,
        "confidence": 0.6,
        "status": "recorded",
        "fact_status": "not_verified",
        "evidence_hashes": ["sha256:" + "a" * 64],
    }
    observation_ref = {
        "observation_id": observation["observation_id"],
        "version": observation["version"],
        "observation_hash": sha256_prefixed(observation),
    }
    competitors = competing_hypotheses or [
        {
            "hypothesis_id": hypothesis_id,
            "version": version,
            "hypothesis_text": hypothesis_text,
        },
        {
            "hypothesis_id": "sma-uptrend-null",
            "version": "1.0.0",
            "hypothesis_text": (
                "SMA crossovers have no positive conditional expectancy after costs."
            ),
        },
    ]
    question = {
        "schema_version": 1,
        "question_id": "rq-sma-crossover-conditional-expectancy",
        "version": "1.0.0",
        "question_text": (
            "Do SMA crossovers predict positive conditional expectancy after costs?"
        ),
        "actor_id": "researcher-a",
        "recorded_at": "2025-12-03T00:00:00+00:00",
        "observation_refs": [deepcopy(observation_ref)],
        "competing_hypotheses": deepcopy(competitors),
    }
    return {
        "schema_version": 2,
        "hypothesis_id": hypothesis_id,
        "version": version,
        "hypothesis_text": hypothesis_text,
        "actor_id": "researcher-a",
        "created_at": "2025-12-04T00:00:00+00:00",
        "phenomenon": phenomenon,
        "mechanism": mechanism,
        "observation_conditions": ["uptrend", "sufficient candle coverage"],
        "comparison_target": "cash",
        "falsification_criteria": ["validation return is not positive"],
        "experiment_family_id": experiment_family_id,
        "registration_status": registration_status,
        "pre_registered_at": pre_registered_at,
        "registration_evidence_hash": registration_evidence_hash,
        "observations": [observation],
        "research_question": question,
        "research_question_ref": {
            "question_id": question["question_id"],
            "version": question["version"],
            "question_hash": sha256_prefixed(question),
        },
        "observation_refs": [observation_ref],
    }
