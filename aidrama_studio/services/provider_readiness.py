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

    def __init__(self, *, env: Mapping[str, str] | None = None, llm_status=None):
        self._env = env if env is not None else os.environ
        # Injection keeps this service free of import-time config side effects in
        # tests while production uses the existing AIDrama LLM seam.
        self._llm_status = llm_status

    def _llm(self) -> CapabilityReadiness:
        if self._llm_status is not None:
            try:
                ready, detail = self._llm_status()
                return CapabilityReadiness(
                    "LLM", "Configured provider", ReadinessState.READY if ready else ReadinessState.UNAVAILABLE, detail
                )
            except Exception:
                return CapabilityReadiness("LLM", "Configured provider", ReadinessState.ERROR, "LLM 配置检查失败")
        try:
            from aidrama_studio.services.ai import llm_configuration_status

            ready, detail = llm_configuration_status()
            return CapabilityReadiness(
                "LLM", "Configured provider", ReadinessState.READY if ready else ReadinessState.UNAVAILABLE, detail
            )
        except Exception:
            return CapabilityReadiness("LLM", "Configured provider", ReadinessState.ERROR, "LLM 配置检查失败")

    def _video(self) -> CapabilityReadiness:
        if _has_value(self._env, "DASHSCOPE_API_KEY"):
            model = self._env.get("WAN_VIDEO_MODEL", "wan2.7-i2v-2026-04-25").strip()
            return CapabilityReadiness("VIDEO_GENERATIVE", "Wan / DashScope", ReadinessState.READY, f"Wan {model} 凭据已配置")
        return CapabilityReadiness("VIDEO_GENERATIVE", "Wan / DashScope", ReadinessState.UNAVAILABLE, "Wan 尚未配置凭据")

    def _stock_video(self) -> CapabilityReadiness:
        return CapabilityReadiness("VIDEO_STOCK", "本地素材运行时", ReadinessState.READY, "已有素材运行时可用")

    def _image(self) -> CapabilityReadiness:
        # No image adapter is implemented in this closure.  An unset/future
        # provider must remain visibly unavailable rather than appearing ready.
        return CapabilityReadiness("IMAGE", "未配置", ReadinessState.UNAVAILABLE, "图像生成 Provider 尚未配置")

    def _vision(self) -> CapabilityReadiness:
        return CapabilityReadiness("VISION", "未配置", ReadinessState.UNAVAILABLE, "视觉分析 Provider 尚未配置")

    def _tts(self) -> CapabilityReadiness:
        if importlib.util.find_spec("edge_tts") is not None:
            return CapabilityReadiness("TTS", "本地 Edge TTS seam", ReadinessState.READY, "本地 TTS 模块可用")
        return CapabilityReadiness("TTS", "未配置", ReadinessState.UNAVAILABLE, "TTS runtime 不可用")

    def list_capabilities(self) -> tuple[CapabilityReadiness, ...]:
        return (self._llm(), self._image(), self._video(), self._stock_video(), self._vision(), self._tts())

    def snapshot(self) -> dict[str, dict[str, str | bool]]:
        """Return a stable capability-keyed public snapshot."""

        return {item.capability: item.as_public_dict() for item in self.list_capabilities()}


__all__ = ["CapabilityReadiness", "ProviderReadinessService", "ReadinessState"]
