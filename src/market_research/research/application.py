"""Public application service shared by Python callers and CLI adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from market_research.paths import ResearchPathManager
from market_research.storage_io import write_json_atomic, write_text_atomic

from .research_reporting import compare_research_decision_reports, render_research_decision_report_markdown
from .run_lifecycle import start_run
from .strategy_package import build_strategy_research_package
from .validation_pipeline import run_research_validation


@dataclass(frozen=True, slots=True)
class ResearchApplicationService:
    paths: ResearchPathManager
    strategy_registry: Any

    def validate(
        self, *, manifest: Any, manifest_path: str, db_path: str | Path | None,
        mode: str = "strict", execution_calibration: dict[str, Any] | None = None,
        execution_calibration_path: str | None = None, candidate_id: str | None = None,
        out_path: str | Path | None = None, generated_at: str | None = None,
        progress_callback: Any | None = None, run_id: str | None = None,
        record_lifecycle: bool = True,
    ) -> dict[str, Any]:
        command_args = {
            "manifest": manifest_path, "execution_calibration": execution_calibration_path,
            "candidate_id": candidate_id, "out": str(out_path) if out_path is not None else None,
            "mode": mode,
        }
        if not record_lifecycle:
            return self._run_validation(
                manifest=manifest, manifest_path=manifest_path, db_path=db_path, mode=mode,
                execution_calibration=execution_calibration,
                execution_calibration_path=execution_calibration_path, candidate_id=candidate_id,
                out_path=out_path, generated_at=generated_at, progress_callback=progress_callback,
                run_id=run_id,
            )
        handle = start_run(
            manager=self.paths, command="research-validate", command_args=command_args,
        )
        try:
            result = self._run_validation(
                manifest=manifest, manifest_path=manifest_path, db_path=db_path, mode=mode,
                execution_calibration=execution_calibration,
                execution_calibration_path=execution_calibration_path, candidate_id=candidate_id,
                out_path=out_path, generated_at=generated_at, progress_callback=progress_callback,
                run_id=handle.run_id,
            )
        except BaseException as exc:
            aborted = isinstance(exc, KeyboardInterrupt)
            handle.finish(
                status="ABORTED" if aborted else "FAILED",
                exit_code=130 if aborted else 1,
                result_content_hash=None,
                error=exc,
            )
            raise
        passed = result.get("end_to_end_validation_result") == "PASS"
        handle.finish(
            status="SUCCEEDED" if passed else "FAILED", exit_code=0 if passed else 1,
            result_content_hash=str(result.get("content_hash") or "") or None,
        )
        return result

    def export_strategy_package(
        self, *, report: dict[str, Any], approval: dict[str, Any], out_path: str | Path,
    ) -> dict[str, Any]:
        package = build_strategy_research_package(report, approval=approval)
        target = self.paths.external_output_path(out_path, label="strategy package output")
        write_json_atomic(target, package)
        return package

    def compare_reports(
        self, *, reports: list[dict[str, Any]], out_path: str | Path,
    ) -> dict[str, Any]:
        comparison = compare_research_decision_reports(reports)
        target = self.paths.external_output_path(out_path, label="research comparison output")
        write_json_atomic(target, comparison)
        return comparison

    def render_report(self, *, report: dict[str, Any], out_path: str | Path) -> str:
        rendered = render_research_decision_report_markdown(report)
        target = self.paths.external_output_path(out_path, label="rendered research report output")
        write_text_atomic(target, rendered)
        return rendered

    def _run_validation(self, **kwargs: Any) -> dict[str, Any]:
        return run_research_validation(
            manager=self.paths, strategy_registry=self.strategy_registry, **kwargs,
        )
