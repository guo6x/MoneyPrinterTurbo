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

from aidrama_studio.domain import ProductionInputSnapshot

from ..provider_result_download import (
    ProviderResultDownloader,
    ProviderResultPolicy,
    validate_mp4_prefix,
)
from ..streaming_artifact import StreamingArtifactSource
from ..shot_keyframe import (
    ShotFirstFrameArtifactResolver,
    ShotKeyframeError,
)
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
MAX_FIRST_FRAME_IMAGE_BYTES = 20 * 1024 * 1024
# Compatibility constant only; the resolver no longer reads Reference Assets.
MAX_REFERENCE_IMAGE_BYTES = MAX_FIRST_FRAME_IMAGE_BYTES
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
class WanFirstFrameSelection:
    """Transient resolved view of the exact frozen Shot First Frame artifact."""

    first_frame_id: str
    artifact_id: str
    source_type: str
    path: Path = field(repr=False)
    mime_type: str
    sha256: str
    size_bytes: int
    shot_id: str | None = None
    identity_reference_version_ids: tuple[str, ...] = ()
    location_reference_version_ids: tuple[str, ...] = ()
    prop_reference_version_ids: tuple[str, ...] = ()
    style_reference_version_ids: tuple[str, ...] = ()
    previous_shot_artifact_id: str | None = None
    previous_shot_approval_id: str | None = None
    user_source_artifact_id: str | None = None
    user_source_approval_id: str | None = None
    literal_reference_override_version_id: str | None = None
    literal_reuse_authorization_id: str | None = None

    def verified_bytes(self) -> bytes:
        """Read and verify the exact frozen bytes without exposing their path."""

        try:
            content = self.path.read_bytes()
        except OSError as exc:
            raise WanAdapterError("frozen Shot First Frame cannot be read") from exc
        if not 0 < len(content) <= MAX_FIRST_FRAME_IMAGE_BYTES:
            raise WanAdapterError("frozen Shot First Frame size is invalid")
        if len(content) != self.size_bytes:
            raise WanAdapterError("frozen Shot First Frame size changed")
        digest = hashlib.sha256(content).hexdigest()
        if digest != self.sha256:
            raise WanAdapterError("frozen Shot First Frame SHA-256 changed")
        detected = ""
        if content.startswith(b"\xff\xd8\xff"):
            detected = "image/jpeg"
        elif content.startswith(b"\x89PNG\r\n\x1a\n"):
            detected = "image/png"
        elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            detected = "image/webp"
        if not detected or detected != self.mime_type:
            raise WanAdapterError("frozen Shot First Frame MIME/signature is invalid")
        if self.source_type_value not in {
            "GENERATED_KEYFRAME",
            "PREVIOUS_SHOT_LAST_FRAME",
            "USER_PROVIDED",
            "EXPLICIT_REFERENCE_OVERRIDE",
        }:
            raise WanAdapterError("frozen Shot First Frame source type is invalid")
        return content

    @property
    def source_type_value(self) -> str:
        return str(getattr(self.source_type, "value", self.source_type)).strip()

    def validate_snapshot(self, snapshot: ProductionInputSnapshot) -> str:
        """Prove this resolved selection is the snapshot's exact frozen frame."""

        if len(snapshot.shot_parameters) != 1:
            raise WanAdapterError("Wan image-to-video requires exactly one shot")
        shot_id = next(iter(snapshot.shot_parameters))
        if self.shot_id is not None and self.shot_id != shot_id:
            raise WanAdapterError("Shot First Frame does not match the snapshot shot")
        if shot_id not in snapshot.first_frame_required_shot_ids:
            raise WanAdapterError("snapshot does not require a Shot First Frame")
        frozen_frame = snapshot.first_frame_for_shot(shot_id)
        if frozen_frame is None:
            raise WanAdapterError("required frozen Shot First Frame is missing")
        frozen_identity = (
            frozen_frame.id,
            frozen_frame.artifact_id,
            frozen_frame.source_type.value,
            frozen_frame.mime_type,
            frozen_frame.sha256,
            frozen_frame.artifact_size_bytes,
        )
        selected_identity = (
            self.first_frame_id,
            self.artifact_id,
            self.source_type_value,
            self.mime_type,
            self.sha256,
            self.size_bytes,
        )
        if selected_identity != frozen_identity:
            raise WanAdapterError("resolved Shot First Frame changed from frozen snapshot")
        return shot_id

    def safe_metadata(self) -> dict[str, object]:
        """Return durable first-frame identity/provenance without transport data."""

        metadata: dict[str, object] = {
            "first_frame_id": self.first_frame_id,
            "first_frame_artifact_id": self.artifact_id,
            "first_frame_sha256": self.sha256,
            "first_frame_source_type": self.source_type_value,
            "first_frame_mime_type": self.mime_type,
            "first_frame_size_bytes": self.size_bytes,
            "identity_reference_version_ids": list(
                self.identity_reference_version_ids
            ),
            "location_reference_version_ids": list(
                self.location_reference_version_ids
            ),
            "prop_reference_version_ids": list(self.prop_reference_version_ids),
            "style_reference_version_ids": list(
                self.style_reference_version_ids
            ),
        }
        optional_ids = {
            "previous_shot_artifact_id": self.previous_shot_artifact_id,
            "previous_shot_approval_id": self.previous_shot_approval_id,
            "user_source_artifact_id": self.user_source_artifact_id,
            "user_source_approval_id": self.user_source_approval_id,
            "literal_reference_override_version_id": (
                self.literal_reference_override_version_id
            ),
            "literal_reuse_authorization_id": self.literal_reuse_authorization_id,
        }
        metadata.update(
            {key: value for key, value in optional_ids.items() if value is not None}
        )
        return metadata


