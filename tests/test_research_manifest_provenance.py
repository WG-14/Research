from __future__ import annotations

from dataclasses import replace

from bithumb_bot.research.experiment_manifest import parse_manifest
from tests.test_research_backtest_reproducibility import _manifest


def test_parse_manifest_records_execution_field_omission() -> None:
    payload = _manifest()
    payload.pop("research_run", None)

    manifest = parse_manifest(payload)
    provenance = manifest.manifest_input_provenance.research_run.execution

    assert provenance.mode_declared is False
    assert provenance.max_workers_declared is False
    assert provenance.work_unit_declared is False
    assert provenance.process_start_method_declared is False


def test_parse_manifest_records_explicit_serial_execution() -> None:
    payload = _manifest()
    payload["research_run"] = {
        "execution": {
            "mode": "serial",
            "max_workers": 1,
            "work_unit": "candidate_scenario",
            "process_start_method": "auto_safe",
        }
    }

    manifest = parse_manifest(payload)
    provenance = manifest.manifest_input_provenance.research_run.execution

    assert provenance.mode_declared is True
    assert provenance.max_workers_declared is True
    assert provenance.work_unit_declared is True
    assert provenance.process_start_method_declared is True


def test_manifest_replace_preserves_execution_provenance() -> None:
    payload = _manifest()
    payload["research_run"] = {"execution": {"mode": "serial", "max_workers": 1}}
    manifest = parse_manifest(payload)

    replaced = replace(manifest, raw={**manifest.raw, "test_marker": True})

    assert replaced.manifest_input_provenance == manifest.manifest_input_provenance
