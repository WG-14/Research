"""Deliberately invalid calls used to prove the strict type gate is active."""

from market_research.application.contracts import ResearchPreflightRequest
from portal.storage import SafeArtifactRef
from research_operations.database import database_url


request = ResearchPreflightRequest(manifest_path="/tmp/manifest.json")
artifact = SafeArtifactRef(root="report", relative_path="example/report.json")

# Each call crosses a distribution boundary with an incompatible typed value.
database_url(request)
SafeArtifactRef.parse(request)
SafeArtifactRef.parse(artifact)