class WanFirstFrameResolver:
    """Resolve the frozen first-frame artifact; never scan Reference Assets."""

    def __init__(self, artifact_resolver: ShotFirstFrameArtifactResolver | Any | None = None):
        self.artifact_resolver = artifact_resolver

    def resolve(self, snapshot: ProductionInputSnapshot) -> WanFirstFrameSelection:
        resolver = self.artifact_resolver
        if resolver is None:
            from aidrama_studio.storage.repositories import ProjectRepository

            resolver = ShotFirstFrameArtifactResolver(ProjectRepository())
            self.artifact_resolver = resolver
        try:
            resolved = resolver.resolve(snapshot)
        except ShotKeyframeError as exc:
            raise WanAdapterError(str(exc)) from exc
        frame = resolved.first_frame
        previous = frame.previous_shot_provenance
        user_source = frame.user_provided_provenance
        return WanFirstFrameSelection(
            first_frame_id=frame.id,
            artifact_id=frame.artifact_id,
            source_type=frame.source_type.value,
            path=resolved.path,
            mime_type=frame.mime_type,
            sha256=frame.sha256,
            size_bytes=frame.artifact_size_bytes,
            shot_id=frame.shot_id,
            identity_reference_version_ids=tuple(
                item.asset_version_id
                for item in frame.identity_reference_provenance
            ),
            location_reference_version_ids=tuple(
                item.asset_version_id
                for item in frame.location_reference_provenance
            ),
            prop_reference_version_ids=tuple(
                item.asset_version_id for item in frame.prop_reference_provenance
            ),
            style_reference_version_ids=tuple(
                item.asset_version_id for item in frame.style_reference_provenance
            ),
            previous_shot_artifact_id=(
                previous.source_artifact_id if previous is not None else None
            ),
            previous_shot_approval_id=(
                previous.approval_source_id if previous is not None else None
            ),
            user_source_artifact_id=(
                user_source.source_artifact_id if user_source is not None else None
            ),
            user_source_approval_id=(
                user_source.approval_source_id if user_source is not None else None
            ),
            literal_reference_override_version_id=(
                frame.literal_reference_override_version_id
            ),
            literal_reuse_authorization_id=(
                frame.literal_reuse_authorization_id
            ),
        )


