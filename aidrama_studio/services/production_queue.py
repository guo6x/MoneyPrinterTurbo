"""Non-blocking UI facade for durable production jobs."""

from __future__ import annotations

import hashlib
import json
import math
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
from .production_reliability import PaidBudgetError, PaidBudgetService
from .provider_profiles import ProviderProfileError, ProviderProfileService
from .model_settings import SettingsModelService
from .model_runtime import CapabilityKind as UniversalCapabilityKind
from .credentials import CredentialStoreError, WindowsCredentialStore
from .runtime_foundation import (
    GenerationBriefCompiler,
    GenerationBriefService,
    OutputProfileService,
    RuntimeFoundationError,
    RuntimePlanService,
)
from .shot_keyframe import (
    ShotKeyframeError,
    ShotKeyframeReadinessError,
    ShotKeyframeService,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class ProductionQueueError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProductionAuthorizationPreview:
    provider_profile_id: str
    provider_id: str
    model_id: str
    manifest_id: str | None
    manifest_hash: str | None
    codec_id: str | None
    deployment_region: str
    endpoint_profile_id: str
    endpoint_class: str
    selection_source: str
    shot_count: int
    reference_count: int
    first_frame_count: int
    first_frame_fingerprints: dict[str, dict[str, str]]
    pre_live_first_frame_gate: str
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

    LOCAL_MAX_PAID_CREATES_PER_TICK = 8

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        production_service: ProductionService | None = None,
        registry: CapabilityRegistry | None = None,
        provider_profiles: ProviderProfileService | None = None,
        settings_service: SettingsModelService | None = None,
    ) -> None:
        self.production_service = production_service or ProductionService(repository or ProjectRepository())
        self.repository = self.production_service.repository
        self.registry = registry or default_capability_registry()
        self.provider_profiles = provider_profiles or ProviderProfileService(self.repository, registry=self.registry)
        if settings_service is None:
            try:
                credential_store = WindowsCredentialStore(self.repository.paths.root)
            except CredentialStoreError:
                credential_store = None
            settings_service = SettingsModelService(
                self.repository,
                credential_store=credential_store,
            )
        self.settings = settings_service
        self.execution_service = ProductionExecutionService(self.repository, production_service=self.production_service)
        self.brief_compiler = GenerationBriefCompiler(self.repository)
        self.generation_briefs = GenerationBriefService(self.repository)
        self.runtime_plans = RuntimePlanService(self.repository)
        self.paid_budgets = PaidBudgetService(self.repository)
        self.shot_keyframes = ShotKeyframeService(self.repository)

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
        canonical_profile_id = (
            canonical.profile.id if canonical.profile is not None else None
        )
        options: list[dict[str, object]] = []
        for profile in self.provider_profiles.inventory(project_id, capability):
            resolved = self.provider_profiles.resolve(
                project_id,
                capability,
                endpoint_profile_id=profile.id,
            )
            if not resolved.configured:
                continue
            options.append(
                {
                    "provider_id": profile.provider_id,
                    "model_id": profile.model_id,
                    "provider_profile_id": profile.id,
                    "endpoint_profile_id": profile.endpoint_profile_id,
                    "deployment_region": profile.deployment_region.value,
                    "endpoint_class": profile.endpoint_class,
                    "available": resolved.available,
                    "verified": resolved.verified,
                    "default": profile.id == canonical_profile_id,
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
            snapshot = self.execution_service.create_input_snapshot(project_id, job.id)
            generation_briefs = self.generation_briefs.prepare_for_job(
                project_id, job.id
            )
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
            requires_first_frame = self._requires_shot_first_frame(resolved)
            if requires_first_frame:
                (
                    pre_live_report,
                    snapshot,
                    first_frame_fingerprints,
                ) = self._freeze_pre_live_snapshot(
                    project_id,
                    job.id,
                    snapshot,
                    generation_briefs=generation_briefs,
                )
                pre_live_first_frame_gate = pre_live_report.gate.value
            else:
                first_frame_fingerprints = {}
                pre_live_first_frame_gate = "NOT_REQUIRED"
        except (
            ProductionServiceError,
            ProductionExecutionServiceError,
            ProviderProfileError,
            CapabilityUnavailable,
            RuntimeFoundationError,
            ShotKeyframeError,
        ) as exc:
            raise ProductionQueueError(str(exc)) from exc
        minimum, maximum = self._duration_limits(profile.profile)
        allowed_durations = self._allowed_durations(profile.profile)
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
        first_frame_count = len(first_frame_fingerprints)
        content_types = (
            ("TEXT", "SHOT_FIRST_FRAME")
            if requires_first_frame
            else (("TEXT", "REFERENCE_IMAGE") if reference_count else ("TEXT",))
        )
        estimated_requests = sum(request_counts.values()) * int(max_paid_attempts)
        output_profile = self._output_profile(job)
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
            "provider_profile_id": profile.id,
            "provider_id": profile.provider_id,
            "model_id": profile.model_id,
            "manifest_id": profile.profile.get("manifest_id"),
            "manifest_hash": profile.profile.get("manifest_hash"),
            "codec_id": profile.profile.get("codec_id"),
            "deployment_region": profile.deployment_region.value,
            "endpoint_profile_id": profile.endpoint_profile_id,
            "endpoint_class": profile.endpoint_class,
            "selection_source": resolved.source,
            "reference_count": reference_count,
            "first_frame_count": first_frame_count,
            "first_frame_fingerprints": first_frame_fingerprints,
            "pre_live_first_frame_gate": pre_live_first_frame_gate,
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
            provider_profile_id=profile.id,
            provider_id=profile.provider_id,
            model_id=profile.model_id,
            manifest_id=str(profile.profile.get("manifest_id") or "") or None,
            manifest_hash=str(profile.profile.get("manifest_hash") or "") or None,
            codec_id=str(profile.profile.get("codec_id") or "") or None,
            deployment_region=profile.deployment_region.value,
            endpoint_profile_id=profile.endpoint_profile_id,
            endpoint_class=profile.endpoint_class,
            selection_source=resolved.source,
            shot_count=count,
            reference_count=reference_count,
            first_frame_count=first_frame_count,
            first_frame_fingerprints=first_frame_fingerprints,
            pre_live_first_frame_gate=pre_live_first_frame_gate,
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
        authorization = dict(authorization or {})
        max_paid_attempts = authorization.get(
            "max_paid_attempts",
            active[-1].request_summary.get("max_paid_attempts", 1) if active else 1,
        )
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
        if active:
            current = active[-1]
            if (
                current.request_summary.get("authorization_fingerprint")
                != preview.authorization_fingerprint
                or current.request_summary.get("first_frame_fingerprints")
                != preview.first_frame_fingerprints
            ):
                raise ProductionQueueError(
                    "已有 Production task 与当前 Shot First Frame truth 不匹配；"
                    "不会替换或重复提交付费请求"
                )
            return current
        if authorization.get("approved") is not True:
            raise ProductionQueueError("开始生成前必须明确批准本次有界付费请求")
        # Bind the approval to the exact provider/model/request bound shown to
        # the user.  Callers cannot approve one preview and silently execute a
        # different provider or a larger request budget.
        for key, expected in (
            ("provider_profile_id", preview.provider_profile_id),
            ("provider_id", preview.provider_id),
            ("model_id", preview.model_id),
            ("manifest_id", preview.manifest_id),
            ("manifest_hash", preview.manifest_hash),
            ("codec_id", preview.codec_id),
            ("deployment_region", preview.deployment_region),
            ("endpoint_profile_id", preview.endpoint_profile_id),
            ("endpoint_class", preview.endpoint_class),
            ("reference_count", preview.reference_count),
            ("first_frame_count", preview.first_frame_count),
            ("first_frame_fingerprints", preview.first_frame_fingerprints),
            ("pre_live_first_frame_gate", preview.pre_live_first_frame_gate),
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
            "provider_profile_id": preview.provider_profile_id,
            "provider_id": preview.provider_id,
            "model_id": preview.model_id,
            "manifest_id": preview.manifest_id,
            "manifest_hash": preview.manifest_hash,
            "codec_id": preview.codec_id,
            "deployment_region": preview.deployment_region,
            "endpoint_profile_id": preview.endpoint_profile_id,
            "endpoint_class": preview.endpoint_class,
            "selection_source": preview.selection_source,
            "reference_count": preview.reference_count,
            "first_frame_count": preview.first_frame_count,
            "first_frame_fingerprints": preview.first_frame_fingerprints,
            "pre_live_first_frame_gate": preview.pre_live_first_frame_gate,
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
        # Re-read and physically validate every exact frame immediately before
        # any durable intent or budget mutation. A changed frame can never
        # reuse an earlier paid authorization fingerprint.
        try:
            pre_intent_snapshot = self.execution_service.create_input_snapshot(
                project_id, job.id
            )
            generation_briefs = self.generation_briefs.prepare_for_job(
                project_id, job.id
            )
            if preview.pre_live_first_frame_gate == "NOT_REQUIRED":
                frozen_snapshot = pre_intent_snapshot
                pre_intent_fingerprints = {}
            else:
                (
                    _pre_intent_report,
                    frozen_snapshot,
                    pre_intent_fingerprints,
                ) = self._freeze_pre_live_snapshot(
                    project_id,
                    job.id,
                    pre_intent_snapshot,
                    generation_briefs=generation_briefs,
                )
        except (
            ProductionExecutionServiceError,
            RuntimeFoundationError,
            ShotKeyframeError,
        ) as exc:
            raise ProductionQueueError(str(exc)) from exc
        if pre_intent_fingerprints != preview.first_frame_fingerprints:
            raise ProductionQueueError(
                "Shot First Frame truth 在授权确认期间发生变化，请重新预览并确认"
            )
        # Persist the UI/production action intent before preparing plans. The
        # stable authorization fingerprint makes double-click, page refresh,
        # and repeated resume() converge on this one durable record.
        intent_key = (
            f"production-job:{job.id}:authorization:"
            f"{preview.authorization_fingerprint}"
        )
        intent_now = _now()
        intent, created_intent = self.repository.get_or_create_provider_task(
            ProviderTask(
                id=uuid4().hex,
                project_id=project_id,
                execution_id=None,
                capability=CapabilityKind.VIDEO_GENERATIVE.value,
                provider_id=preview.provider_id,
                model_id=preview.model_id,
                idempotency_key=intent_key,
                state="PREPARING",
                request_summary={
                    "production_job_id": job.id,
                    "attempt_number": 1,
                    **frozen_authorization,
                },
                created_at=intent_now,
                updated_at=intent_now,
            )
        )
        if not created_intent and intent.state != "PREPARING":
            return intent
        if not created_intent:
            frozen_authorization = {
                key: intent.request_summary[key]
                for key in frozen_authorization
                if key in intent.request_summary
            }
        try:
            self.paid_budgets.authorize_job(
                project_id,
                job.id,
                authorization_fingerprint=preview.authorization_fingerprint,
                planned_creates=preview.estimated_provider_requests,
                authorized_max=preview.estimated_provider_requests,
            )
        except PaidBudgetError as exc:
            raise ProductionQueueError(str(exc)) from exc
        try:
            provider_profile = self.provider_profiles.select(
                project_id,
                CapabilityKind.VIDEO_GENERATIVE,
                provider_id=preview.provider_id,
                endpoint_profile_id=preview.provider_profile_id,
            )
            self._assert_preview_profile(preview, provider_profile)
            shot_plan = self.repository.get_shot_revision(job.shot_plan_revision_id)
            if shot_plan is None:
                raise ProductionQueueError("ProductionJob 缺少 Shot Plan revision")
            generation_input = shot_plan.get("generation_input") or {}
            raw_batch_size = generation_input.get(
                "max_execution_batch_size",
                self.LOCAL_MAX_PAID_CREATES_PER_TICK,
            )
            if isinstance(raw_batch_size, bool):
                raise ProductionQueueError("max_execution_batch_size 无效")
            try:
                requested_batch_size = int(raw_batch_size)
            except (TypeError, ValueError) as exc:
                raise ProductionQueueError("max_execution_batch_size 无效") from exc
            if requested_batch_size <= 0:
                raise ProductionQueueError("max_execution_batch_size 必须大于 0")
            max_paid_creates_per_tick = min(
                requested_batch_size,
                self.LOCAL_MAX_PAID_CREATES_PER_TICK,
            )
            shot_by_id = {shot.id: shot for shot in shot_plan["content"].shots}
            minimum, maximum = self._duration_limits(provider_profile.profile)
            allowed_durations = self._allowed_durations(provider_profile.profile)
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
                references = self._shot_references(
                    frozen_snapshot.reference_asset_versions, brief
                )
                first_frame_fingerprint = None
                if preview.pre_live_first_frame_gate != "NOT_REQUIRED":
                    first_frame = frozen_snapshot.first_frame_for_shot(shot.id)
                    if first_frame is None:
                        raise ProductionQueueError(
                            "PRE_LIVE_FIRST_FRAME_GATE=BLOCKED: "
                            f"镜头 {shot.id} 缺少冻结 Shot First Frame"
                        )
                    first_frame_fingerprint = preview.first_frame_fingerprints.get(
                        shot.id
                    )
                    if first_frame_fingerprint is None:
                        raise ProductionQueueError(
                            f"镜头 {shot.id} 缺少 Shot First Frame authorization fingerprint"
                        )
                plan_options = dict(
                    production_job_id=job.id,
                    brief=brief,
                    transmitted_content_types=preview.transmitted_content_types,
                    estimated_request_count=1,
                    generation_mode=(
                        "image_to_video"
                        if first_frame_fingerprint is not None
                        else ("image_to_video" if references else "text_to_video")
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
                    continuity_strategy=self._continuity_strategy(
                        provider_profile.profile,
                        requires_first_frame=first_frame_fingerprint is not None,
                    ),
                    authorization=(
                        {
                            **frozen_authorization,
                            "shot_first_frame": first_frame_fingerprint,
                        }
                        if first_frame_fingerprint is not None
                        else frozen_authorization
                    ),
                    prompt_template_version=str(provider_profile.profile.get("prompt_template_version") or "v1"),
                )
                if preview.manifest_id:
                    plan = self.runtime_plans.create_from_selection(
                        project_id,
                        capability=UniversalCapabilityKind.VIDEO,
                        selection_service=self.settings,
                        **plan_options,
                    )
                    self._assert_runtime_plan_preview(plan, preview)
                else:
                    plan = self.runtime_plans.create(
                        project_id,
                        provider_capability=CapabilityKind.VIDEO_GENERATIVE.value,
                        provider_id=preview.provider_id,
                        model_id=preview.model_id,
                        endpoint_profile_id=preview.endpoint_profile_id,
                        deployment_region=preview.deployment_region,
                        endpoint_class=preview.endpoint_class,
                        credential_reference=provider_profile.credential_reference,
                        selection_source=preview.selection_source,
                        **plan_options,
                    )
                plan_ids[shot.id] = plan.id
        except (
            ProductionExecutionServiceError,
            ProviderProfileError,
            CapabilityUnavailable,
            RuntimeFoundationError,
            ShotKeyframeError,
        ) as exc:
            raise ProductionQueueError(str(exc)) from exc
        now = _now()
        task = self.repository.update_provider_task(
            intent.model_copy(
                update={
                    "state": "QUEUED",
                    "request_summary": {
                        **dict(intent.request_summary),
                        "production_job_id": job.id,
                        "attempt_number": 1,
                        "runtime_plan_ids_by_shot": plan_ids,
                        "provider_profile_id": provider_profile.id,
                        "endpoint_profile_id": preview.endpoint_profile_id,
                        "deployment_region": preview.deployment_region,
                        "endpoint_class": preview.endpoint_class,
                        "shot_count": preview.shot_count,
                        "max_paid_creates_per_tick": max_paid_creates_per_tick,
                        **frozen_authorization,
                    },
                    "error_message": None,
                    "updated_at": now,
                }
            )
        )
        if job.status not in {ProductionJobStatus.QUEUED, ProductionJobStatus.RUNNING}:
            self.repository.update_production_job_status(job.id, ProductionJobStatus.QUEUED, updated_at=now)
        return task

    def budget_projection(
        self,
        project_id: str,
        production_job_id: str,
        *,
        execution_id: str | None = None,
    ):
        shots = self.repository.list_production_shots(production_job_id)
        return self.paid_budgets.projection(
            project_id,
            production_job_id,
            execution_id=execution_id,
            planned_fallback=len(shots),
        )

    def resume_preparing_tasks(
        self, project_id: str | None = None
    ) -> list[ProviderTask]:
        """Finish durable UI intents interrupted before queue publication."""

        projects = (
            [project_id]
            if project_id is not None
            else [project.id for project in self.repository.list_projects()]
        )
        resumed: list[ProviderTask] = []
        for scoped_project_id in projects:
            for task in self.repository.list_provider_tasks(scoped_project_id):
                if task.execution_id is not None or task.state != "PREPARING":
                    continue
                job_id = str(
                    task.request_summary.get("production_job_id") or ""
                )
                if not job_id:
                    continue
                authorization = dict(task.request_summary)
                resumed.append(
                    self.enqueue_job(
                        scoped_project_id,
                        job_id,
                        authorization=authorization,
                        provider_id=str(
                            task.request_summary.get("provider_id")
                            or task.provider_id
                        ),
                        endpoint_profile_id=str(
                            task.request_summary.get("endpoint_profile_id") or ""
                        )
                        or None,
                    )
                )
        return resumed

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
        # ``provider_id`` is retained as a compatibility keyword only. The
        # selected manifest/profile is the sole duration authority.
        del provider_id
        raw = profile.get("supported_durations")
        values: list[float] = []
        if isinstance(raw, (list, tuple)):
            for item in raw:
                try:
                    value = float(item)
                except (TypeError, ValueError):
                    raise ProductionQueueError(
                        "Provider supported_durations 无效"
                    )
                if not math.isfinite(value) or value <= 0:
                    raise ProductionQueueError(
                        "Provider supported_durations 无效"
                    )
                values.append(value)
        if values:
            return min(values), max(values)
        if (
            "minimum_duration_seconds" not in profile
            or "maximum_duration_seconds" not in profile
        ):
            raise ProductionQueueError(
                "VIDEO manifest/profile 缺少明确 duration contract；不会使用通用 fallback"
            )
        try:
            minimum = float(profile["minimum_duration_seconds"])
            maximum = float(profile["maximum_duration_seconds"])
        except (TypeError, ValueError) as exc:
            raise ProductionQueueError("Provider duration profile 无效") from exc
        if (
            not math.isfinite(minimum)
            or not math.isfinite(maximum)
            or minimum <= 0
            or maximum < minimum
        ):
            raise ProductionQueueError("Provider duration profile 无效")
        return minimum, maximum

    @staticmethod
    def _allowed_durations(
        profile: Mapping[str, object],
        *,
        provider_id: str | None = None,
    ) -> tuple[float, ...]:
        del provider_id
        raw = profile.get("supported_durations")
        if not isinstance(raw, (list, tuple)):
            return ()
        values: list[float] = []
        for item in raw:
            try:
                value = float(item)
            except (TypeError, ValueError):
                raise ProductionQueueError("Provider supported_durations 无效")
            if not math.isfinite(value) or value <= 0:
                raise ProductionQueueError("Provider supported_durations 无效")
            values.append(value)
        return tuple(sorted(set(values)))

    @staticmethod
    def _assert_preview_profile(preview, profile) -> None:
        expected = {
            "provider_profile_id": profile.id,
            "provider_id": profile.provider_id,
            "model_id": profile.model_id,
            "endpoint_profile_id": profile.endpoint_profile_id,
            "manifest_id": str(profile.profile.get("manifest_id") or "") or None,
            "manifest_hash": str(profile.profile.get("manifest_hash") or "") or None,
            "codec_id": str(profile.profile.get("codec_id") or "") or None,
        }
        for field, value in expected.items():
            if getattr(preview, field) != value:
                raise ProductionQueueError(
                    f"授权预览后的 exact {field} 已变化；必须重新确认"
                )

    @staticmethod
    def _assert_runtime_plan_preview(plan, preview) -> None:
        manifest_id = str(plan.provider_parameters.get("manifest_id") or "") or None
        manifest_hash = str(plan.provider_parameters.get("manifest_hash") or "") or None
        codec_id = str(plan.provider_parameters.get("codec_id") or "") or None
        expected = {
            "provider_id": preview.provider_id,
            "model_id": preview.model_id,
            "endpoint_profile_id": preview.endpoint_profile_id,
            "manifest_id": preview.manifest_id,
            "manifest_hash": preview.manifest_hash,
            "codec_id": preview.codec_id,
        }
        actual = {
            "provider_id": plan.provider_id,
            "model_id": plan.model_id,
            "endpoint_profile_id": plan.endpoint_profile_id,
            "manifest_id": manifest_id,
            "manifest_hash": manifest_hash,
            "codec_id": codec_id,
        }
        if actual != expected:
            raise ProductionQueueError(
                "RuntimePlan exact Settings/manifest selection 与授权预览不一致"
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

    def _freeze_pre_live_snapshot(
        self,
        project_id: str,
        production_job_id: str,
        snapshot,
        *,
        generation_briefs,
    ):
        """Validate the sequence and freeze exact, physically checked frames."""

        try:
            report, frames_by_shot = self.shot_keyframes.require_pre_live(
                project_id, production_job_id
            )
        except (ShotKeyframeReadinessError, ShotKeyframeError) as exc:
            raise ProductionQueueError(str(exc)) from exc
        ordered_frames = tuple(
            frames_by_shot[shot_id] for shot_id in report.planned_shot_ids
        )
        briefs_by_shot = {brief.shot_id: brief for brief in generation_briefs}
        for frame in ordered_frames:
            brief = briefs_by_shot.get(frame.shot_id)
            if brief is None or frame.generation_brief_id != brief.id:
                raise ProductionQueueError(
                    "PRE_LIVE_FIRST_FRAME_GATE=BLOCKED: "
                    f"镜头 {frame.shot_id} 的 Shot First Frame 与当前 GenerationBrief 不匹配"
                )
        frozen = self.shot_keyframes.freeze_snapshot(
            snapshot,
            ordered_frames,
            required_shot_ids=report.planned_shot_ids,
        )
        fingerprints = {
            frame.shot_id: {
                "first_frame_id": frame.id,
                "artifact_id": frame.artifact_id,
                "sha256": frame.sha256,
                "source_type": frame.source_type.value,
            }
            for frame in ordered_frames
        }
        return report, frozen, fingerprints

    @staticmethod
    def _continuity_strategy(
        profile: Mapping[str, object], *, requires_first_frame: bool
    ) -> str:
        strategy = str(profile.get("continuity_strategy") or "").strip()
        if requires_first_frame and (
            not strategy or strategy.upper() == "REFERENCE_ONLY"
        ):
            return "SHOT_FIRST_FRAME_WITH_REFERENCE_CONSTRAINTS"
        return strategy or "REFERENCE_ONLY"

    def _requires_shot_first_frame(self, resolved) -> bool:
        profile = resolved.profile
        declared = bool(
            profile is not None
            and isinstance(profile.profile, Mapping)
            and (
                profile.profile.get("requires_shot_first_frame") is True
                or profile.profile.get("requires_first_frame") is True
            )
        )
        provider = self.provider_profiles.provider_for_selection(resolved)
        adapter = getattr(provider, "adapter", provider)
        return declared or bool(
            getattr(adapter, "requires_shot_first_frame", False)
        )


__all__ = ["ProductionAuthorizationPreview", "ProductionQueueError", "ProductionQueueService"]
