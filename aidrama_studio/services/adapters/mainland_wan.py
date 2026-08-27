"""Production adapter wrapper for the Mainland universal Wan runtime.

The product queue continues to own immutable RuntimePlan and execution state.
This wrapper translates that frozen product state into one universal-runtime
request, then delegates create, poll, secure result download, and content
addressing to :class:`MainlandProviderRuntime`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
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
)
from aidrama_studio.services.model_runtime.mainland_manifests import (
    build_mainland_manifests,
)
from aidrama_studio.services.reference_assets import ReferenceAssetService
from aidrama_studio.storage.database import DatabasePaths, get_default_paths
from aidrama_studio.storage.repositories import ProjectRepository

from .production_adapter import ProductionRuntimeAdapter, RuntimeSubmission
from .wan_video import WanPromptMapper, WanReferenceResolver


class MainlandWanAdapterError(RuntimeError):
    """A sanitized product/runtime bridge failure."""


class MainlandWanProductionAdapter(ProductionRuntimeAdapter):
    """Wrap one frozen AIDrama shot with the exact Mainland Wan manifest."""

    name = "wan_video"
    provider_id = "WAN_VIDEO"
    model_id = "wan2.7-i2v-2026-04-25"
    poll_interval_seconds = 5.0
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
        credential_store: WindowsCredentialStore | None = None,
        runtime_factory: Callable[..., object] = MainlandProviderRuntime,
        artifact_sink_factory: Callable[[Path], object] = ContentAddressedArtifactSink,
        runtime_plan: RuntimePlan | None = None,
        provider_task: ProviderTask | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.paths = paths or get_default_paths()
        self.repository = repository or ProjectRepository(self.paths)
        self.credential_store = credential_store or WindowsCredentialStore(
            self.paths.root
        )
        self.runtime_factory = runtime_factory
        self.artifact_sink_factory = artifact_sink_factory
        self.runtime_plan = runtime_plan
        self.provider_task = provider_task
        self.env = dict(os.environ if env is None else env)
        self._environment_credential = str(
            self.env.pop("DASHSCOPE_API_KEY", "") or ""
        ).strip()
        self.reference_resolver = WanReferenceResolver(
            ReferenceAssetService(self.repository)
        )
        self._requests: dict[str, CapabilityRequest] = {}
        self._runtimes: dict[str, object] = {}
        self._sinks: dict[str, object] = {}

    @property
    def status(self):
        from aidrama_studio.services.ai_capabilities import (
            CapabilityKind as LegacyCapabilityKind,
        )
        from aidrama_studio.services.ai_capabilities import CapabilityStatus

        configured = self._credential_present()
        create_enabled = self.env.get("AIDRAMA_ALLOW_PAID_LIVE_TESTS") == "1"
        available = configured and create_enabled
        return CapabilityStatus(
            LegacyCapabilityKind.VIDEO_GENERATIVE,
            self.provider_id,
            available,
            (
                "configured"
                if available
                else (
                    "provider credential unavailable"
                    if not configured
                    else "paid live authorization is required"
                )
            ),
            {
                "model": self.model_id,
                "configured": configured,
                "credential_present": configured,
                "credential_reference": "DASHSCOPE_API_KEY",
                "verification_state": "NOT_VERIFIED",
                "live_authorized": create_enabled,
                "deployment_region": "MAINLAND_CHINA",
                "endpoint_class": "DASHSCOPE_CN",
                "endpoint_profile_id": "DASHSCOPE_CN_BEIJING_V1",
                "provider_resolution": "720P",
                "supported_native_resolutions": ["720P", "1080P"],
                "native_fps": 24.0,
                "minimum_duration_seconds": 2,
                "maximum_duration_seconds": 15,
                "supported_durations": list(range(2, 16)),
                "audio_strategy": "EXTERNAL_TTS",
                "continuity_strategy": "REFERENCE_ONLY",
                "prompt_template_version": "mainland-wan-i2v-v1",
                "supports_poll_without_paid_create_authorization": True,
                "paid_create_retry_count": 0,
            },
            configured=configured,
            verified=False,
        )

    def for_runtime_plan(
        self,
        runtime_plan: RuntimePlan,
        *,
        provider_task: ProviderTask | None = None,
    ) -> "MainlandWanProductionAdapter":
        """Return an exact plan-bound adapter without putting secrets in the plan."""

        child_env = dict(self.env)
        if self._environment_credential:
            child_env["DASHSCOPE_API_KEY"] = self._environment_credential
        return type(self)(
            self.repository,
            paths=self.paths,
            credential_store=self.credential_store,
            runtime_factory=self.runtime_factory,
            artifact_sink_factory=self.artifact_sink_factory,
            runtime_plan=runtime_plan,
            provider_task=provider_task,
            env=child_env,
        )

    def validate(self, snapshot: ProductionInputSnapshot) -> bool:
        try:
            if not self._credential_present():
                return False
            if self.env.get("AIDRAMA_ALLOW_PAID_LIVE_TESTS") != "1":
                return False
            plan = self._require_plan(snapshot)
            if plan.authorization.get("approved") is not True:
                return False
            manifest = self._manifest()
            self._build_request(snapshot, manifest)
            return True
        except (MainlandWanAdapterError, OSError, TypeError, ValueError):
            return False

    def submit(self, snapshot: ProductionInputSnapshot) -> RuntimeSubmission:
        if not self.validate(snapshot):
            raise MainlandWanAdapterError(
                "Mainland Wan request is not ready for one authorized create"
            )
        credential = self._credential_value()
        if not credential:
            raise MainlandWanAdapterError("DASHSCOPE_API_KEY is not configured")
        sink = self.artifact_sink_factory(
            self.paths.root / "provider_artifacts" / "mainland"
        )
        reference = self.reference_resolver.resolve(snapshot)
        input_resolver = FrozenFileInputResolver(
            {reference.version_id: reference.path}
        )
        runtime = self.runtime_factory(
            credentials={"DASHSCOPE_API_KEY": credential},
            create_authorized=True,
            artifact_sink=sink,
            input_resolver=input_resolver,
        )
        manifest = runtime.primary_manifest(CapabilityKind.VIDEO)
        request = self._build_request(snapshot, manifest)
        result = runtime.submit(
            request,
            authorization=dict(self.runtime_plan.authorization),
        )
        if not isinstance(result, DriverSubmission):
            raise MainlandWanAdapterError("Wan create did not return a task identity")
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
                "provider_references_actually_used": [
                    {
                        "order": 1,
                        "role": reference.role,
                        "binding_key": reference.binding_key,
                        "reference_asset_version_id": reference.version_id,
                        "request_media_sha256": request.inputs[0].sha256,
                        "mime_type": reference.mime_type,
                        "size_bytes": request.inputs[0].size_bytes,
                    }
                ],
            },
        )

    def get_status(self, runtime_reference: str) -> str:
        runtime, _sink, request = self._runtime_context(
            runtime_reference,
            require_input=False,
        )
        status = runtime.poll(
            self._manifest().id,
            runtime_reference,
            request=request,
        )
        try:
            return self._STATUS_MAP[status.outcome]
        except KeyError as exc:
            raise MainlandWanAdapterError("Wan returned an unknown task state") from exc

    def get_result(self, runtime_reference: str) -> dict[str, object]:
        runtime, sink, request = self._runtime_context(
            runtime_reference,
            require_input=True,
        )
        if request is None:
            raise MainlandWanAdapterError("Wan result request context is unavailable")
        result = runtime.fetch_result(
            self._manifest().id,
            runtime_reference,
            request=request,
        )
        if (
            not isinstance(result, CapabilityResult)
            or not result.succeeded
            or len(result.outputs) != 1
        ):
            raise MainlandWanAdapterError("Wan result is not a single valid artifact")
        output = result.outputs[0]
        path_for = getattr(sink, "path_for", None)
        if not callable(path_for):
            raise MainlandWanAdapterError("Wan artifact sink cannot resolve output")
        path = Path(path_for(output))
        if not path.is_file():
            raise MainlandWanAdapterError("Wan content-addressed artifact is unavailable")
        return {
            "path": path,
            "filename": f"wan-{output.sha256[:16]}.mp4",
            "artifact_type": "wan-video",
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

    get_artifacts = get_result

    def cancel(self, runtime_reference: str) -> bool:
        del runtime_reference
        raise MainlandWanAdapterError("Wan task cancellation is not supported")

    def _runtime_context(
        self,
        runtime_reference: str,
        *,
        require_input: bool,
    ) -> tuple[object, object, CapabilityRequest | None]:
        cached_runtime = self._runtimes.get(runtime_reference)
        if cached_runtime is not None:
            return (
                cached_runtime,
                self._sinks[runtime_reference],
                self._requests.get(runtime_reference),
            )
        credential = self._credential_value()
        if not credential:
            raise MainlandWanAdapterError("DASHSCOPE_API_KEY is not configured")
        sink = self.artifact_sink_factory(
            self.paths.root / "provider_artifacts" / "mainland"
        )
        request = None
        input_resolver = None
        snapshot = self._recovery_snapshot()
        if snapshot is not None:
            reference = self.reference_resolver.resolve(snapshot)
            input_resolver = FrozenFileInputResolver(
                {reference.version_id: reference.path}
            )
            request = self._build_request(snapshot, self._manifest())
        if require_input and (request is None or input_resolver is None):
            raise MainlandWanAdapterError("Wan recovery input context is unavailable")
        runtime = self.runtime_factory(
            credentials={"DASHSCOPE_API_KEY": credential},
            create_authorized=False,
            artifact_sink=sink,
            input_resolver=input_resolver,
        )
        self._runtimes[runtime_reference] = runtime
        self._sinks[runtime_reference] = sink
        if request is not None:
            self._requests[runtime_reference] = request
        return runtime, sink, request

    def _recovery_snapshot(self) -> ProductionInputSnapshot | None:
        task = self.provider_task
        if task is None or not task.execution_id:
            return None
        execution = self.repository.get_production_execution(task.execution_id)
        if execution is None:
            raise MainlandWanAdapterError("Wan recovery execution is unavailable")
        return execution.input_snapshot

    def _build_request(self, snapshot: ProductionInputSnapshot, manifest) -> CapabilityRequest:
        plan = self._require_plan(snapshot)
        if plan.model_id != manifest.model_id:
            raise MainlandWanAdapterError("RuntimePlan model does not match Wan manifest")
        reference = self.reference_resolver.resolve(snapshot)
        try:
            data = reference.path.read_bytes()
        except OSError as exc:
            raise MainlandWanAdapterError("locked reference cannot be read") from exc
        digest = hashlib.sha256(data).hexdigest()
        shot_parameters = next(iter(snapshot.shot_parameters.values()))
        if not isinstance(shot_parameters, Mapping):
            raise MainlandWanAdapterError("shot parameters are invalid")
        prompt = WanPromptMapper.build(shot_parameters)
        duration = int(round(float(plan.provider_generation_duration)))
        resolution = str(
            plan.provider_parameters.get("provider_resolution")
            or plan.provider_parameters.get("wan_resolution")
            or "720P"
        ).upper()
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
            inputs=(
                ContentRef(
                    source_kind="REFERENCE_ASSET_VERSION",
                    source_id=reference.version_id,
                    role="first_frame",
                    mime_type=reference.mime_type,
                    sha256=digest,
                    size_bytes=len(data),
                    metadata={
                        "binding_key": reference.binding_key,
                        "reference_asset_version_id": reference.version_id,
                    },
                ),
            ),
            prompt_or_text=prompt,
            provider_parameters={
                "duration_seconds": duration,
                "resolution": resolution,
            },
            authorization_fingerprint=str(
                plan.authorization.get("authorization_fingerprint") or ""
            ),
            create_authorized=True,
            authorization_required=True,
        )

    def _require_plan(self, snapshot: ProductionInputSnapshot) -> RuntimePlan:
        plan = self.runtime_plan
        if plan is None:
            raise MainlandWanAdapterError("Wan adapter requires a frozen RuntimePlan")
        if plan.project_id != snapshot.project_id:
            raise MainlandWanAdapterError("RuntimePlan does not belong to the project")
        if plan.provider_id.casefold() != self.provider_id.casefold():
            raise MainlandWanAdapterError("RuntimePlan provider is not WAN_VIDEO")
        if plan.estimated_request_count != 1:
            raise MainlandWanAdapterError("Wan RuntimePlan must allow exactly one create")
        if plan.authorization.get("max_paid_attempts", 1) != 1:
            raise MainlandWanAdapterError("Wan RuntimePlan paid attempt limit must be one")
        return plan

    def _recovery_request_id(self) -> str | None:
        task = self.provider_task
        if task is None:
            return None
        value = task.metadata.get("request_id")
        return str(value).strip() if isinstance(value, str) and value.strip() else None

    def _credential_present(self) -> bool:
        if self._environment_credential:
            return True
        try:
            return "DASHSCOPE_API_KEY" in self.credential_store.configured_providers()
        except Exception:
            return False

    def _credential_value(self) -> str:
        try:
            stored = self.credential_store.get("DASHSCOPE_API_KEY")
        except Exception:
            stored = None
        return str(stored or self._environment_credential).strip()

    @staticmethod
    def _manifest():
        return next(
            item
            for item in build_mainland_manifests(
                credential_presence={"DASHSCOPE_API_KEY": True},
                create_authorized=True,
                artifact_sink_available=True,
            )
            if item.capability is CapabilityKind.VIDEO
        )


__all__ = ["MainlandWanAdapterError", "MainlandWanProductionAdapter"]
