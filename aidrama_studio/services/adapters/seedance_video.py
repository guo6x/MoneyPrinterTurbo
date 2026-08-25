"""Volcengine Ark Seedance 2.5 video runtime adapter.

The adapter consumes only an injected immutable RuntimePlan/GenerationBrief
and one execution snapshot. It never reconstructs creative truth from the
latest database state, writes SQL, or persists provider result URLs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import requests

from aidrama_studio.domain import (
    GenerationBrief,
    OutputProfile,
    ProductionInputSnapshot,
    ReferenceAssetType,
    RuntimePlan,
)

from ..provider_result_download import (
    ProviderResultDownloader,
    ProviderResultPolicy,
    validate_image_prefix,
    validate_mp4_prefix,
)
from ..reference_assets import ReferenceAssetService
from .production_adapter import (
    ProductionRuntimeAdapter,
    RuntimeSubmission,
    RuntimeTransientError,
    parse_retry_after,
)


DEFAULT_SEEDANCE_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_SEEDANCE_MODEL = "doubao-seedance-2-5-260628"
SEEDANCE_TASK_PATH = "/contents/generations/tasks"
DEFAULT_SEEDANCE_RESULT_HOSTS = (
    "ark-content-generation-v2-cn-beijing.tos-cn-beijing.volces.com",
)
MAX_REFERENCE_IMAGE_BYTES = 10 * 1024 * 1024
MAX_REQUEST_MEDIA_BYTES = 50 * 1024 * 1024
MAX_VIDEO_BYTES = 1024 * 1024 * 1024


class SeedanceAdapterError(RuntimeError):
    """A non-secret Seedance contract or provider failure."""


class SeedanceTransientError(SeedanceAdapterError, RuntimeTransientError):
    transient = True

    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        SeedanceAdapterError.__init__(self, message)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class SeedanceProviderConfig:
    api_key: str = field(default="", repr=False)
    base_url: str = DEFAULT_SEEDANCE_BASE_URL
    model: str = DEFAULT_SEEDANCE_MODEL
    allow_paid_live_tests: bool = False
    timeout_seconds: float = 30.0
    max_reference_images: int = 9
    max_reference_image_bytes: int = MAX_REFERENCE_IMAGE_BYTES
    max_request_media_bytes: int = MAX_REQUEST_MEDIA_BYTES
    max_download_bytes: int = MAX_VIDEO_BYTES
    result_hosts: tuple[str, ...] = DEFAULT_SEEDANCE_RESULT_HOSTS

    @classmethod
    def from_environment(cls, **overrides: object) -> "SeedanceProviderConfig":
        raw_hosts = os.environ.get("SEEDANCE_RESULT_HOSTS", "")
        values: dict[str, object] = {
            "api_key": os.environ.get("ARK_API_KEY", "").strip(),
            "base_url": os.environ.get(
                "SEEDANCE_BASE_URL", DEFAULT_SEEDANCE_BASE_URL
            ).strip(),
            "model": os.environ.get("SEEDANCE_VIDEO_MODEL", DEFAULT_SEEDANCE_MODEL).strip(),
            "allow_paid_live_tests": os.environ.get(
                "AIDRAMA_ALLOW_PAID_LIVE_TESTS", ""
            )
            == "1",
            "result_hosts": tuple(
                item.strip() for item in raw_hosts.split(",") if item.strip()
            )
            or DEFAULT_SEEDANCE_RESULT_HOSTS,
        }
        values.update(overrides)
        return cls(**values)

    def validate(self, *, require_live: bool = False) -> None:
        try:
            parsed = urlsplit(self.base_url.rstrip("/"))
            port = parsed.port
        except ValueError as exc:
            raise SeedanceAdapterError("Seedance base_url 无效") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port is not None
        ):
            raise SeedanceAdapterError("Seedance base_url 必须是无凭据的 HTTPS 地址")
        if not self.model.strip():
            raise SeedanceAdapterError("Seedance model 不能为空")
        if self.timeout_seconds <= 0:
            raise SeedanceAdapterError("Seedance timeout 必须为正数")
        if not 1 <= int(self.max_reference_images) <= 20:
            raise SeedanceAdapterError("Seedance reference 数量上限无效")
        if min(
            int(self.max_reference_image_bytes),
            int(self.max_request_media_bytes),
            int(self.max_download_bytes),
        ) <= 0:
            raise SeedanceAdapterError("Seedance size limit 必须为正数")
        if require_live and (not self.api_key or not self.allow_paid_live_tests):
            raise SeedanceAdapterError("Seedance live request 需要 key 与显式付费授权")


class SeedanceInputMapper:
    """Compile the official typed-content payload from frozen product truth."""

    @classmethod
    def map_snapshot(
        cls,
        snapshot: ProductionInputSnapshot,
        config: SeedanceProviderConfig,
        *,
        runtime_plan: RuntimePlan,
        generation_brief: GenerationBrief,
        reference_service: ReferenceAssetService,
        output_profile: OutputProfile | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        cls._validate_provenance(snapshot, config, runtime_plan, generation_brief)
        shot_id = next(iter(snapshot.shot_parameters))
        prompt = cls._compile_prompt(generation_brief)
        content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
        actual_references: list[dict[str, object]] = []
        total_media_bytes = 0
        if len(runtime_plan.reference_version_ids) > config.max_reference_images:
            raise SeedanceAdapterError("冻结 reference 数量超过 Seedance 上限")
        available = {
            str(binding): str(version_id)
            for binding, version_id in snapshot.reference_asset_versions.items()
        }
        for order, version_id in enumerate(runtime_plan.reference_version_ids, start=1):
            binding_key = str(runtime_plan.reference_roles.get(version_id) or "").strip()
            if not binding_key or available.get(binding_key) != version_id:
                raise SeedanceAdapterError("RuntimePlan reference 不属于 execution snapshot")
            resolved = cls._resolve_reference(
                snapshot,
                version_id,
                binding_key,
                reference_service,
                config,
            )
            size_bytes = int(resolved["size_bytes"])
            total_media_bytes += size_bytes
            if total_media_bytes > config.max_request_media_bytes:
                raise SeedanceAdapterError("Seedance reference request 超过大小预算")
            path = resolved["path"]
            if not isinstance(path, Path):
                raise SeedanceAdapterError("Seedance reference path 无效")
            data_uri = (
                "data:"
                + str(resolved["mime_type"])
                + ";base64,"
                + base64.b64encode(path.read_bytes()).decode("ascii")
            )
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": data_uri},
                    "role": "reference_image",
                }
            )
            actual_references.append(
                {
                    "order": order,
                    "role": binding_key.split(":", 1)[0],
                    "binding_key": binding_key,
                    "reference_asset_version_id": version_id,
                    "request_media_sha256": resolved["sha256"],
                    "mime_type": resolved["mime_type"],
                    "size_bytes": size_bytes,
                }
            )

        payload: dict[str, object] = {
            "model": config.model,
            "content": content,
            "resolution": cls._provider_resolution(runtime_plan),
            "ratio": cls._aspect_ratio(runtime_plan, output_profile),
            "generate_audio": cls._generate_audio(runtime_plan),
            "watermark": bool(runtime_plan.provider_parameters.get("watermark", False)),
            "output_format": str(
                runtime_plan.provider_parameters.get("output_format") or "mp4"
            ),
            "return_last_frame": cls._return_last_frame(runtime_plan),
        }
        parameters = runtime_plan.provider_parameters
        frames = parameters.get("frames")
        if frames is not None:
            if isinstance(frames, bool):
                raise SeedanceAdapterError("Seedance frames 无效")
            try:
                frame_count = int(frames)
            except (TypeError, ValueError) as exc:
                raise SeedanceAdapterError("Seedance frames 无效") from exc
            if frame_count <= 0:
                raise SeedanceAdapterError("Seedance frames 无效")
            payload["frames"] = frame_count
        else:
            duration = float(runtime_plan.provider_generation_duration)
            payload["duration"] = int(duration) if duration.is_integer() else duration
        for key in (
            "omni_reference_task_type",
            "seed",
            "camera_fixed",
            "draft",
            "service_tier",
        ):
            if key in parameters:
                payload[key] = parameters[key]

        request_sha256 = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        trace = {
            "provider": "SEEDANCE",
            "model": config.model,
            "production_shot_id": shot_id,
            "generation_brief_id": generation_brief.id,
            "generation_brief_hash": generation_brief.sha256,
            "runtime_plan_id": runtime_plan.id,
            "runtime_plan_hash": runtime_plan.plan_hash,
            "prompt_template_version": runtime_plan.prompt_template_version,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "canonical_request_sha256": request_sha256,
            "request_parameters": {
                key: value for key, value in payload.items() if key != "content"
            },
            "snapshot_references_available": available,
            "provider_references_actually_used": actual_references,
        }
        return payload, trace

    @staticmethod
    def _validate_provenance(
        snapshot: ProductionInputSnapshot,
        config: SeedanceProviderConfig,
        plan: RuntimePlan,
        brief: GenerationBrief,
    ) -> None:
        if not isinstance(snapshot, ProductionInputSnapshot):
            raise SeedanceAdapterError("Seedance 需要 ProductionInputSnapshot")
        if len(snapshot.shot_parameters) != 1:
            raise SeedanceAdapterError("Seedance execution 必须且只能包含一个 shot")
        shot_id = next(iter(snapshot.shot_parameters))
        if (
            snapshot.runtime_plan_id != plan.id
            or snapshot.runtime_plan_hash != plan.plan_hash
            or snapshot.generation_brief_id != brief.id
            or plan.generation_brief_id != brief.id
            or plan.generation_brief_hash != brief.sha256
            or plan.project_id != snapshot.project_id
            or brief.project_id != snapshot.project_id
            or brief.shot_id != shot_id
        ):
            raise SeedanceAdapterError("Seedance frozen provenance 不匹配")
        if plan.provider_id.casefold() not in {
            "seedance",
            "seedanceproductionadapter",
        }:
            raise SeedanceAdapterError("RuntimePlan 不是 Seedance provider")
        if plan.model_id != config.model:
            raise SeedanceAdapterError("Seedance model 与冻结 RuntimePlan 不匹配")
        if plan.authorization.get("approved") is not True:
            raise SeedanceAdapterError("Seedance RuntimePlan 缺少明确付费授权")

    @staticmethod
    def _compile_prompt(brief: GenerationBrief) -> str:
        sections: list[str] = []
        if brief.character_context:
            sections.append(
                "Characters: "
                + json.dumps(brief.character_context, ensure_ascii=False, sort_keys=True)
            )
        if brief.location_context:
            sections.append(
                "Location: "
                + json.dumps(brief.location_context, ensure_ascii=False, sort_keys=True)
            )
        if brief.key_props:
            sections.append("Key props: " + ", ".join(brief.key_props))
        if brief.style:
            sections.append(
                "Style: " + json.dumps(brief.style, ensure_ascii=False, sort_keys=True)
            )
        for label, value in (
            ("Action", brief.action),
            ("Framing", brief.framing),
            ("Composition", brief.composition),
            ("Camera movement", brief.camera_movement),
            ("Lens intent", brief.lens_intent),
            (
                "Lighting",
                json.dumps(brief.lighting, ensure_ascii=False, sort_keys=True)
                if brief.lighting
                else "",
            ),
            ("Mood", brief.mood),
            ("Continuity", "; ".join(brief.continuity_constraints)),
            ("Negative constraints", "; ".join(brief.negative_constraints)),
            ("Dialogue/audio intent", brief.dialogue_audio_intent),
        ):
            if str(value).strip():
                sections.append(f"{label}: {value}")
        sections.append(
            f"Creative target duration: {brief.target_duration_seconds:g} seconds"
        )
        prompt = "\n".join(sections).strip()
        if not prompt:
            raise SeedanceAdapterError("GenerationBrief 无法编译有效 prompt")
        return prompt

    @classmethod
    def _resolve_reference(
        cls,
        snapshot: ProductionInputSnapshot,
        version_id: str,
        binding_key: str,
        service: ReferenceAssetService,
        config: SeedanceProviderConfig,
    ) -> dict[str, object]:
        version = service.repository.get_reference_asset_version(version_id)
        if version is None or version.project_id != snapshot.project_id:
            raise SeedanceAdapterError("Seedance reference version 不属于项目")
        asset = service.repository.get_reference_asset(version.asset_id)
        if asset is None or asset.project_id != snapshot.project_id:
            raise SeedanceAdapterError("Seedance reference asset 不属于项目")
        expected = {
            "CHARACTER": {ReferenceAssetType.CHARACTER_REFERENCE},
            "LOCATION": {ReferenceAssetType.LOCATION_REFERENCE},
            "STYLE": {ReferenceAssetType.STYLE_REFERENCE},
            "PROP": {ReferenceAssetType.PROP_REFERENCE},
            "SHOT": set(ReferenceAssetType),
        }.get(binding_key.split(":", 1)[0].upper())
        if expected is None or asset.asset_type not in expected:
            raise SeedanceAdapterError("Seedance reference binding type 不兼容")
        if asset.current_version_id != version.id:
            raise SeedanceAdapterError("Seedance reference 不是当前 LOCKED version")
        if version.metadata.get("source_story_revision_id") != snapshot.story_revision_id:
            raise SeedanceAdapterError("Seedance reference 已过期")
        try:
            path = service.resolve_version_path(snapshot.project_id, version.id)
        except Exception as exc:
            raise SeedanceAdapterError("Seedance reference path 无法安全解析") from exc
        if not path.is_file():
            raise SeedanceAdapterError("Seedance reference 文件不存在")
        size = path.stat().st_size
        if (
            size <= 0
            or size > config.max_reference_image_bytes
            or size != version.size_bytes
        ):
            raise SeedanceAdapterError("Seedance reference 文件大小无效")
        digest = cls._sha256(path)
        if digest != version.sha256:
            raise SeedanceAdapterError("Seedance reference SHA-256 不匹配")
        mime = cls._image_mime(path)
        if version.mime_type.split(";", 1)[0].lower() != mime:
            raise SeedanceAdapterError("Seedance reference MIME 不匹配")
        return {
            "path": path,
            "size_bytes": size,
            "sha256": digest,
            "mime_type": mime,
        }

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _image_mime(path: Path) -> str:
        with path.open("rb") as handle:
            prefix = handle.read(16)
        if prefix.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
            return "image/webp"
        raise SeedanceAdapterError("Seedance reference 图片签名无效")

    @staticmethod
    def _provider_resolution(plan: RuntimePlan) -> str:
        raw = str(plan.provider_parameters.get("provider_resolution") or "").strip()
        if raw:
            return raw
        dimensions = str(plan.resolution).lower().replace(" ", "")
        try:
            width, height = (int(item) for item in dimensions.split("x", 1))
        except (TypeError, ValueError):
            return str(plan.resolution)
        longest = max(width, height)
        if longest >= 2560:
            return "2k"
        if longest >= 1920:
            return "1080p"
        if longest >= 1280:
            return "720p"
        return "480p"

    @staticmethod
    def _aspect_ratio(plan: RuntimePlan, profile: OutputProfile | None) -> str:
        explicit = str(plan.provider_parameters.get("ratio") or "").strip()
        if explicit:
            return explicit
        if profile is not None:
            return profile.aspect_ratio
        dimensions = str(plan.resolution).lower().replace(" ", "")
        try:
            width, height = (int(item) for item in dimensions.split("x", 1))
        except (TypeError, ValueError):
            return "adaptive"
        common = {
            (16, 9): "16:9",
            (9, 16): "9:16",
            (1, 1): "1:1",
            (4, 3): "4:3",
            (3, 4): "3:4",
        }
        divisor = math.gcd(width, height)
        return common.get((width // divisor, height // divisor), "adaptive")

    @staticmethod
    def _generate_audio(plan: RuntimePlan) -> bool:
        if "generate_audio" in plan.provider_parameters:
            return bool(plan.provider_parameters["generate_audio"])
        return plan.audio_strategy.upper() == "NATIVE_PROVIDER_AUDIO"

    @staticmethod
    def _return_last_frame(plan: RuntimePlan) -> bool:
        if "return_last_frame" in plan.provider_parameters:
            return bool(plan.provider_parameters["return_last_frame"])
        return plan.continuity_strategy.upper() in {
            "PREVIOUS_LAST_FRAME",
            "FIRST_LAST_FRAME",
            "MULTIMODAL_CONTINUITY",
        }


class SeedanceProductionAdapter(ProductionRuntimeAdapter):
    name = "seedance"
    provider_id = "SEEDANCE"
    poll_interval_seconds = 10.0
    submission_uncertain_on_error = True
    STATUS_MAP = {
        "waiting": "QUEUED",
        "queued": "QUEUED",
        "submitted": "QUEUED",
        "processing": "RUNNING",
        "running": "RUNNING",
        "in_progress": "RUNNING",
        "completed": "SUCCEEDED",
        "succeeded": "SUCCEEDED",
        "success": "SUCCEEDED",
        "failed": "FAILED",
        "error": "FAILED",
        "cancelled": "CANCELLED",
        "canceled": "CANCELLED",
    }

    def __init__(
        self,
        config: SeedanceProviderConfig | None = None,
        *,
        client: Any | None = None,
        runtime_plan: RuntimePlan | None = None,
        generation_brief: GenerationBrief | None = None,
        output_profile: OutputProfile | None = None,
        reference_service: ReferenceAssetService | None = None,
        downloader: ProviderResultDownloader | None = None,
        image_downloader: ProviderResultDownloader | None = None,
    ) -> None:
        self.config = config or SeedanceProviderConfig.from_environment()
        self._client = client
        self.runtime_plan = runtime_plan
        self.generation_brief = generation_brief
        self.output_profile = output_profile
        self.reference_service = reference_service or ReferenceAssetService()
        video_policy = ProviderResultPolicy(
            self.config.result_hosts,
            self.config.max_download_bytes,
            timeout_seconds=self.config.timeout_seconds,
        )
        image_policy = ProviderResultPolicy(
            self.config.result_hosts,
            self.config.max_reference_image_bytes,
            timeout_seconds=self.config.timeout_seconds,
            accepted_content_types=(
                "image/jpeg",
                "image/png",
                "image/webp",
                "application/octet-stream",
            ),
        )
        self.downloader = downloader or ProviderResultDownloader(video_policy)
        self.image_downloader = image_downloader or ProviderResultDownloader(image_policy)
        self._trace: dict[str, dict[str, object]] = {}

    @property
    def status(self):
        from ..ai_capabilities import CapabilityKind, CapabilityStatus

        available = bool(self.config.api_key and self.config.allow_paid_live_tests)
        return CapabilityStatus(
            CapabilityKind.VIDEO_GENERATIVE,
            "SEEDANCE",
            available,
            "configured"
            if available
            else (
                "provider credential unavailable"
                if not self.config.api_key
                else "paid live authorization is required"
            ),
            {
                "model": self.config.model,
                "live_authorized": self.config.allow_paid_live_tests,
                "poll_interval_seconds": 10,
                "configured": bool(self.config.api_key),
                "deployment_region": "MAINLAND_CHINA",
                "endpoint_class": "ARK_CN_BEIJING",
                "endpoint_profile_id": "runtime:VIDEO_GENERATIVE:SEEDANCE:ARK_CN_BEIJING",
                "credential_reference": "ARK_API_KEY",
                "verification_state": "NOT_VERIFIED",
                "supports_cancel": False,
            },
            configured=bool(self.config.api_key),
            verified=False,
        )

    def map_input(
        self, snapshot: ProductionInputSnapshot
    ) -> tuple[dict[str, object], dict[str, object]]:
        if self.runtime_plan is None or self.generation_brief is None:
            raise SeedanceAdapterError(
                "Seedance adapter 缺少冻结 RuntimePlan/GenerationBrief"
            )
        return SeedanceInputMapper.map_snapshot(
            snapshot,
            self.config,
            runtime_plan=self.runtime_plan,
            generation_brief=self.generation_brief,
            reference_service=self.reference_service,
            output_profile=self.output_profile,
        )

    def validate(self, snapshot: ProductionInputSnapshot) -> bool:
        try:
            self.config.validate(require_live=False)
            self.map_input(snapshot)
            return True
        except (SeedanceAdapterError, OSError, TypeError, ValueError):
            return False

    def submit(self, snapshot: ProductionInputSnapshot) -> RuntimeSubmission:
        self.config.validate(require_live=True)
        payload, trace = self.map_input(snapshot)
        client = self._client or self._requests_client()
        response = client.post(
            self.config.base_url.rstrip("/") + SEEDANCE_TASK_PATH,
            json=payload,
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            timeout=self.config.timeout_seconds,
        )
        data = self._response_json(response)
        reference = data.get("id") or data.get("task_id") or data.get("taskId")
        if not reference:
            raise SeedanceAdapterError("Seedance response 缺少 task identity")
        trace = dict(trace)
        trace["provider_task_id"] = str(reference)
        self._trace[str(reference)] = trace
        return RuntimeSubmission(str(reference), trace)

    def get_status(self, runtime_reference: str) -> str:
        return self.map_status(self._get_task(runtime_reference).get("status"))

    def cancel(self, runtime_reference: str) -> bool:
        del runtime_reference
        raise SeedanceAdapterError(
            "已核对的 Seedance API 契约未提供安全 cancel 操作"
        )

    def get_result(self, runtime_reference: str) -> dict[str, object]:
        data = self._get_task(runtime_reference)
        if self.map_status(data.get("status")) != "SUCCEEDED":
            raise SeedanceAdapterError("Seedance result requested before task succeeded")
        content = data.get("content")
        if not isinstance(content, Mapping):
            raise SeedanceAdapterError("Seedance success response 缺少 content")
        video_url = content.get("video_url")
        if not isinstance(video_url, str) or not video_url.strip():
            raise SeedanceAdapterError("Seedance success response 缺少 video_url")
        trace = dict(self._trace.get(runtime_reference, {}))
        trace.update(
            {
                "provider": "SEEDANCE",
                "model": self.config.model,
                "provider_task_id": runtime_reference,
                "mime_type": "video/mp4",
            }
        )
        artifacts: list[dict[str, object]] = [
            {
                "stream_source": self.downloader.source(
                    video_url, prefix_validator=validate_mp4_prefix
                ),
                "filename": f"seedance-{_safe_task_name(runtime_reference)}.mp4",
                "artifact_type": "seedance-video",
                "metadata": trace,
            }
        ]
        last_frame_url = content.get("last_frame_url")
        if isinstance(last_frame_url, str) and last_frame_url.strip():
            artifacts.append(
                {
                    "stream_source": self.image_downloader.source(
                        last_frame_url,
                        prefix_validator=validate_image_prefix,
                        accept="image/*,application/octet-stream",
                    ),
                    "filename": (
                        f"seedance-{_safe_task_name(runtime_reference)}-last-frame.bin"
                    ),
                    "artifact_type": "seedance-last-frame",
                    "metadata": {
                        "provider": "SEEDANCE",
                        "model": self.config.model,
                        "provider_task_id": runtime_reference,
                        "continuity_artifact": True,
                    },
                }
            )
        return {"artifacts": artifacts}

    get_artifacts = get_result

    def _get_task(self, runtime_reference: str) -> dict[str, object]:
        if not isinstance(runtime_reference, str) or not runtime_reference.strip():
            raise SeedanceAdapterError("Seedance task identity 不能为空")
        client = self._client or self._requests_client()
        try:
            response = client.get(
                self.config.base_url.rstrip("/")
                + SEEDANCE_TASK_PATH
                + f"/{runtime_reference.strip()}",
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                timeout=self.config.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise SeedanceTransientError(
                f"Seedance polling transport failed: {type(exc).__name__}"
            ) from exc
        return self._response_json(response)

    @classmethod
    def map_status(cls, raw: object) -> str:
        value = raw.value if hasattr(raw, "value") else str(raw or "").strip().lower()
        try:
            return cls.STATUS_MAP[value.lower()]
        except KeyError as exc:
            raise SeedanceAdapterError(f"unknown Seedance status: {raw}") from exc

    @staticmethod
    def _response_json(response: Any) -> dict[str, object]:
        status = int(getattr(response, "status_code", 200))
        if status == 429 or status >= 500:
            retry_after = None
            headers = getattr(response, "headers", {})
            try:
                raw = headers.get("Retry-After") or headers.get("retry-after")
                retry_after = parse_retry_after(raw)
            except AttributeError:
                retry_after = None
            raise SeedanceTransientError(
                f"Seedance HTTP {status}", retry_after_seconds=retry_after
            )
        if status >= 400:
            raise SeedanceAdapterError(f"Seedance HTTP {status}")
        try:
            value = response.json()
        except Exception as exc:
            raise SeedanceAdapterError("Seedance response JSON 无效") from exc
        if not isinstance(value, dict):
            raise SeedanceAdapterError("Seedance response 不是 object")
        return value

    @staticmethod
    def _requests_client():
        import requests

        return requests


def _safe_task_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", str(value or "").strip())[:80] or "task"


__all__ = [
    "DEFAULT_SEEDANCE_BASE_URL",
    "DEFAULT_SEEDANCE_MODEL",
    "DEFAULT_SEEDANCE_RESULT_HOSTS",
    "SEEDANCE_TASK_PATH",
    "SeedanceAdapterError",
    "SeedanceInputMapper",
    "SeedanceProductionAdapter",
    "SeedanceProviderConfig",
    "SeedanceTransientError",
]
