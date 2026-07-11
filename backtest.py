from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

SMOKE_BACKTEST_WARNING = (
    "This is a smoke backtest only. It must not be used as evidence for strategy validation, "
    "approved profiles, live readiness, or capital allocation."
)

ROOT_BACKTEST_REFUSAL = {
    "diagnostic_only": True,
    "non_candidate_selection_eligible": True,
    "validation_grade": False,
    "evidence_scope": "smoke_only_not_manifest_backed",
    "standalone_backtest_not_full_validation": True,
    "reason_code": "standalone_backtest_not_full_validation",
    "operator_next_action": "use_manifest_backed_research_validation",
    "validation_command": "uv run bithumb-research research-validate --manifest <path>",
    "diagnostic_command": "uv run bithumb-research research-backtest --manifest <path>",
}


def root_backtest_refusal_lines() -> tuple[str, ...]:
    payload = ROOT_BACKTEST_REFUSAL
    return (
        f"[SMOKE-BACKTEST REFUSED] {SMOKE_BACKTEST_WARNING}",
        " ".join(
            (
                f"diagnostic_only={str(payload['diagnostic_only']).lower()}",
                f"non_candidate_selection_eligible={str(payload['non_candidate_selection_eligible']).lower()}",
                f"validation_grade={str(payload['validation_grade']).lower()}",
                f"evidence_scope={payload['evidence_scope']}",
                "standalone_backtest_not_full_validation="
                f"{str(payload['standalone_backtest_not_full_validation']).lower()}",
            )
        ),
        " ".join(
            (
                f"reason_code={payload['reason_code']}",
                f"operator_next_action={payload['operator_next_action']}",
                f"validation_command='{payload['validation_command']}'",
                f"diagnostic_command='{payload['diagnostic_command']}'",
            )
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail-closed compatibility wrapper. Validation-grade validation must use "
            "`uv run bithumb-research research-validate --manifest <path>`."
        )
    )
    parser.add_argument(
        "--diagnostic-smoke-only",
        action="store_true",
        help="Explicitly run the non-candidate_selection_eligible smoke backtest implementation.",
    )
    args, remaining = parser.parse_known_args(argv)
    if not args.diagnostic_smoke_only:
        for line in root_backtest_refusal_lines():
            print(line, file=sys.stderr)
        return 2
    from tools.diagnostic_smoke_backtest import main as smoke_main

    return smoke_main(["--diagnostic-smoke-only", *remaining])


if __name__ == "__main__":
    raise SystemExit(main())
