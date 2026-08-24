"""Optional Vision-QC decision support above deterministic QC.

Technical QC remains canonical.  This module only asks a configured
VisionAnalysisProvider for structured, explicitly labelled AI_ANALYSIS data;
when no live provider is configured it returns NOT_RUN rather than a fake pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

from .ai_capabilities import CapabilityUnavailable, UnavailableVisionProvider, VisionAnalysisProvider
from .production_qc import ProductionQCService, ProductionQCServiceError
from aidrama_studio.storage.repositories import ProjectRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class VisionQCResult:
    project_id: str
    execution_id: str
    artifact_id: str | None
    status: str
    analysis_kind: str = "AI_ANALYSIS"
    provider: str = "UNCONFIGURED_VISION"
    metrics: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    reason: str = ""
    created_at: str = ""


class VisionQCService:
    def __init__(self, repository: ProjectRepository | None = None, *, provider: VisionAnalysisProvider | None = None) -> None:
        self.repository = repository or ProjectRepository()
        self.provider = provider or UnavailableVisionProvider()
        self._deterministic = ProductionQCService(self.repository)

    def analyze(self, project_id: str, execution_id: str, artifact_id: str | None = None, *, context: Mapping[str, object] | None = None) -> VisionQCResult:
        execution = self._deterministic._get_execution(project_id, execution_id)
        artifact = self._deterministic._select_artifact(execution_id, artifact_id)
        relative_path = artifact.path if artifact else ""
        try:
            path = self._deterministic._resolve_artifact_path(project_id, relative_path)
            analysis = self.provider.analyze(artifact_path=str(path), context=context)
            return VisionQCResult(project_id, execution_id, artifact.id if artifact else None, "AI_ANALYSIS", analysis.analysis_kind, analysis.provider, dict(analysis.metrics), "", _now())
        except (CapabilityUnavailable, FileNotFoundError, ProductionQCServiceError) as exc:
            status = "NOT_RUN" if isinstance(self.provider, UnavailableVisionProvider) or isinstance(exc, CapabilityUnavailable) else "FAILED"
            return VisionQCResult(project_id, execution_id, artifact.id if artifact else None, status, "AI_ANALYSIS", getattr(self.provider, "provider_name", "VISION"), {}, str(exc), _now())


__all__ = ["VisionQCResult", "VisionQCService"]
