"""Published read-only research exploration boundary for adapters.

Web and other adapters import this module instead of reaching into the
research implementation package.  The implementation remains owned by the
offline research engine; this facade defines the stable application-level
query surface that adapters are allowed to consume.
"""

from market_research.research.data_exploration_queries import (
    query_dataset_artifact_detail,
    query_dataset_artifacts,
    query_feature_definition_detail,
    query_feature_definitions,
)
from market_research.research.exploration_queries import (
    ExplorationRecord,
    ResearchExplorationQueryError,
    query_final_research_package_detail,
    query_final_research_package_diff,
    query_final_research_package_lineage,
    query_final_research_packages,
    query_lineage_detail,
    query_lineage_records,
    query_prospective_detail,
    query_prospective_validations,
    query_structured_validation_decisions,
    query_validation_decision_detail,
    safe_research_projection,
)

__all__ = [
    "ExplorationRecord",
    "ResearchExplorationQueryError",
    "query_dataset_artifact_detail",
    "query_dataset_artifacts",
    "query_feature_definition_detail",
    "query_feature_definitions",
    "query_final_research_package_detail",
    "query_final_research_package_diff",
    "query_final_research_package_lineage",
    "query_final_research_packages",
    "query_lineage_detail",
    "query_lineage_records",
    "query_prospective_detail",
    "query_prospective_validations",
    "query_structured_validation_decisions",
    "query_validation_decision_detail",
    "safe_research_projection",
]
