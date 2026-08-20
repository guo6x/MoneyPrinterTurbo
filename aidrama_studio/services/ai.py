from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from loguru import logger

from app.config import config
from app.models.llm_provider import get_llm_provider
from app.services import llm as mpt_llm


class AIDramaAIError(RuntimeError):
    """Safe, user-facing error from the shared MPT LLM seam."""


def snapshot_llm_config() -> dict[str, Any]:
    """Freeze the current MPT app LLM settings for one generation attempt."""
    return config.snapshot_config_with_pending(config.app)


def llm_configuration_status() -> tuple[bool, str]:
    snapshot = snapshot_llm_config()
    provider_id = str(snapshot.get("llm_provider", "")).lower()
    provider = get_llm_provider(provider_id)
    if provider is None:
        return False, "当前 Provider 不受支持"
    if provider.requires_api_key and not snapshot.get(provider.config_key("api_key"), ""):
        return False, "API Key 尚未配置"
    if provider.requires_model_name and not snapshot.get(
        provider.config_key("model_name"), provider.default_model
    ):
        return False, "模型名称尚未配置"
    base_url = snapshot.get(provider.config_key("base_url"), "")
    if provider.requires_base_url and not (base_url or provider.effective_default_base_url):
        return False, "Base URL 尚未配置"
    return True, f"{provider.default_label} 已配置"


def _secret_values(snapshot: Mapping[str, Any]) -> list[str]:
    values = []
    for key, value in snapshot.items():
        key_text = str(key).lower()
        if any(token in key_text for token in ("api_key", "token", "secret", "password")):
            if isinstance(value, str) and value:
                values.append(value)
    return values


def sanitize_error(message: str, snapshot: Mapping[str, Any] | None = None) -> str:
    safe = message.replace("Error:", "", 1).strip()
    for secret in _secret_values(snapshot or {}):
        safe = safe.replace(secret, "***")
    return safe[:1000] or "AI 服务返回了未知错误"


def generate_text(prompt: str, config_snapshot: Mapping[str, Any]) -> str:
    """
    Call the existing MoneyPrinterTurbo provider implementation.

    PRIVATE_UPSTREAM_COUPLING=KNOWN_ACCEPTED_FOR_QUICK_DEMO
    This deliberately uses MPT's private `_generate_response` seam for the quick
    product layer. A future stable adapter can replace this without changing Story.
    """
    if not prompt.strip():
        raise AIDramaAIError("AI 提示词不能为空")
    try:
        response = mpt_llm._generate_response(  # noqa: SLF001
            prompt=prompt, app_config=dict(config_snapshot)
        )
    except Exception as exc:  # the upstream seam is intentionally guarded
        logger.exception("AIDrama MPT LLM seam failed")
        raise AIDramaAIError(sanitize_error(str(exc), config_snapshot)) from exc
    if not isinstance(response, str) or not response.strip():
        raise AIDramaAIError("AI 服务返回为空")
    if response.startswith("Error:"):
        raise AIDramaAIError(sanitize_error(response, config_snapshot))
    return response.strip()
