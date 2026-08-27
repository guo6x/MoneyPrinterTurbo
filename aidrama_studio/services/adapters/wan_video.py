"""Alibaba Cloud Model Studio Wan image-to-video runtime adapter.

The adapter deliberately uses the already-installed ``requests`` client rather
than adding a provider SDK.  It translates one immutable AIDrama shot
snapshot into the DashScope asynchronous video-synthesis API and returns the
downloaded result to ``ProductionWorker`` for project-isolated persistence.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from aidrama_studio.domain import ProductionInputSnapshot, ReferenceAssetType

from ..provider_result_download import (
    ProviderResultDownloader,
    ProviderResultPolicy,
    validate_mp4_prefix,
)
from ..streaming_artifact import StreamingArtifactSource
from .production_adapter import (
    ProductionRuntimeAdapter,
    RuntimeContentRejectedError,
    RuntimeSubmission,
    RuntimeTransientError,
    parse_retry_after,
)


DEFAULT_WAN_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
# The current Model Studio image-to-video protocol supports the dated Wan 2.7
# image-to-video model.  Keep this in one configuration surface so a future
# dated model can be selected explicitly without scattering provider names.
DEFAULT_WAN_MODEL = "wan2.7-i2v-2026-04-25"
WAN_VIDEO_SYNTHESIS_PATH = "/services/aigc/video-generation/video-synthesis"
WAN_TASK_PATH = "/tasks/{task_id}"
# Wan 2.7 accepts image media up to 20 MB.  The adapter remains stricter than
# the provider for format/signature validation, but does not reject a valid
# image solely because it is between the old 10 MB and current 20 MB limits.
MAX_REFERENCE_IMAGE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 512 * 1024 * 1024
DEFAULT_WAN_RESULT_HOSTS = (
    "dashscope-result-bj.oss-cn-beijing.aliyuncs.com",
    "dashscope-result-hz.oss-cn-hangzhou.aliyuncs.com",
    "dashscope-result-sg.oss-ap-southeast-1.aliyuncs.com",
)

_WAN_CONTENT_REJECTION_CODES = frozenset({"datainspectionfailed"})


class WanAdapterError(RuntimeError):
    """Raised when Wan input, provider responses, or artifacts are invalid."""


class WanProviderHTTPError(WanAdapterError):
    """A sanitized HTTP/provider error; response bodies are never retained."""

    def __init__(self, status_code: int, code: str = "") -> None:
        self.status_code = status_code
        self.code = code
        detail = f" ({code})" if code else ""
        super().__init__(f"Wan provider HTTP {status_code}{detail}")


class WanTransientError(WanAdapterError, RuntimeTransientError):
    transient = True

    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        WanAdapterError.__init__(self, message)
        self.retry_after_seconds = retry_after_seconds


def _wan_provider_code(payload: object) -> str:
    if not isinstance(payload, Mapping):
        return ""
    containers = [payload]
    for key in ("output", "error"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            containers.append(nested)
    for container in containers:
        for key in ("code", "error_code", "errorCode"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:80]
    return ""


def _raise_wan_content_rejection(payload: object) -> None:
    code = _wan_provider_code(payload)
    normalized = code.casefold()
    if normalized in _WAN_CONTENT_REJECTION_CODES:
        raise RuntimeContentRejectedError(
            policy_stage="UNSPECIFIED", provider_code=code
        )


@dataclass(frozen=True)
class WanProviderConfig:
    """Explicit, non-secret Wan provider configuration."""

    api_key: str = field(default="", repr=False)
    base_url: str = DEFAULT_WAN_BASE_URL
    model: str = DEFAULT_WAN_MODEL
    allow_paid_live_tests: bool = False
    duration_seconds: int = 5
    # Wan 2.7 supports 720P and 1080P for this protocol.  720P is the
    # deliberately conservative default for an explicit, low-cost smoke.
    resolution: str = "720P"
    request_timeout_seconds: float = 30.0
    max_download_bytes: int = MAX_VIDEO_BYTES
    result_hosts: tuple[str, ...] = DEFAULT_WAN_RESULT_HOSTS

    @classmethod
    def from_environment(cls, **overrides: object) -> "WanProviderConfig":
        """Read the key only from the process environment.

        The key is intentionally not copied into an AIDrama config, snapshot,
        event, or artifact metadata record.
        """

        values = {
            "api_key": os.environ.get("DASHSCOPE_API_KEY", "").strip(),
            "base_url": os.environ.get("DASHSCOPE_BASE_URL", DEFAULT_WAN_BASE_URL).strip(),
            "model": os.environ.get("WAN_VIDEO_MODEL", DEFAULT_WAN_MODEL).strip(),
            "allow_paid_live_tests": os.environ.get(
                "AIDRAMA_ALLOW_PAID_LIVE_TESTS", ""
            )
            == "1",
            "result_hosts": tuple(
                item.strip()
                for item in os.environ.get("WAN_RESULT_HOSTS", "").split(",")
                if item.strip()
            )
            or DEFAULT_WAN_RESULT_HOSTS,
        }
        values.update(overrides)
        return cls(**values)

    def validate(
        self,
        *,
        require_api_key: bool = True,
        require_paid_create: bool = False,
    ) -> None:
        if require_api_key and not self.api_key.strip():
            raise WanAdapterError("DASHSCOPE_API_KEY is not configured")
        if require_paid_create and not self.allow_paid_live_tests:
            raise WanAdapterError(
                "Wan paid create requires AIDRAMA_ALLOW_PAID_LIVE_TESTS=1"
            )
        try:
            parsed = urlsplit(self.base_url.rstrip("/"))
            port = parsed.port
        except ValueError as exc:
            raise WanAdapterError("Wan base_url is invalid") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port is not None
        ):
            raise WanAdapterError("Wan base_url must be a credential-free HTTPS URL")
        if not self.model.strip():
            raise WanAdapterError("Wan model is required")
        if (
            isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, (int, float))
            or int(self.duration_seconds) != self.duration_seconds
            or not 2 <= int(self.duration_seconds) <= 15
        ):
            raise WanAdapterError("Wan duration_seconds must be between 2 and 15")
        if not isinstance(self.resolution, str) or self.resolution.upper() not in {"720P", "1080P"}:
            raise WanAdapterError("Wan resolution must be 720P or 1080P")
        if self.request_timeout_seconds <= 0:
            raise WanAdapterError("Wan request timeout must be positive")
        if self.max_download_bytes <= 0:
            raise WanAdapterError("Wan max download size must be positive")


@dataclass(frozen=True)
class WanReferenceSelection:
    """The exact frozen reference supplied to one provider task."""

    role: str
    binding_key: str
    version_id: str
    path: Path
    mime_type: str


class WanReferenceResolver:
    """Resolve a snapshot's exact locked reference without reading "latest"."""

    def __init__(self, reference_service: Any | None = None):
        if reference_service is None:
            from aidrama_studio.services.reference_assets import ReferenceAssetService

            reference_service = ReferenceAssetService()
        self.reference_service = reference_service

    def resolve(self, snapshot: ProductionInputSnapshot) -> WanReferenceSelection:
        if not isinstance(snapshot, ProductionInputSnapshot):
            raise WanAdapterError("Wan requires a ProductionInputSnapshot")
        if len(snapshot.shot_parameters) != 1:
            raise WanAdapterError("Wan image-to-video requires exactly one shot")
        shot_id, raw_parameters = next(iter(snapshot.shot_parameters.items()))
        if not isinstance(raw_parameters, Mapping):
            raise WanAdapterError("shot parameters must be a mapping")
        references = dict(snapshot.reference_asset_versions)
        subject_ids = raw_parameters.get("subject", [])
        if not isinstance(subject_ids, (list, tuple)):
            subject_ids = []
        subject_ids = [str(value).strip() for value in subject_ids if str(value).strip()]

        character_keys = [f"CHARACTER:{subject_id}" for subject_id in subject_ids]
        character_keys = [key for key in character_keys if key in references]
        if not character_keys and not subject_ids:
            character_keys = sorted(
                key for key in references if str(key).upper().startswith("CHARACTER:")
            )
        if character_keys:
            return self._resolve_version(snapshot, "character", character_keys[0], references[character_keys[0]])

        location_keys = sorted(
            key for key in references if str(key).upper().startswith("LOCATION:")
        )
        if location_keys:
            return self._resolve_version(snapshot, "location", location_keys[0], references[location_keys[0]])
        raise WanAdapterError(f"shot {shot_id} has no locked character or location reference")

    def _resolve_version(
        self,
        snapshot: ProductionInputSnapshot,
        role: str,
        binding_key: str,
        raw_version_id: object,
    ) -> WanReferenceSelection:
        version_id = str(raw_version_id or "").strip()
        if not version_id:
            raise WanAdapterError(f"{binding_key} reference version is empty")
        repository = self.reference_service.repository
        version = repository.get_reference_asset_version(version_id)
        if version is None or version.project_id != snapshot.project_id:
            raise WanAdapterError("reference version does not belong to the snapshot project")
        asset = repository.get_reference_asset(version.asset_id)
        if asset is None or asset.project_id != snapshot.project_id:
            raise WanAdapterError("reference asset does not belong to the snapshot project")
        expected_type = {
            "character": ReferenceAssetType.CHARACTER_REFERENCE,
            "location": ReferenceAssetType.LOCATION_REFERENCE,
        }[role]
        if asset.asset_type is not expected_type:
            raise WanAdapterError("reference asset type does not match the selected binding")
        if asset.current_version_id != version.id:
            raise WanAdapterError("reference version is not the locked current version")
        if version.metadata.get("source_story_revision_id") != snapshot.story_revision_id:
            raise WanAdapterError("reference version is outdated for the snapshot Story revision")
        try:
            path = self.reference_service.resolve_version_path(snapshot.project_id, version.id)
        except Exception as exc:
            raise WanAdapterError("reference version path cannot be resolved safely") from exc
        if not path.is_file():
            raise WanAdapterError("reference image file does not exist")
        file_size = path.stat().st_size
        if file_size <= 0 or file_size > MAX_REFERENCE_IMAGE_BYTES:
            raise WanAdapterError("reference image size is outside the supported limit")
        if file_size != version.size_bytes:
            raise WanAdapterError("reference image size does not match its immutable version metadata")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != version.sha256:
            raise WanAdapterError("reference image SHA-256 does not match its immutable version metadata")
        mime_type = self._validate_image(path, version.mime_type)
        return WanReferenceSelection(role, binding_key, version.id, path, mime_type)

    @staticmethod
    def _validate_image(path: Path, declared_mime: str) -> str:
        with path.open("rb") as handle:
            header = handle.read(16)
        detected = ""
        if header.startswith(b"\xff\xd8\xff"):
            detected = "image/jpeg"
        elif header.startswith(b"\x89PNG\r\n\x1a\n"):
            detected = "image/png"
        elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            detected = "image/webp"
        if not detected:
            raise WanAdapterError("reference image signature is invalid")
        if declared_mime and declared_mime.lower().split(";", 1)[0] != detected:
            raise WanAdapterError("reference image MIME does not match its signature")
        return detected


