from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from bithumb_bot.decision_equivalence import sha256_prefixed


PAPER_LEGACY_PARAMETER_SOURCE = "paper_legacy_compat"
STRATEGY_PARAMETERS_JSON_FALLBACK = "STRATEGY_PARAMETERS_JSON"
SETTINGS_DERIVED_FALLBACK = "runtime_parameter_adapter.from_settings"


@dataclass(frozen=True)
class PaperLegacyParameterFallback:
    raw_parameters: dict[str, object]
    parameter_source: str
    audit: dict[str, object]


def fallback_source_hash(source: str, payload: Mapping[str, object]) -> str:
    return sha256_prefixed(
        {
            "paper_legacy_compat": True,
            "fallback_source": source,
            "fallback_payload": dict(payload),
        }
    )


def strategy_parameters_json_fallback(raw_json: str) -> PaperLegacyParameterFallback | None:
    raw = str(raw_json or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"strategy_parameters_json_invalid:{exc}") from exc
    if not isinstance(payload, Mapping):
        raise RuntimeError("strategy_parameters_json_must_be_object")
    normalized = {str(key): value for key, value in payload.items()}
    return _fallback(STRATEGY_PARAMETERS_JSON_FALLBACK, normalized)


def settings_derived_fallback(adapter: object, settings_obj: object) -> PaperLegacyParameterFallback:
    payload = dict(adapter.from_settings(settings_obj))
    return _fallback(SETTINGS_DERIVED_FALLBACK, payload)


def _fallback(source: str, payload: Mapping[str, object]) -> PaperLegacyParameterFallback:
    normalized = {str(key): value for key, value in dict(payload).items()}
    return PaperLegacyParameterFallback(
        raw_parameters=normalized,
        parameter_source=PAPER_LEGACY_PARAMETER_SOURCE,
        audit={
            "authority": PAPER_LEGACY_PARAMETER_SOURCE,
            "parameter_source": PAPER_LEGACY_PARAMETER_SOURCE,
            "legacy_fallback": source,
            "legacy_compatibility_used": True,
            "paper_legacy_compat": True,
            "fallback_source_hash": fallback_source_hash(source, normalized),
        },
    )

