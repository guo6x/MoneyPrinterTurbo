"""Non-blocking UI facade for durable production jobs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping
from uuid import uuid4

from aidrama_studio.domain import ProductionJobStatus, ProviderTask
from aidrama_studio.storage.repositories import ProjectRepository

from .active_work import TERMINAL_PROVIDER_STATES

from .ai_capabilities import (
    CapabilityKind,
    CapabilityRegistry,
    CapabilityUnavailable,
    default_capability_registry,
)
from .production import ProductionService, ProductionServiceError
from .production_orchestrator import ProductionOrchestrator
from .production_execution import ProductionExecutionService, ProductionExecutionServiceError
from .provider_profiles import ProviderProfileError, ProviderProfileService
from .runtime_foundation import (
    GenerationBriefCompiler,
    GenerationBriefService,
    OutputProfileService,
    RuntimeFoundationError,
    RuntimePlanService,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class ProductionQueueError(RuntimeError):
    pass


_SEEDANCE_PROVIDER_IDS = frozenset({"seedance", "seedance_video"})
_SEEDANCE_SUPPORTED_DURATIONS = tuple(float(item) for item in range(4, 31))


@dataclass(frozen=True, slots=True)
class ProductionAuthorizationPreview:
    provider_id: str
    model_id: str
    deployment_region: str
    endpoint_profile_id: str
    endpoint_class: str
    selection_source: str
    shot_count: int
    reference_count: int
    max_paid_attempts: int
    estimated_provider_requests: int
    target_episode_duration_seconds: float
    native_generation_resolution: str
    native_generation_fps: float
    delivery_resolution: str
    target_fps: float
    delivery_strategy: str
    quality_mode: str
    generation_brief_hashes: dict[str, str]
    duration_requests_by_shot: dict[str, int]
    transmitted_content_types: tuple[str, ...]
    authorization_fingerprint: str


class ProductionQueueService:
    """Queue/cancel/read facade; it never performs provider work inline."""

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        production_service: ProductionService | None = None,
        registry: CapabilityRegistry | None = None,
        provider_profiles: ProviderProfileService | None = None,
    ) -> None:
        self.production_service = production_service or ProductionService(repository or ProjectRepository())
        self.repository = self.production_service.repository
        self.registry = registry or default_capability_registry()
        self.provider_profiles = provider_profiles or ProviderProfileService(self.repository, registry=self.registry)
        self.execution_service = ProductionExecutionService(self.repository, production_service=self.production_service)
        self.brief_compiler = GenerationBriefCompiler(self.repository)
        self.generation_briefs = GenerationBriefService(self.repository)
        self.runtime_plans = RuntimePlanService(self.repository)

    def run_job(
        self,
        project_id: str,
        production_job_id: str,
        *,
        authorization: Mapping[str, object] | None = None,
        provider_id: str | None = None,
        endpoint_profile_id: str | None = None,
    ):
        return self.enqueue_job(
            project_id,
            production_job_id,
            authorization=authorization,
            provider_id=provider_id,
            endpoint_profile_id=endpoint_profile_id,
        )

    resume_job = run_job

    def list_provider_options(
        self,
        project_id: str,
        capability: CapabilityKind | str = CapabilityKind.VIDEO_GENERATIVE,
    ) -> tuple[dict[str, object], ...]:
        canonical = self.provider_profiles.resolve(project_id, capability)
        canonical_endpoint = (
            canonical.profile.endpoint_profile_id if canonical.profile is not None else None
        )
        options: list[dict[str, object]] = []
        for profile in self.provider_profiles.inventory(project_id, capability):
            resolved = self.provider_profiles.resolve(
                project_id,
                capability,
                endpoint_profile_id=profile.endpoint_profile_id,
            )
            if not resolved.configured:
                continue
            options.append(
                {
                    "provider_id": profile.provider_id,
                    "model_id": profile.model_id,
                    "endpoint_profile_id": profile.endpoint_profile_id,
                    "deployment_region": profile.deployment_region.value,
                    "endpoint_class": profile.endpoint_class,
                    "available": resolved.available,
                    "verified": resolved.verified,
                    "default": profile.endpoint_profile_id == canonical_endpoint,
                }
            )
        options.sort(
            key=lambda item: (
                0 if item["default"] else 1,
                str(item["provider_id"]),
                str(item["model_id"]),
                str(item["endpoint_profile_id"]),
            )
        )
        return tuple(options)

    def preview_authorization(
        self,
        project_id: str,
        production_job_id: str,
        *,
        provider_id: str | None = None,
        endpoint_profile_id: str | None = None,
        max_paid_attempts: int = 1,
    ) -> ProductionAuthorizationPreview:
        if isinstance(max_paid_attempts, bool) or not 1 <= int(max_paid_attempts) <= 3:
            raise ProductionQueueError("每个镜头最大付费尝试次数必须为 1–3")
        try:
            job = self.production_service.get_job(project_id, production_job_id)
            readiness = self.production_service.validate_job_readiness(project_id, job.shot_plan_revision_id)
            if not readiness.get("ready"):
                raise ProductionQueueError("ProductionJob 尚未 READY")
            shots = self.production_service.create_production_shots(project_id, job.id)
            resolved = self.provider_profiles.resolve(
                project_id,
                CapabilityKind.VIDEO_GENERATIVE,
                provider_id=provider_id,
                endpoint_profile_id=endpoint_profile_id,
                require_available=True,
            )
            if resolved.profile is None or not resolved.available:
                raise CapabilityUnavailable(resolved.detail)
            profile = resolved.profile
            snapshot = self.execution_service.create_input_snapshot(project_id, job.id)
        except (ProductionServiceError, ProviderProfileError, CapabilityUnavailable) as exc:
            raise ProductionQueueError(str(exc)) from exc
        minimum, maximum = self._duration_limits(
            profile.profile, provider_id=profile.provider_id
        )
        allowed_durations = self._allowed_durations(
            profile.profile, provider_id=profile.provider_id
        )
        request_counts: dict[str, int] = {}
        shot_revision = self.repository.get_shot_revision(job.shot_plan_revision_id)
        if shot_revision is None:
            raise ProductionQueueError("ProductionJob 缺少 Shot Plan revision")
        durations = {shot.id: float(shot.duration_seconds) for shot in shot_revision["content"].shots}
        for production_shot in shots:
            duration = durations.get(production_shot.shot_id)
            if duration is None:
                raise ProductionQueueError(f"ProductionShot 缺少冻结时长: {production_shot.shot_id}")
            duration_plan = self.provider_profiles.plan_duration(
                duration,
                minimum=minimum,
                maximum=maximum,
                allowed_durations=allowed_durations,
            )
            if len(duration_plan.chunks) != 1:
                raise ProductionQueueError(
                    f"镜头 {production_shot.shot_id} 需要 {len(duration_plan.chunks)} 次 provider 请求；"
                    "当前 V1 单镜头执行只允许一次已授权请求，请先调整镜头时长"
                )
            request_counts[production_shot.shot_id] = 1
        count = len(shots)
        reference_count = len(set(snapshot.reference_asset_versions.values()))
        content_types = ("TEXT", "REFERENCE_IMAGE") if reference_count else ("TEXT",)
        estimated_requests = sum(request_counts.values()) * int(max_paid_attempts)
        output_profile = self._output_profile(job)
        generation_briefs = self.generation_briefs.prepare_for_job(project_id, job.id)
        generation_brief_hashes = {
            item.shot_id: item.sha256 for item in generation_briefs
        }
        native_resolution = self._native_resolution(profile.profile, output_profile)
        native_fps = self._native_fps(profile.profile)
        delivery_resolution = output_profile.target_resolution
        native_pixels = self._pixels(native_resolution)
        delivery_pixels = output_profile.delivery_width * output_profile.delivery_height
        delivery_strategy = (
            "NATIVE" if native_resolution == delivery_resolution
            else "DETERMINISTIC_UPSCALE" if delivery_pixels > native_pixels
            else "DETERMINISTIC_SCALE"
        )
        fingerprint_payload = {
            "project_id": project_id,
            "production_job_id": production_job_id,
            "provider_id": profile.provider_id,
            "model_id": profile.model_id,
            "deployment_region": profile.deployment_region.value,
            "endpoint_profile_id": profile.endpoint_profile_id,
            "endpoint_class": profile.endpoint_class,
            "selection_source": resolved.source,
            "reference_count": reference_count,
            "estimated_provider_requests": estimated_requests,
            "output_profile_id": output_profile.id,
            "output_profile_version": output_profile.version_number,
            "target_episode_duration_seconds": output_profile.target_episode_duration_seconds,
            "native_generation_resolution": native_resolution,
            "native_generation_fps": native_fps,
            "delivery_resolution": delivery_resolution,
            "target_fps": output_profile.target_fps,
            "delivery_strategy": delivery_strategy,
            "quality_mode": output_profile.quality_mode,
            "generation_brief_hashes": generation_brief_hashes,
            "transmitted_content_types": list(content_types),
            "disclosure_version": "regional-provider-v1",
        }
        return ProductionAuthorizationPreview(
            provider_id=profile.provider_id,
            model_id=profile.model_id,
            deployment_region=profile.deployment_region.value,
            endpoint_profile_id=profile.endpoint_profile_id,
            endpoint_class=profile.endpoint_class,
            selection_source=resolved.source,
            shot_count=count,
            reference_count=reference_count,
            max_paid_attempts=int(max_paid_attempts),
            estimated_provider_requests=estimated_requests,
            target_episode_duration_seconds=output_profile.target_episode_duration_seconds,
            native_generation_resolution=native_resolution,
            native_generation_fps=native_fps,
            delivery_resolution=delivery_resolution,
            target_fps=output_profile.target_fps,
            delivery_strategy=delivery_strategy,
            quality_mode=output_profile.quality_mode,
            generation_brief_hashes=generation_brief_hashes,
            duration_requests_by_shot=request_counts,
            transmitted_content_types=content_types,
            authorization_fingerprint=hashlib.sha256(
                json.dumps(
                    fingerprint_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        )

    def enqueue_job(
        self,
        project_id: str,
        production_job_id: str,
        *,
        authorization: Mapping[str, object] | None = None,
        provider_id: str | None = None,
        endpoint_profile_id: str | None = None,
    ) -> ProviderTask:
        try:
            job = self.production_service.get_job(project_id, production_job_id)
            readiness = self.production_service.validate_job_readiness(project_id, job.shot_plan_revision_id)
            if not readiness.get("ready"):
                raise ProductionQueueError("ProductionJob 尚未 READY")
            self.production_service.create_production_shots(project_id, job.id)
        except ProductionServiceError as exc:
            raise ProductionQueueError(str(exc)) from exc
        active = [task for task in self.repository.list_provider_tasks(project_id) if task.execution_id is None and task.request_summary.get("production_job_id") == job.id and task.state in {"QUEUED", "RUNNING", "PAUSED", "RECONCILIATION_REQUIRED"}]
        if active:
            return active[-1]
        authorization = dict(authorization or {})
        if authorization.get("approved") is not True:
            raise ProductionQueueError("开始生成前必须明确批准本次有界付费请求")
        max_paid_attempts = authorization.get("max_paid_attempts", 1)
        try:
            max_paid_attempts = int(max_paid_attempts)
        except (TypeError, ValueError) as exc:
            raise ProductionQueueError("max_paid_attempts 无效") from exc
        preview = self.preview_authorization(
            project_id,
            production_job_id,
            provider_id=provider_id,
            endpoint_profile_id=endpoint_profile_id,
            max_paid_attempts=max_paid_attempts,
        )
        # Bind the approval to the exact provider/model/request bound shown to
        # the user.  Callers cannot approve one preview and silently execute a
        # different provider or a larger request budget.
        for key, expected in (
            ("provider_id", preview.provider_id),
            ("model_id", preview.model_id),
            ("deployment_region", preview.deployment_region),
            ("endpoint_profile_id", preview.endpoint_profile_id),
            ("endpoint_class", preview.endpoint_class),
            ("reference_count", preview.reference_count),
            ("estimated_provider_requests", preview.estimated_provider_requests),
            ("target_episode_duration_seconds", preview.target_episode_duration_seconds),
            ("native_generation_resolution", preview.native_generation_resolution),
            ("native_generation_fps", preview.native_generation_fps),
            ("delivery_resolution", preview.delivery_resolution),
            ("target_fps", preview.target_fps),
            ("delivery_strategy", preview.delivery_strategy),
            ("quality_mode", preview.quality_mode),
            ("authorization_fingerprint", preview.authorization_fingerprint),
        ):
            supplied = authorization.get(key)
            if key == "authorization_fingerprint" and supplied is None:
                raise ProductionQueueError("付费授权缺少区域感知 selection fingerprint，请重新确认")
            if supplied is not None and supplied != expected:
                raise ProductionQueueError(f"付费授权与当前 {key} 不匹配，请重新确认")
        authorization_id = str(authorization.get("authorization_id") or uuid4().hex)
        authorized_at = str(authorization.get("authorized_at") or _now())
        frozen_authorization = {
            "approved": True,
            "authorization_id": authorization_id,
            "authorized_at": authorized_at,
            "max_paid_attempts": preview.max_paid_attempts,
            "estimated_provider_requests": preview.estimated_provider_requests,
            "provider_id": preview.provider_id,
            "model_id": preview.model_id,
            "deployment_region": preview.deployment_region,
            "endpoint_profile_id": preview.endpoint_profile_id,
            "endpoint_class": preview.endpoint_class,
            "selection_source": preview.selection_source,
            "reference_count": preview.reference_count,
            "target_episode_duration_seconds": preview.target_episode_duration_seconds,
            "native_generation_resolution": preview.native_generation_resolution,
            "native_generation_fps": preview.native_generation_fps,
            "delivery_resolution": preview.delivery_resolution,
            "target_fps": preview.target_fps,
            "delivery_strategy": preview.delivery_strategy,
            "quality_mode": preview.quality_mode,
            "generation_brief_hashes": preview.generation_brief_hashes,
            "transmitted_content_types": list(preview.transmitted_content_types),
            "disclosure_version": "regional-provider-v1",
            "authorization_fingerprint": preview.authorization_fingerprint,
        }
        try:
            provider_profile = self.provider_profiles.select(
                project_id,
                CapabilityKind.VIDEO_GENERATIVE,
                provider_id=preview.provider_id,
                endpoint_profile_id=preview.endpoint_profile_id,
            )
            snapshot = self.execution_service.create_input_snapshot(project_id, job.id)
            shot_plan = self.repository.get_shot_revision(job.shot_plan_revision_id)
            if shot_plan is None:
                raise ProductionQueueError("ProductionJob 缺少 Shot Plan revision")
            shot_by_id = {shot.id: shot for shot in shot_plan["content"].shots}
            minimum, maximum = self._duration_limits(
                provider_profile.profile, provider_id=provider_profile.provider_id
            )
            allowed_durations = self._allowed_durations(
                provider_profile.profile, provider_id=provider_profile.provider_id
            )
            plan_ids: dict[str, str] = {}
            for production_shot in self.repository.list_production_shots(job.id):
                shot = shot_by_id.get(production_shot.shot_id)
                if shot is None:
                    raise ProductionQueueError(f"ProductionShot 不属于冻结 Shot Plan: {production_shot.shot_id}")
                brief = self.generation_briefs.current(project_id, job.id, shot.id)
                duration_plan = self.provider_profiles.plan_duration(
                    float(shot.duration_seconds), minimum=minimum, maximum=maximum,
                    allowed_durations=allowed_durations,
                )
                references = self._shot_references(snapshot.reference_asset_versions, brief)
                plan = self.runtime_plans.create(
                    project_id,
                    production_job_id=job.id,
                    brief=brief,
                    provider_capability=CapabilityKind.VIDEO_GENERATIVE.value,
                    provider_id=preview.provider_id,
                    model_id=preview.model_id,
                    endpoint_profile_id=preview.endpoint_profile_id,
                    deployment_region=preview.deployment_region,
                    endpoint_class=preview.endpoint_class,
                    credential_reference=provider_profile.credential_reference,
                    selection_source=preview.selection_source,
                    transmitted_content_types=preview.transmitted_content_types,
                    estimated_request_count=1,
                    generation_mode=(
                        "image_to_video" if references else "text_to_video"
                    ),
                    resolution=preview.native_generation_resolution,
                    native_generation_fps=preview.native_generation_fps,
                    provider_generation_duration=duration_plan.provider_duration_seconds,
                    target_creative_duration=duration_plan.target_creative_duration_seconds,
                    duration_strategy=duration_plan.strategy,
                    audio_strategy=str(provider_profile.profile.get("audio_strategy") or "EXTERNAL_TTS"),
                    provider_parameters={
                        key: value
                        for key, value in provider_profile.profile.items()
                        if key not in {"api_key", "authorization", "token", "secret"}
                    },
                    reference_version_ids=tuple(references.values()),
                    reference_roles={version_id: binding for binding, version_id in references.items()},
                    continuity_strategy=str(provider_profile.profile.get("continuity_strategy") or "REFERENCE_ONLY"),
                    authorization=frozen_authorization,
                    prompt_template_version=str(provider_profile.profile.get("prompt_template_version") or "v1"),
                )
                plan_ids[shot.id] = plan.id
        except (
            ProductionExecutionServiceError,
            ProviderProfileError,
            CapabilityUnavailable,
            RuntimeFoundationError,
        ) as exc:
            raise ProductionQueueError(str(exc)) from exc
        attempt_number = 1 + sum(1 for task in self.repository.list_provider_tasks(project_id) if task.execution_id is None and task.request_summary.get("production_job_id") == job.id)
        now = _now()
        task = self.repository.create_provider_task(ProviderTask(
            id=uuid4().hex, project_id=project_id, execution_id=None,
            capability=CapabilityKind.VIDEO_GENERATIVE.value,
            provider_id=preview.provider_id,
            model_id=preview.model_id,
            idempotency_key=f"production-job:{job.id}:attempt:{attempt_number}", state="QUEUED",
            request_summary={
                "production_job_id": job.id,
                "attempt_number": attempt_number,
                "runtime_plan_ids_by_shot": plan_ids,
                "provider_profile_id": provider_profile.id,
                "endpoint_profile_id": preview.endpoint_profile_id,
                "deployment_region": preview.deployment_region,
                "endpoint_class": preview.endpoint_class,
                "shot_count": preview.shot_count,
                **frozen_authorization,
            },
            created_at=now, updated_at=now,
        ))
        if job.status not in {ProductionJobStatus.QUEUED, ProductionJobStatus.RUNNING}:
            self.repository.update_production_job_status(job.id, ProductionJobStatus.QUEUED, updated_at=now)
        return task

    def prepare_generation_briefs(self, project_id: str, production_job_id: str):
        return self.generation_briefs.prepare_for_job(project_id, production_job_id)

    def save_generation_brief_override(
        self,
        project_id: str,
        production_job_id: str,
        shot_id: str,
        patch: Mapping[str, object],
        *,
        base_brief_id: str | None = None,
    ):
        return self.generation_briefs.create_override(
            project_id,
            production_job_id,
            shot_id,
            patch,
            base_brief_id=base_brief_id,
        )

    def _output_profile(self, job):
        profile = (
            self.repository.get_output_profile(job.output_profile_id)
            if job.output_profile_id else None
        )
        if profile is None:
            profile = OutputProfileService(self.repository).ensure_for_job(
                job.project_id, job.id
            )
        return profile

    @staticmethod
    def _pixels(resolution: str) -> int:
        try:
            width, height = (int(part) for part in resolution.lower().split("x", 1))
        except (TypeError, ValueError) as exc:
            raise ProductionQueueError("Provider native resolution 无效") from exc
        return width * height

    @staticmethod
    def _native_fps(profile: Mapping[str, object]) -> float:
        value = profile.get("native_fps", profile.get("provider_fps", 24.0))
        try:
            fps = float(value)
        except (TypeError, ValueError) as exc:
            raise ProductionQueueError("Provider native FPS capability 无效") from exc
        if fps <= 0 or fps > 240:
            raise ProductionQueueError("Provider native FPS capability 无效")
        return fps

    @staticmethod
    def _native_resolution(profile: Mapping[str, object], output_profile) -> str:
        raw = (
            profile.get("native_generation_resolution")
            or profile.get("provider_resolution")
            or profile.get("wan_resolution")
            or profile.get("resolution")
        )
        supported = profile.get("supported_native_resolutions")
        if raw is None and isinstance(supported, (list, tuple)) and supported:
            raw = supported[-1]
        # Legacy local/mock profiles predate capability metadata. Their
        # explicit compatibility ceiling is 1080p and is shown as such; a 4K
        # delivery is therefore never misrepresented as native 4K.
        raw_text = str(raw or "1080p").strip()
        if "x" in raw_text.lower():
            try:
                width, height = (
                    int(part) for part in raw_text.lower().split("x", 1)
                )
            except (TypeError, ValueError) as exc:
                raise ProductionQueueError("Provider native resolution capability 无效") from exc
            if width < 16 or height < 16:
                raise ProductionQueueError("Provider native resolution capability 无效")
            return f"{width}x{height}"
        normalized = raw_text.lower()
        labels = {"720p": "720p", "1080p": "1080p", "1440p": "1440p", "4k": "4K"}
        label = labels.get(normalized)
        if label is None:
            raise ProductionQueueError("Provider native resolution capability 无效")
        width, height = OutputProfileService.dimensions_for(
            label, output_profile.aspect_ratio
        )
        return f"{width}x{height}"

    def cancel_job(self, project_id: str, production_job_id: str, reason: str = "user"):
        job = self.production_service.get_job(project_id, production_job_id)
        tasks = [task for task in self.repository.list_provider_tasks(project_id) if task.execution_id is None and task.request_summary.get("production_job_id") == job.id]
        if tasks:
            task = tasks[-1]
            if task.state == "QUEUED":
                self.repository.update_provider_task(task.model_copy(update={"state": "CANCELLED", "metadata": dict(task.metadata) | {"cancel_reason": reason}, "updated_at": _now()}))
            elif task.state not in TERMINAL_PROVIDER_STATES:
                self.repository.update_provider_task(task.model_copy(update={"state": "RECONCILIATION_REQUIRED", "metadata": dict(task.metadata) | {"cancel_requested": True, "cancel_reason": reason}, "updated_at": _now()}))
        if job.status not in {ProductionJobStatus.SUCCEEDED, ProductionJobStatus.FAILED, ProductionJobStatus.CANCELLED}:
            self.repository.update_production_job_status(job.id, ProductionJobStatus.CANCELLED, updated_at=_now())
        return self.production_service.get_job(project_id, job.id)

    def get_job_progress(self, project_id: str, production_job_id: str) -> dict[str, object]:
        # This is a pure persisted-state projection; no adapter is required.
        return ProductionOrchestrator(production_service=self.production_service).get_job_progress(project_id, production_job_id)

    @staticmethod
    def _duration_limits(
        profile: Mapping[str, object],
        *,
        provider_id: str | None = None,
    ) -> tuple[float, float]:
        provider_value = str(
            provider_id or profile.get("provider_id") or ""
        ).strip().casefold()
        if provider_value in _SEEDANCE_PROVIDER_IDS:
            ProductionQueueService._validate_seedance_duration_profile(profile)
            return 4.0, 30.0
        raw = profile.get("supported_durations")
        values: list[float] = []
        if isinstance(raw, (list, tuple)):
            for item in raw:
                try:
                    value = float(item)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    values.append(value)
        if values:
            return min(values), max(values)
        try:
            minimum = float(profile.get("minimum_duration_seconds", 2.0))
            maximum = float(profile.get("maximum_duration_seconds", 15.0))
        except (TypeError, ValueError) as exc:
            raise ProductionQueueError("Provider duration profile 无效") from exc
        return minimum, maximum

    @staticmethod
    def _allowed_durations(
        profile: Mapping[str, object],
        *,
        provider_id: str | None = None,
    ) -> tuple[float, ...]:
        provider_value = str(
            provider_id or profile.get("provider_id") or ""
        ).strip().casefold()
        if provider_value in _SEEDANCE_PROVIDER_IDS:
            ProductionQueueService._validate_seedance_duration_profile(profile)
            return _SEEDANCE_SUPPORTED_DURATIONS
        raw = profile.get("supported_durations")
        if not isinstance(raw, (list, tuple)):
            return ()
        values: list[float] = []
        for item in raw:
            try:
                value = float(item)
            except (TypeError, ValueError):
                raise ProductionQueueError("Provider supported_durations 无效")
            if value <= 0:
                raise ProductionQueueError("Provider supported_durations 无效")
            values.append(value)
        return tuple(sorted(set(values)))

    @staticmethod
    def _validate_seedance_duration_profile(
        profile: Mapping[str, object],
    ) -> None:
        """Require the official Seedance 2.5 profile without fallback."""

        if profile.get("requires_explicit_selection") is not True:
            raise ProductionQueueError(
                "Seedance 必须通过显式 Provider 选择，且不得使用通用 duration fallback"
            )
        if profile.get("minimum_duration_seconds") != 4:
            raise ProductionQueueError("Seedance duration profile 必须从 4 秒开始")
        if profile.get("maximum_duration_seconds") != 30:
            raise ProductionQueueError("Seedance duration profile 必须支持到 30 秒")
        if profile.get("supported_durations") != [
            int(item) for item in _SEEDANCE_SUPPORTED_DURATIONS
        ]:
            raise ProductionQueueError(
                "Seedance supported_durations 必须是 4–30 秒的整数集合"
            )

    @staticmethod
    def _shot_references(reference_versions: Mapping[str, str], brief) -> dict[str, str]:
        character_ids = {
            str(item.get("id"))
            for item in brief.character_context
            if isinstance(item, Mapping) and item.get("id")
        }
        location_id = str(brief.location_context.get("id") or "")
        allowed = {f"CHARACTER:{item}" for item in character_ids}
        if location_id:
            allowed.add(f"LOCATION:{location_id}")
        allowed.add(f"SHOT:{brief.shot_id}")
        # Style/prop references are project-level creative constraints and are
        # relevant to every shot. Character/location references are selected
        # by the exact frozen GenerationBrief, never alphabetically.
        return {
            str(binding): str(version_id)
            for binding, version_id in reference_versions.items()
            if str(binding) in allowed
            or str(binding).startswith("STYLE:")
            or str(binding).startswith("PROP:")
        }


__all__ = ["ProductionAuthorizationPreview", "ProductionQueueError", "ProductionQueueService"]
