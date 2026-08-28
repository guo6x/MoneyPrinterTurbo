"""Production bridge for the Mainland Universal Ark Seedance runtime.

This adapter is deliberately a thin product seam.  Settings and the frozen
``RuntimePlan`` provide the exact manifest identity; the Universal runtime
then owns protocol, codec, transport, task identity and artifact handling.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from aidrama_studio.domain import ProductionInputSnapshot, ProviderTask, RuntimePlan
from aidrama_studio.services.credentials import WindowsCredentialStore
from aidrama_studio.services.model_runtime import (
    CapabilityKind,
    CapabilityRequest,
    CapabilityResult,
    ContentAddressedArtifactSink,
    ContentRef,
    DriverSubmission,
    FrozenFileInputResolver,
    MainlandProviderRuntime,
    RuntimeOutcome,
    ArkSeedanceCodec,
)
from aidrama_studio.services.model_runtime.mainland_manifests import (
    MAINLAND_COMPATIBILITY_MANIFEST_IDS,
    build_mainland_manifests,
)
from aidrama_studio.storage.database import DatabasePaths, get_default_paths
from aidrama_studio.storage.repositories import ProjectRepository

from ..reference_assets import ReferenceAssetService
from ..shot_keyframe import ShotFirstFrameArtifactResolver
from .production_adapter import ProductionRuntimeAdapter, RuntimeSubmission
from .seedance_video import (
    DEFAULT_SEEDANCE_MODEL,
    SeedanceAdapterError,
    SeedanceProviderConfig,
)


@dataclass(frozen=True, slots=True)
class _SeedanceFirstFrameSelection:
    """Transient view of one verified ShotFirstFrame artifact."""

    first_frame_id: str
    artifact_id: str
    source_type: str
    path: Path = field(repr=False)
    mime_type: str
    sha256: str
    size_bytes: int
    shot_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def verified_bytes(self) -> bytes:
        try:
            content = self.path.read_bytes()
        except OSError as exc:
            raise MainlandSeedanceAdapterError(
                "frozen Shot First Frame cannot be read"
            ) from exc
        if len(content) <= 0 or len(content) != self.size_bytes:
            raise MainlandSeedanceAdapterError("frozen Shot First Frame size changed")
        if hashlib.sha256(content).hexdigest() != self.sha256:
            raise MainlandSeedanceAdapterError("frozen Shot First Frame SHA-256 changed")
        signatures = {
            "image/jpeg": content.startswith(b"\xff\xd8\xff"),
            "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/webp": content.startswith(b"RIFF") and content[8:12] == b"WEBP",
        }
        if not signatures.get(self.mime_type, False):
            raise MainlandSeedanceAdapterError("frozen Shot First Frame MIME/signature is invalid")
        return content

    def validate_snapshot(self, snapshot: ProductionInputSnapshot) -> str:
        if len(snapshot.shot_parameters) != 1:
            raise MainlandSeedanceAdapterError("Seedance first-frame input requires exactly one shot")
        shot_id = next(iter(snapshot.shot_parameters))
        if self.shot_id is not None and self.shot_id != shot_id:
            raise MainlandSeedanceAdapterError("Shot First Frame does not match snapshot shot")
        if shot_id not in snapshot.first_frame_required_shot_ids:
            raise MainlandSeedanceAdapterError("snapshot does not require a Shot First Frame")
        frame = snapshot.first_frame_for_shot(shot_id)
        if frame is None or (
            frame.id,
            frame.artifact_id,
            frame.source_type.value,
            frame.mime_type,
            frame.sha256,
            frame.artifact_size_bytes,
        ) != (
            self.first_frame_id,
            self.artifact_id,
            self.source_type,
            self.mime_type,
            self.sha256,
            self.size_bytes,
        ):
            raise MainlandSeedanceAdapterError("resolved Shot First Frame changed from frozen snapshot")
        return shot_id

    def safe_metadata(self) -> dict[str, object]:
        return {
            "first_frame_id": self.first_frame_id,
            "first_frame_artifact_id": self.artifact_id,
            "first_frame_sha256": self.sha256,
            "first_frame_source_type": self.source_type,
            "first_frame_mime_type": self.mime_type,
            "first_frame_size_bytes": self.size_bytes,
            **dict(self.metadata),
        }


class MainlandSeedanceAdapterError(RuntimeError):
    """A sanitized Universal Seedance bridge failure."""


class MainlandSeedanceProductionAdapter(ProductionRuntimeAdapter):
    """Wrap one frozen AIDrama shot with the exact Ark Seedance manifest."""

    name = "seedance"
    provider_id = "SEEDANCE"
    model_id = DEFAULT_SEEDANCE_MODEL
    requires_paid_budget = True
    requires_shot_first_frame = True
    poll_interval_seconds = 10.0
    submission_uncertain_on_error = True

    _STATUS_MAP = {
        RuntimeOutcome.SUBMITTED: "QUEUED",
        RuntimeOutcome.RUNNING: "RUNNING",
        RuntimeOutcome.SUCCEEDED: "SUCCEEDED",
        RuntimeOutcome.FAILED: "FAILED",
        RuntimeOutcome.CANCELLED: "CANCELLED",
        RuntimeOutcome.RECONCILIATION_REQUIRED: "RUNNING",
        RuntimeOutcome.ARTIFACT_PENDING: "RUNNING",
    }

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        paths: DatabasePaths | None = None,
        credential_store: WindowsCredentialStore | object | None = None,
        runtime_factory: Callable[..., object] = MainlandProviderRuntime,
        artifact_sink_factory: Callable[[Path], object] = ContentAddressedArtifactSink,
        runtime_plan: RuntimePlan | None = None,
        provider_task: ProviderTask | None = None,
        generation_brief: object | None = None,
        output_profile: object | None = None,
        reference_service: ReferenceAssetService | None = None,
        env: Mapping[str, str] | None = None,
        first_frame_resolver: object | None = None,
        sessions: Mapping[str, object] | None = None,
    ) -> None:
        self.paths = paths or get_default_paths()
        self.repository = repository or ProjectRepository(self.paths)
        self.credential_store = credential_store or WindowsCredentialStore(self.paths.root)
        self.runtime_factory = runtime_factory
        self.artifact_sink_factory = artifact_sink_factory
        self.runtime_plan = runtime_plan
        self.provider_task = provider_task
        self.generation_brief = generation_brief
        self.output_profile = output_profile
        self.reference_service = reference_service or ReferenceAssetService(self.repository)
        self.env = dict(os.environ if env is None else env)
        self._environment_credential = str(self.env.pop("ARK_API_KEY", "") or "").strip()
        self.first_frame_resolver = first_frame_resolver or ShotFirstFrameArtifactResolver(
            self.repository
        )
        self.sessions = dict(sessions or {})
        self._requests: dict[str, CapabilityRequest] = {}
        self._runtimes: dict[str, object] = {}
        self._sinks: dict[str, object] = {}

    @property
    def status(self):
        from aidrama_studio.services.ai_capabilities import (
            CapabilityKind as LegacyCapabilityKind,
            CapabilityStatus,
        )

        configured = self._credential_present()
        create_enabled = self.env.get("AIDRAMA_ALLOW_PAID_LIVE_TESTS") == "1"
        available = configured and create_enabled
        manifest = next(
            item
            for item in build_mainland_manifests()
            if item.id == MAINLAND_COMPATIBILITY_MANIFEST_IDS["SEEDANCE_VIDEO"]
        )
        minimum = int(manifest.duration.minimum or 0)
        maximum = int(manifest.duration.maximum or 0)
        return CapabilityStatus(
            LegacyCapabilityKind.VIDEO_GENERATIVE,
            self.provider_id,
            available,
            "configured"
            if available
            else (
                "provider credential unavailable"
                if not configured
                else "paid live authorization is required"
            ),
            {
                "model": self.model_id,
                "configured": configured,
                "credential_present": configured,
                "credential_reference": "ARK_API_KEY",
                "verification_state": "NOT_VERIFIED",
                "live_authorized": create_enabled,
                "deployment_region": "MAINLAND_CHINA",
                "endpoint_class": "ARK_CN_BEIJING",
                # This is the runtime identity projected by the manifest.
                "endpoint_profile_id": (
                    "runtime:VIDEO_GENERATIVE:SEEDANCE:ARK_CN_BEIJING"
                ),
                "requires_explicit_selection": True,
                "minimum_duration_seconds": minimum,
                "maximum_duration_seconds": maximum,
                "supported_durations": list(range(minimum, maximum + 1)),
                "requires_shot_first_frame": True,
                "supports_poll_without_paid_create_authorization": True,
            },
            configured=configured,
            verified=False,
        )

    def for_runtime_plan(
        self,
        runtime_plan: RuntimePlan,
        *,
        provider_task: ProviderTask | None = None,
    ) -> "MainlandSeedanceProductionAdapter":
        """Bind a fresh adapter to one immutable plan without secret copying."""

        child_env = dict(self.env)
        if self._environment_credential:
            child_env["ARK_API_KEY"] = self._environment_credential
        return type(self)(
            self.repository,
            paths=self.paths,
            credential_store=self.credential_store,
            runtime_factory=self.runtime_factory,
            artifact_sink_factory=self.artifact_sink_factory,
            runtime_plan=runtime_plan,
            provider_task=provider_task,
            generation_brief=self.generation_brief,
            output_profile=self.output_profile,
            reference_service=self.reference_service,
            env=child_env,
            first_frame_resolver=self.first_frame_resolver,
            sessions=self.sessions,
        )

    def validate(self, snapshot: ProductionInputSnapshot) -> bool:
        try:
            if not self._credential_present():
                return False
            if self.env.get("AIDRAMA_ALLOW_PAID_LIVE_TESTS") != "1":
                return False
            plan = self._require_plan(snapshot)
            manifest = self._manifest()
            first_frame = self._resolve_first_frame(snapshot)
            request = self._build_request(
                snapshot,
                manifest,
                first_frame=first_frame,
                create_authorized=True,
            )
            ArkSeedanceCodec(
                input_resolver=self._input_resolver(snapshot, first_frame)
            ).validate(request, manifest)
            return plan.authorization.get("approved") is True
        except (MainlandSeedanceAdapterError, OSError, TypeError, ValueError):
            return False

    def submit(self, snapshot: ProductionInputSnapshot) -> RuntimeSubmission:
        if not self.validate(snapshot):
            raise MainlandSeedanceAdapterError(
                "Mainland Seedance request is not ready for one authorized create"
            )
        credential = self._credential_value()
        if not credential:
            raise MainlandSeedanceAdapterError("ARK_API_KEY is not configured")
        sink = self.artifact_sink_factory(
            self.paths.root / "provider_artifacts" / "mainland"
        )
        manifest = self._manifest()
        first_frame = self._resolve_first_frame(snapshot)
        input_resolver = self._input_resolver(snapshot, first_frame)
        runtime_options: dict[str, object] = {
            "credentials": {"ARK_API_KEY": credential},
            "create_authorized": True,
            "artifact_sink": sink,
            "input_resolver": input_resolver,
        }
        if self.sessions:
            runtime_options["sessions"] = self.sessions
        runtime = self.runtime_factory(**runtime_options)
        request = self._build_request(
            snapshot,
            manifest,
            first_frame=first_frame,
            create_authorized=True,
        )
        result = runtime.submit(
            request,
            authorization=dict(self.runtime_plan.authorization),
        )
        if not isinstance(result, DriverSubmission):
            raise MainlandSeedanceAdapterError(
                "Seedance create did not return a task identity"
            )
        reference_id = result.protocol_reference
        self._requests[reference_id] = request
        self._runtimes[reference_id] = runtime
        self._sinks[reference_id] = sink
        request_hash = hashlib.sha256(
            json.dumps(
                request.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return RuntimeSubmission(
            runtime_reference=reference_id,
            metadata={
                "provider_task_id": result.provider_task_id or reference_id,
                "request_id": request.request_id,
                "request_sha256": request_hash,
                "manifest_id": manifest.id,
                "manifest_hash": manifest.manifest_hash,
                "model": manifest.model_id,
                "endpoint_profile_id": manifest.endpoint_profile_id,
                "deployment_region": manifest.deployment_region,
                "paid_create_retry_count": 0,
                **self._first_frame_metadata(self._resolve_first_frame(snapshot)),
            },
        )

    def get_status(self, runtime_reference: str) -> str:
        runtime, _sink, request = self._runtime_context(runtime_reference, require_input=False)
        status = runtime.poll(self._manifest().id, runtime_reference, request=request)
        try:
            return self._STATUS_MAP[status.outcome]
        except (AttributeError, KeyError) as exc:
            raise MainlandSeedanceAdapterError("Seedance returned an unknown task state") from exc

    def get_result(self, runtime_reference: str) -> dict[str, object]:
        runtime, sink, request = self._runtime_context(runtime_reference, require_input=True)
        if request is None:
            raise MainlandSeedanceAdapterError("Seedance result request context is unavailable")
        result = runtime.fetch_result(self._manifest().id, runtime_reference, request=request)
        if not isinstance(result, CapabilityResult) or not result.succeeded or not result.outputs:
            raise MainlandSeedanceAdapterError("Seedance result is not a valid artifact")
        path_for = getattr(sink, "path_for", None)
        if not callable(path_for):
            raise MainlandSeedanceAdapterError("Seedance artifact sink cannot resolve output")
        artifacts: list[dict[str, object]] = []
        for output in result.outputs:
            path = Path(path_for(output))
            if not path.is_file():
                raise MainlandSeedanceAdapterError("Seedance content-addressed artifact is unavailable")
            suffix = "mp4" if output.mime_type == "video/mp4" else "bin"
            artifacts.append(
                {
                    "path": path,
                    "filename": f"seedance-{output.sha256[:16]}.{suffix}",
                    "artifact_type": "seedance-video" if output.mime_type == "video/mp4" else "seedance-last-frame",
                    "metadata": {
                        "provider_task_id": runtime_reference,
                        "manifest_id": self._manifest().id,
                        "model": self.model_id,
                        "mime_type": output.mime_type,
                        "content_addressed": True,
                        "sha256": output.sha256,
                        "size_bytes": output.size_bytes,
                        "paid_create_retry_count": 0,
                    },
                }
            )
        return {"artifacts": artifacts}

    get_artifacts = get_result

    def cancel(self, runtime_reference: str) -> bool:
        del runtime_reference
        raise MainlandSeedanceAdapterError(
            "Seedance cancellation is not supported by the frozen V1 contract"
        )

    def _runtime_context(
        self,
        runtime_reference: str,
        *,
        require_input: bool,
    ) -> tuple[object, object, CapabilityRequest | None]:
        cached = self._runtimes.get(runtime_reference)
        if cached is not None:
            return cached, self._sinks[runtime_reference], self._requests.get(runtime_reference)
        credential = self._credential_value()
        if not credential:
            raise MainlandSeedanceAdapterError("ARK_API_KEY is not configured")
        sink = self.artifact_sink_factory(
            self.paths.root / "provider_artifacts" / "mainland"
        )
        request = None
        input_resolver = None
        snapshot = self._recovery_snapshot()
        if snapshot is not None:
            first_frame = self._resolve_first_frame(snapshot)
            input_resolver = self._input_resolver(snapshot, first_frame)
            request = self._build_request(
                snapshot,
                self._manifest(),
                first_frame=first_frame,
                create_authorized=False,
            )
        if require_input and (request is None or input_resolver is None):
            raise MainlandSeedanceAdapterError("Seedance recovery input context is unavailable")
        runtime_options: dict[str, object] = {
            "credentials": {"ARK_API_KEY": credential},
            "create_authorized": False,
            "artifact_sink": sink,
            "input_resolver": input_resolver,
        }
        if self.sessions:
            runtime_options["sessions"] = self.sessions
        runtime = self.runtime_factory(**runtime_options)
        self._runtimes[runtime_reference] = runtime
        self._sinks[runtime_reference] = sink
        if request is not None:
            self._requests[runtime_reference] = request
        return runtime, sink, request

    def _build_request(
        self,
        snapshot: ProductionInputSnapshot,
        manifest,
        *,
        first_frame: object | None = None,
        create_authorized: bool,
    ) -> CapabilityRequest:
        plan = self._require_plan(snapshot)
        if plan.model_id != manifest.model_id:
            raise MainlandSeedanceAdapterError("RuntimePlan model does not match Seedance manifest")
        first_frame = first_frame or self._resolve_first_frame(snapshot)
        try:
            frame_bytes = first_frame.verified_bytes()
        except SeedanceAdapterError as exc:
            raise MainlandSeedanceAdapterError(str(exc)) from exc
        brief = self._brief_for_plan(plan)
        prompt = self._compile_prompt(brief, snapshot)
        inputs: list[ContentRef] = [
            ContentRef(
                source_kind="SHOT_FIRST_FRAME_ARTIFACT",
                source_id=first_frame.artifact_id,
                role="first_frame",
                mime_type=first_frame.mime_type,
                sha256=first_frame.sha256,
                size_bytes=len(frame_bytes),
                metadata=self._first_frame_metadata(first_frame),
            )
        ]
        for version_id in plan.reference_version_ids:
            binding = str(plan.reference_roles.get(version_id) or "").strip()
            if not binding:
                raise MainlandSeedanceAdapterError("RuntimePlan reference role is missing")
            resolved = self._resolve_reference(snapshot, version_id, binding)
            inputs.append(
                ContentRef(
                    source_kind="REFERENCE_ASSET_VERSION",
                    source_id=version_id,
                    role=binding,
                    mime_type=str(resolved["mime_type"]),
                    sha256=str(resolved["sha256"]),
                    size_bytes=int(resolved["size_bytes"]),
                    metadata={"binding_key": binding},
                )
            )
        parameters: dict[str, object] = {
            "duration_seconds": int(round(float(plan.provider_generation_duration))),
            "resolution": str(
                plan.provider_parameters.get("provider_resolution")
                or plan.provider_parameters.get("resolution")
                or "720P"
            ),
            "aspect_ratio": str(
                plan.provider_parameters.get("ratio")
                or getattr(self.output_profile, "aspect_ratio", None)
                or "16:9"
            ),
            "generate_audio": plan.provider_parameters.get(
                "generate_audio", plan.audio_strategy.upper() == "NATIVE_PROVIDER_AUDIO"
            ),
            "watermark": plan.provider_parameters.get("watermark", False),
            "return_last_frame": plan.provider_parameters.get(
                "return_last_frame", plan.continuity_strategy.upper() != "SHOT_LOCAL"
            ),
        }
        if "seed" in plan.provider_parameters:
            parameters["seed"] = plan.provider_parameters["seed"]
        request_id = self._recovery_request_id() or uuid4().hex
        return CapabilityRequest(
            request_id=request_id,
            project_id=snapshot.project_id,
            execution_id=(self.provider_task.execution_id if self.provider_task else None),
            capability=CapabilityKind.VIDEO,
            protocol_family=manifest.protocol,
            provider_id=manifest.provider_id,
            model_id=manifest.model_id,
            manifest_id=manifest.id,
            manifest_hash=manifest.manifest_hash,
            codec_id=manifest.codec_id,
            runtime_plan_id=plan.id,
            runtime_plan_hash=plan.plan_hash,
            snapshot_hash=snapshot.runtime_plan_hash,
            inputs=tuple(inputs),
            prompt_or_text=prompt,
            provider_parameters=parameters,
            authorization_fingerprint=str(
                plan.authorization.get("authorization_fingerprint") or ""
            ),
            create_authorized=create_authorized,
            authorization_required=True,
        )

    def _manifest(self):
        plan = self.runtime_plan
        if plan is None:
            raise MainlandSeedanceAdapterError("Seedance adapter requires a frozen RuntimePlan")
        manifest_id = str(plan.provider_parameters.get("manifest_id") or "").strip()
        if manifest_id != MAINLAND_COMPATIBILITY_MANIFEST_IDS["SEEDANCE_VIDEO"]:
            raise MainlandSeedanceAdapterError("RuntimePlan does not carry the exact Seedance manifest identity")
        manifest = self._manifest_from_registry(manifest_id)
        expected_hash = str(plan.provider_parameters.get("manifest_hash") or "").strip()
        expected_codec = str(plan.provider_parameters.get("codec_id") or "").strip()
        if expected_hash != manifest.manifest_hash or expected_codec != manifest.codec_id:
            raise MainlandSeedanceAdapterError(
                "RuntimePlan Seedance manifest provenance does not match the registered manifest"
            )
        return manifest

    @staticmethod
    def _manifest_from_registry(manifest_id: str):
        from aidrama_studio.services.model_runtime import default_manifest_registry

        manifest = default_manifest_registry(include_placeholders=False).get(manifest_id)
        if manifest is None:
            raise MainlandSeedanceAdapterError("Seedance manifest is not registered")
        return manifest

    def _require_plan(self, snapshot: ProductionInputSnapshot) -> RuntimePlan:
        plan = self.runtime_plan
        if plan is None:
            raise MainlandSeedanceAdapterError("Seedance adapter requires a frozen RuntimePlan")
        if plan.project_id != snapshot.project_id or snapshot.runtime_plan_id != plan.id or snapshot.runtime_plan_hash != plan.plan_hash:
            raise MainlandSeedanceAdapterError("Seedance RuntimePlan provenance does not match snapshot")
        if plan.provider_id.casefold() != self.provider_id.casefold():
            raise MainlandSeedanceAdapterError("RuntimePlan provider is not SEEDANCE")
        if (
            plan.endpoint_profile_id
            != "runtime:VIDEO_GENERATIVE:SEEDANCE:ARK_CN_BEIJING"
            or plan.endpoint_class != "ARK_CN_BEIJING"
            or plan.deployment_region != "MAINLAND_CHINA"
            or plan.credential_reference != "ARK_API_KEY"
        ):
            raise MainlandSeedanceAdapterError(
                "RuntimePlan endpoint is not the exact Mainland Ark Seedance profile"
            )
        if plan.estimated_request_count != 1 or plan.authorization.get("max_paid_attempts", 1) != 1:
            raise MainlandSeedanceAdapterError("Seedance RuntimePlan must allow exactly one create")
        if plan.authorization.get("approved") is not True:
            raise MainlandSeedanceAdapterError("Seedance RuntimePlan lacks explicit paid authorization")
        return plan

    def _resolve_first_frame(self, snapshot: ProductionInputSnapshot):
        try:
            resolved = self.first_frame_resolver.resolve(snapshot)
        except Exception as exc:
            raise MainlandSeedanceAdapterError(str(exc)) from exc
        # The canonical resolver returns a ``ResolvedShotFirstFrame``.  A
        # duck-typed selection remains accepted for deterministic test seams.
        if hasattr(resolved, "first_frame") and hasattr(resolved, "path"):
            frame = resolved.first_frame
            resolved = _SeedanceFirstFrameSelection(
                first_frame_id=frame.id,
                artifact_id=frame.artifact_id,
                source_type=frame.source_type.value,
                path=resolved.path,
                mime_type=frame.mime_type,
                sha256=frame.sha256,
                size_bytes=frame.artifact_size_bytes,
                shot_id=frame.shot_id,
                metadata={
                    "identity_reference_version_ids": [
                        item.asset_version_id for item in frame.identity_reference_provenance
                    ],
                    "location_reference_version_ids": [
                        item.asset_version_id for item in frame.location_reference_provenance
                    ],
                    "prop_reference_version_ids": [
                        item.asset_version_id for item in frame.prop_reference_provenance
                    ],
                    "style_reference_version_ids": [
                        item.asset_version_id for item in frame.style_reference_provenance
                    ],
                },
            )
        if not all(
            callable(getattr(resolved, name, None))
            for name in ("verified_bytes", "validate_snapshot", "safe_metadata")
        ):
            raise MainlandSeedanceAdapterError(
                "Seedance resolver did not return an exact Shot First Frame"
            )
        try:
            resolved.validate_snapshot(snapshot)
        except Exception as exc:
            raise MainlandSeedanceAdapterError(str(exc)) from exc
        return resolved

    def _input_resolver(
        self,
        snapshot: ProductionInputSnapshot,
        first_frame: object,
    ) -> FrozenFileInputResolver:
        paths = {first_frame.artifact_id: first_frame.path}
        plan = self._require_plan(snapshot)
        for version_id in plan.reference_version_ids:
            binding = str(plan.reference_roles.get(version_id) or "").strip()
            resolved = self._resolve_reference(snapshot, version_id, binding)
            paths[version_id] = resolved["path"]
        return FrozenFileInputResolver(paths)

    def _resolve_reference(self, snapshot, version_id: str, binding: str) -> dict[str, object]:
        try:
            from .seedance_video import SeedanceInputMapper

            return SeedanceInputMapper._resolve_reference(
                snapshot,
                version_id,
                binding,
                self.reference_service,
                SeedanceProviderConfig(api_key=""),
            )
        except Exception as exc:
            raise MainlandSeedanceAdapterError(str(exc)) from exc

    def _brief_for_plan(self, plan):
        if self.generation_brief is not None:
            return self.generation_brief
        if not plan.generation_brief_id:
            # Legacy/unit fixtures may carry only the immutable shot snapshot.
            # The production queue always pins a GenerationBrief; retaining a
            # snapshot-only prompt fallback keeps this bridge compatible
            # without consulting mutable creative state.
            return None
        brief = self.repository.get_generation_brief(plan.generation_brief_id) if plan.generation_brief_id else None
        if brief is None:
            raise MainlandSeedanceAdapterError("Seedance RuntimePlan GenerationBrief is unavailable")
        self.generation_brief = brief
        return brief

    @staticmethod
    def _compile_prompt(brief, snapshot) -> str:
        from .seedance_video import SeedanceInputMapper

        try:
            return SeedanceInputMapper._compile_prompt(brief)
        except Exception:
            parameters = next(iter(snapshot.shot_parameters.values()), {})
            if not isinstance(parameters, Mapping):
                raise MainlandSeedanceAdapterError("Seedance prompt source is invalid")
            values = [str(parameters.get(key) or "").strip() for key in ("visual_intent", "action", "camera_movement")]
            prompt = ". ".join(value for value in values if value)
            if not prompt:
                raise MainlandSeedanceAdapterError("Seedance prompt source is empty")
            return prompt

    def _recovery_snapshot(self) -> ProductionInputSnapshot | None:
        task = self.provider_task
        if task is None or not task.execution_id:
            return None
        execution = self.repository.get_production_execution(task.execution_id)
        if execution is None:
            raise MainlandSeedanceAdapterError("Seedance recovery execution is unavailable")
        return execution.input_snapshot

    def _recovery_request_id(self) -> str | None:
        task = self.provider_task
        value = task.metadata.get("request_id") if task is not None else None
        return str(value).strip() if isinstance(value, str) and value.strip() else None

    def _credential_present(self) -> bool:
        if self._environment_credential:
            return True
        try:
            return "ARK_API_KEY" in self.credential_store.configured_providers()
        except Exception:
            return False

    def _credential_value(self) -> str:
        if self._environment_credential:
            return self._environment_credential
        try:
            value = self.credential_store.get("ARK_API_KEY")
        except Exception as exc:
            raise MainlandSeedanceAdapterError("ARK_API_KEY could not be read") from exc
        return str(value or "").strip()

    @staticmethod
    def _first_frame_metadata(frame) -> dict[str, object]:
        return frame.safe_metadata()


__all__ = ["MainlandSeedanceAdapterError", "MainlandSeedanceProductionAdapter"]
