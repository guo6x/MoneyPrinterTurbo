"""Capability/provider readiness for the product settings surface.

This service intentionally reports configuration *state*, never secret values.
It does not make network calls or claim that a provider is live merely because
an adapter exists.  The resulting records are suitable for rendering in
Streamlit and for deterministic unit tests.
"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from .ai_capabilities import CapabilityKind, CapabilityRegistry, default_capability_registry


class ReadinessState(StrEnum):
    READY = "READY"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


@dataclass(frozen=True)
class CapabilityReadiness:
    capability: str
    provider: str
    state: ReadinessState
    detail: str

    @property
    def ready(self) -> bool:
        return self.state is ReadinessState.READY

    def as_public_dict(self) -> dict[str, str | bool]:
        return {
            "capability": self.capability,
            "provider": self.provider,
            "state": self.state.value,
            "detail": self.detail,
            "ready": self.ready,
        }


def _has_value(values: Mapping[str, object], key: str) -> bool:
    value = values.get(key, "")
    return isinstance(value, str) and bool(value.strip())


class ProviderReadinessService:
    """Report product capability readiness without exposing credentials."""

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        llm_status=None,
        registry: CapabilityRegistry | None = None,
    ):
        self._env = env if env is not None else os.environ
        # Injection keeps this service free of import-time config side effects in
        # tests while production uses the existing AIDrama LLM seam.
        self._llm_status = llm_status
        self._registry = registry

    def _capability_registry(self) -> CapabilityRegistry:
        # A caller may inject a registry for deterministic tests.  Otherwise
        # construct the same capability inventory used by Settings and the
        # runtime boundary.  Construction performs no network calls.
        return self._registry or default_capability_registry(env=self._env)

    def _from_registry(self, capability: CapabilityKind) -> CapabilityReadiness:
        provider = self._capability_registry().get(capability)
        if provider is None:
            return CapabilityReadiness(capability.value, "未配置", ReadinessState.UNAVAILABLE, "capability boundary unavailable")
        status = provider.status
        state = ReadinessState.READY if status.available else ReadinessState.UNAVAILABLE
        return CapabilityReadiness(
            capability.value,
            str(status.provider),
            state,
            str(status.reason or ("ready" if status.available else "unavailable")),
        )

    def _llm(self) -> CapabilityReadiness:
        if self._llm_status is not None:
            try:
                ready, detail = self._llm_status()
                return CapabilityReadiness(
                    "LLM", "Configured provider", ReadinessState.READY if ready else ReadinessState.UNAVAILABLE, detail
                )
            except Exception:
                return CapabilityReadiness("LLM", "Configured provider", ReadinessState.ERROR, "LLM 配置检查失败")
        return self._from_registry(CapabilityKind.LLM)

    def _video(self) -> CapabilityReadiness:
        return self._from_registry(CapabilityKind.VIDEO_GENERATIVE)

    def _stock_video(self) -> CapabilityReadiness:
        return self._from_registry(CapabilityKind.VIDEO_STOCK)

    def _image(self) -> CapabilityReadiness:
        return self._from_registry(CapabilityKind.IMAGE)

    def _vision(self) -> CapabilityReadiness:
        return self._from_registry(CapabilityKind.VISION)

    def _tts(self) -> CapabilityReadiness:
        return self._from_registry(CapabilityKind.TTS)

    def list_capabilities(self) -> tuple[CapabilityReadiness, ...]:
        return (self._llm(), self._image(), self._video(), self._stock_video(), self._vision(), self._tts())

    def snapshot(self) -> dict[str, dict[str, str | bool]]:
        """Return a stable capability-keyed public snapshot."""

        return {item.capability: item.as_public_dict() for item in self.list_capabilities()}


__all__ = ["CapabilityReadiness", "ProviderReadinessService", "ReadinessState"]
