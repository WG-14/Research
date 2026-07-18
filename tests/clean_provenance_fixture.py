"""Opt-in committed-checkout provenance for receipt-producing E2E tests."""

from __future__ import annotations

from market_research.research.code_provenance import collect_code_provenance
from market_research.research.hashing import sha256_prefixed


def committed_checkout_provenance(project_root):
    """Model a committed checkout while the shared patch worktree is dirty."""

    provenance = collect_code_provenance(project_root)
    provenance["git_dirty"] = False
    provenance["git_status_hash"] = sha256_prefixed(
        {"receipt_e2e_fixture": "clean_status"}
    )
    provenance["git_diff_hash"] = sha256_prefixed({"receipt_e2e_fixture": "clean_diff"})
    provenance["code_provenance_hash"] = sha256_prefixed(
        {
            key: value
            for key, value in provenance.items()
            if key != "code_provenance_hash"
        },
        label="code_provenance",
    )
    return provenance


def install_committed_checkout_provenance(monkeypatch) -> None:
    monkeypatch.setattr(
        "market_research.research.execution_plan.collect_code_provenance",
        committed_checkout_provenance,
    )


__all__ = [
    "committed_checkout_provenance",
    "install_committed_checkout_provenance",
]
