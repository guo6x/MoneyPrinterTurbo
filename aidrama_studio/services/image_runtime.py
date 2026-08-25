"""Profile-bound image generation boundary.

The provider adapter remains responsible for transport.  This service is the
only product runtime entry point: it resolves the exact IMAGE profile and
requires a fresh, safe disclosure before invoking an adapter.
"""

from __future__ import annotations

from typing import Mapping

from aidrama_studio.storage.repositories import ProjectRepository

from .ai_capabilities import CapabilityKind, CapabilityRegistry, ImageCandidate, ImageGenerationProvider, default_capability_registry
from .provider_profiles import ProviderDisclosure, ProviderProfileError, ProviderProfileService


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
        safe_metadata = dict(metadata or {})
        # Keep the disclosure available to the adapter as safe routing
        # provenance without ever including the prompt or credentials.
        safe_metadata["provider_disclosure"] = safe_disclosure
        return provider.generate_candidate(
            prompt,
            project_id=project_id,
            metadata=safe_metadata,
        )


__all__ = ["ImageRuntimeError", "ImageRuntimeService"]