class WanPromptMapper:
    """Deterministically map structured shot fields into one Wan prompt."""

    @staticmethod
    def build(shot_parameters: Mapping[str, object]) -> str:
        def text(value: object) -> str:
            if hasattr(value, "value"):
                value = value.value
            return str(value or "").strip()

        parts: list[str] = []
        visual_intent = text(shot_parameters.get("visual_intent"))
        action = text(shot_parameters.get("action"))
        subjects = shot_parameters.get("subject", [])
        subject_text = ", ".join(text(value) for value in subjects) if isinstance(subjects, (list, tuple)) else text(subjects)
        if visual_intent:
            parts.append(visual_intent)
        if subject_text:
            parts.append(f"Subject: {subject_text}")
        if action:
            parts.append(f"Action: {action}")
        for label, key in (
            ("Framing", "shot_size"),
            ("Camera angle", "camera_angle"),
            ("Camera movement", "camera_movement"),
            ("Lens", "lens"),
            ("Movement notes", "movement_notes"),
            ("Expression", "expression"),
            ("Dialogue or narration", "dialogue_or_narration"),
        ):
            value = text(shot_parameters.get(key))
            if value:
                parts.append(f"{label}: {value}")
        lighting = shot_parameters.get("lighting")
        if isinstance(lighting, Mapping):
            lighting_parts = [
                f"{label}: {text(lighting.get(key))}"
                for label, key in (("quality", "quality"), ("direction", "direction"), ("tone", "tone"), ("notes", "notes"))
                if text(lighting.get(key))
            ]
            if lighting_parts:
                parts.append("Lighting: " + ", ".join(lighting_parts))
        prompt = ". ".join(parts).strip()
        if not prompt:
            raise WanAdapterError("shot prompt fields are empty")
        return prompt[:4000]


