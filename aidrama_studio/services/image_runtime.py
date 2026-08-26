"""Profile-bound image generation boundary.

The provider adapter remains responsible for transport.  This service is the
only product runtime entry point: it resolves the exact IMAGE profile and
requires a fresh, safe disclosure before invoking an adapter.
"""

from __future__ import annotations

from typing import Mapping

from aidrama_studio.domain import ReferenceImageCandidate
from aidrama_studio.storage.repositories import ProjectRepository
from aidrama_studio.storage.reference_assets import validate_image_input

from .ai_capabilities import CapabilityKind, CapabilityRegistry, ImageCandidate, ImageGenerationProvider, default_capability_registry
from .provider_profiles import ProviderDisclosure, ProviderProfileService
from .reference_assets import ReferenceAssetService, ReferenceAssetServiceError


class ImageRuntimeError(RuntimeError):
    pass


class ImageRuntimeService:
    def __init__(
        self,
        repository: ProjectRepository | None = None,
        *,
        provider: ImageGenerationProvider | None = None,
        registry: CapabilityRegistry | None = None,
        provider_profiles: ProviderProfileService | None = None,
    ) -> None:
        self.repository = repository or ProjectRepository()
        self.registry = registry or (
            CapabilityRegistry([provider])
            if provider is not None
            else default_capability_registry()
        )
        self.provider_profiles = provider_profiles or ProviderProfileService(
            self.repository, registry=self.registry
        )
        self.provider = provider or self.registry.get(CapabilityKind.IMAGE)

    def generate_candidate(
        self,
        project_id: str,
        prompt: str,
        *,
        metadata: Mapping[str, object] | None = None,
        disclosure: ProviderDisclosure | Mapping[str, object] | None = None,
    ) -> ImageCandidate:
        if self.repository.get_project(project_id) is None:
            raise ImageRuntimeError("项目不存在")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ImageRuntimeError("image prompt 不能为空")
        try:
            resolved = self.provider_profiles.resolve(
                project_id, CapabilityKind.IMAGE, require_available=True
            )
            provider = self.provider_profiles.provider_for_selection(resolved)
            safe_disclosure = self.provider_profiles.require_disclosure(
                project_id,
                CapabilityKind.IMAGE,
                disclosure,
                transmitted_content_types=("TEXT_BRIEF", "TEXT_CONSTRAINTS"),
            )
        except Exception as exc:
            raise ImageRuntimeError(
                "Provider disclosure/selection 不可用；不会调用 Image Provider"
            ) from exc
        if not isinstance(provider, ImageGenerationProvider):
            raise ImageRuntimeError("选中的 Image provider 无效")
        try:
            status_metadata = dict(getattr(provider.status, "metadata", {}) or {})
        except Exception as exc:
            raise ImageRuntimeError(
                "Image Provider readiness check failed；不会调用 Provider"
            ) from exc
        deployment_region = str(
            status_metadata.get("deployment_region") or "UNSPECIFIED"
        ).upper()
        if (
            deployment_region != "LOCAL"
            and status_metadata.get("live_authorized") is not True
        ):
            raise ImageRuntimeError(
                "Image live generation requires AIDRAMA_ALLOW_PAID_LIVE_TESTS=1"
            )
        safe_metadata = dict(metadata or {})
        # Keep the disclosure available to the adapter as safe routing
        # provenance without ever including the prompt or credentials.
        safe_metadata["provider_disclosure"] = safe_disclosure
        return provider.generate_candidate(
            prompt,
            project_id=project_id,
            metadata=safe_metadata,
        )

    def generate_and_record_candidate(
        self,
        project_id: str,
        asset_id: str,
        prompt: str,
        *,
        source_story_revision_id: str,
        filename: str,
        metadata: Mapping[str, object] | None = None,
        disclosure: ProviderDisclosure | Mapping[str, object] | None = None,
        parent_candidate_id: str | None = None,
        actor: str = "user",
        reference_assets: ReferenceAssetService | None = None,
    ) -> ReferenceImageCandidate:
        """Generate, physically validate, then durably record one Draft.

        This is the product entry point used by the reference workspace.  It
        deliberately stops at the immutable candidate boundary: promotion to
        a version and locking that version remain separate human actions.
        """

        # Freeze the exact routing disclosure before the provider call.  The
        # returned candidate must echo this value verbatim; otherwise a buggy
        # or compromised adapter could forge model/endpoint/region provenance.
        try:
            resolved = self.provider_profiles.resolve(
                project_id,
                CapabilityKind.IMAGE,
                require_available=True,
            )
            if resolved.profile is None or not resolved.available:
                raise ImageRuntimeError(resolved.detail)
            expected_disclosure = self.provider_profiles.require_disclosure(
                project_id,
                CapabilityKind.IMAGE,
                disclosure,
                transmitted_content_types=("TEXT_BRIEF", "TEXT_CONSTRAINTS"),
            )
        except ImageRuntimeError:
            raise
        except Exception as exc:
            raise ImageRuntimeError(
                "Provider disclosure/selection 不可用；不会调用 Image Provider"
            ) from exc
        try:
            generated = self.generate_candidate(
                project_id,
                prompt,
                metadata=metadata,
                disclosure=expected_disclosure,
            )
        except ImageRuntimeError:
            raise
        except Exception as exc:
            raise ImageRuntimeError("Image generation 失败；未记录 candidate") from exc
        if generated.project_id != project_id:
            raise ImageRuntimeError("Image provider 返回了错误的 project provenance")
        if generated.prompt != prompt:
            raise ImageRuntimeError("Image provider 返回的 prompt provenance 不一致")
        if not isinstance(generated.content, (bytes, bytearray, memoryview)):
            raise ImageRuntimeError("Image provider 未返回可验证的 image bytes")
        content = bytes(generated.content)
        mime_type = str(generated.mime_type or "").strip().lower()
        try:
            safe_name, normalized_mime, _ = validate_image_input(
                content,
                filename,
                mime_type,
            )
        except ValueError as exc:
            raise ImageRuntimeError(
                "Image provider 返回的文件未通过物理图片验证；不会记录 candidate"
            ) from exc

        raw_disclosure = generated.metadata.get("provider_disclosure")
        if isinstance(raw_disclosure, ProviderDisclosure):
            provider_disclosure = raw_disclosure.as_dict()
        elif isinstance(raw_disclosure, Mapping):
            provider_disclosure = dict(raw_disclosure)
        else:
            raise ImageRuntimeError("Image provider provenance 缺失；不会记录 candidate")
        if (
            provider_disclosure != expected_disclosure
            or not self.provider_profiles.validate_disclosure(
                project_id,
                CapabilityKind.IMAGE,
                provider_disclosure,
            )
        ):
            raise ImageRuntimeError(
                "Image provider provenance 与冻结 selection 不一致；不会记录 candidate"
            )
        provider_id = str(provider_disclosure.get("provider_id") or "").strip()
        model_id = str(provider_disclosure.get("model_id") or "").strip()
        endpoint_profile_id = str(
            provider_disclosure.get("endpoint_profile_id") or ""
        ).strip()
        deployment_region = str(
            provider_disclosure.get("deployment_region") or ""
        ).strip()
        if not all((provider_id, model_id, endpoint_profile_id, deployment_region)):
            raise ImageRuntimeError("Image provider provenance 不完整；不会记录 candidate")
        if generated.provider.casefold() != provider_id.casefold():
            raise ImageRuntimeError("Image provider provenance 不一致；不会记录 candidate")

        raw_parameters = generated.metadata.get("request_parameters", {})
        if not isinstance(raw_parameters, Mapping):
            raise ImageRuntimeError("Image request provenance 无效；不会记录 candidate")
        service = reference_assets or ReferenceAssetService(self.repository)
        try:
            return service.record_image_candidate(
                project_id,
                asset_id,
                source_story_revision_id=source_story_revision_id,
                provider_id=provider_id,
                model_id=model_id,
                endpoint_profile_id=endpoint_profile_id,
                deployment_region=deployment_region,
                prompt=generated.prompt,
                content=content,
                filename=safe_name,
                mime_type=normalized_mime,
                request_parameters=dict(raw_parameters),
                parent_candidate_id=parent_candidate_id,
                actor=actor,
            )
        except (ReferenceAssetServiceError, ValueError) as exc:
            raise ImageRuntimeError("Image candidate 记录失败") from exc


__all__ = ["ImageRuntimeError", "ImageRuntimeService"]
