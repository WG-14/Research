from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bithumb_bot.runtime_data_provider import RuntimeFeatureSnapshot
from bithumb_bot.runtime_strategy_decision import _project_runtime_feature_snapshot


def test_production_runtime_modules_do_not_import_legacy_parameter_fallback_directly() -> None:
    allowed = {"src/bithumb_bot/runtime_strategy_set.py"}
    production_files = (
        "src/bithumb_bot/runtime_strategy_decision.py",
        "src/bithumb_bot/runtime_decision_service.py",
        "src/bithumb_bot/runtime_strategy_set.py",
        "src/bithumb_bot/runtime_adapter_bootstrap.py",
        "src/bithumb_bot/runtime_data_provider.py",
    )
    violations = [
        path
        for path in production_files
        if "legacy_compat.runtime_parameters" in Path(path).read_text(encoding="utf-8")
        and path not in allowed
    ]

    assert violations == []


def test_legacy_parameter_fallback_module_is_explicitly_paper_compatibility() -> None:
    source = Path("src/bithumb_bot/legacy_compat/runtime_parameters.py").read_text(encoding="utf-8")

    assert "PAPER_LEGACY_PARAMETER_SOURCE" in source
    assert "paper_legacy_compat" in source
    assert "STRATEGY_PARAMETERS_JSON" in source
    assert "runtime_parameter_adapter.from_settings" in source


def test_db_bound_projector_signature_is_rejected_before_projection() -> None:
    calls: list[str] = []

    class _DbBoundProjector:
        def project_feature_snapshot(self, conn, request, feature_snapshot):  # type: ignore[no-untyped-def]
            calls.append("called")
            return feature_snapshot

    with pytest.raises(RuntimeError, match="promotion_runtime_adapter_db_bound_projector_forbidden:unit"):
        _project_runtime_feature_snapshot(
            adapter=_DbBoundProjector(),
            request=SimpleNamespace(strategy_name="unit"),
            feature_snapshot=RuntimeFeatureSnapshot({"feature_payload": {}, "feature_snapshot_hash": "sha256:x"}),
        )

    assert calls == []


def test_builtin_sma_db_bound_projector_fails_closed_at_projection_boundary() -> None:
    from bithumb_bot.runtime_adapters.sma_with_filter import SmaWithFilterRuntimeDecisionAdapter

    with pytest.raises(RuntimeError, match="promotion_runtime_adapter_db_bound_projector_forbidden:sma_with_filter"):
        _project_runtime_feature_snapshot(
            adapter=SmaWithFilterRuntimeDecisionAdapter(),
            request=SimpleNamespace(strategy_name="sma_with_filter"),
            feature_snapshot=RuntimeFeatureSnapshot({"feature_payload": {}, "feature_snapshot_hash": "sha256:x"}),
        )