# Source-compatible names now carry exact first-frame semantics.  They do not
# retain the removed Character/Location fallback.
WanReferenceSelection = WanFirstFrameSelection
WanReferenceResolver = WanFirstFrameResolver


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
        first_frame: WanFirstFrameSelection,
    ) -> tuple[dict[str, object], dict[str, object]]:
        config.validate(require_api_key=False)
        if snapshot.project_id.strip() == "":
            raise WanAdapterError("snapshot project_id is required")
        shot_id, raw_parameters = next(iter(snapshot.shot_parameters.items()), (None, None))
        if not isinstance(shot_id, str) or not isinstance(raw_parameters, Mapping):
            raise WanAdapterError("snapshot must contain one valid shot")
        if not isinstance(first_frame, WanFirstFrameSelection):
            raise WanAdapterError("Wan requires an exact WanFirstFrameSelection")
        if first_frame.validate_snapshot(snapshot) != shot_id:
            raise WanAdapterError("Shot First Frame snapshot scope is invalid")
        prompt = WanPromptMapper.build(raw_parameters)
        duration = cls._number(raw_parameters.get("wan_duration_seconds"), config.duration_seconds)
        resolution = str(raw_parameters.get("wan_resolution") or config.resolution).strip().upper()
        if duration < 2 or duration > 15 or duration != int(duration):
            raise WanAdapterError("wan_duration_seconds must be an integer from 2 to 15")
        if resolution not in {"720P", "1080P"}:
            raise WanAdapterError("wan_resolution must be 720P or 1080P")
        image_bytes = first_frame.verified_bytes()
        image_data = base64.b64encode(image_bytes).decode("ascii")
        image_uri = f"data:{first_frame.mime_type};base64,{image_data}"
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
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "canonical_request_sha256": canonical_request_sha256,
            "duration": int(duration),
            "resolution": resolution,
            **first_frame.safe_metadata(),
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
    requires_shot_first_frame = True
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
        first_frame_resolver: WanFirstFrameResolver | Any | None = None,
        reference_resolver: WanFirstFrameResolver | Any | None = None,
    ) -> None:
        if (
            first_frame_resolver is not None
            and reference_resolver is not None
            and first_frame_resolver is not reference_resolver
        ):
            raise WanAdapterError("configure only one exact first-frame resolver")
        self.config = config or WanProviderConfig.from_environment()
        self.client = client or WanVideoClient(self.config)
        # ``reference_resolver`` is a constructor-only compatibility alias.  It
        # must return WanFirstFrameSelection and cannot restore legacy fallback.
        self.first_frame_resolver = (
            first_frame_resolver
            or reference_resolver
            or WanFirstFrameResolver()
        )
        self._trace: dict[str, dict[str, object]] = {}

    def map_input(self, snapshot: ProductionInputSnapshot) -> tuple[dict[str, object], dict[str, object]]:
        first_frame = self.first_frame_resolver.resolve(snapshot)
        return WanInputMapper.map_snapshot(snapshot, self.config, first_frame)

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
                "requires_shot_first_frame": True,
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
    "MAX_FIRST_FRAME_IMAGE_BYTES",
    "MAX_REFERENCE_IMAGE_BYTES",
    "DEFAULT_WAN_RESULT_HOSTS",
    "WanAdapterError",
    "WanProviderHTTPError",
    "WanTransientError",
    "WanProviderConfig",
    "WanFirstFrameSelection",
    "WanFirstFrameResolver",
    "WanReferenceSelection",
    "WanReferenceResolver",
    "WanPromptMapper",
    "WanInputMapper",
    "WanVideoClient",
    "WanProductionAdapter",
]
