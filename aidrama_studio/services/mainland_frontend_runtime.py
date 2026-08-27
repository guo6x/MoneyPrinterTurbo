"""Product-facing bridge for the Mainland universal model runtime.

The universal runtime owns provider contracts and transport.  This module
only connects those contracts to the existing Streamlit credential and
reference-candidate seams.  Constructing the bridge is offline: credentials
are read only when a user explicitly submits a paid generation action.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from uuid import uuid4

from aidrama_studio.domain import ReferenceBindingType
from aidrama_studio.services.credentials import WindowsCredentialStore
from aidrama_studio.services.model_runtime import (
    CapabilityKind,
    CapabilityRequest,
    CapabilityResult,
    ContentAddressedArtifactSink,
    MainlandProviderRuntime,
)
from aidrama_studio.services.reference_assets import ReferenceAssetService
from aidrama_studio.storage.database import DatabasePaths, get_default_paths
from aidrama_studio.storage.repositories import ProjectRepository


DASHSCOPE_CREDENTIAL_REQUIREMENT = {
    "key": "DASHSCOPE_API_KEY",
    "label": "阿里云百炼 / DashScope",
    "description": "用于中国大陆的 Z-Image 参考图与 Wan 视频生成。保存不会发起请求。",
}


class MainlandFrontendRuntimeError(RuntimeError):
    """A safe product-boundary failure with no provider response details."""


class MainlandFrontendRuntimeBridge:
    """Expose Mainland readiness and one explicit reference-image action."""

    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        paths: DatabasePaths | None = None,
        credential_store: WindowsCredentialStore | None = None,
        runtime_factory: Callable[..., object] = MainlandProviderRuntime,
        artifact_sink_factory: Callable[[Path], object] = ContentAddressedArtifactSink,
    ) -> None:
        self.paths = paths or get_default_paths()
        self.repository = repository or ProjectRepository(self.paths)
        self.credential_store = credential_store or WindowsCredentialStore(
            self.paths.root
        )
        self.runtime_factory = runtime_factory
        self.artifact_sink_factory = artifact_sink_factory

    @staticmethod
    def credential_requirements() -> tuple[dict[str, str], ...]:
        return (dict(DASHSCOPE_CREDENTIAL_REQUIREMENT),)

    def capability_snapshot(
        self, project_id: str | None = None
    ) -> dict[str, dict[str, object]]:
        """Return provider-neutral UI facts without performing network I/O."""

        del project_id
        configured = self._credential_present()
        reason = (
            "连接已安全保存；首次生成仍需逐次费用确认。"
            if configured
            else "需要先在设置中保存阿里云百炼 / DashScope 连接。"
        )
        common = {
            "configured": configured,
            "verified": False,
            "runtime_available": True,
            "create_authorized": False,
            "authorization_required": True,
            "safe_reason": reason,
        }
        return {
            "IMAGE": {
                "capability": "IMAGE",
                "model_or_profile": "Z-Image Turbo · 中国大陆",
                **common,
            },
            "VIDEO": {
                "capability": "VIDEO",
                "model_or_profile": "Wan 2.7 I2V · 中国大陆",
                **common,
            },
        }

    def handle_activity(
        self,
        project_id: str,
        operation: str,
        payload: Mapping[str, object],
    ) -> object:
        """Execute only the explicit, user-authorized reference image action."""

        if operation != "REFERENCE_IMAGE_CANDIDATE":
            raise MainlandFrontendRuntimeError("当前后台活动类型尚未连接")
        if payload.get("create_authorized") is not True:
            raise MainlandFrontendRuntimeError("生成前需要明确确认本次付费创建")

        credential = self.credential_store.get("DASHSCOPE_API_KEY")
        if not credential:
            raise MainlandFrontendRuntimeError("请先在设置中保存阿里云百炼连接")

        prompt = str(payload.get("prompt") or "").strip()
        subject_id = str(payload.get("subject_id") or "").strip()
        story_revision_id = str(
            payload.get("source_story_revision_id") or ""
        ).strip()
        try:
            binding_type = ReferenceBindingType(
                str(payload.get("binding_type") or "").strip()
            )
        except ValueError as exc:
            raise MainlandFrontendRuntimeError("参考图绑定类型无效") from exc
        if not all((project_id.strip(), prompt, subject_id, story_revision_id)):
            raise MainlandFrontendRuntimeError("参考图请求缺少必要的产品状态")

        resolution = self._image_resolution(project_id)
        sink = self.artifact_sink_factory(
            self.paths.root / "provider_artifacts" / "mainland"
        )
        runtime = self.runtime_factory(
            credentials={"DASHSCOPE_API_KEY": credential},
            create_authorized=True,
            artifact_sink=sink,
        )
        manifest = runtime.primary_manifest(CapabilityKind.IMAGE)
        request = CapabilityRequest(
            request_id=uuid4().hex,
            project_id=project_id,
            capability=CapabilityKind.IMAGE,
            protocol_family=manifest.protocol,
            provider_id=manifest.provider_id,
            model_id=manifest.model_id,
            manifest_id=manifest.id,
            manifest_hash=manifest.manifest_hash,
            codec_id=manifest.codec_id,
            prompt_or_text=prompt,
            provider_parameters={
                "resolution": resolution,
                "prompt_extend": False,
            },
            create_authorized=True,
            authorization_required=True,
        )
        result = runtime.submit(
            request,
            authorization={"approved": True, "create_authorized": True},
        )
        if not isinstance(result, CapabilityResult) or not result.succeeded:
            raise MainlandFrontendRuntimeError("Z-Image 未返回可用结果")
        if len(result.outputs) != 1:
            raise MainlandFrontendRuntimeError("Z-Image 结果数量无效")
        output = result.outputs[0]
        path_for = getattr(sink, "path_for", None)
        if not callable(path_for):
            raise MainlandFrontendRuntimeError("参考图 artifact 无法解析")
        artifact_path = Path(path_for(output))
        try:
            content = artifact_path.read_bytes()
        except OSError as exc:
            raise MainlandFrontendRuntimeError("参考图 artifact 无法读取") from exc

        service = ReferenceAssetService(self.repository)
        asset = service.ensure_workspace_asset(
            project_id,
            binding_type,
            subject_id,
        )
        return service.record_image_candidate(
            project_id,
            asset.id,
            source_story_revision_id=story_revision_id,
            provider_id=manifest.provider_id,
            model_id=manifest.model_id,
            endpoint_profile_id=manifest.endpoint_profile_id,
            deployment_region=manifest.deployment_region,
            prompt=prompt,
            content=content,
            filename=f"z-image-{output.sha256[:16]}.png",
            mime_type=output.mime_type,
            request_parameters={
                "resolution": resolution,
                "prompt_extend": False,
            },
            actor="user",
        )

    def _credential_present(self) -> bool:
        try:
            return "DASHSCOPE_API_KEY" in self.credential_store.configured_providers()
        except Exception:
            return False

    def _image_resolution(self, project_id: str) -> str:
        project = self.repository.get_project(project_id)
        if project is None:
            raise MainlandFrontendRuntimeError("项目不存在")
        aspect = str(getattr(project.aspect_ratio, "value", project.aspect_ratio))
        return {
            "16:9": "1280*720",
            "9:16": "720*1280",
        }.get(aspect, "1024*1024")


def install_mainland_frontend_runtime() -> MainlandFrontendRuntimeBridge:
    """Install the bridge into the existing provider-neutral Streamlit seams."""

    import streamlit as st

    bridge = MainlandFrontendRuntimeBridge()
    st.session_state["_aidrama_credential_requirements"] = (
        bridge.credential_requirements()
    )
    st.session_state["_aidrama_capability_source"] = bridge.capability_snapshot
    st.session_state["_aidrama_activity_adapter"] = bridge.handle_activity
    return bridge


__all__ = [
    "DASHSCOPE_CREDENTIAL_REQUIREMENT",
    "MainlandFrontendRuntimeBridge",
    "MainlandFrontendRuntimeError",
    "install_mainland_frontend_runtime",
]
