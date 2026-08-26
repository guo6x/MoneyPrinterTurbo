"""OpenAI image capability adapter.

The adapter uses the documented HTTP image-generation boundary without adding
an SDK dependency. It never persists credentials and never auto-locks the
resulting candidate.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Mapping

from ..ai_capabilities import (
    CapabilityKind,
    CapabilityStatus,
    CapabilityUnavailable,
    ImageCandidate,
    ImageGenerationProvider,
)


@dataclass(frozen=True)
class OpenAIImageProviderConfig:
    api_key: str = field(default="", repr=False)
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-image-2"
    timeout_seconds: int = 120
    allow_paid_live_tests: bool = False


class OpenAIImageProvider(ImageGenerationProvider):
    provider_name = "OPENAI_GPT_IMAGE"
    capability = CapabilityKind.IMAGE

    def __init__(
        self,
        config: OpenAIImageProviderConfig | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        # An explicitly supplied mapping (including an empty one) is a
        # deterministic configuration boundary; never fall through to process
        # secrets merely because the mapping has no entries.
        values = os.environ if env is None else env
        self.config = config or OpenAIImageProviderConfig(
            api_key=str(values.get("OPENAI_API_KEY", "")).strip(),
            base_url=str(values.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).strip(),
            model=str(values.get("OPENAI_IMAGE_MODEL", "gpt-image-2")).strip(),
            allow_paid_live_tests=str(values.get("AIDRAMA_ALLOW_PAID_LIVE_TESTS", "")) == "1",
        )

    @property
    def status(self) -> CapabilityStatus:
        available = bool(self.config.api_key and self.config.allow_paid_live_tests)
        if not self.config.api_key:
            reason = "OpenAI image credential unavailable"
        elif not self.config.allow_paid_live_tests:
            reason = "paid live authorization is required"
        else:
            reason = "configured"
        return CapabilityStatus(
            CapabilityKind.IMAGE,
            self.provider_name,
            available,
            reason,
            {
                "model": self.config.model,
                "live_authorized": self.config.allow_paid_live_tests,
                "configured": bool(self.config.api_key),
                "deployment_region": "INTERNATIONAL",
                "endpoint_class": "OPENAI_PUBLIC",
                "endpoint_profile_id": "runtime:IMAGE:OPENAI_IMAGE:OPENAI_PUBLIC",
                "credential_reference": "OPENAI_API_KEY",
                "credential_present": bool(self.config.api_key),
                "verification_state": "NOT_VERIFIED",
            },
            configured=bool(self.config.api_key),
            verified=False,
        )

    def generate_candidate(
        self,
        prompt: str,
        *,
        project_id: str,
        metadata: Mapping[str, object] | None = None,
    ) -> ImageCandidate:
        if not isinstance(prompt, str) or not prompt.strip():
            raise CapabilityUnavailable("image prompt 不能为空")
        if not self.status.available:
            raise CapabilityUnavailable(self.status.reason)
        # GPT Image always returns base64 image data.  ``response_format`` is
        # a DALL-E-only request field and must be omitted for this adapter.
        body = json.dumps(
            {
                "model": self.config.model,
                "prompt": prompt,
                "n": 1,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.config.base_url.rstrip("/") + "/images/generations",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise CapabilityUnavailable("OpenAI image request failed") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise CapabilityUnavailable("OpenAI image response missing data")
        item = data[0]
        raw = item.get("b64_json")
        if isinstance(raw, str) and raw:
            try:
                content = base64.b64decode(raw, validate=True)
            except Exception as exc:
                raise CapabilityUnavailable("OpenAI image response bytes invalid") from exc
        else:
            url = item.get("url")
            if not isinstance(url, str):
                raise CapabilityUnavailable("OpenAI image response has no safe image payload")
            content = self._download_result(url)
        if not content:
            raise CapabilityUnavailable("OpenAI image response is empty")
        return ImageCandidate(
            project_id=project_id,
            provider=self.provider_name,
            prompt=prompt,
            content=content,
            mime_type="image/png",
            metadata={
                **dict(metadata or {}),
                # These values describe the request actually sent.  Caller
                # metadata must never be able to replace provider provenance.
                "model": self.config.model,
                "request_parameters": {"n": 1},
            },
        )

    def _download_result(self, url: str) -> bytes:
        parsed = urllib.parse.urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port
        ):
            raise CapabilityUnavailable("provider result URL is unsafe")
        host = parsed.hostname.lower().rstrip(".")
        if not (
            host == "api.openai.com"
            or host.endswith(".openai.com")
            or host.endswith(".blob.core.windows.net")
        ):
            raise CapabilityUnavailable("provider result URL host is not allowlisted")
        try:
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            for address in addresses:
                ip = address[4][0]
                if ip.startswith(
                    (
                        "10.",
                        "127.",
                        "169.254.",
                        "192.168.",
                        "172.16.",
                        "172.17.",
                        "172.18.",
                        "172.19.",
                        "172.2",
                        "172.3",
                        "::1",
                        "fc",
                        "fd",
                    )
                ):
                    raise CapabilityUnavailable("provider result URL resolves to private address")
        except socket.gaierror as exc:
            raise CapabilityUnavailable("provider result URL cannot resolve") from exc
        request = urllib.request.Request(url, method="GET", headers={"Accept": "image/*"})
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                data = response.read(20 * 1024 * 1024 + 1)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise CapabilityUnavailable("provider result download failed") from exc
        if len(data) > 20 * 1024 * 1024:
            raise CapabilityUnavailable("provider result exceeds image size limit")
        return data
