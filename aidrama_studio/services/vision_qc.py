"""Append-only semantic Vision analysis above deterministic media QC.

Technical QC remains canonical. A configured provider receives one exact,
project-scoped input snapshot: the physical video artifact, a deterministic
frame manifest, the frozen GenerationBrief context, and the exact immutable
reference versions selected for the execution. No provider response can
silently override human review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence
from uuid import uuid4

from .ai_capabilities import (
    CapabilityKind,
    CapabilityRegistry,
    UnavailableVisionProvider,
    VisionAnalysisProvider,
    VisionAnalysisRequest,
    VisionMediaInput,
)
from .production_qc import ProductionQCService
from .provider_profiles import ProviderDisclosure, ProviderProfileError, ProviderProfileService
from .reference_assets import ReferenceAssetService
from .runtime_foundation import AIInvocationService
from .security import sanitize_error, sanitize_persistent_metadata
from aidrama_studio.domain import VisionAnalysisRecord, VisionFrameManifest
from aidrama_studio.storage.repositories import ProjectRepository


VISION_BLOCKS_FINAL = False


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
    analysis_id: str | None = None
    frame_manifest_id: str | None = None


class VisionQCError(RuntimeError):
    pass


class VisionFrameSamplingService:
    """Create immutable deterministic frame manifests inside one project."""

    def __init__(self, repository: ProjectRepository | None = None) -> None:
        self.repository = repository or ProjectRepository()
        self.qc = ProductionQCService(self.repository)

    def sample(
        self,
        project_id: str,
        execution_id: str,
        artifact_id: str | None = None,
        *,
        max_frames: int = 8,
        suspicious_windows: Sequence[Mapping[str, object]] = (),
    ) -> VisionFrameManifest:
        if max_frames < 1 or max_frames > 32:
            raise VisionQCError("max_frames 必须在 1 到 32 之间")
        self.qc._get_execution(project_id, execution_id)
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
        if not math.isfinite(duration) or duration <= 0:
            raise VisionQCError("video duration 无效")
        points = self._sample_points(duration, max_frames, suspicious_windows)
        manifest_id = uuid4().hex
        root = (
            self.repository.paths.projects
            / project_id
            / "production"
            / execution_id
            / "vision"
            / "frames"
            / manifest_id
        ).resolve()
        project_root = (self.repository.paths.projects / project_id).resolve()
        if project_root not in root.parents:
            raise VisionQCError("vision frame path escapes project")
        root.mkdir(parents=True, exist_ok=False)
        samples: list[dict[str, object]] = []
        ffmpeg = self._ffmpeg()
        try:
            for index, (timestamp, role) in enumerate(points):
                filename = f"frame-{index:03d}.jpg"
                relative = PurePosixPath(
                    "production",
                    execution_id,
                    "vision",
                    "frames",
                    manifest_id,
                    filename,
                ).as_posix()
                target = root / filename
                temporary = root / f".{target.stem}.{uuid4().hex}.tmp{target.suffix}"
                try:
                    command = [
                        ffmpeg,
                        "-y",
                        "-ss",
                        str(timestamp),
                        "-i",
                        str(path),
                        "-frames:v",
                        "1",
                        "-q:v",
                        "3",
                        str(temporary),
                    ]
                    completed = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=60,
                    )
                    if (
                        completed.returncode != 0
                        or not temporary.is_file()
                        or temporary.stat().st_size <= 0
                    ):
                        raise VisionQCError("frame extraction failed")
                    with temporary.open("rb+") as handle:
                        if handle.read(3) != b"\xff\xd8\xff":
                            raise VisionQCError("frame extraction returned invalid JPEG")
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, target)
                finally:
                    if temporary.exists():
                        temporary.unlink()
                samples.append(
                    {
                        "index": index,
                        "role": role,
                        "time_seconds": timestamp,
                        "path": relative,
                        "sha256": self._sha256(target),
                    }
                )
            digest = hashlib.sha256(
                json.dumps(
                    samples,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            manifest = VisionFrameManifest(
                id=manifest_id,
                project_id=project_id,
                execution_id=execution_id,
                artifact_id=artifact.id,
                frame_count=len(samples),
                samples=tuple(samples),
                sha256=digest,
                created_at=_now(),
            )
            return self.repository.create_vision_frame_manifest(manifest)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise

    @staticmethod
    def _sample_points(
        duration: float,
        max_frames: int,
        suspicious_windows: Sequence[Mapping[str, object]],
    ) -> list[tuple[float, str]]:
        # Seeking exactly to container duration is not guaranteed to yield a
        # decoded frame. Keep the deterministic LAST sample safely inside the
        # final encoded interval.
        last = max(0.0, duration - min(0.1, duration / 4.0))
        candidates: dict[float, tuple[int, str]] = {}

        def add(value: float, priority: int, role: str) -> None:
            timestamp = round(min(last, max(0.0, float(value))), 3)
            existing = candidates.get(timestamp)
            if existing is None or priority < existing[0]:
                candidates[timestamp] = (priority, role)

        add(0.0, 0, "FIRST")
        add(last, 0, "LAST")
        add(duration / 2.0, 1, "MIDDLE")
        for raw in suspicious_windows:
            try:
                start = float(raw.get("start_seconds", raw.get("time_seconds", 0)))
                end = float(raw.get("end_seconds", start))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(start) or not math.isfinite(end):
                continue
            if end < start:
                start, end = end, start
            for point in (start, (start + end) / 2.0, end):
                add(point, 0, "SUSPICIOUS_QC_WINDOW")

        desired = min(max_frames, max(3, int(math.ceil(duration * 2.0)) + 1))
        if desired == 1:
            add(0.0, 2, "INTERVAL")
        else:
            for index in range(desired):
                add(last * index / max(1, desired - 1), 2, "INTERVAL")

        selected = sorted(
            candidates.items(), key=lambda item: (item[1][0], item[0])
        )[:max_frames]
        return [
            (timestamp, detail[1])
            for timestamp, detail in sorted(selected, key=lambda item: item[0])
        ]

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
    blocks_final = VISION_BLOCKS_FINAL

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        provider: VisionAnalysisProvider | None = None,
        sampler: VisionFrameSamplingService | None = None,
        registry: CapabilityRegistry | None = None,
        provider_profiles: ProviderProfileService | None = None,
    ) -> None:
        self.repository = repository or ProjectRepository()
        if registry is not None:
            self.registry = registry
        elif provider is not None:
            self.registry = CapabilityRegistry([provider])
        else:
            from .providers.universal_vision import (
                build_universal_vision_providers,
            )

            providers = build_universal_vision_providers(self.repository)
            self.registry = CapabilityRegistry(
                providers or (UnavailableVisionProvider(),)
            )
        self.provider_profiles = provider_profiles or ProviderProfileService(
            self.repository, registry=self.registry
        )
        registered = self.registry.list(CapabilityKind.VISION)
        self.provider = provider or (
            registered[0] if registered else UnavailableVisionProvider()
        )
        self._deterministic = ProductionQCService(self.repository)
        self.sampler = sampler or VisionFrameSamplingService(self.repository)
        self.references = ReferenceAssetService(self.repository)
        self.invocations = AIInvocationService(self.repository)

    def analyze(
        self,
        project_id: str,
        execution_id: str,
        artifact_id: str | None = None,
        *,
        context: Mapping[str, object] | None = None,
        disclosure: ProviderDisclosure | Mapping[str, object] | None = None,
    ) -> VisionQCResult:
        execution = self._deterministic._get_execution(project_id, execution_id)
        artifact = self._deterministic._select_artifact(execution_id, artifact_id)
        provider_name = str(
            getattr(self.provider, "provider_name", "UNCONFIGURED_VISION")
        )
        if artifact is None:
            return self._failure(
                project_id,
                execution_id,
                None,
                provider_name,
                "FAILED",
                "artifact 不存在",
            )
        try:
            resolved = self.provider_profiles.resolve(
                project_id, CapabilityKind.VISION, require_available=True
            )
            provider = self.provider_profiles.provider_for_selection(resolved)
            safe_disclosure = self.provider_profiles.require_disclosure(
                project_id,
                CapabilityKind.VISION,
                disclosure,
                transmitted_content_types=(
                    "VIDEO_ARTIFACT", "SAMPLED_FRAME", "REFERENCE_VERSION"
                ),
            )
            status = provider.status
            provider_name = str(getattr(provider, "provider_name", provider_name))
        except Exception as exc:
            return self._failure(
                project_id,
                execution_id,
                artifact.id,
                provider_name,
                "NOT_RUN" if isinstance(exc, ProviderProfileError) else "FAILED",
                exc,
            )
        if not status.available:
            return self._failure(
                project_id,
                execution_id,
                artifact.id,
                provider_name,
                "NOT_RUN",
                status.reason,
                model_id=str(status.metadata.get("model") or "unavailable"),
            )

        frame_manifest: VisionFrameManifest | None = None
        request: VisionAnalysisRequest | None = None
        invocation_id = uuid4().hex
        started_at = _now()
        model_id = str(status.metadata.get("model") or "unspecified")
        runtime_selection: dict[str, object] = {}
        selection_source = getattr(provider, "runtime_selection", None)
        if callable(selection_source):
            try:
                raw_selection = selection_source()
                safe_selection = sanitize_persistent_metadata(raw_selection)
                if isinstance(safe_selection, Mapping):
                    runtime_selection = dict(safe_selection)
            except Exception:
                runtime_selection = {}
        runtime_plan = (
            self.repository.get_runtime_plan(execution.runtime_plan_id)
            if execution.runtime_plan_id
            else None
        )
        try:
            suspicious = self._suspicious_windows(context)
            frame_manifest = self.sampler.sample(
                project_id,
                execution_id,
                artifact.id,
                suspicious_windows=suspicious,
            )
            request = self._build_request(
                project_id,
                execution,
                artifact,
                frame_manifest,
                context=context,
                provider=provider,
            )
            request_summary = {
                "correlation_id": invocation_id,
                "input_provenance": request.public_dict(),
                "provider_disclosure": safe_disclosure,
                "runtime_selection": runtime_selection,
            }
            self.invocations.record(
                project_id,
                capability="VISION",
                provider_id=provider_name,
                model_id=model_id,
                status="STARTED",
                production_job_id=execution.production_job_id,
                execution_id=execution.id,
                reference_version_ids=request.reference_version_ids,
                generation_brief_hash=request.generation_brief_hash,
                runtime_plan=runtime_plan,
                request_summary=request_summary,
                started_at=started_at,
                invocation_id=f"{invocation_id}-started",
            )
            analysis = provider.analyze(request=request)
            metadata = (
                dict(analysis.metadata)
                if isinstance(analysis.metadata, Mapping)
                else {}
            )
            model_id = str(metadata.get("model") or model_id)
            interaction_id = metadata.get("interaction_id")
            usage = metadata.get("usage")
            self.invocations.record(
                project_id,
                capability="VISION",
                provider_id=analysis.provider,
                model_id=model_id,
                status="SUCCEEDED",
                production_job_id=execution.production_job_id,
                execution_id=execution.id,
                reference_version_ids=request.reference_version_ids,
                generation_brief_hash=request.generation_brief_hash,
                runtime_plan=runtime_plan,
                request_summary=request_summary,
                provider_task_id=(
                    str(interaction_id) if isinstance(interaction_id, str) else None
                ),
                started_at=started_at,
                finished_at=_now(),
                usage=usage if isinstance(usage, Mapping) else {},
                invocation_id=f"{invocation_id}-succeeded",
            )
            record = self._persist_analysis(
                project_id,
                execution_id,
                artifact.id,
                frame_manifest,
                analysis.provider,
                model_id,
                "AI_ANALYSIS",
                dict(analysis.metrics),
                metadata,
                request,
            )
            return VisionQCResult(
                project_id,
                execution_id,
                artifact.id,
                "AI_ANALYSIS",
                analysis.analysis_kind,
                analysis.provider,
                dict(analysis.metrics),
                "",
                record.created_at,
                record.id,
                frame_manifest.id,
            )
        except Exception as exc:
            reason = sanitize_error(exc)
            provider_failure_metadata: dict[str, object] = {}
            raw_failure_metadata = getattr(exc, "safe_metadata", None)
            if isinstance(raw_failure_metadata, Mapping):
                lifecycle = raw_failure_metadata.get("remote_file_lifecycle")
                if isinstance(lifecycle, Mapping):
                    safe_lifecycle = sanitize_persistent_metadata(dict(lifecycle))
                    if isinstance(safe_lifecycle, Mapping):
                        provider_failure_metadata["remote_file_lifecycle"] = dict(
                            safe_lifecycle
                        )
            if request is not None:
                try:
                    failure_summary = {
                        "correlation_id": invocation_id,
                        "input_provenance": request.public_dict(),
                        "error": reason,
                    }
                    failure_summary.update(provider_failure_metadata)
                    self.invocations.record(
                        project_id,
                        capability="VISION",
                        provider_id=provider_name,
                        model_id=model_id,
                        status="FAILED",
                        production_job_id=execution.production_job_id,
                        execution_id=execution.id,
                        reference_version_ids=request.reference_version_ids,
                        generation_brief_hash=request.generation_brief_hash,
                        runtime_plan=runtime_plan,
                        request_summary=failure_summary,
                        started_at=started_at,
                        finished_at=_now(),
                        invocation_id=f"{invocation_id}-failed",
                    )
                except Exception:
                    pass
            return self._failure(
                project_id,
                execution_id,
                artifact.id,
                provider_name,
                "FAILED",
                reason,
                frame_manifest=frame_manifest,
                request=request,
                model_id=model_id,
                provider_metadata=provider_failure_metadata,
            )

    def _build_request(
        self,
        project_id: str,
        execution,
        artifact,
        frame_manifest: VisionFrameManifest,
        *,
        context: Mapping[str, object] | None,
        provider: VisionAnalysisProvider | None = None,
    ) -> VisionAnalysisRequest:
        project_root = (self.repository.paths.projects / project_id).resolve()
        video_path = self._deterministic._resolve_artifact_path(
            project_id, artifact.path
        )
        video_mime = str(artifact.metadata_json.get("mime_type") or "video/mp4")
        video = VisionMediaInput(
            source_kind="VIDEO_ARTIFACT",
            source_id=artifact.id,
            path=video_path,
            mime_type=video_mime,
            sha256=self._sha256(video_path),
            role="GENERATED_SHOT",
        )
        frames: list[VisionMediaInput] = []
        for sample in frame_manifest.samples:
            relative = sample.get("path")
            sha256 = sample.get("sha256")
            if not isinstance(relative, str) or not isinstance(sha256, str):
                raise VisionQCError("Vision frame manifest 无效")
            path = (project_root / relative).resolve()
            if project_root not in path.parents or not path.is_file():
                raise VisionQCError("Vision frame path 不属于该项目")
            frames.append(
                VisionMediaInput(
                    source_kind="SAMPLED_FRAME",
                    source_id=f"{frame_manifest.id}:{sample.get('index')}",
                    path=path,
                    mime_type="image/jpeg",
                    sha256=sha256,
                    role=str(sample.get("role") or "INTERVAL"),
                    time_seconds=float(sample.get("time_seconds") or 0),
                )
            )

        runtime_plan = (
            self.repository.get_runtime_plan(execution.runtime_plan_id)
            if execution.runtime_plan_id
            else None
        )
        if runtime_plan is not None and runtime_plan.project_id != project_id:
            raise VisionQCError("RuntimePlan 不属于该项目")
        snapshot = execution.input_snapshot
        if runtime_plan is not None:
            reference_ids = list(runtime_plan.reference_version_ids)
            roles = dict(runtime_plan.reference_roles)
            generation_brief_hash = runtime_plan.generation_brief_hash
        else:
            raw = dict(snapshot.reference_asset_versions) if snapshot else {}
            reference_ids = list(dict.fromkeys(str(value) for value in raw.values()))
            roles: dict[str, str] = {}
            for key, value in raw.items():
                roles.setdefault(str(value), str(key))
            generation_brief_hash = None
        references: list[VisionMediaInput] = []
        for version_id in reference_ids:
            version = self.repository.get_reference_asset_version(version_id)
            if version is None or version.project_id != project_id:
                raise VisionQCError("Vision reference version 不属于该项目")
            path = self.references.resolve_version_path(project_id, version_id)
            if not path.is_file() or self._sha256(path) != version.sha256:
                raise VisionQCError("Vision reference file/hash 无效")
            references.append(
                VisionMediaInput(
                    source_kind="REFERENCE_VERSION",
                    source_id=version.id,
                    path=path,
                    mime_type=version.mime_type,
                    sha256=version.sha256,
                    role=str(roles.get(version.id) or "REFERENCE"),
                )
            )

        brief = None
        brief_id = execution.generation_brief_id or (
            runtime_plan.generation_brief_id if runtime_plan is not None else None
        )
        if brief_id:
            brief = self.repository.get_generation_brief(brief_id)
            if brief is None or brief.project_id != project_id:
                raise VisionQCError("GenerationBrief 不属于该项目")
            generation_brief_hash = brief.sha256
        creative_context: dict[str, object] = {}
        if brief is not None:
            creative_context = {
                "generation_brief_id": brief.id,
                "shot_id": brief.shot_id,
                "content": brief.model_dump(
                    mode="json",
                    exclude={"id", "project_id", "production_job_id", "created_at"},
                ),
            }
        if context:
            allowed = {
                key: value
                for key, value in context.items()
                if key
                in {
                    "technical_qc_findings",
                    "shot_constraints",
                    "review_focus",
                }
            }
            if allowed:
                creative_context["analysis_context"] = allowed
        sanitized = sanitize_persistent_metadata(creative_context)
        if not isinstance(sanitized, Mapping):
            sanitized = {}
        return VisionAnalysisRequest(
            project_id=project_id,
            execution_id=execution.id,
            artifact_id=artifact.id,
            video=video,
            frames=tuple(frames),
            references=tuple(references),
            frame_manifest_id=frame_manifest.id,
            generation_brief_hash=generation_brief_hash,
            prompt_template_version=str(
                getattr(
                    provider or self.provider,
                    "prompt_template_version",
                    "aidrama-vision-qc-v1",
                )
            ),
            creative_context=dict(sanitized),
        )

    def latest(
        self,
        project_id: str,
        execution_id: str,
    ) -> VisionQCResult | None:
        """Project the latest durable advisory analysis for a cold Review UI."""

        self._deterministic._get_execution(project_id, execution_id)
        records = self.repository.list_vision_analyses(project_id, execution_id)
        if not records:
            return None
        record = records[-1]
        return VisionQCResult(
            project_id=record.project_id,
            execution_id=record.execution_id,
            artifact_id=record.artifact_id,
            status=record.status,
            analysis_kind="AI_ANALYSIS",
            provider=record.provider_id,
            metrics=dict(record.metrics),
            reason=str(record.input_provenance.get("failure_reason") or ""),
            created_at=record.created_at,
            analysis_id=record.id,
            frame_manifest_id=record.frame_manifest_id,
        )

    def _persist_analysis(
        self,
        project_id: str,
        execution_id: str,
        artifact_id: str | None,
        frame_manifest: VisionFrameManifest | None,
        provider_id: str,
        model_id: str,
        status: str,
        metrics: Mapping[str, object],
        metadata: Mapping[str, object],
        request: VisionAnalysisRequest | None,
    ) -> VisionAnalysisRecord:
        prompt_hash = metadata.get("prompt_template_sha256") or getattr(
            self.provider, "prompt_template_sha256", None
        )
        if not isinstance(prompt_hash, str) or len(prompt_hash) != 64:
            prompt_hash = None
        comparison = metadata.get("reference_comparison")
        if not isinstance(comparison, Mapping):
            comparison = {}
        provenance: dict[str, object] = (
            request.public_dict() if request is not None else {}
        )
        for key in (
            "failure_reason",
            "remote_file_lifecycle",
            "runtime_selection",
            "summary",
        ):
            if key in metadata:
                provenance[key] = metadata[key]
        safe_provenance = sanitize_persistent_metadata(provenance)
        safe_metrics = sanitize_persistent_metadata(dict(metrics))
        safe_comparison = sanitize_persistent_metadata(dict(comparison))
        interaction_id = metadata.get("interaction_id")
        record = VisionAnalysisRecord(
            id=uuid4().hex,
            project_id=project_id,
            execution_id=execution_id,
            artifact_id=artifact_id,
            frame_manifest_id=frame_manifest.id if frame_manifest else None,
            provider_id=provider_id,
            model_id=model_id or "unspecified",
            status=status,
            metrics=(safe_metrics if isinstance(safe_metrics, dict) else {}),
            reference_comparison=(
                safe_comparison if isinstance(safe_comparison, dict) else {}
            ),
            reference_version_ids=(
                request.reference_version_ids if request is not None else ()
            ),
            prompt_template_sha256=prompt_hash,
            input_provenance=(
                safe_provenance if isinstance(safe_provenance, dict) else {}
            ),
            provider_interaction_id=(
                str(interaction_id) if isinstance(interaction_id, str) else None
            ),
            created_at=_now(),
        )
        return self.repository.create_vision_analysis(record)

    def _failure(
        self,
        project_id: str,
        execution_id: str,
        artifact_id: str | None,
        provider_id: str,
        status: str,
        reason: object,
        *,
        frame_manifest: VisionFrameManifest | None = None,
        request: VisionAnalysisRequest | None = None,
        model_id: str = "unavailable",
        provider_metadata: Mapping[str, object] | None = None,
    ) -> VisionQCResult:
        safe_reason = sanitize_error(reason)
        metadata: dict[str, object] = dict(provider_metadata or {})
        metadata["failure_reason"] = safe_reason
        metadata["prompt_template_sha256"] = (
            metadata.get("prompt_template_sha256")
            or getattr(self.provider, "prompt_template_sha256", None)
        )
        try:
            record = self._persist_analysis(
                project_id,
                execution_id,
                artifact_id,
                frame_manifest,
                provider_id,
                model_id,
                status,
                {},
                metadata,
                request,
            )
        except Exception:
            record = None
        return VisionQCResult(
            project_id,
            execution_id,
            artifact_id,
            status,
            "AI_ANALYSIS",
            provider_id,
            {},
            safe_reason,
            record.created_at if record else _now(),
            record.id if record else None,
            frame_manifest.id if frame_manifest else None,
        )

    @staticmethod
    def _suspicious_windows(
        context: Mapping[str, object] | None,
    ) -> tuple[Mapping[str, object], ...]:
        if not context:
            return ()
        raw = context.get("suspicious_windows")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return ()
        return tuple(item for item in raw if isinstance(item, Mapping))[:32]

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()


__all__ = [
    "VISION_BLOCKS_FINAL",
    "VisionFrameSamplingService",
    "VisionQCError",
    "VisionQCResult",
    "VisionQCService",
]
