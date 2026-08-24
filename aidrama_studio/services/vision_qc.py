"""Optional Vision-QC decision support above deterministic QC.

Technical QC remains canonical.  This module only asks a configured
VisionAnalysisProvider for structured, explicitly labelled AI_ANALYSIS data;
when no live provider is configured it returns NOT_RUN rather than a fake pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from uuid import uuid4

from .ai_capabilities import CapabilityUnavailable, UnavailableVisionProvider, VisionAnalysisProvider
from .production_qc import ProductionQCService, ProductionQCServiceError
from aidrama_studio.domain import VisionAnalysisRecord, VisionFrameManifest
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


class VisionQCError(RuntimeError):
    pass


class VisionFrameSamplingService:
    """Create a deterministic, project-scoped frame manifest."""

    def __init__(self, repository: ProjectRepository | None = None) -> None:
        self.repository = repository or ProjectRepository()
        self.qc = ProductionQCService(self.repository)

    def sample(self, project_id: str, execution_id: str, artifact_id: str | None = None, *, max_frames: int = 8) -> VisionFrameManifest:
        if max_frames < 1 or max_frames > 32:
            raise VisionQCError("max_frames 必须在 1 到 32 之间")
        execution = self.qc._get_execution(project_id, execution_id)
        artifact = self.qc._select_artifact(execution_id, artifact_id)
        if artifact is None:
            raise VisionQCError("artifact 不存在")
        path = self.qc._resolve_artifact_path(project_id, artifact.path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise VisionQCError("artifact 文件不存在或为空")
        probe = self.qc._probe_video(path)
        if not probe.get("video_stream"):
            raise VisionQCError("只能对真实 video artifact 采样")
        duration = float(probe.get("duration_seconds") or 0)
        count = min(max_frames, max(1, int(round(duration * 2))))
        times = [0.0] if count == 1 else [round((duration * index) / (count - 1), 3) for index in range(count)]
        root = (self.repository.paths.projects / project_id / "production" / execution_id / "vision" / "frames").resolve()
        project_root = (self.repository.paths.projects / project_id).resolve()
        if project_root not in root.parents:
            raise VisionQCError("vision frame path escapes project")
        root.mkdir(parents=True, exist_ok=True)
        samples: list[dict[str, object]] = []
        ffmpeg = self._ffmpeg()
        for index, timestamp in enumerate(times):
            relative = PurePosixPath("production", execution_id, "vision", "frames", f"frame-{index:03d}.jpg").as_posix()
            target = root / f"frame-{index:03d}.jpg"
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            try:
                command = [ffmpeg, "-y", "-ss", str(timestamp), "-i", str(path), "-frames:v", "1", "-q:v", "3", str(temporary)]
                completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)
                if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size <= 0:
                    raise VisionQCError("frame extraction failed")
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    temporary.unlink()
            samples.append({"index": index, "time_seconds": timestamp, "path": relative, "sha256": self._sha256(target)})
        digest = hashlib.sha256(json.dumps(samples, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        manifest = VisionFrameManifest(id=uuid4().hex, project_id=project_id, execution_id=execution_id, artifact_id=artifact.id, frame_count=len(samples), samples=tuple(samples), sha256=digest, created_at=_now())
        return self.repository.create_vision_frame_manifest(manifest)

    @staticmethod
    def _ffmpeg() -> str:
        from app.utils.utils import get_ffmpeg_binary

        return get_ffmpeg_binary()

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()


class VisionQCService:
    def __init__(self, repository: ProjectRepository | None = None, *, provider: VisionAnalysisProvider | None = None, sampler: VisionFrameSamplingService | None = None) -> None:
        self.repository = repository or ProjectRepository()
        self.provider = provider or UnavailableVisionProvider()
        self._deterministic = ProductionQCService(self.repository)
        self.sampler = sampler or VisionFrameSamplingService(self.repository)

    def analyze(self, project_id: str, execution_id: str, artifact_id: str | None = None, *, context: Mapping[str, object] | None = None) -> VisionQCResult:
        execution = self._deterministic._get_execution(project_id, execution_id)
        artifact = self._deterministic._select_artifact(execution_id, artifact_id)
        relative_path = artifact.path if artifact else ""
        frame_manifest = None
        if artifact is not None:
            try:
                frame_manifest = self.sampler.sample(project_id, execution_id, artifact.id)
            except Exception:
                # Sampling failure is recorded as NOT_RUN/FAILED below; it
                # must never turn into a fabricated vision pass.
                frame_manifest = None
        try:
            path = self._deterministic._resolve_artifact_path(project_id, relative_path)
            analysis = self.provider.analyze(artifact_path=str(path), context=dict(context or {}) | {"frame_manifest_id": frame_manifest.id if frame_manifest else None})
            record = self.repository.create_vision_analysis(VisionAnalysisRecord(id=uuid4().hex, project_id=project_id, execution_id=execution_id, artifact_id=artifact.id if artifact else None, frame_manifest_id=frame_manifest.id if frame_manifest else None, provider_id=analysis.provider, model_id=str(analysis.metadata.get("model", "unspecified")), status="AI_ANALYSIS", metrics=dict(analysis.metrics), reference_comparison=dict(analysis.metadata.get("reference_comparison", {})) if isinstance(analysis.metadata, Mapping) else {}, created_at=_now()))
            return VisionQCResult(project_id, execution_id, artifact.id if artifact else None, "AI_ANALYSIS", analysis.analysis_kind, analysis.provider, dict(analysis.metrics), "", record.created_at)
        except (CapabilityUnavailable, FileNotFoundError, ProductionQCServiceError) as exc:
            status = "NOT_RUN" if isinstance(self.provider, UnavailableVisionProvider) or isinstance(exc, CapabilityUnavailable) else "FAILED"
            if artifact is not None:
                try:
                    self.repository.create_vision_analysis(VisionAnalysisRecord(id=uuid4().hex, project_id=project_id, execution_id=execution_id, artifact_id=artifact.id, frame_manifest_id=frame_manifest.id if frame_manifest else None, provider_id=getattr(self.provider, "provider_name", "VISION"), model_id="unavailable", status=status, metrics={}, reference_comparison={}, created_at=_now()))
                except Exception:
                    pass
            return VisionQCResult(project_id, execution_id, artifact.id if artifact else None, status, "AI_ANALYSIS", getattr(self.provider, "provider_name", "VISION"), {}, str(exc), _now())


__all__ = ["VisionFrameSamplingService", "VisionQCError", "VisionQCResult", "VisionQCService"]