class WanInputMapper:
    """Build a provider payload and non-secret trace metadata."""

    @classmethod
    def map_snapshot(
        cls,
        snapshot: ProductionInputSnapshot,
        config: WanProviderConfig,
        resolver: WanReferenceResolver,
    ) -> tuple[dict[str, object], dict[str, object]]:
        config.validate(require_api_key=False)
        if snapshot.project_id.strip() == "":
            raise WanAdapterError("snapshot project_id is required")
        shot_id, raw_parameters = next(iter(snapshot.shot_parameters.items()), (None, None))
        if not isinstance(shot_id, str) or not isinstance(raw_parameters, Mapping):
            raise WanAdapterError("snapshot must contain one valid shot")
        reference = resolver.resolve(snapshot)
        prompt = WanPromptMapper.build(raw_parameters)
        duration = cls._number(raw_parameters.get("wan_duration_seconds"), config.duration_seconds)
        resolution = str(raw_parameters.get("wan_resolution") or config.resolution).strip().upper()
        if duration < 2 or duration > 15 or duration != int(duration):
            raise WanAdapterError("wan_duration_seconds must be an integer from 2 to 15")
        if resolution not in {"720P", "1080P"}:
            raise WanAdapterError("wan_resolution must be 720P or 1080P")
        image_bytes = reference.path.read_bytes()
        image_sha256 = hashlib.sha256(image_bytes).hexdigest()
        image_data = base64.b64encode(image_bytes).decode("ascii")
        image_uri = f"data:{reference.mime_type};base64,{image_data}"
        payload = {
            "model": config.model,
            # Wan 2.7's new image-to-video protocol uses a media list.  A
            # data-URI is explicitly supported for first-frame images and
            # keeps the project-isolated blob private (no public upload URL).
            "input": {
                "prompt": prompt,
                "media": [{"type": "first_frame", "url": image_uri}],
            },
            "parameters": {"duration": int(duration), "resolution": resolution},
        }
        canonical_request_sha256 = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        metadata = {
            "provider": "alibaba_model_studio",
            "provider_name": "Wan / DashScope",
            "model": config.model,
            "production_shot_id": shot_id,
            "reference_role": reference.role,
            "reference_binding_key": reference.binding_key,
            "reference_asset_version_id": reference.version_id,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "canonical_request_sha256": canonical_request_sha256,
            "duration": int(duration),
            "resolution": resolution,
            "reference_mime_type": reference.mime_type,
            "snapshot_references_available": {
                str(key): str(value)
                for key, value in snapshot.reference_asset_versions.items()
            },
            "provider_references_actually_used": [
                {
                    "order": 1,
                    "role": reference.role,
                    "binding_key": reference.binding_key,
                    "reference_asset_version_id": reference.version_id,
                    "request_media_sha256": image_sha256,
                    "mime_type": reference.mime_type,
                    "size_bytes": len(image_bytes),
                }
            ],
        }
        return payload, metadata

    @staticmethod
    def _number(value: object, default: int) -> float:
        if value is None or value == "":
            return float(default)
        if isinstance(value, bool):
            raise WanAdapterError("wan_duration_seconds must be numeric")
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise WanAdapterError("wan_duration_seconds must be numeric") from exc


