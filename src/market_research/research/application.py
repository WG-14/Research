"""Public application service shared by Python callers and CLI adapters."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from market_research.paths import ResearchPathManager
from market_research.storage_io import (
    write_json_atomic,
    write_json_atomic_create_or_verify,
    write_text_atomic,
)

from .data_governance import (
    DataGovernanceError,
    publish_data_usage_binding_for_artifact,
)
from .experiment_identity import bind_research_validation_experiment
from .knowledge_contract import AuthorityRef
from .research_reporting import (
    compare_research_decision_reports,
    render_research_decision_report_markdown,
)
from .research_classification import requires_candidate_validation
from .run_lifecycle import start_run
from .strategy_package import StrategyPackageError, build_strategy_research_package
from .study_lifecycle import (
    complete_study_validation as preserve_validation_result,
    preserve_study_validation_failure as preserve_failed_validation,
)
from .validation_pipeline import run_research_validation


@dataclass(frozen=True, slots=True)
class ResearchApplicationService:
    paths: ResearchPathManager
    strategy_registry: Any

    def validate(
        self,
        *,
        manifest: Any,
        manifest_path: str,
        db_path: str | Path | None,
        mode: str = "strict",
        execution_calibration: dict[str, Any] | None = None,
        execution_calibration_path: str | None = None,
        candidate_id: str | None = None,
        out_path: str | Path | None = None,
        generated_at: str | None = None,
        progress_callback: Any | None = None,
        run_id: str | None = None,
        record_lifecycle: bool = True,
    ) -> dict[str, Any]:
        validation_bound = requires_candidate_validation(
            getattr(manifest, "research_classification", None)
        )
        bind_research_validation_experiment(
            manager=self.paths,
            experiment_id=manifest.experiment_id,
            manifest_hash=manifest.manifest_hash(),
        )
        command_args = {
            "manifest": manifest_path,
            "execution_calibration": execution_calibration_path,
            "candidate_id": candidate_id,
            "out": str(out_path) if out_path is not None else None,
            "mode": mode,
        }
        if not record_lifecycle:
            try:
                result = self._run_validation(
                    manifest=manifest,
                    manifest_path=manifest_path,
                    db_path=db_path,
                    mode=mode,
                    execution_calibration=execution_calibration,
                    execution_calibration_path=execution_calibration_path,
                    candidate_id=candidate_id,
                    out_path=out_path,
                    generated_at=generated_at,
                    progress_callback=progress_callback,
                    run_id=run_id,
                )
            except BaseException as exc:
                if (
                    not isinstance(exc, KeyboardInterrupt)
                    and run_id is not None
                    and validation_bound
                    and getattr(manifest, "hypothesis_spec", None) is not None
                ):
                    try:
                        preserve_failed_validation(
                            manager=self.paths,
                            manifest=manifest,
                            run_id=run_id,
                            error=exc,
                        )
                    except (
                        OSError,
                        RuntimeError,
                        TypeError,
                        ValueError,
                    ) as preserve_exc:
                        exc.add_note(
                            "validation failure evidence publication also failed: "
                            f"{type(preserve_exc).__name__}"
                        )
                raise
            if (
                run_id is not None
                and validation_bound
                and getattr(manifest, "hypothesis_spec", None) is not None
            ):
                preserve_validation_result(
                    manager=self.paths,
                    manifest=manifest,
                    run_id=run_id,
                    report=result,
                )
            return result
        handle = start_run(
            manager=self.paths,
            command="research-validate",
            command_args=command_args,
        )
        try:
            result = self._run_validation(
                manifest=manifest,
                manifest_path=manifest_path,
                db_path=db_path,
                mode=mode,
                execution_calibration=execution_calibration,
                execution_calibration_path=execution_calibration_path,
                candidate_id=candidate_id,
                out_path=out_path,
                generated_at=generated_at,
                progress_callback=progress_callback,
                run_id=handle.run_id,
            )
        except BaseException as exc:
            aborted = isinstance(exc, KeyboardInterrupt)
            if (
                not aborted
                and validation_bound
                and getattr(manifest, "hypothesis_spec", None) is not None
            ):
                try:
                    preserve_failed_validation(
                        manager=self.paths,
                        manifest=manifest,
                        run_id=handle.run_id,
                        error=exc,
                    )
                except (OSError, RuntimeError, TypeError, ValueError) as preserve_exc:
                    exc.add_note(
                        "validation failure evidence publication also failed: "
                        f"{type(preserve_exc).__name__}"
                    )
            handle.finish(
                status="ABORTED" if aborted else "FAILED",
                exit_code=130 if aborted else 1,
                result_content_hash=None,
                error=exc,
            )
            raise
        if validation_bound and getattr(manifest, "hypothesis_spec", None) is not None:
            try:
                preserve_validation_result(
                    manager=self.paths,
                    manifest=manifest,
                    run_id=handle.run_id,
                    report=result,
                )
            except BaseException as exc:
                handle.finish(
                    status="FAILED",
                    exit_code=1,
                    result_content_hash=None,
                    error=exc,
                )
                raise
        passed = result.get("end_to_end_validation_result") == "PASS"
        handle.finish(
            status="SUCCEEDED" if passed else "FAILED",
            exit_code=0 if passed else 1,
            result_content_hash=str(result.get("content_hash") or "") or None,
        )
        return result

    def export_strategy_package(
        self,
        *,
        report: dict[str, Any],
        approval: dict[str, Any],
        out_path: str | Path,
    ) -> dict[str, Any]:
        package = build_strategy_research_package(
            report,
            approval=approval,
            manager=self.paths,
        )
        if (
            package.get("authoritative") is not True
            or package.get("package_authority_result") != "PASS"
        ):
            raise StrategyPackageError(
                "official_strategy_package_must_be_authoritative"
            )
        target = self.paths.external_output_path(
            out_path, label="strategy package output"
        )
        experiment_id = str(report.get("experiment_id") or "").strip()
        if not experiment_id:
            raise StrategyPackageError("strategy_package_experiment_id_required")
        canonical_target = self.paths.report_path(
            "research", experiment_id, "strategy_package.json"
        )
        targets = tuple(dict.fromkeys((target.absolute(), canonical_target.absolute())))
        publication_target = target
        try:
            expected = (
                json.dumps(
                    package,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            for publication_target in targets:
                if publication_target.is_symlink():
                    raise ValueError("atomic_json_target_conflict")
                if (
                    publication_target.exists()
                    and publication_target.read_bytes() != expected
                ):
                    raise ValueError("atomic_json_target_conflict")
            for publication_target in targets:
                write_json_atomic_create_or_verify(publication_target, package)
        except (OSError, ValueError) as exc:
            raise StrategyPackageError(
                f"strategy_package_publication_failed:{publication_target.name}:{exc}"
            ) from exc
        try:
            publish_data_usage_binding_for_artifact(
                manager=self.paths,
                source=report,
                affected_authority_refs=(
                    AuthorityRef(
                        authority="strategy_package_export",
                        subject_type="research_package",
                        subject_id=experiment_id,
                        subject_version=str(package["content_hash"]),
                        authority_hash=str(package["content_hash"]),
                    ),
                ),
                recorded_by=str(approval.get("reviewer_id") or "strategy-package"),
                recorded_at=str(approval.get("approved_at") or "") or None,
                required_purpose="RESEARCH_PACKAGE_EXPORT",
            )
        except DataGovernanceError as exc:
            raise StrategyPackageError(
                f"strategy_package_data_usage_binding_failed:{exc}"
            ) from exc
        return package

    def compare_reports(
        self,
        *,
        reports: list[dict[str, Any]],
        out_path: str | Path,
    ) -> dict[str, Any]:
        comparison = compare_research_decision_reports(reports)
        target = self.paths.external_output_path(
            out_path, label="research comparison output"
        )
        write_json_atomic(target, comparison)
        return comparison

    def render_report(self, *, report: dict[str, Any], out_path: str | Path) -> str:
        rendered = render_research_decision_report_markdown(report)
        target = self.paths.external_output_path(
            out_path, label="rendered research report output"
        )
        write_text_atomic(target, rendered)
        return rendered

    def _run_validation(self, **kwargs: Any) -> dict[str, Any]:
        return run_research_validation(
            manager=self.paths,
            strategy_registry=self.strategy_registry,
            **kwargs,
        )
