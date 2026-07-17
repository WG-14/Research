"""Stable low-level contracts shared with trusted platform adapters.

Web and Operations import these names instead of reaching into Research
implementation packages.  Adding or removing a name is therefore an explicit
cross-package compatibility decision covered by monorepo boundary tests.
"""

from market_research.research.experiment_manifest import (
    ManifestValidationError,
    parse_manifest_with_registry,
)
from market_research.research.experiment_identity import (
    validate_experiment_identity_registry,
)
from market_research.research.governance import (
    GovernanceError,
    GovernanceSubjectType,
    StrategyCandidateLifecycleState,
    governance_registry_path,
    load_governance_rows,
    validate_governance_registry,
)
from market_research.research.hash_chain import (
    append_hash_chained_jsonl_idempotent,
    validate_hash_chained_jsonl,
    verify_hash_chained_jsonl_event,
)
from market_research.research.hashing import (
    content_hash_payload,
    report_content_hash_payload,
    sha256_prefixed,
)
from market_research.research.research_decision_report import (
    validate_research_decision_report,
)
from market_research.research.segmented_hash_chain import (
    append_segmented_hash_chained_jsonl_idempotent,
    read_segmented_hash_chain_full_snapshot,
    validate_segmented_hash_chain_incremental,
    verify_segmented_hash_chained_jsonl_event,
)
from market_research.research_composition import load_builtin_manifest

__all__ = [
    "GovernanceError",
    "GovernanceSubjectType",
    "ManifestValidationError",
    "StrategyCandidateLifecycleState",
    "append_hash_chained_jsonl_idempotent",
    "append_segmented_hash_chained_jsonl_idempotent",
    "content_hash_payload",
    "governance_registry_path",
    "load_governance_rows",
    "load_builtin_manifest",
    "parse_manifest_with_registry",
    "read_segmented_hash_chain_full_snapshot",
    "report_content_hash_payload",
    "sha256_prefixed",
    "validate_governance_registry",
    "validate_hash_chained_jsonl",
    "validate_experiment_identity_registry",
    "validate_research_decision_report",
    "validate_segmented_hash_chain_incremental",
    "verify_hash_chained_jsonl_event",
    "verify_segmented_hash_chained_jsonl_event",
]