class WanVideoClient:
    """Small HTTP boundary for DashScope async video synthesis."""

    def __init__(
        self,
        config: WanProviderConfig,
        *,
        session: Any | None = None,
        downloader: ProviderResultDownloader | None = None,
    ):
        self.config = config
        self.session = session or requests.Session()
        if session is None and hasattr(self.session, "trust_env"):
            self.session.trust_env = False
        self.downloader = downloader or ProviderResultDownloader(
            ProviderResultPolicy(
                config.result_hosts,
                config.max_download_bytes,
                timeout_seconds=config.request_timeout_seconds,
            ),
            session=self.session,
        )

    def create_task(self, payload: Mapping[str, object]) -> str:
        self.config.validate(require_api_key=True, require_paid_create=True)
        response = self._request("POST", WAN_VIDEO_SYNTHESIS_PATH, json_body=payload, async_header=True)
        output = response.get("output") if isinstance(response.get("output"), Mapping) else response
        task_id = output.get("task_id") or output.get("taskId") if isinstance(output, Mapping) else None
        if not task_id:
            raise WanAdapterError("Wan create task response has no task id")
        return str(task_id)

    def get_task(self, task_id: str) -> dict[str, object]:
        self.config.validate(require_api_key=True)
        if not str(task_id or "").strip():
            raise WanAdapterError("Wan task id is required")
        return self._request("GET", WAN_TASK_PATH.format(task_id=str(task_id).strip()))

    def stream_result(self, url: str) -> StreamingArtifactSource:
        """Return a process-local writer; the signed URL is never metadata."""

        return self.downloader.source(url, prefix_validator=validate_mp4_prefix)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
        async_header: bool = False,
    ) -> dict[str, object]:
        self.config.validate(require_api_key=True)
        headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}
        if async_header:
            headers["X-DashScope-Async"] = "enable"
        try:
            response = self.session.request(
                method,
                f"{self.config.base_url.rstrip('/')}{path}",
                headers=headers,
                json=dict(json_body) if json_body is not None else None,
                timeout=self.config.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise WanTransientError(
                f"Wan request failed: {type(exc).__name__}"
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            body: object = {}
            try:
                body = response.json()
            except (TypeError, ValueError):
                pass
            status_code = int(response.status_code)
            if status_code == 429 or status_code >= 500:
                retry_after = None
                try:
                    raw = response.headers.get("Retry-After") or response.headers.get(
                        "retry-after"
                    )
                    retry_after = parse_retry_after(raw)
                except AttributeError:
                    retry_after = None
                raise WanTransientError(
                    f"Wan provider HTTP {status_code}",
                    retry_after_seconds=retry_after,
                )
            _raise_wan_content_rejection(body)
            code = _wan_provider_code(body)
            raise WanProviderHTTPError(status_code, code)
        try:
            body = response.json()
        except (TypeError, ValueError) as exc:
            raise WanAdapterError("Wan provider returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise WanAdapterError("Wan provider response must be an object")
        return body


class WanProductionAdapter(ProductionRuntimeAdapter):
    """ProductionRuntimeAdapter for one Wan image-to-video shot."""

    name = "wan_video"
    requires_paid_budget = True
    poll_interval_seconds = 10.0
    submission_uncertain_on_error = True
    STATUS_MAP = {
        "PENDING": "QUEUED",
        "QUEUED": "QUEUED",
        "SUBMITTED": "QUEUED",
        "RUNNING": "RUNNING",
        "PROCESSING": "RUNNING",
        "SUCCEEDED": "SUCCEEDED",
        "SUCCESS": "SUCCEEDED",
        "COMPLETED": "SUCCEEDED",
        "FAILED": "FAILED",
        "ERROR": "FAILED",
        "CANCELLED": "CANCELLED",
        "CANCELED": "CANCELLED",
        # The API uses UNKNOWN when a task no longer exists (for example,
        # after the documented 24-hour task retention window).  It is not a
        # successful or running state, so fail truthfully while retaining the
        # runtime reference in the execution event.
        "UNKNOWN": "FAILED",
    }

    def __init__(
        self,
        client: WanVideoClient | Any | None = None,
        *,
        config: WanProviderConfig | None = None,
        reference_resolver: WanReferenceResolver | None = None,
    ) -> None:
        self.config = config or WanProviderConfig.from_environment()
        self.client = client or WanVideoClient(self.config)
        self.reference_resolver = reference_resolver or WanReferenceResolver()
        self._trace: dict[str, dict[str, object]] = {}

    def map_input(self, snapshot: ProductionInputSnapshot) -> tuple[dict[str, object], dict[str, object]]:
        return WanInputMapper.map_snapshot(snapshot, self.config, self.reference_resolver)

    @property
    def status(self):
        from ..ai_capabilities import CapabilityKind, CapabilityStatus

        configured = bool(self.config.api_key)
        available = configured and self.config.allow_paid_live_tests
        return CapabilityStatus(
            CapabilityKind.VIDEO_GENERATIVE,
            "WAN_VIDEO",
            available,
            "configured"
            if available
            else (
                "provider credential unavailable"
                if not configured
                else "paid live authorization is required"
            ),
            {
                "model": self.config.model,
                "live_authorized": self.config.allow_paid_live_tests,
                "configured": configured,
                "deployment_region": "MAINLAND_CHINA",
                "endpoint_class": "DASHSCOPE_CN",
                "endpoint_profile_id": (
                    "runtime:VIDEO_GENERATIVE:WAN_VIDEO:DASHSCOPE_CN"
                ),
                "credential_reference": "DASHSCOPE_API_KEY",
                "credential_present": configured,
                "verification_state": "NOT_VERIFIED",
                # Polling an already-created task is read-only with respect to
                # paid provider creation and must remain available after the
                # create authorization flag is removed.
                "supports_poll_without_paid_create_authorization": True,
            },
            configured=configured,
            verified=False,
        )

    def validate(self, snapshot: ProductionInputSnapshot) -> bool:
        try:
            self.map_input(snapshot)
            # Validate readiness even when a test transport is injected.  A
            # fake client must not make a production adapter appear usable
            # without the real provider credential.
            self.config.validate(require_api_key=True, require_paid_create=True)
            return True
        except (WanAdapterError, OSError, TypeError, ValueError):
            return False

    def submit(self, snapshot: ProductionInputSnapshot) -> RuntimeSubmission:
        # Fail closed before reaching either the injected or HTTP create
        # boundary. Polling an already-created task deliberately does not use
        # this paid-create gate.
        self.config.validate(require_api_key=True, require_paid_create=True)
        payload, trace = self.map_input(snapshot)
        task_id = str(self.client.create_task(payload) or "").strip()
        if not task_id:
            raise WanAdapterError("Wan create task returned an empty task id")
        trace = dict(trace)
        trace["provider_task_id"] = task_id
        self._trace[task_id] = trace
        return RuntimeSubmission(runtime_reference=task_id, metadata=trace)

    def get_status(self, runtime_reference: str) -> str:
        response = self.client.get_task(runtime_reference)
        return self.map_status(response)

    def cancel(self, runtime_reference: str) -> bool:
        # DashScope's video-synthesis task API does not expose a safe cancel
        # operation for this adapter.  Raising keeps ProductionExecution in a
        # truthful RUNNING state instead of pretending cancellation succeeded.
        raise WanAdapterError("Wan task cancellation is not supported")

    def get_result(self, runtime_reference: str) -> dict[str, object]:
        response = self.client.get_task(runtime_reference)
        if self.map_status(response) != "SUCCEEDED":
            raise WanAdapterError("Wan result requested before task succeeded")
        video_url = self._video_url(response)
        stream_source = self.client.stream_result(video_url)
        trace = dict(self._trace.get(runtime_reference, {}))
        trace.update(
            {
                "provider_task_id": runtime_reference,
                "mime_type": "video/mp4",
            }
        )
        return {
            "stream_source": stream_source,
            "filename": f"wan-{_safe_task_name(runtime_reference)}.mp4",
            "artifact_type": "wan-video",
            "metadata": trace,
        }

    get_artifacts = get_result

    @classmethod
    def map_status(cls, response: object) -> str:
        value = response
        if isinstance(response, Mapping):
            output = response.get("output")
            if isinstance(output, Mapping):
                value = output.get("task_status") or output.get("status") or output.get("state")
            value = value or response.get("task_status") or response.get("status") or response.get("state")
        if hasattr(value, "value"):
            value = value.value
        key = str(value or "").strip().upper()
        if key in {"FAILED", "ERROR"}:
            _raise_wan_content_rejection(response)
        try:
            return cls.STATUS_MAP[key]
        except KeyError as exc:
            raise WanAdapterError(f"unknown Wan task status: {value}") from exc

    @staticmethod
    def _video_url(response: Mapping[str, object]) -> str:
        output = response.get("output")
        candidate: object = output if isinstance(output, Mapping) else response
        if isinstance(candidate, Mapping):
            for key in ("video_url", "videoUrl", "url"):
                value = candidate.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            results = candidate.get("results")
            if isinstance(results, (list, tuple)) and results and isinstance(results[0], Mapping):
                value = results[0].get("video_url") or results[0].get("videoUrl") or results[0].get("url")
                if isinstance(value, str) and value.strip():
                    return value.strip()
        raise WanAdapterError("Wan success response has no video URL")

def _safe_task_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", str(value or "").strip())[:80] or "task"


__all__ = [
    "DEFAULT_WAN_BASE_URL",
    "DEFAULT_WAN_MODEL",
    "MAX_REFERENCE_IMAGE_BYTES",
    "DEFAULT_WAN_RESULT_HOSTS",
    "WanAdapterError",
    "WanProviderHTTPError",
    "WanTransientError",
    "WanProviderConfig",
    "WanReferenceSelection",
    "WanReferenceResolver",
    "WanPromptMapper",
    "WanInputMapper",
    "WanVideoClient",
    "WanProductionAdapter",
]
