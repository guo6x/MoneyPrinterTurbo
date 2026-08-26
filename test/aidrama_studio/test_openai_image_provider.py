from __future__ import annotations

import base64
import json

from aidrama_studio.services.providers.openai_image import (
    OpenAIImageProvider,
    OpenAIImageProviderConfig,
)


class _Response:
    def __init__(self, payload: dict[str, object]):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def test_gpt_image_request_body_matches_official_contract(monkeypatch):
    captured: dict[str, object] = {}

    def fake_urlopen(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response(
            {"data": [{"b64_json": base64.b64encode(b"image-bytes").decode("ascii")}]}
        )

    monkeypatch.setattr(
        "aidrama_studio.services.providers.openai_image.urllib.request.urlopen",
        fake_urlopen,
    )
    provider = OpenAIImageProvider(
        OpenAIImageProviderConfig(
            api_key="test-key",
            base_url="https://example.test/v1",
            model="gpt-image-2",
            allow_paid_live_tests=True,
            timeout_seconds=17,
        )
    )

    candidate = provider.generate_candidate(
        "A lantern in a quiet studio", project_id="project-1"
    )

    request = captured["request"]
    body = json.loads(request.data.decode("utf-8"))
    assert body == {
        "model": "gpt-image-2",
        "prompt": "A lantern in a quiet studio",
        "n": 1,
    }
    assert "response_format" not in body
    assert request.full_url == "https://example.test/v1/images/generations"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("Authorization") == "Bearer test-key"
    assert captured["timeout"] == 17
    assert candidate.content == b"image-bytes"
    assert candidate.metadata["request_parameters"] == {"n": 1}


def test_openai_image_explicit_empty_environment_does_not_fall_back_to_process_secret(
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "process-secret")
    provider = OpenAIImageProvider(env={})

    assert provider.config.api_key == ""
    assert "process-secret" not in repr(provider.config)


def test_openai_image_config_repr_does_not_include_api_key():
    config = OpenAIImageProviderConfig(api_key="secret-key")

    assert "secret-key" not in repr(config)
